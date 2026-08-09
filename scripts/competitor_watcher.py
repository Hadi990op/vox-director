#!/usr/bin/env python3
"""
Competitor Watcher — Scrape YouTube for viral history videos from faceless channels
======================================================================================
Searches YouTube for history documentary shorts, extracts viral video titles/topics,
and returns structured data for the Idea Engine to combine.

Uses yt-dlp (no API key needed) for search + metadata extraction.

Output: JSON list of viral videos with title, views, duration, url, channel.
"""
import json
import subprocess
import sys
import time
import re
from pathlib import Path

# History niche search queries — different angles for diverse ideas
SEARCH_QUERIES = [
    "history documentary short animated",
    "history explained short",
    "food history documentary short",
    "ancient empire history animated short",
    "weird history facts short",
    "history mystery explained short",
    "war history short documentary",
    "lost history discovered short",
    "history of inventions short",
    "drink history documentary short",
]

# Minimum view count to be considered "viral" (scales with niche)
MIN_VIEWS = 100_000

# Maximum duration (seconds) — we make short videos (30-120s)
# but competitor research can look at longer ones for ideas
MAX_DURATION = 600  # 10 minutes

# How many results per search query
RESULTS_PER_QUERY = 10


def search_youtube(query, limit=RESULTS_PER_QUERY):
    """Search YouTube using yt-dlp and return list of video metadata."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print", "%(id)s|||%(title)s|||%(view_count)s|||%(duration)s|||%(channel)s|||%(channel_id)s",
        f"ytsearch{limit}:{query}",
        "--no-warnings",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        videos = []
        for line in result.stdout.strip().split("\n"):
            if not line or "|||" not in line:
                continue
            parts = line.split("|||")
            if len(parts) < 6:
                continue
            vid_id, title, views_str, dur_str, channel, channel_id = parts[:6]
            try:
                views = int(views_str) if views_str and views_str != "NA" else 0
                duration = int(dur_str) if dur_str and dur_str != "NA" else 0
            except (ValueError, TypeError):
                views, duration = 0, 0

            videos.append({
                "id": vid_id,
                "title": title.strip(),
                "views": views,
                "duration": duration,
                "channel": channel.strip() if channel else "",
                "channel_id": channel_id.strip() if channel_id else "",
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "search_query": query,
            })
        return videos
    except subprocess.TimeoutExpired:
        print(f"  ⏰ Timeout searching: {query}")
        return []
    except Exception as e:
        print(f"  ❌ Error searching '{query}': {e}")
        return []


def is_faceless_channel(video):
    """Heuristic: detect if this is likely a faceless channel.
    
    Faceless channels typically:
    - Have 'animated', 'explained', 'documentary', 'history' in title/channel
    - Don't have person's name in channel name
    - Have animated/educational keywords
    """
    text = (video.get("channel", "") + " " + video.get("title", "")).lower()
    faceless_keywords = ["animated", "explained", "documentary", "history", "facts",
                        "brief", "short", "mini", "story", "channel", "studio"]
    faceless_score = sum(1 for kw in faceless_keywords if kw in text)
    # If channel name looks like a person's name (2 words, no keywords), probably not faceless
    channel = video.get("channel", "").lower()
    person_indicators = ["vlog", "diaries", "personal", "my"]
    person_score = sum(1 for kw in person_indicators if kw in channel)
    return faceless_score >= 1 and person_score == 0


def extract_topic_from_title(title):
    """Extract the core topic from a video title.
    
    Examples:
      "The History of Pizza | Short Documentary" → "history of pizza"
      "How Rome Fell — Animated History" → "how rome fell"
    """
    # Remove channel name suffix (after | or — or -)
    topic = re.split(r'\s*[|—–-]\s*', title)[0].strip()
    # Remove emojis
    topic = re.sub(r'[^\w\s]', '', topic).strip()
    # Remove trailing words like "documentary", "short", "animated"
    stop_words = ["documentary", "short", "animated", "explained",
                  "history channel", "full episode", "part 1", "part 2"]
    for sw in stop_words:
        topic = re.sub(r'\b' + sw + r'\b', '', topic, flags=re.IGNORECASE).strip()
    topic = re.sub(r'\s+', ' ', topic).strip()
    return topic.lower()


def find_viral_history_videos(queries=None, min_views=MIN_VIEWS, max_duration=MAX_DURATION):
    """Main function: search YouTube, filter for viral faceless history videos.
    
    Returns list of dicts sorted by views (descending).
    """
    queries = queries or SEARCH_QUERIES
    all_videos = []
    seen_ids = set()

    print(f"🔍 Searching YouTube for viral history videos...")
    print(f"   {len(queries)} queries × {RESULTS_PER_QUERY} results each")
    print()

    for i, query in enumerate(queries, 1):
        print(f"  [{i}/{len(queries)}] Searching: '{query}'")
        videos = search_youtube(query)
        for v in videos:
            if v["id"] in seen_ids:
                continue
            seen_ids.add(v["id"])
            if v["views"] >= min_views and v["duration"] <= max_duration:
                v["is_faceless"] = is_faceless_channel(v)
                v["topic_extracted"] = extract_topic_from_title(v["title"])
                all_videos.append(v)
        time.sleep(0.5)  # be nice to YouTube

    # Sort by views descending
    all_videos.sort(key=lambda x: x["views"], reverse=True)

    print()
    print(f"📊 Found {len(all_videos)} viral history videos (≥{min_views:,} views)")
    print(f"   Top 10:")
    for v in all_videos[:10]:
        faceless = "👁️" if v.get("is_faceless") else "👤"
        print(f"   {faceless} {v['views']:>10,} views | {v['title'][:60]}")

    return all_videos


def get_top_videos(n=10, queries=None):
    """Get top N viral history videos. Convenience function."""
    videos = find_viral_history_videos(queries)
    return videos[:n]


def save_results(videos, path):
    """Save results to JSON file."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(videos, indent=2, ensure_ascii=False))
    print(f"💾 Saved {len(videos)} videos to {out}")


if __name__ == "__main__":
    videos = find_viral_history_videos()
    save_results(videos, "/opt/baal-agent/workspace/vox-director/out/competitor_research.json")
    print(f"\n✅ Done! {len(videos)} viral history videos found.")
