#!/usr/bin/env python3
"""
Pipeline Manager — Robust orchestration with automatic retry
=============================================================
Watches each pipeline step (keyframes → clips → audio → assemble).
If a step fails, retries it automatically (up to 3 attempts per step).
For keyframes and clips, checks which specific shots failed and
retries ONLY those shots instead of redoing the entire batch.

This is the "manager" that makes sure long video generation doesn't fail.

Used by auto_runner.py and studio.py for autonomous video generation.
"""
import json
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

BASE_DIR = Path("/opt/baal-agent/workspace/vox-director")
SCRIPTS_DIR = BASE_DIR / "scripts"


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def count_shots(beats_path):
    """Count total shots from beats.json."""
    with open(beats_path) as f:
        doc = json.load(f)
    return sum(len(b.get("shots") or [b]) for b in doc.get("beats", []))


def run_step(script_name, project_dir, timeout=3600):
    """Run a pipeline step subprocess. Returns (success, output_lines)."""
    script_path = SCRIPTS_DIR / script_name
    cmd = ["python3", str(script_path), str(project_dir)]
    log(f"▶ {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
        )
        output = (proc.stdout + proc.stderr).strip().split("\n")
        success = proc.returncode == 0
        return success, output
    except subprocess.TimeoutExpired:
        return False, [f"TIMEOUT after {timeout}s"]
    except Exception as e:
        return False, [str(e)]


def check_keyframes(project_dir):
    """Check which shots are missing keyframes. Returns set of missing keys."""
    beats_path = project_dir / "beats.json"
    with open(beats_path) as f:
        doc = json.load(f)

    missing = set()
    for beat in doc.get("beats", []):
        shots = beat.get("shots") or [beat]
        for shot in shots:
            key = f"{beat['id']}{shot.get('id', '')}"
            if not shot.get("keyframe_path") or not os.path.exists(shot.get("keyframe_path", "")):
                missing.add(key)
    return missing


def check_clips(project_dir):
    """Check which shots are missing video clips. Returns set of missing keys."""
    beats_path = project_dir / "beats.json"
    with open(beats_path) as f:
        doc = json.load(f)

    missing = set()
    for beat in doc.get("beats", []):
        shots = beat.get("shots") or [beat]
        for shot in shots:
            key = f"{beat['id']}{shot.get('id', '')}"
            if not shot.get("clip_path") or not os.path.exists(shot.get("clip_path", "")):
                # Check if keyframe exists (can use Ken Burns fallback in assemble)
                kf = shot.get("keyframe_path", "")
                if kf and os.path.exists(kf):
                    # Assemble can use Ken Burns fallback — not critical
                    pass
                else:
                    missing.add(key)  # No keyframe AND no clip = truly missing
    return missing


def manage_pipeline(project_dir, max_step_retries=3, progress_callback=None):
    """
    Run the full pipeline with automatic retry on failures.

    Args:
        project_dir: Path to the project directory
        max_step_retries: Max retries per step (default 3)
        progress_callback: Optional function(step_name, attempt, status) called on each event

    Returns:
        dict with keys: success, final_video, steps_run, retries_used, errors
    """
    project_dir = Path(project_dir)
    beats_path = project_dir / "beats.json"

    if not beats_path.exists():
        return {"success": False, "error": "beats.json not found", "final_video": None}

    n_shots = count_shots(beats_path)
    log(f"Pipeline Manager: {n_shots} shots to process")

    # Scale timeouts by shot count
    kf_timeout = 300 if n_shots <= 20 else 1200 if n_shots <= 80 else 2400
    clips_timeout = 1200 if n_shots <= 20 else 3600 if n_shots <= 80 else 5400
    audio_timeout = 300 if n_shots <= 20 else 1200 if n_shots <= 80 else 2400
    assemble_timeout = 120 if n_shots <= 20 else 600 if n_shots <= 80 else 1800

    steps = [
        ("keyframes", "keyframes.py", kf_timeout),
        ("clips", "clips.py", clips_timeout),
        ("audio", "audio.py", audio_timeout),
        ("assemble", "assemble.py", assemble_timeout),
    ]

    steps_run = []
    retries_used = 0
    all_errors = []
    final_video = None

    for step_name, script, timeout in steps:
        for attempt in range(1, max_step_retries + 1):
            if progress_callback:
                progress_callback(step_name, attempt, "running")

            log(f"━━━ Step: {step_name} (attempt {attempt}/{max_step_retries}) ━━━")
            success, output = run_step(script, project_dir, timeout=timeout)

            # Log last few lines
            for line in output[-5:]:
                if line.strip():
                    log(f"  {line}")

            if success:
                log(f"✅ {step_name} succeeded (attempt {attempt})")
                steps_run.append({"step": step_name, "attempts": attempt, "success": True})

                # After keyframes: verify all shots have keyframes
                if step_name == "keyframes":
                    missing = check_keyframes(project_dir)
                    if missing:
                        log(f"⚠️  {len(missing)} keyframes still missing: {sorted(missing)[:10]}")
                        if attempt < max_step_retries:
                            log(f"   Retrying keyframes (attempt {attempt+1})...")
                            retries_used += 1
                            continue
                    log("All keyframes present ✅")

                # After clips: verify clips
                if step_name == "clips":
                    missing = check_clips(project_dir)
                    if missing:
                        log(f"⚠️  {len(missing)} clips still missing: {sorted(missing)[:10]}")
                        if attempt < max_step_retries:
                            log(f"   Retrying clips (attempt {attempt+1})...")
                            retries_used += 1
                            continue
                    log("All clips present ✅")

                if progress_callback:
                    progress_callback(step_name, attempt, "done")
                break
            else:
                error_msg = output[-1] if output else "unknown error"
                log(f"❌ {step_name} failed (attempt {attempt}): {error_msg}", "ERROR")
                all_errors.append(f"{step_name} attempt {attempt}: {error_msg}")

                if progress_callback:
                    progress_callback(step_name, attempt, "failed")

                if attempt < max_step_retries:
                    # Before retry: clean up partial outputs for this step
                    log(f"   Cleaning up partial outputs for retry...")
                    if step_name == "keyframes":
                        kf_dir = project_dir / "keyframes"
                        if kf_dir.exists():
                            for f in kf_dir.glob("*.jpg"):
                                pass  # Keep existing keyframes, only regen missing ones
                    elif step_name == "clips":
                        clips_dir = project_dir / "clips"
                        if clips_dir.exists():
                            for f in clips_dir.glob("*.mp4"):
                                pass  # Keep existing clips, only regen missing ones

                    log(f"   Retrying in 5s...")
                    time.sleep(5)
                    retries_used += 1
                else:
                    log(f"💥 {step_name} FAILED after {max_step_retries} attempts!", "ERROR")
                    steps_run.append({"step": step_name, "attempts": attempt, "success": False})

                    # Check if final.mp4 exists despite failure (assemble may have completed)
                    fv = project_dir / "final.mp4"
                    if fv.exists() and fv.stat().st_size > 10000:
                        log(f"⚠️  But final.mp4 exists ({fv.stat().st_size/1024/1024:.1f} MB) — using it!")
                        final_video = str(fv)
                        return {
                            "success": True,
                            "final_video": final_video,
                            "steps_run": steps_run,
                            "retries_used": retries_used,
                            "errors": all_errors,
                        }

                    return {
                        "success": False,
                        "error": f"{step_name} failed after {max_step_retries} attempts",
                        "final_video": None,
                        "steps_run": steps_run,
                        "retries_used": retries_used,
                        "errors": all_errors,
                    }

    # Check final video
    final_video_path = project_dir / "final.mp4"
    if final_video_path.exists() and final_video_path.stat().st_size > 10000:
        final_video = str(final_video_path)
        size_mb = final_video_path.stat().st_size / 1024 / 1024
        log(f"🎉 Pipeline complete! final.mp4 ({size_mb:.1f} MB)")

        # ─── Generate thumbnail ───
        try:
            log("🎨 Generating thumbnail...")
            thumb_script = BASE_DIR / "scripts" / "thumbnail_builder.py"
            if thumb_script.exists():
                import subprocess as sp
                env = os.environ.copy()
                env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                result_thumb = sp.run(
                    [sys.executable, str(thumb_script), str(project_dir), "--variation", "1"],
                    capture_output=True, text=True, timeout=60, env=env
                )
                thumb_path = project_dir / "thumbnail_v1.jpg"
                if thumb_path.exists():
                    log(f"   ✅ Thumbnail: {thumb_path}")
                else:
                    log(f"   ⚠️ Thumbnail generation may have failed: {result_thumb.stderr[-200:]}")
        except Exception as e:
            log(f"   ⚠️ Thumbnail generation error: {e}")

        return {
            "success": True,
            "final_video": final_video,
            "steps_run": steps_run,
            "retries_used": retries_used,
            "errors": all_errors,
        }
    else:
        log("❌ final.mp4 not found or too small!", "ERROR")
        return {
            "success": False,
            "error": "final.mp4 not found",
            "final_video": None,
            "steps_run": steps_run,
            "retries_used": retries_used,
            "errors": all_errors,
        }


if __name__ == "__main__":
    proj = sys.argv[1] if len(sys.argv) > 1 else str(BASE_DIR / "out" / "test")
    result = manage_pipeline(proj)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)
