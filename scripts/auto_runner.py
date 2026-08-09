#!/usr/bin/env python3
"""
Vox Tube Autonomous Runner — End-to-end autonomous video generation
====================================================================
This script orchestrates the full autonomous pipeline:

  1. Competitor Watcher  → find viral history videos on YouTube
  2. Idea Engine          → combine competitor ideas into unique new topic
  3. Script Generation    → call Vox Studio API to generate beats.json
  4. Video Pipeline       → call Vox Studio API to create the video
  5. YouTube Upload       → upload via Composio MCP

Usage:
  python3 auto_runner.py                    # Full autonomous run
  python3 auto_runner.py --dry-run          # Generate idea only, don't make video
  python3 auto_runner.py --topic "..."     # Skip idea generation, use given topic
  python3 auto_runner.py --skip-research   # Use cached competitor research
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

# Import our modules
sys.path.insert(0, str(BASE_DIR / "scripts"))
from competitor_watcher import find_viral_history_videos, save_results
from idea_engine import generate_idea


def log(msg, level="INFO"):
    """Print a timestamped log message."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def step1_research(skip_cache=False):
    """Step 1: Find viral history videos from competitors."""
    log("=" * 60)
    log("STEP 1: Competitor Research")
    log("=" * 60)

    research_path = OUT_DIR / "competitor_research.json"
    if not skip_cache and research_path.exists():
        age = time.time() - research_path.stat().st_mtime
        if age < 3600 * 6:  # cache for 6 hours
            log(f"Using cached research ({age/3600:.1f}h old)")
            videos = json.loads(research_path.read_text())
            log(f"Loaded {len(videos)} viral videos from cache")
            return videos

    videos = find_viral_history_videos()
    save_results(videos, research_path)
    return videos


def step2_idea(competitor_videos, topic_override=None):
    """Step 2: Generate a unique video idea from competitor research."""
    log("=" * 60)
    log("STEP 2: Idea Generation")
    log("=" * 60)

    if topic_override:
        log(f"Using provided topic: {topic_override}")
        return {
            "topic": topic_override,
            "title": topic_override.title(),
            "concept": f"Exploring {topic_override}",
            "angle": "User-specified topic",
            "inspired_by": [],
        }

    idea = generate_idea(competitor_videos, niche="history")
    if not idea:
        log("Idea generation failed!", "ERROR")
        sys.exit(1)
    return idea


def step3_generate_script(idea):
    """Step 3: Call Vox Studio API to generate beats.json."""
    log("=" * 60)
    log("STEP 3: Script Generation")
    log("=" * 60)

    topic = idea["topic"]
    log(f"Generating script for: {topic}")

    import urllib.request
    import urllib.error

    payload = json.dumps({
        "topic": topic,
        "duration": 600,  # 10 minutes — YouTube pushes long-form
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

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "error" in data:
            log(f"Script generation failed: {data['error']}", "ERROR")
            return None
        beats = data["beats"]
        log(f"Script generated! {len(beats.get('beats', []))} beats")
        log(f"  Title: {beats.get('yt_title', '?')[:60]}")
        return beats
    except Exception as e:
        log(f"Script generation error: {e}", "ERROR")
        return None


def step4_create_video(beats):
    """Step 4: Call Vox Studio API to create the video (pipeline)."""
    log("=" * 60)
    log("STEP 4: Video Generation Pipeline")
    log("=" * 60)

    import urllib.request

    payload = json.dumps({"beats": beats}).encode("utf-8")
    req = urllib.request.Request(
        f"{STUDIO_URL}/api/create",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        job_id = data.get("job_id")
        if not job_id:
            log(f"Failed to create job: {data}", "ERROR")
            return None
        log(f"Pipeline started! Job ID: {job_id}")
        return job_id
    except Exception as e:
        log(f"Create job error: {e}", "ERROR")
        return None


def step5_wait_for_video(job_id, timeout=1200):
    """Wait for the video pipeline to complete."""
    log("=" * 60)
    log("STEP 5: Waiting for Video Pipeline")
    log("=" * 60)

    import urllib.request

    start = time.time()
    last_status = ""

    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"{STUDIO_URL}/api/jobs")
            with urllib.request.urlopen(req, timeout=10) as resp:
                jobs_data = json.loads(resp.read().decode("utf-8"))

            job = next((j for j in jobs_data if j["id"] == job_id), None)
            if not job:
                log(f"Job {job_id} not found!", "ERROR")
                return None

            status = job.get("status", "?")
            progress = job.get("progress", 0)
            step_label = job.get("step_label", "")

            if status != last_status:
                log(f"Status: {status} ({progress}%) — {step_label}")
                last_status = status

            if status == "done":
                log(f"✅ Video complete! ({progress}%)")
                log(f"   File: {job.get('result_video', '?')}")
                return job
            elif status == "failed":
                log(f"❌ Pipeline failed: {job.get('error', '?')}", "ERROR")
                return job
            elif status == "cancelled":
                log(f"⏹️ Pipeline cancelled", "WARN")
                return job

        except Exception as e:
            log(f"Error checking job status: {e}", "WARN")

        time.sleep(10)

    log(f"⏰ Pipeline timed out after {timeout}s", "ERROR")
    return None


def step6_upload_to_youtube(job_id, idea, beats):
    """Step 6: Upload the completed video to YouTube."""
    log("=" * 60)
    log("STEP 6: YouTube Upload")
    log("=" * 60)

    import urllib.request

    # Use AI-generated YouTube metadata if available
    yt_title = beats.get("yt_title", idea.get("title", "Untitled"))
    yt_desc = beats.get("yt_description", idea.get("concept", ""))
    yt_tags = beats.get("yt_tags", ["history", "documentary", "short", "educational"])

    payload = json.dumps({
        "title": yt_title,
        "description": yt_desc,
        "tags": yt_tags,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{STUDIO_URL}/api/upload-yt/{job_id}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        log(f"Upload started: {data}")

        # Wait for upload to complete
        upload_start = time.time()
        while time.time() - upload_start < 300:
            try:
                req2 = urllib.request.Request(f"{STUDIO_URL}/api/upload-status/{job_id}")
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    status = json.loads(resp2.read().decode("utf-8"))

                if status.get("status") == "done":
                    video_url = status.get("video_url", "")
                    log(f"✅ Uploaded to YouTube! URL: {video_url}")
                    return video_url
                elif status.get("status") == "error":
                    log(f"❌ Upload failed: {status.get('error', '?')}", "ERROR")
                    return None

                log(f"Upload progress: {status.get('progress', 0)}% — {status.get('message', '')}")
            except Exception as e:
                log(f"Error checking upload: {e}", "WARN")

            time.sleep(5)

        log("⏰ Upload timed out", "ERROR")
        return None
    except Exception as e:
        log(f"Upload error: {e}", "ERROR")
        return None


def save_autonomous_log(idea, job_id, video_url, status):
    """Save a log of this autonomous run."""
    log_path = OUT_DIR / "autonomous_log.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "topic": idea.get("topic"),
        "title": idea.get("title"),
        "concept": idea.get("concept"),
        "angle": idea.get("angle"),
        "inspired_by": idea.get("inspired_by", []),
        "job_id": job_id,
        "video_url": video_url,
        "status": status,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log(f"Log saved to {log_path}")


def run_autonomous(topic=None, dry_run=False, skip_research=False):
    """Main autonomous pipeline."""
    log("🤖 Vox Tube Autonomous Runner Started")
    log(f"   Time: {datetime.now(timezone.utc).isoformat()}")
    log(f"   Mode: {'dry-run' if dry_run else 'full'}")
    log("")

    # Step 1: Research
    competitor_videos = step1_research(skip_cache=skip_research)
    log("")

    # Step 2: Idea
    idea = step2_idea(competitor_videos, topic)
    log("")

    if dry_run:
        log("🧪 Dry run complete — idea generated, no video creation.")
        save_autonomous_log(idea, None, None, "dry_run")
        return idea

    # Step 3: Generate script
    beats = step3_generate_script(idea)
    if not beats:
        save_autonomous_log(idea, None, None, "script_failed")
        sys.exit(1)
    log("")

    # Step 4: Create video
    job_id = step4_create_video(beats)
    if not job_id:
        save_autonomous_log(idea, None, None, "pipeline_failed")
        sys.exit(1)
    log("")

    # Step 5: Wait for video
    job = step5_wait_for_video(job_id, timeout=7200)  # 2 hours for 10-min video
    if not job or job.get("status") != "done":
        save_autonomous_log(idea, job_id, None, "video_failed")
        log("❌ Video generation failed!", "ERROR")
        sys.exit(1)
    log("")

    # Step 6: Upload to YouTube
    video_url = step6_upload_to_youtube(job_id, idea, beats)
    status = "uploaded" if video_url else "upload_failed"
    log("")

    # Log
    save_autonomous_log(idea, job_id, video_url, status)

    log("=" * 60)
    if video_url:
        log(f"🎉 AUTONOMOUS RUN COMPLETE!")
        log(f"   Topic: {idea.get('topic')}")
        log(f"   Title: {idea.get('title')}")
        log(f"   YouTube: {video_url}")
    else:
        log(f"⚠️ Video created but upload failed")
        log(f"   Topic: {idea.get('topic')}")
        log(f"   Job: {job_id}")
    log("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vox Tube Autonomous Runner")
    parser.add_argument("--dry-run", action="store_true", help="Generate idea only, don't make video")
    parser.add_argument("--topic", default=None, help="Use specific topic instead of generating one")
    parser.add_argument("--skip-research", action="store_true", help="Use cached competitor research")
    args = parser.parse_args()

    run_autonomous(topic=args.topic, dry_run=args.dry_run, skip_research=args.skip_research)
