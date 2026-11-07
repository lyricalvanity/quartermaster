"""What the agent asks of the operating system, in one place, with a Windows and a Linux answer each.

  ram_status()                 total / available / used GiB, commit (Windows) or swap (Linux)
  nvidia_smi()                 the nvidia-smi executable, or None
  gpu_dedicated_mib(pid)       dedicated GPU memory one process holds, MiB (Windows counter / nvidia-smi apps)
  process_list(names)          {name_lower: [pids]} for the given executable names
  pid_listening(port)          pids of TCP listeners on a port
  binds(port)                  the addresses a port's listeners are bound to
  cmdline(pid)                 a process's command line, or ""
  kill_tree(pid) / kill(pid)   end a process and its children / one process
  popen_detached(argv, ...)    start a server detached from this process: no console, its own group or session
  exe_names(stems)             process names as the OS lists them (stem.exe on Windows, stem on Linux)

Linux support is new (2026-11-06) and read-only tested where the workstation could not run it; the Windows
branches are the ones the agent ran on for a week. Stdlib only. Written 2026-11-06 (Faber)."""
from __future__ import annotations

import ctypes
import os
import platform as _platform
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

WINDOWS = _platform.system() == "Windows"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DETACH_FLAGS = (NO_WINDOW | NEW_GROUP) if WINDOWS else 0
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
TYPEPERF = r"C:\Windows\System32\typeperf.exe"


def run(args, timeout=20, cwd=None):
    """rc, stdout, stderr; never raises for a missing program or a timeout (rc 1 and the reason in stderr)."""
    try:
        p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           creationflags=DETACH_FLAGS if WINDOWS else 0, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


# ---- memory ---------------------------------------------------------------------------------

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram_status() -> dict:
    gib = 1024 ** 3
    if WINDOWS:
        m = _MemoryStatusEx()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return {"total_gib": round(m.ullTotalPhys / gib, 1), "available_gib": round(m.ullAvailPhys / gib, 1),
                "used_gib": round((m.ullTotalPhys - m.ullAvailPhys) / gib, 1), "used_percent": m.dwMemoryLoad,
                "commit_total_gib": round(m.ullTotalPageFile / gib, 1),
                "commit_used_gib": round((m.ullTotalPageFile - m.ullAvailPageFile) / gib, 1),
                "commit_free_gib": round(m.ullAvailPageFile / gib, 1)}
    kb = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            kb[k.strip()] = int(v.split()[0]) if v.strip() else 0
    except (OSError, ValueError):
        return {"total_gib": None, "available_gib": None, "used_gib": None, "used_percent": None,
                "commit_total_gib": None, "commit_used_gib": None, "commit_free_gib": None}
    total, avail = kb.get("MemTotal", 0), kb.get("MemAvailable", 0)
    swap_t, swap_f = kb.get("SwapTotal", 0), kb.get("SwapFree", 0)
    # "commit" on Linux: RAM plus swap, which is what the kernel can hand out before it starts refusing
    return {"total_gib": round(total / 2**20, 1), "available_gib": round(avail / 2**20, 1),
            "used_gib": round((total - avail) / 2**20, 1), "used_percent": round(100 * (total - avail) / total) if total else None,
            "commit_total_gib": round((total + swap_t) / 2**20, 1),
            "commit_used_gib": round((total - avail + swap_t - swap_f) / 2**20, 1),
            "commit_free_gib": round((avail + swap_f) / 2**20, 1)}


# ---- GPU ------------------------------------------------------------------------------------

def nvidia_smi() -> str | None:
    p = shutil.which("nvidia-smi")
    if p:
        return p
    for c in (r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
              "/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi"):
        if Path(c).exists():
            return c
    return None


def gpu_dedicated_mib(pid: int) -> float | None:
    """Dedicated GPU memory one process holds. Windows: the GPU Process Memory counter, exact under WDDM where
    nvidia-smi has no per-process figure. Linux: nvidia-smi's compute-apps list."""
    if WINDOWS:
        rc, out, _ = run([TYPEPERF, rf"\GPU Process Memory(pid_{int(pid)}_*)\Dedicated Usage", "-sc", "1"], timeout=15)
        if rc != 0:
            return None
        rows = [l for l in out.splitlines() if l.startswith('"')]
        if len(rows) < 2:
            return None
        vals = [v.strip('"') for v in rows[1].split('","')][1:]
        total = 0.0
        for v in vals:
            try:
                total += float(v)
            except ValueError:
                pass
        return round(total / 2**20, 1)
    smi = nvidia_smi()
    if not smi:
        return None
    rc, out, _ = run([smi, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"], timeout=10)
    if rc != 0:
        return None
    total = 0.0
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0] == str(pid):
            try:
                total += float(parts[1])
            except ValueError:
                pass
    return round(total, 1)


# ---- processes and ports --------------------------------------------------------------------------

def exe_names(stems) -> tuple:
    return tuple((s + ".exe") if WINDOWS and not s.endswith(".exe") else s for s in stems)


def process_list(names) -> dict:
    """{name_lower: [pids]} for the executables named (as the OS lists them)."""
    want = {n.lower() for n in names}
    found: dict[str, list[int]] = {}
    if WINDOWS:
        rc, out, _ = run(["tasklist.exe", "/FO", "CSV", "/NH"], timeout=15)
        if rc == 0:
            for line in out.splitlines():
                if not line.startswith('"'):
                    continue
                cols = [c.strip('"') for c in line.split('","')]
                name = cols[0].lower()
                if name in want:
                    try:
                        found.setdefault(name, []).append(int(cols[1]))
                    except (IndexError, ValueError):
                        pass
        return found
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            name = (d / "comm").read_text().strip().lower()
        except OSError:
            continue
        if name in want:
            found.setdefault(name, []).append(int(d.name))
    return found


def _listeners() -> list[tuple[str, int, int]]:
    """[(address, port, pid)] for every TCP listener."""
    out_rows = []
    if WINDOWS:
        rc, out, _ = run(["netstat.exe", "-ano", "-p", "tcp"], timeout=15)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
                    addr, _, port = parts[1].rpartition(":")
                    try:
                        out_rows.append((addr, int(port), int(parts[4])))
                    except ValueError:
                        pass
        rc, out, _ = run(["netstat.exe", "-ano", "-p", "tcpv6"], timeout=15)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
                    addr, _, port = parts[1].rpartition(":")
                    try:
                        out_rows.append((addr, int(port), int(parts[4])))
                    except ValueError:
                        pass
        return out_rows
    rc, out, _ = run(["ss", "-ltnpH"], timeout=15)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            addr, _, port = parts[3].rpartition(":")
            m = re.search(r"pid=(\d+)", line)
            try:
                out_rows.append((addr, int(port), int(m.group(1)) if m else 0))
            except ValueError:
                pass
    return out_rows


def pid_listening(port: int) -> list[int]:
    return sorted({pid for _, p, pid in _listeners() if p == port and pid})


def binds(port: int) -> list[str]:
    return sorted({addr for addr, p, _ in _listeners() if p == port})


def ports_bound_on(ip: str) -> set[int]:
    """The ports something listens on at one address (the bridge check)."""
    return {p for addr, p, _ in _listeners() if addr == ip}


def cmdline(pid: int) -> str:
    if WINDOWS:
        rc, out, _ = run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"], timeout=30)
        return out.strip() if rc == 0 else ""
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def kill_tree(pid: int) -> str:
    """End a process and everything it started. Returns the OS's one-line answer."""
    if WINDOWS:
        rc, out, err = run(["taskkill.exe", "/T", "/F", "/PID", str(int(pid))], timeout=30)
        return (out or err).strip()[:160]
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        return f"SIGTERM to group of {pid}"
    except (OSError, ProcessLookupError) as e:
        try:
            os.kill(int(pid), signal.SIGTERM)
            return f"SIGTERM to {pid}"
        except (OSError, ProcessLookupError) as e2:
            return f"{e2}"


def kill(pid: int) -> str:
    if WINDOWS:
        rc, out, err = run(["taskkill.exe", "/F", "/PID", str(int(pid))], timeout=15)
        return (out or err).strip()[:160]
    try:
        os.kill(int(pid), signal.SIGTERM)
        return f"SIGTERM to {pid}"
    except (OSError, ProcessLookupError) as e:
        return str(e)


def popen_detached(argv, cwd=None, stdout=None, env=None) -> subprocess.Popen:
    """Start a server that outlives this call and takes no signal meant for the agent: no console window and
    its own process group on Windows (a .bat goes through cmd /c); its own session on Linux."""
    argv = [str(a) for a in argv]
    if WINDOWS and argv and argv[0].lower().endswith((".bat", ".cmd")):
        argv = ["cmd.exe", "/c"] + argv
    kw = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": stdout, "stderr": subprocess.STDOUT, "env": env}
    if WINDOWS:
        kw["creationflags"] = DETACH_FLAGS
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def python_console() -> str:
    """This interpreter as a console python (pythonw.exe has no stdout to read)."""
    return sys.executable.replace("pythonw.exe", "python.exe")
