#!/usr/bin/env python3
"""
SFX (Sound Effects) module for vox-director — free, ffmpeg-generated.

Generates short sound effects that play at beat transitions and during
key moments, adding the punch and texture that makes Vox-style videos
feel professional. All sounds are synthesized with ffmpeg — no API, no
downloads, no cost.

Sound types:
  - whoosh:    paper-swish transition between beats (wind through paper)
  - impact:    hard cut hit on beat starts with strong headlines
  - riser:     rising tension swoosh for hook/payoff beats
  - page:      paper page turn / flip
  - pop:       small pop for element appearances
  - shimmer:   subtle shimmer for triumphant/golden beats

Usage in assemble.py:
  from sfx import generate_sfx, get_sfx_for_beats
  sfx_files = generate_sfx(project_dir)  # generates all SFX to audio/sfx/
  sfx_plan = get_sfx_for_beats(beats)    # returns [{file, start, vol}, ...]
"""
import os
import subprocess

SFX_DIR_NAME = "sfx"


def _ff(args):
    """Run ffmpeg, raise on failure."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def _gen_whoosh(dest, duration=0.6):
    """Paper-swish transition: filtered noise sweep."""
    _ff([
        "-f", "lavfi", "-i",
        f"anoisesrc=d={duration}:c=pink:a=0.5",
        "-af",
        f"highpass=f=800,lowpass=f=4000,"
        f"afade=t=in:st=0:d={duration*0.4},"
        f"afade=t=out:st={duration*0.5}:d={duration*0.5},"
        f"volume=0.6",
        "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4",
        dest,
    ])


def _gen_impact(dest, duration=0.3):
    """Hard cut hit: low-frequency thump + click."""
    _ff([
        "-f", "lavfi", "-i", f"sine=frequency=80:duration={duration}",
        "-f", "lavfi", "-i", f"anoisesrc=d=0.05:c=white:a=0.8",
        "-filter_complex",
        f"[0:a]volume=0.8,afade=t=out:st=0.05:d={duration-0.05}[low];"
        f"[1:a]volume=0.4,highpass=f=2000[click];"
        f"[low][click]amix=inputs=2:duration=first",
        "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4",
        dest,
    ])


def _gen_riser(dest, duration=1.0):
    """Rising tension swoosh: ascending filtered noise."""
    _ff([
        "-f", "lavfi", "-i", f"anoisesrc=d={duration}:c=pink:a=0.4",
        "-af",
        f"highpass=f=500,"
        f"afade=t=in:st=0:d={duration*0.8},"
        f"afade=t=out:st={duration*0.85}:d={duration*0.15},"
        f"volume=0.5",
        "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4",
        dest,
    ])


def _gen_page_turn(dest, duration=0.4):
    """Paper page turn: short filtered noise burst."""
    _ff([
        "-f", "lavfi", "-i", f"anoisesrc=d={duration}:c=white:a=0.3",
        "-af",
        f"bandpass=f=2500:width_type=h:w=2000,"
        f"afade=t=in:st=0:d=0.05,"
        f"afade=t=out:st={duration*0.3}:d={duration*0.7},"
        f"volume=0.4",
        "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4",
        dest,
    ])


def _gen_pop(dest, duration=0.15):
    """Small pop for element appearance: short sine blip."""
    _ff([
        "-f", "lavfi", "-i", f"sine=frequency=600:duration={duration}",
        "-af",
        f"afade=t=out:st=0.02:d={duration-0.02},"
        f"volume=0.4",
        "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4",
        dest,
    ])


def _gen_shimmer(dest, duration=0.8):
    """Subtle shimmer for golden/triumphant beats: high sine sweep."""
    _ff([
        "-f", "lavfi", "-i", f"sine=frequency=1200:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=1800:duration={duration}",
        "-filter_complex",
        f"[0:a]volume=0.2,tremolo=f=8:d=0.5[s1];"
        f"[1:a]volume=0.15,tremolo=f=6:d=0.4[s2];"
        f"[s1][s2]amix=inputs=2:duration=longest,"
        f"afade=t=in:st=0:d=0.1,"
        f"afade=t=out:st={duration*0.6}:d={duration*0.4}",
        "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4",
        dest,
    ])


# Map SFX types to generator functions
_SFX_GENERATORS = {
    "whoosh": _gen_whoosh,
    "impact": _gen_impact,
    "riser": _gen_riser,
    "page": _gen_page_turn,
    "pop": _gen_pop,
    "shimmer": _gen_shimmer,
}


def generate_sfx(project_dir):
    """Generate all SFX files to <project>/audio/sfx/. Returns dict of
    {sfx_type: file_path}."""
    sfx_dir = os.path.join(project_dir, "audio", SFX_DIR_NAME)
    os.makedirs(sfx_dir, exist_ok=True)

    sfx_files = {}
    for sfx_type, gen_func in _SFX_GENERATORS.items():
        dest = os.path.join(sfx_dir, f"{sfx_type}.mp3")
        if not os.path.exists(dest):
            gen_func(dest)
            print(f"[sfx] {sfx_type} -> {dest}")
        sfx_files[sfx_type] = dest

    return sfx_files


def get_sfx_for_beats(beat_spans):
    """Given beat_spans (from assemble.py: [{start, dur, beat}, ...]),
    return a list of SFX cues: [{file, start, vol}, ...].

    Rules:
    - Beat 1: impact (hook hit)
    - Hook beats (hook != 'none' and != first): riser before the beat
    - Every other beat transition: whoosh
    - Feel-based: 'triumphant'/'grand' beats get shimmer
    - Final beat: impact + shimmer (payoff)
    """
    cues = []
    if not beat_spans:
        return cues

    for i, bs in enumerate(beat_spans):
        beat = bs["beat"]
        start = bs["start"]
        hook = beat.get("hook", "none")
        feel = (beat.get("feel", "") or "").lower()

        if i == 0:
            # Opening hook: impact hit
            cues.append({"type": "impact", "start": start, "vol": 0.5})
        elif hook and hook != "none":
            # Hook beat: riser just before the beat start
            riser_start = max(0, start - 0.8)
            cues.append({"type": "riser", "start": riser_start, "vol": 0.3})
        else:
            # Normal transition: whoosh at beat start
            cues.append({"type": "whoosh", "start": start, "vol": 0.25})

        # Feel-based: triumphant/grand beats get shimmer
        if any(w in feel for w in ("triumphant", "grand", "golden", "epic", "glorious")):
            cues.append({"type": "shimmer", "start": start + 0.3, "vol": 0.2})

    # Final beat: impact for payoff
    if len(beat_spans) > 1:
        last = beat_spans[-1]
        cues.append({"type": "impact", "start": last["start"], "vol": 0.4})

    return cues
