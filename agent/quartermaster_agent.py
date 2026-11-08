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
import platform_host as host
import updaters

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UI_DIR = ROOT / "ui"
# QM_HOME: where inventory.json, presets.json and agent.key live (default: beside this file); with it set, logs go
# beside them too, so a second agent (a test, another port) never touches the installed one's files. QM_PORT: the port.
HOME = Path(os.environ["QM_HOME"]).resolve() if os.environ.get("QM_HOME") else HERE
LOG_DIR = (HOME / "logs") if os.environ.get("QM_HOME") else ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ACTIONS_LOG = LOG_DIR / "actions.jsonl"
AGENT_LOG = LOG_DIR / "agent.log"
INVENTORY = HOME / "inventory.json"
KEY = (HOME / "agent.key").read_text(encoding="utf-8").strip()
HOST, PORT = "127.0.0.1", int(os.environ.get("QM_PORT") or 8790)
UI_PORT = 8800                                   # the nginx container's page (docker-compose.yml)
# nginx forwards Host as $host, which drops the port, so the bare names are the container's requests
ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | {f"{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (PORT, UI_PORT)}
ALLOWED_ORIGINS = {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (PORT, UI_PORT)}
NVIDIA_SMI = host.nvidia_smi() or "nvidia-smi"
NO_WINDOW, NEW_GROUP = host.NO_WINDOW, host.NEW_GROUP
DEBUG = "--debug" in sys.argv
# engine processes as the OS names them (stem.exe on Windows); what the card's "apps" list is filtered to
ENGINE_PROCS = host.exe_names(("strata", "strata-dsv41", "strata-vision", "maya", "maya-vision",
                               "ollama", "ollama_llama_server", "ollama-runner", "llama-server"))
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf", ".ico": "image/x-icon",
                ".json": "application/json"}
# a machine's own font map (ui/fonts/local.css, ignored by git); the page always links it, so an empty one is served when absent
NO_FONT_MAP = b"/* no local font map: see README, Fonts */\n"


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
# Schema 2 (SPEC.md section 4): engines[] and units[] (a unit is one server of one engine), gpus[] (the
# first is the card the state machine manages), presets in presets.json beside the inventory. A schema-1
# file (servers[] with a group each, one gpu, presets inside) is migrated on first load: the result is
# written back once, the original kept as inventory.schema1.json.

PRESETS = HOME / "presets.json"
SLOTS = 6                       # theme accent slots --e1..--e6, by engine order; --e0 is the folded legacy band
ENGINE_NAMES = {"strata": "Strata", "maya": "Maya", "ollama": "Ollama", "llamacpp": "llama.cpp", "comfyui": "ComfyUI"}
UNSTOPPABLE = ("ollama", "llamacpp-router")      # drivers whose process serves others too: unload, never stop
# A unit's *kind* is what it is (its catalog record, its band, its update recipe); its *driver* is how the agent
# moves it: the code path for probe, load, unload, start and stop. Every kind here drives as itself; a fork
# names its parent's driver in its record (PolyStrata: strata) and needs no code; `http` and `cli` read a table.
DRIVERS = ("strata", "strata-launch", "maya", "ollama", "llamacpp", "llamacpp-router", "http", "cli")


def migrate_schema1(data: dict) -> tuple[dict, list]:
    """A schema-1 inventory as schema 2. Returns (inventory, presets). Pure: writes nothing."""
    out = {"schema": 2}
    for k in ("_comment", "host", "owner"):
        if k in data:
            out[k] = data[k]
    out["gpus"] = data.get("gpus") or ([data["gpu"]] if data.get("gpu") else [])
    for k in ("ram_total_gib", "pin_budget_gib", "commit_warn_gib", "poll_s", "policy", "rev"):
        if k in data:
            out[k] = data[k]
    engines, by_id, units = [], {}, []
    for s in data.get("servers", []):
        u = dict(s)
        kind = str(s.get("kind", "")).split("-")[0] or "engine"
        eid = s.get("group") or kind
        if eid == "legacy":                       # the folded group was never an engine
            eid = kind
            u["legacy"] = True
        label = s.get("engine") if isinstance(s.get("engine"), str) else ""
        u["engine"] = eid
        if label:
            u["engine_label"] = label
        u.pop("group", None)
        if eid not in by_id:
            e = {"id": eid, "kind": kind, "name": ENGINE_NAMES.get(kind, eid.capitalize())}
            m = re.search(r"\d+\.\d+(?:\.\d+)*", label or "")
            if kind == "ollama" and s.get("port"):
                e["version_url"], e["version_path"] = f"http://127.0.0.1:{s['port']}/api/version", "version"
            elif m:
                e["version"] = m.group(0)
            by_id[eid] = e
            engines.append(e)
        units.append(u)
    out["engines"], out["units"] = engines, units
    for k in ("shims", "openwebui", "comfyui", "art"):
        if k in data:
            out[k] = data[k]
    return out, list(data.get("presets", []))


class Inventory:
    def __init__(self):
        self.load()

    def load(self):
        data = json.loads(INVENTORY.read_text(encoding="utf-8"))
        if int(data.get("schema") or 1) < 2:
            data, presets = migrate_schema1(data)
            keep = HOME / "inventory.schema1.json"
            if not keep.exists():
                keep.write_bytes(INVENTORY.read_bytes())
            if presets and not PRESETS.exists():
                PRESETS.write_text(json.dumps({"presets": presets}, indent=2) + "\n", encoding="utf-8", newline="\n")
            INVENTORY.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
            log("inventory migrated to schema 2 (original kept as inventory.schema1.json; presets now in presets.json)")
        self.data = data
        self.engines = list(data.get("engines", []))
        for i, e in enumerate(self.engines):
            e["slot"] = f"e{min(i + 1, SLOTS)}"
        self.engine_by_id = {e["id"]: e for e in self.engines}
        self.servers = {}
        for u in data.get("units", []):
            u.setdefault("engine", str(u.get("kind", "")).split("-")[0])
            if u["engine"] not in self.engine_by_id:          # a unit naming no declared engine still gets a band
                e = {"id": u["engine"], "kind": u["engine"], "name": ENGINE_NAMES.get(u["engine"], u["engine"].capitalize()),
                     "slot": f"e{min(len(self.engines) + 1, SLOTS)}"}
                self.engines.append(e)
                self.engine_by_id[e["id"]] = e
            u["group"] = u["engine"]                              # older code says group
            u["driver"] = driver_of(u)
            u["timeouts"] = {"start_s": 120, "load_s": 600, "unload_s": 90, **(u.get("timeouts") or {})}   # a hand-written unit may omit them
            self.servers[u["id"]] = u
        # the card's "apps" list is filtered to engine processes: the built-in names, every catalog record's
        # executables, and the procs a table names (a kind added by record alone is seen on the card too)
        global ENGINE_PROCS
        named = {x for r in CATALOG.values() if r.get("role") == "engine"
                 for x in ((r.get("detect") or {}).get("executables") or []) + ((r.get("table") or {}).get("procs") or [])}
        named |= {x for s in self.servers.values() for x in ((s.get("table") or {}).get("procs") or [])}
        ENGINE_PROCS = tuple(sorted(set(ENGINE_PROCS) | {n.lower() for n in host.exe_names(tuple(named))}))
        self.shims = {s["id"]: s for s in data.get("shims", [])}
        self.clients = {c["id"]: c for c in data.get("clients", []) if c.get("id")}
        if PRESETS.exists():
            presets = json.loads(PRESETS.read_text(encoding="utf-8")).get("presets", [])
        else:
            presets = data.get("presets", [])
        self.presets = {p["id"]: p for p in presets}
        self.gpus = data.get("gpus") or ([data["gpu"]] if data.get("gpu") else [])
        self.gpu = self.gpus[0] if self.gpus else {"index": 0, "name": "", "total_mib": 0, "free_card_mib": 2048}
        self.policy = data.get("policy", {})
        self.poll_s = float(data.get("poll_s", 2.0))
        self.openwebui = data.get("openwebui")       # optional; absent = no OpenWebUI tile
        self.comfyui = data.get("comfyui")           # optional; absent = no ComfyUI tile
        self.art = data.get("art")                   # optional; absent = no art queue (needs comfyui too)

    def public(self):
        """What the page gets: the inventory with the derived pieces filled in."""
        return {**self.data, "gpu": self.gpu, "gpus": self.gpus, "engines": self.engines,
                "units": list(self.servers.values()), "presets": list(self.presets.values())}


_ENGINE_VER: dict[str, tuple[float, str | None]] = {}


def engine_version(e: dict) -> str | None:
    """An engine's version from whichever source it declares: version (static), version_file, version_cmd
    (first version-looking token of its output) or version_url + version_path (a dotted JSON path). Cached
    ten minutes; every source is local, nothing leaves the machine."""
    hit = _ENGINE_VER.get(e["id"])
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    v = e.get("version")
    try:
        if e.get("version_file"):
            v = Path(e["version_file"]).read_text(encoding="utf-8", errors="replace").strip().splitlines()[0][:40]
        elif e.get("version_cmd"):
            rc, out, err = run(list(e["version_cmd"]), timeout=25, cwd=e.get("root"))
            text = (out or err).strip()
            # version_re, when given, picks the number out of a busier --version (its first group that matched)
            m = re.search(e.get("version_re") or r"\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]+)?", text)
            v = next((g for g in m.groups() if g), m.group(0)) if m else (text.splitlines()[0][:40] if text else v)
        elif e.get("version_url"):
            st, body = http("GET", e["version_url"], timeout=3.0)
            cur = body if st == 200 else None
            for k in str(e.get("version_path") or "version").split("."):
                cur = cur.get(k) if isinstance(cur, dict) else None
            if cur:
                v = str(cur)[:40]
    except Exception:  # noqa: BLE001 - a version is decoration; the band shows none
        pass
    _ENGINE_VER[e["id"]] = (time.time(), v)
    return v


# ---- the catalog and the update recipes -------------------------------------------------------
# catalog/<kind>.json ships with the code (SPEC.md section 7a). An engine or client takes its update recipe
# from the catalog record of its kind unless its inventory entry carries an "update" block, which wins
# key by key. Checks run only when someone clicks; the last result per id is kept in logs/updates.json.

CATALOG_DIR = ROOT / "catalog"
CATALOG: dict[str, dict] = {}
for _f in sorted(CATALOG_DIR.glob("*.json")) if CATALOG_DIR.is_dir() else []:
    try:
        _rec = json.loads(_f.read_text(encoding="utf-8"))
        CATALOG[_rec.get("id") or _f.stem] = _rec
    except (OSError, ValueError) as _e:
        print(f"catalog: {_f.name} did not load: {_e}", file=sys.stderr)
UPDATES_FILE = LOG_DIR / "updates.json"
try:
    UPDATES: dict[str, dict] = json.loads(UPDATES_FILE.read_text(encoding="utf-8")) if UPDATES_FILE.exists() else {}
except (OSError, ValueError):
    UPDATES = {}


def driver_of(u: dict) -> str:
    """How the agent moves a unit: the unit's own `driver`, else its kind's catalog record, else the kind itself.
    An unknown driver is kept as written so the probe reports it rather than guessing."""
    return str(u.get("driver") or (CATALOG.get(str(u.get("kind", ""))) or {}).get("driver") or u.get("kind") or "")


def suggestions() -> list[dict]:
    """Rules over the inventory and the state, nothing fetched (SPEC.md 7a). Each: {kind, title, why, how}."""
    st = STATE.get()
    out = []
    has_card = bool(INV.gpu.get("total_mib"))
    engines_with_units = {s["engine"] for s in INV.servers.values()}
    model_units = [s for s in INV.servers.values() if s["driver"] != "ollama" and not s.get("legacy")]
    if has_card and not engines_with_units:
        out.append({"kind": "engine", "title": "Nothing serves a model yet",
                    "why": "the inventory names a card but no engine with a unit",
                    "how": "Ollama is the quickest first model (pull one, it answers on 11434); llama.cpp gives every knob and runs any GGUF; "
                           "Strata or Maya hold one large model resident at very long context. Run `qm setup` after installing one; the catalog's guides have the steps."})
    if engines_with_units and not INV.openwebui and not INV.clients:
        out.append({"kind": "openwebui", "title": "No front end or client is listed",
                    "why": "engines serve models but nothing in the inventory consumes them",
                    "how": "OpenWebUI is a chat front end for several engines and people; Pi or Codex CLI are terminal coding agents. "
                           "`qm detect` adds a client it finds; the OpenWebUI guide has the install."})
    if INV.comfyui and not INV.art:
        out.append({"kind": "comfyui", "title": "ComfyUI without the art queue",
                    "why": "image batches could wait for the card and run between resident models in one swap",
                    "how": "add an `art` block to the inventory (agent/inventory.example.json shows it: queue_dir, out_roots, models) and reload."})
    if len(model_units) >= 2 and len(INV.presets) < 2:
        out.append({"kind": "presets", "title": "Several models, few presets",
                    "why": f"{len(model_units)} units take turns on the card and {len(INV.presets)} preset{'s' if len(INV.presets) != 1 else ''} name an arrangement",
                    "how": "Save current on the Presets band after you have set a working arrangement up; + New composes one from the agent's ops."})
    served = {s.get("port") for s in INV.servers.values()} | {sh.get("port") for sh in INV.shims.values()}
    for c in INV.clients.values():
        orphan = sorted({int(m.group(1)) for b in c.get("bindings") or [] for m in [re.search(r":(\d+)(?:/|$)", str(b.get("url", "")))] if m} - served)
        if orphan:
            out.append({"kind": "client", "title": f"{c.get('name', c['id'])} points at {', '.join(':' + str(p) for p in orphan)}",
                        "why": "nothing in the roster serves those ports; its requests there will fail quietly",
                        "how": f"add the unit that should answer there, or fix the binding in {c.get('config', 'its configuration')}."})
    blank = [s["id"] for s in model_units if not (s.get("footprint") or {}).get("vram_gib")]
    if blank:
        out.append({"kind": "inventory", "title": f"{len(blank)} unit{'s' if len(blank) != 1 else ''} without a footprint",
                    "why": "the card's planning and the warnings use vram_gib and ram_gib; setup cannot measure them",
                    "how": f"fill in footprint.vram_gib and ram_gib for {', '.join(blank[:4])}{'…' if len(blank) > 4 else ''} after one load (the VRAM gauge shows what it took)."})
    no_ver = [e["id"] for e in INV.engines if not any(e.get(k) for k in ("version", "version_cmd", "version_file", "version_url"))]
    if no_ver:
        out.append({"kind": "inventory", "title": f"No version source for {', '.join(no_ver)}",
                    "why": "the band shows nothing and an update check has nothing to compare",
                    "how": "give the engine a version_cmd, version_file or version_url in the inventory (the catalog record says which it has)."})
    for uid, u in ((STATE.get() or {}).get("updates") or UPDATES).items():
        if u.get("ok") and u.get("available"):
            out.append({"kind": "update", "title": f"{uid}: {u.get('latest')} is newer than {u.get('installed') or '?'}",
                        "why": f"from the check you ran {u.get('checked_at', '')}",
                        "how": "the Update entry in its menu applies it where the recipe can; otherwise by hand, as the menu says."})
    if INV.servers and not INV.presets:
        pass
    return out


def scan_proposals() -> dict:
    """What detection sees that the inventory lacks: a read of the machine on a click, never applied here."""
    import setup as _setup
    kinds = {k for k, _ in _setup.KINDS}
    found = _setup.detect_all(kinds)
    have_e = {e["id"] for e in INV.engines}
    have_k = {_setup.unit_key(s) for s in INV.servers.values()}
    have_c = set(INV.clients)
    return {"engines": [e["id"] for e in found["engines"] if e["id"] not in have_e],
            "units": [{"id": u["id"], "engine": u["engine"], "port": u["port"], "name": u["name"], "running": u.get("running", False)}
                      for u in found["units"] if _setup.unit_key(u) not in have_k],
            "services": [k for k in found["services"] if not getattr(INV, k, None)],
            "clients": [{"id": c["id"], "version": c.get("version")} for c in found["clients"] if c["id"] not in have_c],
            "notes": found["notes"]}


def updatable(uid: str) -> tuple[dict, str] | tuple[None, None]:
    """(entry, 'engine'|'client') for an id the update ops may act on."""
    if uid in INV.engine_by_id:
        return INV.engine_by_id[uid], "engine"
    if uid in INV.clients:
        return INV.clients[uid], "client"
    return None, None


def recipe_for(entry: dict) -> dict:
    kind = entry.get("kind") or entry.get("id")
    base = (CATALOG.get(kind) or {}).get("update") or {}
    return {**base, **(entry.get("update") or {})}


def update_ctx(entry: dict, role: str) -> dict:
    py = entry.get("python")
    if not py and entry.get("version_cmd") and "python" in str(entry["version_cmd"][0]).lower():
        py = entry["version_cmd"][0]
    installed = engine_version(entry) if role == "engine" else (engine_version(entry) if entry.get("version_cmd") else entry.get("version"))
    return {"id": entry["id"], "name": entry.get("name", entry["id"]), "root": entry.get("root"), "python": py, "exe": entry.get("exe"),
            "installed": installed, "backup_root": entry.get("backup_root") or str(LOG_DIR / "update-backups")}


def settled(u: dict, live: str | None) -> dict:
    """An update record as of now. A version moved outside Quartermaster (by hand, or by the program's own
    updater) to the one the check found leaves the record stale: the live version wins and nothing is offered."""
    if u.get("ok") and u.get("available") and live and not updaters.newer(u.get("latest"), live):
        return {**u, "installed": live, "available": False, "can_apply": False}
    return u


def remember_update(uid: str, rec: dict) -> None:
    UPDATES[uid] = {**rec, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        UPDATES_FILE.write_text(json.dumps(UPDATES, indent=2) + "\n", encoding="utf-8", newline="\n")
    except OSError:
        pass


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


ram_status = host.ram_status


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
                name = Path(parts[1]).name
                # Ollama's runner is its own copy of llama-server (lib/ollama/llama-server): name it for whose it is
                if name.lower().startswith("llama-server") and "ollama" in parts[1].lower():
                    name = host.exe_names(("ollama-runner",))[0]
                apps.append({"pid": parts[0], "name": name})
    return apps


def process_list():
    """{name_lower: [pids]} for the engine processes only."""
    return host.process_list(ENGINE_PROCS)


pid_listening = host.pid_listening


# ---- llama.cpp in router mode --------------------------------------------------------------------
# llama-server started with --models-dir (or --models-preset) is a router: it holds no model itself, starts one
# child llama-server per loaded model on a port of its own, and answers GET /models with each model's status.
# Every model it offers is a unit of kind llamacpp-router (the router's port, its own router_model). Load and
# unload go through POST /models/load and /models/unload; the router process is never ended for either.

_ROUTER_CACHE: dict[int, tuple[float, dict | None]] = {}


def router_models(port: int, max_age: float = 0.5) -> dict | None:
    """{model id or alias: status} from a router's GET /models, or None when it does not answer. A poll probes
    every unit of one router; one request serves them all."""
    hit = _ROUTER_CACHE.get(port)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        st, body = http("GET", f"http://127.0.0.1:{port}/models", timeout=2.0)
    except Exception:  # noqa: BLE001 - down
        st, body = 0, None
    out = None
    if st == 200 and isinstance(body, dict):
        out = {}
        for m in body.get("data") or []:
            if isinstance(m, dict) and m.get("id"):
                for name in [m["id"], *(m.get("aliases") or [])]:
                    out[name] = m.get("status") or {}
    _ROUTER_CACHE[port] = (time.time(), out)
    return out


def router_arg(status: dict, flag: str) -> str | None:
    """One flag's value from the argv the router runs (or would run) a model's child with."""
    a = [str(x) for x in status.get("args") or []]
    return a[a.index(flag) + 1] if flag in a and a.index(flag) + 1 < len(a) else None


def systemctl_user(p: Plan, st, verb: str, unit: str, timeout: float = 60):
    p.say(st, f"systemctl --user {verb} {unit}")
    rc, out, err = host.run(["systemctl", "--user", verb, unit], timeout=timeout)
    if rc != 0:
        raise PlanFailed(f"systemctl --user {verb} {unit} failed: {(err or out).strip()[:200]}")


# ---- the table drivers: http and cli ----------------------------------------------------------------
# An engine whose API is "a path to call and a key to read" needs no code of its own: its catalog record says
# `"driver": "http"` (or "cli") and carries a `table`, which a unit may override key by key. The table names:
#   probe   {path, status}                     answering is up (default GET /health, 200)
#   loaded  true | {path?, status?, <pred>}    the model is on the card (default: up means loaded, one model per process)
#   busy    {path?, <pred>}                    answering a request; guards an unload
#   load    {method, path, body, timeout_s, hold}   hold: the server answers only when done (Strata's /v1/load)
#   unload  {method, path, body, timeout_s, hold}   absent: unloading is stopping the process (llama-server)
#   procs   [names]                            processes swept by name after a stop, when a child outlives the server
#   auth    {header, prefix, key_file, key_re} a header the server wants, read from a file; the key is never in a record
# cli adds status {argv, json, <pred> | loaded_re}, load/unload/start/stop {argv, timeout_s, cwd}; its probe is
# the port (or probe.path) for up and the status command for loaded.
# A predicate (<pred>): `key` (dotted path; a list on the way means any element) with one of equals, not_equals,
# in, truthy, nonempty; a key alone tests that it exists. Strings take {model} and {port} from the unit.
# Written 2026-11-07 (Faber), for the kinds nobody here can run: the table is filled from the docs, the record's
# `tested` says who has driven it.

PRED_KEYS = ("key", "equals", "not_equals", "in", "truthy", "nonempty")


def table_of(s: dict) -> dict:
    """The unit's table, tokens filled: its kind's catalog `table`, with the unit's own `table` winning key by
    key (one level)."""
    base = (CATALOG.get(str(s.get("kind", ""))) or {}).get("table") or {}
    out = dict(base)
    for k, v in (s.get("table") or {}).items():
        out[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return fill_tokens(out, s)


def fill_tokens(x, s: dict, escape: bool = False):
    """{model} and {port} in a table's strings, from the unit; inside a `loaded_re` the model is regex-escaped."""
    if isinstance(x, str):
        model = str(s.get("model_id") or s.get("router_model") or s.get("model") or "")
        return x.replace("{model}", re.escape(model) if escape else model).replace("{port}", str(s.get("port") or ""))
    if isinstance(x, list):
        return [fill_tokens(v, s, escape) for v in x]
    if isinstance(x, dict):
        return {k: fill_tokens(v, s, k == "loaded_re") for k, v in x.items()}
    return x


def pick(data, key: str) -> list:
    """The values at a dotted path; a list on the way contributes every element's value."""
    vals = [data]
    for part in key.split("."):
        nxt = []
        for v in vals:
            if isinstance(v, list):
                nxt.extend(x[part] for x in v if isinstance(x, dict) and part in x)
            elif isinstance(v, dict) and part in v:
                nxt.append(v[part])
        vals = nxt
    return vals


def pred_part(spec) -> dict | bool:
    """The predicate inside a table entry, or True when it has none (then the call answering is the test)."""
    if isinstance(spec, dict) and any(k in spec for k in PRED_KEYS):
        return {k: spec[k] for k in PRED_KEYS if k in spec}
    return True


def holds(data, pred) -> bool:
    """Does a JSON answer satisfy a predicate? True (or none) is satisfied by any answer."""
    if pred is True or pred is None:
        return True
    if not isinstance(pred, dict):
        return False
    vals = pick(data, pred["key"]) if pred.get("key") else [data]
    if "equals" in pred:
        return any(v == pred["equals"] for v in vals)
    if "not_equals" in pred:
        return bool(vals) and all(v != pred["not_equals"] for v in vals)
    if "in" in pred:
        return any(v in pred["in"] for v in vals)
    if pred.get("truthy") or pred.get("nonempty"):
        return any(bool(v) for v in vals)
    return bool(vals)


def unit_headers(s: dict, t: dict) -> dict:
    """A header the server wants on every call (TabbyAPI's admin key), read from a file the unit or its table
    names: {"header": "x-admin-key", "key_file": "~/tabbyAPI/api_tokens.yml", "key_re": "^admin_key:\\s*(\\S+)"}.
    The value is read on each call and never logged; a missing file means no header."""
    a = s.get("auth") or t.get("auth")
    if not isinstance(a, dict) or not a.get("key_file"):
        return {}
    try:
        text = Path(os.path.expanduser(a["key_file"])).read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if a.get("key_re"):
        m = re.search(a["key_re"], text, re.M)
        text = m.group(1) if m else ""
    return {a.get("header", "Authorization"): a.get("prefix", "") + text} if text else {}


def http_get(s: dict, spec: dict | None, t: dict, default_path: str, timeout: float = 2.0):
    """GET one table path on the unit's port. (status, data), or (None, None) when nothing answers."""
    path = fill_tokens((spec or {}).get("path") or default_path, s)
    try:
        return http("GET", f"http://127.0.0.1:{s['port']}{path}", timeout=timeout, headers=unit_headers(s, t))
    except Exception:  # noqa: BLE001 - down
        return None, None


def _scalars(h) -> dict:
    return {k: v for k, v in list(h.items())[:12] if v is None or isinstance(v, (str, int, float, bool))} if isinstance(h, dict) else {}


def _answered(st: int | None, spec: dict) -> bool:
    return st is not None and (st == int(spec["status"]) if "status" in spec else st < 400)


def http_probe(s: dict, info: dict) -> dict:
    """The http driver's probe. Health shows the probe answer's scalars; state follows loaded and busy."""
    t = table_of(s)
    pr = t.get("probe") or {}
    st, h = http_get(s, pr, t, "/health")
    if not _answered(st, {"status": pr.get("status", 200)}):
        return info
    info["up"] = True
    info["health"] = _scalars(h)
    ld = t.get("loaded", True)
    if isinstance(ld, dict):
        if ld.get("path"):
            st2, d = http_get(s, ld, t, "/health")
            info["loaded"] = _answered(st2, ld) and holds(d, pred_part(ld))
        else:
            info["loaded"] = holds(h, pred_part(ld))
    else:
        info["loaded"] = bool(ld)
    bz = t.get("busy")
    if info["loaded"] and isinstance(bz, dict):
        st3, d = http_get(s, bz, t, pr.get("path") or "/health", timeout=1.0)
        info["busy"] = _answered(st3, bz) and holds(d, pred_part(bz))
    info["state"] = "busy" if info["busy"] else "ready" if info["loaded"] else "idle"
    return info


def http_call(p: Plan, st, s: dict, spec: dict, t: dict, what: str, result: dict | None = None):
    """One table call (load or unload). `hold` sends it on a thread, since the server answers only when done, and
    the caller waits on the probe; otherwise the answer is in hand and 4xx/5xx fails the plan."""
    method, path = str(spec.get("method", "POST")).upper(), spec["path"]
    body = spec.get("body", {}) if method != "GET" else None
    timeout = float(spec.get("timeout_s") or 60)
    url = f"http://127.0.0.1:{s['port']}{path}"
    hdr = unit_headers(s, t)
    if spec.get("hold"):
        result = result if result is not None else {}

        def fire():
            try:
                result["r"] = http(method, url, body=body, timeout=timeout, headers=hdr)
            except Exception as e:  # noqa: BLE001
                result["r"] = (0, str(e))
        threading.Thread(target=fire, daemon=True).start()
        p.say(st, f"{method} {path} sent")
        return None
    code, r = http(method, url, body=body, timeout=timeout, headers=hdr)
    p.say(st, f"{method} {path} -> {code} {json.dumps(r)[:160]}")
    if code >= 400:
        raise PlanFailed(f"{s['name']} refused to {what}: {json.dumps(r)[:200]}")
    return code, r


_CLI_CACHE: dict[str, tuple[float, tuple]] = {}


def cli_run(s: dict, spec: dict, timeout: float | None = None):
    return run([str(a) for a in spec["argv"]], timeout=timeout or float(spec.get("timeout_s") or 60),
               cwd=spec.get("cwd") or (s.get("start") or {}).get("cwd"))


def cli_status(s: dict, t: dict, max_age: float = 4.0) -> tuple:
    """(rc, data, text) from the status command, data parsed as JSON when the table says `json`. Cached a few
    seconds: a poll every two seconds must not spawn a process per unit each time."""
    hit = _CLI_CACHE.get(s["id"])
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    spec = t.get("status") or {}
    try:
        rc, out, err = cli_run(s, spec, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        rc, out, err = 1, "", str(e)
    data = out
    if spec.get("json"):
        try:
            data = json.loads(out or "null")
        except ValueError:
            data = None
    res = (rc, data, out)
    _CLI_CACHE[s["id"]] = (time.time(), res)
    return res


def cli_probe(s: dict, info: dict) -> dict:
    """The cli driver's probe: up is probe.path answering, or anything listening on the port; loaded is the status
    command's answer against its predicate (JSON) or loaded_re (text)."""
    t = table_of(s)
    pr = t.get("probe") or {}
    if pr.get("path"):
        st, h = http_get(s, pr, t, "/health")
        if not _answered(st, {"status": pr.get("status", 200)}):
            return info
        info["health"] = _scalars(h)
    elif not pid_listening(s["port"]):
        return info
    info["up"] = True
    sp = t.get("status") or {}
    if sp.get("argv"):
        rc, data, text = cli_status(s, t)
        if rc == 0:
            if sp.get("loaded_re"):
                info["loaded"] = bool(re.search(str(sp["loaded_re"]), text, re.M))
            else:
                info["loaded"] = holds(data, pred_part(sp))
    else:
        info["loaded"] = True
    info["state"] = "ready" if info["loaded"] else "idle"
    return info


def loads_at_start(s: dict) -> bool:
    """Does starting this unit put its model on the card? Declared on the unit, true of a launch preset, and true
    of a table driver with no way to tell loaded from up (one model for the life of the process)."""
    d = s["driver"]
    if (s.get("start") or {}).get("loads_at_start") or d == "strata-launch":
        return True
    if d == "http":
        return not isinstance(table_of(s).get("loaded", True), dict)
    if d == "cli":
        return not (table_of(s).get("status") or {}).get("argv")
    return False


def cli_call(p: Plan, st, s: dict, verb: str):
    """Run the table's command for load, unload, start or stop; a non-zero exit fails the plan."""
    spec = table_of(s).get(verb) or {}
    if not spec.get("argv"):
        raise PlanFailed(f"{s['name']} has no {verb} command in its table")
    p.say(st, " ".join(str(a) for a in spec["argv"]))
    try:
        rc, out, err = cli_run(s, spec)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PlanFailed(f"{verb} command failed to run: {e}") from None
    _CLI_CACHE.pop(s["id"], None)
    for line in (out + err).strip().splitlines()[-3:]:
        p.say(st, line[:160])
    if rc != 0:
        raise PlanFailed(f"{s['name']} {verb} exited {rc}: {(err or out).strip()[-200:]}")


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
        kind = s["driver"]
        try:
            if kind == "http":
                return http_probe(s, info)
            if kind == "cli":
                return cli_probe(s, info)
            if kind not in DRIVERS:
                info["health"] = {"status": f"unknown driver {kind!r}"}
                return info
            if kind == "ollama":
                st, ps = http("GET", base + "/api/ps", timeout=2.0)
                models = (ps or {}).get("models", []) if st == 200 else []
                info.update(up=True, loaded=bool(models), state="loaded" if models else "idle",
                            models=[{"name": m.get("name"), "size_vram_gib": round((m.get("size_vram") or 0) / 2**30, 1),
                                     "size_gib": round((m.get("size") or 0) / 2**30, 1),
                                     "expires_at": m.get("expires_at")} for m in models])
                return info
            if kind == "llamacpp-router":
                # the router's /health says ok with nothing loaded: a model's own status is the only truth
                models = router_models(port)
                if models is None:
                    return info
                info["up"] = True
                status = models.get(s.get("router_model") or "")
                if status is None:
                    info["health"] = {"status": "not offered by the router"}
                    return info
                value = str(status.get("value") or "")
                ctx = router_arg(status, "--ctx-size") or ""
                info["health"] = {"status": value, "model": s.get("router_model"), **({"max_context": int(ctx)} if ctx.isdigit() else {})}
                info["loaded"] = value == "loaded"
                child = router_arg(status, "--port") or ""
                if info["loaded"] and child.isdigit():
                    try:
                        st2, slots = http("GET", f"http://127.0.0.1:{child}/slots", timeout=1.0)
                        if st2 == 200 and isinstance(slots, list):
                            info["busy"] = any(isinstance(x, dict) and x.get("is_processing") for x in slots)
                    except Exception:  # noqa: BLE001 - busy only guards an unload; the state stands without it
                        pass
                info["state"] = "busy" if info["busy"] else "ready" if info["loaded"] else "loading" if value == "loading" else "idle"
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
        holders = [sid for sid, i in snap["servers"].items() if i.get("loaded") and INV.servers[sid]["driver"] != "ollama"]
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
        try:
            aq = art_queue()
            if aq:
                snap["art"] = aq.summary()
        except Exception as e:  # noqa: BLE001 - the queue is decoration for the poll; a queue_dir that cannot be made must not kill it
            snap["art"] = {"error": str(e)[:160]}
            snap["warnings"].append(f"art queue: {str(e)[:120]}")
        snap["plan"] = PLANS.current_public()
        snap["inventory_rev"] = INV.data.get("rev", 1)
        snap["engines"] = {}
        for e in INV.engines:
            mine = [i for sid, i in snap["servers"].items() if INV.servers[sid]["engine"] == e["id"]]
            rp = recipe_for(e)
            snap["engines"][e["id"]] = {"version": engine_version(e), "slot": e["slot"], "units": len(mine),
                                        "up": sum(1 for i in mine if i.get("up")), "loaded": sum(1 for i in mine if i.get("loaded")),
                                        "recipe": rp.get("recipe") or "none", "recipe_note": rp.get("note")}
        snap["clients"] = {}
        for c in INV.clients.values():
            rp = recipe_for(c)
            ports = []
            for b in c.get("bindings") or []:
                m = re.search(r":(\d+)(?:/|$)", str(b.get("url", "")))
                if m:
                    ports.append(int(m.group(1)))
            snap["clients"][c["id"]] = {"version": engine_version(c) if c.get("version_cmd") else c.get("version"), "ports": sorted(set(ports)),
                                        "recipe": rp.get("recipe") or "none", "recipe_note": rp.get("note")}
        snap["updates"] = {uid: settled(u, (snap["engines"].get(uid) or snap["clients"].get(uid) or {}).get("version"))
                           for uid, u in UPDATES.items()}
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
            except Exception as e:  # noqa: BLE001
                log("poll error: " + traceback.format_exc()[-600:])
                # the page must say so rather than show dashes: publish the last snapshot with the failure on it
                with self.lock:
                    broken = dict(self.snapshot)
                broken["time"] = time.time()
                broken["warnings"] = [f"the agent's poll failed: {type(e).__name__}: {str(e)[:160]} (logs/agent.log has the trace)"]
                with self.lock:
                    self.snapshot = broken
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
    kind = s["driver"]
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
        elif kind == "llamacpp":
            # llama-server holds its one model for its whole life: unloading it is ending the process
            p.say(st, "llama-server loads one model at start and keeps it; unloading is stopping the process")
            p.done_step(st)
            op_stop_process(p, sid)
        elif kind == "llamacpp-router":
            # the router ends this model's child process and stays up for the others
            code, r = http("POST", base + "/models/unload", body={"model": s["router_model"]}, timeout=60)
            _ROUTER_CACHE.pop(s["port"], None)
            p.say(st, f"/models/unload {s['router_model']} -> {code} {json.dumps(r)[:160]}")
            if code >= 400:
                raise PlanFailed(f"{name} refused to unload: {json.dumps(r)[:200]}")
            _wait(p, st, lambda: (not fresh_info(sid).get("loaded"), "still loaded"), s["timeouts"].get("unload_s", 60), what=f"{name} to release the card")
        elif kind == "maya":
            code, r = http("POST", base + "/models/unload", body={}, timeout=60)
            p.say(st, f"/models/unload -> {code} {json.dumps(r)[:160]}")
            if code >= 400:
                raise PlanFailed(f"{name} refused to unload: {json.dumps(r)[:200]}")
            _wait(p, st, lambda: (not fresh_info(sid).get("loaded"), "still loaded"), s["timeouts"].get("unload_s", 60), what=f"{name} to release the card")
        elif kind == "http":
            t = table_of(s)
            if not t.get("unload"):
                p.say(st, f"{name} holds its model for the life of the process; unloading is stopping it")
                p.done_step(st)
                op_stop_process(p, sid)
            else:
                http_call(p, st, s, t["unload"], t, "unload")
                _wait(p, st, lambda: (not fresh_info(sid).get("loaded"), "still loaded"), s["timeouts"].get("unload_s", 90), what=f"{name} to release the card")
        elif kind == "cli":
            if not (table_of(s).get("unload") or {}).get("argv"):
                p.say(st, f"{name} holds its model for the life of the process; unloading is stopping it")
                p.done_step(st)
                op_stop_process(p, sid)
            else:
                cli_call(p, st, s, "unload")
                _wait(p, st, lambda: (not fresh_info(sid).get("loaded"), "still loaded"), s["timeouts"].get("unload_s", 90), what=f"{name} to release the card")
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
    drv = s["driver"]
    if drv == "strata-launch":
        return op_stop_launch(p, sid)
    if drv == "ollama":
        raise PlanFailed("Ollama is a service; unload its models instead")
    if drv == "llamacpp-router":
        raise PlanFailed(f"{s['name']} is one model of the llama.cpp router on :{s['port']}; Unload ends its process, and the router, "
                         "which serves the others, is not stopped from here")
    unit = (s.get("start") or {}).get("systemd_user")
    cli_stop = drv == "cli" and (table_of(s).get("stop") or {}).get("argv")
    st = p.step(f"Stop {s['name']} process", f"systemd user unit {unit}" if unit else "its own stop command" if cli_stop
                else f"the process listening on {s['port']}")
    STATE.set_transient(sid, "stopping")
    try:
        if unit:
            # a killed service is a clean exit to systemd (no restart) and a stale state; let it stop its own
            systemctl_user(p, st, "stop", unit, timeout=s["timeouts"].get("unload_s", 60) + 30)
            _wait(p, st, lambda: (not fresh_info(sid).get("up"), "port still open"), 60, what=f"{s['name']} to exit")
            p.done_step(st)
            return
        if cli_stop:
            cli_call(p, st, s, "stop")
            _wait(p, st, lambda: (not fresh_info(sid).get("up"), "port still open"), 60, what=f"{s['name']} to exit")
            p.done_step(st)
            return
        pids = pid_listening(s["port"])
        if not pids:
            p.done_step(st, "nothing listening")
            return
        for pid in pids:
            p.check()
            p.say(st, f"end {pid}: {host.kill_tree(pid)[:120]}")
        _wait(p, st, lambda: (not fresh_info(sid).get("up"), "port still open"), 60, what=f"{s['name']} to exit")
        # the engine child sometimes outlives the server (seen with maya.exe): one more sweep by name
        names = {"maya": host.exe_names(("maya", "maya-vision")), "strata": host.exe_names(("strata-vision",))}.get(drv, ())
        if drv in ("http", "cli"):
            names = tuple(n.lower() for n in host.exe_names(tuple(table_of(s).get("procs") or ())))
        procs = host.process_list(names) if names else {}
        for n in names:
            for pid in procs.get(n, []):
                if drv in ("maya", "http", "cli") or len(INV.servers) == 1:
                    host.kill(pid)
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
        if s["driver"] == "strata-launch":
            argv = [s["python"], s["strata_py"], "launch", "--preset", s["preset"], "--no-tui", "--yes"]
            p.say(st, "strata launch --preset " + s["preset"] + " --no-tui --yes")
            out_path = LOG_DIR / f"{sid}.out.log"
            with out_path.open("ab") as out:
                out.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} quartermaster start\n".encode())
                proc = host.popen_detached(argv, cwd=s["cwd"], stdout=out)
            # launch returns once it serves; we wait on health, not on the process
            _wait(p, st, lambda: (fresh_info(sid).get("up"), f"launch pid {proc.pid} {'alive' if proc.poll() is None else 'exited ' + str(proc.returncode)}"),
                  s["timeouts"]["start_s"], what=f"{s['name']} to open port {s['port']}")
        elif (s.get("start") or {}).get("systemd_user"):
            # a server systemd keeps (a router most often): start its unit, never a second copy beside it
            systemctl_user(p, st, "start", s["start"]["systemd_user"])
            _wait(p, st, lambda: (fresh_info(sid).get("up"), "waiting for the port"), s["timeouts"].get("start_s", 120),
                  what=f"{s['name']} to open port {s['port']}")
        elif s["driver"] == "cli" and (table_of(s).get("start") or {}).get("argv"):
            # the program's own start command (lms server start, foundry service start) returns once it serves
            cli_call(p, st, s, "start")
            _wait(p, st, lambda: (fresh_info(sid).get("up"), "waiting for the port"), s["timeouts"].get("start_s", 120),
                  what=f"{s['name']} to open port {s['port']}")
        elif not (s.get("start") or {}).get("argv"):
            raise PlanFailed(f"{s['name']} is down and the inventory gives no way to start it")
        else:
            p.say(st, " ".join(s["start"]["argv"]))
            out_path = LOG_DIR / f"{sid}.out.log"
            with out_path.open("ab") as out:
                out.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} quartermaster start\n".encode())
                proc = host.popen_detached(s["start"]["argv"], cwd=s["start"].get("cwd"), stdout=out)

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
        if s["driver"] == "strata":
            # /v1/load holds the request until the model is up; fire it on a thread and poll health meanwhile

            def fire():
                try:
                    result["r"] = http("POST", base + "/v1/load", body={}, timeout=s["timeouts"]["load_s"] + 30)
                except Exception as e:  # noqa: BLE001
                    result["r"] = (0, str(e))
            threading.Thread(target=fire, daemon=True).start()
            p.say(st, "/v1/load sent")
        elif s["driver"] == "maya":

            def fire():
                try:
                    result["r"] = http("POST", base + "/models/load", body={}, timeout=s["timeouts"]["load_s"] + 30)
                except Exception as e:  # noqa: BLE001
                    result["r"] = (0, str(e))
            threading.Thread(target=fire, daemon=True).start()
            p.say(st, "/models/load sent")
        elif s["driver"] == "llamacpp-router":
            # answers at once; the model's status in GET /models goes loading -> loaded
            code, r = http("POST", base + "/models/load", body={"model": s["router_model"]}, timeout=60)
            _ROUTER_CACHE.pop(s["port"], None)
            p.say(st, f"/models/load {s['router_model']} -> {code} {json.dumps(r)[:120]}")
            if code >= 400:
                result["r"] = (code, r)
        elif s["driver"] == "http":
            t = table_of(s)
            if not t.get("load"):
                raise PlanFailed(f"{s['name']} is up without its model and its table has no load call; stop and start it")
            http_call(p, st, s, t["load"], t, "load", result)
        elif s["driver"] == "cli":
            cli_call(p, st, s, "load")
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
        if s["driver"] == "maya":
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
            if INV.servers[sid]["driver"] == "ollama" and not INV.policy.get("unload_ollama_on_resident", True):
                continue
            out.append(sid)
    return out


def op_make_resident(p: Plan):
    sid = p.target
    if sid not in INV.servers or INV.servers[sid]["driver"] == "ollama":
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


def op_stop_all(p: Plan):
    """End every running unit of one engine (the band's menu). Ollama and a llama.cpp router's models are served
    by a process that is not theirs to end, and are skipped."""
    if not p.args.get("confirm"):
        raise PlanFailed("stop_all ends server processes: send confirm=true")
    if p.target not in INV.engine_by_id:
        raise PlanFailed("unknown engine")
    st = STATE.get()["servers"]
    ups = [sid for sid, s in INV.servers.items() if s["engine"] == p.target and s["driver"] not in UNSTOPPABLE and st.get(sid, {}).get("up")]
    if not ups:
        s = p.step("Nothing running", INV.engine_by_id[p.target]["name"])
        p.done_step(s)
        return
    for sid in ups:
        p.check()
        op_stop_process(p, sid)


def op_check_update(p: Plan):
    """Ask the engine's or client's recipe whether something newer exists. Network, on this click only."""
    entry, role = updatable(p.target)
    if not entry:
        raise PlanFailed("unknown engine or client")
    rp, ctx = recipe_for(entry), update_ctx(entry, role)
    st = p.step(f"Check {ctx['name']} for an update", f"recipe {rp.get('recipe') or 'none'}")
    r = updaters.check(rp, ctx)
    remember_update(p.target, r)
    if not r.get("ok"):
        raise PlanFailed(r.get("error") or "check failed")
    if r.get("available"):
        p.done_step(st, f"{r.get('installed') or '?'} -> {r.get('latest')}" + (" (Update applies it)" if r.get("can_apply") else " (by hand)"))
    elif r.get("available") is None:
        p.done_step(st, r.get("note") or "no comparison possible")
    else:
        p.done_step(st, f"{r.get('installed') or '?'} is current")


def op_apply_update(p: Plan):
    """Move an engine or client to the version the last check found: back up, apply, verify, roll back on failure."""
    if not p.args.get("confirm"):
        raise PlanFailed("apply_update changes an installation: send confirm=true")
    entry, role = updatable(p.target)
    if not entry:
        raise PlanFailed("unknown engine or client")
    rp, ctx = recipe_for(entry), update_ctx(entry, role)
    last = UPDATES.get(p.target) or {}
    if role == "engine":
        up = [sid for sid, s in INV.servers.items() if s["engine"] == p.target and STATE.get()["servers"].get(sid, {}).get("up")]
        if up:
            raise PlanFailed(f"stop {entry.get('name', p.target)}'s running units first: {', '.join(up)}")
    if not last.get("can_apply") and (rp.get("recipe") not in ("self",)):
        raise PlanFailed("run Check first; this recipe can be applied only from a check that found a newer version")
    now = ctx.get("installed")
    if last.get("available") and now and not updaters.newer(last.get("latest"), now):
        raise PlanFailed(f"{ctx['name']} is already at {now}, moved outside Quartermaster since the check; run Check again")
    st = p.step(f"Update {ctx['name']}", f"{last.get('installed') or ctx.get('installed') or '?'} -> {last.get('latest') or 'its own newest'} ({rp.get('recipe')})")
    r = updaters.apply(rp, ctx, last.get("latest"))
    if r.get("backup"):
        p.say(st, f"backup: {r['backup']}")
    if not r.get("ok"):
        if r.get("code_updated") and r.get("backup"):
            p.say(st, "the update broke part way; rolling back")
            rb = updaters.rollback(rp, ctx, r["backup"])
            p.say(st, f"rollback: {'ok' if rb.get('ok') else rb.get('error')}")
        raise PlanFailed(r.get("error") or "update failed")
    _ENGINE_VER.pop(p.target, None)
    probed = any(entry.get(k) for k in ("version_cmd", "version_file", "version_url"))
    after = update_ctx(entry, role).get("installed") if probed else (r.get("after") or last.get("latest") or ctx.get("installed"))
    remember_update(p.target, {**last, "installed": after, "available": False, "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"), "backup": r.get("backup")})
    p.done_step(st, f"now {after or r.get('after') or '?'}" + (f"; {r['note']}" if r.get("note") else ""))


def op_rollback_update(p: Plan):
    if not p.args.get("confirm"):
        raise PlanFailed("rollback_update changes an installation: send confirm=true")
    entry, role = updatable(p.target)
    if not entry:
        raise PlanFailed("unknown engine or client")
    rp, ctx = recipe_for(entry), update_ctx(entry, role)
    last = UPDATES.get(p.target) or {}
    if not last.get("backup"):
        raise PlanFailed("no backup recorded for the last update")
    st = p.step(f"Roll back {ctx['name']}", str(last["backup"]))
    r = updaters.rollback(rp, ctx, last["backup"])
    if not r.get("ok"):
        raise PlanFailed(r.get("error") or "rollback failed")
    _ENGINE_VER.pop(p.target, None)
    remember_update(p.target, {**last, "installed": update_ctx(entry, role).get("installed"), "backup": None, "rolled_back_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    p.done_step(st, r.get("note") or "done")


def op_start(p: Plan):
    if p.target not in INV.servers:
        raise PlanFailed("unknown server")
    s = INV.servers[p.target]
    if s["driver"] == "ollama":
        raise PlanFailed("Ollama is a service")
    # starting a server that loads at start is a load: the card must be clear
    at_start = loads_at_start(s)
    if at_start:
        hs = holders(exclude=p.target)
        if hs and not p.args.get("force"):
            raise PlanFailed(f"{', '.join(INV.servers[h]['name'] for h in hs)} holds the card; use Make resident, or unload first")
    op_start_server(p, p.target)
    if at_start:
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
    if not s or s["driver"] != "maya":
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
    if not s or s["driver"] != "maya":
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
        host.popen_detached(sh["start"]["argv"], cwd=sh["start"].get("cwd"), stdout=out)

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
        p.say(st, f"end {pid}: {host.kill_tree(pid)}")
    p.done_step(st)


def op_preset(p: Plan):
    pr = INV.presets.get(p.target)
    if not pr:
        raise PlanFailed("unknown preset")
    head = p.step(f"Preset: {pr['name']}", pr.get("desc", ""))
    p.done_step(head)
    for step in pr["steps"]:
        p.check()
        spec = PRESET_OPS.get(step["op"])
        if not spec:
            raise PlanFailed(f"a preset may not run {step['op']!r}")
        args = {"value": step.get("value"), "force": p.args.get("force")}
        if spec.get("confirm"):
            args["confirm"] = True          # the person confirmed it when they saved the preset
        sub = Plan(step["op"], step.get("target"), args, p.by)
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
#
# The systemd driver (Linux, 2026-11-07) keeps that contract: each verb names a unit, and the agent asks
# systemd to start it with --no-ask-password, at system or user scope. A verb names either a sealed control
# unit the owner installed (a oneshot that does the job and writes the same control-log lines the tasks do)
# or, as {"unit": ..., "verb": "start"|"stop"|"restart"}, the app's own unit. Whatever walls OpenWebUI off
# lives beneath those units; the agent knows their names and their log, nothing else. Who may start them is
# polkit's to say: a unit that wants a password fails at once, since nobody can type one for this process.

# Task names come from inventory.json (openwebui.tasks), never from a request. The inventory is already the
# trust root for every argv the agent runs, so naming the tasks there adds nothing a request could reach.
OWUI_TASK_DEFAULTS = {"start": "owui-ctl-start", "stop": "owui-ctl-stop", "update": "owui-ctl-update"}


def owui_tasks() -> dict:
    return {**OWUI_TASK_DEFAULTS, **((INV.openwebui or {}).get("tasks") or {})}


def owui_driver() -> str:
    o = INV.openwebui or {}
    return o.get("driver") or ("systemd" if o.get("units") else "schtasks")


def owui_units() -> dict:
    """verb -> (unit, systemctl verb) for the systemd driver. A verb the inventory leaves out has no control."""
    out = {}
    for v, u in ((INV.openwebui or {}).get("units") or {}).items():
        unit, sv = (u, "start") if isinstance(u, str) else ((u or {}).get("unit"), (u or {}).get("verb", "start"))
        if v in ("start", "stop", "update") and unit and sv in ("start", "stop", "restart"):
            out[v] = (unit, sv)
    return out


def owui_controls() -> dict:
    """verb -> the name of what it runs: a scheduled task, or a unit."""
    return {v: u for v, (u, _) in owui_units().items()} if owui_driver() == "systemd" else owui_tasks()


def owui_systemctl() -> list:
    return ["systemctl"] + (["--user"] if (INV.openwebui or {}).get("scope") == "user" else [])


def owui_install_hint() -> str:
    return (INV.openwebui or {}).get("install_hint") or (
        "install the units named in openwebui.units (contrib/openwebui-systemd)" if owui_driver() == "systemd"
        else "install the scheduled tasks named in openwebui.tasks")


def owui_step(verb: str, default: str) -> str:
    """The step's words: the inventory's openwebui.steps.<verb> describes this installation's scheme, if it likes."""
    return ((INV.openwebui or {}).get("steps") or {}).get(verb) or default


def owui_task_state(verb: str) -> str:
    """'run': this process may run it. 'gated': it exists and Windows wants elevation. 'missing'.
    A unit reads 'run' once systemd has it loaded; whether polkit lets this user start it shows on the first try."""
    if owui_driver() == "systemd":
        u = owui_units().get(verb)
        if not u:
            return "missing"
        try:
            rc, out, _ = run(owui_systemctl() + ["show", u[0], "-p", "LoadState", "--value"], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return "missing"
        return "run" if rc == 0 and out.strip() == "loaded" else "missing"
    if not host.WINDOWS:
        return "missing"                      # the scheduled-task driver is Windows only
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
    bound = host.ports_bound_on(ip)
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
            info["tasks"] = {v: owui_task_state(v) for v in ("start", "stop", "update")}
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
    log = (INV.openwebui or {}).get("ctl_log")            # a unit driven directly has none
    try:
        return Path(log).stat().st_size if log else 0
    except OSError:
        return 0


def _owui_log_since(offset: int) -> list[str]:
    log = (INV.openwebui or {}).get("ctl_log")
    if not log:
        return []
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() < offset:
                offset = 0                       # the log was replaced under us
            f.seek(offset)
            return f.read(512 * 1024).decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _owui_fire(p: Plan, st, verb: str, timeout: float = 60) -> int:
    """Start one of the three controls. Returns the control log's size from before it ran."""
    name = owui_controls().get(verb) or verb
    state = owui_task_state(verb)
    before = _owui_log_size()
    if state == "missing":
        raise PlanFailed(f"{name} is not installed: {owui_install_hint()}")
    if owui_driver() == "systemd":
        unit, sv = owui_units()[verb]
        # with a control log the log says when the job is done; without one, systemctl waits for the job itself
        argv = owui_systemctl() + ["--no-ask-password"] + (["--no-block"] if INV.openwebui.get("ctl_log") else []) + [sv, unit]
        try:
            rc, out, err = run(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise PlanFailed(f"systemctl {sv} {unit} did not return in {int(timeout)} s; the job may still be running")
        if rc != 0:
            msg = (err or out).strip()
            if "authentication required" in msg.lower() or "access denied" in msg.lower():
                raise PlanFailed(f"polkit wants a password to {sv} {unit}, and nobody can type one for this process: {owui_install_hint()}")
            raise PlanFailed(f"systemctl {sv} {unit}: {msg[:200]}")
        p.say(st, f"{unit}: {sv} asked")
        return before
    if state == "run":
        rc, out, err = run(["schtasks.exe", "/Run", "/TN", name], timeout=30)
        if rc != 0:
            raise PlanFailed(f"schtasks /Run {name}: {(err or out).strip()[:200]}")
    else:
        # ShellExecute "runas": Windows shows its consent prompt on the secure desktop and this call returns
        # when a person has answered it. Nothing this process sends can answer it.
        p.say(st, "waiting for your answer to the Windows elevation prompt")
        if not host.WINDOWS:
            raise PlanFailed("elevation through the consent prompt is Windows only")
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        r = shell32.ShellExecuteW(None, "runas", "schtasks.exe", f'/Run /TN "{name}"', None, 0)
        if int(r or 0) <= 32:
            raise PlanFailed("the elevation prompt was declined or timed out; nothing was run")
    p.say(st, f"task {name} asked to run")
    return before


def _owui_run(p: Plan, st, verb: str, timeout: float) -> int:
    """Run one control to its end and return its code: the control log's done line where there is a log,
    systemctl's own wait where a unit has none."""
    before = _owui_fire(p, st, verb, timeout)
    if owui_driver() == "systemd" and not INV.openwebui.get("ctl_log"):
        return 0                                  # systemctl waited, and would have failed the fire otherwise
    return _owui_wait_task(p, st, verb, before, timeout)


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
        return any(started.search(l) for l in new_lines()), "nothing in the control log yet"

    def has_finished():
        lines = new_lines()
        for l in lines:
            m = done.search(l)
            if m:
                return True, int(m.group(1))
        return False, ""

    name = owui_controls().get(verb) or verb
    _wait(p, st, has_started, 60, every=1.5, what=f"{name} to start")
    return _wait(p, st, has_finished, timeout, every=2.0, what=f"{name} to finish")


def _owui_wait_health(p: Plan, want_up: bool, timeout: float):
    st = p.step("Wait for /health" if want_up else "Wait for the port to close")
    _wait(p, st, lambda: (owui_health() == want_up, "answering" if not want_up else "not answering yet"),
          timeout, every=3.0, what="OpenWebUI /health")
    OWUI_WATCH.refresh(slow=True)
    return st


def op_owui_start(p: Plan):
    _owui_need()
    st = p.step("Start OpenWebUI", owui_step("start", "through its start control"))
    if owui_health():
        return p.done_step(st, "already up")
    rc = _owui_run(p, st, "start", 180)
    if rc != 0:
        raise PlanFailed(f"the start control ended with code {rc}; see the OpenWebUI control log")
    p.done_step(st, "started")
    p.done_step(_owui_wait_health(p, True, 300), "200")


def op_owui_stop(p: Plan):
    _owui_need()
    if not p.args.get("confirm"):
        raise PlanFailed("stop takes OpenWebUI down for everyone using it: send confirm=true")
    st = p.step("Stop OpenWebUI", owui_step("stop", "through its stop control; Start, or a reboot, brings it back"))
    if not owui_health():
        return p.done_step(st, "already down")
    rc = _owui_run(p, st, "stop", 180)
    if rc != 0:
        raise PlanFailed(f"the stop control ended with code {rc}; see the OpenWebUI control log")
    p.done_step(st, "stopped")
    p.done_step(_owui_wait_health(p, False, 90), "down")


def op_owui_update(p: Plan):
    _owui_need()
    if not p.args.get("confirm"):
        raise PlanFailed("update replaces the installed version and may migrate the database one way: send confirm=true")
    st = p.step("Update OpenWebUI", owui_step("update", "through its update control, which backs up and upgrades as it was written to")
                + ". Cancel stops the watching, not the update.")
    if not owui_health():
        raise PlanFailed("OpenWebUI is not up; start it first")
    was = (OWUI_WATCH.get() or {}).get("version")
    rc = _owui_run(p, st, "update", 2400)
    if rc != 0:
        raise PlanFailed(f"the update control ended with code {rc}; see the OpenWebUI control log")
    p.done_step(st, "update finished")
    st2 = _owui_wait_health(p, True, 300)
    now = (OWUI_WATCH.get() or {}).get("version")
    p.done_step(st2, f"already current ({now})" if now == was else f"{was or '?'} -> {now or '?'}")


# ---- ComfyUI: a service that shares the card ---------------------------------------
# It is the owner's own process, often started by hand. The agent starts exactly the argv in the inventory, stops only a process on its port whose command line names ComfyUI, and asks
# ComfyUI itself to drop its models (POST /free). It reads two small things from ComfyUI's API
# (/system_stats, and the queue count from /prompt) and nothing of the user's workflows, queue contents
# or outputs. How much of the card it holds comes from Windows, not from ComfyUI.

COMFY_HOLD_MIB = 1024          # dedicated VRAM above this counts as "ComfyUI holds part of the card" (idle is ~0.2 GiB)
_COMFY_PID = {"pid": None, "at": 0.0}
# A process's dedicated GPU memory, in MiB: Windows' own counter (ComfyUI's /system_stats cannot be used for this:
# under DynamicVRAM it reported 64 MiB reserved with 27.4 GB on the card, 2026-11-05, and nvidia-smi gives no
# per-process figure under WDDM); nvidia-smi's compute-apps list on Linux.
gpu_dedicated_mib = host.gpu_dedicated_mib
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
    return [f"{a}:{port}" for a in host.binds(port)]


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


_cmdline = host.cmdline


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
        proc = host.popen_detached(argv, cwd=c["start"].get("cwd"), stdout=out, env=env)

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
        p.say(st, f"end {pid}: {host.kill_tree(pid)[:120]}")
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
    argv = [host.python_console(), str(HERE / "comfy_update.py"), mode, u["repo"], u["python"], *extra, pins]
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
    root = Path(INV.art.get("queue_dir") or ((ROOT / "artq") if HOME == HERE else (HOME / "artq")))
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
    prior = next((h for h in hs if INV.servers[h]["driver"] != "ollama"), None)
    then = str(p.args.get("then") or "prior")
    if then == "prior":
        back = prior
    elif then == "none":
        back = None
    elif then in INV.servers and INV.servers[then]["driver"] != "ollama":
        back = then
    else:
        raise PlanFailed("then must be prior, none, or a server id")
    force = bool(p.args.get("force"))
    for h in hs:
        if INV.servers[h]["driver"] != "ollama" and fresh_info(h).get("busy") and not force:
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


# ---- presets: the one thing the page may write ------------------------------------
# A preset is a list of steps over these ops and the inventory's ids, nothing else; the page's editor offers
# exactly this table (GET /api/presets serves it) and the agent checks a saved preset against it again.
# Updates, the art queue and presets themselves are not in it: those have their own doors.
PRESET_OPS = {
    "make_resident": {"target": "model", "label": "Make resident"},
    "unload": {"target": "unit", "label": "Unload"},
    "unload_all": {"target": None, "label": "Clear the card"},
    "start": {"target": "model", "label": "Start the server"},
    "stop": {"target": "model", "label": "Stop the server (ends the process)", "confirm": True},
    "stop_all": {"target": "engine", "label": "Stop every unit of an engine", "confirm": True},
    "park": {"target": "parkable", "label": "Park"},
    "unpark": {"target": "parkable", "label": "Unpark"},
    "set_context": {"target": "maya", "value": "context", "label": "Set context"},
    "set_budget": {"target": "maya", "value": "budget", "label": "Set thinking budget"},
    "start_shim": {"target": "shim", "label": "Start a shim"},
    "stop_shim": {"target": "shim", "label": "Stop a shim"},
    "comfy_start": {"target": None, "needs": "comfyui", "label": "Start ComfyUI"},
    "comfy_free": {"target": None, "needs": "comfyui", "label": "ComfyUI: drop its models"},
    "comfy_stop": {"target": None, "needs": "comfyui", "label": "Stop ComfyUI", "confirm": True},
    "owui_start": {"target": None, "needs": "openwebui", "label": "Start OpenWebUI"},
    "owui_stop": {"target": None, "needs": "openwebui", "label": "Stop OpenWebUI"},
}
PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def preset_targets(kind: str) -> list[str]:
    """The ids a step of this target kind may name."""
    if kind == "unit":
        return list(INV.servers)
    if kind == "model":
        return [sid for sid, s in INV.servers.items() if s["driver"] != "ollama"]
    if kind == "parkable":
        return [sid for sid, s in INV.servers.items() if s.get("parkable")]
    if kind == "maya":
        return [sid for sid, s in INV.servers.items() if s["driver"] == "maya"]
    if kind == "shim":
        return list(INV.shims)
    if kind == "engine":
        return [e["id"] for e in INV.engines]
    return []


def validate_preset(raw) -> dict:
    """A preset as the page sent it, checked against PRESET_OPS and the inventory. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("a preset is an object")
    pid = str(raw.get("id") or "").strip().lower()
    if not PRESET_ID.match(pid):
        raise ValueError("id: lower-case letters, digits and dashes, 1-40 characters")
    name = str(raw.get("name") or "").strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("name: 1-60 characters")
    desc = str(raw.get("desc") or "").strip()[:200]
    steps_in = raw.get("steps")
    if not isinstance(steps_in, list) or not 1 <= len(steps_in) <= 12:
        raise ValueError("steps: 1 to 12 of them")
    steps = []
    for i, st in enumerate(steps_in, 1):
        if not isinstance(st, dict):
            raise ValueError(f"step {i}: an object")
        op = st.get("op")
        spec = PRESET_OPS.get(op)
        if not spec:
            raise ValueError(f"step {i}: {op!r} is not an op a preset may run")
        if spec.get("needs") and not getattr(INV, spec["needs"]):
            raise ValueError(f"step {i}: {op} needs the inventory's {spec['needs']} block")
        out = {"op": op}
        if spec["target"]:
            tgt = st.get("target")
            if tgt not in preset_targets(spec["target"]):
                raise ValueError(f"step {i}: {op} needs one of {preset_targets(spec['target'])}")
            out["target"] = tgt
        val = st.get("value")
        if spec.get("value") == "context":
            choices = INV.servers[out["target"]].get("context_choices") or []
            if val not in choices:
                raise ValueError(f"step {i}: context must be one of {choices}")
            out["value"] = int(val)
        elif spec.get("value") == "budget":
            try:
                n = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"step {i}: budget is a number") from None
            if n != 0 and not 256 <= n <= 262144:
                raise ValueError(f"step {i}: budget is 0 or 256..262144")
            out["value"] = n
        steps.append(out)
    return {"id": pid, "name": name, "desc": desc, "steps": steps}


def save_presets(presets: list[dict]) -> None:
    """presets.json, written whole and atomically; the inventory file is never touched here."""
    tmp = PRESETS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"presets": presets}, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, PRESETS)
    INV.presets = {p["id"]: p for p in presets}


def presets_public() -> dict:
    return {"presets": list(INV.presets.values()),
            "ops": {op: {**spec, "targets": preset_targets(spec["target"]) if spec["target"] else [],
                         "available": not spec.get("needs") or bool(getattr(INV, spec["needs"]))}
                    for op, spec in PRESET_OPS.items()},
            "context_choices": {sid: s.get("context_choices") or [] for sid, s in INV.servers.items() if s["driver"] == "maya"}}


OPS = {"make_resident": op_make_resident, "unload": op_unload, "unload_all": op_unload_all, "stop": op_stop, "stop_all": op_stop_all,
       "start": op_start, "park": op_park, "unpark": op_unpark, "set_context": op_set_context,
       "set_budget": op_set_budget, "start_shim": op_start_shim, "stop_shim": op_stop_shim, "preset": op_preset,
       "owui_start": op_owui_start, "owui_stop": op_owui_stop, "owui_update": op_owui_update,
       "comfy_start": op_comfy_start, "comfy_stop": op_comfy_stop, "comfy_free": op_comfy_free,
       "art_batch": op_art_batch, "comfy_update": op_comfy_update, "comfy_rollback": op_comfy_rollback,
       "check_update": op_check_update, "apply_update": op_apply_update, "rollback_update": op_rollback_update}

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
            if rel != "fonts/local.css":
                return self._json(404, {"error": "not found"})
            body = NO_FONT_MAP
        else:
            body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(f.suffix.lower(), "application/octet-stream"))
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
        if path == "/api/presets":
            return self._json(200, presets_public())
        if path == "/api/catalog":
            return self._json(200, {"catalog": CATALOG})
        if path == "/api/suggestions":
            try:
                return self._json(200, {"suggestions": suggestions()})
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": f"suggestions: {e}"})
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
        if path == "/api/presets":
            try:
                pr = validate_preset(body.get("preset", body))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            kept = [p for p in INV.presets.values() if p["id"] != pr["id"]]
            was = pr["id"] in INV.presets
            try:
                save_presets(kept + [pr] if not was else [pr if p["id"] == pr["id"] else p for p in INV.presets.values()])
            except OSError as e:
                return self._json(500, {"error": f"presets.json: {e}"})
            self._art_log("preset_save", pr["id"], by, f"{'replaced' if was else 'created'} {pr['name']} ({len(pr['steps'])} steps)")
            return self._json(200, {"preset": pr, "replaced": was})
        if path == "/api/scan":
            try:
                r = scan_proposals()
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": f"scan: {e}"})
            self._art_log("scan", None, by, f"{len(r['engines'])} engines, {len(r['units'])} units, {len(r['services'])} services, {len(r['clients'])} clients not in the roster")
            return self._json(200, r)
        if path == "/api/presets/delete":
            pid = str(body.get("id") or "")
            if pid not in INV.presets:
                return self._json(404, {"error": "no such preset"})
            try:
                save_presets([p for p in INV.presets.values() if p["id"] != pid])
            except OSError as e:
                return self._json(500, {"error": f"presets.json: {e}"})
            self._art_log("preset_delete", pid, by, "")
            return self._json(200, {"deleted": pid})
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
                _ENGINE_VER.clear()
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"error": f"inventory did not load: {e}"})
            return self._json(200, {"ok": True, "units": list(INV.servers), "engines": [e["id"] for e in INV.engines],
                                    "presets": list(INV.presets)})
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
