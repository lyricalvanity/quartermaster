"""Quartermaster's art queue: image jobs that wait for the card.

An agent or the owner queues a batch of prompts; nothing runs then. When the batch op runs, the
agent parks whatever model holds the card, runs every queued batch in ComfyUI, and puts the model back.
One swap per batch run instead of one per image. Maya's GLM keeps its conversation on disk across the
unload (MAYA_GLM_SLOT_KEEP=1), so the agent's next turn reads only its new tokens.

A batch, as submitted (POST /api/art/submit, or `qm art submit`):

    {"name": "game-props", "out_dir": "D:\\projects\\game\\art\\raw", "workflow": "qwen-image",
     "skip_existing": true,
     "jobs": [{"name": "crate", "prompt": "...", "w": 1024, "h": 1024, "steps": 6, "seed": 1, "n": 1, "neg": "..."}]}

A job writes <out_dir>/<name>.png, or <name>_<k>.png when n > 1. Seeds step by 7919 per copy.

What a batch cannot do: name a ComfyUI graph (workflows are the builders below, chosen by name), write
outside the inventory's art.out_roots or under anything in art.deny, or name a file outside its out_dir.

Written 2026-11-05 (Faber). Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
NEG = "blurry, lowres, watermark, text, signature, jpeg artifacts, deformed"


# ---- workflows: the only graphs the queue sends ----------------------------------

def qwen_image(job: dict, models: dict, refs: list[str] | None = None) -> tuple[dict, str]:
    """Qwen-Image 2512 fp8 + the Lightning 4-step LoRA (as gen.py). Returns (graph, id of the output node)."""
    m = {"unet": "qwen_image_2512_fp8_e4m3fn.safetensors", "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
         "vae": "qwen_image_vae.safetensors", "lora": "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors",
         "shift": 3.1, **(models or {})}
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "5": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": float(m["shift"])}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": job["prompt"]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": job.get("neg") or NEG}},
        "8": {"class_type": "EmptySD3LatentImage", "inputs": {"width": job["w"], "height": job["h"], "batch_size": 1}},
        "9": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "positive": ["6", 0], "negative": ["7", 0],
                                                   "latent_image": ["8", 0], "seed": job["seed"], "steps": job["steps"],
                                                   "cfg": job.get("cfg", 1.0), "sampler_name": "euler",
                                                   "scheduler": "simple", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "PreviewImage", "inputs": {"images": ["10", 0]}},
    }
    if m.get("lora"):
        g["4"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": m["lora"], "strength_model": 1.0}}
    else:
        g["5"]["inputs"]["model"] = ["1", 0]
    return g, "11"


TURBO_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"


def qwen_image_21(job: dict, models: dict, refs: list[str] | None = None) -> tuple[dict, str]:
    """Qwen-Image-2.1 Turbo as ComfyUI's official template (image_qwen_image_2_1_turbo_t2i, v0.44): its 8-step
    sigma schedule is baked into the checkpoint, so `steps` is ignored. With refs (names ComfyUI's /upload/image
    gave back) the same graph edits / composes from up to 10 reference images; the encoder then needs the VAE.
    The template's Qwen 3.5 prompt enhancer is off by default there and not used here."""
    m = {"unet": "qwen_image_2.1_turbo_int8_convrot.safetensors", "clip": "qwen3vl_8b_int8_convrot.safetensors",
         "vae": "qwen_image_2.1_vae_bf16.safetensors", "sigmas": TURBO_SIGMAS, **(models or {})}
    enc = {"clip": ["2", 0], "prompt": job["prompt"], "negative_prompt": job.get("neg") or "",
           "resolution": int(job.get("ref_resolution", 1024))}
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "4": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "5": {"class_type": "TextEncodeQwenImage21", "inputs": enc},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": job["w"], "height": job["h"], "batch_size": 1}},
        "7": {"class_type": "ManualSigmas", "inputs": {"sigmas": m["sigmas"]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": job["seed"]}},
        "10": {"class_type": "CFGGuider", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["5", 1],
                                                     "cfg": job.get("cfg", 1.0)}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["10", 0], "sampler": ["8", 0],
                                                                 "sigmas": ["7", 0], "latent_image": ["6", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "PreviewImage", "inputs": {"images": ["12", 0]}},
    }
    for i, name in enumerate(refs or []):
        nid = str(20 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"images.image_{i + 1}"] = [nid, 0]
    if refs:
        enc["vae"] = ["3", 0]
    return g, "13"


HERE = Path(__file__).resolve().parent


def flux3_klein(job: dict, models: dict, refs: list[str] | None = None) -> tuple[dict, str]:
    """FLUX 3 Image Klein, distilled checkpoint, as ComfyUI's templates (image_flux3_klein_distilled_t2i and
    _image_edit, v0.44) and BFL's own (flux3_klein_bf16_t2i / _mi2i in the -comfy repo): KSampler cfg 1, 16 steps,
    euler, simple; upsampler sampling as BFL's (top_k 1, repetition 1.0, presence 1.5 for text, 0 with references).
    BFL's code says not to run Klein without its prompt upsampler (Qwen 3.5 9B); it is on unless the job says
    "upsample": false, with the templates' system prompts (flux3_upsampler_*.txt, copied from them) and
    sampling. The caption it writes is node "caption" (PreviewAny), saved beside the image by the agent.
    With refs: ReferenceMultiLatent (1 megapixel each), negative zeroed; the latent is the job's w x h unless
    "keep_ref_size" (an edit of image 1 at its own size)."""
    m = {"unet": "flux3-image-klein-bf16.safetensors", "clip": "qwen3vl_4b_bf16.safetensors",
         "vae": "flux2-vae.safetensors", "upsampler": "qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors", **(models or {})}
    edit = bool(refs)
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "flux3", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": job["w"], "height": job["h"], "batch_size": 1}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},
    }
    text = job["prompt"]
    for i, name in enumerate(refs or []):
        g[str(30 + i)] = {"class_type": "LoadImage", "inputs": {"image": name}}
    if job.get("upsample", True):
        sp = (HERE / ("flux3_upsampler_edit.txt" if edit else "flux3_upsampler_t2i.txt")).read_text(encoding="utf-8")
        tg = {"clip": ["12", 0], "prompt": job["prompt"], "system_prompt": sp, "max_length": 3584,
              "sampling_mode": "on", "sampling_mode.temperature": 1.0, "sampling_mode.top_k": 1,
              "sampling_mode.top_p": 0.95, "sampling_mode.min_p": 0.0, "sampling_mode.repetition_penalty": 1.0,
              "sampling_mode.seed": 0, "sampling_mode.presence_penalty": 0.0 if edit else 1.5,
              "thinking": False, "use_default_template": True, "mtp": "off"}
        g["12"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": m["upsampler"], "type": "flux3", "device": "default"}}
        g["11"] = {"class_type": "TextGenerate", "inputs": tg}
        g["caption"] = {"class_type": "PreviewAny", "inputs": {"source": ["11", 0]}}
        if edit:
            g["50"] = {"class_type": "BatchImagesNode",
                       "inputs": {f"images.image{i}": [str(30 + i), 0] for i in range(len(refs))}}
            tg["image"] = ["50", 0]
        text = ["11", 0]
    g["4"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": text}}
    if edit:
        g["32"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}}
        rml = {"positive": ["4", 0], "negative": ["32", 0], "vae": ["3", 0], "resolution": "resize",
               "resolution.megapixels": float(job.get("ref_megapixels", 1.0))}
        for i in range(len(refs)):
            rml[f"images.image{i}"] = [str(30 + i), 0]
        g["17"] = {"class_type": "ReferenceMultiLatent", "inputs": rml}
        pos, neg = ["17", 0], ["17", 1]
        latent = ["17", 2] if job.get("keep_ref_size") else ["6", 0]
    else:
        g["5"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": job.get("neg") or ""}}
        pos, neg, latent = ["4", 0], ["5", 0], ["6", 0]
    g["7"] = {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": pos, "negative": neg, "latent_image": latent,
                                                   "seed": job["seed"], "steps": job["steps"],
                                                   "cfg": job.get("cfg", 1.0), "sampler_name": "euler",
                                                   "scheduler": "simple", "denoise": 1.0}}
    return g, "9"


WORKFLOWS = {"qwen-image": qwen_image, "qwen-image-2.1": qwen_image_21, "flux3-klein": flux3_klein}
DEFAULTS = {"qwen-image": {"w": 1024, "h": 1024, "steps": 6, "cfg": 1.0},
            "qwen-image-2.1": {"w": 1024, "h": 1024, "steps": 8, "cfg": 1.0},
            "flux3-klein": {"w": 1024, "h": 1024, "steps": 16, "cfg": 1.0}}
TAKES_REFS = {"qwen-image-2.1", "flux3-klein"}
REF_EXT = (".png", ".jpg", ".jpeg", ".webp")


# ---- validation ----------------------------------------------------------------------

class Invalid(ValueError):
    pass


def _int(v, lo, hi, what):
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise Invalid(f"{what} must be an integer") from None
    if not lo <= i <= hi:
        raise Invalid(f"{what} must be between {lo} and {hi}")
    return i


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:                       # different drives
        return False


def check_out_dir(out_dir, roots: list[str], deny: list[str]) -> str:
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise Invalid("out_dir is required: the folder the images go to")
    if not os.path.isabs(out_dir) or out_dir.startswith("\\\\"):
        raise Invalid("out_dir must be an absolute local path")
    real = os.path.realpath(out_dir)
    for d in deny:
        if _under(real, os.path.realpath(d)):
            raise Invalid(f"out_dir may not be under {d}")
    if not any(_under(real, os.path.realpath(r)) for r in roots):
        raise Invalid(f"out_dir must be under one of the inventory's art.out_roots: {', '.join(roots) or '(none set)'}")
    return real


def validate(body, cfg: dict, deny: list[str]) -> dict:
    if isinstance(body, list):
        raise Invalid("send a batch object with out_dir and jobs; a bare job list needs --out")
    if not isinstance(body, dict):
        raise Invalid("send a JSON object")
    name = str(body.get("name") or "batch")
    if not BATCH_RE.match(name):
        raise Invalid("name: letters, digits, '.', '_' and '-', up to 60")
    wf = str(body.get("workflow") or "qwen-image")
    if wf not in WORKFLOWS:
        raise Invalid(f"workflow must be one of {sorted(WORKFLOWS)}")
    out = check_out_dir(body.get("out_dir"), cfg.get("out_roots") or [], deny)
    jobs = body.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise Invalid("jobs must be a non-empty list")
    d = DEFAULTS[wf]
    clean, names, total = [], set(), 0
    for i, j in enumerate(jobs):
        if not isinstance(j, dict):
            raise Invalid(f"job {i} is not an object")
        jn = str(j.get("name") or "")
        if not NAME_RE.match(jn):
            raise Invalid(f"job {i}: name must be letters, digits, '_' or '-' (it becomes the file name)")
        if jn in names:
            raise Invalid(f"job {i}: name {jn} is used twice")
        names.add(jn)
        prompt = j.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
            raise Invalid(f"job {jn}: prompt must be text, up to 6000 characters")
        neg = j.get("neg")
        if neg is not None and (not isinstance(neg, str) or len(neg) > 3000):
            raise Invalid(f"job {jn}: neg must be text, up to 3000 characters")
        w = _int(j.get("w", d["w"]), 256, 2048, f"job {jn}: w")
        h = _int(j.get("h", d["h"]), 256, 2048, f"job {jn}: h")
        if w % 16 or h % 16:
            raise Invalid(f"job {jn}: w and h must be multiples of 16")
        n = _int(j.get("n", 1), 1, 16, f"job {jn}: n")
        c = {"name": jn, "prompt": prompt, "w": w, "h": h, "n": n,
             "steps": _int(j.get("steps", d["steps"]), 1, 60, f"job {jn}: steps"),
             "seed": _int(j.get("seed", 1), 0, 2**48, f"job {jn}: seed")}
        if neg is not None:
            c["neg"] = neg
        refs = j.get("refs")
        if refs is not None:
            if wf not in TAKES_REFS:
                raise Invalid(f"job {jn}: refs need a workflow that takes them ({', '.join(sorted(TAKES_REFS))})")
            if not isinstance(refs, list) or not 1 <= len(refs) <= 10:
                raise Invalid(f"job {jn}: refs must be a list of 1 to 10 image paths")
            clean_refs = []
            for r in refs:
                if not isinstance(r, str) or not r.lower().endswith(REF_EXT):
                    raise Invalid(f"job {jn}: a ref must be a .png, .jpg or .webp path")
                try:
                    clean_refs.append(check_out_dir(r, cfg.get("out_roots") or [], deny))
                except Invalid as e:
                    raise Invalid(f"job {jn}: ref {r}: {str(e).replace('out_dir', 'it')}") from None
            c["refs"] = clean_refs
            c["ref_resolution"] = _int(j.get("ref_resolution", 1024), 0, 2048, f"job {jn}: ref_resolution")
            if j.get("keep_ref_size"):
                c["keep_ref_size"] = True
        if "steps" in j:
            c["steps_set"] = True
        if "upsample" in j:
            if wf != "flux3-klein":
                raise Invalid(f"job {jn}: upsample is a flux3-klein option")
            c["upsample"] = bool(j["upsample"])
        if "cfg" in j:
            try:
                c["cfg"] = float(j["cfg"])
            except (TypeError, ValueError):
                raise Invalid(f"job {jn}: cfg must be a number") from None
            if not 0.0 <= c["cfg"] <= 20.0:
                raise Invalid(f"job {jn}: cfg must be between 0 and 20")
        clean.append(c)
        total += n
    cap = int(cfg.get("max_images", 400))
    if total > cap:
        raise Invalid(f"{total} images in one batch; the inventory's art.max_images is {cap}")
    return {"name": name, "workflow": wf, "out_dir": out, "skip_existing": bool(body.get("skip_existing", True)),
            "jobs": clean, "images": total}


def outputs(batch: dict) -> list[tuple[dict, int, Path]]:
    """Every image a batch will write: (job, copy index, path)."""
    out = []
    for j in batch["jobs"]:
        for k in range(j["n"]):
            fn = f"{j['name']}.png" if j["n"] == 1 else f"{j['name']}_{k}.png"
            out.append((j, k, Path(batch["out_dir"]) / fn))
    return out


# ---- the queue on disk ---------------------------------------------------------------

class ArtQueue:
    """queue/ holds the batches waiting, oldest first by file name; done/ and failed/ the ones that ran,
    each with its results. A batch file is written whole to a temp name and renamed, so a reader never sees
    half of one."""

    def __init__(self, root: Path):
        self.root = root
        for d in ("queue", "done", "failed"):
            (root / d).mkdir(parents=True, exist_ok=True)

    def submit(self, batch: dict, by: str) -> dict:
        bid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        rec = dict(batch, id=bid, by=by, submitted=time.time(), status="queued")
        tmp = self.root / "queue" / f".{bid}.tmp"
        tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        os.replace(tmp, self.root / "queue" / f"{bid}.json")
        return rec

    def _read(self, d: str) -> list[dict]:
        out = []
        for f in sorted((self.root / d).glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def pending(self) -> list[dict]:
        return self._read("queue")

    def get(self, bid: str) -> dict | None:
        p = self.root / "queue" / f"{bid}.json"
        if not BATCH_RE.match(bid) or not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def finish(self, batch: dict, status: str, results: dict) -> None:
        """status: done | failed | removed. Moves the batch out of queue/ with its results attached."""
        rec = dict(batch, status=status, finished=time.time(), results=results)
        dest = self.root / ("done" if status == "done" else "failed") / f"{batch['id']}.json"
        tmp = dest.with_name(f".{batch['id']}.tmp")
        tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        os.replace(tmp, dest)
        try:
            (self.root / "queue" / f"{batch['id']}.json").unlink()
        except FileNotFoundError:
            pass

    def save_progress(self, batch: dict, results: dict) -> None:
        """A batch that stopped part way (cancel, an engine came back) stays queued with what it wrote so far."""
        rec = dict(batch, results=results)
        p = self.root / "queue" / f"{batch['id']}.json"
        tmp = p.with_name(f".{batch['id']}.tmp")
        tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    @staticmethod
    def brief(b: dict) -> dict:
        r = b.get("results") or {}
        return {k: b.get(k) for k in ("id", "name", "workflow", "out_dir", "images", "by", "submitted", "status", "finished")} | \
               {"written": r.get("written"), "skipped": r.get("skipped"), "failed": r.get("failed"),
                "seconds": r.get("seconds"), "error": r.get("error")}

    def summary(self) -> dict:
        q = self.pending()
        recent = sorted(self._read("done") + self._read("failed"), key=lambda b: b.get("finished") or 0)[-8:]
        return {"batches": len(q), "images": sum(int(b.get("images") or 0) for b in q),
                "queued": [self.brief(b) for b in q], "recent": [self.brief(b) for b in recent[::-1]]}


# ---- ComfyUI client ------------------------------------------------------------------

def _call(url, method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = raw[:300]
        raise RuntimeError(f"ComfyUI {path} -> {e.code}: {json.dumps(msg)[:400]}") from None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def upload_ref(url: str, path: str) -> str:
    """Put a reference image into ComfyUI's input folder (subfolder quartermaster-artq) and return the name a
    LoadImage node takes. The file is read here, so ComfyUI never opens a path from a batch."""
    p = Path(path)
    data = p.read_bytes()
    boundary = "----qmartq" + uuid.uuid4().hex
    fname = f"{uuid.uuid4().hex[:8]}-{p.name}"
    parts = []
    for k, v in (("subfolder", "quartermaster-artq"), ("type", "input"), ("overwrite", "true")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{fname}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(url + "/upload/image", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read())
    return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]


def generate(url: str, graph: dict, out_node: str, timeout: float, check=lambda: None) -> tuple[bytes, float]:
    """Queue one graph, wait for it, return the PNG bytes of its first image. check() may raise to abandon the
    wait (the job is then interrupted so it does not keep the card)."""
    r = _call(url, "POST", "/prompt", {"prompt": graph, "client_id": "quartermaster-artq"})
    pid = r["prompt_id"]
    t0 = time.time()
    while True:
        time.sleep(0.7)
        try:
            check()
        except BaseException:
            try:
                _call(url, "POST", "/interrupt", {}, timeout=10)
                _call(url, "POST", "/queue", {"delete": [pid]}, timeout=10)
            except Exception:  # noqa: BLE001
                pass
            raise
        hist = _call(url, "GET", f"/history/{pid}", timeout=20)
        h = hist.get(pid) if isinstance(hist, dict) else None
        if h:
            s = h.get("status") or {}
            if s.get("status_str") == "error":
                msgs = [m for m in s.get("messages", []) if m and m[0] == "execution_error"]
                detail = (msgs[0][1].get("exception_message") if msgs else None) or json.dumps(s)[:300]
                raise RuntimeError(f"ComfyUI error: {str(detail).strip()[:300]}")
            if s.get("completed"):
                break
        if time.time() - t0 > timeout:
            try:
                _call(url, "POST", "/interrupt", {}, timeout=10)
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"timed out after {int(timeout)} s")
    imgs = (h.get("outputs") or {}).get(out_node, {}).get("images") or []
    if not imgs:
        raise RuntimeError("the job finished without an image")
    im = imgs[0]
    q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "temp")})
    data = _call(url, "GET", "/view?" + q, timeout=60)
    if not isinstance(data, (bytes, bytearray)) or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("ComfyUI's /view did not return a PNG")
    cap = (h.get("outputs") or {}).get("caption") or {}
    caption = cap.get("text") or cap.get("string")
    if isinstance(caption, list):
        caption = "\n".join(str(x) for x in caption)
    return bytes(data), time.time() - t0, caption
