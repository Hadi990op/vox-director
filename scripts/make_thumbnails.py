#!/usr/bin/env python3
"""Make two YouTube thumbnail variations for the Kings' Food video."""
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

OUT = "/opt/baal-agent/workspace/vox-director/out/kings-food"
TW, TH = 1280, 720

FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_NARROW_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf"

def load_font(path, size):
    return ImageFont.truetype(path, size)

def thick_outline(draw, pos, text, font, fill, outline_color=(0,0,0), width=5):
    x, y = pos
    for dx in range(-width, width+1):
        for dy in range(-width, width+1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x+dx, y+dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)

def draw_gradient_left(img, strength=0.88):
    overlay = Image.new("RGBA", img.size, (0,0,0,0))
    d = ImageDraw.Draw(overlay)
    w, h = img.size
    for x in range(w):
        alpha = int(strength * 255 * (1 - x/w) ** 1.8)
        d.line([(x, 0), (x, h)], fill=(0,0,0,alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay)

def draw_gradient_right(img, strength=0.88):
    overlay = Image.new("RGBA", img.size, (0,0,0,0))
    d = ImageDraw.Draw(overlay)
    w, h = img.size
    for x in range(w):
        alpha = int(strength * 255 * (x/w) ** 1.8)
        d.line([(x, 0), (x, h)], fill=(0,0,0,alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay)

def draw_gradient_bottom(img, strength=0.5):
    overlay = Image.new("RGBA", img.size, (0,0,0,0))
    d = ImageDraw.Draw(overlay)
    w, h = img.size
    for y in range(h):
        alpha = int(strength * 255 * (y/h) ** 1.5)
        d.line([(0, y), (w, y)], fill=(0,0,0,alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay)

# ============================================================
# THUMBNAIL 1: "HE DRANK CURRENCY"
# Aztec emperor frame — left text, right image visible
# ============================================================
def make_thumbnail_1():
    src = Image.open(f"{OUT}/raw_aztec.jpg").convert("RGB")
    src = src.resize((TW, TH), Image.LANCZOS)
    src = ImageEnhance.Color(src).enhance(1.45)
    src = ImageEnhance.Contrast(src).enhance(1.3)
    src = ImageEnhance.Brightness(src).enhance(0.85)

    canvas = src.convert("RGBA")
    canvas = draw_gradient_left(canvas, 0.92)
    canvas = draw_gradient_bottom(canvas, 0.35)

    draw = ImageDraw.Draw(canvas)

    # Top small label
    font_label = load_font(FONT_BOLD, 34)
    thick_outline(draw, (60, 60), "THE AZTEC EMPEROR", font_label, (255, 220, 80), width=3)

    # "HE DRANK" — white
    font_top = load_font(FONT_BOLD, 88)
    thick_outline(draw, (50, 110), "HE DRANK", font_top, (255, 255, 255), width=6)

    # "CURRENCY" — gold, huge
    font_big = load_font(FONT_BOLD, 128)
    thick_outline(draw, (45, 200), "CURRENCY", font_big, (255, 215, 60), width=7)

    # Red bar
    draw.rectangle([(50, 340), (300, 348)], fill=(220, 40, 40))

    # Subtitle
    font_sub = load_font(FONT_NARROW_BOLD, 42)
    thick_outline(draw, (50, 365), "The king who ate", font_sub, (255, 255, 255), width=3)
    thick_outline(draw, (50, 415), "entire empires", font_sub, (255, 100, 100), width=3)

    # Bottom badge
    font_badge = load_font(FONT_BOLD, 26)
    badge = "100,000 YEAR HISTORY"
    bbox = draw.textbbox((0,0), badge, font=font_badge)
    bw = bbox[2]-bbox[0]+40
    bh = bbox[3]-bbox[1]+18
    bx, by = 50, TH-85
    draw.rounded_rectangle([(bx,by),(bx+bw,by+bh)], radius=8, fill=(190, 25, 25))
    draw.text((bx+20, by+5), badge, font=font_badge, fill=(255,255,255))

    final = canvas.convert("RGB")
    final.save(f"{OUT}/thumb_1_drunk_currency.jpg", quality=95)
    print("Thumb 1 done")

# ============================================================
# THUMBNAIL 2: "5000 CALORIES A DAY"
# Henry VIII frame — right text, left image visible
# ============================================================
def make_thumbnail_2():
    src = Image.open(f"{OUT}/raw_henry.jpg").convert("RGB")
    src = src.resize((TW, TH), Image.LANCZOS)
    src = ImageEnhance.Color(src).enhance(1.35)
    src = ImageEnhance.Contrast(src).enhance(1.25)
    src = ImageEnhance.Brightness(src).enhance(0.88)

    canvas = src.convert("RGBA")
    canvas = draw_gradient_right(canvas, 0.92)
    canvas = draw_gradient_bottom(canvas, 0.35)

    draw = ImageDraw.Draw(canvas)

    # "5000" — gold, massive
    font_num = load_font(FONT_BOLD, 180)
    thick_outline(draw, (TW-460, 60), "5000", font_num, (255, 220, 50), width=8)

    # "CALORIES A DAY" — white
    font_cal = load_font(FONT_BOLD, 58)
    thick_outline(draw, (TW-460, 255), "CALORIES", font_cal, (255, 255, 255), width=4)
    thick_outline(draw, (TW-460, 320), "A DAY", font_cal, (255, 255, 255), width=4)

    # Red bar
    draw.rectangle([(TW-460, 395), (TW-220, 403)], fill=(220, 40, 40))

    # "He ate himself to death"
    font_sub = load_font(FONT_NARROW_BOLD, 44)
    thick_outline(draw, (TW-460, 420), "He ate himself", font_sub, (255, 255, 255), width=3)
    thick_outline(draw, (TW-460, 475), "to DEATH", font_sub, (255, 80, 80), width=3)

    # Bottom-left badge
    font_badge = load_font(FONT_BOLD, 26)
    badge = "KINGS' SECRET FOOD"
    bbox = draw.textbbox((0,0), badge, font=font_badge)
    bw = bbox[2]-bbox[0]+40
    bh = bbox[3]-bbox[1]+18
    bx, by = 50, TH-85
    draw.rounded_rectangle([(bx,by),(bx+bw,by+bh)], radius=8, fill=(190, 25, 25))
    draw.text((bx+20, by+5), badge, font=font_badge, fill=(255,255,255))

    # Top-left duration badge
    font_dur = load_font(FONT_BOLD, 22)
    dur = "8 MIN DOC"
    bbox = draw.textbbox((0,0), dur, font=font_dur)
    dw = bbox[2]-bbox[0]+24
    dh = bbox[3]-bbox[1]+14
    draw.rounded_rectangle([(50,55),(50+dw,55+dh)], radius=6, fill=(30,30,30))
    draw.text((62, 58), dur, font=font_dur, fill=(255,220,50))

    final = canvas.convert("RGB")
    final.save(f"{OUT}/thumb_2_5000_calories.jpg", quality=95)
    print("Thumb 2 done")

make_thumbnail_1()
make_thumbnail_2()
print("Both done!")
