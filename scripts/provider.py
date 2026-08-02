#!/usr/bin/env python3
"""
Provider abstraction — the pluggable media backend the pipeline stages talk to.

Atlas Cloud is the default and, for now, the only backend. Stages call a Provider
(submit_image/video/audio, remove_bg, get_status, upload, download) instead of a
concrete client, so adding a backend is: subclass Provider + one registry entry.
Pick a backend per project with beats.json `{"provider": "atlas_cloud"}` (default).

The layer is a thin in-process wrapper — zero extra network hops, so it does NOT
slow the pipeline; the only cost is the API latency, which is unchanged.
"""
import time
from abc import ABC, abstractmethod

import atlas_cloud

# Free provider (Pollinations images + ffmpeg motion + edge-tts) — zero cost
try:
    import free_provider as _fp
    _HAS_FREE = True
except Exception:
    _HAS_FREE = False


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    """The surface the stages need. get_status normalizes every backend's polling
    response to {status: pending|completed|failed, output: <url|None>, error}."""
    name = "base"

    @abstractmethod
    def submit_image(self, model, prompt, **params): ...
    @abstractmethod
    def submit_video(self, model, prompt, **params): ...
    @abstractmethod
    def submit_audio(self, model, **params): ...
    @abstractmethod
    def remove_bg(self, model, image_url, **params): ...
    @abstractmethod
    def get_status(self, job_id): ...
    @abstractmethod
    def upload(self, path): ...
    @abstractmethod
    def download(self, url, dest): ...


class AtlasCloudProvider(Provider):
    """Wraps the atlas_cloud client — identical behavior to calling it directly."""
    name = "atlas_cloud"

    def submit_image(self, model, prompt, **params):
        return atlas_cloud.submit_image(model, prompt, **params)

    def submit_video(self, model, prompt, **params):
        return atlas_cloud.submit_video(model, prompt, **params)

    def submit_audio(self, model, **params):
        return atlas_cloud.submit_media(model, **params)

    def remove_bg(self, model, image_url, **params):
        body = {"model": model, "image": image_url, **params}
        return atlas_cloud._post("/model/generateImage", body)["data"]["id"]

    def get_status(self, job_id):
        try:
            d = atlas_cloud._get(f"/model/prediction/{job_id}").get("data", {})
        except atlas_cloud.AtlasCloudError as e:
            return {"status": "failed", "output": None, "error": str(e)}
        st = d.get("status")
        if st in ("completed", "succeeded"):
            out = d.get("outputs") or d.get("output")
            out = out[0] if isinstance(out, list) else out
            return {"status": "completed", "output": out, "error": None}
        if st == "failed":
            return {"status": "failed", "output": None, "error": d.get("error", "")}
        return {"status": "pending", "output": None, "error": None}

    def upload(self, path):
        return atlas_cloud.upload(path)

    def download(self, url, dest):
        return atlas_cloud.download(url, dest)


_REGISTRY = {"atlas_cloud": AtlasCloudProvider}
if _HAS_FREE:
    _REGISTRY["free"] = _fp.FreeProvider


def get_provider(name=None):
    """Return a Provider instance by name (default 'atlas_cloud')."""
    name = (name or "atlas_cloud").lower()
    if name not in _REGISTRY:
        raise ProviderError(f"unknown provider '{name}'; available: {list(_REGISTRY)}")
    return _REGISTRY[name]()


def run_jobs(prov, specs, *, poll_s=3, stall_s=90, max_retries=2, deadline_s=900):
    """Submit + poll a batch of jobs, resubmitting any that FAIL or STALL.

    specs: dict of key -> submit() callable returning a job id. A job that fails,
    or stays pending past `stall_s`, is resubmitted (fresh id) up to `max_retries`
    times — this is what stops one stuck prediction from wasting the whole deadline.
    Returns key -> output URL (or None). Prints progress like the old loops did.

    Supports _job_update: if get_status returns a "_job_update" key, the job id
    is replaced with the updated value (for two-phase providers like Agnes AI
    that submit a task on first poll then poll it on subsequent calls).

    For the free provider, image jobs are synchronous (download on first poll).
    To speed this up, we use a thread pool to poll multiple jobs in parallel.
    """
    import concurrent.futures

    st = {}
    for key, submit in specs.items():
        st[key] = {"pid": submit(), "t": time.time(), "tries": 0}
        print(f"[{key}] submitted {st[key]['pid'][:80]}...")

    done = {}
    deadline = time.time() + deadline_s

    # Use a thread pool to poll jobs in parallel (greatly speeds up free provider
    # where each image download takes ~30s sequentially).
    def poll_one(key_submit):
        key, submit = key_submit
        if key in done:
            return key, "skip", None
        s = st[key]
        try:
            r = prov.get_status(s["pid"])
        except Exception as e:
            return key, "error", str(e)
        status = r["status"]
        if r.get("_job_update"):
            s["pid"] = r["_job_update"]
            s["t"] = time.time()
        if status == "completed":
            return key, "completed", r["output"]
        elif status == "failed":
            return key, "failed", r.get("error", "")
        elif status == "pending" and time.time() - s["t"] > stall_s:
            return key, "stalled", None
        return key, "pending", None

    while len(done) < len(specs) and time.time() < deadline:
        time.sleep(poll_s)
        # Poll all pending jobs in parallel
        pending_keys = [(k, v) for k, v in specs.items() if k not in done]
        if not pending_keys:
            break

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(pending_keys), 8)) as ex:
            futures = {ex.submit(poll_one, ks): ks[0] for ks in pending_keys}
            for fut in concurrent.futures.as_completed(futures):
                key = futures[fut]
                try:
                    k, status, output = fut.result()
                except Exception as e:
                    print(f"[{key}] poll error: {e}")
                    continue
                if k in done:
                    continue
                if status == "completed":
                    done[k] = output
                    print(f"[{k}] done")
                elif status == "failed" or status == "stalled":
                    s = st[k]
                    if s["tries"] < max_retries:
                        s["tries"] += 1
                        s["pid"] = specs[k]()
                        s["t"] = time.time()
                        why = "failed" if status == "failed" else f"stalled>{int(stall_s)}s"
                        print(f"[{k}] {why} -> resubmit #{s['tries']}")
                    elif status == "failed":
                        done[k] = None
                        print(f"[{k}] FAILED: {(output or '')[:120]}")
                elif status == "error":
                    s = st[k]
                    if s["tries"] < max_retries:
                        s["tries"] += 1
                        s["pid"] = specs[k]()
                        s["t"] = time.time()
                        print(f"[{k}] poll error -> resubmit #{s['tries']}: {output[:80]}")
                    else:
                        done[k] = None
                        print(f"[{k}] FAILED (poll errors): {output[:120]}")

    for key in specs:
        done.setdefault(key, None)
    return done
