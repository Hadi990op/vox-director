#!/usr/bin/env python3
"""
Paper-collage post-processor — turns any AI image into a Vox-style collage.

Pollinations free tier only has the "sana" model which produces photorealistic
images, not paper collages. This module post-processes each keyframe with
Pillow to add the collage DNA:

  1. Bold flat background color (per beat)
  2. Halftone dot overlay (newspaper print texture)
  3. Paper texture (subtle grain + fibers)
  4. Torn edge vignette (rough paper edges)
  5. Color grading (boost saturation, high contrast)
  6. Newspaper clipping border (dark border on subject cut-outs)
  7. Headline text overlay (bold cut-out style)

The result reads as a hand-assembled paper collage even from a photorealistic
source image, matching the Vox explainer aesthetic.

Usage:
  from collage_post import apply_collage_effect
  apply_collage_effect("kf_1a.jpg", "kf_1a_collage.jpg",
                       bg_color="#C8362D", headline="THE FIRST EV")
"""
import os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageOps

# ---- Font paths (same as text_overlay.py) ----
FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
FONT_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

# Bold flat background colors per beat feel (Vox palette)
BG_COLORS = {
    "red": (200, 54, 45),
    "deep-red": (139, 30, 30),
    "warm-ochre": (204, 145, 61),
    "gold": (212, 175, 55),
    "teal": (45, 130, 130),
    "mustard": (214, 171, 61),
    "cream": (245, 235, 215),
    "charcoal": (38, 35, 33),
    "navy": (30, 50, 90),
    "olive": (107, 110, 64),
    "rust": (176, 80, 40),
    "avocado": (120, 140, 70),
    "warm gold amber": (212, 175, 55),
    "earthy clay tan": (190, 150, 110),
    "imperial deep-red": (139, 30, 30),
}


def _font(paths, size):
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _parse_color(color_str):
    """Parse a color from hex string, name, or RGB tuple."""
    if isinstance(color_str, (tuple, list)):
        return tuple(color_str[:3])
    if isinstance(color_str, str):
        s = color_str.strip().lower()
        if s in BG_COLORS:
            return BG_COLORS[s]
        if s.startswith("#"):
            r = int(s[1:3], 16)
            g = int(s[3:5], 16)
            b = int(s[5:7], 16)
            return (r, g, b)
    return (200, 54, 45)  # default red


def _halftone_overlay(img, dot_size=6, opacity=40):
    """Add halftone dot pattern overlay (newspaper print texture)."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for y in range(0, h, dot_size * 2):
        for x in range(0, w, dot_size * 2):
            # Offset every other row for proper halftone pattern
            ox = x + (dot_size if (y // (dot_size * 2)) % 2 else 0)
            # Draw a small dot
            r = max(1, dot_size // 3)
            draw.ellipse([ox, y, ox + r * 2, y + r * 2],
                         fill=(0, 0, 0, opacity))

    return Image.alpha_composite(img.convert("RGBA"), overlay)


def _paper_texture(img, intensity=15):
    """Add subtle paper grain/fiber texture."""
    w, h = img.size
    # Generate random noise texture
    noise = Image.new("L", (w, h))
    random.seed(42)
    pixels = noise.load()
    for y in range(h):
        for x in range(w):
            pixels[x, y] = random.randint(128 - intensity, 128 + intensity)

    # Blur slightly for fiber-like texture
    noise = noise.filter(ImageFilter.GaussianBlur(0.5))

    # Blend as soft overlay
    noise_rgba = Image.merge("RGBA", (noise, noise, noise,
                                      Image.new("L", (w, h), intensity * 2)))
    return Image.alpha_composite(img.convert("RGBA"), noise_rgba)


def _torn_edge_vignette(img, border_width=30):
    """Add rough torn-paper edge vignette."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Create rough torn border with random jagged edges
    random.seed(123)
    points_top = [(0, 0)]
    for x in range(0, w, 20):
        jitter = random.randint(-8, 8)
        points_top.append((x, max(0, border_width + jitter)))
    points_top.append((w, 0))

    points_bottom = [(0, h)]
    for x in range(0, w, 20):
        jitter = random.randint(-8, 8)
        points_bottom.append((x, min(h, h - border_width + jitter)))
    points_bottom.append((w, h))

    # Draw dark torn edges
    draw.polygon(points_top, fill=(30, 25, 20, 100))
    draw.polygon(points_bottom, fill=(30, 25, 20, 100))

    # Left and right edges
    points_left = [(0, 0)]
    for y in range(0, h, 20):
        jitter = random.randint(-8, 8)
        points_left.append((max(0, border_width + jitter), y))
    points_left.append((0, h))

    points_right = [(w, 0)]
    for y in range(0, h, 20):
        jitter = random.randint(-8, 8)
        points_right.append((min(w, w - border_width + jitter), y))
    points_right.append((w, h))

    draw.polygon(points_left, fill=(30, 25, 20, 100))
    draw.polygon(points_right, fill=(30, 25, 20, 100))

    return Image.alpha_composite(img.convert("RGBA"), overlay)


def _color_grade(img, bg_color):
    """Bold flat-color background grading + contrast boost."""
    # Convert to RGBA
    img = img.convert("RGBA")
    w, h = img.size

    # Boost saturation and contrast (Vox punch)
    img_rgb = img.convert("RGB")
    img_rgb = ImageEnhance.Color(img_rgb).enhance(1.4)
    img_rgb = ImageEnhance.Contrast(img_rgb).enhance(1.2)
    img_rgb = ImageEnhance.Brightness(img_rgb).enhance(1.05)

    # Create a bold flat background color
    bg = Image.new("RGB", (w, h), bg_color)

    # Blend: keep the subject sharp but push background toward flat color
    # Use a blurred mask to separate subject from background
    gray = img_rgb.convert("L")
    # Enhance the luminance difference for better masking
    gray = ImageEnhance.Contrast(gray).enhance(1.3)

    # Create a mask: bright areas = subject (keep), dark areas = bg (replace)
    mask = gray.point(lambda x: max(0, min(255, (x - 60) * 3)))

    # Blur the mask for smooth transitions
    mask = mask.filter(ImageFilter.GaussianBlur(15))

    # Composite: subject on flat bg
    result = Image.composite(img_rgb, bg, mask)

    return result.convert("RGBA")


def _newspaper_border(img, border_width=4, opacity=160):
    """Add dark newspaper clipping border around the image."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Draw irregular dark border (like a cut-out newspaper clipping)
    draw.rectangle([0, 0, w - 1, border_width], fill=(20, 15, 10, opacity))
    draw.rectangle([0, h - border_width - 1, w - 1, h - 1], fill=(20, 15, 10, opacity))
    draw.rectangle([0, 0, border_width, h - 1], fill=(20, 15, 10, opacity))
    draw.rectangle([w - border_width - 1, 0, w - 1, h - 1], fill=(20, 15, 10, opacity))

    return Image.alpha_composite(img.convert("RGBA"), overlay)


def _headline_overlay(img, headline, bg_color):
    """Add a bold cut-out headline banner at the top."""
    if not headline:
        return img

    w, h = img.size
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Headline size — bold, prominent
    font_size = int(h * 0.09)
    fnt = _font(FONT_BOLD, font_size)

    # Measure text
    text_w = draw.textlength(headline, font=fnt)
    text_h = font_size

    # Banner position — top area
    banner_h = int(text_h * 2.2)
    banner_y = int(h * 0.05)

    # Draw torn-paper banner background (slightly off-white/cream)
    banner_color = (245, 235, 215, 240)
    # Add slight irregularity to banner edges
    random.seed(hash(headline) % 1000)
    points = [(0, banner_y)]
    for x in range(0, w + 20, 30):
        j = random.randint(-5, 5)
        points.append((x, banner_y + j))
    points.append((w, banner_y))
    points.append((w, banner_y + banner_h))
    for x in range(w, -20, -30):
        j = random.randint(-5, 5)
        points.append((x, banner_y + banner_h + j))
    points.append((0, banner_y + banner_h))
    draw.polygon(points, fill=banner_color)

    # Draw headline text — dark ink color
    text_x = (w - text_w) // 2
    text_y = banner_y + (banner_h - text_h) // 2

    # Shadow
    draw.text((text_x + 2, text_y + 3), headline, font=fnt,
              fill=(0, 0, 0, 120))
    # Main text
    ink = (30, 25, 20, 255)
    draw.text((text_x, text_y), headline, font=fnt, fill=ink)

    return Image.alpha_composite(img, overlay)


def _tape_corners(img):
    """Add washi tape pieces at corners for collage feel."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    tape_w = int(w * 0.08)
    tape_h = int(h * 0.04)
    tape_color = (220, 200, 160, 100)  # translucent washi tape

    # Top-left corner
    draw.polygon([(0, 0), (tape_w, 0), (tape_w - 15, tape_h), (-15, tape_h)],
                 fill=tape_color)
    # Top-right corner
    draw.polygon([(w, 0), (w - tape_w, 0), (w - tape_w + 15, tape_h), (w + 15, tape_h)],
                 fill=tape_color)
    # Bottom-left
    draw.polygon([(0, h), (tape_w, h), (tape_w - 15, h - tape_h), (-15, h - tape_h)],
                 fill=tape_color)
    # Bottom-right
    draw.polygon([(w, h), (w - tape_w, h), (w - tape_w + 15, h - tape_h), (w + 15, h - tape_h)],
                 fill=tape_color)

    return Image.alpha_composite(img.convert("RGBA"), overlay)


def apply_collage_effect(src_path, dest_path, bg_color="red", headline="",
                         halftone=True, paper_texture=True, torn_edges=True,
                         newspaper_border=True, tape=True, color_grade=True):
    """Apply the full Vox-style paper collage post-processing pipeline.

    Args:
        src_path: Source image (any format)
        dest_path: Output image (JPG)
        bg_color: Background color (hex string like "#C8362D" or name like "red")
        headline: Optional headline text to overlay
        halftone: Add halftone dot texture
        paper_texture: Add paper grain
        torn_edges: Add torn paper edge vignette
        newspaper_border: Add dark newspaper clipping border
        tape: Add washi tape at corners
        color_grade: Apply bold flat-color background grading

    Returns:
        dest_path
    """
    img = Image.open(src_path).convert("RGBA")
    bg = _parse_color(bg_color)

    # 1. Color grade — bold flat background + saturation/contrast boost
    if color_grade:
        img = _color_grade(img, bg)

    # 2. Halftone dots
    if halftone:
        img = _halftone_overlay(img, dot_size=7, opacity=35)

    # 3. Paper texture
    if paper_texture:
        img = _paper_texture(img, intensity=12)

    # 4. Torn edge vignette
    if torn_edges:
        img = _torn_edge_vignette(img, border_width=25)

    # 5. Newspaper clipping border
    if newspaper_border:
        img = _newspaper_border(img, border_width=3, opacity=140)

    # 6. Washi tape corners
    if tape:
        img = _tape_corners(img)

    # 7. Headline overlay
    if headline:
        img = _headline_overlay(img, headline, bg)

    # Save as JPEG
    img.convert("RGB").save(dest_path, "JPEG", quality=92)
    return dest_path
