"""Quartermaster setup: find what this machine runs and write the inventory.

  python agent/setup.py                 first run: host facts, a checklist of kinds, detection, review, write
  python agent/setup.py detect          later: re-scan and propose what the inventory does not have yet
  options: --yes (accept every proposal and default), --kinds strata,maya,... (skip the checklist),
           --skip id,id (leave these proposals out; for scripted runs with --yes),
           --home DIR (write the inventory, presets and key there; the agent reads it with QM_HOME=DIR),
           --force (setup over an existing inventory; the old one is kept as a .bak), --no-startup

What it reads: the OS (host name, CPU, RAM, nvidia-smi), the engines' own configuration files for ports,
model names and logs, listening ports and the command lines of the processes behind them, and a client's
configuration for the local URLs it points at. Files named like secrets are never opened. Nothing leaves
the machine; the owner confirms before anything is written. Stdlib only."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WIN = platform.system() == "Windows"
HOMEDIR = Path.home()
LOCAL = Path(os.environ.get("LOCALAPPDATA") or HOMEDIR / "AppData" / "Local")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
KINDS = [("strata", "Strata"), ("maya", "Maya"), ("ollama", "Ollama"), ("llamacpp", "llama.cpp (llama-server)"),
         ("comfyui", "ComfyUI"), ("openwebui", "OpenWebUI"), ("pi", "Pi coding agent"), ("codex", "Codex CLI"),
         ("claude", "Claude Code")]
SECRET_NAMES = re.compile(r"(auth|token|secret|credential|password|\.key$)", re.I)
ARGS = {"yes": False, "kinds": None, "home": None, "force": False, "startup": None, "mode": "setup", "skip": None}


# ---- small helpers ---------------------------------------------------------------

def run(args, timeout=20, cwd=None):
    try:
        p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def get_json(url, timeout=2.0):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "null")
    except Exception:  # noqa: BLE001
        return 0, None


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ask(question: str, default: bool = True) -> bool:
    if ARGS["yes"]:
        return default
    if not sys.stdin or not sys.stdin.isatty():
        sys.exit(f"no terminal to answer '{question}': run with --yes to accept the defaults")
    a = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not a else a.startswith("y")


def ask_text(question: str, default: str) -> str:
    if ARGS["yes"] or not sys.stdin or not sys.stdin.isatty():
        return default
    a = input(f"{question} [{default}] ").strip()
    return a or default


def which(name: str) -> str | None:
    return shutil.which(name)


def py_in(root: Path) -> Path | None:
    for v in (".venv", "venv"):
        p = root / v / ("Scripts/python.exe" if WIN else "bin/python")
        if p.exists():
            return p
    return None


def version_token(text: str) -> str:
    m = re.search(r"\d+\.\d+(?:\.\d+)*", text or "")
    return m.group(0) if m else (text.strip().splitlines()[0][:40] if text.strip() else "")


def fixed_drives() -> list[Path]:
    if not WIN:
        return [Path("/")]
    out = []
    for c in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if ctypes.windll.kernel32.GetDriveTypeW(f"{c}:\\") == 3:         # DRIVE_FIXED
            out.append(Path(f"{c}:\\"))
    return out


# ---- the host ----------------------------------------------------------------------

def ram_total_gib() -> float | None:
    if WIN:
        class M(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = M()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return round(m.ullTotalPhys / 1024 ** 3, 1)
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 ** 2, 1)
    except OSError:
        pass
    return None


def cpu_name() -> str:
    if WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown CPU"


def gpus() -> list[dict]:
    smi = which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if WIN else "/usr/bin/nvidia-smi")
    rc, out, _ = run([smi, "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"], timeout=15)
    found = []
    if rc == 0:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                try:
                    total = int(float(parts[2]))
                except ValueError:
                    continue
                found.append({"index": int(parts[0]), "name": parts[1], "total_mib": total,
                              "free_card_mib": 4096 if total >= 24000 else 2048,
                              "_free_card": "the card counts as free for a load when nvidia-smi memory.used is below this; raise it if the bare desktop sits higher"})
    return found


def host_facts() -> dict:
    ram = ram_total_gib()
    return {"host": socket.gethostname(), "os": platform.platform(), "cpu": cpu_name(), "cores": os.cpu_count(),
            "ram_total_gib": ram, "gpus": gpus()}


# ---- running processes: the universal fallback -----------------------------------------

def listening() -> dict[int, int]:
    """{port: pid} for TCP listeners."""
    ports = {}
    if WIN:
        rc, out, _ = run(["netstat.exe", "-ano", "-p", "tcp"], timeout=15)
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 5 and p[0] == "TCP" and p[3] == "LISTENING":
                try:
                    ports.setdefault(int(p[1].rsplit(":", 1)[1]), int(p[4]))
                except ValueError:
                    pass
    else:
        rc, out, _ = run(["ss", "-ltnp"], timeout=15)
        for line in out.splitlines()[1:]:
            p = line.split()
            m = re.search(r"pid=(\d+)", line)
            if len(p) >= 4 and m:
                try:
                    ports.setdefault(int(p[3].rsplit(":", 1)[1]), int(m.group(1)))
                except ValueError:
                    pass
    return ports


def cmdlines(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    if WIN:
        ps = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        flt = " -or ".join(f"$_.ProcessId -eq {int(p)}" for p in pids)
        rc, out, _ = run([ps, "-NoProfile", "-NonInteractive", "-Command",
                          f"Get-CimInstance Win32_Process | Where-Object {{ {flt} }} | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"], timeout=40)
        try:
            data = json.loads(out or "[]")
            data = data if isinstance(data, list) else [data]
            return {int(d["ProcessId"]): d.get("CommandLine") or "" for d in data if d}
        except (ValueError, KeyError, TypeError):
            return {}
    res = {}
    for p in pids:
        try:
            res[p] = Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            pass
    return res


def argv_of(pid: int, cmd: str) -> list[str]:
    """A process's argv: exact from /proc on Linux, split from its command line elsewhere."""
    if not WIN:
        try:
            return [a.decode("utf-8", "replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            pass
    return [a or b for a, b in re.findall(r"\"([^\"]+)\"|(\S+)", cmd)]


def router_list(port: int) -> list[dict]:
    """A llama.cpp router's GET /models: [{id, status: {value, args}, ...}], [] if it does not answer."""
    st, body = get_json(f"http://127.0.0.1:{port}/models")
    return [m for m in (body or {}).get("data", []) if isinstance(m, dict) and m.get("id")] if st == 200 and isinstance(body, dict) else []


def flag(args: list, name: str) -> str | None:
    args = [str(a) for a in args]
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def systemd_unit(pid: int) -> tuple[str, str] | None:
    """("user" | "system", "name.service") when systemd keeps this process, from its cgroup (Linux)."""
    if WIN:
        return None
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text().strip().splitlines()[-1].split(":", 2)[-1]
    except (OSError, IndexError):
        return None
    leaf = cg.rstrip("/").rsplit("/", 1)[-1]
    if not leaf.endswith(".service") or leaf.startswith("user@"):
        return None
    return ("user" if "/user@" in cg else "system"), leaf


def running_servers() -> list[dict]:
    """Recognisable engine processes behind listening ports: [{port, pid, kind, cmd}]. Two kinds are for telling
    apart only and no detector takes them: ollama-runner (Ollama's own llama-server, one per loaded model) and
    llamacpp-child (a model a llama.cpp router started on a port of its own)."""
    ports = listening()
    cl = cmdlines(sorted(set(ports.values())))
    found = []
    for port, pid in sorted(ports.items()):
        cmd = cl.get(pid, "")
        low = cmd.lower()
        kind = None
        if "serve\\server.py" in low and "--engine" in low and "strata" in low or "strata.exe" in low or "strata-dsv41" in low:
            kind = "strata"
        elif "maya.py" in low or "maya.exe" in low:
            kind = "maya"
        elif "llama-server" in low:
            # Ollama ships its runner as lib/ollama/llama-server: that one is Ollama's, not a llama.cpp install
            kind = "ollama-runner" if "ollama" in low.split("llama-server", 1)[0] else "llamacpp"
        elif "comfyui" in low and "main.py" in low:
            kind = "comfyui"
        elif "ollama" in low:
            kind = "ollama"
        if kind:
            found.append({"port": port, "pid": pid, "kind": kind, "cmd": cmd})
    children = set()
    for r in found:
        if r["kind"] != "llamacpp":
            continue
        st, props = get_json(f"http://127.0.0.1:{r['port']}/props")
        if (isinstance(props, dict) and props.get("role") == "router") or re.search(r"--models-(dir|preset)\b", r["cmd"]):
            r["kind"] = "llamacpp-router"
            for m in router_list(r["port"]):
                child = flag((m.get("status") or {}).get("args") or [], "--port") or ""
                if child.isdigit() and int(child):
                    children.add(int(child))
    for r in found:
        if r["kind"] == "llamacpp" and r["port"] in children:
            r["kind"] = "llamacpp-child"
    return found


def unit_key(u: dict) -> tuple:
    """What makes a unit the same unit for detect: its port, and for a router's model, which model."""
    return u.get("port"), u.get("router_model")


def quant_of(stem: str) -> str:
    m = re.search(r"(?:^|[-_.])(i?q\d(?:_[a-z0-9]+)*|bf16|f16|f32|mxfp4)$", stem, re.I)
    return m.group(1).upper() if m else ""


# ---- detectors ---------------------------------------------------------------------------

def detect_strata(seen: dict) -> dict:
    out = {"engines": [], "units": [], "notes": []}
    roots = [HOMEDIR / "Strata", LOCAL / "Strata", LOCAL / "Programs" / "Strata"]
    exe = which("strata")
    if exe:
        roots.insert(0, Path(exe).resolve().parent)
    root = next((r for r in roots if (r / "strata.py").exists() or (r / "engine" / "strata.exe").exists()), None)
    if not root:
        return out
    py = py_in(root) or Path(sys.executable)
    e = {"id": "strata", "kind": "strata", "name": "Strata", "root": str(root)}
    if (root / "strata.py").exists():
        e["version_cmd"] = [str(py), str(root / "strata.py"), "--version"]
    out["engines"].append(e)
    bats = {b: b.read_text(encoding="utf-8", errors="replace") for b in root.glob("run-*.bat")} if WIN else {}
    for cfg in sorted(root.glob("strata-*.json")):
        d = load_json(cfg)
        if not isinstance(d, dict) or not d.get("port"):
            continue
        uid = cfg.stem[len("strata-"):]
        bat = next((b for b, t in bats.items() if cfg.name in t), None)
        argv = [str(bat)] if bat else [str(py), str(root / "serve" / "server.py"), "--engine", "strata", "--config", str(cfg), "--port", str(d["port"])]
        idle = d.get("idle_unload_s")
        out["units"].append({
            "id": uid, "engine": "strata", "kind": "strata", "name": d.get("model_name") or uid, "model": d.get("model_name") or "",
            "model_id": d.get("model_name"), "context": None, "vision": bool(d.get("vision")), "quant": "", "port": int(d["port"]),
            "footprint": {"vram_gib": None, "ram_gib": None},
            "start": {"argv": argv, "cwd": str(d.get("cwd") or root), "loads_at_start": True},
            "log": d.get("log") or str(root / f"{cfg.stem}.log"), "config": str(cfg),
            "timeouts": {"start_s": 360, "load_s": 360, "unload_s": 90},
            "notes": f"idle_unload_s {idle}" + ("; loads on the first inference request from anyone" if idle == 0 else ""),
            "parkable": idle == 0, "source": ""})
        seen[int(d["port"])] = uid
    launch = LOCAL / "Strata" / "launch"
    if launch.is_dir():
        for pj in sorted(launch.glob("*.json")):
            if pj.name.endswith(".choices.json"):
                continue
            d = load_json(pj)
            if not isinstance(d, dict) or not d.get("port"):
                continue
            preset = pj.stem
            out["units"].append({
                "id": preset, "engine": "strata", "kind": "strata-launch", "name": d.get("model_name") or preset,
                "model": d.get("model_name") or "", "model_id": d.get("model_name"), "context": None, "vision": True, "quant": "",
                "port": int(d["port"]), "footprint": {"vram_gib": None, "ram_gib": None}, "preset": preset,
                "python": str(py), "strata_py": str(root / "strata.py"), "cwd": str(d.get("cwd") or root),
                "log": d.get("log") or str(launch / f"{preset}.engine.log"),
                "timeouts": {"start_s": 420, "load_s": 420, "unload_s": 200},
                "notes": "launch namespace: start = strata launch --preset --no-tui --yes; stop = strata launch --stop --preset. No lazy load: the server IS the load.",
                "source": ""})
            if int(d["port"]) in seen:
                out["notes"].append(f"launch preset {preset} shares port {d['port']} with {seen[int(d['port'])]}: keep the ones you use")
            seen.setdefault(int(d["port"]), preset)
    return out


def detect_maya(seen: dict) -> dict:
    out = {"engines": [], "units": [], "notes": []}
    roots = [HOMEDIR / "Maya", HOMEDIR / "project-maya", LOCAL / "Maya"]
    root = next((r for r in roots if (r / "maya.py").exists()), None)
    if not root:
        return out
    py = py_in(root) or Path(sys.executable)
    e = {"id": "maya", "kind": "maya", "name": "Maya", "root": str(root)}
    if (root / "VERSION").exists():
        e["version_file"] = str(root / "VERSION")
    out["engines"].append(e)
    bats = {b: b.read_text(encoding="utf-8", errors="replace") for b in root.glob("run-*.bat")} if WIN else {}
    for cfg in sorted(root.glob("maya-*.json")):
        d = load_json(cfg)
        if not isinstance(d, dict) or not d.get("port"):
            continue
        uid = cfg.stem[len("maya-"):]
        bat = next((b for b, t in bats.items() if cfg.name in t), None)
        argv = [str(bat), "--plain"] if bat else [str(py), str(root / "maya.py"), "--config", str(cfg), "--port", str(d["port"]), "--plain"]
        out["units"].append({
            "id": f"maya-{uid}", "engine": "maya", "kind": "maya", "name": d.get("model_name") or uid, "model": d.get("model_name") or "",
            "model_id": d.get("model_name"), "context": None, "vision": bool(d.get("vision")), "quant": "", "port": int(d["port"]),
            "footprint": {"vram_gib": None, "ram_gib": None},
            "start": {"argv": argv, "cwd": str(d.get("cwd") or root), "loads_at_start": True},
            "log": d.get("log") or str(root / f"{cfg.stem}.log"), "config": str(cfg),
            "timeouts": {"start_s": 240, "load_s": 240, "unload_s": 60, "reload_s": 300},
            "context_choices": [32768, 131072, 262144, 524288, 1048576],
            "notes": f"idle_unload_s {d.get('idle_unload_s')}. After an engine crash prefer stop + start over a settings reload.",
            "source": ""})
        seen[int(d["port"])] = f"maya-{uid}"
    return out


def detect_ollama(seen: dict) -> dict:
    out = {"engines": [], "units": [], "notes": []}
    exe = which("ollama") or next((str(p) for p in (LOCAL / "Programs" / "Ollama" / "ollama.exe", Path("/usr/local/bin/ollama")) if p.exists()), None)
    host = os.environ.get("OLLAMA_HOST", "")
    port = int(host.rsplit(":", 1)[1]) if re.search(r":\d+$", host) else 11434
    st, v = get_json(f"http://127.0.0.1:{port}/api/version")
    if not exe and st != 200:
        return out
    out["engines"].append({"id": "ollama", "kind": "ollama", "name": "Ollama", "version_url": f"http://127.0.0.1:{port}/api/version", "version_path": "version"})
    out["units"].append({"id": "ollama", "engine": "ollama", "kind": "ollama", "name": "Ollama", "engine_label": "ollama",
                         "model": "small models, loaded on demand", "port": port, "vision": False, "context": None,
                         "footprint": {"vram_gib": None, "ram_gib": None}, "timeouts": {"unload_s": 60},
                         "notes": "Service, always up. Loads on demand. Unload = /api/generate keep_alive 0 per loaded model."})
    seen[port] = "ollama"
    if st == 200:
        st2, tags = get_json(f"http://127.0.0.1:{port}/api/tags")
        n = len((tags or {}).get("models", [])) if st2 == 200 else None
        out["notes"].append(f"Ollama {v.get('version') if isinstance(v, dict) else ''} answers on :{port}" + (f" with {n} models" if n is not None else ""))
    else:
        out["notes"].append(f"Ollama is installed ({exe}) but not answering on :{port}")
    return out


def start_for(r: dict, argv: list[str]) -> tuple[dict, str]:
    """How the agent should start a server it found running: its systemd user unit when systemd keeps it (never
    a second copy beside the unit), nothing for a system unit (that is root's), else the argv it runs with."""
    su = systemd_unit(r["pid"])
    if su and su[0] == "user":
        return {"systemd_user": su[1]}, f" (systemd user unit {su[1]})"
    if su:
        return {}, f" (system unit {su[1]}: start and stop it with sudo systemctl; the agent will not)"
    return {"argv": argv, "cwd": str(Path(argv[0]).parent) if argv else ""}, ""


def detect_llamacpp(seen: dict, running: list[dict]) -> dict:
    out = {"engines": [], "units": [], "notes": []}
    exe = which("llama-server") or next((str(p) for p in (HOMEDIR / "llama.cpp" / ("llama-server.exe" if WIN else "llama-server"),) if p.exists()), None)
    procs = [r for r in running if r["kind"] in ("llamacpp", "llamacpp-router")]
    if not exe and procs:
        first = argv_of(procs[0]["pid"], procs[0]["cmd"])[:1]
        exe = first[0] if first and Path(first[0]).exists() else None
    if not exe and not procs:
        return out
    e = {"id": "llamacpp", "kind": "llamacpp", "name": "llama.cpp"}
    if exe:
        # releases were tagged by build (b6123) and are now versions (v0.9.0); --version says "version: 0.8.0-dev
        # (build 11800, ...)" on a new build and "version: 6123 (abc)" on an old one: take whichever matches the tags
        e["root"], e["version_cmd"], e["version_re"] = str(Path(exe).parent), [exe, "--version"], r"version: (\d+\.\d+[\w.\-]*)|version: (\d+) \("
    out["engines"].append(e)
    taken = set(seen.values())
    for r in procs:
        if r["port"] in seen:
            continue
        argv = argv_of(r["pid"], r["cmd"])
        start, how = start_for(r, argv)
        if r["kind"] == "llamacpp-router":
            models = router_list(r["port"])
            for m in models:
                status = m.get("status") or {}
                args = status.get("args") or []
                gguf, ctx = flag(args, "--model") or "", flag(args, "--ctx-size") or ""
                uid = m["id"] if m["id"] not in taken else f"{m['id']}-{r['port']}"
                taken.add(uid)
                out["units"].append({
                    "id": uid, "engine": "llamacpp", "kind": "llamacpp-router", "name": m["id"], "model": Path(gguf).name if gguf else m["id"],
                    "router_model": m["id"], "context": int(ctx) if ctx.isdigit() else None,
                    "vision": "image" in ((m.get("architecture") or {}).get("input_modalities") or []),
                    "quant": quant_of(Path(gguf).stem if gguf else m["id"]), "port": r["port"], "footprint": {"vram_gib": None, "ram_gib": None},
                    "start": start, "log": "", "timeouts": {"start_s": 120, "load_s": 180, "unload_s": 60},
                    "notes": "one model of a llama.cpp router: load and unload through its API (POST /models/load, /models/unload); the router itself is never stopped from here"})
            seen[r["port"]] = "llamacpp-router"
            out["notes"].append(f"llama.cpp router on :{r['port']} offers {len(models)} model{'s' if len(models) != 1 else ''}{how}")
            continue
        m = re.search(r"(?:-m|--model)\s+\"?([^\"\s]+)", r["cmd"])
        model = Path(m.group(1)).stem if m else f"llama-server :{r['port']}"
        if start.get("argv"):
            start["loads_at_start"] = True
        out["units"].append({"id": f"llama-{r['port']}", "engine": "llamacpp", "kind": "llamacpp", "name": model, "model": model,
                             "context": None, "vision": False, "quant": quant_of(model), "port": r["port"], "footprint": {"vram_gib": None, "ram_gib": None},
                             "start": start, "log": "", "timeouts": {"start_s": 300, "load_s": 300, "unload_s": 30},
                             "notes": "described from the running process; one model for the server's life, unload = stop" + how})
        seen[r["port"]] = f"llama-{r['port']}"
    if exe and not procs:
        out["notes"].append(f"llama-server found at {exe}; a unit is proposed only while one is running (start it, then `detect`)")
    return out


def comfy_python(root: Path, proc: dict | None) -> Path:
    """The interpreter to start ComfyUI with. A running ComfyUI's own interpreter wins; otherwise, of the venvs
    beside or inside the checkout, the one carrying the most accelerators (sage attention, triton, xformers):
    two venvs can both start ComfyUI and differ in exactly what the owner's launch flags need."""
    if proc:
        m = re.match(r"\s*\"?([^\"]+?python[w]?\.exe|\S*python[0-9.]*)\"?\s", proc["cmd"] + " ", re.I)
        if m and Path(m.group(1)).exists():
            return Path(m.group(1))
    cands = [p for p in (py_in(root), py_in(root.parent)) if p]
    if not cands:
        return Path(sys.executable)
    if len(cands) == 1:
        return cands[0]

    def score(py: Path) -> int:
        rc, o, _ = run([py, "-c", "import importlib.util as u;print(sum(bool(u.find_spec(m)) for m in ('torch','aiohttp','sageattention','triton','xformers')))"], timeout=60)
        try:
            return int(o.strip()) if rc == 0 else -1
        except ValueError:
            return -1
    return max(cands, key=score)


def detect_comfyui(seen: dict, running: list[dict]) -> dict:
    out = {"engines": [], "units": [], "notes": []}
    cands = [HOMEDIR / "ComfyUI" / "ComfyUI", HOMEDIR / "ComfyUI", HOMEDIR / "comfy" / "ComfyUI"]
    for d in fixed_drives():
        cands += [d / "ComfyUI" / "ComfyUI", d / "ComfyUI", d / "AI" / "ComfyUI", d / "comfy" / "ComfyUI"]
    root = next((c for c in cands if (c / "main.py").exists() and (c / "comfy").is_dir()), None)
    proc = next((r for r in running if r["kind"] == "comfyui"), None)
    if not root and proc:
        m = re.search(r"\"?([^\"\s]*main\.py)", proc["cmd"], re.I)
        if m:
            root = Path(m.group(1)).resolve().parent
    if not root:
        return out
    py = comfy_python(root, proc)
    port = proc["port"] if proc else 8188
    ver = ""
    vf = root / "comfyui_version.py"
    if vf.exists():
        ver = version_token(vf.read_text(encoding="utf-8", errors="replace"))
    out["service"] = {
        "_comment": "The agent starts exactly this argv, stops only a process on this port whose command line contains 'match', and asks ComfyUI to drop its models with POST /free. ComfyUI has no login: never add --listen (the agent refuses to start it with one). Add your own flags to argv (sage attention, xformers off).",
        "name": "ComfyUI", "url": f"http://127.0.0.1:{port}", "open_url": f"http://127.0.0.1:{port}", "port": port,
        "start": {"argv": [str(py), str(root / "main.py")], "cwd": str(root)},
        "match": "ComfyUI\\main.py" if WIN else "ComfyUI/main.py", "log": str(root / "user" / "comfyui.log"),
        "timeouts": {"start_s": 240, "stop_s": 60, "free_s": 120},
        "update": {"repo": str(root), "python": str(py), "backup_root": str(root.parent / "update-backups"), "pins": ["torch", "torchvision", "torchaudio"]}}
    out["notes"].append(f"ComfyUI {ver} at {root}" + (" (running)" if proc else ""))
    seen[port] = "comfyui"
    return out


def detect_openwebui() -> dict:
    out = {"notes": []}
    port = next((p for p in (3000, 8080) if get_json(f"http://127.0.0.1:{p}/health")[0] == 200), None)
    tasks = {}
    if WIN:
        for verb in ("start", "stop", "update"):
            rc, o, e = run(["schtasks.exe", "/Query", "/TN", f"owui-ctl-{verb}"], timeout=15)
            tasks[verb] = "run" if rc == 0 else ("gated" if "denied" in (o + e).lower() else "missing")
    if any(v != "missing" for v in tasks.values()):
        out["service"] = {
            "_comment": "OpenWebUI driven by three Windows scheduled tasks (start/stop/update) whose scripts live where only administrators can write; the agent runs them by name and passes them nothing.",
            "name": "OpenWebUI", "url": f"http://127.0.0.1:{port or 3000}", "open_url": f"http://localhost:{port or 3000}",
            "tasks": {"start": "owui-ctl-start", "stop": "owui-ctl-stop", "update": "owui-ctl-update"}}
        out["notes"].append(f"OpenWebUI: scheduled tasks {tasks}; answering on :{port}" if port else f"OpenWebUI: scheduled tasks {tasks}; not answering")
        return out
    if port:
        rc, o, _ = run(["docker", "ps", "--format", "{{.Image}}"], timeout=15)
        how = "docker" if rc == 0 and "open-webui" in o else "a process"
        out["notes"].append(f"OpenWebUI answers on :{port} ({how}); the generic service driver is a later stage, so no tile is written yet")
    return out


def client(cid: str, name: str, exe: str | None, cfg: Path | None, bindings: list[dict]) -> dict | None:
    if not exe:
        return None
    rc, o, e = run([exe, "--version"], timeout=20)
    c = {"id": cid, "name": name, "exe": exe, "version_cmd": [exe, "--version"], "version": version_token(o or e)}
    if cfg:
        c["config"] = str(cfg)
    if bindings:
        c["bindings"] = bindings
    return c


def local_url(u: str) -> bool:
    return bool(re.match(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", u or ""))


def detect_clients(kinds: set[str]) -> dict:
    out = {"clients": [], "notes": []}
    if "pi" in kinds:
        cfg = HOMEDIR / ".pi" / "agent" / "models.json"
        binds = []
        d = load_json(cfg) if cfg.exists() else None
        for pname, p in ((d or {}).get("providers") or {}).items():
            url = (p or {}).get("baseUrl") or (p or {}).get("base_url") or ""
            if local_url(url):
                binds.append({"name": pname, "url": url})
        c = client("pi", "Pi", which("pi"), cfg if cfg.exists() else None, binds)
        if c:
            out["clients"].append(c)
    if "codex" in kinds:
        cfg = HOMEDIR / ".codex" / "config.toml"
        binds = []
        if cfg.exists():
            text = cfg.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"\[model_providers\.([^\]]+)\][^\[]*?base_url\s*=\s*\"([^\"]+)\"", text, re.S):
                if local_url(m.group(2)):
                    binds.append({"name": m.group(1), "url": m.group(2)})
        c = client("codex", "Codex CLI", which("codex"), cfg if cfg.exists() else None, binds)
        if c:
            out["clients"].append(c)
    if "claude" in kinds:
        c = client("claude", "Claude Code", which("claude"), None, [])
        if c:
            out["clients"].append(c)
    return out


# ---- putting it together -------------------------------------------------------------------

def detect_all(kinds: set[str]) -> dict:
    seen: dict[int, str] = {}
    running = running_servers()
    found = {"engines": [], "units": [], "services": {}, "clients": [], "notes": []}

    def take(r):
        for k in ("engines", "units", "clients", "notes"):
            found[k] += r.get(k, [])
        return r
    if "strata" in kinds:
        take(detect_strata(seen))
    if "maya" in kinds:
        take(detect_maya(seen))
    if "ollama" in kinds:
        take(detect_ollama(seen))
    if "llamacpp" in kinds:
        take(detect_llamacpp(seen, running))
    if "comfyui" in kinds:
        r = take(detect_comfyui(seen, running))
        if r.get("service"):
            found["services"]["comfyui"] = r["service"]
    if "openwebui" in kinds:
        r = take(detect_openwebui())
        if r.get("service"):
            found["services"]["openwebui"] = r["service"]
    take(detect_clients(kinds))
    for r in running:
        if r["port"] in seen:
            continue
        if r["kind"] in ("strata", "maya") and not any(e["id"] == r["kind"] for e in found["engines"]):
            found["notes"].append(f"a {r['kind']} server is listening on :{r['port']} (pid {r['pid']}) but its install was not found: {r['cmd'][:120]}")
    skip = set(ARGS["skip"] or [])
    if skip:
        found["units"] = [u for u in found["units"] if u["id"] not in skip]
        found["clients"] = [c for c in found["clients"] if c["id"] not in skip]
        found["engines"] = [e for e in found["engines"] if e["id"] not in skip]
    for u in found["units"]:
        u["running"] = any(r["port"] == u["port"] for r in running)
    # an engine with nothing to show is a bare band: say so instead of proposing it
    with_units = {u["engine"] for u in found["units"]}
    for e in [e for e in found["engines"] if e["id"] not in with_units]:
        found["engines"].remove(e)
        found["notes"].append(f"{e['name']} is installed ({e.get('root', '')}) but no unit was found for it; add one by hand when you have one")
    return found


def show(hf: dict, found: dict) -> None:
    g = ", ".join(f"GPU {x['index']}: {x['name']} {x['total_mib']} MiB" for x in hf["gpus"]) or "no NVIDIA GPU seen"
    print(f"\nHost  {hf['host']}  ({hf['os']})\n      {hf['cpu']}, {hf['cores']} threads, {hf['ram_total_gib']} GiB RAM\n      {g}")
    if found["engines"]:
        print("\nEngines")
        for e in found["engines"]:
            print(f"  {e['id']:<10} {e['name']:<10} {e.get('root', '')}")
    if found["units"]:
        print("\nUnits")
        for u in found["units"]:
            print(f"  {u['id']:<28} {u['engine']:<8} :{u['port']:<6} {u['name'][:36]:<36} {'running' if u.get('running') else ''}")
    if found["services"]:
        print("\nServices")
        for k, s in found["services"].items():
            print(f"  {k:<10} {s['url']}")
    if found["clients"]:
        print("\nClients")
        for c in found["clients"]:
            b = ", ".join(x["url"].split("//")[1].split("/")[0] for x in c.get("bindings", []))
            print(f"  {c['id']:<8} {c['version'] or '?':<10} {('-> ' + b) if b else ''}")
    if found["notes"]:
        print("\nNotes")
        for n in found["notes"]:
            print(f"  - {n}")
    print()


def inventory_from(hf: dict, found: dict, owner: str) -> dict:
    ram = hf["ram_total_gib"] or 0
    inv = {"_comment": "Written by agent/setup.py; edit freely. The agent executes only what is written here. Footprints (vram_gib, ram_gib), contexts and quants are yours to fill in: setup cannot measure them. After editing: POST /api/reload or restart the agent.",
           "schema": 2, "host": hf["host"], "owner": owner, "cpu": hf["cpu"], "os": hf["os"],
           "gpus": hf["gpus"] or [{"index": 0, "name": "no NVIDIA GPU found", "total_mib": 0, "free_card_mib": 2048}],
           "ram_total_gib": ram, "pin_budget_gib": round(ram * 0.5), "commit_warn_gib": 16, "poll_s": 2.0,
           "policy": {"park_8099_default": False, "unload_ollama_on_resident": True, "free_comfyui_on_resident": True, "humans_only": True},
           "engines": found["engines"], "units": [{k: v for k, v in u.items() if k != "running"} for u in found["units"]],
           "shims": [], "clients": found["clients"]}
    for k, s in found["services"].items():
        inv[k] = s
    return inv


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def launchers(home: Path) -> None:
    if not WIN:
        dest = ROOT if home == HERE else home
        unit = dest / "quartermaster.service"
        env = f"Environment=QM_HOME={home}\n" if home != HERE else ""
        unit.write_text(f"""[Unit]
Description=Quartermaster host agent (loopback only)
After=network.target

[Service]
Type=simple
WorkingDirectory={HERE}
{env}ExecStart={sys.executable} {HERE / 'quartermaster_agent.py'}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""", encoding="utf-8", newline="\n")
        print(f"  wrote {unit.name}: a systemd user unit. To start the agent at login:\n"
              f"    mkdir -p ~/.config/systemd/user && cp \"{unit}\" ~/.config/systemd/user/ && systemctl --user enable --now quartermaster\n"
              f"  (loginctl enable-linger $USER keeps it running when you are logged out)")
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    pyw = pyw if pyw.exists() else Path(sys.executable)
    env = f"set QM_HOME={home}\r\n" if home != HERE else ""
    dest = ROOT if home == HERE else home            # a separate home gets its own launchers, beside its inventory
    agent = HERE / "quartermaster_agent.py"
    cmd = dest / "Quartermaster Agent.cmd"
    cmd.write_text(f"@echo off\r\nrem Copy this file into shell:startup to start the Quartermaster agent at logon.\r\n{env}"
                   f"start \"\" /D \"{HERE}\" \"{pyw}\" \"{agent}\"\r\n", encoding="utf-8", newline="")
    bat = dest / "run-quartermaster.bat"
    bat.write_text(f"@echo off\r\nrem Quartermaster: start the host agent (if it is not up) and the UI container, then open the page.\r\n"
                   f"cd /d \"{ROOT}\"\r\n{env}"
                   f"curl -s -m 2 -o nul http://127.0.0.1:8790/ || start \"\" /D \"{HERE}\" \"{pyw}\" \"{agent}\"\r\n"
                   f"docker compose up -d --build\r\nif errorlevel 1 (\r\n  echo docker compose failed; the agent's own page is at http://127.0.0.1:8790/\r\n  pause\r\n  exit /b 1\r\n)\r\n"
                   f"start http://127.0.0.1:8800/\r\n", encoding="utf-8", newline="")
    print(f"  wrote {cmd.name} and {bat.name}")
    if ARGS["startup"] is not False and ask("Start the agent at logon (copy the .cmd into your Startup folder)?", False):
        su = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        try:
            shutil.copy2(cmd, su / cmd.name)
            print(f"  copied to {su}")
        except OSError as e:
            print(f"  could not copy into {su}: {e}")


def do_setup(home: Path) -> int:
    inv_path = home / "inventory.json"
    if inv_path.exists() and not ARGS["force"]:
        print(f"{inv_path} exists. Run `setup.py detect` to add what is new, or `setup.py --force` to start over (the old file is kept as a .bak).")
        return 2
    print("Quartermaster setup: nothing is written until you say so.")
    hf = host_facts()
    kinds = set(ARGS["kinds"] or [])
    if not kinds:
        print("\nWhich of these should it look for?")
        for k, label in KINDS:
            if ask(f"  {label}?", True):
                kinds.add(k)
    found = detect_all(kinds)
    show(hf, found)
    if not found["engines"] and not found["services"]:
        print("Nothing to manage was found. The inventory can still be written and filled in by hand (agent/inventory.example.json shows the shape).")
    owner = ask_text("Owner's name (shown by the themes that address you)", os.environ.get("USERNAME") or os.environ.get("USER") or "Dave")
    inv = inventory_from(hf, found, owner)
    if not ask(f"Write {inv_path}?", True):
        print("nothing written")
        return 1
    home.mkdir(parents=True, exist_ok=True)
    if inv_path.exists():
        bak = inv_path.with_name(f"inventory.{time.strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(inv_path, bak)
        print(f"  old inventory kept as {bak.name}")
    write_json(inv_path, inv)
    print(f"  wrote {inv_path}")
    pre = home / "presets.json"
    if not pre.exists():
        presets = [{"id": "clear", "name": "Clear the card", "desc": "Nothing resident: small models and image work get the whole card", "steps": [{"op": "unload_all"}]}]
        for u in inv["units"]:
            if u["kind"] in ("strata", "strata-launch", "maya", "llamacpp", "llamacpp-router") and not u.get("legacy"):
                presets.append({"id": f"resident-{u['id']}", "name": f"{u['name'][:30]} resident", "desc": f"{u['name']} on :{u['port']}, nothing else on the card",
                                "steps": [{"op": "make_resident", "target": u["id"]}]})
        write_json(pre, {"presets": presets})
        print(f"  wrote {pre.name} ({len(presets)} presets)")
    key = home / "agent.key"
    if not key.exists():
        key.write_text(secrets.token_hex(32), encoding="utf-8", newline="\n")
        print(f"  wrote {key.name}")
    launchers(home)
    if (ROOT / ".git").is_dir() and (ROOT / "tools" / "scrub_check.py").exists() and not (ROOT / ".git" / "hooks" / "pre-commit").exists():
        if ask("This checkout is a git repository: install the scrub check as its pre-commit hook?", True):
            run([sys.executable, ROOT / "tools" / "scrub_check.py", "--install-hook"], timeout=30)
            print("  hook installed")
    print(f"\nNext: fill in footprints and contexts in {inv_path.name}, then start the agent:\n  python {HERE / 'quartermaster_agent.py'} --debug"
          + (f"\n  (with QM_HOME={home})" if home != HERE else "") + "\nand open http://127.0.0.1:8790/")
    return 0


def do_detect(home: Path) -> int:
    inv_path = home / "inventory.json"
    inv = load_json(inv_path)
    if not isinstance(inv, dict):
        print(f"no inventory at {inv_path}: run setup first")
        return 2
    if int(inv.get("schema") or 1) < 2:
        print("the inventory is schema 1: start the agent once to migrate it, then run detect again")
        return 2
    kinds = set(ARGS["kinds"] or [k for k, _ in KINDS])
    found = detect_all(kinds)
    have_e = {e["id"] for e in inv.get("engines", [])}
    have_u = {u["id"] for u in inv.get("units", [])}
    have_k = {unit_key(u) for u in inv.get("units", [])}
    have_c = {c["id"] for c in inv.get("clients", [])}
    new_e = [e for e in found["engines"] if e["id"] not in have_e]
    new_u = [u for u in found["units"] if u["id"] not in have_u and unit_key(u) not in have_k]
    new_s = {k: s for k, s in found["services"].items() if k not in inv}
    new_c = [c for c in found["clients"] if c["id"] not in have_c]
    for n in found["notes"]:
        print(f"  - {n}")
    if not (new_e or new_u or new_s or new_c):
        print("nothing new: the inventory already has everything detection can see")
        return 0
    changed = False
    for e in new_e:
        if ask(f"Add engine {e['id']} ({e.get('root', e['name'])})?", True):
            inv.setdefault("engines", []).append(e); changed = True
    for u in new_u:
        u = {k: v for k, v in u.items() if k != "running"}
        if u["engine"] not in {e["id"] for e in inv.get("engines", [])}:
            continue
        if ask(f"Add unit {u['id']} ({u['engine']} :{u['port']} {u['name'][:40]})?", True):
            inv.setdefault("units", []).append(u); changed = True
    for k, s in new_s.items():
        if ask(f"Add the {s['name']} tile ({s['url']})?", True):
            inv[k] = s; changed = True
    for c in new_c:
        if ask(f"Add client {c['id']} ({c['version'] or '?'})?", True):
            inv.setdefault("clients", []).append(c); changed = True
    if not changed:
        print("nothing added")
        return 0
    bak = inv_path.with_name(f"inventory.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(inv_path, bak)
    write_json(inv_path, inv)
    print(f"  wrote {inv_path} (previous kept as {bak.name}); POST /api/reload or restart the agent")
    return 0


def main(argv: list[str]) -> int:
    args = list(argv)
    if args and args[0] in ("setup", "detect"):
        ARGS["mode"] = args.pop(0)
    while args:
        a = args.pop(0)
        if a == "--yes":
            ARGS["yes"] = True
        elif a == "--force":
            ARGS["force"] = True
        elif a == "--no-startup":
            ARGS["startup"] = False
        elif a == "--kinds" and args:
            ARGS["kinds"] = [k.strip() for k in args.pop(0).split(",") if k.strip()]
        elif a == "--skip" and args:
            ARGS["skip"] = [k.strip() for k in args.pop(0).split(",") if k.strip()]
        elif a == "--home" and args:
            ARGS["home"] = Path(args.pop(0)).resolve()
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            sys.exit(f"unknown argument {a!r}; --help lists them")
    bad = [k for k in (ARGS["kinds"] or []) if k not in dict(KINDS)]
    if bad:
        sys.exit(f"unknown kinds {bad}; known: {', '.join(k for k, _ in KINDS)}")
    home = ARGS["home"] or HERE
    return do_detect(home) if ARGS["mode"] == "detect" else do_setup(home)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
