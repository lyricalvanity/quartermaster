"""Quartermaster host agent: the one process on the host that may order the inference servers about.

It reads the card, the RAM and every server in inventory.json every couple of seconds, serves that state as
JSON and as a Server-Sent-Events stream, and executes *plans* (make_resident, unload, stop, start, park,
set_context, set_budget, presets) built only from what the inventory says. The UI (served from ../ui by this
process, or from the quartermaster Docker container) sends intents, never commands.

Run:   pythonw.exe quartermaster_agent.py            (from the Startup folder, no console)
       python  quartermaster_agent.py --debug        (console, verbose)
Key:   agent.key next to this file; bearer for the container, same-origin for the agent's own page.
Reach: http://127.0.0.1:8790  (loopback only; the container reaches it as host.docker.internal:8790)

Rules it keeps (SPEC.md, "What it must not do"):
  - It unloads a Strata server only through Strata's own endpoints. "stop" ends a server process and is a
    separate, labelled action that the client must send with confirm=true.
  - It never starts anything that is not in the inventory. Argv come from the file, never from the request.
  - Every mutating call is appended to logs/actions.jsonl.

Written 2026-11-03 (Faber). Stdlib only.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.request
import urllib.error

import artqueue

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UI_DIR = ROOT / "ui"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
ACTIONS_LOG = LOG_DIR / "actions.jsonl"
AGENT_LOG = LOG_DIR / "agent.log"
INVENTORY = HERE / "inventory.json"
KEY = (HERE / "agent.key").read_text(encoding="utf-8").strip()
HOST, PORT = "127.0.0.1", 8790
UI_PORT = 8800                                   # the nginx container's page (docker-compose.yml)
# nginx forwards Host as $host, which drops the port, so the bare names are the container's requests
ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | {f"{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (PORT, UI_PORT)}
ALLOWED_ORIGINS = {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (PORT, UI_PORT)}
NVIDIA_SMI = r"C:\Windows\System32\nvidia-smi.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DEBUG = "--debug" in sys.argv
ENGINE_PROCS = ("strata.exe", "strata-dsv41.exe", "strata-vision.exe", "maya.exe", "maya-vision.exe",
                "ollama.exe", "ollama_llama_server.exe", "ollama-runner.exe")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".woff2": "font/woff2", ".ico": "image/x-icon", ".json": "application/json"}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if DEBUG:
        print(line, flush=True)
    try:
        with AGENT_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---- inventory ---------------------------------------------------------------

class Inventory:
    def __init__(self):
        self.load()

    def load(self):
        data = json.loads(INVENTORY.read_text(encoding="utf-8"))
        self.data = data
        self.servers = {s["id"]: s for s in data["servers"]}
        self.shims = {s["id"]: s for s in data.get("shims", [])}
        self.presets = {p["id"]: p for p in data.get("presets", [])}
        self.gpu = data["gpu"]
        self.policy = data.get("policy", {})
        self.poll_s = float(data.get("poll_s", 2.0))
        self.openwebui = data.get("openwebui")       # optional; absent = no OpenWebUI tile
        self.comfyui = data.get("comfyui")           # optional; absent = no ComfyUI tile
        self.art = data.get("art")                   # optional; absent = no art queue (needs comfyui too)

    def public(self):
        return self.data


INV = Inventory()


# ---- helpers -----------------------------------------------------------------

def run(args, timeout=20, cwd=None):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW, cwd=cwd,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def http(method: str, url: str, body=None, timeout=5.0, headers=None):
    """JSON in, JSON out. Returns (status, parsed-or-text). Never raises for HTTP errors; raises for connection errors."""
    data = None
    h = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        return status, json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def tail_file(path: str, lines: int = 80, exclude: str | None = None) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - 256 * 1024))
            chunk = f.read().decode("utf-8", "replace")
    except OSError as e:
        return [f"(log unreadable: {e})"]
    out = chunk.splitlines()
    if exclude:
        try:
            rx = re.compile(exclude)
            out = [l for l in out if not rx.search(l)]
        except re.error:
            pass
    return out[-lines:]


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram_status():
    m = _MemoryStatusEx()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    gib = 1024 ** 3
    return {"total_gib": round(m.ullTotalPhys / gib, 1), "available_gib": round(m.ullAvailPhys / gib, 1),
            "used_gib": round((m.ullTotalPhys - m.ullAvailPhys) / gib, 1), "used_percent": m.dwMemoryLoad,
            "commit_total_gib": round(m.ullTotalPageFile / gib, 1),
            "commit_used_gib": round((m.ullTotalPageFile - m.ullAvailPageFile) / gib, 1),
            "commit_free_gib": round(m.ullAvailPageFile / gib, 1)}


GPU_FIELDS = ["memory.used", "memory.total", "utilization.gpu", "temperature.gpu", "power.draw", "power.limit",
              "clocks.sm", "fan.speed"]


def gpu_status():
    rc, out, err = run([NVIDIA_SMI, f"--id={INV.gpu['index']}", "--query-gpu=" + ",".join(GPU_FIELDS),
                        "--format=csv,noheader,nounits"], timeout=10)
    if rc != 0 or not out.strip():
        return {"error": (err or out).strip()[:300]}
    vals = [v.strip() for v in out.strip().splitlines()[0].split(",")]
    g = dict(zip(GPU_FIELDS, vals))

    def num(k):
        try:
            return float(g[k])
        except (KeyError, ValueError):
            return None
    return {"used_mib": num("memory.used"), "total_mib": num("memory.total"), "util": num("utilization.gpu"),
            "temp_c": num("temperature.gpu"), "power_w": num("power.draw"), "power_limit_w": num("power.limit"),
            "sm_mhz": num("clocks.sm"), "fan": num("fan.speed")}


def gpu_apps():
    rc, out, _ = run([NVIDIA_SMI, "--query-compute-apps=pid,process_name", "--format=csv,noheader"], timeout=10)
    apps = []
    if rc == 0:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                apps.append({"pid": parts[0], "name": Path(parts[1]).name})
    return apps


def process_list():
    """{name_lower: [pids]} for the engine processes only (one tasklist call)."""
    rc, out, _ = run(["tasklist.exe", "/FO", "CSV", "/NH"], timeout=15)
    found = {}
    if rc == 0:
        for line in out.splitlines():
            if not line.startswith('"'):
                continue
            cols = [c.strip('"') for c in line.split('","')]
            name = cols[0].lower()
            if name in ENGINE_PROCS:
                try:
                    found.setdefault(name, []).append(int(cols[1]))
                except (IndexError, ValueError):
                    pass
    return found


def pid_listening(port: int) -> list[int]:
    rc, out, _ = run(["netstat.exe", "-ano", "-p", "tcp"], timeout=15)
    pids = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return sorted(set(pids))


# ---- state -------------------------------------------------------------------

class State:
    """The poller's view of the machine, rebuilt every poll_s; plans and the UI read it under the lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = {"time": 0, "servers": {}, "gpu": {}, "ram": {}, "ollama": {"models": []}, "warnings": []}
        self.parked: set[str] = set()
        if INV.policy.get("park_8099_default"):
            self.parked.add("orca-q8")
        self.transient: dict[str, str] = {}      # id -> "starting" | "loading" | "unloading" | "stopping" set by plans
        self.history: dict[str, list] = {}       # id -> [(t, tok_s)] for the sparkline
        self.gpu_history: list = []

    def set_transient(self, sid, value):
        with self.lock:
            if value:
                self.transient[sid] = value
            else:
                self.transient.pop(sid, None)

    # -- probes --
    def probe_server(self, s: dict) -> dict:
        port = s["port"]
        base = f"http://127.0.0.1:{port}"
        info = {"id": s["id"], "up": False, "loaded": False, "busy": False, "state": "down", "health": None,
                "live": None, "last_request": None, "tok_s": None, "pid": None}
        kind = s["kind"]
        try:
            if kind == "ollama":
                st, ps = http("GET", base + "/api/ps", timeout=2.0)
                models = (ps or {}).get("models", []) if st == 200 else []
                info.update(up=True, loaded=bool(models), state="loaded" if models else "idle",
                            models=[{"name": m.get("name"), "size_vram_gib": round((m.get("size_vram") or 0) / 2**30, 1),
                                     "size_gib": round((m.get("size") or 0) / 2**30, 1),
                                     "expires_at": m.get("expires_at")} for m in models])
                return info
            st, h = http("GET", base + "/health", timeout=2.0)
            if st != 200 or not isinstance(h, dict):
                return info
            info["up"] = True
            info["health"] = {k: h.get(k) for k in ("status", "model", "max_context", "images", "loaded", "unloaded",
                                                     "version", "model_label") if k in h}
            if kind == "maya":
                info["loaded"] = not h.get("unloaded", False)
                reloading = h.get("status") == "reloading"
            else:
                info["loaded"] = bool(h.get("loaded", True))
                reloading = False
            # live state from /metrics (passive on both engines)
            if info["loaded"]:
                try:
                    st2, m = http("GET", base + "/metrics", timeout=2.5)
                    if st2 == 200 and isinstance(m, dict):
                        live = m.get("live") or {}
                        info["live"] = {k: live.get(k) for k in ("state", "phase", "queued", "generated", "prompt_tokens",
                                                                "prompt_read", "prompt_total", "tok_s", "tok_s_mean",
                                                                "prefill_tok_s_mean", "elapsed_s", "max_tokens") if k in live}
                        reqs = m.get("requests") or []
                        if reqs:
                            r = reqs[-1]
                            info["last_request"] = {k: r.get(k) for k in ("prompt_tokens", "output_tokens", "prompt_ms",
                                                                          "prefill_tok_s", "decode_ms", "decode_tok_s",
                                                                          "finish", "finish_reason", "started", "reused") if k in r}
                        info["recent_requests"] = [{k: r.get(k) for k in ("prompt_tokens", "output_tokens", "prefill_tok_s",
                                                                           "decode_tok_s", "finish_reason", "finish") if k in r}
                                                   for r in reqs[-5:]]
                        eng = m.get("engine") or {}
                        info["engine"] = {k: eng.get(k) for k in ("context", "kv", "vram_slots", "ram_slots", "vram_gb", "ram_gb",
                                                                  "expert_slots", "cpu_threads", "mtp", "kv_gb") if k in eng}
                        tot = m.get("totals") or {}
                        info["totals"] = {k: tot.get(k) for k in ("requests", "prompt_tokens", "output_tokens", "peak_decode") if k in tot}
                        info["busy"] = live.get("state") in ("reading", "generating")
                        info["tok_s"] = live.get("tok_s") if info["busy"] else None
                        # Strata's live comes without a state key when idle: use queued/active
                        if "state" not in live and (live.get("active") or live.get("running")):
                            info["busy"] = True
                except Exception as e:  # noqa: BLE001 - metrics are decoration
                    info["metrics_error"] = str(e)[:120]
            if reloading:
                info["state"] = "reloading"
            elif info["busy"]:
                info["state"] = "busy"
            elif info["loaded"]:
                info["state"] = "ready"
            else:
                info["state"] = "idle"           # server up, model unloaded (the card is free of it)
        except Exception:  # noqa: BLE001 - down
            pass
        return info

    def poll_once(self):
        snap = {"time": time.time(), "servers": {}, "warnings": []}
        try:
            snap["gpu"] = gpu_status()
        except Exception as e:  # noqa: BLE001
            snap["gpu"] = {"error": str(e)}
        try:
            snap["gpu"]["apps"] = [a for a in gpu_apps() if a["name"].lower() in ENGINE_PROCS]
        except Exception:  # noqa: BLE001
            snap["gpu"]["apps"] = []
        snap["ram"] = ram_status()
        try:
            snap["processes"] = process_list()
        except Exception as e:  # noqa: BLE001
            snap["processes"] = {"error": str(e)}
        threads = []
        results = {}

        def one(s):
            results[s["id"]] = self.probe_server(s)
        for s in INV.servers.values():
            t = threading.Thread(target=one, args=(s,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=6)
        for sid, s in INV.servers.items():
            info = results.get(sid) or {"id": sid, "up": False, "loaded": False, "busy": False, "state": "down"}
            tr = self.transient.get(sid)
            if tr:
                info["state"] = tr
            info["parked"] = sid in self.parked
            snap["servers"][sid] = info
            # sparkline history (tok/s while busy, else 0)
            hist = self.history.setdefault(sid, [])
            hist.append([round(snap["time"]), info.get("tok_s") or 0])
            del hist[:-60]
            info["tok_history"] = hist[-60:]
        # shims: port open?
        snap["shims"] = {}
        for sid, sh in INV.shims.items():
            try:
                st, tags = http("GET", f"http://127.0.0.1:{sh['port']}/api/tags", timeout=1.0)
                snap["shims"][sid] = {"up": st == 200}
            except Exception:  # noqa: BLE001
                snap["shims"][sid] = {"up": False}
        # resident
        holders = [sid for sid, i in snap["servers"].items() if i.get("loaded") and INV.servers[sid]["kind"] != "ollama"]
        oll = snap["servers"].get("ollama", {})
        snap["ollama"] = {"models": oll.get("models", []), "up": oll.get("up", False)}
        snap["holders"] = holders
        snap["resident"] = holders[0] if len(holders) == 1 else None
        if len(holders) > 1:
            snap["warnings"].append(f"two engines hold the card: {', '.join(holders)}")
        if snap["ram"]["commit_free_gib"] < INV.data.get("commit_warn_gib", 20):
            snap["warnings"].append(f"commit headroom {snap['ram']['commit_free_gib']} GB (page file growth ahead)")
        for sid in self.parked:
            if snap["servers"].get(sid, {}).get("loaded") and sid not in PLANS.loading_now():
                snap["warnings"].append(f"{INV.servers[sid]['name']} is parked but came back loaded; re-unloading")
        g = snap["gpu"]
        # the card is taken but no server admits to it: Strata says "unloaded" for the whole of a self-triggered load
        comfy = COMFY_WATCH.get()
        snap["comfyui"] = comfy
        comfy_holds = bool(comfy and comfy.get("holding"))
        if comfy and comfy.get("exposed"):
            snap["warnings"].append(f"ComfyUI is listening beyond loopback ({', '.join(comfy['exposed'])}); it has no login")
        if comfy and comfy.get("queue") and holders:
            snap["warnings"].append(f"ComfyUI is generating while {INV.servers[holders[0]]['name']} holds the card")
        unattributed = (not holders and not comfy_holds and not PLANS.loading_now()
                        and (g.get("used_mib") or 0) > INV.gpu["free_card_mib"])
        snap["card_unattributed"] = unattributed
        if unattributed:
            apps = ", ".join(sorted({a["name"] for a in g.get("apps", [])})) or "no engine process listed"
            snap["warnings"].append(f"{(g.get('used_mib') or 0)/1024:.1f} GB on the card with no resident model: {apps} (a server may be loading on a request)")
        self.gpu_history.append([round(snap["time"]), g.get("used_mib"), g.get("util"), g.get("temp_c"), g.get("power_w")])
        del self.gpu_history[:-120]
        snap["gpu_history"] = self.gpu_history[-120:]
        snap["parked"] = sorted(self.parked)
        snap["openwebui"] = OWUI_WATCH.get()
        aq = art_queue()
        if aq:
            try:
                snap["art"] = aq.summary()
            except Exception as e:  # noqa: BLE001 - the queue is decoration for the poll
                snap["art"] = {"error": str(e)[:160]}
        snap["plan"] = PLANS.current_public()
        snap["inventory_rev"] = INV.data.get("rev", 1)
        with self.lock:
            self.snapshot = snap
        return snap

    def get(self):
        with self.lock:
            return self.snapshot

    def loop(self):
        while True:
            t0 = time.time()
            try:
                snap = self.poll_once()
                PLANS.park_watch(snap)
            except Exception:  # noqa: BLE001
                log("poll error: " + traceback.format_exc()[-600:])
            time.sleep(max(0.5, INV.poll_s - (time.time() - t0)))


# ---- plans -------------------------------------------------------------------

class Cancelled(Exception):
    pass


class PlanFailed(Exception):
    pass


class Plan:
    def __init__(self, op, target, args, by):
        self.id = uuid.uuid4().hex[:10]
        self.op, self.target, self.args, self.by = op, target, args or {}, by
        self.status = "queued"
        self.steps: list[dict] = []
        self.started = time.time()
        self.ended = None
        self.error = None
        self.cancel = threading.Event()
        self.loading: set[str] = set()

    def step(self, name, detail=""):
        st = {"name": name, "status": "running", "started": time.time(), "ended": None, "detail": detail, "lines": []}
        self.steps.append(st)
        return st

    def done_step(self, st, detail=None, status="done"):
        st["status"] = status
        st["ended"] = time.time()
        if detail is not None:
            st["detail"] = detail

    def say(self, st, line):
        st["lines"].append(line)
        del st["lines"][:-12]

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def public(self):
        return {"id": self.id, "op": self.op, "target": self.target, "args": self.args, "by": self.by,
                "status": self.status, "steps": self.steps, "started": self.started, "ended": self.ended,
                "error": self.error}


class Plans:
    def __init__(self):
        self.lock = threading.Lock()
        self.current: Plan | None = None
        self.recent: list[dict] = []

    def current_public(self):
        with self.lock:
            return self.current.public() if self.current else None

    def loading_now(self):
        with self.lock:
            return set(self.current.loading) if self.current else set()

    def submit(self, op, target, args, by) -> Plan:
        with self.lock:
            if self.current and self.current.status in ("queued", "running"):
                raise PlanFailed(f"a plan is already running ({self.current.op} {self.current.target or ''}); cancel it first")
            p = Plan(op, target, args, by)
            self.current = p
        threading.Thread(target=self._run, args=(p,), daemon=True).start()
        return p

    def cancel_current(self):
        with self.lock:
            if self.current and self.current.status == "running":
                self.current.cancel.set()
                return True
        return False

    def _run(self, p: Plan):
        p.status = "running"
        t0 = time.time()
        try:
            OPS[p.op](p)
            p.status = "done"
        except Cancelled:
            p.status = "cancelled"
            for st in p.steps:
                if st["status"] == "running":
                    self_done(st, "cancelled")
        except PlanFailed as e:
            p.status, p.error = "failed", str(e)
            for st in p.steps:
                if st["status"] == "running":
                    self_done(st, "failed")
        except Exception as e:  # noqa: BLE001
            p.status, p.error = "failed", f"{type(e).__name__}: {e}"
            log("plan crash: " + traceback.format_exc()[-800:])
            for st in p.steps:
                if st["status"] == "running":
                    self_done(st, "failed")
        finally:
            p.ended = time.time()
            for sid in list(STATE.transient):
                STATE.set_transient(sid, None)
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "op": p.op, "target": p.target, "args": p.args, "by": p.by,
                   "result": p.status, "error": p.error, "duration_s": round(time.time() - t0, 1), "plan": p.id}
            try:
                with ACTIONS_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass
            with self.lock:
                self.recent.append(p.public())
                del self.recent[:-20]
            log(f"plan {p.id} {p.op} {p.target or ''} -> {p.status} {p.error or ''}")

    def park_watch(self, snap):
        """A parked server that came back loaded without a plan loading it is unloaded again (spec section 5)."""
        for sid in list(STATE.parked):
            info = snap["servers"].get(sid, {})
            if info.get("loaded") and sid not in self.loading_now() and not (self.current and self.current.status == "running"):
                s = INV.servers[sid]
                if info.get("busy"):
                    continue          # it is answering someone: wait for the turn to end rather than yank the card
                try:
                    st, r = http("POST", f"http://127.0.0.1:{s['port']}/v1/unload?wait=30", body={}, timeout=45)
                    log(f"park: re-unloaded {sid}: {st} {str(r)[:120]}")
                    with ACTIONS_LOG.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "op": "park_reunload", "target": sid,
                                            "by": "agent", "result": "done" if st == 200 else f"http {st}"}) + "\n")
                except Exception as e:  # noqa: BLE001
                    log(f"park: re-unload of {sid} failed: {e}")


def self_done(st, status):
    st["status"] = status
    st["ended"] = time.time()


# ---- primitive operations (each polls until the engine agrees) -----------------

def _wait(p: Plan, st, cond, timeout, every=1.5, what="..."):
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        p.check()
        try:
            ok, detail = cond()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)[:120]
        if ok:
            return detail
        n += 1
        if n % 4 == 0 and detail:
            p.say(st, f"{what}: {detail}")
        time.sleep(every)
    raise PlanFailed(f"timed out after {int(timeout)} s waiting for {what}")


def server_info(sid):
    return STATE.get()["servers"].get(sid) or {}


def fresh_info(sid):
    return STATE.probe_server(INV.servers[sid])


def op_unload_server(p: Plan, sid: str, force=False):
    s = INV.servers[sid]
    kind = s["kind"]
    base = f"http://127.0.0.1:{s['port']}"
    info = fresh_info(sid)
    name = s["name"]
    if kind == "ollama":
        models = info.get("models") or []
        if not models:
            return
        st = p.step(f"Unload Ollama models", ", ".join(m["name"] for m in models))
        for m in models:
            p.check()
            code, r = http("POST", base + "/api/generate", body={"model": m["name"], "keep_alive": 0}, timeout=30)
            p.say(st, f"{m['name']}: {code}")
        _wait(p, st, lambda: (not fresh_info(sid).get("models"), "still loaded"), s["timeouts"].get("unload_s", 60), what="Ollama to release")
        p.done_step(st)
        return
    if not info.get("up") or not info.get("loaded"):
        return
    if info.get("busy") and not force:
        raise PlanFailed(f"{name} is answering a request; wait, or click again with force")
    st = p.step(f"Unload {name}", f"port {s['port']}")
    STATE.set_transient(sid, "unloading")
    try:
        if kind == "strata":
            # Strata's own endpoint; wait= lets it finish the running turn instead of answering "busy"
            code, r = http("POST", base + "/v1/unload?wait=60" if force else base + "/v1/unload?wait=20", body={}, timeout=90)
            p.say(st, f"/v1/unload -> {code} {json.dumps(r)[:160]}")
            if code >= 400:
                raise PlanFailed(f"{name} refused to unload: {json.dumps(r)[:200]}")
            _wait(p, st, lambda: (not fresh_info(sid).get("loaded"), "still loaded"), s["timeouts"].get("unload_s", 90), what=f"{name} to release the card")
        elif kind == "strata-launch":
            # the launch namespace has no lazy load: unloading IS stopping the server
            op_stop_launch(p, sid, st)
        elif kind == "maya":
            code, r = http("POST", base + "/models/unload", body={}, timeout=60)
            p.say(st, f"/models/unload -> {code} {json.dumps(r)[:160]}")
            if code >= 400:
                raise PlanFailed(f"{name} refused to unload: {json.dumps(r)[:200]}")
            _wait(p, st, lambda: (not fresh_info(sid).get("loaded"), "still loaded"), s["timeouts"].get("unload_s", 60), what=f"{name} to release the card")
        p.done_step(st)
    finally:
        STATE.set_transient(sid, None)


def op_stop_launch(p: Plan, sid: str, st=None):
    s = INV.servers[sid]
    own = st is None
    if own:
        st = p.step(f"Stop {s['name']}", f"strata launch --stop --preset {s['preset']}")
    STATE.set_transient(sid, "stopping")
    try:
        rc, out, err = run([s["python"], s["strata_py"], "launch", "--stop", "--preset", s["preset"], "--output", "json"],
                           timeout=s["timeouts"].get("unload_s", 200), cwd=s["cwd"])
        for line in (out + err).strip().splitlines()[-4:]:
            p.say(st, line[:160])
        _wait(p, st, lambda: (not fresh_info(sid).get("up"), "port still open"), 60, what=f"{s['name']} to exit")
        if own:
            p.done_step(st)
    finally:
        STATE.set_transient(sid, None)


def op_stop_process(p: Plan, sid: str):
    """End a server process: the labelled, confirmed action. Strata: taskkill of the serve process listening on its port."""
    s = INV.servers[sid]
    if s["kind"] == "strata-launch":
        return op_stop_launch(p, sid)
    if s["kind"] == "ollama":
        raise PlanFailed("Ollama is a service; unload its models instead")
    st = p.step(f"Stop {s['name']} process", f"the process listening on {s['port']}")
    STATE.set_transient(sid, "stopping")
    try:
        pids = pid_listening(s["port"])
        if not pids:
            p.done_step(st, "nothing listening")
            return
        for pid in pids:
            p.check()
            rc, out, err = run(["taskkill.exe", "/T", "/F", "/PID", str(pid)], timeout=30)
            p.say(st, f"taskkill {pid}: {(out or err).strip()[:120]}")
        _wait(p, st, lambda: (not fresh_info(sid).get("up"), "port still open"), 60, what=f"{s['name']} to exit")
        # the engine child sometimes outlives the server (seen with maya.exe): one more sweep by name
        names = {"maya": ("maya.exe", "maya-vision.exe"), "strata": ("strata-vision.exe",)}.get(s["kind"], ())
        procs = process_list()
        for n in names:
            for pid in procs.get(n, []):
                if s["kind"] == "maya" or len(INV.servers) == 1:
                    run(["taskkill.exe", "/F", "/PID", str(pid)], timeout=15)
                    p.say(st, f"swept {n} {pid}")
        p.done_step(st)
    finally:
        STATE.set_transient(sid, None)


def op_start_server(p: Plan, sid: str):
    s = INV.servers[sid]
    info = fresh_info(sid)
    if info.get("up"):
        return
    st = p.step(f"Start {s['name']}", "")
    STATE.set_transient(sid, "starting")
    try:
        if s["kind"] == "strata-launch":
            argv = [s["python"], s["strata_py"], "launch", "--preset", s["preset"], "--no-tui", "--yes"]
            p.say(st, "strata launch --preset " + s["preset"] + " --no-tui --yes")
            out_path = LOG_DIR / f"{sid}.out.log"
            with out_path.open("ab") as out:
                out.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} quartermaster start\n".encode())
                proc = subprocess.Popen(argv, cwd=s["cwd"], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        creationflags=NO_WINDOW | NEW_GROUP)
            # launch returns once it serves; we wait on health, not on the process
            _wait(p, st, lambda: (fresh_info(sid).get("up"), f"launch pid {proc.pid} {'alive' if proc.poll() is None else 'exited ' + str(proc.returncode)}"),
                  s["timeouts"]["start_s"], what=f"{s['name']} to open port {s['port']}")
        else:
            argv = ["cmd.exe", "/c"] + list(s["start"]["argv"])
            p.say(st, " ".join(s["start"]["argv"]))
            out_path = LOG_DIR / f"{sid}.out.log"
            with out_path.open("ab") as out:
                out.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} quartermaster start\n".encode())
                proc = subprocess.Popen(argv, cwd=s["start"].get("cwd"), stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, creationflags=NO_WINDOW | NEW_GROUP)

            def up():
                if proc.poll() is not None:
                    tail = tail_file(str(out_path), 3)
                    raise PlanFailed(f"{s['name']} exited with {proc.returncode}: " + " | ".join(tail)[-300:])
                return fresh_info(sid).get("up"), "waiting for the port"
            _wait(p, st, up, s["timeouts"]["start_s"], what=f"{s['name']} to open port {s['port']}")
        p.done_step(st)
    finally:
        STATE.set_transient(sid, None)


def maya_touch(p: Plan, st, s):
    """Maya's /models/load does not reset its idle clock: left alone, idle_unload_s fires seconds after a load
    (seen 2026-11-03: loaded 17:02:17, "unloaded after 1800 s idle" 17:02:37). One thinking-off token resets it.
    Sent after every load and after a context reload (which also brings the model up)."""
    try:
        code, r = http("POST", f"http://127.0.0.1:{s['port']}/v1/chat/completions",
                       body={"model": s.get("model_id", "glm"), "messages": [{"role": "user", "content": "ok"}],
                             "max_tokens": 1, "temperature": 0, "stream": False,
                             "chat_template_kwargs": {"enable_thinking": False}}, timeout=120)
        p.say(st, f"idle-clock touch -> {code}")
    except Exception as e:  # noqa: BLE001 - the load stands even if the touch fails
        p.say(st, f"idle-clock touch failed: {e}")


def op_load_server(p: Plan, sid: str):
    s = INV.servers[sid]
    base = f"http://127.0.0.1:{s['port']}"
    info = fresh_info(sid)
    if info.get("loaded") and info.get("state") not in ("reloading",):
        return
    st = p.step(f"Load {s['name']}", "")
    STATE.set_transient(sid, "loading")
    p.loading.add(sid)
    result = {}
    try:
        if s["kind"] == "strata":
            # /v1/load holds the request until the model is up; fire it on a thread and poll health meanwhile

            def fire():
                try:
                    result["r"] = http("POST", base + "/v1/load", body={}, timeout=s["timeouts"]["load_s"] + 30)
                except Exception as e:  # noqa: BLE001
                    result["r"] = (0, str(e))
            threading.Thread(target=fire, daemon=True).start()
            p.say(st, "/v1/load sent")
        elif s["kind"] == "maya":

            def fire():
                try:
                    result["r"] = http("POST", base + "/models/load", body={}, timeout=s["timeouts"]["load_s"] + 30)
                except Exception as e:  # noqa: BLE001
                    result["r"] = (0, str(e))
            threading.Thread(target=fire, daemon=True).start()
            p.say(st, "/models/load sent")
        # strata-launch loads at start; nothing to send
        logp = s.get("log")

        def loaded():
            i = fresh_info(sid)
            if i.get("loaded") and i.get("state") in ("ready", "busy", "loading"):
                return True, "loaded"
            r = result.get("r")
            if r and r[0] >= 400:
                raise PlanFailed(f"{s['name']} did not load: {json.dumps(r[1])[:200]}")
            tl = tail_file(logp, 1) if logp else []
            return False, (tl[-1][:140] if tl else "loading")
        _wait(p, st, loaded, s["timeouts"]["load_s"], every=2.0, what=f"{s['name']} to load")
        if s["kind"] == "maya":
            maya_touch(p, st, s)
        p.done_step(st)
    finally:
        p.loading.discard(sid)
        STATE.set_transient(sid, None)


def op_verify_card_free(p: Plan, exclude: str | None = None):
    st = p.step("Verify the card is free", f"nvidia-smi used < {INV.gpu['free_card_mib']} MiB")

    def free():
        g = gpu_status()
        used = g.get("used_mib")
        if used is None:
            return False, g.get("error", "no reading")
        if used < INV.gpu["free_card_mib"]:
            return True, f"{int(used)} MiB used"
        apps = [a["name"] for a in gpu_apps() if a["name"].lower() in ENGINE_PROCS]
        extra = "; ComfyUI is up (Stop it if it will not let go)" if comfy_up() else ""
        return False, f"{int(used)} MiB used; engines on the card: {', '.join(apps) or 'none named'}{extra}"
    detail = _wait(p, st, free, 90, every=2.0, what="VRAM to drain")
    p.done_step(st, detail)


# ---- the ops the client may ask for -----------------------------------------------

def holders(exclude: str | None = None):
    """Who holds the card right now: fresh probes, not the cached poll (which lagged a load once and let a
    plan skip the unload of a server that had just finished loading, 2026-11-03 17:13)."""
    fresh = {}
    threads = []
    for sid, s in INV.servers.items():
        t = threading.Thread(target=lambda sid=sid, s=s: fresh.__setitem__(sid, STATE.probe_server(s)), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=8)
    out = []
    for sid, info in fresh.items():
        if sid == exclude:
            continue
        if info.get("loaded"):
            if INV.servers[sid]["kind"] == "ollama" and not INV.policy.get("unload_ollama_on_resident", True):
                continue
            out.append(sid)
    return out


def op_make_resident(p: Plan):
    sid = p.target
    if sid not in INV.servers or INV.servers[sid]["kind"] == "ollama":
        raise PlanFailed(f"unknown or non-resident target {sid!r}")
    s = INV.servers[sid]
    force = bool(p.args.get("force"))
    snap = p.step("Snapshot", "")
    info = fresh_info(sid)
    hs = holders(exclude=sid)
    p.say(snap, f"{s['name']}: {info.get('state')}; holders to clear: {', '.join(hs) or 'none'}")
    cn = comfy_note()
    if cn:
        p.say(snap, cn)
    p.done_step(snap, f"holders: {', '.join(INV.servers[h]['name'] for h in hs) or 'none'}")
    if info.get("loaded") and info.get("state") in ("ready", "busy") and not hs:
        p.step("Already resident", s["name"])["status"] = "done"
        p.steps[-1]["ended"] = time.time()
        return
    for h in hs:
        op_unload_server(p, h, force=force)
    freed = comfy_release(p, force)
    if hs or freed or not info.get("loaded"):
        op_verify_card_free(p)
    op_start_server(p, sid)
    op_load_server(p, sid)
    fin = p.step("Ready", "")
    i = fresh_info(sid)
    p.done_step(fin, f"{s['name']} {i.get('state')} on {s['port']}")
    if sid in STATE.parked:
        STATE.parked.discard(sid)        # making it resident is the human saying "not parked any more"


def op_unload(p: Plan):
    if p.target not in INV.servers:
        raise PlanFailed("unknown server")
    op_unload_server(p, p.target, force=bool(p.args.get("force")))


def op_unload_all(p: Plan):
    hs = holders()
    if not hs and not comfy_wants_release():
        st = p.step("Nothing to unload", "")
        p.done_step(st)
        return
    for h in hs:
        op_unload_server(p, h, force=bool(p.args.get("force")))
    comfy_release(p, bool(p.args.get("force")))
    op_verify_card_free(p)


def op_stop(p: Plan):
    if not p.args.get("confirm"):
        raise PlanFailed("stop ends a server process: send confirm=true")
    if p.target not in INV.servers:
        raise PlanFailed("unknown server")
    op_stop_process(p, p.target)


def op_start(p: Plan):
    if p.target not in INV.servers:
        raise PlanFailed("unknown server")
    s = INV.servers[p.target]
    if s["kind"] == "ollama":
        raise PlanFailed("Ollama is a service")
    # starting a server that loads at start is a load: the card must be clear
    if s.get("start", {}).get("loads_at_start") or s["kind"] == "strata-launch":
        hs = holders(exclude=p.target)
        if hs and not p.args.get("force"):
            raise PlanFailed(f"{', '.join(INV.servers[h]['name'] for h in hs)} holds the card; use Make resident, or unload first")
    op_start_server(p, p.target)
    if s.get("start", {}).get("loads_at_start") or s["kind"] == "strata-launch":
        op_load_server(p, p.target)


def op_park(p: Plan):
    s = INV.servers.get(p.target)
    if not s or not s.get("parkable"):
        raise PlanFailed("that server is not parkable")
    STATE.parked.add(p.target)
    st = p.step(f"Park {s['name']}", "unload now, and re-unload whenever it comes back loaded on its own")
    p.done_step(st)
    op_unload_server(p, p.target, force=bool(p.args.get("force")))


def op_unpark(p: Plan):
    STATE.parked.discard(p.target)
    st = p.step("Unpark", INV.servers.get(p.target, {}).get("name", p.target))
    p.done_step(st)


def op_set_context(p: Plan):
    s = INV.servers.get(p.target)
    if not s or s["kind"] != "maya":
        raise PlanFailed("context is live-settable on Maya only")
    n = int(p.args.get("value") or p.args.get("n") or 0)
    if n not in s.get("context_choices", [n]):
        raise PlanFailed(f"context {n} is not one of {s.get('context_choices')}")
    base = f"http://127.0.0.1:{s['port']}"
    info = fresh_info(p.target)
    if not info.get("up"):
        raise PlanFailed(f"{s['name']} is down")
    if (info.get("health") or {}).get("max_context") == n:
        st = p.step("Context unchanged", f"{n:,} already")
        p.done_step(st)
        return
    if info.get("busy"):
        raise PlanFailed(f"{s['name']} is answering; a context change reloads the engine")
    st = p.step(f"Set context {n:,}", "POST /api/context (the engine reloads)")
    STATE.set_transient(p.target, "reloading")
    p.loading.add(p.target)
    try:
        code, r = http("POST", base + "/api/context", body={"max_context": n}, timeout=30)
        p.say(st, f"/api/context -> {code} {json.dumps(r)[:160]}")
        if code >= 400:
            raise PlanFailed(f"context change refused: {json.dumps(r)[:200]}")

        def done():
            i = fresh_info(p.target)
            h = i.get("health") or {}
            return (h.get("max_context") == n and h.get("status") == "ok" and i.get("loaded")), f"{h.get('status')} ctx={h.get('max_context')}"
        _wait(p, st, done, s["timeouts"].get("reload_s", 300), every=2.0, what="the reload")
        maya_touch(p, st, s)
        p.done_step(st)
    finally:
        p.loading.discard(p.target)
        STATE.set_transient(p.target, None)


def op_set_budget(p: Plan):
    s = INV.servers.get(p.target)
    if not s or s["kind"] != "maya":
        raise PlanFailed("thinking budget is a Maya setting")
    n = int(p.args.get("value") or p.args.get("n") or 0)
    if n != 0 and not 256 <= n <= 262144:
        raise PlanFailed("budget is 0 (off) or 256..262144")
    st = p.step(f"Set thinking budget {n:,}", "POST /api/thinking-budget")
    code, r = http("POST", f"http://127.0.0.1:{s['port']}/api/thinking-budget", body={"budget": n}, timeout=15)
    p.say(st, f"-> {code} {json.dumps(r)[:160]}")
    if code >= 400:
        raise PlanFailed(f"budget refused: {json.dumps(r)[:200]}")
    p.done_step(st, json.dumps(r)[:120])


def op_start_shim(p: Plan):
    sh = INV.shims.get(p.target)
    if not sh:
        raise PlanFailed("unknown shim")
    if STATE.get().get("shims", {}).get(p.target, {}).get("up"):
        st = p.step("Shim already up", sh["name"])
        p.done_step(st)
        return
    st = p.step(f"Start {sh['name']}", " ".join(sh["start"]["argv"]))
    out_path = LOG_DIR / f"{p.target}.out.log"
    with out_path.open("ab") as out:
        subprocess.Popen(["cmd.exe", "/c"] + list(sh["start"]["argv"]), cwd=sh["start"].get("cwd"), stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW | NEW_GROUP)

    def up():
        try:
            c, _ = http("GET", f"http://127.0.0.1:{sh['port']}/api/tags", timeout=1.0)
            return c == 200, "waiting"
        except Exception:  # noqa: BLE001
            return False, "waiting for the port"
    _wait(p, st, up, 30, what="the shim to open its port")
    p.done_step(st)


def op_stop_shim(p: Plan):
    sh = INV.shims.get(p.target)
    if not sh:
        raise PlanFailed("unknown shim")
    st = p.step(f"Stop {sh['name']}", "")
    for pid in pid_listening(sh["port"]):
        run(["taskkill.exe", "/T", "/F", "/PID", str(pid)], timeout=20)
        p.say(st, f"taskkill {pid}")
    p.done_step(st)


def op_preset(p: Plan):
    pr = INV.presets.get(p.target)
    if not pr:
        raise PlanFailed("unknown preset")
    head = p.step(f"Preset: {pr['name']}", pr.get("desc", ""))
    p.done_step(head)
    for step in pr["steps"]:
        p.check()
        sub = Plan(step["op"], step.get("target"), {"value": step.get("value"), "force": p.args.get("force")}, p.by)
        sub.steps = p.steps          # share the stepper
        sub.cancel = p.cancel
        sub.loading = p.loading
        OPS[step["op"]](sub)


# ---- OpenWebUI (sequestered) ---------------------------------------------------
#
# This driver is for an OpenWebUI that runs as another user (a WSL distro, podman) which this process cannot
# list or enter. From here the agent can do two things: look at it from outside (/health, /api/version, two
# host-side logs, the bridge listeners) and ask Task Scheduler to run one of three scheduled tasks. What a
# task does is a sealed script that only administrators can write; this agent passes it nothing.
#
# Who can ring those bells is decided by Windows, not here: a task this unelevated process may run reads
# as "run"; one that wants elevation reads as "gated" and goes through a consent prompt a person answers.
# Written 2026-11-04 (Faber).

# Task names come from inventory.json (openwebui.tasks), never from a request. The inventory is already the
# trust root for every argv the agent runs, so naming the tasks there adds nothing a request could reach.
OWUI_TASK_DEFAULTS = {"start": "owui-ctl-start", "stop": "owui-ctl-stop", "update": "owui-ctl-update"}


def owui_tasks() -> dict:
    return {**OWUI_TASK_DEFAULTS, **((INV.openwebui or {}).get("tasks") or {})}


def owui_install_hint() -> str:
    return (INV.openwebui or {}).get("install_hint") or "install the scheduled tasks named in openwebui.tasks"


def owui_task_state(verb: str) -> str:
    """'run': this process may run it. 'gated': it exists and Windows wants elevation. 'missing'."""
    rc, out, err = run(["schtasks.exe", "/Query", "/TN", owui_tasks()[verb]], timeout=15)
    if rc == 0:
        return "run"
    return "gated" if "denied" in (out + err).lower() else "missing"


def owui_health() -> bool:
    try:
        st, _ = http("GET", INV.openwebui["url"] + "/health", timeout=3.0)
        return st == 200
    except Exception:  # noqa: BLE001 - down, or the port 3000 proxy reset the connection
        return False


def owui_bridge(o: dict):
    """(listeners bound, listeners expected) on the bridge address. iphlpsvc can leave them unbound after a boot."""
    ip, want = o.get("bridge_ip"), set(o.get("bridge_ports") or [])
    if not ip:
        return None, None
    rc, out, _ = run(["netstat.exe", "-ano", "-p", "tcp"], timeout=15)
    bound = set()
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "TCP" and parts[3] == "LISTENING" and parts[1].startswith(ip + ":"):
                try:
                    bound.add(int(parts[1].rsplit(":", 1)[1]))
                except ValueError:
                    pass
    return len(bound & want), len(want)


class OwuiWatch:
    """A slow look at OpenWebUI from outside the wall. Reads nothing of OpenWebUI's own data."""

    def __init__(self):
        self.lock = threading.Lock()
        self.info = None
        self._slow_at = 0.0

    def get(self):
        with self.lock:
            return dict(self.info) if self.info else None

    def refresh(self, slow=False):
        o = INV.openwebui
        if not o:
            with self.lock:
                self.info = None
            return
        info = self.get() or {"version": None, "tasks": {}, "bridge_bound": None, "bridge_expected": None,
                              "keepalive_last": None, "session_warning": False, "keepalive_exited": False}
        info["name"], info["url"] = o.get("name", "OpenWebUI"), o["url"]
        up = owui_health()
        if up and (slow or not info.get("up") or not info.get("version")):
            try:
                st, v = http("GET", o["url"] + "/api/version", timeout=3.0)
                if st == 200 and isinstance(v, dict):
                    info["version"] = str(v.get("version"))[:40]
            except Exception:  # noqa: BLE001
                pass
        info["up"] = up
        # both logs sit where any process running as the owner can write: they are text to show, never to act on
        info["ctl_tail"] = [l[:200] for l in tail_file(o["ctl_log"], 8)] if o.get("ctl_log") else []
        if slow or time.time() - self._slow_at > 20:
            self._slow_at = time.time()
            info["tasks"] = {v: owui_task_state(v) for v in owui_tasks()}
            info["install_hint"] = owui_install_hint()
            info["bridge_bound"], info["bridge_expected"] = owui_bridge(o)
            kl = tail_file(o["keepalive_log"], 8) if o.get("keepalive_log") else []
            last = max((i for i, l in enumerate(kl) if "starting " in l), default=None)
            if last is not None:
                info["keepalive_last"] = kl[last].split("starting ", 1)[1].strip()[:40]
                after = kl[last + 1:]
                info["session_warning"] = any("systemd user session" in l for l in after)
                info["keepalive_exited"] = any(l.startswith("exited code") for l in after)
        info["checked"] = time.time()
        with self.lock:
            self.info = info

    def loop(self):
        while True:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                log("owui watch error: " + traceback.format_exc()[-400:])
            cur = PLANS.current_public()
            busy = bool(cur and cur["status"] == "running" and str(cur["op"]).startswith("owui_"))
            time.sleep(2.0 if busy else 5.0)


OWUI_WATCH = OwuiWatch()


def _owui_need():
    if not INV.openwebui:
        raise PlanFailed("inventory.json has no openwebui block")


def _owui_log_size() -> int:
    try:
        return Path(INV.openwebui["ctl_log"]).stat().st_size
    except OSError:
        return 0


def _owui_log_since(offset: int) -> list[str]:
    try:
        with open(INV.openwebui["ctl_log"], "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() < offset:
                offset = 0                       # the log was replaced under us
            f.seek(offset)
            return f.read(512 * 1024).decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _owui_fire(p: Plan, st, verb: str) -> int:
    """Start one of the three scheduled tasks. Returns the control log's size from before it ran."""
    name = owui_tasks()[verb]
    state = owui_task_state(verb)
    before = _owui_log_size()
    if state == "missing":
        raise PlanFailed(f"task {name} is not installed: {owui_install_hint()}")
    if state == "run":
        rc, out, err = run(["schtasks.exe", "/Run", "/TN", name], timeout=30)
        if rc != 0:
            raise PlanFailed(f"schtasks /Run {name}: {(err or out).strip()[:200]}")
    else:
        # ShellExecute "runas": Windows shows its consent prompt on the secure desktop and this call returns
        # when a person has answered it. Nothing this process sends can answer it.
        p.say(st, "waiting for your answer to the Windows elevation prompt")
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        r = shell32.ShellExecuteW(None, "runas", "schtasks.exe", f'/Run /TN "{name}"', None, 0)
        if int(r or 0) <= 32:
            raise PlanFailed("the elevation prompt was declined or timed out; nothing was run")
    p.say(st, f"task {name} asked to run")
    return before


def _owui_wait_task(p: Plan, st, verb: str, before: int, timeout: float) -> int:
    """Follow the control log until the task writes its 'done rc=N' line; returns N."""
    started = re.compile(rf"=== owui-ctl {verb} start ")
    done = re.compile(rf"=== owui-ctl {verb} done rc=(-?\d+)")
    seen = 0

    def new_lines():
        nonlocal seen
        lines = _owui_log_since(before)
        for l in lines[seen:]:
            if l.strip() and not l.startswith("==="):
                p.say(st, l.strip()[:160])
        seen = len(lines)
        return lines

    def has_started():
        return any(started.search(l) for l in new_lines()), "the task has not written to the control log"

    def has_finished():
        lines = new_lines()
        for l in lines:
            m = done.search(l)
            if m:
                return True, int(m.group(1))
        return False, ""

    _wait(p, st, has_started, 60, every=1.5, what=f"task {OWUI_TASKS[verb]} to start")
    return _wait(p, st, has_finished, timeout, every=2.0, what=f"task {OWUI_TASKS[verb]} to finish")


def _owui_wait_health(p: Plan, want_up: bool, timeout: float):
    st = p.step("Wait for /health" if want_up else "Wait for the port to close")
    _wait(p, st, lambda: (owui_health() == want_up, "answering" if not want_up else "not answering yet"),
          timeout, every=3.0, what="OpenWebUI /health")
    OWUI_WATCH.refresh(slow=True)
    return st


def op_owui_start(p: Plan):
    _owui_need()
    st = p.step("Start OpenWebUI", "start the app unit inside the sequestered distro")
    if owui_health():
        return p.done_step(st, "already up")
    rc = _owui_wait_task(p, st, "start", _owui_fire(p, st, "start"), 180)
    if rc != 0:
        raise PlanFailed(f"the start task ended with code {rc}; see the OpenWebUI control log")
    p.done_step(st, "unit started")
    p.done_step(_owui_wait_health(p, True, 300), "200")


def op_owui_stop(p: Plan):
    _owui_need()
    if not p.args.get("confirm"):
        raise PlanFailed("stop takes OpenWebUI down for everyone using it: send confirm=true")
    st = p.step("Stop OpenWebUI", "stop the app unit; the distro and its keepalive stay up, and a reboot starts it again")
    if not owui_health():
        return p.done_step(st, "already down")
    rc = _owui_wait_task(p, st, "stop", _owui_fire(p, st, "stop"), 180)
    if rc != 0:
        raise PlanFailed(f"the stop task ended with code {rc}; see the OpenWebUI control log")
    p.done_step(st, "unit stopped")
    p.done_step(_owui_wait_health(p, False, 90), "down")


def op_owui_update(p: Plan):
    _owui_need()
    if not p.args.get("confirm"):
        raise PlanFailed("update pulls a new image and may migrate the database one way: send confirm=true")
    st = p.step("Update OpenWebUI", "tag the old image, pull :main, back up the data volume, restart. "
                                    "Cancel stops the watching, not the update.")
    if not owui_health():
        raise PlanFailed("OpenWebUI is not up; start it first")
    was = (OWUI_WATCH.get() or {}).get("version")
    rc = _owui_wait_task(p, st, "update", _owui_fire(p, st, "update"), 2400)
    if rc != 0:
        raise PlanFailed(f"the update script ended with code {rc}; see the OpenWebUI control log")
    p.done_step(st, "update script finished")
    st2 = _owui_wait_health(p, True, 300)
    now = (OWUI_WATCH.get() or {}).get("version")
    p.done_step(st2, f"already current ({now})" if now == was else f"{was or '?'} -> {now or '?'}")


# ---- ComfyUI: a service that shares the card ---------------------------------------
# It is the owner's own process, often started by hand. The agent starts exactly the argv in the inventory, stops only a process on its port whose command line names ComfyUI, and asks
# ComfyUI itself to drop its models (POST /free). It reads two small things from ComfyUI's API
# (/system_stats, and the queue count from /prompt) and nothing of the user's workflows, queue contents
# or outputs. How much of the card it holds comes from Windows, not from ComfyUI.

COMFY_HOLD_MIB = 1024          # dedicated VRAM above this counts as "ComfyUI holds part of the card" (idle is ~0.2 GiB)
TYPEPERF = r"C:\Windows\System32\typeperf.exe"
_COMFY_PID = {"pid": None, "at": 0.0}


def gpu_dedicated_mib(pid: int):
    """A process's dedicated GPU memory from Windows' own counter, in MiB; None if it has no GPU instance.
    ComfyUI's /system_stats cannot be used for this: under DynamicVRAM it reported 64 MiB reserved with
    27.4 GB on the card (2026-11-05), and nvidia-smi gives no per-process figure under WDDM."""
    rc, out, _ = run([TYPEPERF, rf"\GPU Process Memory(pid_{int(pid)}_*)\Dedicated Usage", "-sc", "1"], timeout=15)
    total, found = 0.0, False
    for line in out.splitlines():
        if line.startswith('"') and "PDH-CSV" not in line:
            for v in [c.strip('"') for c in line.split('","')][1:]:
                try:
                    total += float(v)
                    found = True
                except ValueError:
                    pass
    return round(total / 2**20) if found else None


POWERSHELL =r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
LOOPBACK = ("127.0.0.1", "[::1]")


def comfy_probe() -> dict:
    """What ComfyUI says about itself. Always returns the same keys; up=False when it does not answer."""
    c = INV.comfyui
    info = {"name": c.get("name", "ComfyUI"), "url": c["url"], "port": c["port"], "up": False, "version": None,
            "queue": 0, "vram_held_mib": None, "holding": False, "device": None, "on_card": False}
    try:
        st, s = http("GET", c["url"] + "/system_stats", timeout=2.5)
        if st != 200 or not isinstance(s, dict):
            return info
        info["up"] = True
        info["version"] = (str((s.get("system") or {}).get("comfyui_version") or "")[:24]) or None
        dev = (s.get("devices") or [{}])[0]
        info["device"] = str(dev.get("type") or "")[:12] or None
        info["on_card"] = info["device"] == "cuda"       # started with --cpu it reports system RAM as "vram": not the card
        if info["on_card"]:
            if _COMFY_PID["pid"] is None or time.time() - _COMFY_PID["at"] > 30:
                pids = pid_listening(c["port"])
                _COMFY_PID.update(pid=pids[0] if pids else None, at=time.time())
            held = gpu_dedicated_mib(_COMFY_PID["pid"]) if _COMFY_PID["pid"] else None
            if held is None:
                _COMFY_PID["pid"] = None                 # look the pid up again next time
            else:
                info["vram_held_mib"] = held
                info["holding"] = held >= COMFY_HOLD_MIB
        st2, q = http("GET", c["url"] + "/prompt", timeout=2.5)       # {"exec_info": {"queue_remaining": n}}: a count, not the queue
        if st2 == 200 and isinstance(q, dict):
            info["queue"] = int((q.get("exec_info") or {}).get("queue_remaining") or 0)
    except Exception:  # noqa: BLE001 - down
        pass
    return info


def comfy_up() -> bool:
    if not INV.comfyui:
        return False
    try:
        st, s = http("GET", INV.comfyui["url"] + "/system_stats", timeout=2.5)
        return st == 200 and isinstance(s, dict)
    except Exception:  # noqa: BLE001
        return False


def comfy_binds(port: int) -> list[str]:
    """Local addresses listening on the port, e.g. ['127.0.0.1:8188']."""
    rc, out, _ = run(["netstat.exe", "-ano", "-p", "tcp"], timeout=15)
    found = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "TCP" and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                found.append(parts[1])
    return sorted(set(found))


class ComfyWatch:
    """ComfyUI's state on its own slow clock, so a closed port never holds up the main poll."""

    def __init__(self):
        self.lock = threading.Lock()
        self.info = None
        self._slow_at = 0.0

    def get(self):
        with self.lock:
            return dict(self.info) if self.info else None

    def refresh(self, slow=False):
        if not INV.comfyui:
            with self.lock:
                self.info = None
            return
        prev = self.get() or {}
        info = comfy_probe()
        info["exposed"] = prev.get("exposed") or []
        if not info["up"]:
            info["exposed"] = []
        elif slow or not prev.get("up") or time.time() - self._slow_at > 30:
            self._slow_at = time.time()
            info["exposed"] = [b for b in comfy_binds(info["port"]) if b.rsplit(":", 1)[0] not in LOOPBACK]
        info["checked"] = time.time()
        with self.lock:
            self.info = info

    def loop(self):
        while True:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                log("comfy watch error: " + traceback.format_exc()[-400:])
            cur = PLANS.current_public()
            busy = bool(cur and cur["status"] == "running")
            time.sleep(2.0 if busy else 4.0)


COMFY_WATCH = ComfyWatch()


def _comfy_need() -> dict:
    if not INV.comfyui:
        raise PlanFailed("inventory.json has no comfyui block")
    return INV.comfyui


def comfy_note() -> str:
    """One line for a plan's snapshot, or '' when ComfyUI is not in the picture."""
    if not INV.comfyui:
        return ""
    i = comfy_probe()
    if not i["up"]:
        return ""
    return f"ComfyUI: {i['vram_held_mib'] if i['vram_held_mib'] is not None else '?'} MiB on the card, {i['queue']} in its queue"


def comfy_wants_release() -> bool:
    """True when a card-clearing plan has something to ask of ComfyUI."""
    if not INV.comfyui or not INV.policy.get("free_comfyui_on_resident", True):
        return False
    i = comfy_probe()
    return i["up"] and i["on_card"] and (i["queue"] > 0 or i["vram_held_mib"] is None or i["holding"])


def comfy_free(p: Plan, timeout: float | None = None):
    """Ask ComfyUI to unload its models. It acts between jobs, so a running job finishes first."""
    c = _comfy_need()
    st = p.step("Free ComfyUI memory", "POST /free: it unloads its models and stays up")
    if not comfy_up():
        return p.done_step(st, "not running")
    code, r = http("POST", c["url"] + "/free", body={"unload_models": True, "free_memory": True}, timeout=20)
    p.say(st, f"/free -> {code}")
    if code >= 400:
        raise PlanFailed(f"ComfyUI refused /free: {json.dumps(r)[:200]}")

    def freed():
        i = comfy_probe()
        if not i["up"]:
            return True, "ComfyUI went down"
        if i["queue"] > 0:
            return False, f"{i['queue']} job(s) in its queue; it lets go when the running one ends"
        if i["vram_held_mib"] is None:
            return True, "asked; ComfyUI is not on the card" if not i["on_card"] else "asked; ComfyUI reports no VRAM figure"
        return i["vram_held_mib"] < COMFY_HOLD_MIB, f"{i['vram_held_mib']} MiB on the card"
    detail = _wait(p, st, freed, timeout or c.get("timeouts", {}).get("free_s", 120), every=2.0, what="ComfyUI to let go")
    COMFY_WATCH.refresh()
    p.done_step(st, detail)


def comfy_release(p: Plan, force: bool) -> bool:
    """Part of clearing the card. Returns True if ComfyUI was asked to let go."""
    if not comfy_wants_release():
        return False
    i = comfy_probe()
    if i["queue"] > 0 and not force:
        raise PlanFailed(f"ComfyUI is generating ({i['queue']} job(s) in its queue); wait for it, or stop ComfyUI")
    comfy_free(p)
    return True


def _cmdline(pid: int) -> str:
    rc, out, _ = run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                      f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"], timeout=30)
    return out.strip() if rc == 0 else ""


def op_comfy_start(p: Plan):
    c = _comfy_need()
    argv = [str(a) for a in c["start"]["argv"]]
    st = p.step("Start ComfyUI", " ".join(argv))
    if comfy_up():
        return p.done_step(st, "already up")
    squatters = pid_listening(c["port"])
    if squatters:
        raise PlanFailed(f"port {c['port']} is taken by pid {squatters[0]}, which does not answer as ComfyUI")
    if any(a.startswith("--listen") for a in argv):
        raise PlanFailed("the inventory's argv has --listen: ComfyUI has no login and must stay on loopback")
    g = gpu_status()
    if g.get("used_mib") is not None and g.get("total_mib") is not None:
        p.say(st, f"{int(g['total_mib'] - g['used_mib'])} MiB free on the card; ComfyUI loads its models on the first job")
    out_path = LOG_DIR / "comfyui.out.log"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with out_path.open("ab") as out:
        out.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} quartermaster start\n".encode())
        proc = subprocess.Popen(argv, cwd=c["start"].get("cwd"), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, env=env, creationflags=NO_WINDOW | NEW_GROUP)

    def up():
        if proc.poll() is not None:
            tail = tail_file(str(out_path), 3)
            raise PlanFailed(f"ComfyUI exited with {proc.returncode}: " + " | ".join(tail)[-300:])
        return comfy_up(), "loading its nodes"
    _wait(p, st, up, c.get("timeouts", {}).get("start_s", 240), every=2.0, what=f"ComfyUI to open port {c['port']}")
    COMFY_WATCH.refresh(slow=True)
    i = COMFY_WATCH.get() or {}
    if i.get("exposed"):
        raise PlanFailed(f"ComfyUI came up listening on {', '.join(i['exposed'])}: stop it and check its arguments")
    p.done_step(st, f"answering on {c['port']}" + (f", v{i['version']}" if i.get("version") else ""))


def op_comfy_stop(p: Plan):
    if not p.args.get("confirm"):
        raise PlanFailed("stop ends ComfyUI and any job it is running: send confirm=true")
    comfy_kill(p)


def comfy_kill(p: Plan):
    c = _comfy_need()
    st = p.step("Stop ComfyUI", f"end the ComfyUI process listening on {c['port']}")
    pids = pid_listening(c["port"])
    if not pids:
        return p.done_step(st, "nothing listening")
    needle = str(c.get("match") or "main.py").lower()
    for pid in pids:
        cmd = _cmdline(pid)
        if needle not in cmd.lower():
            raise PlanFailed(f"pid {pid} on port {c['port']} is not ComfyUI ({cmd[:90] or 'no command line readable'}); left alone")
    for pid in pids:
        p.check()
        rc, out, err = run(["taskkill.exe", "/T", "/F", "/PID", str(pid)], timeout=30)
        p.say(st, f"taskkill {pid}: {(out or err).strip()[:120]}")
    _wait(p, st, lambda: (not comfy_up() and not pid_listening(c["port"]), "port still open"),
          c.get("timeouts", {}).get("stop_s", 60), what="ComfyUI to exit")
    COMFY_WATCH.refresh()
    p.done_step(st, "down")


def op_comfy_free(p: Plan):
    comfy_free(p)


# ---- ComfyUI update (2026-11-05) ------------------------------------------------------
# comfy_update.py does the work (check / apply / rollback); paths come from inventory comfyui.update only.
# The torch stack is pinned by a constraints file built from the venv's own freeze: it is a cu130 nightly
# that pip cannot fetch again.

def _upd_cfg() -> dict:
    c = _comfy_need()
    u = c.get("update")
    if not u:
        raise PlanFailed("inventory.json's comfyui block has no update section")
    return u


def _upd_run(p: Plan, st, mode: str, *extra, timeout=900) -> dict:
    u = _upd_cfg()
    pins = ",".join(u.get("pins") or [])
    argv = [sys.executable.replace("pythonw.exe", "python.exe"), str(HERE / "comfy_update.py"), mode, u["repo"], u["python"], *extra, pins]
    rc, out, err = run(argv, timeout=timeout)
    last = (out.strip().splitlines() or [""])[-1]
    try:
        r = json.loads(last)
    except ValueError:
        raise PlanFailed(f"comfy_update.py {mode} gave no result: {(err or out)[-300:]}") from None
    return r


def _comfy_import_failures() -> list[str]:
    """Custom nodes ComfyUI could not import at its last start (it lists them with '(IMPORT FAILED)')."""
    logp = (INV.comfyui or {}).get("log")
    lines = tail_file(logp, 400) if logp else []
    start = max((i for i, l in enumerate(lines) if "Import times for custom nodes" in l), default=None)
    if start is None:
        return []
    return [l.split("seconds", 1)[-1].strip(": ").strip() for l in lines[start:start + 40] if "IMPORT FAILED" in l]


def op_comfy_update(p: Plan):
    if not p.args.get("confirm"):
        raise PlanFailed("update stops ComfyUI and changes its code and packages: send confirm=true")
    u = _upd_cfg()
    ci = comfy_probe()
    if ci["up"] and ci["queue"] > 0:
        raise PlanFailed(f"ComfyUI is busy with {ci['queue']} job(s); update it when it is idle")
    st = p.step("Check for a release", "git fetch --tags; pip dry run with torch pinned")
    r = _upd_run(p, st, "check", timeout=900)
    if not r.get("ok"):
        raise PlanFailed(r.get("error") or "check failed")
    cur = (r.get("current") or {}).get("describe")
    if r.get("up_to_date"):
        return p.done_step(st, f"already up to date ({cur}; newest release {r['latest']})")
    if r.get("dirty"):
        raise PlanFailed("ComfyUI's tree has local changes; not updating over them")
    if not r.get("fast_forward"):
        raise PlanFailed(f"{cur} does not fast-forward to {r['latest']} (a local commit?); update by hand")
    for c in r.get("changes") or []:
        p.say(st, "pip: " + c)
    for c in r.get("pinned") or []:
        p.say(st, "pinned: " + c)
    p.done_step(st, f"{cur} -> {r['latest']}; {len(r.get('changes') or [])} package(s) change, torch stack pinned")
    tag = r["latest"]
    was_up = ci["up"]
    if was_up:
        comfy_kill(p)
    st = p.step("Update", f"git merge --ff-only {tag}; pip install -r requirements.txt with the pins")
    a = _upd_run(p, st, "apply", u["backup_root"], tag, timeout=1900)
    if not a.get("ok"):
        if a.get("code_updated") and a.get("backup"):
            p.say(st, a.get("error", "")[:200])
            _comfy_rollback(p, a["backup"], was_up)
            raise PlanFailed(f"update failed and was rolled back: {a.get('error', '')[:200]}")
        raise PlanFailed(a.get("error") or "apply failed")
    for c in a.get("changed") or []:
        p.say(st, "now " + c)
    p.done_step(st, f"{(a.get('after') or {}).get('describe')}; backup {Path(a['backup']).name}")
    try:
        op_comfy_start(p)
        i = comfy_probe()
        want = tag.lstrip("v")
        if not i["up"] or i.get("device") != "cuda":
            raise PlanFailed(f"ComfyUI came up on {i.get('device') or 'nothing'}, not cuda")
        if i.get("version") and i["version"] != want:
            raise PlanFailed(f"ComfyUI says {i['version']}, expected {want}")
    except (PlanFailed, Cancelled) as e:
        p.cancel.clear()
        _comfy_rollback(p, a["backup"], was_up)
        raise PlanFailed(f"{tag} did not boot ({e}); rolled back") from None
    bad = _comfy_import_failures()
    st = p.step("Boot test", f"v{i.get('version')} on cuda")
    for b in bad:
        p.say(st, "custom node failed to import: " + b)
    p.done_step(st, f"v{i.get('version')} on cuda" + (f"; {len(bad)} custom node(s) failed to import" if bad else "; custom nodes imported"))
    if not was_up:
        comfy_kill(p)


def _comfy_rollback(p: Plan, backup: str, was_up: bool):
    if comfy_up():
        comfy_kill(p)
    st = p.step("Roll back", f"git reset to the backup's head; pip restore from {Path(backup).name}")
    r = _upd_run(p, st, "rollback", backup, timeout=1900)
    if not r.get("ok"):
        raise PlanFailed("ROLLBACK FAILED, ComfyUI needs a hand: " + (r.get("error") or "")[:240])
    p.done_step(st, f"back on {(r.get('now') or {}).get('describe')}")
    if was_up:
        op_comfy_start(p)


def op_comfy_rollback(p: Plan):
    """Undo the newest update (by its backup folder)."""
    if not p.args.get("confirm"):
        raise PlanFailed("rollback stops ComfyUI and restores its code and packages: send confirm=true")
    u = _upd_cfg()
    root = Path(u["backup_root"])
    backups = sorted(d for d in root.glob("2*") if (d / "head.txt").is_file()) if root.is_dir() else []
    if not backups:
        raise PlanFailed("no update backups to roll back to")
    was_up = comfy_up()
    _comfy_rollback(p, str(backups[-1]), was_up)


# ---- the art queue: image batches that wait for the card ---------------------------
# Written 2026-11-05. An agent queues prompts (POST /api/art/submit, `qm art submit`); art_batch then parks the
# resident model, runs every queued batch in ComfyUI and loads the model again: one swap for the lot. Maya's GLM
# writes its conversation to disk on the unload and reads it back (MAYA_GLM_SLOT_KEEP=1, measured: a
# 375,105-token conversation saved in 2.6 s), so the caller's next turn does not re-read its whole context.

_ART = {"root": None, "q": None}


def art_queue():
    """The queue for the inventory's art block, or None without one."""
    if not INV.art or not INV.comfyui:
        return None
    root = Path(INV.art.get("queue_dir") or (ROOT / "artq"))
    if _ART["root"] != root:
        _ART.update(root=root, q=artqueue.ArtQueue(root))
    return _ART["q"]


def art_deny() -> list[str]:
    """Folders a batch may never write into, whatever art.out_roots says: Quartermaster's own."""
    return [str(ROOT)] + list((INV.art or {}).get("deny", []))


def _art_card_taken(comfy_held_mib) -> str:
    """'' while the card holds only ComfyUI (and the desktop); else who or what else is on it."""
    hs = holders()
    if hs:
        return ", ".join(INV.servers[h]["name"] for h in hs)
    g = gpu_status()
    used = g.get("used_mib")
    if used is not None and comfy_held_mib is not None and used - comfy_held_mib > INV.gpu["free_card_mib"]:
        apps = ", ".join(sorted({a["name"] for a in gpu_apps() if a["name"].lower() in ENGINE_PROCS})) or "no engine named"
        return f"{(used - comfy_held_mib) / 1024:.1f} GB besides ComfyUI ({apps})"
    return ""


def _art_run_batch(p: Plan, b: dict, cfg: dict) -> dict:
    q = art_queue()
    url = INV.comfyui["url"]
    st = p.step(f"Generate {b['name']}", f"{b['images']} image(s) to {b['out_dir']}")
    out_dir = Path(artqueue.check_out_dir(b["out_dir"], cfg.get("out_roots") or [], art_deny()))   # the inventory may have changed since
    out_dir.mkdir(parents=True, exist_ok=True)
    build = artqueue.WORKFLOWS[b["workflow"]]
    models = (cfg.get("models") or {}).get(b["workflow"])
    timeout = float(cfg.get("image_timeout_s", 600))
    outs = artqueue.outputs(b)
    files, errors, secs = [], [], []
    uploaded: dict[str, list[str]] = {}          # job name -> the names ComfyUI gave its reference images
    skipped = 0

    def results(error=None):
        r = {"written": len(files), "skipped": skipped, "failed": len(errors), "files": files, "errors": errors[:20],
             "seconds": round(sum(secs), 1), "mean_s": round(sum(secs) / len(secs), 1) if secs else None}
        if error:
            r["error"] = error
        return r

    for idx, (j, k, path) in enumerate(outs):
        p.check()
        if b.get("skip_existing", True) and path.exists():
            skipped += 1
            continue
        held = (comfy_probe() or {}).get("vram_held_mib")
        taken = _art_card_taken(held)
        if taken:
            why = f"{taken} took the card back during the batch (a request reached it); stopped, the rest stays queued"
            q.save_progress(b, results(why))
            p.say(st, why)
            raise PlanFailed(why)
        try:
            if j.get("refs") and j["name"] not in uploaded:
                uploaded[j["name"]] = [artqueue.upload_ref(url, r) for r in j["refs"]]
            graph, node = build(dict(j, seed=j["seed"] + k * 7919), models, uploaded.get(j["name"]))
            data, dt, caption = artqueue.generate(url, graph, node, timeout, check=p.check)
        except Cancelled:
            q.save_progress(b, results("cancelled"))
            raise
        except Exception as e:  # noqa: BLE001 - one bad image does not sink the batch
            errors.append({"file": path.name, "error": str(e)[:300]})
            p.say(st, f"{path.name}: {str(e)[:160]}")
            if not files and len(errors) >= 3:
                break                                  # three in a row and none made: the workflow itself is wrong
            continue
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        if caption:                                  # what the prompt upsampler told the model (flux3-klein)
            path.with_suffix(".caption.txt").write_text(caption, encoding="utf-8")
        files.append(path.name)
        secs.append(dt)
        st["detail"] = f"{idx + 1}/{len(outs)} · {len(files)} written · last {dt:.1f} s"
        p.say(st, f"{path.name}  {dt:.1f} s")
    r = results()
    status = "done" if not errors else "failed"
    q.finish(b, status, r)
    p.done_step(st, f"{r['written']} written, {r['skipped']} already there, {r['failed']} failed"
                    + (f", {r['mean_s']} s each" if r["mean_s"] else ""), status="done" if not errors else "failed")
    return r


def _art_put_back(p: Plan, comfy_was_up: bool, back: str | None):
    if comfy_up():
        if comfy_was_up:
            comfy_free(p)                   # it was up before the batch: leave it up, empty
        else:
            comfy_kill(p)
    if back:
        op_verify_card_free(p)
        op_start_server(p, back)
        op_load_server(p, back)
        i = fresh_info(back)
        st = p.step("Back on the card", INV.servers[back]["name"])
        p.done_step(st, f"{INV.servers[back]['name']} {i.get('state')} on {INV.servers[back]['port']}")


def op_art_batch(p: Plan):
    cfg = INV.art
    q = art_queue()
    if not q:
        raise PlanFailed("inventory.json needs an art block and a comfyui block")
    t_all = time.time()
    snap = p.step("Snapshot", "")
    batches = q.pending()
    if not batches:
        return p.done_step(snap, "the art queue is empty")
    images = sum(int(b.get("images") or 0) for b in batches)
    hs = holders()
    prior = next((h for h in hs if INV.servers[h]["kind"] != "ollama"), None)
    then = str(p.args.get("then") or "prior")
    if then == "prior":
        back = prior
    elif then == "none":
        back = None
    elif then in INV.servers and INV.servers[then]["kind"] != "ollama":
        back = then
    else:
        raise PlanFailed("then must be prior, none, or a server id")
    force = bool(p.args.get("force"))
    for h in hs:
        if INV.servers[h]["kind"] != "ollama" and fresh_info(h).get("busy") and not force:
            raise PlanFailed(f"{INV.servers[h]['name']} is answering a request; run the queue when it is idle, or force")
    ci = comfy_probe()
    comfy_was_up = ci["up"]
    if comfy_was_up and ci["queue"] > 0:
        raise PlanFailed(f"ComfyUI is busy with {ci['queue']} job(s) of its own; run the queue when it is idle")
    p.say(snap, f"{len(batches)} batch(es), {images} image(s); on the card: {', '.join(hs) or 'nothing'}")
    p.done_step(snap, f"{images} image(s) in {len(batches)} batch(es); afterwards "
                      + (f"{INV.servers[back]['name']} goes back on the card" if back else "the card stays empty"))
    error = None
    made = []
    try:
        for h in hs:
            op_unload_server(p, h, force=force)
        if comfy_was_up and ci.get("holding"):
            st = p.step("ComfyUI already up", "its models stay loaded for the batch")
            p.done_step(st)
        else:
            op_verify_card_free(p)
        if not comfy_was_up:
            op_comfy_start(p)
        for b in batches:
            made.append(_art_run_batch(p, b, cfg))
    except BaseException as e:  # noqa: BLE001 - Cancelled and PlanFailed included: the model goes back regardless
        error = e
    p.cancel.clear()                       # a cancel stops the images, not the putting back (cancel again to stop that)
    try:
        _art_put_back(p, comfy_was_up, back)
    except BaseException as e2:  # noqa: BLE001
        if error is None:
            error = e2
        else:
            log(f"art batch: putting the card back failed as well: {e2}")
    if error is not None:
        raise error
    gen = sum(r["seconds"] for r in made)
    fin = p.step("Done", "")
    p.done_step(fin, f"{sum(r['written'] for r in made)} image(s) in {gen:.0f} s of generating; "
                     f"{time.time() - t_all - gen:.0f} s of swapping and loading")


OPS = {"make_resident": op_make_resident, "unload": op_unload, "unload_all": op_unload_all, "stop": op_stop,
       "start": op_start, "park": op_park, "unpark": op_unpark, "set_context": op_set_context,
       "set_budget": op_set_budget, "start_shim": op_start_shim, "stop_shim": op_stop_shim, "preset": op_preset,
       "owui_start": op_owui_start, "owui_stop": op_owui_stop, "owui_update": op_owui_update,
       "comfy_start": op_comfy_start, "comfy_stop": op_comfy_stop, "comfy_free": op_comfy_free,
       "art_batch": op_art_batch, "comfy_update": op_comfy_update, "comfy_rollback": op_comfy_rollback}

STATE = State()
PLANS = Plans()


# ---- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Quartermaster/0.1"

    def log_message(self, fmt, *args):
        if DEBUG:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- auth --
    # Rewritten 2026-11-04. Two holes in the first version, both reachable from a web page in the browser:
    #   1. the container adds the bearer to every /api call, and the bearer alone used to pass a write, so a
    #      cross-site form POST through 8800 was authorized;
    #   2. "same origin" was Origin == Host, which a DNS-rebinding page satisfies with its own name.
    # Now: the Host must be one of ours, and a request that carries any browser header must come from one
    # of our two pages with the custom header and JSON, bearer or not. The bearer alone is for clients
    # that send no browser headers (qm.py, curl).
    def _host_ok(self):
        return self.headers.get("Host", "").strip().lower() in ALLOWED_HOSTS

    def _browser(self):
        return any(self.headers.get(h) is not None for h in ("Origin", "Referer", "Sec-Fetch-Site"))

    def _foreign_origin(self):
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin.strip().rstrip("/").lower() not in ALLOWED_ORIGINS      # "null" lands here too
        ref = self.headers.get("Referer")
        if ref:
            u = urlparse(ref)
            return f"{u.scheme}://{u.netloc}".lower() not in ALLOWED_ORIGINS
        return self.headers.get("Sec-Fetch-Site") not in ("same-origin", "none")

    def _bearer_ok(self):
        auth = self.headers.get("Authorization", "")
        return auth == "Bearer " + KEY

    def _read_ok(self):
        if self._browser():
            return not self._foreign_origin()
        return True                                  # no browser headers: a local client, as before

    def _write_ok(self):
        if self._browser():
            return (not self._foreign_origin() and self.headers.get("X-Quartermaster") is not None
                    and self.headers.get("Content-Type", "").startswith("application/json"))
        return self._bearer_ok()

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _art_log(self, op, target, by, detail):
        try:
            with ACTIONS_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "op": op, "target": target, "by": by,
                                    "result": "done", "detail": detail}) + "\n")
        except OSError:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _static(self, path):
        rel = path[1:] or "index.html"
        f = (UI_DIR / rel).resolve()
        if UI_DIR.resolve() not in f.parents or not f.is_file():
            return self._json(404, {"error": "not found"})
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if not self._host_ok():
            return self._json(403, {"error": "this agent answers only as 127.0.0.1 or localhost"})
        if not path.startswith("/api/"):
            return self._static(path)
        if not self._read_ok():
            return self._json(401, {"error": "missing bearer key or foreign origin"})
        if path == "/api/state":
            return self._json(200, STATE.get())
        if path == "/api/inventory":
            return self._json(200, INV.public())
        if path == "/api/plans":
            with PLANS.lock:
                return self._json(200, {"current": PLANS.current.public() if PLANS.current else None, "recent": PLANS.recent[::-1]})
        if path == "/api/history":
            n = int(q.get("lines", ["100"])[0])
            return self._json(200, {"actions": [json.loads(l) for l in tail_file(str(ACTIONS_LOG), n) if l.strip()][::-1]})
        if path == "/api/log":
            sid = q.get("server", [""])[0]
            n = min(int(q.get("lines", ["80"])[0]), 500)
            exclude = q.get("exclude", [None])[0]
            if sid in INV.servers:
                p = INV.servers[sid].get("log")
            elif sid == "agent":
                p = str(AGENT_LOG)
            elif sid == "openwebui" and INV.openwebui:
                p = INV.openwebui.get("ctl_log")
            elif sid == "owui-keepalive" and INV.openwebui:
                p = INV.openwebui.get("keepalive_log")
            elif sid == "comfyui" and INV.comfyui:
                p = INV.comfyui.get("log")
            elif sid.endswith(".out"):
                p = str(LOG_DIR / (sid[:-4] + ".out.log"))
            else:
                return self._json(404, {"error": "unknown server"})
            return self._json(200, {"server": sid, "path": p, "lines": tail_file(p, n, exclude) if p else []})
        if path == "/api/events":
            return self._sse()
        if path == "/api/art":
            aq = art_queue()
            if not aq:
                return self._json(404, {"error": "no art queue (inventory.json needs art and comfyui blocks)"})
            return self._json(200, dict(aq.summary(), out_roots=INV.art.get("out_roots") or [],
                                        workflows=sorted(artqueue.WORKFLOWS)))
        return self._json(404, {"error": "not found"})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_t = 0
        try:
            while True:
                snap = STATE.get()
                plan = PLANS.current_public()
                if snap.get("time") != last_t or plan:
                    payload = dict(snap)
                    payload["plan"] = plan
                    self.wfile.write(b"event: state\ndata: " + json.dumps(payload).encode() + b"\n\n")
                    self.wfile.flush()
                    last_t = snap.get("time")
                time.sleep(0.7 if plan and plan["status"] == "running" else 1.0)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        raw = self._body()
        if not self._host_ok():
            return self._json(403, {"error": "this agent answers only as 127.0.0.1 or localhost"})
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._write_ok():
            return self._json(401, {"error": "mutating calls need the bearer key (no browser headers), or one of the agent's own pages"})
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            return self._json(400, {"error": "send JSON"})
        by = body.get("by") or ("cli" if self._bearer_ok() and not self.headers.get("Referer") else "ui")
        if path == "/api/action":
            op = body.get("op")
            if op not in OPS:
                return self._json(400, {"error": f"unknown op; one of {sorted(OPS)}"})
            try:
                p = PLANS.submit(op, body.get("target"), body.get("args") or {}, by)
            except PlanFailed as e:
                return self._json(409, {"error": str(e)})
            return self._json(202, {"plan": p.public()})
        if path.startswith("/api/plans/") and path.endswith("/cancel"):
            return self._json(200, {"cancelled": PLANS.cancel_current()})
        if path.startswith("/api/art/"):
            aq = art_queue()
            if not aq:
                return self._json(404, {"error": "no art queue (inventory.json needs art and comfyui blocks)"})
            if path == "/api/art/submit":
                try:
                    batch = artqueue.validate(body.get("batch", body), INV.art, art_deny())
                except artqueue.Invalid as e:
                    return self._json(400, {"error": str(e)})
                rec = aq.submit(batch, by)
                self._art_log("art_submit", rec["id"], by, f"{rec['images']} image(s) to {rec['out_dir']}")
                return self._json(201, {"batch": aq.brief(rec)})
            parts = path.split("/")                  # /api/art/<id>/remove
            if len(parts) == 5 and parts[4] == "remove":
                cur = PLANS.current_public()
                if cur and cur["status"] == "running" and cur["op"] == "art_batch":
                    return self._json(409, {"error": "the queue is running; cancel it first"})
                b = aq.get(parts[3])
                if not b:
                    return self._json(404, {"error": "no queued batch with that id"})
                aq.finish(b, "removed", {"error": f"removed by {by}"})
                self._art_log("art_remove", b["id"], by, b.get("name", ""))
                return self._json(200, {"removed": b["id"]})
            return self._json(404, {"error": "not found"})
        if path == "/api/reload":
            try:
                INV.load()
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"error": f"inventory did not load: {e}"})
            return self._json(200, {"ok": True, "servers": list(INV.servers)})
        return self._json(404, {"error": "not found"})


def main():
    threading.Thread(target=STATE.loop, daemon=True).start()
    threading.Thread(target=OWUI_WATCH.loop, daemon=True).start()
    threading.Thread(target=COMFY_WATCH.loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log(f"quartermaster agent on http://{HOST}:{PORT} ({len(INV.servers)} servers, ui {UI_DIR})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
