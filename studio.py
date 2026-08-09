#!/usr/bin/env python3
"""
Vox Director Studio — Web UI
=============================
A web interface for generating Vox-style paper-collage videos.
User gives: title, prompt, duration, aspect, theme.
Browser uses Puter.js (free, no API key) to generate beats.json with a heavy system prompt.
Backend runs the vox-director pipeline: keyframes → clips → audio → assemble.
"""
import json
import os
import subprocess
import threading
import time
import re
import sys
import shutil
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, render_template_string, send_file, make_response

BASE = Path("/opt/baal-agent/workspace")
VOX = BASE / "vox-director"
OUT = VOX / "out"
SCRIPTS = VOX / "scripts"

app = Flask(__name__)

# Job tracking
jobs = {}
job_counter = 0
job_lock = threading.Lock()

# Persistence: save jobs to disk so they survive server restarts
JOBS_DB = VOX / "jobs.json"


def save_jobs():
    """Persist jobs dict to disk (called inside job_lock)."""
    try:
        serializable = {}
        for jid, j in jobs.items():
            serializable[jid] = {k: v for k, v in j.items() if k != "cancel"}
        with open(JOBS_DB, "w") as f:
            json.dump(serializable, f, indent=2)
    except Exception as e:
        print(f"[persistence] save error: {e}")


def load_jobs():
    """Load jobs from disk on startup."""
    global jobs, job_counter
    if not JOBS_DB.exists():
        return
    try:
        with open(JOBS_DB) as f:
            data = json.load(f)
        for jid, j in data.items():
            # Recover jobs that were running when server died:
            # If final.mp4 exists, mark as done. Otherwise mark as failed.
            if j.get("status") in ("keyframes", "clips", "audio", "assemble"):
                pdir = j.get("project_dir", "")
                if pdir:
                    final_video = Path(pdir) / "final.mp4"
                    if final_video.exists() and final_video.stat().st_size > 10000:
                        j["status"] = "done"
                        j["progress"] = 100
                        j["result_video"] = str(final_video)
                        j["error"] = None
                    else:
                        j["status"] = "failed"
                        j["error"] = "Server restarted during processing"
                else:
                    j["status"] = "failed"
                    j["error"] = "Server restarted during processing"
            j["cancel"] = False
            jobs[jid] = j
        # Set counter past highest existing job number
        max_num = 0
        for jid in jobs:
            try:
                num = int(jid.replace("job_", ""))
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
        job_counter = max_num + 1
        print(f"[persistence] Loaded {len(jobs)} jobs from disk")
    except Exception as e:
        print(f"[persistence] load error: {e}")


# Load existing jobs on startup
load_jobs()


def slugify(text):
    s = re.sub(r'[^a-zA-Z0-9\s-]', '', text.lower())
    s = re.sub(r'[\s-]+', '-', s).strip('-')
    return s[:40] if s else "video"


def _count_shots(project_dir):
    """Count total shots from beats.json for progress estimation."""
    beats_path = project_dir / "beats.json"
    if not beats_path.exists():
        return 0
    try:
        data = json.loads(beats_path.read_text())
        return sum(len(b.get("shots", [])) for b in data.get("beats", []))
    except Exception:
        return 0


def _update_sub_progress(job_id, line, step_name, base_pct, span_pct, project_dir):
    """Update sub-step progress based on output line content."""
    total = _count_shots(project_dir)
    if total == 0:
        return

    # keyframes: lines like "[1a] done" or "[1a] saved"
    if step_name == "keyframes":
        import re as _re
        m = _re.search(r'\[(\d+[ab])\]\s*(?:done|saved)', line)
        if m:
            # Count how many shots are done by checking output dir
            kf_dir = Path(project_dir) / "keyframes"
            done = len(list(kf_dir.glob("*.jpg"))) if kf_dir.exists() else 0
            frac = min(done / total, 1.0)
            pct = base_pct + int(frac * span_pct)
            with job_lock:
                if job_id in jobs and jobs[job_id].get("status") == step_name:
                    jobs[job_id]["progress"] = max(jobs[job_id].get("progress", 0), pct)

    # clips: lines like "[1a] done" or "[1a] saved"
    elif step_name == "clips":
        import re as _re
        m = _re.search(r'\[(\d+[ab])\]\s*(?:done|saved)', line)
        if m:
            clips_dir = Path(project_dir) / "clips"
            done = len(list(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
            frac = min(done / total, 1.0)
            pct = base_pct + int(frac * span_pct)
            with job_lock:
                if job_id in jobs and jobs[job_id].get("status") == step_name:
                    jobs[job_id]["progress"] = max(jobs[job_id].get("progress", 0), pct)


def _watcher_loop(job_id, project_dir, step_name, base_pct, span_pct, stop_event):
    """Background thread that watches output files and updates sub-progress."""
    import time as _time
    project_dir = Path(project_dir)
    start_time = _time.time()
    # Estimated durations (seconds) per step for time-based fallback
    # Scale by shot count: long videos (120 beats × 2 = 240 shots) need much more time
    try:
        with open(project_dir / "beats.json") as f:
            _doc = json.load(f)
        _n_shots = sum(len(b.get("shots") or [b]) for b in _doc.get("beats", []))
    except Exception:
        _n_shots = 20
    _scale = max(1.0, _n_shots / 20)
    est_durations = {
        "keyframes": int(180 * _scale),
        "clips": int(240 * _scale),
        "audio": int(30 * _scale),
        "assemble": int(45 * _scale),
    }

    def update(pct):
        with job_lock:
            if job_id in jobs:
                if jobs[job_id].get("status") == step_name:
                    jobs[job_id]["progress"] = max(jobs[job_id].get("progress", 0), pct)

    total_shots = _count_shots(project_dir)
    last_file_pct = 0

    while not stop_event.is_set():
        file_pct = 0
        if step_name == "keyframes":
            kf_dir = project_dir / "keyframes"
            done = len(list(kf_dir.glob("*.jpg"))) if kf_dir.exists() else 0
            frac = min(done / total_shots, 1.0) if total_shots else 0
            file_pct = base_pct + int(frac * span_pct)
        elif step_name == "clips":
            clips_dir = project_dir / "clips"
            done = len(list(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
            frac = min(done / total_shots, 1.0) if total_shots else 0
            file_pct = base_pct + int(frac * span_pct)
        elif step_name == "audio":
            audio_dir = project_dir / "audio"
            vo = audio_dir / "voiceover.mp3" if audio_dir.exists() else None
            bgm = audio_dir / "bgm.mp3" if audio_dir.exists() else None
            done = sum(1 for f in [vo, bgm] if f and f.exists())
            frac = done / 2.0
            file_pct = base_pct + int(frac * span_pct)
        elif step_name == "assemble":
            if (project_dir / "final.mp4").exists():
                file_pct = base_pct + span_pct

        # Time-based fallback: if file progress is stuck at 0, estimate from elapsed time
        # This handles parallel image generation where all files appear at once
        if file_pct <= base_pct:
            elapsed = _time.time() - start_time
            est = est_durations.get(step_name, 60)
            time_frac = min(elapsed / est, 0.9)  # cap at 90% to never show 100% prematurely
            time_pct = base_pct + int(time_frac * span_pct * 0.8)  # conservative
            update(max(file_pct, time_pct))
        else:
            update(file_pct)
            last_file_pct = file_pct

        _time.sleep(2)





def run_pipeline(job_id, project_dir):
    """Run the vox-director pipeline with live progress tracking."""
    job = jobs[job_id]

    def log(msg):
        ts = datetime.now().strftime('%H:%M:%S')
        job["log"].append(f"[{ts}] {msg}")
        # Cap log size to prevent memory bloat
        if len(job["log"]) > 500:
            job["log"] = job["log"][-300:]

    # Step weights: keyframes heaviest (image gen), clips second, audio/assemble lighter
    steps = [
        ("keyframes", "🎨 Generating collage keyframes (AI images)...", "keyframes.py", 40),
        ("clips",     "🎬 Animating keyframes (motion)...",           "clips.py",     35),
        ("audio",     "🎙️ Generating voice + music...",               "audio.py",     10),
        ("assemble",  "🎞️ Assembling final video...",                  "assemble.py",  15),
    ]

    total_weight = sum(s[3] for s in steps)
    cumulative = 0

    try:
        for i, (step_name, step_desc, script, weight) in enumerate(steps):
            if job.get("cancel"):
                job["status"] = "cancelled"
                log("⏹️ Job cancelled by user.")
                with job_lock:
                    save_jobs()
                return

            base_pct = int(cumulative * 100 / total_weight)
            span_pct = int(weight * 100 / total_weight)
            job["status"] = step_name
            job["step_index"] = i
            job["step_total"] = len(steps)
            job["step_label"] = step_desc
            job["progress"] = base_pct
            log(step_desc)
            with job_lock:
                save_jobs()

            script_path = SCRIPTS / script
            cmd = ["python3", str(script_path), str(project_dir)]

            log(f"   ▶ python3 {script}")

            # Start file-watcher thread for sub-progress
            stop_watcher = threading.Event()
            watcher = threading.Thread(
                target=_watcher_loop,
                args=(job_id, project_dir, step_name, base_pct, span_pct, stop_watcher),
                daemon=True,
            )
            watcher.start()

            # Run subprocess with live line-by-line output
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(VOX),
            )

            try:
                import select
                while True:
                    if job.get("cancel"):
                        proc.terminate()
                        stop_watcher.set()
                        job["status"] = "cancelled"
                        log("⏹️ Job cancelled by user.")
                        return

                    # Wait for output with 1s timeout so we can check cancel
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if ready:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        line = line.rstrip()
                        if line.strip():
                            # Clean up verbose output lines
                            clean = line
                            # Truncate submitted URLs: "[1a] submitted {...}" -> "[1a] submitted image"
                            import re as _re2
                            clean = _re2.sub(r'(submitted|done)\s*\{.*\}', r'\1', clean)
                            clean = _re2.sub(r'(submitted|done)\s*".*"', r'\1', clean)
                            # General truncation for any remaining long lines
                            if len(clean) > 150:
                                clean = clean[:150] + '…'
                            log(f"   {clean}")
                            # Sub-progress: count shot completions from output
                            _update_sub_progress(job_id, line, step_name, base_pct, span_pct, project_dir)
                    elif proc.poll() is not None:
                        break
            except Exception:
                # Fallback: simple readline loop
                for line in proc.stdout:
                    line = line.rstrip()
                    if line.strip():
                        import re as _re3
                        clean = _re3.sub(r'(submitted|done)\s*\{.*\}', r'\1', line)
                        clean = _re3.sub(r'(submitted|done)\s*".*"', r'\1', clean)
                        if len(clean) > 150:
                            clean = clean[:150] + '…'
                        log(f"   {clean}")

            proc.wait(timeout=30)
            stop_watcher.set()
            watcher.join(timeout=3)

            cumulative += weight
            job["progress"] = int(cumulative * 100 / total_weight)

            if proc.returncode != 0:
                # Before declaring failure, check if final.mp4 exists anyway
                # (assemble may have completed but crashed during cleanup)
                final_video = project_dir / "final.mp4"
                if final_video.exists() and final_video.stat().st_size > 10000:
                    job["status"] = "done"
                    job["progress"] = 100
                    job["result_video"] = str(final_video)
                    size_mb = final_video.stat().st_size / 1024 / 1024
                    log(f"⚠️ Step '{step_name}' exited with {proc.returncode}, but final.mp4 exists ({size_mb:.1f} MB) — marking done")
                    with job_lock:
                        save_jobs()
                    return
                job["status"] = "failed"
                job["error"] = f"Step '{step_name}' failed (exit {proc.returncode})"
                log(f"❌ Step '{step_name}' FAILED (exit {proc.returncode})!")
                with job_lock:
                    save_jobs()
                return

            log(f"   ✅ {step_name} done")

        final_video = project_dir / "final.mp4"
        if final_video.exists():
            job["status"] = "done"
            job["progress"] = 100
            job["result_video"] = str(final_video)
            size_mb = final_video.stat().st_size / 1024 / 1024
            log(f"🎉 Video complete! ({size_mb:.1f} MB)")
            log(f"📁 {final_video}")
        else:
            job["status"] = "failed"
            job["error"] = "final.mp4 not found"
            log("❌ final.mp4 not found!")
        with job_lock:
            save_jobs()

    except subprocess.TimeoutExpired:
        # Check if final.mp4 exists despite timeout
        final_video = project_dir / "final.mp4"
        if final_video.exists() and final_video.stat().st_size > 10000:
            job["status"] = "done"
            job["progress"] = 100
            job["result_video"] = str(final_video)
            log(f"⚠️ Pipeline timed out, but final.mp4 exists — marking done")
        else:
            job["status"] = "failed"
            job["error"] = "Pipeline timed out"
            log("❌ Pipeline timed out!")
        with job_lock:
            save_jobs()
    except Exception as e:
        # Check if final.mp4 exists despite error
        final_video = project_dir / "final.mp4"
        if final_video.exists() and final_video.stat().st_size > 10000:
            job["status"] = "done"
            job["progress"] = 100
            job["result_video"] = str(final_video)
            log(f"⚠️ Error: {e}, but final.mp4 exists — marking done")
        else:
            job["status"] = "failed"
            job["error"] = str(e)
            log(f"❌ Error: {e}")
        with job_lock:
            save_jobs()


# ============================================================
# HTML — with Puter.js for free AI text generation in browser
# ============================================================
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<title>Vox Director Studio</title>
<style>
:root {
  --bg: #0a0a0a; --card: #161616; --card2: #1e1e1e;
  --accent: #ff3838; --accent2: #ff6b6b;
  --text: #f0f0f0; --dim: #888; --border: #2a2a2a;
  --green: #2db84d; --yellow: #f5a623; --blue: #4285f4;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Segoe UI',system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; padding:20px; }
.container { max-width:800px; margin:0 auto; }
header { text-align:center; margin-bottom:24px; }
header h1 { font-size:28px; font-weight:800; }
header h1 .v { color:var(--accent); }
header p { color:var(--dim); margin-top:4px; font-size:14px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; margin-bottom:18px; }
.card h2 { font-size:16px; margin-bottom:14px; }
label { display:block; font-size:12px; color:var(--dim); margin-bottom:5px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }
input, select, textarea {
  width:100%; padding:10px 12px; background:var(--bg); border:1px solid var(--border);
  border-radius:8px; color:var(--text); font-size:14px; margin-bottom:14px; font-family:inherit;
}
input:focus, select:focus, textarea:focus { outline:none; border-color:var(--accent); }
textarea { resize:vertical; min-height:80px; }
.row { display:flex; gap:12px; }
.row > div { flex:1; }
button {
  background:var(--accent); color:white; border:none; padding:12px 28px;
  border-radius:8px; font-size:15px; font-weight:700; cursor:pointer; transition:all 0.2s;
  width:100%;
}
button:hover { background:var(--accent2); transform:translateY(-1px); }
button:disabled { opacity:0.4; cursor:not-allowed; transform:none; }
.hint { font-size:12px; color:var(--dim); margin-top:-8px; margin-bottom:14px; }
.progress-wrap { margin:12px 0; }
.progress-bar { height:8px; background:var(--border); border-radius:4px; overflow:hidden; }
.progress-fill { height:100%; background:linear-gradient(90deg, var(--accent), var(--accent2)); transition:width 0.5s ease; border-radius:4px; }
.progress-fill.done { background:linear-gradient(90deg, var(--green), #4ade80); }
.progress-fill.failed { background:linear-gradient(90deg, #dc2626, #ef4444); }
.progress-label { display:flex; justify-content:space-between; font-size:12px; color:var(--dim); margin-bottom:6px; }
.progress-pct { font-weight:700; color:var(--text); }

/* Step tracker */
.step-tracker { display:flex; gap:4px; margin:10px 0 14px; }
.step-pill { flex:1; padding:6px 4px; border-radius:6px; font-size:10px; text-align:center; font-weight:600; background:var(--border); color:var(--dim); transition:all 0.3s; display:flex; flex-direction:column; align-items:center; gap:2px; }
.step-pill.active { background:rgba(245,166,35,0.15); color:var(--yellow); border:1px solid rgba(245,166,35,0.4); }
.step-pill.done { background:rgba(45,184,77,0.15); color:var(--green); }
.step-pill.failed { background:rgba(255,56,56,0.15); color:var(--accent); }
.step-icon { font-size:14px; }
.step-name { font-size:9px; line-height:1; }

/* Live log */
.log-box {
  background:#000; border:1px solid var(--border); border-radius:8px;
  padding:12px; margin-top:12px; max-height:280px; overflow-y:auto;
  font-family:'Courier New',monospace; font-size:11px; line-height:1.7;
}
.log-line { color:#aaa; white-space:pre-wrap; word-break:break-word; }
.log-line.step-start { color:var(--yellow); font-weight:700; border-top:1px solid var(--border); padding-top:6px; margin-top:6px; }
.log-line.step-done { color:var(--green); }
.log-line.error { color:var(--accent); }
.log-line.success { color:var(--green); font-weight:700; }
.log-line .ts { color:#555; margin-right:6px; }

/* Pulsing live indicator */
.live-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--yellow); margin-right:6px; animation:pulse 1.2s infinite; }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.4;transform:scale(0.8)} }

/* Keyframe count badges */
.kf-counter { display:inline-block; background:var(--border); padding:1px 8px; border-radius:10px; font-size:10px; margin-left:6px; color:var(--text); }
.badge { display:inline-block; padding:2px 10px; border-radius:10px; font-size:11px; font-weight:600; }
.badge-pending { background:#333; color:#888; }
.badge-running { background:rgba(245,166,35,0.2); color:var(--yellow); }
.badge-done { background:rgba(45,184,77,0.2); color:var(--green); }
.badge-failed { background:rgba(255,56,56,0.2); color:var(--accent); }
.badge-script { background:rgba(66,133,244,0.2); color:var(--blue); }
.job-card { background:var(--card2); border:1px solid var(--border); border-radius:10px; padding:14px; margin-bottom:10px; }
.job-title { font-weight:600; font-size:14px; }
.job-meta { font-size:12px; color:var(--dim); margin-top:4px; }
.video-result { margin-top:12px; }
.video-result video { width:100%; border-radius:8px; }
a { color:var(--accent); text-decoration:none; }
.empty { color:var(--dim); text-align:center; padding:20px; }
.script-preview {
  background:#0d0d0d; border:1px solid var(--border); border-radius:8px;
  padding:14px; margin-top:12px; max-height:400px; overflow-y:auto;
  font-size:12px; line-height:1.5; white-space:pre-wrap; font-family:'Courier New',monospace;
}
.beat-card {
  background:var(--card2); border-left:3px solid var(--accent);
  padding:10px 14px; margin:8px 0; border-radius:0 8px 8px 0;
}
.beat-headline { font-weight:700; font-size:13px; color:var(--yellow); }
.beat-narration { font-size:12px; color:var(--text); margin-top:4px; }
.beat-shots { font-size:11px; color:var(--dim); margin-top:4px; }
.yt-meta-box {
  background:var(--card2); border:1px solid var(--border);
  padding:14px 16px; margin:10px 0; border-radius:10px;
}
.yt-meta-label { font-size:11px; font-weight:700; color:var(--dim); text-transform:uppercase; letter-spacing:0.5px; }
.yt-meta-title { font-size:15px; font-weight:700; color:var(--text); margin-top:4px; line-height:1.4; }
.yt-meta-desc { font-size:12px; color:var(--text); margin-top:4px; line-height:1.6; opacity:0.85; }
.yt-meta-tags { margin-top:6px; display:flex; flex-wrap:wrap; gap:6px; }
.yt-tag { font-size:11px; padding:3px 10px; border-radius:12px; background:var(--card); color:var(--cyan); border:1px solid var(--border); }
.spinner {
  display:inline-block; width:16px; height:16px;
  border:2px solid var(--border); border-top-color:var(--accent);
  border-radius:50%; animation:spin 0.8s linear infinite;
}
@keyframes spin { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Vox <span class="v">Director</span> Studio</h1>
    <p>Turn any topic into a Vox-style paper-collage video — 100% free, no login needed</p>
    <div id="composio-status" style="margin-top:10px;font-size:13px;min-height:20px;">Loading YouTube connection status…</div>
  </header>

  <!-- Create Video Form -->
  <div class="card">
    <h2>🎬 Create New Video</h2>

    <label>Video Title / Topic *</label>
    <input type="text" id="topic" placeholder="e.g. The History of Coffee, Why Roman Empire Fell, How AI Works" />
    <div class="hint">What is the video about? Be specific.</div>

    <label>Creative Direction (optional)</label>
    <textarea id="prompt" placeholder="e.g. Focus on the surprising facts. Use a dramatic tone. Cover the origins, spread, and modern impact. Make it feel like a mystery being solved."></textarea>
    <div class="hint">Give extra direction on tone, focus, style, or specific points to cover.</div>

    <div class="row">
      <div>
        <label>Duration</label>
        <select id="duration">
          <option value="15">15s (Short)</option>
          <option value="30" selected>30s (Standard)</option>
          <option value="60">60s (Extended)</option>
          <option value="90">90s (Long)</option>
          <option value="120">120s (2 min)</option>
          <option value="180">180s (3 min)</option>
          <option value="300">300s (5 min)</option>
          <option value="600">600s (10 min) ⭐</option>
          <option value="900">900s (15 min) ⭐</option>
          <option value="custom">Custom...</option>
        </select>
        <input type="number" id="duration-custom" min="5" max="1200" value="45" 
               style="display:none;margin-top:6px;width:100%;padding:8px;border-radius:8px;border:1px solid var(--border);background:var(--card2);color:var(--text);" 
               placeholder="Enter seconds (5-1200)" />
        <div class="hint" id="duration-hint" style="display:none;">Enter duration in seconds (5 to 600).</div>
      </div>
      <div>
        <label>Aspect Ratio</label>
        <select id="aspect">
          <option value="16:9" selected>16:9 (YouTube)</option>
          <option value="9:16">9:16 (Shorts/Reels)</option>
          <option value="1:1">1:1 (Square)</option>
          <option value="3:4">3:4 (Portrait)</option>
        </select>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Visual Theme</label>
        <select id="theme">
          <option value="american-retro">American Retro (1950s ad)</option>
          <option value="swiss-modern">Swiss Modern (clean, tech)</option>
          <option value="punk-zine">Punk Zine (rebel, music)</option>
          <option value="soviet-constructivist">Soviet Constructivist (revolution)</option>
          <option value="wpa-propaganda">WPA Propaganda (1930s history)</option>
          <option value="70s-groovy">70s Groovy (culture, food)</option>
          <option value="chinese-ink">Chinese Ink (Asian history)</option>
          <option value="atomic-age">Atomic Age (science, space)</option>
          <option value="newsprint-editorial" selected>Newsprint Editorial (news, tech)</option>
          <option value="gilded-deco">Gilded Deco (luxury, heritage)</option>
          <option value="stick-figure">Stick Figure (2D character animation)</option>
        </select>
      </div>
      <div>
        <label>Narrative Arc</label>
        <select id="arc">
          <option value="auto" selected>Auto (smart pick)</option>
          <option value="hook_payoff">Hook → Payoff</option>
          <option value="timeline">Timeline (history)</option>
          <option value="how_it_works">How It Works</option>
          <option value="man_in_hole">Man in Hole (comeback)</option>
          <option value="myth_buster">Myth Buster</option>
          <option value="listicle">Listicle (top N)</option>
          <option value="three_act">Three Act (story)</option>
        </select>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Language</label>
        <select id="language">
          <option value="en" selected>English</option>
          <option value="zh">Chinese</option>
          <option value="ja">Japanese</option>
          <option value="es">Spanish</option>
          <option value="fr">French</option>
          <option value="de">German</option>
          <option value="hi">Hindi</option>
          <option value="ur">Urdu</option>
        </select>
      </div>
      <div>
        <label>Narrator Voice</label>
        <select id="voice">
          <option value="leo" selected>Leo (male, documentary)</option>
          <option value="max">Max (male, casual)</option>
          <option value="lily">Lily (female, warm)</option>
          <option value="emma">Emma (female, young)</option>
          <option value="mia">Mia (female, professional)</option>
        </select>
      </div>
    </div>

    <button onclick="generateScript()" id="gen-btn">🧠 Generate Script & Create Video</button>
  </div>

  <!-- Script Preview (shown after generation) -->
  <div class="card" id="script-card" style="display:none;">
    <h2>📝 Generated Script Preview</h2>
    <div id="script-preview"></div>
    <div style="display:flex; gap:10px; margin-top:14px;">
      <button onclick="approveAndRun()" style="width:auto;flex:1;background:var(--green);">✅ Looks Good — Generate Video</button>
      <button onclick="regenerateScript()" class="btn-ghost" style="width:auto;flex:1;background:var(--card2);border:1px solid var(--border);">🔄 Regenerate</button>
      <button onclick="editScript()" class="btn-ghost" style="width:auto;flex:1;background:var(--card2);border:1px solid var(--border);">✏️ Edit</button>
    </div>
  </div>

  <!-- Edit Script (textarea) -->
  <div class="card" id="edit-card" style="display:none;">
    <h2>✏️ Edit Script (beats.json)</h2>
    <textarea id="edit-json" style="min-height:300px;font-family:monospace;font-size:12px;"></textarea>
    <button onclick="saveEditedScript()" style="margin-top:10px;">💾 Save & Generate Video</button>
  </div>

  <!-- Jobs List -->
  <div class="card">
    <h2>📋 Video History</h2>
    <div style="font-size:12px;color:var(--dim);margin-bottom:10px;" id="history-count"></div>
    <div id="jobs-list">
      <div class="empty">No videos yet. Create one above!</div>
    </div>
  </div>

  <!-- Autonomous Mode -->
  <div class="card" style="border-color:var(--accent);">
    <h2>🤖 Autonomous Mode <span style="font-size:12px;color:var(--dim);font-weight:400" id="auto-active-count"></span></h2>
    <p style="color:var(--dim);font-size:13px;margin-bottom:16px">
      Automatically researches viral history videos from faceless channels, generates unique ideas, creates <b>10-15 min long-form videos</b>, and uploads to YouTube. <b>4 videos per week</b>, fully autonomous.
    </p>

    <!-- Quick Actions -->
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px">
      <button onclick="runWeeklyBatch()" style="padding:12px 20px;border-radius:10px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-weight:700;font-size:14px">🚀 Weekly Batch (4 Videos)</button>
      <button onclick="runFullAuto()" style="padding:12px 20px;border-radius:10px;border:1px solid var(--border);background:var(--card2);color:var(--text);cursor:pointer;font-weight:600;font-size:14px">⚡ Single Auto Run</button>
      <button onclick="runResearch()" style="padding:12px 20px;border-radius:10px;border:1px solid var(--border);background:var(--card2);color:var(--text);cursor:pointer;font-weight:600;font-size:14px">🔍 Research Now</button>
    </div>

    <!-- Idea Generator -->
    <div style="border-top:1px solid var(--border);padding-top:16px;margin-bottom:16px">
      <label style="font-size:12px;color:var(--dim);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Topic (optional — leave empty for auto-generate)</label>
      <input type="text" id="auto-topic-input" placeholder="e.g. The Strange History of Coffee" style="width:100%;padding:10px 12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;margin-bottom:10px" />
      <button onclick="generateIdea()" style="padding:10px 16px;border-radius:8px;border:1px solid var(--border);background:var(--card2);color:var(--text);cursor:pointer;font-weight:600;font-size:13px">🧠 Generate Idea</button>
      <button onclick="generateAutoVideo()" style="padding:10px 16px;border-radius:8px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-weight:600;font-size:13px;margin-left:8px">🎬 Generate Video</button>
      <div id="idea-result" style="margin-top:12px"></div>
    </div>

    <!-- Active Run Progress -->
    <div id="auto-run-section" style="display:none;border-top:1px solid var(--border);padding-top:16px;margin-bottom:16px">
      <h3 style="font-size:14px;margin-bottom:10px">⚡ Active Run</h3>
      <div id="auto-run-progress"></div>
    </div>

    <!-- Competitor Research -->
    <div style="border-top:1px solid var(--border);padding-top:16px;margin-bottom:16px">
      <h3 style="font-size:14px;margin-bottom:10px">📊 Competitor Research — Viral History Videos</h3>
      <div id="competitor-list"></div>
    </div>

    <!-- Schedule -->
    <div style="border-top:1px solid var(--border);padding-top:16px;margin-bottom:16px">
      <h3 style="font-size:14px;margin-bottom:10px">⏰ Weekly Schedule</h3>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="schedule-enabled" onchange="toggleSchedule()" style="width:auto;margin:0" />
          <span style="font-size:13px">Enable weekly auto-batch (4 videos every Monday 6:00 UTC)</span>
        </label>
        <span id="schedule-status" style="font-size:13px;color:var(--dim)">🔴 Disabled</span>
      </div>
      <p style="font-size:12px;color:var(--dim);margin-top:8px">
        Generates 4 long-form (10-min) videos from competitor research, then uploads each to YouTube. Pipeline manager handles failures with automatic retry.
      </p>
    </div>

    <!-- History -->
    <div style="border-top:1px solid var(--border);padding-top:16px">
      <h3 style="font-size:14px;margin-bottom:10px">📜 Autonomous Run History</h3>
      <div id="auto-history"></div>
    </div>
  </div>
</div>

<script>
let currentBeats = null;
let pollTimer = null;
let ytMetadata = {};  // job_id -> {title, description, tags}

// Show/hide custom duration input
document.getElementById('duration').addEventListener('change', function() {
  const custom = document.getElementById('duration-custom');
  const hint = document.getElementById('duration-hint');
  if (this.value === 'custom') {
    custom.style.display = 'block';
    hint.style.display = 'block';
    custom.focus();
  } else {
    custom.style.display = 'none';
    hint.style.display = 'none';
  }
});

async function generateScript() {
  const topic = document.getElementById('topic').value.trim();
  if (!topic) { alert('Please enter a topic!'); return; }

  let duration;
  const durVal = document.getElementById('duration').value;
  if (durVal === 'custom') {
    duration = parseInt(document.getElementById('duration-custom').value);
    if (!duration || duration < 5 || duration > 1200) {
      alert('Please enter a valid duration between 5 and 1200 seconds!');
      return;
    }
  } else {
    duration = parseInt(durVal);
  }
  const aspect = document.getElementById('aspect').value;
  const theme = document.getElementById('theme').value;
  let arc = document.getElementById('arc').value;
  const language = document.getElementById('language').value;
  const voice = document.getElementById('voice').value;
  const extraPrompt = document.getElementById('prompt').value.trim();

  // Auto-pick arc
  if (arc === 'auto') {
    const tl = topic.toLowerCase();
    if (/history|evolution|origin|ancient|rise|fall|war|empire|dynasty/.test(tl)) arc = 'timeline';
    else if (/how|why|what|work|science|tech|engine|machine|system/.test(tl)) arc = 'how_it_works';
    else if (/comeback|transformation|recovery|rise of/.test(tl)) arc = 'man_in_hole';
    else arc = 'hook_payoff';
  }

  const btn = document.getElementById('gen-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Generating script...';
  btn.style.opacity = '0.7';

  // Show loading status below button
  let statusDiv = document.getElementById('gen-status');
  if (!statusDiv) {
    statusDiv = document.createElement('div');
    statusDiv.id = 'gen-status';
    statusDiv.style.cssText = 'margin-top:10px;padding:10px 14px;border-radius:8px;background:var(--card2);border:1px solid var(--border);font-size:13px;color:var(--dim);';
    btn.parentNode.insertBefore(statusDiv, btn.nextSibling);
  }
  statusDiv.style.display = 'block';
  let dots = 0;
  const dotTimer = setInterval(() => {
    dots = (dots + 1) % 4;
    statusDiv.innerHTML = '🧠 AI script likh raha hai' + '.'.repeat(dots) + '<br><span style="font-size:11px;opacity:0.6;">Ye 15-20 second lagta hai. Wait karein.</span>';
  }, 500);

  try {
    console.log('[generateScript] Calling server...');
    const res = await fetch('api/generate-script', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, duration, aspect, theme, arc, language, voice, prompt: extraPrompt })
    });
    console.log('[generateScript] Server response:', res.status);
    if (!res.ok) throw new Error('Server returned ' + res.status);
    const result = await res.json();
    console.log('[generateScript] Result source:', result.source || 'unknown');
    if (result.error) throw new Error(result.error);

    clearInterval(dotTimer);
    statusDiv.innerHTML = '✅ Script ready! Niche preview check karein.';
    statusDiv.style.borderColor = 'var(--green)';
    statusDiv.style.color = 'var(--green)';

    currentBeats = result.beats;
    showScriptPreview();
    document.getElementById('script-card').style.display = 'block';
    document.getElementById('edit-card').style.display = 'none';
    console.log('[generateScript] ✓ Script generated!');

    // Scroll to script preview
    document.getElementById('script-card').scrollIntoView({ behavior:'smooth', block:'start' });

  } catch (e) {
    console.error('[generateScript] Failed:', e);
    clearInterval(dotTimer);
    statusDiv.innerHTML = '❌ Error: ' + e.message;
    statusDiv.style.borderColor = 'var(--accent)';
    statusDiv.style.color = 'var(--accent)';
    alert('Script generation failed: ' + e.message);
  }

  btn.disabled = false;
  btn.innerHTML = '🧠 Generate Script & Create Video';
  btn.style.opacity = '1';
}

function showScriptPreview() {
  if (!currentBeats) return;
  const beats = currentBeats.beats || [];
  
  // YouTube metadata section
  let ytHtml = '';
  if (currentBeats.yt_title || currentBeats.yt_description) {
    ytHtml = `<div class="yt-meta-box">
      <div class="yt-meta-label">📺 YouTube Title</div>
      <div class="yt-meta-title">${currentBeats.yt_title || ''}</div>
      <div class="yt-meta-label" style="margin-top:10px;">📝 Description</div>
      <div class="yt-meta-desc">${(currentBeats.yt_description || '').replace(/\\n/g, '<br>')}</div>
      ${currentBeats.yt_tags ? `<div class="yt-meta-label" style="margin-top:10px;">🏷️ Tags</div><div class="yt-meta-tags">${currentBeats.yt_tags.map(t => `<span class="yt-tag">${t}</span>`).join('')}</div>` : ''}
    </div>`;
  }
  
  let html = `<div style="margin-bottom:8px;color:var(--dim);font-size:13px;">
    <strong>${currentBeats.topic || 'Untitled'}</strong> · ${beats.length} beats · ${beats.reduce((a,b) => a + (b.shots||[]).length, 0)} shots · ${currentBeats.arc || 'auto'} arc
  </div>`;
  html += ytHtml;
  for (const beat of beats) {
    html += `<div class="beat-card">
      <div class="beat-headline">Beat ${beat.id}: ${beat.title_en || 'NO TITLE'}</div>
      <div class="beat-narration">🎙️ "${beat.narration || ''}"</div>
      <div class="beat-shots">📷 ${beat.bg || ''} · ${(beat.shots||[]).map(s => s.shot_size + '/' + s.camera_move).join(' → ')}</div>
    </div>`;
  }
  document.getElementById('script-preview').innerHTML = html;
}

function regenerateScript() {
  generateScript();
}

function editScript() {
  document.getElementById('edit-json').value = JSON.stringify(currentBeats, null, 2);
  document.getElementById('edit-card').style.display = 'block';
}

function saveEditedScript() {
  try {
    currentBeats = JSON.parse(document.getElementById('edit-json').value);
    document.getElementById('edit-card').style.display = 'none';
    showScriptPreview();
    approveAndRun();
  } catch (e) {
    alert('Invalid JSON: ' + e.message);
  }
}

async function approveAndRun() {
  if (!currentBeats) { alert('No script generated! Try generating again.'); return; }

  const btn = document.getElementById('gen-btn');
  btn.disabled = true;
  btn.innerHTML = '⏳ Starting pipeline...';
  btn.style.opacity = '0.7';

  let statusDiv = document.getElementById('gen-status');
  if (statusDiv) {
    statusDiv.innerHTML = '🎬 Video pipeline start ho raha hai...';
    statusDiv.style.display = 'block';
    statusDiv.style.borderColor = 'var(--border)';
    statusDiv.style.color = 'var(--dim)';
  }

  try {
    const res = await fetch('api/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ beats: currentBeats })
    });

    if (!res.ok) {
      const txt = await res.text();
      console.error('API error response:', res.status, txt);
      alert('Server error: ' + res.status + ' ' + txt.slice(0, 200));
      btn.disabled = false;
      btn.innerHTML = '🧠 Generate Script & Create Video';
      btn.style.opacity = '1';
      return;
    }

    const result = await res.json();

    if (result.error) {
      alert('Error: ' + result.error);
    } else {
      // Store YT metadata for this job (used when uploading to YouTube)
      if (result.job_id && currentBeats.yt_title) {
        ytMetadata[result.job_id] = {
          title: currentBeats.yt_title,
          description: currentBeats.yt_description || '',
          tags: currentBeats.yt_tags || []
        };
      }
      document.getElementById('topic').value = '';
      document.getElementById('prompt').value = '';
      document.getElementById('script-card').style.display = 'none';
      document.getElementById('edit-card').style.display = 'none';

      // Show success message
      if (statusDiv) {
        statusDiv.innerHTML = '✅ Video generation shuru! Niche progress dekhein.';
        statusDiv.style.borderColor = 'var(--green)';
        statusDiv.style.color = 'var(--green)';
      }

      pollJobs();

      // Scroll to jobs list
      document.querySelector('.card:last-child').scrollIntoView({ behavior:'smooth', block:'start' });
    }
  } catch (e) {
    console.error('approveAndRun error:', e);
    alert('Error starting pipeline: ' + e.message + ' Type: ' + e.name);
  }

  document.getElementById('gen-btn').disabled = false;
  document.getElementById('gen-btn').innerHTML = '🧠 Generate Script & Create Video';
}

const STEPS = [
  { key:'keyframes', icon:'🎨', name:'Keyframes' },
  { key:'clips',     icon:'🎬', name:'Clips' },
  { key:'audio',     icon:'🎙️', name:'Audio' },
  { key:'assemble',  icon:'🎞️', name:'Assemble' },
];

function renderStepTracker(j) {
  const active = STEPS.some(s => s.key === j.status);
  if (!active && j.status !== 'done' && j.status !== 'failed') return '';

  const currentIdx = STEPS.findIndex(s => s.key === j.status);
  let html = '<div class="step-tracker">';
  for (let i = 0; i < STEPS.length; i++) {
    const s = STEPS[i];
    let cls = '';
    if (j.status === 'done') cls = 'done';
    else if (j.status === 'failed' && i === currentIdx) cls = 'failed';
    else if (i < currentIdx) cls = 'done';
    else if (i === currentIdx) cls = 'active';
    html += `<div class="step-pill ${cls}"><span class="step-icon">${s.icon}</span><span class="step-name">${s.name}</span></div>`;
  }
  html += '</div>';
  return html;
}

function renderLog(j) {
  if (!j.log || j.log.length === 0) return '';
  const lines = j.log.map(l => {
    let cls = 'log-line';
    if (/🎨|🎬|🎙️|🎞️/.test(l)) cls += ' step-start';
    else if (/✅.*done/i.test(l)) cls += ' step-done';
    else if (/❌|FAILED|Error/i.test(l)) cls += ' error';
    else if (/🎉|complete/i.test(l)) cls += ' success';
    const m = l.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*(.*)$/);
    if (m) return `<div class="${cls}"><span class="ts">${m[1]}</span>${m[2]}</div>`;
    return `<div class="${cls}">${l}</div>`;
  }).join('');
  return `<div class="log-box" id="log-${j.id}">${lines}</div>`;
}

function renderJob(j) {
  const active = STEPS.some(s => s.key === j.status);
  let badge = `<span class="badge badge-${j.status}">${j.status}</span>`;

  if (active) {
    badge = `<span class="badge badge-running"><span class="live-dot"></span>${j.status} · ${j.progress}%</span>`;
  }
  if (j.status === 'done') badge = `<span class="badge badge-done">✅ Done</span>`;
  if (j.status === 'failed') badge = `<span class="badge badge-failed">❌ Failed</span>`;
  if (j.status === 'cancelled') badge = `<span class="badge badge-pending">⏹️ Cancelled</span>`;

  let progress = '';
  if (active || j.status === 'done' || j.status === 'failed') {
    const pct = j.status === 'done' ? 100 : (j.progress || 0);
    let fillCls = '';
    if (j.status === 'done') fillCls = 'done';
    if (j.status === 'failed') fillCls = 'failed';
    let label = active ? (j.step_label || j.status) : (j.status === 'done' ? 'Complete' : 'Failed');
    // Add elapsed time for active jobs
    if (active && j.created_at) {
      const elapsed = Math.floor((Date.now() / 1000) - j.created_at);
      const mins = Math.floor(elapsed / 60);
      const secs = elapsed % 60;
      label += ` · ${mins}:${secs.toString().padStart(2,'0')}`;
    }
    progress = `<div class="progress-wrap">
      <div class="progress-label"><span>${label}</span><span class="progress-pct">${pct}%</span></div>
      <div class="progress-bar"><div class="progress-fill ${fillCls}" style="width:${pct}%"></div></div>
    </div>`;
  }

  const stepTracker = renderStepTracker(j);
  const log = renderLog(j);

  let video = '';
  if (j.status === 'done' && j.result_video) {
    video = `<div class="video-result">
      <video controls preload="metadata" src="api/video/${j.id}" style="width:100%;border-radius:8px;"></video>
      <div style="margin-top:8px;display:flex;gap:8px;">
        <a href="api/video/${j.id}" download style="display:inline-block;padding:8px 16px;border-radius:8px;border:1px solid var(--border);background:var(--card2);color:var(--text);text-decoration:none;">⬇️ Download</a>
        <button onclick="uploadToYT('${j.id}')" style="display:inline-block;width:auto;padding:8px 16px;">📤 Upload to YouTube</button>
      </div>
    </div>`;
  }

  let error = j.error ? `<div style="color:var(--accent);font-size:12px;margin-top:8px;">⚠️ ${j.error}</div>` : '';
  let cancelBtn = active ? `<button onclick="cancelJob('${j.id}')" style="width:auto;margin-top:8px;padding:6px 14px;font-size:12px;background:var(--card2);border:1px solid var(--border);">⏹️ Cancel</button>` : '';
  let deleteBtn = !active ? `<button onclick="deleteJob('${j.id}')" style="width:auto;margin-top:8px;margin-left:8px;padding:6px 14px;font-size:12px;background:var(--card2);border:1px solid var(--accent);color:var(--accent);">🗑️ Delete</button>` : '';

  return `<div class="job-card" id="job-${j.id}">
    <div style="display:flex;justify-content:space-between;align-items:start;">
      <div>
        <div class="job-title">${j.topic}</div>
        <div class="job-meta">${j.duration}s · ${j.aspect} · ${j.theme}</div>
      </div>
      ${badge}
    </div>
    ${progress}${stepTracker}${error}${log}${video}
    <div style="display:flex;gap:0;">${cancelBtn}${deleteBtn}</div>
  </div>`;
}

async function pollJobs() {
  try {
    const res = await fetch('api/jobs');
    const data = await res.json();

    const el = document.getElementById('jobs-list');
    if (data.length === 0) {
      el.innerHTML = '<div class="empty">No videos yet. Create one above!</div>';
      const hc = document.getElementById('history-count');
      if (hc) hc.textContent = '';
      return;
    }

    // Update history count
    const done = data.filter(j => j.status === 'done').length;
    const failed = data.filter(j => j.status === 'failed').length;
    const running = data.filter(j => ['keyframes','clips','audio','assemble'].includes(j.status)).length;
    const hc = document.getElementById('history-count');
    if (hc) hc.textContent = `${data.length} project(s) · ${done} done · ${failed} failed${running ? ' · ' + running + ' running' : ''}`;

    // Remember scroll positions of log boxes
    const scrollPos = {};
    data.forEach(j => {
      const logEl = document.getElementById(`log-${j.id}`);
      if (logEl) scrollPos[j.id] = logEl.scrollTop;
    });

    el.innerHTML = data.reverse().map(j => renderJob(j)).join('');

    // Restore scroll / auto-scroll to bottom for active jobs
    data.forEach(j => {
      const logEl = document.getElementById(`log-${j.id}`);
      if (logEl) {
        const wasNearBottom = scrollPos[j.id] === undefined || (logEl.scrollHeight - scrollPos[j.id] - logEl.clientHeight < 50);
        logEl.scrollTop = wasNearBottom ? logEl.scrollHeight : scrollPos[j.id];
      }
    });

    if (data.some(j => STEPS.some(s => s.key === j.status))) {
      setTimeout(pollJobs, 2000);
    }
  } catch (e) { console.error(e); }
}

async function cancelJob(id) {
  await fetch('api/cancel/' + id, { method: 'POST' });
  pollJobs();
}

async function deleteJob(id) {
  if (!confirm('Delete this video project permanently? This removes the video and all files. This cannot be undone.')) return;
  try {
    const res = await fetch('api/delete/' + id, { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      alert('Error: ' + data.error);
    } else {
      // Remove from local YT metadata
      delete ytMetadata[id];
      pollJobs();
    }
  } catch (e) {
    alert('Error deleting: ' + e.message);
  }
}

async function uploadToYT(id) {
  const meta = ytMetadata[id] || {};
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳ Uploading...';
  try {
    const res = await fetch('api/upload-yt/' + id, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(meta)
    });
    const data = await res.json();
    if (data.error) {
      alert('Error: ' + data.error);
      btn.disabled = false;
      btn.textContent = '📤 Upload to YouTube';
      return;
    }
    // Poll upload status
    const statusKey = data.status_key;
    const pollUpload = async () => {
      try {
        const sr = await fetch('api/upload-status/' + id);
        const sd = await sr.json();
        if (sd.status === 'done') {
          btn.textContent = '✅ Uploaded!';
          if (sd.video_url) {
            alert('✅ Uploaded to YouTube!\\n\\nURL: ' + sd.video_url);
            window.open(sd.video_url, '_blank');
          } else {
            alert('✅ Upload complete!\\n\\n' + (sd.output || '').slice(-200));
          }
        } else if (sd.status === 'error') {
          btn.disabled = false;
          btn.textContent = '📤 Upload to YouTube';
          alert('❌ Upload failed: ' + (sd.error || '').slice(0, 300));
        } else {
          btn.textContent = '⏳ Uploading... (' + Math.floor((Date.now()/1000 - (sd.started||0))) + 's)';
          setTimeout(pollUpload, 5000);
        }
      } catch(e) {
        setTimeout(pollUpload, 5000);
      }
    };
    setTimeout(pollUpload, 3000);
  } catch(e) {
    btn.disabled = false;
    btn.textContent = '📤 Upload to YouTube';
    alert('Error: ' + e.message);
  }
}

pollJobs();
setInterval(pollJobs, 10000);

// === YouTube Connection Status ===
async function checkComposio() {
  console.log('[yt] checking YouTube connection status...');
  try {
    const res = await fetch('api/yt/status', {cache: 'no-store'});
    const data = await res.json();
    const el = document.getElementById('composio-status');
    if (!el) return;

    if (data.connected) {
      el.innerHTML = '<span style="color:var(--green);font-weight:600;">✅ YouTube connected — uploads ready</span>';
    } else if (data.needs_secret) {
      el.innerHTML = '<span style="color:var(--accent);">⚠️ YouTube not connected — <a href="#" onclick="showYTSetup();return false;" style="color:var(--accent);text-decoration:underline;">click to setup</a></span>';
    } else if (data.needs_auth) {
      el.innerHTML = '<span style="color:var(--accent);">⚠️ YouTube needs authorization — <a href="#" onclick="authYouTube();return false;" style="color:var(--accent);text-decoration:underline;">click to authorize</a></span>';
    } else {
      el.innerHTML = '<span style="color:var(--accent);">⚠️ YouTube not configured</span>';
    }
  } catch(e) {
    console.error('[yt] error:', e);
    const el = document.getElementById('composio-status');
    if (el) el.innerHTML = '<span style="color:var(--accent);">⚠️ YouTube not configured — <a href="#" onclick="showYTSetup();return false;" style="color:var(--accent);text-decoration:underline;">click to setup</a></span>';
  }
}
function showYTSetup() {
  alert('YouTube Upload Setup (One-time, 5 minutes):\\n\\n' +
    '1. Go to https://console.cloud.google.com/\\n' +
    '2. Create a project (or use existing)\\n' +
    '3. Enable \"YouTube Data API v3\":\\n' +
    '   https://console.cloud.google.com/apis/library/youtube.googleapis.com\\n' +
    '4. Go to Credentials → Create Credentials → OAuth client ID\\n' +
    '5. Application type: Desktop app\\n' +
    '6. Download the JSON file\\n' +
    '7. Upload it below (or save as client_secret.json)\\n\\n' +
    'After uploading client_secret.json, click \"Authorize YouTube\" to login.');
}
async function authYouTube() {
  try {
    const res = await fetch('api/yt/auth', {method:'POST'});
    const data = await res.json();
    if (data.error) { alert('Error: ' + data.error); return; }
    if (data.auth_url) {
      // Show redirect URI instructions first
      const msg = 'IMPORTANT: Before authorizing, make sure this redirect URI is added\\n' +
        'to your Google Cloud Console under OAuth credentials:\\n\\n' +
        data.redirect_uri + '\\n\\n' +
        'If you already added it, click OK to open the authorization page.\\n' +
        'After authorizing, you will be redirected back automatically.';
      alert(msg);
      window.open(data.auth_url, '_blank');
      // Poll for connection status
      const pollAuth = async () => {
        await checkComposio();
        const el = document.getElementById('composio-status');
        if (el && el.innerHTML.includes('connected')) return;
        setTimeout(pollAuth, 3000);
      };
      setTimeout(pollAuth, 5000);
    } else if (data.needs_secret) {
      showYTSetup();
    }
  } catch(e) { alert('Error: ' + e.message); }
}
async function connectComposio() { authYouTube(); }
// Run immediately and also after DOM is fully ready
document.addEventListener('DOMContentLoaded', checkComposio);
checkComposio();

// ── Autonomous Tab ─────────────────────────────────────────────
async function autoInit() {
  await loadCompetitors();
  await loadSchedule();
  await loadHistory();
  await loadAutoRuns();
}
async function loadCompetitors() {
  try {
    const res = await fetch('api/auto/competitors', {cache:'no-store'});
    if (!res.ok) { document.getElementById('competitor-list').innerHTML = '<p style="color:var(--dim)">No research yet. Click "Research" first.</p>'; return; }
    const data = await res.json();
    const el = document.getElementById('competitor-list');
    el.innerHTML = `<p style="color:var(--dim);margin-bottom:10px;">${data.total} viral history videos found</p>`;
    data.videos.slice(0,10).forEach((v,i) => {
      const faceless = v.is_faceless ? '👁️' : '👤';
      el.innerHTML += `<div style="display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);align-items:start;">
        <span>${faceless}</span>
        <div style="flex:1"><a href="${v.url}" target="_blank" style="color:var(--text);text-decoration:none">${v.title.substring(0,60)}</a>
        <br><span style="color:var(--dim);font-size:12px">${(v.views/1e6).toFixed(1)}M views · ${v.channel||''}</span></div>
      </div>`;
    });
  } catch(e) { console.error(e); }
}
async function runResearch() {
  const btn = event.target;
  btn.disabled = true; btn.textContent = '⏳ Researching...';
  try {
    const res = await fetch('api/auto/research', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data = await res.json();
    pollAutoStatus(data.run_id, () => { loadCompetitors(); btn.disabled=false; btn.textContent='🔍 Research Now'; });
  } catch(e) { alert('Error: '+e.message); btn.disabled=false; btn.textContent='🔍 Research Now'; }
}
async function generateIdea() {
  const btn = event.target;
  const topic = document.getElementById('auto-topic-input').value.trim();
  btn.disabled = true; btn.textContent = '🧠 Thinking...';
  try {
    const body = topic ? JSON.stringify({topic}) : '{}';
    const res = await fetch('api/auto/idea', {method:'POST',headers:{'Content-Type':'application/json'},body});
    const data = await res.json();
    pollAutoStatus(data.run_id, (run) => {
      if (run.status === 'done' && run.result) {
        const r = run.result;
        document.getElementById('idea-result').innerHTML = `
          <div style="padding:16px;background:var(--card2);border-radius:10px;border:1px solid var(--green)">
            <h3 style="color:var(--green);margin-bottom:8px">${r.title||'Untitled'}</h3>
            <p style="color:var(--dim);font-size:13px;margin-bottom:8px"><strong>Topic:</strong> ${r.topic||''}</p>
            <p style="font-size:13px;margin-bottom:8px">${r.concept||''}</p>
            <p style="color:var(--dim);font-size:12px;margin-bottom:8px"><strong>Why viral:</strong> ${r.angle||''}</p>
            ${r.inspired_by && r.inspired_by.length ? '<p style="font-size:12px;color:var(--dim)">Inspired by:<br>' + r.inspired_by.map(s=>'• '+s.title.substring(0,50)+' ('+(s.views/1e6).toFixed(1)+'M)').join('<br>') + '</p>' : ''}
            <button onclick="document.getElementById('auto-topic-input').value='${(r.topic||'').replace(/'/g,"\\'")}';generateAutoVideo()" style="margin-top:12px;padding:8px 20px;border-radius:8px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-weight:600">🎬 Generate Video</button>
          </div>`;
      } else {
        document.getElementById('idea-result').innerHTML = '<p style="color:var(--accent)">❌ '+run.error+'</p>';
      }
      btn.disabled=false; btn.textContent='🧠 Generate Idea';
    });
  } catch(e) { alert('Error: '+e.message); btn.disabled=false; btn.textContent='🧠 Generate Idea'; }
}
async function generateAutoVideo() {
  const topic = document.getElementById('auto-topic-input').value.trim();
  if (!topic) { alert('Enter a topic first'); return; }
  if (!confirm('Start autonomous video generation for:\\n'+topic+'\\n\\nThis will: generate script → create video → upload to YouTube.')) return;
  const btn = event.target;
  btn.disabled = true; btn.textContent = '⏳ Starting...';
  try {
    const res = await fetch('api/auto/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic,skip_research:true})});
    const data = await res.json();
    document.getElementById('auto-run-section').style.display = 'block';
    pollAutoStatus(data.run_id, null, 'auto-run-progress');
    btn.disabled=false; btn.textContent='🎬 Generate Video';
  } catch(e) { alert('Error: '+e.message); btn.disabled=false; btn.textContent='🎬 Generate Video'; }
}
async function runFullAuto() {
  if (!confirm('Start FULL autonomous run?\\n\\nThis will: research competitors → generate idea → create script → make video → upload to YouTube.\\n\\nThis takes ~15-20 minutes.')) return;
  const btn = event.target;
  btn.disabled = true; btn.textContent = '⏳ Running...';
  try {
    const res = await fetch('api/auto/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skip_research:false})});
    const data = await res.json();
    document.getElementById('auto-run-section').style.display = 'block';
    pollAutoStatus(data.run_id, null, 'auto-run-progress');
    btn.disabled=false; btn.textContent='⚡ Single Auto Run';
  } catch(e) { alert('Error: '+e.message); btn.disabled=false; btn.textContent='⚡ Single Auto Run'; }
}

async function runWeeklyBatch() {
  if (!confirm('Start WEEKLY BATCH?\\n\\nThis will generate 4 long-form (10-min) videos:\\n  1. Research competitor viral videos\\n  2. Generate 4 unique ideas\\n  3. Create scripts (80 beats each)\\n  4. Run pipeline manager for each video (with retry)\\n  5. Upload each to YouTube\\n\\nThis takes 2-4 HOURS. The page can stay open to monitor progress.')) return;
  const btn = event.target;
  btn.disabled = true; btn.textContent = '⏳ Batch Running...';
  try {
    const res = await fetch('api/auto/weekly-batch', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:4,skip_research:false})});
    const data = await res.json();
    document.getElementById('auto-run-section').style.display = 'block';
    pollAutoStatus(data.run_id, null, 'auto-run-progress');
    btn.disabled=false; btn.textContent='🚀 Weekly Batch (4 Videos)';
  } catch(e) { alert('Error: '+e.message); btn.disabled=false; btn.textContent='🚀 Weekly Batch (4 Videos)'; }
}
function pollAutoStatus(runId, callback, progressEl) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch('api/auto/status/'+runId, {cache:'no-store'});
      const run = await res.json();
      if (progressEl) {
        const el = document.getElementById(progressEl);
        const pct = run.progress||0;
        const step = run.step||'';
        el.innerHTML = `<div style="margin:10px 0"><div style="background:var(--bg);border-radius:6px;height:24px;overflow:hidden"><div style="background:var(--accent);height:100%;width:${pct}%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600">${pct}%</div></div><p style="margin-top:8px;color:var(--dim);font-size:13px">Step: ${step}</p></div>`;
        if (run.log && run.log.length) {
          el.innerHTML += '<div style="max-height:200px;overflow-y:auto;font-size:12px;color:var(--dim);background:var(--bg);padding:10px;border-radius:8px;margin-top:8px">'+run.log.slice(-15).map(l=>'<div>'+l+'</div>').join('')+'</div>';
        }
        if (run.video_url) {
          el.innerHTML += '<a href="'+run.video_url+'" target="_blank" style="color:var(--green);font-weight:600">✅ Watch on YouTube →</a>';
        }
      }
      if (run.status === 'done' || run.status === 'error') {
        clearInterval(interval);
        if (callback) callback(run);
        loadHistory(); loadAutoRuns();
      }
    } catch(e) { console.error(e); }
  }, 3000);
}
async function loadSchedule() {
  try {
    const res = await fetch('api/auto/schedule', {cache:'no-store'});
    if (!res.ok) return;
    const data = await res.json();
    const job = data.find(j => j.id === 'vox-weekly-batch');
    if (job) {
      document.getElementById('schedule-enabled').checked = job.enabled;
      document.getElementById('schedule-status').textContent = job.enabled ? '🟢 Active' : '🔴 Disabled';
    }
  } catch(e) {}
}
async function toggleSchedule() {
  const enabled = document.getElementById('schedule-enabled').checked;
  try {
    await fetch('api/auto/schedule', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
    document.getElementById('schedule-status').textContent = enabled ? '🟢 Active' : '🔴 Disabled';
  } catch(e) { alert('Error: '+e.message); }
}
async function loadHistory() {
  try {
    const res = await fetch('api/auto/history', {cache:'no-store'});
    const data = await res.json();
    const el = document.getElementById('auto-history');
    if (!data.length) { el.innerHTML = '<p style="color:var(--dim)">No autonomous runs yet.</p>'; return; }
    el.innerHTML = data.slice(-10).reverse().map(e => `
      <div style="display:flex;gap:8px;padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="flex:1">
          <a href="${e.video_url||'#'}" target="_blank" style="color:var(--text);text-decoration:none">${e.yt_title||e.title||e.topic||'Untitled'}</a>
          <br><span style="color:var(--dim);font-size:12px">${e.status} · ${new Date(e.timestamp).toLocaleString()}</span>
        </div>
      </div>`).join('');
  } catch(e) {}
}
async function loadAutoRuns() {
  try {
    const res = await fetch('api/auto/runs', {cache:'no-store'});
    const data = await res.json();
    // Just show active runs count
    const active = data.filter(r => r.status === 'running');
    if (active.length) { document.getElementById('auto-active-count').textContent = active.length + ' running'; }
  } catch(e) {}
}
document.addEventListener('DOMContentLoaded', autoInit);
autoInit();
</script>
</body>
</html>"""


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def index():
    resp = make_response(render_template_string(HTML))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route("/api/create", methods=["POST"])
def api_create():
    global job_counter

    data = request.json
    beats = data.get("beats")
    if not beats:
        return jsonify({"error": "beats.json is required"}), 400

    topic = beats.get("topic", "Untitled")
    project_slug = beats.get("project") or slugify(topic)
    project_dir = OUT / project_slug
    
    # Don't overwrite existing projects — add timestamp suffix
    if project_dir.exists() and (project_dir / "beats.json").exists():
        project_dir = OUT / f"{project_slug}-{int(time.time())}"
    
    project_dir.mkdir(parents=True, exist_ok=True)

    # Write beats.json
    beats_path = project_dir / "beats.json"
    with open(beats_path, "w") as f:
        json.dump(beats, f, indent=2)

    job_id = f"job_{job_counter}"
    job_counter += 1

    with job_lock:
        jobs[job_id] = {
            "id": job_id,
            "topic": topic,
            "duration": sum(float(s.get("dur", 5)) for b in beats.get("beats", []) for s in b.get("shots", [])),
            "aspect": beats.get("aspect", "16:9"),
            "theme": beats.get("theme", "unknown"),
            "status": "keyframes",
            "progress": 0,
            "step_index": 0,
            "step_total": 4,
            "step_label": "🎨 Generating collage keyframes (AI images)...",
            "created_at": time.time(),
            "log": [
                f"[{datetime.now().strftime('%H:%M:%S')}] Script received: {len(beats.get('beats', []))} beats, {sum(len(b.get('shots', [])) for b in beats.get('beats', []))} shots",
                f"[{datetime.now().strftime('%H:%M:%S')}] Project: {project_slug}",
            ],
            "error": None,
            "result_video": None,
            "project_dir": str(project_dir),
            "cancel": False,
        }
        save_jobs()

    thread = threading.Thread(target=run_pipeline, args=(job_id, project_dir), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})

@app.route("/api/jobs")
def api_jobs():
    with job_lock:
        return jsonify(list(jobs.values()))


@app.route("/api/generate-script", methods=["POST"])
def api_generate_script():
    """Generate beats.json for a Vox-style video. Tries Pollinations AI first, falls back to template."""
    import urllib.request
    import urllib.error
    import random

    data = request.json or {}
    topic = data.get("topic", "Untitled")
    duration = int(data.get("duration", 30))
    aspect = data.get("aspect", "16:9")
    theme = data.get("theme", "newsprint-editorial")
    arc = data.get("arc", "hook_payoff")
    language = data.get("language", "en")
    voice = data.get("voice", "leo")
    extra_prompt = data.get("prompt", "")

    # Long-form video support: ~6-8s per beat
    # 30s=6 beats, 60s=10, 120s=18, 180s=24, 300s=40, 600s(10min)=80, 900s(15min)=120
    beat_count = (3 if duration <= 15 else 6 if duration <= 30 else 10 if duration <= 60
                  else 14 if duration <= 90 else 18 if duration <= 120 else 24 if duration <= 180
                  else 36 if duration <= 240 else 40 if duration <= 300
                  else 60 if duration <= 480 else 80 if duration <= 600 else 120)

    # For long videos (40+ beats), generate in chunks to avoid AI timeout
    CHUNK_SIZE = 20  # beats per AI call
    use_chunked = beat_count > CHUNK_SIZE

    # Theme-specific scene direction injected into the system prompt
    theme_direction = ""
    if theme == "stick-figure":
        theme_direction = (
            "\n\nSTICK-FIGURE STYLE DIRECTION:\n"
            "- Every scene MUST feature 2D stick-figure characters as the main visual element.\n"
            "- Describe the stick figures' ACTIONS and POSES: walking, running, pointing, "
            "carrying objects, falling, jumping, thinking, building, fighting, celebrating.\n"
            "- Scene descriptions should read like stage directions for stick-figure actors: "
            "'a stick figure walks toward a giant clock and points at it' not just 'a person near a clock'.\n"
            "- Keep characters as simple stick figures (line body, round head, no facial details) — "
            "their body language tells the story.\n"
            "- Props and scenery are simple flat geometric paper shapes (rectangles, circles, triangles).\n"
            "- Mix with Vox collage elements: torn paper, halftone, bold flat backgrounds.\n"
            "- element_motion should describe how the stick figures MOVE: 'stick figure runs and trips, "
            "arms flailing; paper gears spin', not abstract element motion.\n"
        )

    system_prompt = (
        "You are VOX DIRECTOR — an elite short-form documentary video scriptwriter. "
        "Produce a complete beats.json for a Vox-style paper-collage video.\n\n"
        "NARRATIVE ARCS: hook_payoff, timeline, how_it_works, man_in_hole, myth_buster, listicle\n"
        "HOOK RULES: Hook in <=3 seconds. Beat 1 headline MUST carry the payoff-promise.\n"
        "Each beat has 2 shots (wide + detail). Vary camera moves. Rich element motion.\n"
        "Bold flat background colors. End with hard_cut.\n\n"
        "CRITICAL NARRATION RULES:\n"
        "- EVERY beat MUST have a UNIQUE narration line — NEVER repeat the same text twice.\n"
        "- Each narration MUST contain specific facts, details, or insights about the TOPIC.\n"
        "- Narration should tell a complete story about the topic from start to finish.\n"
        "- Do NOT use generic filler like 'Now you know the secret' for multiple beats.\n"
        "- Each line should be 8-20 words, punchy, and informative.\n\n"
        "ALSO generate a YouTube-optimized title and description:\n"
        "- title: Max 70 chars, click-worthy, includes the topic, uses curiosity/power words. NOT clickbait.\n"
        "- description: 2-3 paragraphs. First line is a hook summary. Then key points from the video. "
        "End with relevant hashtags (3-5). Include a line inviting viewers to subscribe.\n"
        "- tags: 8-12 relevant SEO tags for YouTube.\n\n"
        'Output ONLY valid JSON with this schema:\n'
        '{"project":"slug","topic":"...","language":"en","aspect":"16:9","style":"collage",'
        '"provider":"free","theme":"...","arc":"...","voice":{"voice_id":"leo","language":"en","speed":1.0},'
        '"music":"...","caption_style":"white","captions":true,"watermark":"",'
        '"yt_title":"...","yt_description":"...","yt_tags":["tag1","tag2"],'
        '"beats":[{"id":1,"title_cn":"","title_en":"HEADLINE","bg":"color","feel":"tone",'
        '"hook":"hook_pattern","narration":"...","shots":[{"id":"a","dur":5,"title":true,'
        '"shot_size":"WIDE","camera_move":"push_in","scene":"...","element_motion":"..."},'
        '{"id":"b","dur":5,"title":false,"shot_size":"CLOSE","camera_move":"parallax",'
        '"scene":"...","element_motion":"..."}]}]}'
        + theme_direction
    )

    user_prompt = (
        f"Create a beats.json for a {duration}-second Vox-style paper-collage video.\n\n"
        f"TOPIC: {topic}\nDURATION: {duration} seconds\nASPECT: {aspect}\n"
        f"THEME: {theme}\nARC: {arc}\nLANGUAGE: {language}\n"
        f"CREATIVE DIRECTION: {extra_prompt or 'None — use your best judgment.'}\n\n"
        f"Requirements:\n- {beat_count} beats, each with 2 shots (wide + detail)\n"
        f"- Hook in first 3 seconds\n- Vary camera moves between adjacent beats\n"
        f"- Rich element motion\n- Punchy narration in {language}\n"
        f"- Bold flat background colors\n- End with hard_cut\n"
        + (f"\nLONG-FFORM VIDEO ({duration}s): This is a long-form documentary video. "
           f"Structure it with clear chapters/sections — each section tells a distinct "
           f"part of the story with a mini-payoff. Include callbacks to earlier beats. "
           f"Build tension across beats. Vary pacing — some beats slower (emotional), "
           f"some faster (action). Each narration line should be 10-25 words with "
           f"specific facts, dates, names. NEVER repeat narration across beats.\n"
           if duration >= 300 else "")
        + "\nOutput ONLY the JSON. No markdown, no code fences."
    )

    # --- Method 1: Agnes AI (same API used for video generation, free) ---
    # Load Agnes API keys (multi-key rotation)
    key_path = os.path.join(os.path.dirname(__file__), ".agnes_keys")
    agnes_keys = []
    with open(key_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                agnes_keys.append(line)
    if not agnes_keys:
        return jsonify({"error": "No Agnes API keys found (.agnes_keys)"}), 500

    import random as _rng
    url = "https://apihub.agnes-ai.com/v1/chat/completions"

    def _call_agnes(sys_prompt, usr_prompt, max_tokens=8000, timeout=120):
        """Single Agnes AI call with retry across keys. Returns parsed JSON or None."""
        for attempt in range(3):
            ai_key = _rng.choice(agnes_keys)
            try:
                payload = json.dumps({
                    "model": "agnes-2.5-flash",
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": usr_prompt}
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.8
                }).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {ai_key}"
                }, method="POST")
                print(f"[generate-script] Agnes AI call (attempt {attempt+1}, {max_tokens} tokens)...")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8").strip()

                resp_data = json.loads(raw)
                content = resp_data["choices"][0]["message"]["content"].strip()

                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?\s*", "", content)
                    content = re.sub(r"\s*```$", "", content)

                # Extract JSON: try direct parse, then brace-matching fallback
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    match = re.search(r"\{[\s\S]*\}", content)
                    if match:
                        try:
                            return json.loads(match[0])
                        except json.JSONDecodeError:
                            start = content.find("{")
                            if start >= 0:
                                depth = 0
                                end = start
                                for i in range(start, len(content)):
                                    if content[i] == "{":
                                        depth += 1
                                    elif content[i] == "}":
                                        depth -= 1
                                        if depth == 0:
                                            end = i + 1
                                            break
                                if depth == 0:
                                    return json.loads(content[start:end])
                    print(f"[generate-script] Could not parse JSON from response")
            except Exception as attempt_err:
                print(f"[generate-script] Attempt {attempt+1} failed: {attempt_err}")
                continue
        return None

    if use_chunked:
        # --- Chunked generation for long videos ---
        # Phase 1: Generate an outline (chapter list with beat ranges)
        print(f"[generate-script] Chunked mode: {beat_count} beats in chunks of {CHUNK_SIZE}")
        n_chunks = (beat_count + CHUNK_SIZE - 1) // CHUNK_SIZE

        outline_prompt = (
            f"Create an outline for a {duration}-second documentary video about: {topic}\n"
            f"Total beats: {beat_count}, divided into {n_chunks} chapters.\n"
            f"Each chapter covers ~{CHUNK_SIZE} beats.\n"
            f"ARC: {arc}\n"
            f"For each chapter, provide: chapter number, title, beat range (start-end), "
            f"and a 2-sentence summary of what happens in that chapter.\n\n"
            f"Output ONLY valid JSON: "
            f'{{"chapters":[{{"num":1,"title":"...","beat_start":1,"beat_end":20,'
            f'"summary":"..."}}]}}'
        )
        outline = _call_agnes(system_prompt, outline_prompt, max_tokens=2000, timeout=90)
        if not outline or "chapters" not in outline:
            print(f"[generate-script] Outline generation failed, falling back to single call")
            use_chunked = False
        else:
            chapters = outline["chapters"]
            print(f"[generate-script] Outline: {len(chapters)} chapters")
            all_beats_list = []
            yt_meta = {}

            for ci, chap in enumerate(chapters):
                bs = chap.get("beat_start", ci * CHUNK_SIZE + 1)
                be = chap.get("beat_end", bs + CHUNK_SIZE - 1)
                n_beats = be - bs + 1
                chap_title = chap.get("title", f"Chapter {ci+1}")
                chap_summary = chap.get("summary", "")

                chunk_sys = (
                    "You are VOX DIRECTOR — an elite documentary video scriptwriter. "
                    "Produce beats for ONE chapter of a Vox-style paper-collage video.\n\n"
                    "Each beat has 2 shots (wide + detail). Vary camera moves. Rich element motion.\n"
                    "Bold flat background colors.\n\n"
                    "CRITICAL NARRATION RULES:\n"
                    "- EVERY beat MUST have a UNIQUE narration line — NEVER repeat.\n"
                    "- Each narration MUST contain specific facts, dates, names.\n"
                    "- Each line should be 10-25 words, punchy, and informative.\n\n"
                    'Output ONLY valid JSON with this schema:\n'
                    '{"beats":[{"id":N,"title_cn":"","title_en":"HEADLINE","bg":"color",'
                    '"feel":"tone","hook":"hook_pattern","narration":"...",'
                    '"shots":[{"id":"a","dur":5,"title":true,"shot_size":"WIDE",'
                    '"camera_move":"push_in","scene":"...","element_motion":"..."},'
                    '{"id":"b","dur":5,"title":false,"shot_size":"CLOSE",'
                    '"camera_move":"parallax","scene":"...","element_motion":"..."}]}]}'
                )
                if theme == "stick-figure":
                    chunk_sys += theme_direction

                chunk_usr = (
                    f"Create {n_beats} beats for chapter {ci+1} of a documentary about: {topic}\n"
                    f"CHAPTER TITLE: {chap_title}\n"
                    f"CHAPTER SUMMARY: {chap_summary}\n"
                    f"Beat IDs: {bs} to {be}\n"
                    f"ASPECT: {aspect}\nTHEME: {theme}\nLANGUAGE: {language}\n\n"
                    + ("This is a LONG-FORM video. Include specific facts, dates, names. "
                       "Build tension. Vary pacing.\n" if duration >= 300 else "")
                    + "Output ONLY the JSON. No markdown, no code fences."
                )

                max_tok = min(16000, 2000 + n_beats * 250)
                chunk_result = _call_agnes(chunk_sys, chunk_usr, max_tokens=max_tok, timeout=120)

                if chunk_result and "beats" in chunk_result:
                    chunk_beats = chunk_result["beats"]
                    # Fix beat IDs to be sequential
                    for bi, b in enumerate(chunk_beats):
                        b["id"] = bs + bi
                    all_beats_list.extend(chunk_beats)
                    print(f"[generate-script] Chapter {ci+1}: {len(chunk_beats)} beats (total: {len(all_beats_list)})")

                    # Grab YT metadata from first chunk
                    if ci == 0:
                        yt_meta["yt_title"] = chunk_result.get("yt_title", topic.title())
                        yt_meta["yt_description"] = chunk_result.get("yt_description", "")
                        yt_meta["yt_tags"] = chunk_result.get("yt_tags", ["history", "documentary"])
                else:
                    print(f"[generate-script] Chapter {ci+1} FAILED — skipping")

            if all_beats_list:
                beats = {
                    "project": topic.lower().replace(" ", "-")[:50],
                    "topic": topic,
                    "language": language,
                    "aspect": aspect,
                    "style": "collage",
                    "provider": "free",
                    "theme": theme,
                    "arc": arc,
                    "voice": {"voice_id": voice, "language": language, "speed": 1.0},
                    "music": "dramatic documentary",
                    "caption_style": "white",
                    "captions": True,
                    "watermark": "",
                    "yt_title": yt_meta.get("yt_title", topic.title()),
                    "yt_description": yt_meta.get("yt_description", f"A deep dive into {topic}."),
                    "yt_tags": yt_meta.get("yt_tags", ["history", "documentary", "educational"]),
                    "beats": all_beats_list,
                }
                print(f"[generate-script] Chunked success! {len(all_beats_list)} total beats")
            else:
                print(f"[generate-script] All chunks failed")
                return jsonify({"error": "AI script generation failed — all chunks returned empty"}), 502

    if not use_chunked:
        # --- Single-call generation (short videos) ---
        beats = _call_agnes(system_prompt, user_prompt,
                            max_tokens=min(32000, 2000 + beat_count * 250),
                            timeout=120)

    if not beats:
        print(f"[generate-script] All attempts failed.")
        return jsonify({"error": f"AI script generation failed after 3 attempts. Check Agnes API keys (.agnes_keys)."}), 502

    try:
        # Ensure required fields
        beats["provider"] = "free"
        beats["aspect"] = aspect
        beats["theme"] = theme
        beats["arc"] = arc
        beats["language"] = language
        if not beats.get("voice"):
            beats["voice"] = {}
        beats["voice"]["voice_id"] = voice
        beats["voice"]["language"] = language
        if not beats.get("captions"):
            beats["captions"] = True
        if not beats.get("caption_style"):
            beats["caption_style"] = "white"
        if not beats.get("watermark"):
            beats["watermark"] = ""
        if not beats.get("style"):
            beats["style"] = "collage"
        if not beats.get("project"):
            beats["project"] = slugify(topic)

        # Ensure YouTube metadata fields (fallback if AI didn't generate)
        if not beats.get("yt_title"):
            beats["yt_title"] = topic[:70]
        if not beats.get("yt_description"):
            narrations = [b.get("narration", "") for b in beats.get("beats", [])[:3]]
            beats["yt_description"] = (
                f"{topic}\n\n" +
                "\n".join(f"• {n}" for n in narrations if n) + "\n\n"
                "🔔 Subscribe for more Vox-style explainer videos!\n\n"
                "#shorts #explainer #documentary"
            )
        if not beats.get("yt_tags"):
            beats["yt_tags"] = [topic.lower().split()[0], "explainer", "documentary",
                                "vox", "education", "facts", "shorts", "didyouknow"]

        for beat in beats.get("beats", []):
            if not beat.get("title_cn"):
                beat["title_cn"] = ""
            if not beat.get("hook"):
                beat["hook"] = "surprising_stat" if beat.get("id") == 1 else "none"
            # Ensure narration is not empty
            if not beat.get("narration"):
                beat["narration"] = f"Here's what's fascinating about {topic}."
            for shot in beat.get("shots", []):
                if not shot.get("scene"):
                    shot["scene"] = (beat.get("narration", ""))[:100]
                if not shot.get("element_motion"):
                    shot["element_motion"] = "paper elements drift, halftone pulses"

        # Post-process: fix duplicate narrations from AI (common Agnes bug)
        seen_narrations = {}
        for beat in beats.get("beats", []):
            n = beat.get("narration", "").strip()
            if n in seen_narrations:
                seen_narrations[n] += 1
                # Append beat number to make unique
                beat["narration"] = f"{n} (Part {seen_narrations[n]})"
            else:
                seen_narrations[n] = 1

        return jsonify({"beats": beats, "source": "ai"})

    except Exception as post_err:
        print(f"[generate-script] Post-processing error: {post_err}")
        return jsonify({"error": f"Script post-processing failed: {str(post_err)}"}), 500


def _generate_template_beats(topic, duration, aspect, theme, arc, language, voice, extra_prompt, beat_count):
    """Generate a beats.json structure using templates. No external AI needed."""
    import random

    random.seed(hash(topic) & 0xFFFFFFFF)

    # Color palette that travels across beats
    color_palettes = [
        ["#1a1a2e", "#16213e", "#0f3460", "#533483", "#e94560", "#f5a623"],
        ["#2d3436", "#636e72", "#00b894", "#00cec9", "#0984e3", "#6c5ce7"],
        ["#2c1810", "#5c3d2e", "#8b5a3c", "#d4a373", "#faedcd", "#e9c46a"],
        ["#03071e", "#370617", "#6a040f", "#9d0208", "#dc2f02", "#f48c06"],
        ["#1b4332", "#2d6a4f", "#40916c", "#52b788", "#74c69d", "#b7e4c7"],
        ["#10002b", "#240046", "#3c096c", "#5a189a", "#7b2cbf", "#9d4edd"],
    ]
    palette = random.choice(color_palettes)

    # Camera moves — rotate, no two adjacent same
    cam_moves_a = ["push_in", "pull_out", "pan", "tilt", "parallax", "static"]
    cam_moves_b = ["parallax", "pan", "tilt", "push_in", "pull_out", "element"]

    # Element motion templates
    motions = [
        "paper cutouts drift upward, halftone dots pulse rhythmically",
        "scattered elements float across frame, edges shimmer",
        "layered paper shapes slide in from sides, soft parallax depth",
        "icons bounce in sequence, background texture ripples",
        "elements pivot and sway like paper in wind, dust particles drift",
        "horizontal stripes animate across, geometric shapes tumble",
        "scattered photographs arrange themselves, corners curl slightly",
        "wave-like motion across paper layers, sparkles flicker",
    ]

    # Narration templates per arc
    arc_templates = {
        "hook_payoff": [
            ("Here's what nobody tells you about {topic}.", "THE REAL STORY"),
            ("It started simple — but then everything changed.", "HOW IT BEGAN"),
            ("The numbers don't add up — unless you know this.", "THE SECRET"),
            ("What happened next surprised everyone.", "THE TWIST"),
            ("This changes everything you thought you knew.", "THE REVEAL"),
            ("And that's why it matters more than ever.", "THE PAYOFF"),
        ],
        "timeline": [
            ("It all started centuries ago — long before you'd expect.", "ORIGINS"),
            ("For generations, it stayed exactly the same.", "EARLY DAYS"),
            ("Then one discovery changed everything.", "THE TURNING POINT"),
            ("It spread faster than anyone predicted.", "SPREAD"),
            ("By the modern era, it was everywhere.", "MAINSTREAM"),
            ("Today, it's bigger than anyone imagined.", "THE PRESENT"),
        ],
        "how_it_works": [
            ("Ever wondered how {topic} actually works?", "THE QUESTION"),
            ("It comes down to one simple principle.", "THE BASICS"),
            ("Here's where it gets interesting.", "THE MECHANISM"),
            ("Every piece has to work together perfectly.", "THE SYSTEM"),
            ("And that's what makes it so powerful.", "THE IMPACT"),
            ("Now you know the secret behind it.", "THE ANSWER"),
        ],
        "man_in_hole": [
            ("Things were going great — until they weren't.", "THE FALL"),
            ("Nobody saw it coming.", "THE DROP"),
            ("It got worse before it got better.", "ROCK BOTTOM"),
            ("But then something remarkable happened.", "THE COMEBACK"),
            ("Slowly, piece by piece, it returned.", "RECOVERY"),
            ("Today it's stronger than it ever was.", "STRONGER"),
        ],
        "myth_buster": [
            ("You've probably heard this before — but it's wrong.", "THE MYTH"),
            ("Everyone believes it. Almost nobody questions it.", "THE LIE"),
            ("Here's what's actually happening.", "THE TRUTH"),
            ("The evidence is overwhelming.", "THE PROOF"),
            ("Once you see it, you can't unsee it.", "THE SHIFT"),
            ("Don't believe everything you hear.", "THE LESSON"),
        ],
        "listicle": [
            ("Here are the most surprising things about {topic}.", "THE LIST"),
            ("Number five will shock you.", "NUMBER FIVE"),
            ("This one changed everything.", "NUMBER FOUR"),
            ("You won't believe this happened.", "NUMBER THREE"),
            ("Almost nobody knows this one.", "NUMBER TWO"),
            ("And here's the most important one.", "NUMBER ONE"),
        ],
    }

    templates = arc_templates.get(arc, arc_templates["hook_payoff"])

    # Build beats — cycle through templates but vary narration with topic + index
    # so no two beats have identical narration (the old bug: beats 6+ all repeated
    # the last template's text verbatim).
    beats_list = []
    for i in range(beat_count):
        tpl_idx = i % len(templates)
        narration_tpl, title_en = templates[tpl_idx]
        narration = narration_tpl.replace("{topic}", topic.lower())

        # If we've cycled through all templates, append variation to avoid duplicates
        if i >= len(templates):
            cycle = i // len(templates)
            # Add a unique suffix so each repetition reads differently
            variations = [
                f" And this part of {topic.lower()}? Even more fascinating.",
                f" Here's another layer to the {topic.lower()} story.",
                f" But wait — there's more to {topic.lower()} than you'd think.",
                f" The deeper you go into {topic.lower()}, the stranger it gets.",
                f" And this detail about {topic.lower()} changes everything.",
            ]
            narration += variations[cycle % len(variations)]
            title_en += f" (Part {cycle + 2})"

        # Hook pattern
        if i == 0:
            hook = "surprising_stat"
        elif i == beat_count - 1:
            hook = "hard_cut"
        else:
            hook = "none"

        # Camera moves — ensure no two adjacent beats use the same
        move_a = cam_moves_a[i % len(cam_moves_a)]
        move_b = cam_moves_b[i % len(cam_moves_b)]
        if i > 0 and move_a == beats_list[-1]["shots"][1]["camera_move"]:
            move_a = cam_moves_a[(i + 2) % len(cam_moves_a)]

        # Feel/tone
        feels = ["curious", "intense", "hopeful", "dramatic", "revealing", "reflective", "energetic", "cinematic"]
        feel = feels[i % len(feels)]

        # Scene descriptions — unique per beat, describing a specific moment
        scene_templates_a = [
            f"Wide shot: the origins of {topic.lower()}, ancient setting, historical figures, bold cut-out shapes, {feel} mood",
            f"Wide shot: early development of {topic.lower()}, people discovering and exploring, paper textures, {feel} mood",
            f"Wide shot: a breakthrough moment for {topic.lower()}, dramatic scene with key objects, {feel} mood",
            f"Wide shot: {topic.lower()} spreading across the world, maps and globes as paper cut-outs, {feel} mood",
            f"Wide shot: modern era of {topic.lower()}, technology and progress, bold graphic elements, {feel} mood",
            f"Wide shot: the global impact of {topic.lower()} today, crowd of people, symbols and icons, {feel} mood",
            f"Wide shot: cultural significance of {topic.lower()}, artistic elements, museums and galleries, {feel} mood",
            f"Wide shot: future of {topic.lower()}, futuristic paper cut-out shapes, {feel} mood",
        ]
        scene_templates_b = [
            f"Close-up: intricate detail of {topic.lower()} — hands working, tools, paper cut-out textures",
            f"Close-up: a key object from {topic.lower()} — zoomed in, fine patterns, focused composition",
            f"Close-up: a person's face reacting to {topic.lower()} — expressive, paper cut-out portrait",
            f"Close-up: text and headlines about {topic.lower()} — newspaper clipping, bold typography",
            f"Close-up: mechanical or technical detail of {topic.lower()} — gears, circuits, paper shapes",
            f"Close-up: celebration or reaction to {topic.lower()} — confetti, movement, energy",
            f"Close-up: a map or diagram about {topic.lower()} — paper cut-out chart, bold colors",
            f"Close-up: the final reveal of {topic.lower()} — dramatic detail, high contrast",
        ]
        scene_a = scene_templates_a[i % len(scene_templates_a)]
        scene_b = scene_templates_b[i % len(scene_templates_b)]

        # Element motion
        motion_a = motions[i % len(motions)]
        motion_b = motions[(i + 3) % len(motions)]

        # Duration per shot
        shot_dur = max(3, duration // (beat_count * 2))

        beat = {
            "id": i + 1,
            "title_cn": "",
            "title_en": title_en,
            "bg": palette[i % len(palette)],
            "feel": feel,
            "hook": hook,
            "narration": narration,
            "shots": [
                {
                    "id": "a",
                    "dur": shot_dur + 1,
                    "title": True,
                    "shot_size": "WIDE",
                    "camera_move": move_a,
                    "scene": scene_a,
                    "element_motion": motion_a
                },
                {
                    "id": "b",
                    "dur": shot_dur,
                    "title": False,
                    "shot_size": "CLOSE",
                    "camera_move": move_b,
                    "scene": scene_b,
                    "element_motion": motion_b
                }
            ]
        }
        beats_list.append(beat)

    # Build final beats.json
    result = {
        "project": slugify(topic),
        "topic": topic,
        "language": language,
        "aspect": aspect,
        "style": "collage",
        "provider": "free",
        "theme": theme,
        "arc": arc,
        "voice": {
            "voice_id": voice,
            "language": language,
            "speed": 1.0
        },
        "music": "ambient pad, documentary tone",
        "caption_style": "white",
        "captions": True,
        "watermark": "",
        "yt_title": f"{topic} — Explained in {duration} Seconds",
        "yt_description": (
            f"{topic} — explained like never before.\n\n"
            "In this short Vox-style explainer, we break down the key moments, "
            "surprising facts, and the big picture — all in a quick, visual journey.\n\n"
            "🔔 Subscribe for more bite-sized explainer videos!\n\n"
            "#shorts #explainer #documentary #facts #didyouknow"
        ),
        "yt_tags": [topic.lower().split()[0], "explainer", "documentary",
                    "vox", "education", "facts", "shorts", "didyouknow"],
        "beats": beats_list
    }

    return result

@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    if job_id in jobs:
        jobs[job_id]["cancel"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Job not found"}), 404


@app.route("/api/delete/<job_id>", methods=["POST"])
def api_delete(job_id):
    """Delete a project — removes job from memory, disk files, and persisted DB."""
    import shutil as _shutil
    with job_lock:
        if job_id not in jobs:
            return jsonify({"error": "Job not found"}), 404
        job = jobs.pop(job_id)
        save_jobs()

    # Delete project files from disk
    project_dir = Path(job.get("project_dir", ""))
    if project_dir.exists() and project_dir.is_dir():
        try:
            _shutil.rmtree(project_dir)
            print(f"[delete] Removed project dir: {project_dir}")
        except Exception as e:
            print(f"[delete] Could not remove project dir: {e}")

    return jsonify({"ok": True, "message": f"Deleted {job_id}"})

@app.route("/api/video/<job_id>")
def api_video(job_id):
    if job_id in jobs and jobs[job_id].get("result_video"):
        path = jobs[job_id]["result_video"]
        resp = send_file(path, mimetype="video/mp4", conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    return jsonify({"error": "Video not found"}), 404

@app.route("/api/upload-yt/<job_id>", methods=["POST"])
def api_upload_yt(job_id):
    """Upload finished video to YouTube via Composio API.

    Requires COMPOSIO_API_KEY env var (or .composio_key file) and an active
    YouTube connected account on Composio dashboard.
    """
    if job_id not in jobs or not jobs[job_id].get("result_video"):
        return jsonify({"error": "Video not found"}), 404

    data = request.json or {}
    yt_title = data.get("title", "")
    yt_description = data.get("description", "")
    yt_tags = data.get("tags", [])
    privacy = data.get("privacy", "public")

    video_path = Path(jobs[job_id]["result_video"])

    # Get metadata from beats.json if not provided
    project_dir = Path(jobs[job_id]["project_dir"])
    beats_path = project_dir / "beats.json"
    if beats_path.exists():
        beats_doc = json.loads(beats_path.read_text())
        topic = jobs[job_id].get("topic", "Untitled")
        theme = beats_doc.get("theme", "vox")
        if not yt_title:
            yt_title = f"{topic} | {theme.upper()} Animation"
        if not yt_description:
            desc_lines = [topic, "", f"Style: {theme} animation collage", "", "Generated by Vox Director Studio"]
            yt_description = "\n".join(desc_lines)
        if not yt_tags:
            yt_tags = [theme, "animation", "short", "educational"] + topic.lower().split()[:3]

    # Upload via Composio in a background thread (upload takes 1-3 min)
    upload_status_key = f"upload_{job_id}"
    jobs[upload_status_key] = {"status": "uploading", "started": time.time()}

    def do_upload():
        try:
            import subprocess as sp
            # Try direct YouTube API first (no third-party dependency)
            yt_script = SCRIPTS / "youtube_direct.py"
            if not yt_script.exists():
                yt_script = SCRIPTS / "youtube.py"  # Fallback to Composio
            cmd = [
                sys.executable,
                str(yt_script),
                str(project_dir),
                "--title", yt_title,
                "--description", yt_description,
                "--tags", ",".join(yt_tags),
                "--privacy", privacy,
                "--video", str(video_path),
            ]
            env = os.environ.copy()
            env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            result = sp.run(cmd, capture_output=True, text=True, timeout=600, env=env)
            if result.returncode == 0:
                # Parse video URL from output
                video_url = ""
                for line in result.stdout.split("\n"):
                    if "watch?v=" in line:
                        # Extract URL from output line
                        import re
                        m = re.search(r'https://www\.youtube\.com/watch\?v=\S+', line)
                        if m:
                            video_url = m.group(0)
                        else:
                            video_url = line.strip().split("URL: ")[-1]
                        break
                # Also check JSON output at the end
                if not video_url:
                    for line in reversed(result.stdout.split("\n")):
                        line = line.strip()
                        if line.startswith("{") and "video_url" in line:
                            try:
                                d = json.loads(line)
                                video_url = d.get("video_url", "")
                            except:
                                pass
                        if video_url:
                            break
                jobs[upload_status_key] = {
                    "status": "done",
                    "video_url": video_url,
                    "output": result.stdout[-500:],
                }
            else:
                jobs[upload_status_key] = {
                    "status": "error",
                    "error": result.stderr[-500:] or result.stdout[-500:],
                }
        except Exception as e:
            jobs[upload_status_key] = {"status": "error", "error": str(e)}

    threading.Thread(target=do_upload, daemon=True).start()

    return jsonify({"ok": True, "message": "Upload started", "status_key": upload_status_key})


@app.route("/api/upload-status/<job_id>")
def api_upload_status(job_id):
    """Check YouTube upload progress."""
    upload_status_key = f"upload_{job_id}"
    if upload_status_key in jobs:
        return jsonify(jobs[upload_status_key])
    return jsonify({"status": "idle"})


# ============================================================
# COMPOSIO OAUTH FLOW
# ============================================================

COMPOSIO_CONSUMER_KEY = ""
COMPOSIO_OAUTH_CLIENT_ID = ""

def load_composio_keys():
    """Load Composio consumer key and OAuth client ID from files."""
    global COMPOSIO_CONSUMER_KEY, COMPOSIO_OAUTH_CLIENT_ID
    key_file = Path("/opt/baal-agent/workspace/vox-director/.composio_key")
    client_file = Path("/opt/baal-agent/workspace/vox-director/.composio_client_id")
    if key_file.exists():
        COMPOSIO_CONSUMER_KEY = key_file.read_text().strip()
    if client_file.exists():
        COMPOSIO_OAUTH_CLIENT_ID = client_file.read_text().strip()

load_composio_keys()

# Store JWT tokens
composio_tokens = {}

# Store PKCE code verifiers (in-memory, keyed by state)
_pkce_verifiers = {}


def _generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    import secrets as _secrets
    import hashlib
    import base64

    # Generate random code_verifier (43-128 chars, base64url-safe)
    verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).decode("ascii").rstrip("=")
    # code_challenge = base64url(SHA256(verifier))
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


@app.route("/api/composio/auth-url")
def api_composio_auth_url():
    """Generate OAuth authorization URL for Composio MCP with PKCE."""
    load_composio_keys()
    if not COMPOSIO_OAUTH_CLIENT_ID:
        return jsonify({"error": "OAuth client ID not configured"}), 500

    import secrets as _secrets
    import urllib.parse

    redirect_uri = "https://chimney-copper-marriage-salute.2n6.me/vox/oauth/callback"

    # Generate PKCE pair
    verifier, challenge = _generate_pkce()
    state = _secrets.token_urlsafe(16)
    _pkce_verifiers[state] = verifier

    params = {
        "client_id": COMPOSIO_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email offline_access",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }

    auth_url = f"https://login.composio.dev/oauth2/authorize?{urllib.parse.urlencode(params)}"
    return jsonify({"auth_url": auth_url})


@app.route("/oauth/callback")
def oauth_callback():
    """Handle OAuth callback from Composio (OAuth 2.1 with PKCE)."""
    code = request.args.get("code", "")
    error = request.args.get("error", "")
    state = request.args.get("state", "")

    if error:
        return f"<h2>❌ Authorization failed</h2><p>{error}</p>"

    if not code:
        return "<h2>❌ No authorization code received</h2>", 400

    # Retrieve PKCE verifier for this state
    verifier = _pkce_verifiers.pop(state, "")
    if not verifier:
        return "<h2>❌ Session expired</h2><p>Please try connecting again.</p>", 400

    import httpx
    redirect_uri = "https://chimney-copper-marriage-salute.2n6.me/vox/oauth/callback"

    try:
        r = httpx.post(
            "https://login.composio.dev/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": COMPOSIO_OAUTH_CLIENT_ID,
                "code_verifier": verifier,
            },
            timeout=30,
        )
        if r.status_code != 200:
            return f"<h2>❌ Token exchange failed</h2><pre>{r.text}</pre>", 500

        token_data = r.json()
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")

        composio_tokens["access_token"] = access_token

        token_file = Path("/opt/baal-agent/workspace/vox-director/.composio_token")
        token_file.write_text(access_token)
        token_file.chmod(0o600)
        if refresh_token:
            rf = Path("/opt/baal-agent/workspace/vox-director/.composio_refresh")
            rf.write_text(refresh_token)
            rf.chmod(0o600)

        return """<h2>✅ Authorized!</h2>
        <p>Composio MCP access token saved. You can now upload videos to YouTube.</p>
        <p><a href="/vox/">← Back to Vox Studio</a></p>"""

    except Exception as e:
        return f"<h2>❌ Error</h2><pre>{e}</pre>", 500


@app.route("/api/composio/status")
def api_composio_status():
    """Check if Composio is authenticated (legacy)."""
    load_composio_keys()
    token_file = Path("/opt/baal-agent/workspace/vox-director/.composio_token")
    has_token = token_file.exists() or "access_token" in composio_tokens
    return jsonify({
        "has_consumer_key": bool(COMPOSIO_CONSUMER_KEY),
        "has_token": has_token,
        "needs_auth": not has_token,
    })


# ── Direct YouTube Data API v3 ───────────────────────────────────────────────

YT_CLIENT_SECRET = VOX / "client_secret.json"
YT_TOKEN_FILE = VOX / ".youtube_token.json"


@app.route("/api/yt/status")
def api_yt_status():
    """Check YouTube Data API connection status."""
    has_secret = YT_CLIENT_SECRET.exists()
    has_token = YT_TOKEN_FILE.exists()
    if has_token:
        return jsonify({"connected": True})
    elif has_secret:
        return jsonify({"connected": False, "needs_auth": True})
    else:
        return jsonify({"connected": False, "needs_secret": True})


@app.route("/api/yt/upload-secret", methods=["POST"])
def api_yt_upload_secret():
    """Save uploaded client_secret.json for YouTube API."""
    data = request.json or {}
    secret_content = data.get("content")
    if not secret_content:
        return jsonify({"error": "No content provided"}), 400
    try:
        # Validate it's valid JSON
        json.loads(secret_content)
        YT_CLIENT_SECRET.write_text(secret_content)
        os.chmod(YT_CLIENT_SECRET, 0o600)
        return jsonify({"ok": True, "message": "client_secret.json saved"})
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON"}), 400


@app.route("/api/yt/auth", methods=["POST"])
def api_yt_auth():
    """Start YouTube OAuth flow using the public URL as redirect.

    Returns the Google authorization URL. The user opens it in their browser,
    authorizes, and Google redirects to /api/yt/callback which captures the token.

    Works with both 'web' and 'installed' (Desktop app) credential types.
    """
    if not YT_CLIENT_SECRET.exists():
        return jsonify({"error": "client_secret.json not found", "needs_secret": True})

    from google_auth_oauthlib.flow import Flow

    YT_SCOPES = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube",
    ]

    redirect_uri = "https://chimney-copper-marriage-salute.2n6.me/vox/api/yt/callback"

    # Read client config to determine type (web vs installed)
    client_config = json.loads(YT_CLIENT_SECRET.read_text())
    # Flow.from_client_config requires the redirect_uri to match the configured URIs
    flow = Flow.from_client_config(
        client_config,
        scopes=YT_SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")

    # Store the flow object globally so the callback can use it
    app.yt_oauth_flow = flow
    app.yt_oauth_state = state

    return jsonify({
        "auth_url": auth_url,
        "redirect_uri": redirect_uri,
        "note": "Add this redirect URI to your Google Cloud Console if you haven't already."
    })


@app.route("/api/yt/callback")
def api_yt_callback():
    """OAuth callback — Google redirects here with the authorization code."""
    from google_auth_oauthlib.flow import Flow

    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        return f"<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#eee;'><h1 style='color:#ef4444;'>❌ Authorization Failed</h1><p>Google returned error: {error}</p></body></html>"

    if not code:
        return "<h1>Authorization failed</h1><p>No authorization code received.</p>", 400

    flow = getattr(app, "yt_oauth_flow", None)
    if not flow:
        return "<h1>Authorization failed</h1><p>OAuth flow not found. Please try again from the studio.</p>", 400

    try:
        # For web app type, the client_secret is passed automatically by Flow
        flow.fetch_token(code=code)
        creds = flow.credentials
        token_data = json.loads(creds.to_json())
        YT_TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
        os.chmod(YT_TOKEN_FILE, 0o600)

        return """<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#eee;">
        <h1 style="color:#4ade80;">✅ YouTube Connected!</h1>
        <p>Your YouTube account is now linked. You can close this tab.</p>
        <p>Go back to <a href="/vox/" style="color:#818cf8;">Vox Director Studio</a> to upload videos.</p>
        </body></html>"""
    except Exception as e:
        return f"<h1>Authorization failed</h1><p>Error exchanging code: {e}</p>", 500


def get_composio_jwt():
    """Get the saved JWT token for Composio MCP."""
    if "access_token" in composio_tokens:
        return composio_tokens["access_token"]
    token_file = Path("/opt/baal-agent/workspace/vox-director/.composio_token")
    if token_file.exists():
        return token_file.read_text().strip()
    return None


# ── Autonomous Vox Tube API ──────────────────────────────────────────────────

# Track autonomous runs
autonomous_runs = {}
autonomous_counter = 0
auto_lock = threading.Lock()

AUTO_LOG = VOX / "out" / "autonomous_log.jsonl"


@app.route("/api/auto/research", methods=["POST"])
def api_auto_research():
    """Run competitor research — scrape YouTube for viral history videos."""
    def run_research(run_id):
        run = autonomous_runs[run_id]
        run["log"].append("🔍 Searching YouTube for viral history videos...")
        try:
            sys.path.insert(0, str(SCRIPTS))
            from competitor_watcher import find_viral_history_videos, save_results
            videos = find_viral_history_videos()
            save_results(videos, OUT / "competitor_research.json")
            run["status"] = "done"
            run["result"] = {"videos_found": len(videos), "top_5": [
                {"title": v["title"], "views": v["views"], "url": v["url"]}
                for v in videos[:5]
            ]}
            run["log"].append(f"✅ Found {len(videos)} viral history videos")
        except Exception as e:
            run["status"] = "error"
            run["error"] = str(e)
            run["log"].append(f"❌ Error: {e}")

    global autonomous_counter
    with auto_lock:
        run_id = f"research_{autonomous_counter}"
        autonomous_counter += 1
        autonomous_runs[run_id] = {
            "id": run_id,
            "type": "research",
            "status": "running",
            "log": [],
            "result": None,
            "error": None,
            "created_at": time.time(),
        }

    thread = threading.Thread(target=run_research, args=(run_id,), daemon=True)
    thread.start()
    return jsonify({"run_id": run_id, "status": "started"})


@app.route("/api/auto/idea", methods=["POST"])
def api_auto_idea():
    """Generate a unique video idea from competitor research."""
    data = request.json or {}
    topic_override = data.get("topic")

    def run_idea(run_id, topic_override):
        run = autonomous_runs[run_id]
        try:
            sys.path.insert(0, str(SCRIPTS))
            research_path = OUT / "competitor_research.json"
            if not research_path.exists():
                run["status"] = "error"
                run["error"] = "No competitor research found. Run research first."
                run["log"].append("❌ No competitor research found.")
                return

            videos = json.loads(research_path.read_text())
            run["log"].append(f"📂 Loaded {len(videos)} competitor videos")

            from idea_engine import generate_idea
            run["log"].append("🧠 Generating unique idea with AI...")
            idea = generate_idea(videos, niche="history") if not topic_override else {
                "topic": topic_override,
                "title": topic_override.title(),
                "concept": f"Exploring {topic_override}",
                "angle": "User-specified topic",
                "inspired_by": [],
            }

            if idea:
                run["status"] = "done"
                run["result"] = idea
                run["log"].append(f"✅ Idea: {idea.get('title', '?')}")
            else:
                run["status"] = "error"
                run["error"] = "Idea generation failed"
                run["log"].append("❌ Idea generation failed")
        except Exception as e:
            run["status"] = "error"
            run["error"] = str(e)
            run["log"].append(f"❌ Error: {e}")

    global autonomous_counter
    with auto_lock:
        run_id = f"idea_{autonomous_counter}"
        autonomous_counter += 1
        autonomous_runs[run_id] = {
            "id": run_id,
            "type": "idea",
            "status": "running",
            "log": [],
            "result": None,
            "error": None,
            "created_at": time.time(),
        }

    thread = threading.Thread(target=run_idea, args=(run_id, topic_override), daemon=True)
    thread.start()
    return jsonify({"run_id": run_id, "status": "started"})


@app.route("/api/auto/run", methods=["POST"])
def api_auto_run():
    """Run the full autonomous pipeline: research → idea → script → video → upload."""
    data = request.json or {}
    topic_override = data.get("topic")
    skip_research = data.get("skip_research", True)

    def run_full_pipeline(run_id, topic_override, skip_research):
        run = autonomous_runs[run_id]

        def log(msg):
            ts = datetime.now().strftime('%H:%M:%S')
            run["log"].append(f"[{ts}] {msg}")
            if len(run["log"]) > 200:
                run["log"] = run["log"][-150:]

        try:
            sys.path.insert(0, str(SCRIPTS))

            # Step 1: Research
            if not skip_research:
                log("🔍 Step 1: Competitor research...")
                from competitor_watcher import find_viral_history_videos, save_results
                videos = find_viral_history_videos()
                save_results(videos, OUT / "competitor_research.json")
                log(f"   Found {len(videos)} viral videos")
            else:
                research_path = OUT / "competitor_research.json"
                if research_path.exists():
                    videos = json.loads(research_path.read_text())
                    log(f"📂 Using cached research ({len(videos)} videos)")
                else:
                    log("🔍 No cache — running research...")
                    from competitor_watcher import find_viral_history_videos, save_results
                    videos = find_viral_history_videos()
                    save_results(videos, OUT / "competitor_research.json")

            run["step"] = "idea"
            run["progress"] = 10

            # Step 2: Idea
            log("🧠 Step 2: Generating unique idea...")
            from idea_engine import generate_idea
            if topic_override:
                idea = {
                    "topic": topic_override,
                    "title": topic_override.title(),
                    "concept": f"Exploring {topic_override}",
                    "angle": "User-specified topic",
                    "inspired_by": [],
                }
            else:
                idea = generate_idea(videos, niche="history")

            if not idea:
                run["status"] = "error"
                run["error"] = "Idea generation failed"
                log("❌ Idea generation failed!")
                return

            log(f"   Idea: {idea.get('title', '?')}")
            run["idea"] = idea
            run["progress"] = 20

            # Step 3: Generate script
            run["step"] = "script"
            log("📝 Step 3: Generating script via Agnes AI...")

            import urllib.request
            payload = json.dumps({
                "topic": idea["topic"],
                "duration": 600,  # 10 min long-form (YouTube pushes long videos)
                "aspect": "16:9",
                "theme": "newsprint-editorial",
                "arc": "hook_payoff",
                "language": "en",
                "voice": "leo",
                "prompt": idea.get("concept", ""),
            }).encode("utf-8")

            req = urllib.request.Request(
                f"http://localhost:9200/api/generate-script",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                script_data = json.loads(resp.read().decode("utf-8"))

            if "error" in script_data:
                run["status"] = "error"
                run["error"] = script_data["error"]
                log(f"❌ Script failed: {script_data['error']}")
                return

            beats = script_data["beats"]
            log(f"   Script: {len(beats.get('beats', []))} beats")
            run["progress"] = 30

            # Step 4: Create video
            run["step"] = "pipeline"
            log("🎬 Step 4: Starting video pipeline...")

            payload2 = json.dumps({"beats": beats}).encode("utf-8")
            req2 = urllib.request.Request(
                f"http://localhost:9200/api/create",
                data=payload2,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                job_data = json.loads(resp2.read().decode("utf-8"))

            job_id = job_data.get("job_id")
            if not job_id:
                run["status"] = "error"
                run["error"] = "Failed to create video job"
                log("❌ Failed to create video job")
                return

            run["vox_job_id"] = job_id
            log(f"   Pipeline job: {job_id}")

            # Step 5: Wait for video
            run["step"] = "video"
            start = time.time()
            last_status = ""
            while time.time() - start < 7200:  # 2 hour timeout (long 10-min videos)
                req3 = urllib.request.Request(f"http://localhost:9200/api/jobs")
                with urllib.request.urlopen(req3, timeout=10) as resp3:
                    jobs_data = json.loads(resp3.read().decode("utf-8"))

                job = next((j for j in jobs_data if j["id"] == job_id), None)
                if not job:
                    run["status"] = "error"
                    run["error"] = "Job not found"
                    log("❌ Job not found")
                    return

                status = job.get("status", "?")
                progress = job.get("progress", 0)
                # Map to overall progress (30% → 90%)
                run["progress"] = 30 + int(progress * 0.6)

                if status != last_status:
                    log(f"   {status} ({progress}%) — {job.get('step_label', '')}")
                    last_status = status

                if status == "done":
                    log(f"✅ Video complete!")
                    run["result_video"] = job.get("result_video")
                    break
                elif status in ("failed", "cancelled"):
                    run["status"] = "error"
                    run["error"] = job.get("error", status)
                    log(f"❌ Pipeline {status}: {job.get('error', '')}")
                    return

                time.sleep(10)

            else:
                run["status"] = "error"
                run["error"] = "Pipeline timed out"
                log("⏰ Pipeline timed out")
                return

            run["progress"] = 90

            # Step 6: Upload to YouTube
            run["step"] = "upload"
            log("📤 Step 6: Uploading to YouTube...")

            yt_title = beats.get("yt_title", idea.get("title", "Untitled"))
            yt_desc = beats.get("yt_description", idea.get("concept", ""))
            yt_tags = beats.get("yt_tags", ["history", "documentary", "short"])

            upload_payload = json.dumps({
                "title": yt_title,
                "description": yt_desc,
                "tags": yt_tags,
            }).encode("utf-8")
            req4 = urllib.request.Request(
                f"http://localhost:9200/api/upload-yt/{job_id}",
                data=upload_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req4, timeout=30) as resp4:
                upload_data = json.loads(resp4.read().decode("utf-8"))
            log(f"   Upload started: {upload_data}")

            # Wait for upload
            upload_start = time.time()
            while time.time() - upload_start < 300:
                req5 = urllib.request.Request(f"http://localhost:9200/api/upload-status/{job_id}")
                with urllib.request.urlopen(req5, timeout=10) as resp5:
                    up_status = json.loads(resp5.read().decode("utf-8"))

                if up_status.get("status") == "done":
                    video_url = up_status.get("video_url", "")
                    log(f"✅ Uploaded to YouTube! {video_url}")
                    run["video_url"] = video_url
                    run["status"] = "done"
                    run["progress"] = 100
                    # Save to autonomous log
                    entry = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "topic": idea.get("topic"),
                        "title": idea.get("title"),
                        "yt_title": yt_title,
                        "job_id": job_id,
                        "video_url": video_url,
                        "inspired_by": idea.get("inspired_by", []),
                        "status": "uploaded",
                    }
                    with open(AUTO_LOG, "a") as f:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    return
                elif up_status.get("status") == "error":
                    run["status"] = "error"
                    run["error"] = up_status.get("error", "upload failed")
                    log(f"❌ Upload failed: {up_status.get('error', '')}")
                    return

                time.sleep(5)
            else:
                run["status"] = "error"
                run["error"] = "Upload timed out"
                log("⏰ Upload timed out")

        except Exception as e:
            run["status"] = "error"
            run["error"] = str(e)
            log(f"❌ Error: {e}")

    global autonomous_counter
    with auto_lock:
        run_id = f"auto_{autonomous_counter}"
        autonomous_counter += 1
        autonomous_runs[run_id] = {
            "id": run_id,
            "type": "full",
            "status": "running",
            "step": "starting",
            "progress": 0,
            "log": [],
            "result": None,
            "error": None,
            "idea": None,
            "vox_job_id": None,
            "result_video": None,
            "video_url": None,
            "created_at": time.time(),
        }

    thread = threading.Thread(target=run_full_pipeline, args=(run_id, topic_override, skip_research), daemon=True)
    thread.start()
    return jsonify({"run_id": run_id, "status": "started"})


@app.route("/api/auto/status/<run_id>")
def api_auto_status(run_id):
    """Get status of an autonomous run."""
    run = autonomous_runs.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(run)


@app.route("/api/auto/runs")
def api_auto_runs():
    """List all autonomous runs."""
    with auto_lock:
        return jsonify(list(autonomous_runs.values()))


@app.route("/api/auto/history")
def api_auto_history():
    """Get autonomous run history from log file."""
    if not AUTO_LOG.exists():
        return jsonify([])
    entries = []
    for line in AUTO_LOG.read_text().strip().split("\n"):
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return jsonify(entries)


@app.route("/api/auto/competitors")
def api_auto_competitors():
    """Get cached competitor research data."""
    research_path = OUT / "competitor_research.json"
    if not research_path.exists():
        return jsonify({"error": "No research data. Run research first."}), 404
    videos = json.loads(research_path.read_text())
    return jsonify({
        "total": len(videos),
        "videos": videos[:20],  # top 20
    })


@app.route("/api/auto/schedule", methods=["GET", "POST"])
def api_auto_schedule():
    """Get or update the autonomous schedule (cron.json)."""
    cron_path = Path("/opt/baal-agent/workspace/cron.json")
    if request.method == "GET":
        if not cron_path.exists():
            return jsonify({"error": "No schedule configured"}), 404
        return jsonify(json.loads(cron_path.read_text()))
    else:
        data = request.json or {}
        enabled = data.get("enabled")
        schedule = data.get("schedule")

        cron_data = json.loads(cron_path.read_text()) if cron_path.exists() else []
        for job in cron_data:
            if job.get("id") == "vox-weekly-batch":
                if enabled is not None:
                    job["enabled"] = enabled
                if schedule:
                    job["schedule"] = schedule
        with open(cron_path, "w") as f:
            json.dump(cron_data, f, indent=2)
        return jsonify({"status": "updated", "cron": cron_data})


@app.route("/api/auto/weekly-batch", methods=["POST"])
def api_auto_weekly_batch():
    """Run the weekly batch: generate 4 long-form videos autonomously."""
    global autonomous_counter
    data = request.json or {}
    count = data.get("count", 4)
    skip_research = data.get("skip_research", False)

    with auto_lock:
        run_id = f"batch_{autonomous_counter}"
        autonomous_counter += 1
        autonomous_runs[run_id] = {
            "id": run_id,
            "type": "weekly_batch",
            "status": "running",
            "step": "starting",
            "progress": 0,
            "log": [],
            "result": None,
            "error": None,
            "videos": [],
            "count": count,
            "created_at": time.time(),
        }

    def run_batch(run_id, count, skip_research):
        run = autonomous_runs[run_id]

        def log(msg):
            ts = datetime.now().strftime('%H:%M:%S')
            run["log"].append(f"[{ts}] {msg}")
            if len(run["log"]) > 300:
                run["log"] = run["log"][-200:]

        try:
            sys.path.insert(0, str(SCRIPTS))
            from weekly_batch import run_weekly_batch as _run_batch

            # Monkey-patch the log function to capture output
            import weekly_batch as wb_module
            _orig_log = wb_module.log
            def _capture_log(msg, level="INFO"):
                _orig_log(msg, level)
                log(f"{level}: {msg}")

            wb_module.log = _capture_log

            log(f"🤖 Weekly batch started: {count} videos")

            # Run the batch
            _run_batch(count=count, dry_run=False, skip_research=skip_research)

            run["status"] = "done"
            run["progress"] = 100
            log("✅ Weekly batch complete!")

        except Exception as e:
            run["status"] = "error"
            run["error"] = str(e)
            log(f"❌ Error: {e}")

    thread = threading.Thread(target=run_batch, args=(run_id, count, skip_research), daemon=True)
    thread.start()
    return jsonify({"run_id": run_id, "status": "started", "count": count})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9200, debug=False)
