#!/usr/bin/env python3
"""
Free Provider — a zero-cost backend for vox-director.

Replaces every Atlas Cloud API call with free alternatives:
  - Images:  Pollinations.AI (GET https://image.pollinations.ai/prompt/...), model "flux"/"sana"
  - Motion:  Agnes AI agnes-video-v2.0 (image-to-video, real AI animation) — free, needs API key.
             Falls back to local ffmpeg zoompan (Ken Burns) if no Agnes API key is set.
  - TTS:     edge-tts (Microsoft Edge neural voices, no key, pip install edge-tts)
  - Music:   ffmpeg-generated ambient pad + simple beat (procedural, no API)
  - Upload:  Pollinations serves images by URL directly; for local files we use a
             simple data URL or re-generate. No upload endpoint needed for the free path.
  - Download: curl (same as Atlas Cloud)

All generation is synchronous from the caller's perspective: submit_* returns a
"job id" that get_status() polls.  For the free provider the "job" is either an
HTTP URL we wait on (image) or a local subprocess we run (motion/TTS/music).
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

# Make sibling modules importable when this file is run from anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
UA = "vox-director-free/0.1"

# ---- Pollinations "sana" is the current default model; flux also works when available
DEFAULT_IMAGE_MODEL = "sana"

# ---- Agnes AI (free video generation, image-to-video)
AGNES_API_BASE = "https://apihub.agnes-ai.com"
AGNES_VIDEO_MODEL = "agnes-video-v2.0"

# Aspect ratio -> pixel dims (kept at 1024 on the long edge for free tier speed)
ASPECT_DIMS = {
    "16:9": (1024, 576),
    "9:16": (576, 1024),
    "1:1":  (768, 768),
    "3:4":  (768, 1024),
    "4:3":  (1024, 768),
}

# ---- Multi-key Agnes loader (supports key rotation for parallel generation)
_agnes_keys = []
_agnes_key_lock = threading.Lock()
_agnes_key_idx = 0


def _load_agnes_keys():
    """Load all Agnes API keys from env + file.  Returns a list of keys."""
    global _agnes_keys
    if _agnes_keys:
        return _agnes_keys
    keys = []
    # 1. env AGNES_API_KEY (single key or comma-separated)
    env_key = os.environ.get("AGNES_API_KEY", "").strip()
    if env_key:
        keys.extend(k.strip() for k in env_key.split(",") if k.strip())
    # 2. file .agnes_keys (one key per line — multi-key file)
    keyfile = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", ".agnes_keys")
    try:
        with open(keyfile) as f:
            for line in f:
                k = line.strip()
                if k and k not in keys:
                    keys.append(k)
    except (IOError, OSError):
        pass
    # 3. fallback: .agnes_key (single-key file, old format)
    if not keys:
        old_keyfile = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", ".agnes_key")
        try:
            with open(old_keyfile) as f:
                k = f.read().strip()
                if k:
                    keys.append(k)
        except (IOError, OSError):
            pass
    _agnes_keys = keys
    return keys


def _agnes_next_key():
    """Get next Agnes API key via round-robin (thread-safe)."""
    global _agnes_key_idx
    keys = _load_agnes_keys()
    if not keys:
        return ""
    with _agnes_key_lock:
        key = keys[_agnes_key_idx % len(keys)]
        _agnes_key_idx += 1
    return key


def _agnes_has_keys():
    """Check if any Agnes API keys are available."""
    return bool(_load_agnes_keys())


class FreeProviderError(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers

def _curl(url, dest, timeout=120, retries=3):
    """Download via curl (urllib breaks on some CDNs)."""
    for attempt in range(retries):
        r = subprocess.run(
            ["/usr/bin/curl", "-sL", "--retry", str(retries),
             "-m", str(timeout), "-o", dest, url],
            capture_output=True, text=True)
        if os.path.exists(dest) and os.path.getsize(dest) > 100:
            # Verify it's actually an image (not a JSON error response)
            try:
                from PIL import Image
                img = Image.open(dest)
                img.verify()  # verify it's a valid image
                return dest
            except Exception:
                # Not a valid image — Pollinations returned an error JSON
                # On retry, change the seed slightly to get a different result
                if attempt < retries - 1 and "seed=" in url:
                    import random
                    new_seed = random.randint(1, 999999)
                    url = url.split("seed=")[0] + f"seed={new_seed}&nologo=true"
                    print(f"      [pollinations] retry {attempt+1} with new seed (got non-image)")
                os.remove(dest)
                time.sleep(3 * (attempt + 1))
                continue
        time.sleep(2 * (attempt + 1))
    raise FreeProviderError(f"download failed: {url} -> {dest} ({r.stderr[:200]})")


def _agnes_api_key():
    """Return any single Agnes API key (for backwards compat)."""
    keys = _load_agnes_keys()
    return keys[0] if keys else ""


def _agnes_create_task(prompt, image_url, duration=5, width=1024, height=576, key=None):
    """Create an Agnes AI image-to-video task.  Returns video_id.
    If key is None, uses next key from rotation."""
    if key is None:
        key = _agnes_next_key()
    if not key:
        raise FreeProviderError("No Agnes API keys available")

    # Duration -> num_frames (8n+1 rule, frame_rate=24)
    # 5s = 121 frames, 10s = 241 frames, 3s = 81 frames
    frame_rate = 24
    if duration <= 3:
        num_frames = 81
    elif duration <= 5:
        num_frames = 121
    elif duration <= 10:
        num_frames = 241
    else:
        num_frames = 441  # max, ~18s

    payload = {
        "model": AGNES_VIDEO_MODEL,
        "prompt": prompt,
        "image": image_url,
        "width": width,
        "height": height,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{AGNES_API_BASE}/v1/videos",
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    # Handle queue-full by retrying with a different key
    if isinstance(result, dict) and result.get("code") == "video_queue_full":
        return None  # signal queue full
    video_id = result.get("video_id") or result.get("task_id") or result.get("id")
    if not video_id:
        raise FreeProviderError(f"Agnes AI returned no video_id: {result}")
    return video_id


def _agnes_create_task_with_retry(prompt, image_url, duration=5, width=1024, height=576, max_key_tries=8):
    """Create an Agnes task, retrying with different keys if queue is full."""
    keys = _load_agnes_keys()
    tried = 0
    last_err = None
    for _ in range(max_key_tries):
        key = _agnes_next_key()
        if not key:
            break
        try:
            vid = _agnes_create_task(prompt, image_url, duration, width, height, key=key)
            if vid:
                return vid, key  # return video_id AND the key used
            # queue full — try next key
            tried += 1
            last_err = "queue full"
            time.sleep(1)
        except Exception as e:
            tried += 1
            last_err = str(e)
            time.sleep(1)
    raise FreeProviderError(f"Agnes AI create failed after {tried} key tries: {last_err}")


def _agnes_get_result(video_id, key=None):
    """Poll Agnes AI task. Returns (status, url) where url is the video URL or None."""
    if key is None:
        key = _agnes_api_key()
    req = urllib.request.Request(
        f"{AGNES_API_BASE}/agnesapi?video_id={urllib.parse.quote(video_id)}",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    status = result.get("status", "unknown")
    if status == "completed":
        url = result.get("url") or (result.get("metadata") or {}).get("url")
        return status, url
    if status == "failed":
        err = result.get("error") or result.get("metadata", {}).get("error") or "unknown"
        return "failed", str(err)
    return status, None


def _ffprobe_dur(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ the provider

class FreeProvider:
    """Drop-in replacement for the Atlas Cloud Provider interface.

    Implements the same methods that provider.py's Provider ABC defines:
    submit_image, submit_video, submit_audio, remove_bg, get_status, upload, download.
    """

    name = "free"

    # ---- image generation (Pollinations) ----

    def submit_image(self, model, prompt, **params):
        """Returns a job_id that get_status() polls.  We encode the full request
        as a JSON blob so get_status can reconstruct the URL."""
        aspect = params.get("aspect_ratio", "16:9")
        w, h = ASPECT_DIMS.get(aspect, ASPECT_DIMS["16:9"])
        model_name = model.split("/")[-1] if "/" in model else model
        if model_name not in ("flux", "turbo", "sana", "stable-diffusion"):
            model_name = DEFAULT_IMAGE_MODEL
        # Generate a unique seed per prompt (hash of prompt + random) so
        # different scenes get different images even when submitted together.
        import hashlib
        prompt_hash = int(hashlib.md5(prompt.encode()).hexdigest()[:8], 16)
        seed = params.get("seed", prompt_hash + int(time.time()) % 1000)
        encoded = urllib.parse.quote(prompt, safe="")
        url = (f"{POLLINATIONS_BASE}/{encoded}"
               f"?width={w}&height={h}&model={model_name}&seed={seed}&nologo=true")
        job = {"type": "image", "url": url, "public_url": url, "started": time.time()}
        return json.dumps(job)

    # ---- video generation (local ffmpeg zoompan) ----

    def submit_video(self, model, prompt, **params):
        """Create a motion clip.  If Agnes AI API key is available, use real AI
        image-to-video animation.  Otherwise fall back to ffmpeg zoompan."""
        image_url = params.get("image")
        duration = int(params.get("duration", 5))
        aspect = params.get("aspect_ratio", params.get("ratio", "16:9"))
        w, h = ASPECT_DIMS.get(aspect, ASPECT_DIMS["16:9"])

        use_agnes = _agnes_has_keys()

        # Determine zoompan direction (used as fallback and for prompt enrichment)
        prompt_lower = (prompt or "").lower()
        if "pull" in prompt_lower or "pull_out" in prompt_lower or "pull-out" in prompt_lower:
            zoom_mode = "out"
        elif "pan" in prompt_lower and "horizontal" in prompt_lower:
            zoom_mode = "pan_x"
        elif "tilt" in prompt_lower or "vertical" in prompt_lower:
            zoom_mode = "pan_y"
        elif "static" in prompt_lower or "locked" in prompt_lower:
            zoom_mode = "static"
        else:
            zoom_mode = "in"  # default push-in

        job = {
            "type": "video",
            "image_url": image_url,
            "duration": duration,
            "aspect": aspect,
            "zoom_mode": zoom_mode,
            "use_agnes": use_agnes,
            "video_prompt": prompt,
            "width": w,
            "height": h,
            # Agnes task state (filled in by get_status on first call)
            "agnes_video_id": None,
            "agnes_key": None,
            "agnes_submitted": False,
            "started": time.time(),
        }
        return json.dumps(job)

    # ---- audio (TTS via edge-tts, music via ffmpeg synth) ----

    def submit_audio(self, model, **params):
        """Two modes: TTS narration (edge-tts) or background music (ffmpeg synth)."""
        if "text" in params:
            # --- TTS narration ---
            text = params["text"]
            language = params.get("language", "en")
            voice = params.get("voice_id", "")
            edge_voice = _resolve_edge_voice(voice, language)
            job = {
                "type": "tts",
                "text": text,
                "edge_voice": edge_voice,
                "speed": params.get("speed", 1.0),
                "started": time.time(),
            }
        elif "prompt" in params:
            # --- BGM: generate a simple ambient pad ---
            job = {
                "type": "music",
                "prompt": params.get("prompt", "ambient"),
                "started": time.time(),
            }
        else:
            job = {"type": "unknown", "started": time.time()}
        return json.dumps(job)

    # ---- background removal (not supported in free tier) ----

    def remove_bg(self, model, image_url, **params):
        raise FreeProviderError("remove_bg not available in free provider")

    # ---- status polling ----

    def get_status(self, job_id):
        """Execute the 'job' and return {status, output, error}.

        For the free provider, jobs are lazy — we run them synchronously the
        first time get_status is called (run_jobs polls with poll_s delay, so
        there's one sleep before execution starts, then it completes)."""
        job = json.loads(job_id)
        jtype = job["type"]

        if jtype == "image":
            # Download the Pollinations image to a local path, but return the
            # public Pollinations URL as output — so keyframes.py stores the
            # public URL in keyframe_url (which Agnes AI needs for video).
            # The download() call in keyframes.py will curl it to keyframe_path.
            tmp = "/tmp/free_img_%d.jpg" % int(time.time() * 1000)
            try:
                _curl(job["url"], tmp, timeout=90, retries=4)
                # Return the PUBLIC url (not local path) so keyframe_url is
                # a public URL that Agnes AI can use for image-to-video.
                return {"status": "completed", "output": job["url"], "error": None}
            except Exception as e:
                return {"status": "failed", "output": None, "error": str(e)}

        elif jtype == "video":
            tmp_out = "/tmp/free_vid_%d.mp4" % int(time.time() * 1000)
            try:
                if job.get("use_agnes"):
                    # --- Agnes AI image-to-video (two-phase: submit then poll) ---
                    # Phase 1: submit task (only once)
                    if not job.get("agnes_submitted"):
                        img_url = job["image_url"]
                        if os.path.exists(img_url):
                            # local path — need public URL
                            public_url = job.get("public_url", "")
                            if not public_url:
                                return {"status": "failed", "output": None,
                                        "error": "Agnes AI needs a public image URL but only local path available"}
                            img_url = public_url
                        try:
                            video_id, agnes_key = _agnes_create_task_with_retry(
                                prompt=job.get("video_prompt", "gentle cinematic motion"),
                                image_url=img_url,
                                duration=job["duration"],
                                width=job.get("width", 1024),
                                height=job.get("height", 576),
                            )
                            job["agnes_video_id"] = video_id
                            job["agnes_key"] = agnes_key
                            job["agnes_submitted"] = True
                            print(f"      [agnes] submitted task {video_id[:30]}... with key ...{agnes_key[-6:]}")
                            return {"status": "pending", "output": None, "error": None,
                                    "_job_update": json.dumps(job)}
                        except Exception as e:
                            # If Agnes fails, fall back to ffmpeg
                            print(f"      [agnes] submit failed, using ffmpeg: {e}")
                            job["use_agnes"] = False

                    # Phase 2: poll existing task
                    if job.get("agnes_video_id"):
                        status, vurl = _agnes_get_result(
                            job["agnes_video_id"], key=job.get("agnes_key"))
                        if status == "completed" and vurl:
                            _curl(vurl, tmp_out, timeout=120)
                            return {"status": "completed", "output": tmp_out, "error": None}
                        if status == "failed":
                            # fall back to ffmpeg
                            print(f"      [agnes] task failed ({vurl}), using ffmpeg")
                            job["use_agnes"] = False
                        else:
                            return {"status": "pending", "output": None, "error": None,
                                    "_job_update": json.dumps(job)}

                    # --- ffmpeg zoompan fallback ---
                    if not job.get("use_agnes"):
                        tmp_img = "/tmp/free_vid_src_%d.jpg" % int(time.time() * 1000)
                        img_url = job["image_url"]
                        if os.path.exists(img_url):
                            subprocess.run(["cp", img_url, tmp_img], check=True)
                        else:
                            _curl(img_url, tmp_img, timeout=90)
                        _make_motion_clip(
                            tmp_img, tmp_out,
                            duration=job["duration"],
                            aspect=job.get("aspect", "16:9"),
                            zoom_mode=job.get("zoom_mode", "in"),
                        )
                        return {"status": "completed", "output": tmp_out, "error": None}

                else:
                    # --- ffmpeg zoompan (no Agnes keys) ---
                    tmp_img = "/tmp/free_vid_src_%d.jpg" % int(time.time() * 1000)
                    img_url = job["image_url"]
                    if os.path.exists(img_url):
                        subprocess.run(["cp", img_url, tmp_img], check=True)
                    else:
                        _curl(img_url, tmp_img, timeout=90)
                    _make_motion_clip(
                        tmp_img, tmp_out,
                        duration=job["duration"],
                        aspect=job.get("aspect", "16:9"),
                        zoom_mode=job.get("zoom_mode", "in"),
                    )
                    return {"status": "completed", "output": tmp_out, "error": None}
            except Exception as e:
                return {"status": "failed", "output": None, "error": str(e)}

        elif jtype == "tts":
            tmp_out = "/tmp/free_tts_%d.mp3" % int(time.time() * 1000)
            try:
                _edge_tts(job["text"], job["edge_voice"], tmp_out,
                          rate=job.get("speed", 1.0))
                return {"status": "completed", "output": tmp_out, "error": None}
            except Exception as e:
                return {"status": "failed", "output": None, "error": str(e)}

        elif jtype == "music":
            tmp_out = "/tmp/free_bgm_%d.mp3" % int(time.time() * 1000)
            try:
                _make_bgm(tmp_out, duration=60)
                return {"status": "completed", "output": tmp_out, "error": None}
            except Exception as e:
                return {"status": "failed", "output": None, "error": str(e)}

        return {"status": "failed", "output": None, "error": f"unknown job type {jtype}"}

    # ---- upload / download ----

    def upload(self, path):
        """For the free provider, 'upload' is a no-op — we return the local path.
        The scripts that call upload() (clips.py for user-provided keyframes)
        store the URL and later download it.  We handle this by returning a
        file:// URL that download() can read."""
        return f"file://{os.path.abspath(path)}"

    def download(self, url, dest):
        if url.startswith("file://"):
            src = url[7:]
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            subprocess.run(["cp", src, dest], check=True)
            return dest
        # local path (get_status returns /tmp/... paths)
        if os.path.exists(url):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            subprocess.run(["cp", url, dest], check=True)
            return dest
        # http(s) URL — use curl
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return _curl(url, dest, timeout=120)


# ------------------------------------------------------------------ motion

def _make_motion_clip(src_img, dest_mp4, duration=5, aspect="16:9", zoom_mode="in"):
    """Create a Ken Burns motion clip from a still image using ffmpeg zoompan.

    zoom_mode: 'in' (push-in), 'out' (pull-out), 'pan_x', 'pan_y', 'static'
    """
    # Target resolution
    if "9" in aspect.split(":")[0]:   # portrait
        w, h = 720, 1280
    else:
        w, h = 1280, 720

    fps = 25
    total_frames = duration * fps

    # Build the zoompan expression
    if zoom_mode == "in":
        zexpr = f"min(zoom+0.0012,{1.3})"
        xexpr = "iw/2-(iw/zoom/2)"
        yexpr = "ih/2-(ih/zoom/2)"
    elif zoom_mode == "out":
        zexpr = f"max({1.3}-(0.0012*on),1.0)"
        xexpr = "iw/2-(iw/zoom/2)"
        yexpr = "ih/2-(ih/zoom/2)"
    elif zoom_mode == "pan_x":
        zexpr = "1.15"
        xexpr = f"(iw-iw/{zexpr})*(on/{total_frames})"
        yexpr = "ih/2-(ih/zoom/2)"
    elif zoom_mode == "pan_y":
        zexpr = "1.15"
        xexpr = "iw/2-(iw/zoom/2)"
        yexpr = f"(ih-ih/{zexpr})*(on/{total_frames})"
    else:  # static
        zexpr = "1.0"
        xexpr = "iw/2-(iw/zoom/2)"
        yexpr = "ih/2-(ih/zoom/2)"

    vf = (
        f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
        f"crop={w*2}:{h*2},"
        f"zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}'"
        f":d={total_frames}:s={w}x{h}:fps={fps}"
    )

    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", src_img,
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "fast", "-q:v", "3",
        "-movflags", "+faststart",
        dest_mp4,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FreeProviderError(f"ffmpeg motion failed: {r.stderr[-300:]}")
    return dest_mp4


# ------------------------------------------------------------------ TTS

def _edge_tts(text, voice, dest, rate=1.0):
    """Generate TTS via edge-tts CLI."""
    rate_str = f"+{int((rate-1)*100)}%" if rate > 1 else f"{int((rate-1)*100)}%" if rate < 1 else "+0%"
    cmd = ["edge-tts", "--text", text, "--voice", voice,
           "--rate", rate_str, "--write-media", dest]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dest):
        raise FreeProviderError(f"edge-tts failed: {r.stderr[:300]}")
    return dest


# Map xai/tts voice_ids to edge-tts voices
_EDGE_VOICE_MAP = {
    # male voices
    "leo":    "en-US-AndrewNeural",
    "max":    "en-US-DavisNeural",
    "leo2":   "en-GB-RyanNeural",
    # female voices
    "lily":   "en-US-AriaNeural",
    "emma":   "en-US-AnaNeural",
    "mia":    "en-US-MichelleNeural",
    # multilingual
    "alex":   "en-US-AndrewMultilingualNeural",
}

_LANG_VOICE_DEFAULTS = {
    "en": "en-US-AndrewNeural",
    "zh": "zh-CN-YunxiNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "es": "es-ES-AlvaroNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "hi": "hi-IN-MadhurNeural",
    "ur": "ur-PK-AsadNeural",
    "ar": "ar-SA-HamedNeural",
}


def _resolve_edge_voice(voice_id, language):
    """Map an xai/tts voice_id to an edge-tts voice name."""
    if voice_id and voice_id in _EDGE_VOICE_MAP:
        return _EDGE_VOICE_MAP[voice_id]
    # If voice_id looks like an edge voice already, use it
    if voice_id and "-" in voice_id and "Neural" in voice_id:
        return voice_id
    # Fallback: language-based default
    return _LANG_VOICE_DEFAULTS.get(language, "en-US-AndrewNeural")


# ------------------------------------------------------------------ BGM

def _make_bgm(dest, duration=60):
    """Generate a richer ambient background music track with ffmpeg.

    Creates a multi-layer ambient bed:
    - Low drone (C2) for warmth
    - Mid pad (G3 + C4) for body
    - Subtle high shimmer (E5) for air
    - Gentle rhythmic pulse (tremolo) for movement
    All layered, low-passed, and faded.  Free, no API.
    """
    d = duration
    # Multi-layer synth pad with tremolo for subtle movement
    filter_complex = (
        # Layer 1: deep drone
        f"sine=frequency=65.41:duration={d}[drone];"
        # Layer 2: warm pad (C3 + G3)
        f"sine=frequency=130.81:duration={d}[pad1];"
        f"sine=frequency=196.00:duration={d}[pad2];"
        # Layer 3: high air (E5, very quiet)
        f"sine=frequency=659.25:duration={d}[air];"
        # Mix pad layers
        f"[pad1][pad2]amix=inputs=2:duration=longest[padmix];"
        # Add tremolo for subtle movement
        f"[padmix]tremolo=f=0.5:d=0.3[padtrem];"
        # Air layer — very quiet, low-passed
        f"[air]volume=0.05,lowpass=f=2000[airq];"
        # Final mix: drone + pad + air
        f"[drone]volume=0.4[dq];"
        f"[padtrem]volume=0.25[pq];"
        f"[dq][pq][airq]amix=inputs=3:duration=longest,"
        # Master processing
        f"lowpass=f=1200,"
        f"afade=t=in:st=0:d=3,"
        f"afade=t=out:st={max(d-4,0)}:d=4,"
        f"volume=0.8"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", filter_complex,
        "-ac", "2", "-c:a", "libmp3lame", "-q:a", "5",
        dest,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # Fallback: simpler 2-layer pad
        cmd2 = [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"sine=frequency=130.81:duration={d},"
            f"amix=inputs=2:duration=longest[a1];"
            f"sine=frequency=196.00:duration={d}[a2];"
            f"[a1][a2]amix=inputs=2:duration=longest,volume=0.3,"
            f"lowpass=f=800,afade=t=in:st=0:d=2,"
            f"afade=t=out:st={max(d-3,0)}:d=3",
            "-ac", "2", "-c:a", "libmp3lame", "-q:a", "5", dest,
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        if r2.returncode != 0:
            raise FreeProviderError(f"ffmpeg bgm failed: {r2.stderr[-300:]}")
    return dest
