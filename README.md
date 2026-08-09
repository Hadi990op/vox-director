<p align="right"><b>English</b> · <a href="README.zh.md">简体中文</a></p>

# 🎬 Vox Director

**Turn one topic into a finished Vox-style paper-collage explainer / ad video — script, collage keyframes, motion, voice-over, music and captions, all automated.**

An **agent skill** that runs end to end on the [Atlas Cloud](https://www.atlascloud.ai/?utm_source=github&utm_campaign=vox_director) API + local `ffmpeg`, usable by any coding agent (Claude Code, Codex, etc.). You give it a one-line topic; it gives you an `mp4`.

![License: MIT](https://img.shields.io/badge/License-MIT-black.svg) ![Powered by Atlas Cloud](https://img.shields.io/badge/powered%20by-Atlas%20Cloud-ff5a1f.svg) ![Agent Skill](https://img.shields.io/badge/Agent-Skill-d97757.svg)

<div align="center">

https://github.com/user-attachments/assets/ed08d230-7bcb-4b48-a17d-23c079208f9f

<b>▶ "The evolution of Chinese civilization" · 30s</b>

</div>

<table>
  <tr>
    <td width="33%"><a href="https://github.com/user-attachments/assets/216cd62f-6314-456c-94cf-1090b8559a22"><img src="assets/thumbs/football.jpg" width="100%" alt="How football conquered the world"></a></td>
    <td width="33%"><a href="https://github.com/user-attachments/assets/561788b1-5615-4828-b3f8-b24ae5ad7bcd"><img src="assets/thumbs/mexican.jpg" width="100%" alt="Mexican street food"></a></td>
    <td width="33%"><a href="https://github.com/user-attachments/assets/f69f072f-f50a-41ba-9e66-7ed0aae4ddc0"><img src="assets/thumbs/money.jpg" width="100%" alt="A brief history of money"></a></td>
  </tr>
  <tr>
    <td align="center"><sub>Football history · 60s</sub></td>
    <td align="center"><sub>Mexican street food · 60s</sub></td>
    <td align="center"><sub>A brief history of money · 60s</sub></td>
  </tr>
</table>

<p align="center"><sub><em>▶ more films — click any thumbnail to play</em></sub></p>

---

## What it is

The look is the modern editorial **paper-collage** popularized by Vox explainers: hand-cut paper cut-outs, torn edges, tape, halftone dots, newspaper clippings, bold flat color per beat, big cut-out headlines — brought to life with motion, a narrator, music and captions.

## How it works

One topic flows through one script per stage, all driven by a single `beats.json` per project:

```
topic
  │
  ├─ 1. beat map        pick a narrative arc → write beats.json      ◀── GATE 1: you approve the beat map
  ├─ 2. style bake-off  render the same beat in 3–4 themes           ◀── GATE 2: you pick the look by eye
  ├─ 3. keyframes       one collage poster per beat  (nano-banana-2)
  ├─ 4. motion          animate each poster          (gemini-omni-flash i2v)
  ├─ 5. voice + music   one narrator (xai/tts) + BGM (minimax/music)
  ├─ 6. assemble        ffmpeg: concat, duck music under VO, burn captions + watermark
  └─ final.mp4
```

That flow is **B-roll** — a topic in, everything generated. Two more input modalities reuse the same engine:

- **A-roll — you already have a talking-head video.** It is ASR-segmented into beats and re-styled into the collage look, keeping the real face, lip-sync and gestures frame-for-frame (`gemini-omni-flash/video-edit`, auto-retrying on `seedance-2.0/reference-to-video`).
- **C-roll — you have one still photo** (a selfie, a product shot). The subject is cut out as a photographic sticker — never redrawn — and each beat's poster is generated around it (`nano-banana-2/edit`). The narration can be cloned into the subject's own voice.

Two ideas make or break the result, and the skill is built around both:

1. **The look is born in the image step.** Each beat is a finished collage *poster*. All the collage DNA (torn paper, cut-outs, halftone, headline text) lives in that image — if the poster isn't a rich collage, nothing downstream saves it.
2. **The motion is added after.** By default an AI video model animates the whole poster (the "living poster" path). For dramatic *piece-by-piece* assembly, an optional local keyframe engine cuts the poster into parts and drives them frame-by-frame (no content filters, pixel-exact — great for real people).

Two human decision gates keep you in control (approve the beat map; pick the style); everything else is automated.

## Models (verified on Atlas Cloud)

| Job | Model |
|---|---|
| Keyframe / collage poster | `google/nano-banana-2/text-to-image` |
| Animate (non-real content) | `google/gemini-omni-flash/image-to-video` |
| Animate (**real people / brands**) | `kwaivgi/kling-video-o3-pro/image-to-video` |
| Re-style a talking-head (A-roll) | `google/gemini-omni-flash/video-edit` |
| Anchor a photo in the collage (C-roll) | `google/nano-banana-2/edit` |
| Narration | `xai/tts-v1` |
| Narration in a real person's voice | `bytedance/seed-audio-1.0` (voice cloning) |
| Music | `minimax/music-2.6` |
| Cut out an element (advanced path) | `youchuan/v8.1/remove-background` |

Model IDs drift — the skill fetches the live list from `GET https://api.atlascloud.ai/api/v1/models` before running.

## Quick Start (Standalone — No Coding Agent Needed)

Clone and run the web studio directly:

```bash
git clone https://github.com/Hadi990op/vox-director.git
cd vox-director

# 1. Install system dependencies
sudo apt-get install -y ffmpeg

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Add Agnes AI API keys (free keys from https://platform.agnes-ai.com/)
#    One key per line, comments with #
echo "your-agnes-key-1" > .agnes_keys
echo "your-agnes-key-2" >> .agnes_keys
chmod 600 .agnes_keys

# 4. (Optional) Set up YouTube upload — requires Google OAuth credentials
#    Create a Web Application credential at https://console.cloud.google.com/
#    Set redirect URI to: http://localhost:9200/api/yt/callback
#    Save as client_secret.json in the project root

# 5. Start the studio
python3 studio.py
# Open http://localhost:9200 in your browser
```

Then just type a topic in the studio UI, pick duration/theme, and click **Generate**. The pipeline runs automatically: script → keyframes → clips → voice-over → assemble → final.mp4.

## Autonomous Mode (4 videos/week, fully hands-off)

```bash
# Generate 4 unique ideas from competitor research (dry run)
python3 scripts/weekly_batch.py --dry-run --count 4

# Full batch: research → ideas → scripts → videos → YouTube upload
python3 scripts/weekly_batch.py --count 4 --skip-research

# Single autonomous video
python3 scripts/auto_runner.py
```

Requirements for autonomous mode:
- `.agnes_keys` file with 1+ Agnes AI keys
- `client_secret.json` + `.youtube_token.json` for YouTube uploads (run OAuth flow first via the studio UI)
- `out/competitor_research.json` (run `python3 scripts/competitor_watcher.py` to generate, or the batch will do it automatically)

## Requirements

- **Python 3.9+** with pip
- **ffmpeg** + **ffprobe** (`apt-get install ffmpeg` or `brew install ffmpeg`)
- **Agnes AI** API key(s) — free at [platform.agnes-ai.com](https://platform.agnes-ai.com/) — used for script generation, image generation, and video motion
- **yt-dlp** — installed automatically with `pip install -r requirements.txt` (for competitor research)
- **Google OAuth credentials** (optional) — only needed for YouTube upload. Create a Web Application OAuth client at [console.cloud.google.com](https://console.cloud.google.com/), set the redirect URI to `http://localhost:9200/api/yt/callback`, and save as `client_secret.json`

## What's in the box

```
SKILL.md              the skill (English) — the workflow the agent follows
SKILL.zh.md           the same skill in Chinese
AGENTS.md             entry point for non-Claude agents (Codex, …)
references/           the creative engine
  prompt-guide.md       the LOOK layer — prompt structures, vocab & 9 theme presets
  beat-layer.md         14 narrative arcs + hook/pacing + shot patterns
  voices.md             xai/tts voice roster — pick a voice_id per language/tone
  models-and-gotchas.md every API / ffmpeg gotcha, already solved
  local-engine.md       the advanced element-level motion engine
scripts/              one script per pipeline stage
examples/             ready-to-run beats.json examples
assets/               the showcase film
```

## Credits

Inspired by the collage-ad workflows of **[Stav Zilber](https://x.com/StavZilber)**, **[rom1trs](https://x.com/rom1trs)** and **[Higgsfield](https://x.com/higgsfield_ai)**, and by **[Vox](https://www.vox.com)**'s explainer visual language.

Built end to end on **[Atlas Cloud](https://www.atlascloud.ai/?utm_source=github&utm_campaign=vox_director)** — one prompt, one film.

## License

[MIT](LICENSE) © 2026 Atlas Cloud
