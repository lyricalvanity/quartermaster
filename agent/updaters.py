"""Update recipes: how an engine or a client is checked for a newer version and moved to it.

A recipe is a dict from the catalog (catalog/<kind>.json, "update") or from the inventory entry that
overrides it. Every recipe has three verbs; each returns a plain dict and never raises for an ordinary
failure (ok False and an error instead):

  check(recipe, ctx)            -> {ok, installed, latest, available, note, can_apply}
  apply(recipe, ctx, latest)    -> {ok, backup, before, after, note}
  rollback(recipe, ctx, backup) -> {ok, note}

ctx: {"id", "name", "root", "python", "exe", "installed", "backup_root", "log"} as far as known.

Every check runs because someone clicked; nothing here is scheduled. A check may reach the network
(npm or pnpm, PyPI, GitHub, winget, git fetch): that is the click. Nothing is ever piped into a shell.
Stdlib only. Written 2026-11-06 (Faber)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PIP_NO_COLOR="1", PIP_PROGRESS_BAR="off", NO_COLOR="1")
VER_RE = re.compile(r"\d+\.\d+(?:\.\d+)*(?:[-+.][0-9A-Za-z.]+)?")
RECIPES = ("npm-global", "pnpm-global", "pip", "winget", "git-pull-pip-e", "git-check", "github-release", "self", "script", "comfy", "driver", "none")


def sh(args, cwd=None, timeout=600):
    try:
        p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW, env=ENV)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def which(name: str, hint: str | None = None) -> str | None:
    return (hint if hint and Path(hint).exists() else None) or shutil.which(name)


def vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-+]", (v or "").lstrip("vb")))


def newer(latest: str | None, installed: str | None) -> bool:
    if not latest or not installed:
        return False
    try:
        return vtuple(latest) > vtuple(installed)
    except TypeError:
        return latest != installed


def token(text: str) -> str | None:
    m = VER_RE.search(text or "")
    return m.group(0) if m else None


def result(ok=True, **kw) -> dict:
    return dict(ok=ok, **kw)


# ---- npm-global, pnpm-global ----------------------------------------------------------------
# One package, two managers. A package the manager does not list as global is not applied: installing it
# would put a second copy beside the one on PATH (a pnpm install moved with npm, or the reverse).

NODE_PM = {"npm-global": "npm", "pnpm-global": "pnpm"}


def node_installed(pm: str, pkg: str) -> str | None:
    rc, o, _ = sh([which(pm) or pm, "ls", "-g", pkg, "--json", "--depth=0"], timeout=120)
    try:
        d = json.loads(o or "{}")
    except ValueError:
        return None
    for root in d if isinstance(d, list) else [d]:      # npm prints one root, pnpm a list of them
        v = (((root or {}).get("dependencies") or {}).get(pkg) or {}).get("version")
        if v:
            return v
    return None


def npm_check(recipe, ctx):
    pm, pkg = NODE_PM[recipe["recipe"]], recipe.get("package")
    if not pkg:
        return result(False, error=f"{recipe['recipe']} needs a package name")
    if not which(pm):
        return result(False, installed=ctx.get("installed"), error=f"{pm} is not installed here; if another manager installed {pkg}, set the entry's update recipe to it")
    listed = node_installed(pm, pkg)
    rc, o, e = sh([which(pm), "view", pkg, "version"], timeout=120)
    if rc:
        return result(False, installed=listed or ctx.get("installed"), error=f"{pm} view failed: {(e or o).strip()[-200:]}")
    latest = o.strip().splitlines()[-1].strip() if o.strip() else None
    if not listed:
        return result(installed=ctx.get("installed"), latest=latest, available=newer(latest, ctx.get("installed")), can_apply=False,
                      note=f"{pm} does not list {pkg} as global, so an update through it would add a second copy; set the entry's update recipe to the manager that installed it")
    verb = "add" if pm == "pnpm" else "i"
    return result(installed=listed, latest=latest, available=newer(latest, listed), can_apply=True, note=f"{pm} {verb} -g {pkg}@{latest}")


def npm_apply(recipe, ctx, latest):
    pm, pkg = NODE_PM[recipe["recipe"]], recipe["package"]
    before = node_installed(pm, pkg)
    if not before:
        return result(False, error=f"{pm} does not list {pkg} as global; not installing a second copy")
    rc, o, e = sh([which(pm) or pm, "add" if pm == "pnpm" else "i", "-g", f"{pkg}@{latest}"], timeout=900)
    if rc:
        return result(False, before=before, error=f"{pm} install failed: {(e or o).strip()[-300:]}")
    after = node_installed(pm, pkg)
    return result(backup={"package": pkg, "version": before}, before=before, after=after, note=(o + e).strip()[-200:])


def npm_rollback(recipe, ctx, backup):
    if not backup or not backup.get("version"):
        return result(False, error="no previous version recorded")
    pm = NODE_PM[recipe["recipe"]]
    rc, o, e = sh([which(pm) or pm, "add" if pm == "pnpm" else "i", "-g", f"{backup['package']}@{backup['version']}"], timeout=900)
    return result(rc == 0, note=(e or o).strip()[-200:])


# ---- pip ------------------------------------------------------------------------------------

def pip_installed(py: str, pkg: str) -> str | None:
    rc, o, _ = sh([py, "-m", "pip", "show", pkg], timeout=120)
    m = re.search(r"^Version:\s*(\S+)", o, re.M)
    return m.group(1) if rc == 0 and m else None


def pip_check(recipe, ctx):
    pkg, py = recipe.get("package"), ctx.get("python") or "python"
    if not pkg:
        return result(False, error="pip needs a package name")
    installed = pip_installed(py, pkg) or ctx.get("installed")
    rc, o, e = sh([py, "-m", "pip", "index", "versions", pkg, "--disable-pip-version-check"], timeout=180)
    m = re.search(r"LATEST:\s*(\S+)", o) or re.search(rf"{re.escape(pkg)}\s*\(([^)\s,]+)", o)
    if rc or not m:
        return result(False, installed=installed, error=f"pip index failed: {(e or o).strip()[-200:]}")
    latest = m.group(1)
    return result(installed=installed, latest=latest, available=newer(latest, installed), can_apply=True, note=f"pip install {pkg}=={latest}")


def pip_apply(recipe, ctx, latest):
    pkg, py = recipe["package"], ctx.get("python") or "python"
    before = pip_installed(py, pkg)
    rc, o, e = sh([py, "-m", "pip", "install", "--disable-pip-version-check", f"{pkg}=={latest}"], timeout=1800)
    if rc:
        return result(False, before=before, error=f"pip install failed: {(e or o).strip()[-300:]}")
    return result(backup={"package": pkg, "version": before, "python": py}, before=before, after=pip_installed(py, pkg))


def pip_rollback(recipe, ctx, backup):
    if not backup or not backup.get("version"):
        return result(False, error="no previous version recorded")
    rc, o, e = sh([backup.get("python") or ctx.get("python") or "python", "-m", "pip", "install", "--disable-pip-version-check",
                   f"{backup['package']}=={backup['version']}"], timeout=1800)
    return result(rc == 0, note=(e or o).strip()[-200:])


# ---- winget ---------------------------------------------------------------------------------

def winget_check(recipe, ctx):
    wid = recipe.get("id")
    if not wid:
        return result(False, error="winget needs an id")
    w = which("winget")
    if not w:
        return result(False, error="winget is not on PATH")
    rc, o, e = sh([w, "upgrade", "--id", wid, "--exact", "--accept-source-agreements", "--disable-interactivity"], timeout=300)
    text = o + e
    m = re.search(rf"{re.escape(wid)}\s+(\S+)\s+(\S+)\s+winget", text)
    if m:
        return result(installed=m.group(1), latest=m.group(2), available=True, can_apply=True, note=f"winget upgrade --id {wid}")
    if "No available upgrade" in text or "No installed package" in text or rc == 0:
        return result(installed=ctx.get("installed"), latest=ctx.get("installed"), available=False, can_apply=True, note="winget sees no upgrade")
    return result(False, error=text.strip()[-200:])


def winget_apply(recipe, ctx, latest):
    w = which("winget")
    rc, o, e = sh([w, "upgrade", "--id", recipe["id"], "--exact", "--silent", "--accept-source-agreements", "--accept-package-agreements",
                   "--disable-interactivity"], timeout=1800)
    text = (o + e).strip()
    if rc:
        return result(False, error=f"winget upgrade failed (an installer may need elevation; run it from a console): {text[-300:]}")
    return result(backup=None, before=ctx.get("installed"), after=None, note="winget does not keep the previous installer; there is no rollback")


# ---- git ------------------------------------------------------------------------------------

def git(repo, *args, timeout=300):
    return sh(["git", "-C", repo, *args], timeout=timeout)


def git_state(repo: str) -> dict:
    rc, h, _ = git(repo, "rev-parse", "--short", "HEAD")
    rc2, b, _ = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    rc3, d, _ = git(repo, "describe", "--tags", "--always")
    dirty = bool(git(repo, "status", "--porcelain", "--untracked-files=no")[1].strip())
    return {"head": h.strip(), "branch": b.strip(), "describe": d.strip() if rc3 == 0 else None, "dirty": dirty}


def git_check(recipe, ctx):
    repo = ctx.get("root")
    if not repo or not (Path(repo) / ".git").exists():
        return result(False, error="no git checkout at the engine's root")
    rc, o, e = git(repo, "fetch", "origin", timeout=300)
    if rc:
        return result(False, error=f"git fetch failed: {(e or o).strip()[-200:] or 'no message (credentials? network?)'}")
    tag_note = ""
    rc, o, e = git(repo, "fetch", "--tags", timeout=300)          # best effort: a rewritten upstream refuses to clobber local tags
    if rc and "clobber" in (o + e):
        tag_note = "; upstream's tags differ from yours (rewritten history), tags not compared"
    cur = git_state(repo)
    upstream = f"origin/{cur['branch']}"
    rc, up, _ = git(repo, "rev-parse", "--short", upstream)
    behind = 0
    if rc == 0:
        rc2, n, _ = git(repo, "rev-list", "--count", f"HEAD..{upstream}")
        behind = int(n.strip() or 0) if rc2 == 0 else 0
    rc, tag, _ = git(repo, "describe", "--tags", "--abbrev=0", upstream if rc == 0 else "HEAD")
    latest = f"{up.strip()} ({behind} behind)" if rc == 0 and behind else cur["head"]
    note = f"{cur['describe'] or cur['head']} on {cur['branch']}" + (f"; upstream newest tag {tag.strip()}" if tag.strip() else "") + ("; local changes present" if cur["dirty"] else "") + tag_note
    return result(installed=cur["describe"] or cur["head"], latest=latest, available=behind > 0, can_apply=recipe.get("recipe") == "git-pull-pip-e" and not cur["dirty"],
                  note=note, behind=behind, dirty=cur["dirty"])


def git_pull_apply(recipe, ctx, latest):
    repo, py = ctx.get("root"), ctx.get("python")
    cur = git_state(repo)
    if cur["dirty"]:
        return result(False, error="the checkout has local changes; not pulling over them")
    bdir = Path(ctx.get("backup_root") or (Path(repo).parent / "update-backups")) / f"{ctx.get('id', 'engine')}-{time.strftime('%Y%m%d-%H%M%S')}"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "head.txt").write_text(json.dumps(cur), encoding="utf-8")
    if py:
        rc, o, _ = sh([py, "-m", "pip", "freeze", "--all"], timeout=300)
        (bdir / "freeze.txt").write_text(o, encoding="utf-8")
    rc, o, e = git(repo, "pull", "--ff-only", timeout=600)
    if rc:
        return result(False, backup=str(bdir), error=f"git pull --ff-only failed: {(e or o).strip()[-300:]}")
    note = (o + e).strip()[-200:]
    if py and ((Path(repo) / "pyproject.toml").exists() or (Path(repo) / "setup.py").exists()):
        rc, o, e = sh([py, "-m", "pip", "install", "--disable-pip-version-check", "-e", repo], timeout=1800)
        (bdir / "pip-install.log").write_text(o + "\n" + e, encoding="utf-8")
        if rc:
            return result(False, backup=str(bdir), error=f"pip install -e failed after the pull (roll back): {(e or o).strip()[-300:]}", code_updated=True)
    after = git_state(repo)
    return result(backup=str(bdir), before=cur, after=after, note=note)


def git_rollback(recipe, ctx, backup):
    repo, py = ctx.get("root"), ctx.get("python")
    bdir = Path(backup) if backup else None
    if not bdir or not (bdir / "head.txt").exists():
        return result(False, error="no backup to roll back to")
    head = json.loads((bdir / "head.txt").read_text(encoding="utf-8"))
    rc, o, e = git(repo, "reset", "--hard", head["head"])
    if rc:
        return result(False, error=f"git reset failed: {(e or o).strip()[-200:]}")
    if py and (bdir / "freeze.txt").exists():
        keep = [l for l in (bdir / "freeze.txt").read_text(encoding="utf-8").splitlines() if l.strip() and "@ file:" not in l and " @ " not in l]
        rq = bdir / "restore.txt"
        rq.write_text("\n".join(keep) + "\n", encoding="utf-8")
        sh([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-r", str(rq)], timeout=1800)
        sh([py, "-m", "pip", "install", "--disable-pip-version-check", "-e", repo], timeout=1800)
    return result(note=f"back at {head['head']}")


# ---- github-release (check), self --------------------------------------------------------------

def github_latest(repo: str) -> tuple[str | None, str | None]:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "quartermaster-check"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d.get("tag_name"), d.get("html_url")
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:200]


def github_check(recipe, ctx):
    gh = recipe.get("github")
    if not gh:
        return result(False, error="github-release needs owner/repo")
    tag, url = github_latest(gh)
    if not tag:
        return result(False, error=f"GitHub did not answer: {url}")
    installed = ctx.get("installed")
    return result(installed=installed, latest=tag.lstrip("v"), available=newer(tag, installed), can_apply=bool(recipe.get("self_cmd")),
                  note=(recipe.get("note") or "") + f" ({url})", url=url)


def self_check(recipe, ctx):
    return result(installed=ctx.get("installed"), latest=None, available=None, can_apply=bool(recipe.get("self_cmd")),
                  note=recipe.get("note") or "this program updates itself; Update runs its own command")


def self_apply(recipe, ctx, latest):
    cmd = recipe.get("self_cmd")
    if not cmd:
        return result(False, error="no self_cmd declared")
    cmd = [which(cmd[0], ctx.get("exe")) or cmd[0], *cmd[1:]]
    rc, o, e = sh(cmd, timeout=1800)
    text = (o + e).strip()
    return result(rc == 0, backup=None, before=ctx.get("installed"), after=None, note=text[-300:], error=None if rc == 0 else text[-300:])


# ---- script ---------------------------------------------------------------------------------

def script_check(recipe, ctx):
    cmd = recipe.get("cmd")
    if not cmd:
        return result(False, error="script recipe needs cmd")
    rc, o, e = sh([*cmd, "--check"], cwd=ctx.get("root"), timeout=600)
    if rc:
        return result(False, error=f"check failed: {(e or o).strip()[-300:]}")
    try:
        d = json.loads(o.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return result(False, error="the script's last line was not a JSON object with installed, latest, available")
    return result(installed=d.get("installed", ctx.get("installed")), latest=d.get("latest"), available=bool(d.get("available")), can_apply=True, note=d.get("note") or "")


def script_apply(recipe, ctx, latest):
    rc, o, e = sh([*recipe["cmd"], "--apply"], cwd=ctx.get("root"), timeout=3600)
    text = (o + e).strip()
    backup = None
    try:
        backup = json.loads(text.splitlines()[-1]).get("backup")
    except (ValueError, IndexError, AttributeError):
        pass
    return result(rc == 0, backup=backup, before=ctx.get("installed"), after=None, note=text[-300:], error=None if rc == 0 else text[-300:])


def script_rollback(recipe, ctx, backup):
    rc, o, e = sh([*recipe["cmd"], "--rollback", *( [str(backup)] if backup else [])], cwd=ctx.get("root"), timeout=3600)
    return result(rc == 0, note=(o + e).strip()[-300:])


# ---- dispatch ---------------------------------------------------------------------------------

CHECK = {"npm-global": npm_check, "pnpm-global": npm_check, "pip": pip_check, "winget": winget_check, "git-pull-pip-e": git_check, "git-check": git_check,
         "github-release": github_check, "self": self_check, "script": script_check}
APPLY = {"npm-global": npm_apply, "pnpm-global": npm_apply, "pip": pip_apply, "winget": winget_apply, "git-pull-pip-e": git_pull_apply,
         "github-release": self_apply, "self": self_apply, "script": script_apply}
ROLLBACK = {"npm-global": npm_rollback, "pnpm-global": npm_rollback, "pip": pip_rollback, "git-pull-pip-e": git_rollback, "script": script_rollback}


def check(recipe: dict, ctx: dict) -> dict:
    r = (recipe or {}).get("recipe") or "none"
    if r == "none":
        return result(installed=ctx.get("installed"), latest=None, available=None, can_apply=False, note=(recipe or {}).get("note") or "no update recipe declared")
    if r in ("comfy", "driver"):
        return result(installed=ctx.get("installed"), latest=None, available=None, can_apply=False, note=f"updated by its own {'procedure' if r == 'comfy' else 'driver'} (its tile's Update button)")
    fn = CHECK.get(r)
    if not fn:
        return result(False, error=f"unknown recipe {r!r}; one of {RECIPES}")
    out = fn(recipe, ctx)
    out.setdefault("can_apply", False)
    return out


def apply(recipe: dict, ctx: dict, latest: str | None) -> dict:
    r = (recipe or {}).get("recipe") or "none"
    fn = APPLY.get(r)
    if not fn:
        return result(False, error=f"recipe {r!r} has no apply step; this one is checked by hand")
    return fn(recipe, ctx, latest)


def rollback(recipe: dict, ctx: dict, backup) -> dict:
    fn = ROLLBACK.get((recipe or {}).get("recipe") or "none")
    if not fn:
        return result(False, error="this recipe keeps no backup to roll back to")
    return fn(recipe, ctx, backup)
