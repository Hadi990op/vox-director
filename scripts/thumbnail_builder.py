#!/usr/bin/env python3
"""
Thumbnail Builder — Generate YouTube thumbnails from video keyframes
======================================================================
Downloads competitor thumbnails for inspiration, then generates a unique
white-background thumbnail using the video's own keyframe art + 1-2 word
text overlay that creates curiosity alongside the video title.

Strategy:
  - White background (competitors use dark/colored — we stand out)
  - Video keyframe as the hero visual (cropped, enhanced)
  - 1-2 word text overlay (bold, high-contrast) that COMPLEMENTS the title
    → not a summary, but a curiosity gap
  - Collage elements (torn paper edges, tape, bold shapes) = Vox brand

Usage:
  python3 thumbnail_builder.py <project_dir> [--title "..."] [--variation 1|2|3]
  python3 thumbnail_builder.py <project_dir> --analyze-competitors  # download competitor thumbs
"""
import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import random
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter, ImageOps

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


# ─── Text helpers ───────────────────────────────────────────────
def draw_text_with_outline(draw, pos, text, font, fill, outline_color=(0,0,0), width=4):
    """Draw text with thick outline for readability."""
    x, y = pos
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx == 0 and dy == 0:
                continue
            # Only draw circle outline (skip corners for speed)
            if dx*dx + dy*dy <= width*width + 1:
                draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)


def draw_text_with_shadow(draw, pos, text, font, fill, shadow_color=(0,0,0), offset=(4,4)):
    """Draw text with a drop shadow."""
    x, y = pos
    sx, sy = offset
    draw.text((x + sx, y + sy), text, font=font, fill=shadow_color)
    draw.text((x, y), text, font=font, fill=fill)


def fit_font(text, font_path, max_width, max_size, min_size=20):
    """Find the largest font size that fits text within max_width."""
    for size in range(max_size, min_size - 1, -2):
        font = load_font(font_path, size)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width:
            return font
    return load_font(font_path, min_size)


# ─── Curiosity text generation ──────────────────────────────────
CURIOSITY_TEMPLATES = [
    # (pattern, emotion)
    ("BUT WHY?", "confusion"),
    ("THE TRUTH", "revelation"),
    ("UNTIL THIS", "surprise"),
    ("NOBODY KNEW", "mystery"),
    ("HIDDEN", "mystery"),
    ("BANNED", "controversy"),
    ("WHAT THEY HID", "conspiracy"),
    ("THE REAL STORY", "revelation"),
    ("THEN THIS HAPPENED", "surprise"),
    ("BUT AT WHAT COST?", "tension"),
    ("IT CHANGED EVERYTHING", "impact"),
    ("THE FORGOTTEN", "mystery"),
    ("LOST TO TIME", "mystery"),
    ("UNTIL NOW", "revelation"),
    ("THE SHOCKING TRUTH", "shock"),
    ("WAS IT WORTH IT?", "tension"),
    ("THE HIDDEN COST", "tension"),
    ("WHAT WENT WRONG", "tension"),
    ("A TERRIBLE IDEA", "humor"),
    ("THE WORST DECISION", "humor"),
    ("IT BACKFIRED", "surprise"),
    ("THE SECRET", "mystery"),
    ("YOU WON'T BELIEVE", "shock"),
    ("THE DARK SIDE", "tension"),
    ("NOBODY SAW THIS COMING", "surprise"),
]

# Topic-specific curiosity word extraction
def _call_agnes_ai(system_prompt, user_prompt, timeout=45):
    """Call Agnes AI chat completions API for overlay text generation."""
    keys_file = BASE_DIR / ".agnes_keys"
    if not keys_file.exists():
        return None
    keys = [k.strip() for k in keys_file.read_text().splitlines() if k.strip()]
    if not keys:
        return None
    ai_key = random.choice(keys)

    url = "https://apihub.agnes-ai.com/v1/chat/completions"
    # Use a single user message (not system+user) — model sometimes ignores system
    combined_prompt = (
        "You are a YouTube thumbnail copywriter. "
        "Given a video title, generate a SHORT curiosity hook (1-4 words, ALL CAPS) "
        "to overlay on the thumbnail.\n"
        "Rules:\n"
        "1. Do NOT repeat words from the title\n"
        "2. Create a CURIOSITY GAP — the viewer must click to find the answer\n"
        "3. Maximum 4 words, ideally 2-3 words\n"
        "4. ALL CAPS, punchy, dramatic\n"
        "5. Respond with ONLY the text, nothing else\n"
        "Examples:\n"
        "  Title: 'Why Byzantium Was Drunk All The Time' → THEY WERE ADDICTED\n"
        "  Title: 'How Rome Really Fell' → NOBODY SAW THIS\n"
        "  Title: 'The King Who Drank Gold' → IT KILLED HIM\n"
        "  Title: 'Why The Sky Is Blue' → THE HIDDEN TRUTH\n"
        "  Title: 'The Worst Decision In History' → IT BACKFIRED\n\n"
        f"Now generate overlay text for this title:\n\"{user_prompt}\"\n"
        "Respond with ONLY the overlay text:"
    )

    payload = json.dumps({
        "model": "agnes-2.5-flash",
        "messages": [
            {"role": "user", "content": combined_prompt}
        ],
        "max_tokens": 500,  # model generates reasoning_content first
        "temperature": 0.8
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ai_key}"
    }, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8").strip()
        resp_data = json.loads(raw)
        return resp_data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  ⚠️ Agnes AI overlay generation failed: {e}")
        return None


def generate_overlay_text(topic, title=""):
    """Generate 1-2 word overlay text that creates curiosity with the title.

    The overlay text should NOT repeat the title — it should COMPLEMENT it,
    creating a curiosity gap that makes the viewer want to click.

    Examples:
      Title: "Why Byzantium Was Drunk All The Time" → Overlay: "THE HIDDEN COST"
      Title: "How Rome Really Fell" → Overlay: "NOBODY SAW THIS"
      Title: "The King Who Drank Gold" → Overlay: "IT KILLED HIM"

    Uses Agnes AI for smart hooks, falls back to rule-based generation.
    """
    display_text = (title or topic).strip()

    # ─── Try AI generation first ───
    ai_result = _call_agnes_ai(None, display_text)
    if ai_result:
        # Clean up: remove quotes, limit to 4 words, uppercase
        cleaned = ai_result.strip().strip('"\'').upper()
        # Remove trailing punctuation
        cleaned = re.sub(r'[.!?,;:]+$', '', cleaned)
        words = cleaned.split()
        if words and len(words) <= 5:
            overlay = " ".join(words[:4])
            print(f"  AI overlay: '{overlay}'")
            return overlay

    # ─── Fallback: rule-based generation ───
    print(f"  Using fallback overlay generation")
    random.seed(hash(topic) % 10000)

    text = display_text.lower()
    stop_words = {"the", "a", "an", "of", "how", "why", "what", "when", "where",
                  "did", "was", "is", "are", "history", "explained", "short",
                  "documentary", "animated", "story", "in", "on", "at", "to",
                  "really", "actually", "all", "time"}
    words = [w for w in re.split(r'[\s\-_,]+', text) if w and w not in stop_words and len(w) > 2]

    if words:
        key_word = words[0].upper()
        hooks = [
            f"THE REAL {key_word}",
            f"BUT WHY?",
            f"THE TRUTH",
            f"HIDDEN {key_word}",
            f"LOST {key_word}",
            f"THE FORGOTTEN {key_word}",
            f"UNTIL NOW",
            f"WHAT THEY HID",
            f"THE DARK SIDE",
            f"THEN THIS HAPPENED",
            f"IT CHANGED EVERYTHING",
            f"THE SHOCKING TRUTH",
            f"WAS IT WORTH IT?",
            f"NOBODY KNEW",
        ]
        return random.choice(hooks)

    return random.choice([t[0] for t in CURIOSITY_TEMPLATES])


# ─── Image processing ───────────────────────────────────────────
def enhance_image(img, saturation=1.4, contrast=1.2, brightness=1.05):
    """Enhance an image for thumbnail — vibrant, punchy."""
    img = ImageEnhance.Color(img).enhance(saturation)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    img = ImageEnhance.Brightness(img).enhance(brightness)
    return img


def crop_to_box(img, target_w, target_h):
    """Center-crop image to target dimensions."""
    w, h = img.size
    # Scale to cover target
    scale = max(target_w / w, target_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # Center crop
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def add_torn_paper_edge(img, side="bottom", depth=30, color="white"):
    """Add a torn paper edge effect (Vox collage aesthetic)."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if side == "bottom":
        for x in range(0, w, 3):
            tear = int(depth * (0.5 + 0.5 * __import__('math').sin(x * 0.05 + 1.3)))
            draw.line([(x, h - tear), (x + 3, h)], fill=(255, 255, 255, 255), width=3)
    elif side == "top":
        for x in range(0, w, 3):
            tear = int(depth * (0.5 + 0.5 * __import__('math').sin(x * 0.05 + 0.7)))
            draw.line([(x, 0), (x + 3, tear)], fill=(255, 255, 255, 255), width=3)
    elif side == "left":
        for y in range(0, h, 3):
            tear = int(depth * (0.5 + 0.5 * __import__('math').sin(y * 0.05 + 2.1)))
            draw.line([(0, y), (tear, y + 3)], fill=(255, 255, 255, 255), width=3)
    elif side == "right":
        for y in range(0, h, 3):
            tear = int(depth * (0.5 + 0.5 * __import__('math').sin(y * 0.05 + 0.3)))
            draw.line([(w - tear, y), (w, y + 3)], fill=(255, 255, 255, 255), width=3)

    return Image.alpha_composite(img.convert("RGBA"), overlay)


def add_shadow(img, offset=(8, 8), blur=12, opacity=120):
    """Add a drop shadow to an image (RGBA)."""
    w, h = img.size
    shadow = Image.new("RGBA", (w + abs(offset[0]) + blur * 2, h + abs(offset[1]) + blur * 2), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    # Draw shadow shape (simplified — just a rectangle)
    shadow_draw.rectangle(
        [blur, blur, w + blur, h + blur],
        fill=(0, 0, 0, opacity)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    # Paste original on top
    shadow.paste(img, (blur, blur), img if img.mode == "RGBA" else None)
    return shadow


def add_washi_tape(canvas, x, y, w=120, h=30, rotation=0, color=(255, 220, 100, 200)):
    """Add a washi tape decoration (Vox collage element)."""
    import math
    tape = Image.new("RGBA", (w, h), color)
    # Add slight texture
    d = ImageDraw.Draw(tape)
    for i in range(0, w, 8):
        d.line([(i, 0), (i, h)], fill=(color[0]-15, color[1]-15, color[2]-15, color[3]), width=1)
    if rotation:
        tape = tape.rotate(rotation, expand=True)
    canvas.paste(tape, (x, y), tape)
    return canvas


# ─── Thumbnail layouts ──────────────────────────────────────────
def make_thumbnail_white_bg(keyframe_path, overlay_text, topic="", variation=1):
    """Generate a white-background thumbnail with keyframe art + curiosity text.

    Variation 1: Image on right, text on left
    Variation 2: Image centered, text banner at bottom
    Variation 3: Image on left, text on right
    """
    # Create white canvas
    canvas = Image.new("RGBA", (TW, TH), (255, 255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # Load and enhance keyframe
    kf = Image.open(keyframe_path).convert("RGB")
    kf = enhance_image(kf, saturation=1.5, contrast=1.25, brightness=1.1)

    if variation == 1:
        return _layout_image_right_text_left(canvas, draw, kf, overlay_text, topic)
    elif variation == 2:
        return _layout_image_center_text_bottom(canvas, draw, kf, overlay_text, topic)
    else:
        return _layout_image_left_text_right(canvas, draw, kf, overlay_text, topic)


def _layout_image_right_text_left(canvas, draw, kf, overlay_text, topic):
    """Image occupies right 60%, text on left white area."""
    img_w = int(TW * 0.62)
    img_h = TH

    # Crop keyframe to fit right portion
    kf_cropped = crop_to_box(kf, img_w, img_h)
    kf_rgba = kf_cropped.convert("RGBA")

    # Add torn paper edge on left side of image
    kf_rgba = add_torn_paper_edge(kf_rgba, side="left", depth=25)

    # Add shadow behind image
    kf_shadow = add_shadow(kf_rgba, offset=(6, 6), blur=15, opacity=100)
    canvas.paste(kf_shadow, (TW - img_w - 5, 0), kf_shadow)
    canvas.paste(kf_rgba, (TW - img_w, 0), kf_rgba)

    # ─── Text on left ───
    text_area_w = TW - img_w - 40

    # Split overlay text into lines if long
    words = overlay_text.split()
    if len(overlay_text) > 12:
        mid = len(words) // 2
        line1 = " ".join(words[:mid])
        line2 = " ".join(words[mid:])
    else:
        line1 = overlay_text
        line2 = ""

    # Fit font to text area
    font_main = fit_font(line1, FONT_ANTON, text_area_w, 120, 40)
    bbox1 = font_main.getbbox(line1)
    text_w1 = bbox1[2] - bbox1[0]
    text_h1 = bbox1[3] - bbox1[1]

    # Vertical position — center-ish
    total_h = text_h1 + (font_main.getbbox(line2)[3] - font_main.getbbox(line2)[1] + 10 if line2 else 0)
    y_start = (TH - total_h) // 2 - 20

    # Draw main text — black with accent color
    accent_color = (220, 40, 40)  # bold red
    x_text = 40
    draw_text_with_outline(draw, (x_text, y_start), line1, font_main, accent_color, (255, 255, 255), width=5)

    if line2:
        font2 = fit_font(line2, FONT_ANTON, text_area_w, 100, 36)
        y2 = y_start + text_h1 + 15
        draw_text_with_outline(draw, (x_text, y2), line2, font2, (30, 30, 30), (255, 255, 255), width=4)

    # Add a colored bar accent
    bar_y = y_start + total_h + 20
    draw.rectangle([(x_text, bar_y), (x_text + 80, bar_y + 6)], fill=(220, 40, 40))

    # Small topic label at bottom
    if topic:
        font_label = load_font(FONT_BEBAS, 32)
        label = topic.upper()[:30]
        draw_text_with_outline(draw, (x_text, TH - 60), label, font_label, (120, 120, 120), (255, 255, 255), width=2)

    # Washi tape decoration on image corner
    add_washi_tape(canvas, TW - 150, 15, w=100, h=28, rotation=-8, color=(255, 100, 100, 220))

    return canvas.convert("RGB")


def _layout_image_center_text_bottom(canvas, draw, kf, overlay_text, topic):
    """Image centered with torn edges, text banner at bottom."""
    img_w = int(TW * 0.75)
    img_h = int(TH * 0.65)

    kf_cropped = crop_to_box(kf, img_w, img_h)
    kf_rgba = kf_cropped.convert("RGBA")

    # Add torn paper edges all around
    kf_rgba = add_torn_paper_edge(kf_rgba, side="top", depth=20)
    kf_rgba = add_torn_paper_edge(kf_rgba, side="bottom", depth=25)
    kf_rgba = add_torn_paper_edge(kf_rgba, side="left", depth=20)
    kf_rgba = add_torn_paper_edge(kf_rgba, side="right", depth=20)

    # Shadow
    kf_shadow = add_shadow(kf_rgba, offset=(5, 8), blur=18, opacity=120)
    px = (TW - img_w) // 2
    py = 30
    canvas.paste(kf_shadow, (px - 5, py - 3), kf_shadow)
    canvas.paste(kf_rgba, (px, py), kf_rgba)

    # ─── Text banner at bottom ───
    banner_h = 180
    banner_y = TH - banner_h - 10

    # Draw banner background (semi-transparent black bar)
    banner = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
    banner_draw = ImageDraw.Draw(banner)
    banner_draw.rounded_rectangle([(20, banner_y), (TW - 20, banner_y + banner_h)], radius=15, fill=(30, 30, 30, 235))
    canvas = Image.alpha_composite(canvas, banner)
    draw = ImageDraw.Draw(canvas)

    # Text on banner
    words = overlay_text.split()
    if len(overlay_text) > 14:
        mid = len(words) // 2
        line1 = " ".join(words[:mid])
        line2 = " ".join(words[mid:])
    else:
        line1 = overlay_text
        line2 = ""

    text_area_w = TW - 80
    font_main = fit_font(line1, FONT_ANTON, text_area_w, 90, 36)

    if line2:
        font2 = fit_font(line2, FONT_ANTON, text_area_w, 80, 32)
        bbox1 = font_main.getbbox(line1)
        bbox2 = font2.getbbox(line2)
        h1 = bbox1[3] - bbox1[1]
        h2 = bbox2[3] - bbox2[1]
        total_h = h1 + h2 + 10
        y1 = banner_y + (banner_h - total_h) // 2
        draw_text_with_outline(draw, (40, y1), line1, font_main, (255, 215, 60), (0, 0, 0), width=4)
        draw_text_with_outline(draw, (40, y1 + h1 + 8), line2, font2, (255, 255, 255), (0, 0, 0), width=3)
    else:
        bbox1 = font_main.getbbox(line1)
        h1 = bbox1[3] - bbox1[1]
        y1 = banner_y + (banner_h - h1) // 2
        draw_text_with_outline(draw, (40, y1), line1, font_main, (255, 215, 60), (0, 0, 0), width=4)

    # Washi tape on top corners
    add_washi_tape(canvas, 30, 20, w=90, h=25, rotation=-12, color=(255, 200, 80, 220))
    add_washi_tape(canvas, TW - 120, 20, w=90, h=25, rotation=10, color=(100, 200, 255, 220))

    return canvas.convert("RGB")


def _layout_image_left_text_right(canvas, draw, kf, overlay_text, topic):
    """Image occupies left 60%, text on right white area."""
    img_w = int(TW * 0.62)

    kf_cropped = crop_to_box(kf, img_w, TH)
    kf_rgba = kf_cropped.convert("RGBA")
    kf_rgba = add_torn_paper_edge(kf_rgba, side="right", depth=25)

    kf_shadow = add_shadow(kf_rgba, offset=(-6, 6), blur=15, opacity=100)
    canvas.paste(kf_shadow, (5, 0), kf_shadow)
    canvas.paste(kf_rgba, (0, 0), kf_rgba)

    # ─── Text on right ───
    text_area_w = TW - img_w - 40
    text_x = img_w + 30

    words = overlay_text.split()
    if len(overlay_text) > 12:
        mid = len(words) // 2
        line1 = " ".join(words[:mid])
        line2 = " ".join(words[mid:])
    else:
        line1 = overlay_text
        line2 = ""

    font_main = fit_font(line1, FONT_ANTON, text_area_w, 120, 40)
    bbox1 = font_main.getbbox(line1)
    text_h1 = bbox1[3] - bbox1[1]

    total_h = text_h1 + (font_main.getbbox(line2)[3] - font_main.getbbox(line2)[1] + 10 if line2 else 0)
    y_start = (TH - total_h) // 2 - 20

    accent_color = (220, 40, 40)
    draw_text_with_outline(draw, (text_x, y_start), line1, font_main, accent_color, (255, 255, 255), width=5)

    if line2:
        font2 = fit_font(line2, FONT_ANTON, text_area_w, 100, 36)
        y2 = y_start + text_h1 + 15
        draw_text_with_outline(draw, (text_x, y2), line2, font2, (30, 30, 30), (255, 255, 255), width=4)

    # Colored bar
    bar_y = y_start + total_h + 20
    draw.rectangle([(text_x, bar_y), (text_x + 80, bar_y + 6)], fill=(220, 40, 40))

    if topic:
        font_label = load_font(FONT_BEBAS, 32)
        label = topic.upper()[:30]
        draw_text_with_outline(draw, (text_x, TH - 60), label, font_label, (120, 120, 120), (255, 255, 255), width=2)

    add_washi_tape(canvas, 50, 15, w=100, h=28, rotation=8, color=(100, 200, 255, 220))

    return canvas.convert("RGB")


# ─── Best keyframe selection ────────────────────────────────────
def select_best_keyframe(project_dir):
    """Select the most visually interesting keyframe for the thumbnail.

    Picks the first 'b' keyframe (usually the wider shot) from the first beat.
    Falls back to any keyframe.
    """
    kf_dir = project_dir / "keyframes"
    if not kf_dir.exists():
        return None

    # Prefer kf_1b.jpg (first beat, wide shot)
    candidates = [
        kf_dir / "kf_1b.jpg",
        kf_dir / "kf_1b.png",
        kf_dir / "kf_1a.jpg",
        kf_dir / "kf_1a.png",
        kf_dir / "kf_2b.jpg",
        kf_dir / "kf_2a.jpg",
    ]
    for c in candidates:
        if c.exists():
            return c

    # Fallback: any keyframe
    all_kf = sorted(kf_dir.glob("kf_*b.*")) or sorted(kf_dir.glob("kf_*.*"))
    return all_kf[0] if all_kf else None


# ─── Competitor thumbnail download ──────────────────────────────
def download_competitor_thumbnails(video_ids, out_dir):
    """Download competitor thumbnails for inspiration.

    Stores them in out_dir/competitor_thumbs/ for reference.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for vid_id in video_ids:
        # Check if already downloaded
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


# ─── Main ────────────────────────────────────────────────────────
def generate_thumbnail(project_dir, title="", variation=1, output_name=None):
    """Generate a thumbnail for a video project.

    Args:
        project_dir: Path to the project directory (with beats.json, keyframes/)
        title: Video title (used for overlay text generation)
        variation: Layout variation (1, 2, or 3)
        output_name: Output filename (default: thumbnail_v{variation}.jpg)

    Returns:
        Path to the generated thumbnail
    """
    project_dir = Path(project_dir)

    # Load metadata
    beats_path = project_dir / "beats.json"
    topic = ""
    yt_title = ""
    if beats_path.exists():
        beats = json.loads(beats_path.read_text())
        topic = beats.get("topic", "")
        yt_title = beats.get("yt_title", "")

    # Use provided title or fall back
    display_title = title or yt_title or topic

    # Select keyframe
    kf_path = select_best_keyframe(project_dir)
    if not kf_path:
        print(f"ERROR: No keyframes found in {project_dir}/keyframes/")
        sys.exit(1)

    print(f"  Keyframe: {kf_path.name}")
    print(f"  Topic: {topic}")
    print(f"  Title: {display_title}")

    # Generate overlay text
    overlay_text = generate_overlay_text(topic, display_title)
    print(f"  Overlay text: '{overlay_text}'")

    # Generate thumbnail
    thumb = make_thumbnail_white_bg(kf_path, overlay_text, topic, variation=variation)

    # Save
    if output_name is None:
        output_name = f"thumbnail_v{variation}.jpg"
    out_path = project_dir / output_name
    thumb.save(str(out_path), "JPEG", quality=92)
    print(f"  ✅ Thumbnail saved: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate YouTube thumbnail from video keyframes")
    parser.add_argument("project_dir", help="Path to the video project directory")
    parser.add_argument("--title", default="", help="Video title for overlay text generation")
    parser.add_argument("--variation", type=int, default=1, choices=[1, 2, 3],
                        help="Layout variation: 1=image right, 2=image center+banner, 3=image left")
    parser.add_argument("--all-variations", action="store_true",
                        help="Generate all 3 variations")
    parser.add_argument("--analyze-competitors", action="store_true",
                        help="Download competitor thumbnails for inspiration")
    parser.add_argument("--competitor-ids", nargs="*",
                        help="YouTube video IDs to download thumbnails from")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)

    if args.analyze_competitors:
        if not args.competitor_ids:
            print("Please provide --competitor-ids for thumbnail download")
            sys.exit(1)
        out_dir = project_dir / "competitor_thumbs" if project_dir.name != "competitor_thumbs" else project_dir
        download_competitor_thumbnails(args.competitor_ids, out_dir)
        print(f"\n✅ Downloaded {len(args.competitor_ids)} competitor thumbnails to {out_dir}")
        return

    print("=== Thumbnail Builder ===")
    print(f"  Project: {project_dir}")
    print(f"  Layout: variation {args.variation}")
    print()

    if args.all_variations:
        for v in [1, 2, 3]:
            print(f"  --- Variation {v} ---")
            generate_thumbnail(project_dir, args.title, variation=v)
            print()
    else:
        generate_thumbnail(project_dir, args.title, variation=args.variation)


if __name__ == "__main__":
    main()
