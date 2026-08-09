#!/usr/bin/env python3
"""
Idea Engine — Combine competitor viral video ideas into unique new topic
=========================================================================
Takes 2+ viral video titles/topics from competitor research and uses Agnes AI
to generate a unique new video topic in the same niche.

Example:
  Input:  "A Brief History of Alcohol" (7M views) + "How Fast Food Was Born" (105K views)
  Output: "The Strange History of Energy Drinks" — a new unique topic

The AI is prompted to:
1. Analyze what made the competitor videos viral (topic angle, format, hook)
2. Find a related but unexplored angle in the same niche
3. Generate a click-worthy title + brief concept description
"""
import json
import os
import re
import random
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path("/opt/baal-agent/workspace/vox-director")
AGNES_KEYS_FILE = BASE_DIR / ".agnes_keys"


def load_agnes_keys():
    """Load Agnes AI API keys for multi-key rotation."""
    keys = []
    with open(AGNES_KEYS_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                keys.append(line)
    return keys


def call_agnes_ai(system_prompt, user_prompt, timeout=120):
    """Call Agnes AI chat completions API."""
    keys = load_agnes_keys()
    ai_key = random.choice(keys)

    url = "https://apihub.agnes-ai.com/v1/chat/completions"
    payload = json.dumps({
        "model": "agnes-2.5-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 2000,
        "temperature": 0.9  # higher creativity for unique ideas
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ai_key}"
    }, method="POST")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8").strip()

    resp_data = json.loads(raw)
    return resp_data["choices"][0]["message"]["content"].strip()


def generate_idea(competitor_videos, niche="history"):
    """Take competitor viral videos and generate a unique new video idea.

    Args:
        competitor_videos: list of dicts with 'title', 'views', 'topic_extracted'
        niche: the content niche (default: 'history')

    Returns:
        dict with: topic, title, concept, inspired_by (list of source videos)
    """
    # Pick 2-3 top viral videos as inspiration
    top_videos = competitor_videos[:5]
    selected = random.sample(top_videos, min(3, len(top_videos)))

    # Format competitor info for the prompt
    competitor_info = []
    for v in selected:
        competitor_info.append(
            f"  • Title: \"{v['title']}\"\n"
            f"    Views: {v['views']:,}\n"
            f"    Topic: {v.get('topic_extracted', 'unknown')}\n"
            f"    Channel: {v.get('channel', 'unknown')}\n"
            f"    URL: {v.get('url', '')}"
        )
    competitor_text = "\n".join(competitor_info)

    system_prompt = (
        "You are an expert YouTube content strategist specializing in HISTORY content.\n"
        "Your job: analyze viral history videos from competitors and create a UNIQUE new\n"
        "video idea that's in the same niche but covers an unexplored angle.\n\n"
        "RULES:\n"
        "1. The new topic MUST be history-related (events, people, inventions, food, drink,\n"
        "   empires, wars, mysteries, daily life, cultural phenomena).\n"
        "2. Do NOT copy the competitor's exact topic. Find a RELATED but DIFFERENT angle.\n"
        "3. Think about what made the competitors viral: the topic is surprising, educational,\n"
        "   or has emotional impact. Your idea should have the same qualities.\n"
        "4. The topic should work as a SHORT video (30-60 seconds).\n"
        "5. Combine ideas from multiple competitors — cross-pollinate.\n"
        "   Example: 'history of alcohol' + 'how fast food was born' → 'the strange history\n"
        "   of energy drinks' or 'how coffee changed warfare'.\n"
        "6. The title should be click-worthy but NOT clickbait. Max 70 characters.\n"
        "7. Include a one-sentence concept of what the video covers.\n\n"
        "Output ONLY valid JSON:\n"
        '{"topic":"short topic for the video generator","title":"YouTube-optimized title",'
        '"concept":"one sentence describing what the video covers","angle":"why this will go viral"}'
    )

    user_prompt = (
        f"Here are {len(selected)} viral history videos from competitor channels:\n\n"
        f"{competitor_text}\n\n"
        f"Analyze what makes these viral and create a UNIQUE new history video idea.\n"
        f"Cross-pollinate the topics — find an unexplored angle in the same niche.\n"
        f"The idea must be different from all the above but appeal to the same audience.\n\n"
        f"Output ONLY the JSON. No markdown, no code fences."
    )

    print(f"🧠 Generating unique idea from {len(selected)} viral competitors...")
    for i, v in enumerate(selected, 1):
        print(f"   {i}. {v['title'][:50]} ({v['views']:,} views)")

    try:
        content = call_agnes_ai(system_prompt, user_prompt)

        # Strip markdown code fences if present
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        # Extract JSON
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            content = match[0]

        idea = json.loads(content)

        # Add source attribution
        idea["inspired_by"] = [
            {"title": v["title"], "views": v["views"], "url": v.get("url", "")}
            for v in selected
        ]

        print(f"✅ New idea generated!")
        print(f"   Topic: {idea.get('topic', '?')}")
        print(f"   Title: {idea.get('title', '?')}")
        print(f"   Angle: {idea.get('angle', '?')}")

        return idea

    except Exception as e:
        print(f"❌ Idea generation failed: {e}")
        # Fallback: combine two competitor topics manually
        if len(selected) >= 2:
            t1 = selected[0].get("topic_extracted", "ancient")
            t2 = selected[1].get("topic_extracted", "empire")
            return {
                "topic": f"the forgotten history of {t2} and {t1}",
                "title": f"The Strange Connection Between {t2.title()} and {t1.title()}",
                "concept": f"Exploring the surprising link between {t1} and {t2}",
                "angle": "Cross-pollination of two viral topics",
                "inspired_by": [
                    {"title": v["title"], "views": v["views"], "url": v.get("url", "")}
                    for v in selected
                ]
            }
        return None


if __name__ == "__main__":
    # Load competitor research
    research_path = BASE_DIR / "out" / "competitor_research.json"
    if not research_path.exists():
        print("❌ No competitor research found. Run competitor_watcher.py first.")
        sys.exit(1)

    videos = json.loads(research_path.read_text())
    print(f"📂 Loaded {len(videos)} competitor videos from {research_path}")

    idea = generate_idea(videos)
    if idea:
        print(f"\n{'='*60}")
        print(f"NEW VIDEO IDEA")
        print(f"{'='*60}")
        print(f"Topic:   {idea.get('topic', '?')}")
        print(f"Title:   {idea.get('title', '?')}")
        print(f"Concept: {idea.get('concept', '?')}")
        print(f"Angle:   {idea.get('angle', '?')}")
        print(f"\nInspired by:")
        for src in idea.get("inspired_by", []):
            print(f"  • {src['title'][:50]} ({src['views']:,} views)")
