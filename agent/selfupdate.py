"""Updating Quartermaster itself (docs/self-update.md), stages 1 and 2: what this installation is, what an update of
it takes, and the update for the paths that only fast-forward (B, no restart; C, a restart through a systemd user unit,
or none). The check is read-only apart from its `git fetch`, which is the click. Every step the agent does not take
yet carries the command that does it by hand.

  state(root, cache)                    the version and head, cheap enough for every snapshot
  context(root, port, block, inv)       how this installation runs: platform, checkout, who restarts the agent, page
  check(root, block, inv, port, say)    fetch, then the context, the path and its flags: the record kept under "quartermaster"
  settle(rec, head)                     a record as of now (the checkout may have moved by hand since the check)
  propose_block(root, port)             the `self` block setup and detect offer to write into the inventory
  blocker(rec, block) / ready(...)      why an update cannot run: from the check, and again just before it moves anything
  prepare / move / preflight / hand_over / reset    the update's steps (the agent's ops drive them)
  last_update(log_dir, head)            the newest update's record, for the state and the Roll back entry

The paths (A current .. I archive) and the flags are section 5 of the specification. Nothing here acts on what the
probe finds: the inventory's `self` block says what the agent may run, and the probe only checks that it still
describes this installation. Stdlib only. Written 2026-11-24."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import platform_host as host

ID = "quartermaster"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# never a prompt: a fetch that wants credentials fails plainly instead of waiting at a terminal nobody sees
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C", "LANGUAGE": "C"}
AGENT_PATHS = ("agent/", "catalog/")                    # what the running agent has loaded: a change needs a restart
CONTAINER_PATHS = ("ui/", "docker/", "docker-compose.yml")   # what the nginx image copies at build time
SENSITIVE = ("agent/", "catalog/", "contrib/", "docker/", "docker-compose.yml", "tools/scrub_check.py")
TEMPLATE_DIRS = {"contrib/openwebui-systemd/": "OpenWebUI's systemd controls"}
PATHS = {"A": "current", "B": "no restart (page, docs, tools)", "C": "agent code: restart", "D": "rewritten upstream",
         "E": "local work", "F": "uncommitted changes", "G": "untracked files in the way", "H": "no upstream", "I": "archive"}
APPLY_NOTE = "Update takes the steps marked agent; the ones marked you stay yours, each with its command."
BLOCKED_NOTE = "Update is not offered here ({why}). These are the steps, each with the command that does it by hand."


def run(args, cwd=None, timeout=30, env=None) -> tuple[int, str, str]:
    try:
        p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def git(root, *args, timeout=30) -> tuple[int, str, str]:
    return run(["git", "-C", root, *args], timeout=timeout, env=GIT_ENV)


def git_dir(root: Path) -> Path | None:
    g = Path(root) / ".git"
    if g.is_dir():
        return g
    if g.is_file():                                     # a worktree or submodule: "gitdir: <path>"
        m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8", errors="replace").strip())
        if m:
            d = Path(m.group(1))
            return d if d.is_absolute() else (Path(root) / d).resolve()
    return None


def head_cheap(root: Path) -> tuple[str | None, str | None]:
    """(branch, sha) from the files under .git, no subprocess: the snapshot asks every two seconds."""
    gd = git_dir(root)
    if not gd:
        return None, None
    try:
        head = (gd / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not head.startswith("ref: "):
        return None, head or None                       # detached
    ref = head[5:].strip()
    branch = ref.removeprefix("refs/heads/")
    common = gd
    if (gd / "commondir").exists():                     # a worktree keeps its refs in the main repository
        common = (gd / (gd / "commondir").read_text(encoding="utf-8").strip()).resolve()
    for base in (gd, common):
        f = base / ref
        if f.is_file():
            return branch, f.read_text(encoding="utf-8").strip() or None
    try:
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return branch, line.split()[0]
    except OSError:
        pass
    return branch, None


def version_file(root: Path) -> str | None:
    try:
        return (Path(root) / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0][:40] or None
    except (OSError, IndexError):
        return None


def state(root: Path, cache: dict, running: str | None = None) -> dict:
    """What the page shows beside the brand: the version, the checkout's head, and the head the agent started from
    (`running`). They differ after an update until a restart; `restart_due` says whether that matters, i.e. whether
    anything the agent loads changed between them. Git runs only when either head moves."""
    branch, sha = head_cheap(root)
    if (cache.get("sha"), cache.get("running")) != (sha, running):
        rc, o, _ = git(root, "describe", "--tags", "--always") if sha else (1, "", "")
        due = bool(sha and running and sha != running
                   and any(p.startswith(AGENT_PATHS) for _, p in changed_paths(root, running, sha)))
        cache.update(sha=sha, running=running, describe=o.strip() if rc == 0 else None, restart_due=due)
    return {"version": version_file(root), "git": sha is not None, "branch": branch, "head": sha[:7] if sha else None,
            "head_full": sha, "describe": cache.get("describe"), "running": running, "restart_due": cache.get("restart_due", False)}


# ---- who keeps the agent running -------------------------------------------------------------------------------

def _systemctl_show(unit: str) -> dict:
    rc, o, _ = run(["systemctl", "--user", "show", unit, "-p", "MainPID", "-p", "Restart", "-p", "FragmentPath", "-p", "KillMode"], timeout=10)
    return dict(l.split("=", 1) for l in o.splitlines() if "=" in l) if rc == 0 else {}


def supervisor(pid: int | None = None) -> dict:
    """How the process `pid` (this one by default) is kept running, as far as the operating system says.
    via: systemd-user (restartable by its unit), respawn (Windows, pythonw with no console: the Startup launcher or
    run-quartermaster.bat), none (started by hand), systemd-system (a system unit: not something this user restarts)."""
    me = pid is None or pid == os.getpid()
    pid = os.getpid() if pid is None else pid
    if host.WINDOWS:
        if me:
            try:
                import ctypes
                console = bool(ctypes.windll.kernel32.GetConsoleWindow())
            except (AttributeError, OSError):
                console = False
            exe = Path(sys.executable).name.lower()
        else:
            exe = (host.cmdline(pid).split('" ')[0].split("\\")[-1].strip('"').split()[0:1] or [""])[0].lower()
            console = "pythonw" not in exe
        if "pythonw" in exe and not console:
            return {"via": "respawn", "detail": "pythonw with no console: the Startup launcher or run-quartermaster.bat"}
        return {"via": "none", "detail": "a console window: started by hand"}
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"via": "none", "detail": "its control group could not be read"}
    path = next((l.split("::", 1)[1] for l in lines if l.startswith("0::")), lines[-1].split(":", 2)[-1] if lines else "")
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last.endswith(".service") and "/user@" in path:
        p = _systemctl_show(last)
        try:
            main = int(p.get("MainPID") or 0)
        except ValueError:
            main = 0
        return {"via": "systemd-user", "unit": last, "restart": p.get("Restart"), "kill_mode": p.get("KillMode"),
                "fragment": p.get("FragmentPath") or None, "main_pid": main == pid,
                "detail": f"systemd user unit {last} (Restart={p.get('Restart') or '?'})"}
    if last.endswith(".service"):
        return {"via": "systemd-system", "unit": last, "detail": f"systemd system unit {last}: this user cannot restart it"}
    if last.endswith(".scope"):
        return {"via": "none", "detail": f"started by hand, in a session ({last})"}
    return {"via": "none", "detail": "no supervisor found (no systemd unit holds it)"}


# ---- the page: the agent itself, and the container if one serves it ----------------------------------------------

def _iso_epoch(s: str) -> float | None:
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?", (s or "").strip())
    if not m:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(m.group(1) + (m.group(2) or "")[:7] + ("+00:00" if m.group(3) in (None, "Z") else m.group(3)))
        return dt.astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def container(root: Path, block_container: dict | None) -> dict | None:
    """The compose container that serves this checkout's page, found by its compose labels (service, and the
    project's working directory being this checkout). None when there is no docker and nothing names one."""
    docker = shutil.which("docker")
    if not docker:
        return {"found": False, "error": "docker is not installed"} if block_container else None
    service = (block_container or {}).get("service") or "quartermaster"
    fmt = '{{.ID}}\t{{.Label "com.docker.compose.project.working_dir"}}\t{{.State}}\t{{.Image}}\t{{.Names}}'
    rc, o, e = run([docker, "ps", "-a", "--filter", f"label=com.docker.compose.service={service}", "--format", fmt], timeout=20)
    if rc:
        return {"found": False, "error": "docker is not answering"} if block_container else None
    want = os.path.normcase(str(Path(root).resolve()))
    for line in o.splitlines():
        cols = line.split("\t")
        if len(cols) >= 5 and cols[1] and os.path.normcase(str(Path(cols[1]).resolve())) == want:
            rc2, created, _ = run([docker, "image", "inspect", "--format", "{{.Created}}", cols[3]], timeout=20)
            return {"found": True, "name": cols[4], "state": cols[2], "image": cols[3], "service": service,
                    "image_created": _iso_epoch(created) if rc2 == 0 else None}
    return {"found": False, "service": service} if block_container else None


# ---- the checkout ---------------------------------------------------------------------------------------------------

def upstream(root: Path) -> tuple[str | None, str | None]:
    """(remote, branch) of the current branch's upstream, or (None, None)."""
    rc, o, _ = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc or "/" not in o.strip():
        return None, None
    rc2, remote, _ = git(root, "config", f"branch.{head_cheap(root)[0]}.remote")
    remote = remote.strip() if rc2 == 0 and remote.strip() else o.strip().split("/", 1)[0]
    return remote, o.strip()[len(remote) + 1:]


def show(root: Path, rev: str, path: str) -> str | None:
    rc, o, _ = git(root, "show", f"{rev}:{path}", timeout=20)
    return o if rc == 0 else None


def changed_paths(root: Path, a: str, b: str) -> list[tuple[str, str]]:
    """[(status, path)] between two commits' trees."""
    rc, o, _ = git(root, "diff", "--name-status", "--no-renames", a, b, timeout=60)
    return [(l.split("\t", 1)[0][:1], l.split("\t", 1)[1]) for l in o.splitlines() if "\t" in l] if rc == 0 else []


def dirty_tracked(root: Path) -> list[str]:
    rc, o, _ = git(root, "status", "--porcelain=v1", "--untracked-files=no")
    return [l[3:] for l in o.splitlines() if l.strip()] if rc == 0 else []


def others_on_checkout(root: Path) -> list[dict]:
    """Other agents running this checkout's code (a test instance on another port): an update would move it too."""
    me, script = os.getpid(), str(Path(root) / "agent" / "quartermaster_agent.py")
    found = []
    if host.WINDOWS:
        rc, o, _ = run([host.POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"CommandLine like '%quartermaster_agent.py%'\" | "
                        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"], timeout=30)
        rows = [l.split("\t", 1) for l in o.splitlines() if "\t" in l] if rc == 0 else []
    else:
        rows = []
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    rows.append([d.name, (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")])
                except OSError:
                    pass
    for pid, cmd in rows:
        if int(pid) != me and os.path.normcase(script) in os.path.normcase(cmd) and "--selftest" not in cmd:
            found.append({"pid": int(pid), "cmd": cmd.strip()[:200]})
    return found


def hooks_installed(root: Path) -> dict:
    gd = git_dir(root)
    out = {}
    for name in ("pre-commit", "commit-msg"):
        p = gd / "hooks" / name if gd else None
        try:
            out[name] = bool(p and p.is_file() and "scrub_check.py" in p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            out[name] = False
    return out


def templates_in_use(inv: dict | None) -> list[str]:
    """The contrib templates this machine has installed as root, as far as its inventory says."""
    o = (inv or {}).get("openwebui") or {}
    if o.get("driver") == "systemd" and o.get("ctl_log"):
        return ["contrib/openwebui-systemd/"]
    return []


# ---- the flags' readers ------------------------------------------------------------------------------------------

FACE_RE = re.compile(r"@font-face\s*\{(.*?)\}", re.S)


def font_faces(css: str | None) -> dict[tuple[str, str], str]:
    """{(family lower, weight descriptor): family as written} for every @font-face in a stylesheet."""
    out = {}
    for body in FACE_RE.findall(css or ""):
        fm = re.search(r"font-family:\s*(?:\"([^\"]+)\"|'([^']+)'|([^;]+));", body)
        if not fm:
            continue
        fam = (fm.group(1) or fm.group(2) or fm.group(3) or "").strip()
        wm = re.search(r"font-weight:\s*([^;]+);", body)
        w = " ".join((wm.group(1) if wm else "400").split()).lower()
        w = {"normal": "400", "bold": "700"}.get(w, w)
        out[(fam.lower(), w)] = fam
    return out


def font_gaps(fonts_css: str | None, local_css: str | None) -> list[str]:
    """Faces fonts.css declares, for a family the local font map maps, at a weight the map does not declare. Chromium
    prefers the face declared at the weight asked for, so the browser asks for the Windows file and 404s."""
    local = font_faces(local_css)
    mapped = {f for f, _ in local}
    return sorted(f"{name} {w}" for (f, w), name in font_faces(fonts_css).items() if f in mapped and (f, w) not in local)


def _func_src(src: str | None, name: str) -> str:
    m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\Z)", src or "", re.S)
    return m.group(0) if m else ""


def _hook_text(src: str | None) -> str:
    return "\n".join(m.group(0) for m in re.finditer(r'^(?:MSG_)?HOOK = """.*?"""', src or "", re.S | re.M))


def _schema(src: str | None) -> int | None:
    try:
        return int(json.loads(src or "{}").get("schema") or 0) or None
    except (ValueError, TypeError):
        return None


# ---- the context and the check -----------------------------------------------------------------------------------

def context(root: Path, port: int, block: dict | None, inv: dict | None, home_set: bool = False) -> dict:
    root = Path(root)
    is_git = git_dir(root) is not None
    rel = platform.release()
    plat = "WSL" if "microsoft" in rel.lower() else platform.system()
    remote, branch = upstream(root) if is_git else (None, None)
    return {
        "platform": {"system": plat, "release": rel, "python": platform.python_version()},
        "checkout": {"git": is_git, "branch": head_cheap(root)[0] if is_git else None,
                     "upstream": f"{remote}/{branch}" if remote else None, "remote": remote, "remote_branch": branch},
        "supervisor": supervisor(),
        "home": {"qm_home": home_set, "port": port},
        "page": {"agent_port": port, "container": container(root, (block or {}).get("container"))},
        "templates": templates_in_use(inv),
        "hooks": hooks_installed(root) if is_git else {},
        "font_map": (root / "ui" / "fonts" / "local.css").exists(),
        "others": others_on_checkout(root),
    }


def verify(block: dict | None, root: Path, ctx: dict) -> list[str]:
    """Where the inventory's `self` block no longer describes this installation. The probe never replaces it."""
    if not block:
        return ["the inventory has no self block: run `qm detect` (or `python agent/setup.py detect`) to record how this installation restarts"]
    out = []
    if block.get("repo") and os.path.normcase(str(Path(block["repo"]).resolve())) != os.path.normcase(str(Path(root).resolve())):
        out.append(f"self.repo is {block['repo']}, but this agent runs from {root}")
    co = ctx["checkout"]
    if co["upstream"] and (block.get("remote"), block.get("branch")) != (co["remote"], co["remote_branch"]):
        out.append(f"self names {block.get('remote')}/{block.get('branch')}, but the checkout follows {co['upstream']}")
    want, live = (block.get("restart") or {}), ctx["supervisor"]
    if want.get("via") and want.get("via") != live.get("via"):
        out.append(f"self.restart says {want.get('via')}, but this agent is kept by: {live.get('detail')}")
    elif want.get("via") == "systemd-user" and want.get("unit") and want.get("unit") != live.get("unit"):
        out.append(f"self.restart names {want['unit']}, but this agent runs in {live.get('unit')}")
    c = ctx["page"]["container"]
    if block.get("container") and not (c and c.get("found")):
        out.append(f"self.container names a container that was not found ({(c or {}).get('error') or 'no container with that service for this checkout'})")
    return out


def classify(root: Path, target: str, dirty: list[str]) -> dict:
    """Where HEAD stands against `target`: behind/ahead, the path, the commits that matter."""
    rc, base, _ = git(root, "merge-base", "--is-ancestor", "HEAD", target)
    ff = rc == 0
    behind = int((git(root, "rev-list", "--count", f"HEAD..{target}")[1] or "0").strip() or 0)
    rc, o, _ = git(root, "log", "--format=%H%x1f%T%x1f%h%x1f%s", f"{target}..HEAD", timeout=60)
    local = [dict(zip(("sha", "tree", "short", "subject"), l.split("\x1f"))) for l in o.splitlines() if l.count("\x1f") == 3]
    out = {"ff": ff, "behind": behind, "ahead": len(local), "local": [], "since": "HEAD", "dirty": dirty}
    if behind == 0:                                     # nothing upstream that is not here: current, local commits or not
        out["path"] = "A"
        out["local"] = [{k: c[k] for k in ("short", "subject")} for c in local]
        return out
    if ff:
        out["path"] = "F" if dirty else None
        return out
    # diverged: every local commit's content must be upstream for a reset to lose nothing (the tree-accounting proof)
    rc, o, _ = git(root, "log", "--format=%T%x1f%H", target, timeout=60)
    trees = {}
    for l in o.splitlines():
        t, h = (l.split("\x1f") + [""])[:2]
        trees.setdefault(t, h)
    missing = [c for c in local if c["tree"] not in trees]
    out["local"] = [{"short": c["short"], "subject": c["subject"], "accounted": c["tree"] in trees} for c in local]
    if missing:
        out["path"] = "E"
        return out
    out["path"] = "F" if dirty else "D"
    rc, ht, _ = git(root, "rev-parse", "HEAD^{tree}")
    since = trees.get(ht.strip())                       # HEAD's own content, found again upstream: the update starts there
    if since:
        out["since"] = since
        out["behind"] = int((git(root, "rev-list", "--count", f"{since}..{target}")[1] or "0").strip() or 0)
    return out


def check(root: Path, block: dict | None, inv: dict | None, port: int, say=lambda s: None, home_set: bool = False) -> dict:
    root = Path(root)
    t0 = time.time()
    if git_dir(root) is None:
        return _archive(root, block, say)
    ctx = context(root, port, block, inv, home_set)
    co = ctx["checkout"]
    mismatches = verify(block, root, ctx)
    branch, head = head_cheap(root)
    base = {"ok": True, "id": ID, "installed": state(root, {})["describe"] or version_file(root), "installed_head": head,
            "version": version_file(root), "context": ctx, "mismatches": mismatches, "can_apply": False, "apply_note": APPLY_NOTE}
    if not co["upstream"]:
        say("no upstream: comparing against the remote's default branch")
        return {**base, **_no_upstream(root, branch, head, say)}
    remote = co["remote"]
    say(f"git fetch {remote}")
    rc, o, e = git(root, "fetch", remote, timeout=300)
    if rc:
        msg = (e or o).strip()
        if re.search(r"terminal prompts disabled|could not read Username|Authentication failed|Permission denied \(publickey", msg):
            return {**base, "ok": False, "error": f"the fetch needs credentials; run `git -C {root} fetch {remote}` once yourself"}
        return {**base, "ok": False, "error": f"git fetch failed: {msg[-240:] or 'no message (network?)'}"}
    note = ""
    rc, o, e = git(root, "fetch", "--tags", remote, timeout=300)     # best effort: a rewritten upstream's tags would clobber
    if rc and "clobber" in (o + e):
        note = "upstream's tags differ from this checkout's (a rewritten history); the update refreshes them"
    rc, target, _ = git(root, "rev-parse", co["upstream"])
    target = target.strip()
    if rc or not target:
        return {**base, "ok": False, "error": f"{co['upstream']} could not be read after the fetch"}
    say("reading the checkout and the path")
    pos = classify(root, target, dirty_tracked(root))
    path = pos["path"]
    since = pos["since"]
    changed = changed_paths(root, "HEAD", target) if path != "A" else []
    added = [p for s, p in changed if s == "A"]
    collisions = [p for p in added if (root / p).exists()]
    if path is None:
        path = "G" if collisions else ("C" if any(p.startswith(AGENT_PATHS) for _, p in changed) else "B")
    elif path == "D" and collisions:
        path = "G"
    moving = path in ("B", "C", "D")
    restart = any(p.startswith(AGENT_PATHS) for _, p in changed)
    flags = _flags(root, target, changed, ctx, block) if moving and changed else []
    incoming, total = _incoming(root, since, target)
    tshort = target[:7]
    tver = (show(root, target, "VERSION") or "").strip().splitlines()[0:1]
    tver = tver[0] if tver else None
    rec = {**base, "path": path, "path_label": PATHS[path], "behind": pos["behind"], "ahead": pos["ahead"],
           "local": pos["local"], "dirty": pos["dirty"], "collisions": collisions, "restart": restart and moving,
           "target": target, "target_short": tshort, "target_version": tver,
           "latest": f"{tver or '?'} ({tshort})" if pos["behind"] else base["installed"],
           "available": pos["behind"] > 0 and moving, "incoming": incoming, "incoming_total": total,
           "whats_new": _whats_new(root, since if path == "D" else "HEAD", target) if pos["behind"] else [],
           "flags": flags, "note": note, "took_s": round(time.time() - t0, 1)}
    rec["steps"] = steps(rec, ctx, block, root)
    why = blocker(rec, block)
    rec.update(can_apply=why is None and rec["available"], apply_blocked=why if rec["available"] else None,
               apply_note=APPLY_NOTE if why is None else BLOCKED_NOTE.format(why=why))
    return rec


def _incoming(root: Path, since: str, target: str, cap: int = 30) -> tuple[list[dict], int]:
    rc, o, _ = git(root, "log", "--format=%h%x1f%s%x1f%ae%x1f%cs", f"{since}..{target}", timeout=60)
    rows = [dict(zip(("short", "subject", "author", "date"), l.split("\x1f"))) for l in o.splitlines() if l.count("\x1f") == 3] if rc == 0 else []
    return rows[:cap], len(rows)


def _whats_new(root: Path, a: str, b: str, cap: int = 40) -> list[str]:
    rc, o, _ = git(root, "diff", "--unified=0", a, b, "--", "CHANGELOG.md", timeout=30)
    lines = [l[1:] for l in o.splitlines() if l.startswith("+") and not l.startswith("+++")] if rc == 0 else []
    return [l for l in lines if l.strip()][:cap]


def _flags(root: Path, target: str, changed: list[tuple[str, str]], ctx: dict, block: dict | None) -> list[dict]:
    paths = [p for _, p in changed]
    out = []
    for d in ctx["templates"]:
        mine = [p for p in paths if p.startswith(d)]
        if mine:
            out.append({"id": "root", "who": "you", "text": f"{TEMPLATE_DIRS.get(d, d)} changed upstream ({', '.join(Path(p).name for p in mine)}). "
                        f"Merge the change into your installed copy and install it as root, as {d}README.md says; Quartermaster never runs it.",
                        "cmd": f"git -C {root} diff HEAD {target[:7]} -- {d}"})
    if _func_src(show(root, "HEAD", "agent/setup.py"), "launchers") != _func_src(show(root, target, "agent/setup.py"), "launchers"):
        sup = ctx["supervisor"]
        where = sup.get("fragment") or ("the Startup launcher" if sup.get("via") == "respawn" else "the launcher setup wrote")
        out.append({"id": "launcher", "who": "you", "text": f"setup now writes a different launcher; compare it with {where} (setup.py's launchers())."})
    if ctx["font_map"]:
        local = (root / "ui" / "fonts" / "local.css").read_text(encoding="utf-8", errors="replace")
        new = sorted(set(font_gaps(show(root, target, "ui/fonts.css"), local)) - set(font_gaps(show(root, "HEAD", "ui/fonts.css"), local)))
        if new:
            out.append({"id": "fonts", "who": "you", "text": f"fonts.css now declares {', '.join(new)}, which your font map maps only at other weights: "
                        "add a rule at that weight to ui/fonts/local.css, or the browser asks for the Windows file."})
    a, b = _schema(show(root, "HEAD", "agent/inventory.example.json")), _schema(show(root, target, "agent/inventory.example.json"))
    if a and b and b > a:
        out.append({"id": "schema", "who": "agent", "text": f"the inventory format moves from schema {a} to {b}: the agent migrates it at start and keeps the original."})
    cats = [Path(p).stem for s, p in changed if s == "A" and p.startswith("catalog/") and p.endswith(".json")]
    if cats:
        out.append({"id": "catalog", "who": "you", "text": f"new catalog records ({', '.join(cats)}): run `qm detect` afterwards to see whether any is installed here."})
    hk = ctx["hooks"]
    if any(hk.values()) and "tools/scrub_check.py" in paths and _hook_text(show(root, "HEAD", "tools/scrub_check.py")) != _hook_text(show(root, target, "tools/scrub_check.py")):
        out.append({"id": "hooks", "who": "agent", "text": "the scrub hooks' text changed: reinstall them.", "cmd": f"python {root / 'tools' / 'scrub_check.py'} --install-hook"})
    sens = sorted({p for p in paths if p.startswith(SENSITIVE)})
    if sens:
        out.append({"id": "sensitive", "who": "you", "text": f"code outside the page changes: {', '.join(sens[:8])}{' and more' if len(sens) > 8 else ''}. Read it before it runs."})
    c = ctx["page"]["container"]
    if c and c.get("found") and any(p.startswith(CONTAINER_PATHS) for p in paths):
        out.append({"id": "container", "who": "agent", "text": f"the container {c['name']} copies the page at build time: it is rebuilt after the update (this may pull its base image)."})
    return out


def steps(rec: dict, ctx: dict, block: dict | None, root: Path) -> list[dict]:
    """The plan as stage 2 will run it, each step marked agent or you, with the command that does it by hand now."""
    path, short, co = rec["path"], rec.get("target_short") or "", ctx["checkout"]
    g = f"git -C {root}"
    out = []
    if path == "E":
        return [{"who": "you", "text": "commits here that upstream lacks: push them, or set them aside before updating: "
                 + ", ".join(f"{c['short']} {c['subject'][:50]}" for c in rec["local"] if not c.get("accounted"))}]
    if path == "F":
        return [{"who": "you", "text": "commit, stash or discard these first: " + ", ".join(rec["dirty"][:12]), "cmd": f"{g} status"}]
    if path == "G":
        return [{"who": "you", "text": "move these aside first; an incoming commit adds a file at each: " + ", ".join(rec["collisions"][:12])}]
    if path == "A":
        return []
    if path == "D":
        out.append({"who": "agent", "text": f"reset to {short}: upstream rewrote its history, and every commit made here is there under a new hash",
                    "cmd": f"{g} reset --hard {short}"})
        out.append({"who": "agent", "text": "refresh the tags from upstream", "cmd": f"{g} fetch --tags --force {co['remote']}"})
    else:
        out.append({"who": "agent", "text": f"fast-forward to {short}", "cmd": f"{g} merge --ff-only {short}"})
    if rec.get("restart"):
        out.append({"who": "agent", "text": "test-run the new code before the running agent is touched",
                    "cmd": f"{Path(sys.executable).name} -m py_compile {root / 'agent'}{os.sep}*.py"})
        sup = (block or {}).get("restart") or {}
        via = sup.get("via") or ctx["supervisor"].get("via")
        unit = sup.get("unit") or ctx["supervisor"].get("unit") or "quartermaster.service"
        if via == "systemd-user":
            out.append({"who": "agent", "text": f"restart through {unit}, from a helper outside its control group", "cmd": f"systemctl --user restart {unit}"})
        elif via == "respawn":
            out.append({"who": "agent", "text": "start the new agent once the old one has let go of its port "
                        "(by hand: end pythonw running quartermaster_agent.py in Task Manager, then run Quartermaster Agent.cmd)"})
        else:
            out.append({"who": "you", "text": "restart the agent: nothing supervises it here, so the update stops after the code moves"})
        out.append({"who": "agent", "text": f"watch until the agent reports {short}; roll back by itself if it does not come up", "cmd": "qm status"})
    for f in rec["flags"]:
        if f["id"] == "container":              # the agent rebuilds it from stage 5; until then it is the owner's step
            b = (block or {}).get("container") or {}
            out.append({"who": "you", "text": f["text"], "cmd": f"docker compose -f {b.get('compose_file') or root / 'docker-compose.yml'} up -d --build {b.get('service') or 'quartermaster'}"})
        elif f["id"] == "hooks":
            out.append({"who": "you", "text": f["text"], "cmd": f.get("cmd")})
        elif f["id"] in ("root", "fonts", "launcher", "catalog"):
            out.append({k: v for k, v in f.items() if k != "id"})
    out.append({"who": "you", "text": "reload any open page once the update is done"})
    return out


def _no_upstream(root: Path, branch: str | None, head: str | None, say) -> dict:
    rc, o, _ = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    default = o.strip() if rc == 0 else None
    behind = 0
    if default:
        git(root, "fetch", "origin", timeout=300)
        behind = int((git(root, "rev-list", "--count", f"HEAD..{default}")[1] or "0").strip() or 0)
    why = "a detached HEAD" if not branch else f"branch {branch} has no upstream"
    return {"path": "H", "path_label": PATHS["H"], "behind": behind, "available": False, "latest": default and f"{default} ({behind} ahead of here)",
            "note": f"{why}; compared with {default or 'nothing (no default branch known)'}; update by hand", "steps": [], "flags": [],
            "incoming": [], "incoming_total": 0, "whats_new": []}


def _archive(root: Path, block: dict | None, say) -> dict:
    v = version_file(root)
    gh = (block or {}).get("github")
    rec = {"ok": True, "id": ID, "path": "I", "path_label": PATHS["I"], "installed": v, "version": v, "available": False,
           "can_apply": False, "steps": [], "flags": [], "incoming": [], "whats_new": [], "mismatches": [],
           "context": {"platform": {"system": platform.system(), "release": platform.release(), "python": platform.python_version()},
                       "checkout": {"git": False}, "supervisor": supervisor()}}
    if not gh:
        return {**rec, "note": "not a git checkout, and the self block names no github repository to compare with"}
    import updaters
    say(f"asking GitHub for {gh}'s newest release")
    tag, url = updaters.github_latest(gh)
    if not tag:
        return {**rec, "ok": False, "error": f"GitHub did not answer: {url}"}
    newer = updaters.newer(tag, v)
    return {**rec, "latest": tag.lstrip("v"), "available": newer,
            "note": f"an archive install: download {tag} from {url} and unpack it over this one by hand" if newer else "current"}


def settle(rec: dict | None, head: str | None) -> dict | None:
    """The check's record as of now. A checkout moved by hand to what the check found is current; one moved
    elsewhere makes the record stale until the next check."""
    if not rec or not rec.get("ok") or not head or not rec.get("installed_head"):
        return rec
    if rec.get("target") == head and rec.get("path") != "A":       # updated, by the button or by hand, to what it found
        return {**rec, "available": False, "can_apply": False, "path": "A", "steps": [],
                "path_label": "current (updated here since the check)" if rec.get("applied_at") else "current (moved here since the check)"}
    if head != rec["installed_head"] and head != rec.get("target"):
        return {**rec, "available": False, "stale": True, "note": "the checkout moved since this check: check again"}
    return rec


def propose_block(root: Path, port: int) -> dict:
    """The self block for this installation, from what the operating system says now (setup and detect ask first)."""
    root = Path(root)
    remote, branch = upstream(root) if git_dir(root) else (None, None)
    if not remote and git_dir(root):
        remote, branch = "origin", head_cheap(root)[0]
    pids = host.pid_listening(port)
    if pids:
        sup = supervisor(pids[0])
    elif host.WINDOWS:
        sup = {"via": "respawn"}
    else:
        unit = Path.home() / ".config" / "systemd" / "user" / "quartermaster.service"
        sup = {"via": "systemd-user", "unit": unit.name} if unit.exists() else {"via": "none"}
    restart = {"via": sup.get("via") if sup.get("via") in ("systemd-user", "respawn") else "none"}
    if restart["via"] == "systemd-user":
        restart["unit"] = sup.get("unit") or "quartermaster.service"
    c = container(root, None)
    block = {"repo": str(root), "remote": remote, "branch": branch, "restart": restart,
             "container": {"compose_file": str(root / "docker-compose.yml"), "service": c.get("service", "quartermaster")} if c and c.get("found") else None,
             "require_signed": False}
    if not git_dir(root):
        block["github"] = "lyricalvanity/quartermaster"
    return block


# ---- stage 2: the update ---------------------------------------------------------------------------------------------

STAMP_FMT = "%Y%m%d-%H%M%S"


def blocker(rec: dict, block: dict | None) -> str | None:
    """Why Update is not offered for this check, or None. Paths D (stage 4) and a Windows restart (stage 3) are later
    stages; the refusing paths say their own reason in their steps."""
    path = rec.get("path")
    if path in ("E", "F", "G", "H", "I", "A"):
        return {"A": "nothing to update", "E": "commits here that upstream lacks", "F": "uncommitted changes",
                "G": "untracked files in the way", "H": "no upstream", "I": "not a git checkout"}[path]
    if path == "D":
        return "upstream rewrote its history: the reset comes in a later stage; the steps below do it by hand"
    if not block:
        return "the inventory has no self block: run `qm detect`"
    if rec.get("mismatches"):
        return "the self block does not match how the agent runs: run `qm detect`"
    if (rec.get("context") or {}).get("others"):
        return "another agent runs from this checkout"
    via = (block.get("restart") or {}).get("via")
    if rec.get("restart") and via == "respawn":
        return "restarting on Windows comes in a later stage"
    if rec.get("restart") and via == "systemd-user" and not shutil.which("systemd-run"):
        return "systemd-run is not on PATH, so the restart cannot run outside the agent's unit"
    if rec.get("restart") and via not in ("systemd-user", "none"):
        return f"no restart the agent can take ({via})"
    return None


def ready(root: Path, rec: dict | None, block: dict | None) -> str | None:
    """Just before anything moves: the check still describes the checkout. Returns the reason not to go, or None."""
    if not rec or not rec.get("ok"):
        return "run Check first"
    if rec.get("stale"):
        return "the checkout moved since the check: check again"
    why = blocker(rec, block)
    if why or not rec.get("available"):
        return why or "the last check found nothing to update"
    if head_cheap(root)[1] != rec.get("installed_head"):
        return "the checkout moved since the check: check again"
    if git(root, "cat-file", "-e", f"{rec['target']}^{{commit}}")[0]:
        return f"{rec['target'][:7]} is not in this checkout any more: check again"
    if dirty_tracked(root):
        return "uncommitted changes appeared since the check: " + ", ".join(dirty_tracked(root)[:6])
    added = [p for st, p in changed_paths(root, "HEAD", rec["target"]) if st == "A" and (root / p).exists()]
    if added:
        return "untracked files are now in the way: " + ", ".join(added[:6])
    if others_on_checkout(root):
        return "another agent runs from this checkout"
    live, want = supervisor(), (block.get("restart") or {})
    if rec.get("restart") and want.get("via") == "systemd-user" and (live.get("via"), live.get("unit")) != ("systemd-user", want.get("unit")):
        return f"the self block says {want.get('unit')}, but this agent is kept by: {live.get('detail')}"
    return None


def _sha(path: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def prune(log_dir: Path, keep: int = 3) -> None:
    base = Path(log_dir) / "self-update"
    olds = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name) if base.is_dir() else []
    for d in olds[:-keep] if len(olds) > keep else []:
        shutil.rmtree(d, ignore_errors=True)


def prepare(root: Path, home: Path, log_dir: Path, port: int, kind: str, frm: str, to: str, block: dict, path: str,
            restore_files: bool, files_from: Path | None = None) -> Path:
    """The update's folder: record.json for the helper and the page, copies of the inventory and presets, and the
    helper itself, taken from the tree running now (a broken new tree cannot break the way back)."""
    prune(log_dir, keep=2)
    folder = Path(log_dir) / "self-update" / f"{time.strftime(STAMP_FMT)}-{kind}"
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for name in ("inventory.json", "presets.json"):
        live = Path(home) / name
        src = (Path(files_from) / name) if files_from else live
        if src.exists():
            shutil.copy2(src, folder / name)
            files.append({"path": str(live), "sha256": _sha(live), "copy": name})
    helper = Path(root) / "agent" / "selfupdate_helper.py"
    if helper.exists():
        shutil.copy2(helper, folder / "selfupdate_helper.py")
    rec = {"kind": kind, "stamp": folder.name, "root": str(root), "port": port, "key_file": str(Path(home) / "agent.key"),
           "restart": block.get("restart") or {"via": "none"}, "from": frm, "to": to, "path": path, "files": files,
           "restore_files": restore_files, "watch_s": 60, "settle_s": 1.5, "state": "prepared", "result": None,
           "started": time.time(), "version_from": version_file(root), "log": []}
    (folder / "record.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return folder


def record(folder: Path, **kw) -> dict:
    f = Path(folder) / "record.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    if kw:
        rec.update(kw)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    return rec


def move(root: Path, target: str) -> tuple[bool, str]:
    rc, o, e = git(root, "merge", "--ff-only", target, timeout=120)
    return rc == 0, (o + e).strip()[-300:]


def reset(root: Path, head: str) -> tuple[bool, str]:
    rc, o, e = git(root, "reset", "--hard", head, timeout=120)
    return rc == 0, (o + e).strip()[-300:]


def preflight(root: Path, timeout: int = 180) -> tuple[bool, dict]:
    """The new code, run once beside the agent still serving: `quartermaster_agent.py --selftest` (same interpreter,
    same environment, so the same QM_HOME and inventory)."""
    rc, o, e = run([sys.executable, Path(root) / "agent" / "quartermaster_agent.py", "--selftest"], cwd=Path(root) / "agent",
                   timeout=timeout, env=os.environ.copy())
    line = next((l for l in reversed(o.splitlines()) if l.startswith("{")), "")
    try:
        out = json.loads(line)
    except ValueError:
        out = {"ok": False, "error": (e or o).strip()[-1200:] or f"exit {rc}, no output"}
    out["ok"] = bool(out.get("ok")) and rc == 0
    return out["ok"], out


def hand_over(folder: Path) -> tuple[bool, str]:
    """Start the helper copy in a transient user unit, outside the agent's control group: the agent's own restart
    kills everything in its group (KillMode=control-group), a child of the agent included."""
    unit = f"qm-selfupdate-{Path(folder).name}"
    rc, o, e = run(["systemd-run", "--user", "--collect", "--unit", unit, "--quiet", sys.executable,
                    Path(folder) / "selfupdate_helper.py", folder], timeout=30)
    return rc == 0, (o + e).strip()[-300:] or unit


_LAST: dict = {}


def last_update(log_dir: Path, head: str | None) -> dict | None:
    """The newest update's record, summarised for the state (read again only when it changes)."""
    base = Path(log_dir) / "self-update"
    try:
        newest = max((d for d in base.iterdir() if (d / "record.json").exists()), key=lambda d: d.name)
    except (OSError, ValueError):
        return None
    f = newest / "record.json"
    mt = f.stat().st_mtime
    if _LAST.get("key") != (str(f), mt):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        _LAST.update(key=(str(f), mt), rec=r)
    r = _LAST["rec"]
    ok = r.get("result") in ("updated", "moved; restart pending")
    return {"kind": r.get("kind"), "stamp": r.get("stamp"), "from": r.get("from"), "to": r.get("to"), "path": r.get("path"),
            "state": r.get("state"), "result": r.get("result"), "via": (r.get("restart") or {}).get("via"), "folder": str(newest),
            "error": r.get("error"), "log": (r.get("log") or [])[-8:], "version_from": r.get("version_from"),
            "can_rollback": bool(ok and r.get("kind") == "update" and head and head == r.get("to"))}


def restore_files(folder: Path) -> list[str]:
    """A rollback of an update that migrated the inventory: put the pre-update inventory and presets back before the
    old code starts. The file as it is now is kept beside the copy as <name>.after-update."""
    rec, said = record(folder), []
    for f in rec.get("files") or []:
        live, keep = Path(f["path"]), Path(folder) / f["copy"]
        if keep.exists() and _sha(live) != _sha(keep):
            if live.exists():
                shutil.copy2(live, Path(folder) / f"{live.name}.after-update")
            shutil.copy2(keep, live)
            said.append(f"{live.name} restored (the changed one kept as {live.name}.after-update)")
    return said
