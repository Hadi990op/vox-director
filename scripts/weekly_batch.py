#!/usr/bin/env python3
"""
Weekly Batch Scheduler — Generate 4 videos per week, scheduled uploads
=====================================================================
Generates 4 unique video ideas from competitor research, creates all 4
videos (using the pipeline manager for robustness), and schedules them
for upload throughout the week (e.g., Mon/Wed/Fri/Sun at USA peak time).

Fully autonomous — no human approval needed.

Usage:
  python3 weekly_batch.py                    # Full weekly batch (4 videos)
  python3 weekly_batch.py --dry-run           # Generate 4 ideas only
  python3 weekly_batch.py --count 2           # Generate 2 videos instead of 4
  python3 weekly_batch.py --skip-research     # Use cached competitor research
"""
import argparse
import json
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path("/opt/baal-agent/workspace/vox-director")
OUT_DIR = BASE_DIR / "out"
STUDIO_URL = "http://localhost:9200"
BATCH_LOG = OUT_DIR / "weekly_batch_log.jsonl"
BATCH_STATE = OUT_DIR / "weekly_batch_state.json"

sys.path.insert(0, str(BASE_DIR / "scripts"))
from competitor_watcher import find_viral_history_videos, save_results
from idea_engine import generate_idea
from pipeline_manager import manage_pipeline


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def load_state():
    """Load batch state (which videos were made, which are pending upload)."""
    if BATCH_STATE.exists():
        return json.loads(BATCH_STATE.read_text())
    return {"videos": [], "last_batch": None}


def save_state(state):
    """Save batch state."""
    BATCH_STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def save_batch_log(entry):
    """Append to the weekly batch log."""
    BATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(BATCH_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_unique_ideas(competitor_videos, count=4):
    """Generate multiple unique video ideas, avoiding duplicates."""
    ideas = []
    used_topics = set()

    # Load previously used topics from batch log
    if BATCH_LOG.exists():
        for line in BATCH_LOG.read_text().strip().split("\n"):
            if line:
                try:
                    entry = json.loads(line)
                    used_topics.add(entry.get("topic", "").lower())
                except json.JSONDecodeError:
                    pass

    for i in range(count):
        log(f"  Generating idea {i+1}/{count}...")
        for attempt in range(3):  # Retry idea generation
            idea = generate_idea(competitor_videos, niche="history")
            if idea and idea.get("topic", "").lower() not in used_topics:
                ideas.append(idea)
                used_topics.add(idea["topic"].lower())
                log(f"    ✅ Idea {i+1}: {idea.get('topic', '?')}")
                break
            elif idea:
                log(f"    ⚠️  Duplicate topic, regenerating...")
            time.sleep(2)
        else:
            log(f"    ❌ Could not generate unique idea {i+1} after 3 attempts", "ERROR")

    return ideas


def generate_script_for_idea(idea):
    """Call Vox Studio API to generate beats.json for an idea."""
    import urllib.request

    topic = idea["topic"]
    log(f"  Generating 10-min script for: {topic}")

    payload = json.dumps({
        "topic": topic,
        "duration": 600,  # 10 minutes
        "aspect": "16:9",
        "theme": "newsprint-editorial",
        "arc": "hook_payoff",
        "language": "en",
        "voice": "leo",
        "prompt": idea.get("concept", ""),
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{STUDIO_URL}/api/generate-script",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                log(f"    ❌ Script generation failed: {data['error']}", "ERROR")
                if attempt < 2:
                    log(f"    Retrying in 10s...")
                    time.sleep(10)
                    continue
                return None
            beats = data["beats"]
            n_beats = len(beats.get("beats", []))
            log(f"    ✅ Script: {n_beats} beats, title: {beats.get('yt_title', '?')[:50]}")
            return beats
        except Exception as e:
            log(f"    ❌ Script error (attempt {attempt+1}): {e}", "ERROR")
            if attempt < 2:
                time.sleep(10)

    return None


def create_video_with_manager(beats, project_name):
    """Create video using the pipeline manager (with automatic retry)."""
    project_dir = OUT_DIR / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Save beats.json
    beats_path = project_dir / "beats.json"
    beats_path.write_text(json.dumps(beats, ensure_ascii=False, indent=2))

    # Progress callback
    def on_progress(step, attempt, status):
        log(f"    [{step}] attempt {attempt}: {status}")

    log(f"  🎬 Running pipeline manager for {project_name}...")
    result = manage_pipeline(project_dir, max_step_retries=3, progress_callback=on_progress)

    if result["success"]:
        log(f"  ✅ Video complete! ({result.get('retries_used', 0)} retries used)")
        log(f"     {result['final_video']}")
    else:
        log(f"  ❌ Pipeline failed: {result.get('error', '?')}", "ERROR")
        if result.get("errors"):
            for e in result["errors"][-3:]:
                log(f"     {e}")

    return result


def upload_to_youtube(project_dir_name, idea, beats):
    """Upload video to YouTube via Vox Studio API."""
    import urllib.request

    yt_title = beats.get("yt_title", idea.get("title", "Untitled"))
    yt_desc = beats.get("yt_description", idea.get("concept", ""))
    yt_tags = beats.get("yt_tags", ["history", "documentary", "educational"])

    payload = json.dumps({
        "title": yt_title,
        "description": yt_desc,
        "tags": yt_tags,
    }).encode("utf-8")

    # Find the job_id by listing jobs and matching by project dir
    try:
        req = urllib.request.Request(f"{STUDIO_URL}/api/jobs")
        with urllib.request.urlopen(req, timeout=10) as resp:
            jobs_data = json.loads(resp.read().decode("utf-8"))

        # Find the job for this project
        job = next((j for j in jobs_data if project_dir_name in j.get("project_dir", "")), None)
        if not job:
            log(f"  ⚠️  Could not find job for {project_dir_name}, skipping upload", "WARN")
            return None
        job_id = job["id"]
    except Exception as e:
        log(f"  ⚠️  Could not find job for upload: {e}", "WARN")
        return None

    req = urllib.request.Request(
        f"{STUDIO_URL}/api/upload-yt/{job_id}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        log(f"  📤 Upload started: {data}")

        # Wait for upload
        upload_start = time.time()
        while time.time() - upload_start < 600:
            try:
                req2 = urllib.request.Request(f"{STUDIO_URL}/api/upload-status/{job_id}")
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    status = json.loads(resp2.read().decode("utf-8"))

                if status.get("status") == "done":
                    log(f"  ✅ Uploaded to YouTube! {status.get('video_url', '')}")
                    return status.get("video_url")
                elif status.get("status") == "error":
                    log(f"  ❌ Upload failed: {status.get('error', '?')}", "ERROR")
                    return None

                log(f"  📤 Upload: {status.get('progress', 0)}% — {status.get('message', '')}")
            except Exception as e:
                log(f"  ⚠️  Upload poll error: {e}", "WARN")

            time.sleep(10)

        log("  ⏰ Upload timed out", "ERROR")
        return None
    except Exception as e:
        log(f"  ❌ Upload error: {e}", "ERROR")
        return None


def run_weekly_batch(count=4, dry_run=False, skip_research=False):
    """Main weekly batch pipeline."""
    log("=" * 70)
    log(f"🤖 WEEKLY BATCH STARTED — {count} videos")
    log(f"   Time: {datetime.now(timezone.utc).isoformat()}")
    log(f"   Mode: {'dry-run' if dry_run else 'full production'}")
    log("=" * 70)

    # Step 1: Research
    log("")
    log("━━━ STEP 1: Competitor Research ━━━")
    research_path = OUT_DIR / "competitor_research.json"
    if not skip_research and research_path.exists():
        age = time.time() - research_path.stat().st_mtime
        if age < 3600 * 6:
            log(f"Using cached research ({age/3600:.1f}h old)")
            competitor_videos = json.loads(research_path.read_text())
        else:
            competitor_videos = find_viral_history_videos()
            save_results(competitor_videos, research_path)
    elif skip_research and research_path.exists():
        competitor_videos = json.loads(research_path.read_text())
        log(f"Using cached research (--skip-research)")
    else:
        competitor_videos = find_viral_history_videos()
        save_results(competitor_videos, research_path)

    log(f"Found {len(competitor_videos)} viral history videos")

    # Step 2: Generate 4 unique ideas
    log("")
    log("━━━ STEP 2: Generate Unique Ideas ━━━")
    ideas = get_unique_ideas(competitor_videos, count=count)

    if len(ideas) < count:
        log(f"⚠️  Only generated {len(ideas)}/{count} unique ideas", "WARN")

    if not ideas:
        log("❌ No ideas generated!", "ERROR")
        return

    for i, idea in enumerate(ideas):
        log(f"  Idea {i+1}: {idea.get('topic', '?')}")
        log(f"    Title: {idea.get('title', '?')}")
        log(f"    Angle: {idea.get('angle', '?')}")

    if dry_run:
        log("")
        log("🧪 Dry run complete — 4 ideas generated, no video creation.")
        for idea in ideas:
            save_batch_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "topic": idea.get("topic"),
                "title": idea.get("title"),
                "status": "dry_run",
            })
        return ideas

    # Step 3: Generate videos for each idea
    log("")
    log("━━━ STEP 3: Generate Videos ━━━")

    state = load_state()
    batch_id = datetime.now().strftime("%Y-W%W")
    batch_start = time.time()

    for i, idea in enumerate(ideas):
        log("")
        log(f"━━━ Video {i+1}/{len(ideas)}: {idea.get('topic', '?')} ━━━")

        # Generate script
        beats = generate_script_for_idea(idea)
        if not beats:
            save_batch_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "batch_id": batch_id,
                "topic": idea.get("topic"),
                "title": idea.get("title"),
                "status": "script_failed",
                "inspired_by": idea.get("inspired_by", []),
            })
            log(f"  ❌ Script failed, skipping to next idea", "ERROR")
            continue

        # Create video with pipeline manager (automatic retry)
        project_name = idea.get("topic", f"video-{i}").lower().replace(" ", "-")[:50]
        result = create_video_with_manager(beats, project_name)

        video_status = "video_complete" if result["success"] else "video_failed"

        if result["success"]:
            # Try YouTube upload
            log(f"  📤 Uploading to YouTube...")
            video_url = upload_to_youtube(project_name, idea, beats)
            if video_url:
                video_status = "uploaded"
            else:
                video_status = "upload_failed"

        # Log this video
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "batch_id": batch_id,
            "topic": idea.get("topic"),
            "title": idea.get("title"),
            "concept": idea.get("concept"),
            "angle": idea.get("angle"),
            "inspired_by": idea.get("inspired_by", []),
            "status": video_status,
            "video_url": video_url if result["success"] else None,
            "video_path": result.get("final_video"),
            "retries_used": result.get("retries_used", 0),
            "project": project_name,
        }
        save_batch_log(entry)
        state["videos"].append(entry)

        log(f"  Status: {video_status}")

    # Save state
    state["last_batch"] = batch_id
    state["last_batch_time"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # Summary
    elapsed = (time.time() - batch_start) / 60
    log("")
    log("=" * 70)
    log(f"📊 WEEKLY BATCH SUMMARY")
    log(f"   Batch: {batch_id}")
    log(f"   Time: {elapsed:.1f} minutes")
    log(f"   Ideas: {len(ideas)}")
    succeeded = sum(1 for v in state["videos"] if v.get("status") in ("uploaded", "video_complete"))
    log(f"   Videos: {succeeded}/{len(ideas)} succeeded")
    log(f"   Retries used: {sum(v.get('retries_used', 0) for v in state['videos'][-len(ideas):])}")
    log("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vox Tube Weekly Batch Scheduler")
    parser.add_argument("--dry-run", action="store_true", help="Generate ideas only, don't make videos")
    parser.add_argument("--count", type=int, default=4, help="Number of videos to generate (default: 4)")
    parser.add_argument("--skip-research", action="store_true", help="Use cached competitor research")
    args = parser.parse_args()

    run_weekly_batch(count=args.count, dry_run=args.dry_run, skip_research=args.skip_research)
