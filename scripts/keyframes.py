#!/usr/bin/env python3
"""
Keyframe stage: one styled keyframe per SHOT.

Each beat holds one or more shots (different framings of the same narration
beat) so the cut has variety. Falls back to one implicit shot per beat if a
beat has no "shots". Generates concurrently, downloads to
<project>/keyframes/kf_<beat><shot>.jpg, records url+path onto each shot.

Usage: python3 keyframes.py <project_dir>   (default: out/tang-30s)
"""
import json
import os
import sys

from provider import get_provider, run_jobs
from styles import compose_keyframe_prompt, compose_collage_prompt, resolve_theme, image_params
import collage_post

IMAGE_MODEL = "google/nano-banana-2/text-to-image"


def shots_of(beat):
    """Yield (shot_dict, shot_key) for a beat; synthesize one shot if none."""
    if beat.get("shots"):
        for s in beat["shots"]:
            yield s, f"{beat['id']}{s.get('id','')}"
    else:
        yield beat, f"{beat['id']}"   # beat acts as its own single shot


def run(project_dir):
    bpath = os.path.join(project_dir, "beats.json")
    with open(bpath) as f:
        doc = json.load(f)
    aspect = doc.get("aspect", "16:9")
    img_model = doc.get("image_model", IMAGE_MODEL)   # default nano-banana-2; e.g. openai/gpt-image-2/text-to-image
    img_res = doc.get("image_resolution", "1k")       # 1k (default) | 2k | 4k
    style = doc.get("style", "painterly")
    theme = resolve_theme(doc.get("theme")) or {}   # theme preset -> full look bundle
    collage_style = theme.get("idiom") or doc.get("collage_style", "american-retro")
    # a registered theme wins; a custom (unregistered) theme may set these at doc level
    t_palette = theme.get("palette") or doc.get("palette")
    t_type = theme.get("type_style") or doc.get("type_style")
    t_finish = theme.get("finish") or doc.get("finish")
    era = doc.get("era")            # only needed for the painterly (per-dynasty) style
    kf_dir = os.path.join(project_dir, "keyframes")
    os.makedirs(kf_dir, exist_ok=True)

    prov = get_provider(doc.get("provider"))
    specs, by_key = {}, {}
    for beat in doc["beats"]:
        for shot, key in shots_of(beat):
            if shot.get("keyframe_url"):        # already generated (e.g. reused wide) -> skip
                continue
            scene = shot["scene"]
            if style == "collage":
                prompt = compose_collage_prompt(scene, beat["title_cn"], beat["title_en"],
                                                beat.get("bg", "warm ochre"), aspect,
                                                with_title=shot.get("title", True),
                                                style=collage_style, palette=t_palette,
                                                type_style=t_type, finish=t_finish)
            else:
                prompt = compose_keyframe_prompt(era, scene, beat["title_cn"],
                                                 beat["title_en"], aspect)
            shot["keyframe_prompt"] = prompt
            specs[key] = (lambda p=prompt: prov.submit_image(img_model, p,
                                                             **image_params(img_model, aspect, img_res)))
            by_key[key] = shot

    # Scale deadline by shot count — long videos (80+ beats = 160+ shots) need much more time
    n_specs = len(specs)
    deadline = 300 if n_specs <= 20 else 600 if n_specs <= 40 else 1200 if n_specs <= 80 else 2400
    done = run_jobs(prov, specs, poll_s=3, stall_s=75, max_retries=3, deadline_s=deadline)

    # Whether to apply collage post-processing (default: True for free provider)
    use_collage_post = doc.get("provider", "free") == "free"
    if doc.get("collage_post") is not None:
        use_collage_post = doc.get("collage_post")

    for key, url in done.items():
        if not url:
            continue
        dest = os.path.join(kf_dir, f"kf_{key}.jpg")
        prov.download(url, dest)
        shot = by_key[key]
        shot["keyframe_url"] = url
        shot["keyframe_path"] = dest

        # Crop to target aspect ratio (Agnes AI returns 1024x1024 squares)
        if "agnes-ai" in url or "platform-outputs.agnes" in url:
            try:
                from PIL import Image
                from free_provider import ASPECT_DIMS, _crop_to_aspect
                tw, th = ASPECT_DIMS.get(aspect, ASPECT_DIMS["16:9"])
                _crop_to_aspect(dest, dest, tw, th)
            except Exception as e:
                print(f"[{key}] aspect crop skipped: {e}")

        # Apply collage post-processing for Vox-style look (free provider)
        if use_collage_post:
            beat = shot if "title_en" in shot else next(
                (b for b in doc["beats"]
                 if any(s.get("id") == shot.get("id") for s in (b.get("shots") or [b]))),
                None)
            if beat is None:
                beat = shot
            bg = beat.get("bg", "warm ochre") if isinstance(beat, dict) else "warm ochre"
            headline = beat.get("title_en", "") if shot.get("title", True) else ""
            # Stick-figure style: lighter post-processing — keep clean flat look,
            # skip heavy halftone/torn edges that would muddy the simple line art.
            theme_name = doc.get("theme", "")
            is_stick = theme_name == "stick-figure" or (
                isinstance(theme, dict) and theme.get("idiom") == "stick-figure")
            try:
                if is_stick:
                    collage_post.apply_collage_effect(dest, dest, bg_color=bg, headline=headline,
                                                     halftone=False, torn_edges=False,
                                                     newspaper_border=False, tape=True,
                                                     color_grade=True, paper_texture=False)
                else:
                    collage_post.apply_collage_effect(dest, dest, bg_color=bg, headline=headline)
                print(f"[{key}] saved {dest} (collage post-processed)")
            except Exception as e:
                print(f"[{key}] saved {dest} (collage post FAILED: {e})")
        else:
            print(f"[{key}] saved {dest}")

    with open(bpath, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("updated", bpath)


if __name__ == "__main__":
    proj = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "out", "tang-30s")
    run(os.path.abspath(proj))
