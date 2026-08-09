#!/usr/bin/env python3
"""
Thumbnail Intelligence System — CTR-driven YouTube thumbnail generator
=========================================================================
Replaces the old "keyframe crop + text overlay" approach with a proper
visual storytelling system based on CTR psychology.

Pipeline:
  1. AI CONCEPT GENERATION
     - Analyze video title + topic + beats
     - Generate 5 fundamentally different thumbnail concepts
     - Score each on curiosity, comprehension, emotional impact, etc.
     - Select the strongest concept → produce a BLUEPRINT

  2. AI IMAGE GENERATION
     - Build a detailed visual storytelling prompt from the blueprint
     - Generate a 1280x720 thumbnail image via Agnes AI image generation
     - The image IS the thumbnail (hero, supporting objects, background)

  3. TEXT OVERLAY
     - Add 1-4 word curiosity hook text on top of the generated image
     - Text type chosen by AI: question, contradiction, revelation, etc.
     - Bold typography with outline for mobile readability

Blueprint fields:
  - hero, support elements, text (1-4 words), text function type
  - color palette, composition, CTR mechanism, information gap
  - emotional expression, visual hook

Usage:
  python3 thumbnail_builder.py <project_dir> [--title "..."]
  python3 thumbnail_builder.py <project_dir> --concept-json   # just print blueprint
  python3 thumbnail_builder.py <project_dir> --concepts 5     # generate 5 concepts
"""
import argparse
import json
import os
import re
import subprocess
import sys
import random
import urllib.request
import urllib.error
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

# ─── Paths ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
FONTS_DIR = BASE_DIR / "assets" / "fonts"
TW, TH = 1280, 720  # YouTube thumbnail standard

# ─── Fonts ──────────────────────────────────────────────────────
FONT_ANTON = str(FONTS_DIR / "Anton-Regular.ttf")       # Tall condensed bold
FONT_BEBAS = str(FONTS_DIR / "BebasNeue-Regular.ttf")    # Narrow caps
FONT_ARCHIVO = str(FONTS_DIR / "ArchivoBlack-Regular.ttf")  # Heavy block
FONT_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def load_font(path, size):
    """Load a font, fallback to DejaVu if not found."""
    try:
        return ImageFont.truetype(path, size)
    except (IOError, OSError):
        return ImageFont.truetype(FONT_DEJAVU_BOLD, size)


# ─── Agnes AI helpers ──────────────────────────────────────────
AGNES_API_BASE = "https://apihub.agnes-ai.com"


def _agnes_keys():
    keys_file = BASE_DIR / ".agnes_keys"
    if not keys_file.exists():
        return []
    return [k.strip() for k in keys_file.read_text().splitlines() if k.strip()]


def _agnes_chat(prompt, timeout=90, temperature=0.85):
    """Call Agnes AI chat completions. Returns the content string or None."""
    keys = _agnes_keys()
    if not keys:
        return None
    key = random.choice(keys)

    url = f"{AGNES_API_BASE}/v1/chat/completions"
    payload = json.dumps({
        "model": "agnes-2.5-flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,  # reasoning models generate hidden reasoning first
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8").strip()
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  ⚠️ Agnes AI chat failed: {e}")
        return None


def _agnes_image(prompt, timeout=120):
    """Generate an image via Agnes AI. Downloads and returns PIL Image."""
    keys = _agnes_keys()
    if not keys:
        raise RuntimeError("No Agnes API keys available")

    last_err = None
    for attempt in range(4):
        key = random.choice(keys)
        url = f"{AGNES_API_BASE}/v1/images/generations"
        payload = json.dumps({
            "model": "agnes-image-2.1-flash",
            "prompt": prompt,
            "n": 1,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            data_list = result.get("data") or []
            if not data_list or not data_list[0].get("url"):
                raise RuntimeError("No image URL in response")
            img_url = data_list[0]["url"]

            # Download the image
            with urllib.request.urlopen(img_url, timeout=60) as img_resp:
                img_data = img_resp.read()

            from io import BytesIO
            return Image.open(BytesIO(img_data)).convert("RGB")
        except Exception as e:
            last_err = e
            print(f"  ⚠️ Agnes image attempt {attempt+1} failed: {e}")
            import time
            time.sleep(2)

    raise RuntimeError(f"Agnes AI image generation failed after 4 tries: {last_err}")


# ─── Concept generation (the intelligence layer) ────────────────
CONCEPT_SYSTEM_PROMPT = """\
You are an expert YouTube Thumbnail Strategist, CTR Designer, and Visual Storyteller.

Your job is to design thumbnails that STOP scrolling, communicate the video's core idea instantly, create an information gap, and make the viewer curious enough to click.

CORE PRINCIPLES:
1. Title + Thumbnail = ONE curiosity unit. The thumbnail must NOT repeat the title — it must ADD a new question or surprising implication.
2. ONE core idea. Don't create a collection of unrelated objects. Everything supports one visual argument.
3. 1-4 words of text MAX. Prefer 1-2 words. The thumbnail is NOT a title card.
4. Text types: QUESTION (NO WATER?), CONTRADICTION (WINE ≠ WATER), REVELATION (SECRET), ACCUSATION (FRAUD), WARNING (BANNED), SHOCK (SERIOUSLY?!)
5. Hero object rule: One dominant hero (30-60% of visual attention). Viewer must instantly know "THIS is what I look at."
6. Visual storytelling: Show a mini-story, not a static scene. Emperor + huge goblet + empty jars = "this guy drank a LOT."
7. 3-5 element limit: 1 hero + 1-2 supporting + 1 text + 1 background. Remove anything that doesn't answer: what happened? why? who? what's unusual? what's hidden?
8. Curiosity gap: Reveal 60-70%, hide 30-40%. The viewer needs the video for the missing piece.
9. Mobile test: Text must be readable at 10-15% size. One dominant focal point.
10. Collage style: Physical paper-collage aesthetic — torn edges, layered paper cutouts, washi tape, drop shadows, parchment/cardstock textures. Objects look physically placed, not painted.

DO NOT design a beautiful image. Design a CLICKABLE visual argument.
The target is making the viewer's brain say: "Wait… what?"
NOT: "Wow, nice artwork."
"""


def generate_concepts(title, topic, narration_sample="", num_concepts=5):
    """Generate multiple thumbnail concepts via AI.

    Returns a list of concept blueprints (dicts).
    Each blueprint has: concept_name, hero, support, text, text_function,
    color_palette, composition, ctr_mechanism, info_gap, emotional_trigger,
    visual_description, image_prompt
    """
    user_prompt = f"""\
{CONCEPT_SYSTEM_PROMPT}

VIDEO TITLE: {title}
TOPIC: {topic}
NARRATION EXCERPT: {narration_sample[:500]}

Generate {num_concepts} FUNDAMENTALLY DIFFERENT thumbnail concepts for this video.
Each concept must use a different approach:
  - Concept A: Character-driven (hero person with expression/body language)
  - Concept B: Object-driven (dominant object as hero)
  - Concept C: Mystery/investigation (document/evidence/clue focus)
  - Concept D: Contradiction (visual paradox or before/after)
  - Concept E: Extreme visual metaphor (giant object, surreal scale)

For EACH concept, output a JSON object with these EXACT fields:
{{
  "concept_name": "short name",
  "concept_type": "character|object|mystery|contradiction|metaphor",
  "hero": "the main visual element (person/object/thing)",
  "hero_expression": "emotion/body language if character, else null",
  "support": ["1-2 supporting elements that answer what/why/who"],
  "text": "1-4 word overlay text (ALL CAPS, no quotes)",
  "text_function": "question|contradiction|revelation|accusation|warning|shock",
  "color_palette": ["3-5 specific colors, e.g. 'imperial purple', 'parchment beige', 'wine red', 'gold', 'black'"],
  "composition": "left/right/center hierarchy description",
  "background": "contextual background (not competing with hero)",
  "ctr_mechanism": "why someone clicks — the curiosity trigger",
  "info_gap": "what remains unanswered (the 30-40% hidden)",
  "emotional_trigger": "curiosity|shock|disbelief|fear|humor|surprise|suspicion|fascination",
  "visual_description": "2-3 sentence description of what the thumbnail looks like",
  "image_prompt": "Detailed prompt for AI image generation. Must describe the FULL visual scene in present tense. MUST include: the hero, supporting objects, background, composition (where things are positioned), art style (paper collage with torn edges, layered paper cutouts, washi tape, drop shadows), color palette, and emotional mood. Do NOT include text in the image prompt — text is added separately. 1280x720 landscape format."
}}

Score each concept 1-10 on: curiosity, instant_comprehension, emotional_impact, mobile_readability, title_synergy, visual_uniqueness, ctr_potential.

Output ONLY a JSON array of {num_concepts} concept objects. No markdown, no explanation, just the JSON array.

Example for "Why Byzantium Was Drunk All The Time":
[
  {{
    "concept_name": "The Emperor's Goblet",
    "concept_type": "character",
    "hero": "Byzantine emperor holding an absurdly large wine goblet",
    "hero_expression": "slightly drunk, confident smirk, tilted crown",
    "support": ["multiple empty wine amphorae scattered on floor"],
    "text": "NO WATER?",
    "text_function": "question",
    "color_palette": ["imperial purple", "parchment beige", "wine red", "gold", "black"],
    "composition": "emperor center-right, empty amphorae bottom-left, text top-left",
    "background": "subtle Byzantine mosaic pattern, low contrast",
    "ctr_mechanism": "viewer wonders if water safety explains the drinking",
    "info_gap": "whether water availability is actually the reason",
    "emotional_trigger": "curiosity",
    "visual_description": "A Byzantine emperor in purple robes holds an impossibly large wine goblet, looking drunk and pleased. Multiple empty amphorae lie scattered at his feet. The scene is a paper collage with torn edges and layered cutouts.",
    "image_prompt": "Paper collage style illustration, 1280x720 landscape. A Byzantine emperor in imperial purple robes holds an absurdly oversized golden wine goblet, standing center-right. He has a slightly drunk confident expression, crown tilted. Multiple empty clay amphorae scattered on the floor at bottom-left. Background is a subtle low-contrast Byzantine mosaic pattern in muted gold and beige. Art style: physical paper collage with torn edges, layered paper cutouts, visible drop shadows, washi tape accents, parchment texture. Color palette: imperial purple, parchment beige, wine red, gold, black. Mood: humorous, excessive, curious. No text in image."
  }}
]

Now generate {num_concepts} concepts for the video above. Output ONLY the JSON array:"""

    result = _agnes_chat(user_prompt, timeout=120, temperature=0.9)
    if not result:
        return _fallback_concepts(title, topic, num_concepts)

    # Parse JSON — handle markdown code fences
    cleaned = result.strip()
    if cleaned.startswith("```"):
        # Remove markdown fences
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)

    try:
        concepts = json.loads(cleaned)
        if isinstance(concepts, list) and len(concepts) > 0:
            # Validate and clean each concept
            valid = []
            for c in concepts:
                if _validate_concept(c):
                    valid.append(c)
            if valid:
                return valid[:num_concepts]
    except json.JSONDecodeError as e:
        print(f"  ⚠️ Concept JSON parse failed: {e}")
        # Try to extract JSON array from text
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                concepts = json.loads(match.group())
                if isinstance(concepts, list) and len(concepts) > 0:
                    valid = [c for c in concepts if _validate_concept(c)]
                    if valid:
                        return valid[:num_concepts]
            except:
                pass

    print(f"  ⚠️ AI concept generation failed, using fallback")
    return _fallback_concepts(title, topic, num_concepts)


def _validate_concept(c):
    """Check that a concept has the minimum required fields."""
    required = ["text", "image_prompt", "hero", "composition"]
    return all(c.get(f) for f in required)


def _fallback_concepts(title, topic, num_concepts=3):
    """Generate simple fallback concepts when AI fails."""
    topic_words = [w for w in re.split(r'[\s\-_,]+', topic.lower())
                   if w and len(w) > 3 and w not in {"the", "history", "why", "how", "what", "really", "actually"}]
    key_word = topic_words[0].upper() if topic_words else "THIS"

    base = {
        "hero": f"illustration related to {topic}",
        "hero_expression": None,
        "support": [],
        "text": f"THE TRUTH",
        "text_function": "revelation",
        "color_palette": ["parchment beige", "deep red", "black", "gold"],
        "composition": "hero center, text top",
        "background": "simple textured parchment",
        "ctr_mechanism": "curiosity about hidden truth",
        "info_gap": "what the truth actually is",
        "emotional_trigger": "curiosity",
        "visual_description": f"A paper collage illustration about {topic} with bold torn edges.",
        "image_prompt": f"Paper collage style illustration about {topic}, 1280x720 landscape. Bold torn paper edges, layered cutouts, drop shadows, parchment texture, muted colors with one bold accent. No text in image.",
    }

    concepts = []
    texts = ["THE TRUTH", f"BUT WHY?", "HIDDEN", "EXPOSED"]
    for i in range(min(num_concepts, len(texts))):
        c = dict(base)
        c["concept_name"] = f"Fallback {i+1}"
        c["text"] = texts[i]
        concepts.append(c)

    return concepts


# ─── Text overlay rendering ────────────────────────────────────
def draw_text_with_outline(draw, pos, text, font, fill, outline_color=(0,0,0), width=4):
    """Draw text with thick outline for readability."""
    x, y = pos
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx == 0 and dy == 0:
                continue
            if dx*dx + dy*dy <= width*width + 1:
                draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)


def fit_font(text, font_path, max_width, max_size, min_size=20):
    """Find the largest font size that fits text within max_width."""
    for size in range(max_size, min_size - 1, -2):
        font = load_font(font_path, size)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width:
            return font
    return load_font(font_path, min_size)


# Text function → accent color mapping
TEXT_COLORS = {
    "question":      (255, 215, 60),   # yellow — curiosity
    "contradiction": (255, 80, 80),    # red — conflict
    "revelation":    (255, 215, 60),   # yellow — discovery
    "accusation":    (255, 50, 50),    # bright red — blame
    "warning":    (255, 140, 0),    # orange — caution
    "shock":         (255, 255, 255),  # white — impact
}


def add_text_overlay(img, concept):
    """Add the curiosity hook text overlay to the generated thumbnail image.

    Positions text based on the concept's composition field.
    """
    text = concept.get("text", "").upper()
    if not text:
        return img

    text_function = concept.get("text_function", "question").lower()
    composition = concept.get("composition", "").lower()

    canvas = img.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # Determine text position from composition
    if "top" in composition and "left" in composition:
        position = "top-left"
    elif "top" in composition:
        position = "top"
    elif "bottom" in composition:
        position = "bottom"
    elif "left" in composition:
        position = "left"
    elif "right" in composition:
        position = "right"
    else:
        position = "top-left"

    # Determine text area dimensions based on position
    margin = 40
    if position in ("top-left", "top", "left"):
        text_area_w = int(TW * 0.45)
    elif position == "right":
        text_area_w = int(TW * 0.40)
    else:
        text_area_w = TW - 2 * margin

    # Split text into lines if > 2 words
    words = text.split()
    if len(words) > 2:
        mid = len(words) // 2
        line1 = " ".join(words[:mid])
        line2 = " ".join(words[mid:])
    elif len(words) == 2 and len(text) > 10:
        line1 = words[0]
        line2 = words[1]
    else:
        line1 = text
        line2 = ""

    # Fit fonts
    font1 = fit_font(line1, FONT_ANTON, text_area_w, 130, 40)
    bbox1 = font1.getbbox(line1)
    h1 = bbox1[3] - bbox1[1]

    font2 = None
    h2 = 0
    if line2:
        font2 = fit_font(line2, FONT_ANTON, text_area_w, 110, 36)
        bbox2 = font2.getbbox(line2)
        h2 = bbox2[3] - bbox2[1]

    total_h = h1 + h2 + (15 if line2 else 0)

    # Calculate position
    if position == "top-left" or position == "top":
        x = margin
        y = margin + 10
    elif position == "bottom":
        x = (TW - max(bbox1[2]-bbox1[0], (font2.getbbox(line2)[2]-font2.getbbox(line2)[0] if font2 else 0))) // 2
        y = TH - total_h - margin - 20
    elif position == "left":
        x = margin
        y = (TH - total_h) // 2
    elif position == "right":
        x = TW - text_area_w - margin
        y = (TH - total_h) // 2
    else:
        x = margin
        y = margin + 10

    # Choose colors based on text function
    accent = TEXT_COLORS.get(text_function, (255, 215, 60))

    # Draw a semi-transparent backing box behind text for readability
    box_padding = 20
    box_x1 = x - box_padding
    box_y1 = y - box_padding // 2
    box_x2 = x + text_area_w + box_padding
    box_y2 = y + total_h + box_padding

    # Create overlay for text background
    text_bg = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(text_bg)
    bg_draw.rounded_rectangle(
        [(box_x1, box_y1), (box_x2, box_y2)],
        radius=12,
        fill=(0, 0, 0, 140)  # semi-transparent black
    )
    canvas = Image.alpha_composite(canvas, text_bg)
    draw = ImageDraw.Draw(canvas)

    # Draw text with thick outline
    draw_text_with_outline(draw, (x, y), line1, font1, accent, (0, 0, 0), width=5)

    if line2:
        draw_text_with_outline(draw, (x, y + h1 + 15), line2, font2,
                                (255, 255, 255), (0, 0, 0), width=4)

    return canvas.convert("RGB")


# ─── Thumbnail generation ──────────────────────────────────────
def generate_thumbnail_image(concept):
    """Generate the thumbnail visual via Agnes AI image generation.

    Uses the concept's image_prompt to create a 1280x720 paper-collage
    thumbnail with visual storytelling.
    """
    image_prompt = concept.get("image_prompt", "")
    if not image_prompt:
        raise ValueError("Concept has no image_prompt")

    print(f"  🎨 Generating thumbnail image via Agnes AI...")
    print(f"  Prompt: {image_prompt[:150]}...")

    img = _agnes_image(image_prompt, timeout=180)

    # Agnes returns 1024x1024 — crop/resize to 1280x720
    img = _fit_to_thumbnail(img)

    print(f"  ✅ Image generated: {img.size}")
    return img


def _fit_to_thumbnail(img):
    """Resize/crop an image to 1280x720 (16:9) thumbnail size."""
    src_w, src_h = img.size
    target_ratio = TW / TH
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # Source is wider — crop width
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        # Source is taller — crop height
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    return img.resize((TW, TH), Image.LANCZOS)


def build_thumbnail(project_dir, title="", topic="", concept_index=0,
                    narration_sample="", num_concepts=5):
    """Full pipeline: generate concepts → pick best → generate image → add text.

    Returns dict with: thumbnail_path, concept, all_concepts
    """
    project_dir = Path(project_dir)

    # Load metadata from beats.json if available
    if not topic or not title:
        beats_path = project_dir / "beats.json"
        if beats_path.exists():
            beats = json.loads(beats_path.read_text())
            topic = topic or beats.get("topic", "")
            title = title or beats.get("yt_title", "")
            if not narration_sample:
                # Extract narration sample from beats
                narrations = [b.get("narration", "") for b in beats.get("beats", []) if b.get("narration")]
                narration_sample = " ".join(narrations[:5])

    print(f"\n=== Thumbnail Intelligence System ===")
    print(f"  Title: {title}")
    print(f"  Topic: {topic}")

    # Step 1: Generate concepts
    print(f"\n  📋 Generating {num_concepts} thumbnail concepts...")
    concepts = generate_concepts(title, topic, narration_sample, num_concepts=num_concepts)

    print(f"\n  Generated {len(concepts)} concepts:")
    for i, c in enumerate(concepts):
        print(f"    [{i}] {c.get('concept_name', '?')} — text: \"{c.get('text', '?')}\" — type: {c.get('concept_type', '?')}")

    # Save all concepts to JSON for UI
    concepts_path = project_dir / "thumbnail_concepts.json"
    concepts_path.write_text(json.dumps(concepts, indent=2, ensure_ascii=False))
    print(f"\n  💾 Saved concepts: {concepts_path}")

    # Pick concept
    if concept_index >= len(concepts):
        concept_index = 0
    chosen = concepts[concept_index]
    print(f"\n  → Selected concept [{concept_index}]: {chosen.get('concept_name', '?')}")
    print(f"    Hero: {chosen.get('hero', '?')}")
    print(f"    Text: \"{chosen.get('text', '?')}\" ({chosen.get('text_function', '?')})")
    print(f"    CTR mechanism: {chosen.get('ctr_mechanism', '?')}")
    print(f"    Info gap: {chosen.get('info_gap', '?')}")

    # Step 2: Generate thumbnail image
    print(f"\n  🎨 Generating thumbnail visual...")
    img = generate_thumbnail_image(chosen)

    # Step 3: Add text overlay
    print(f"\n  ✏️ Adding text overlay: \"{chosen.get('text', '')}\"")
    img = add_text_overlay(img, chosen)

    # Save
    out_path = project_dir / "thumbnail.jpg"
    img.save(str(out_path), "JPEG", quality=92)
    print(f"\n  ✅ Thumbnail saved: {out_path}")

    # Also save concept-specific version
    concept_out = project_dir / f"thumbnail_concept_{concept_index}.jpg"
    img.save(str(concept_out), "JPEG", quality=92)

    return {
        "thumbnail_path": str(out_path),
        "concept": chosen,
        "all_concepts": concepts,
        "concept_index": concept_index,
    }


def generate_thumbnail_for_concept(project_dir, concept, concept_index=0):
    """Generate a thumbnail for a specific concept (no concept generation step).

    Used when the user selects a concept from the UI.
    """
    project_dir = Path(project_dir)

    print(f"\n  🎨 Generating thumbnail for concept [{concept_index}]: {concept.get('concept_name', '?')}")
    img = generate_thumbnail_image(concept)

    print(f"  ✏️ Adding text overlay: \"{concept.get('text', '')}\"")
    img = add_text_overlay(img, concept)

    out_path = project_dir / f"thumbnail_concept_{concept_index}.jpg"
    img.save(str(out_path), "JPEG", quality=92)

    # Also set as main thumbnail
    main_path = project_dir / "thumbnail.jpg"
    img.save(str(main_path), "JPEG", quality=92)

    print(f"  ✅ Thumbnail saved: {out_path}")
    return str(out_path)


# ─── Competitor thumbnail download (kept from old version) ─────
def download_competitor_thumbnails(video_ids, out_dir):
    """Download competitor thumbnails for inspiration."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for vid_id in video_ids:
        existing = list(out_dir.glob(f"{vid_id}.*"))
        if existing:
            continue
        try:
            subprocess.run(
                ["yt-dlp", "--write-thumbnail", "--skip-download", "--no-warnings",
                 f"https://www.youtube.com/watch?v={vid_id}",
                 "-o", str(out_dir / f"{vid_id}.%(ext)s")],
                capture_output=True, text=True, timeout=30
            )
            print(f"  📥 Downloaded thumbnail: {vid_id}")
        except Exception as e:
            print(f"  ❌ Failed to download {vid_id}: {e}")


# ─── CLI ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Thumbnail Intelligence System — CTR-driven YouTube thumbnail generator")
    parser.add_argument("project_dir", help="Path to the video project directory")
    parser.add_argument("--title", default="", help="Video title")
    parser.add_argument("--topic", default="", help="Video topic")
    parser.add_argument("--concept", type=int, default=0, help="Which concept to use (0-indexed)")
    parser.add_argument("--concepts", type=int, default=5, help="Number of concepts to generate")
    parser.add_argument("--concept-json", action="store_true", help="Only generate concepts and print as JSON, don't generate image")
    parser.add_argument("--use-concept", type=int, default=None, help="Use an existing concept from thumbnail_concepts.json (by index)")
    parser.add_argument("--analyze-competitors", action="store_true", help="Download competitor thumbnails")
    parser.add_argument("--competitor-ids", nargs="*", help="YouTube video IDs for competitor thumbnails")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)

    if args.analyze_competitors:
        if not args.competitor_ids:
            print("Please provide --competitor-ids")
            sys.exit(1)
        out_dir = project_dir / "competitor_thumbs" if project_dir.name != "competitor_thumbs" else project_dir
        download_competitor_thumbnails(args.competitor_ids, out_dir)
        print(f"\n✅ Downloaded {len(args.competitor_ids)} competitor thumbnails to {out_dir}")
        return

    # Load metadata
    beats_path = project_dir / "beats.json"
    title = args.title
    topic = args.topic
    narration_sample = ""
    if beats_path.exists():
        beats = json.loads(beats_path.read_text())
        title = title or beats.get("yt_title", "")
        topic = topic or beats.get("topic", "")
        narrations = [b.get("narration", "") for b in beats.get("beats", []) if b.get("narration")]
        narration_sample = " ".join(narrations[:5])

    # Concept-only mode
    if args.concept_json:
        concepts = generate_concepts(title, topic, narration_sample, num_concepts=args.concepts)
        print(json.dumps(concepts, indent=2, ensure_ascii=False))
        return

    # Use existing concept mode
    if args.use_concept is not None:
        concepts_path = project_dir / "thumbnail_concepts.json"
        if not concepts_path.exists():
            print("ERROR: No thumbnail_concepts.json found. Generate concepts first.")
            sys.exit(1)
        concepts = json.loads(concepts_path.read_text())
        idx = args.use_concept
        if idx >= len(concepts):
            print(f"ERROR: Concept index {idx} out of range (have {len(concepts)})")
            sys.exit(1)
        generate_thumbnail_for_concept(project_dir, concepts[idx], concept_index=idx)
        return

    # Full pipeline
    result = build_thumbnail(
        project_dir, title=title, topic=topic,
        concept_index=args.concept, narration_sample=narration_sample,
        num_concepts=args.concepts
    )
    print(f"\n✅ Done! Thumbnail: {result['thumbnail_path']}")


if __name__ == "__main__":
    main()
