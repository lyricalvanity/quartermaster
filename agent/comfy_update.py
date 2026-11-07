"""Update ComfyUI to its newest release tag without touching the pinned torch stack, and undo it.

Run by the Quartermaster agent (op comfy_update), never by a request's arguments: the paths come from the
inventory's comfyui.update block. Prints one JSON object on its last line.

  python comfy_update.py check    <repo> <venv_python> [<constraint names>]
  python comfy_update.py apply    <repo> <venv_python> <backup_root> <tag> [<constraint names>]
  python comfy_update.py rollback <repo> <venv_python> <backup_dir> [<constraint names>]

check     git fetch --tags; the newest vX.Y.Z tag; whether HEAD fast-forwards to it; what pip would change
          (pip install --dry-run --report with the constraints).
apply     refuses a dirty tree; writes <backup_root>/<time>/{head.txt, freeze.txt, constraints.txt};
          git merge --ff-only <tag> on the current branch; pip install -r requirements.txt -c constraints.txt.
rollback  git reset --hard to backup head.txt (the tree was clean when it was taken), then
          pip install -r freeze.txt -c constraints.txt (the freeze without the constrained and file:// lines).

The constraints pin the installed versions of the named packages (default torch, torchvision, torchaudio,
triton-windows): this venv runs a cu130 nightly that pip cannot fetch again, and the ComfyUI reference card
records a broken boot after an unguarded `pip install -r` (2026-10-05). If a requirement cannot be met
without moving a pinned package, pip fails and nothing is half-installed past that point.

Written 2026-11-05 (Faber). Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DEFAULT_PINS = ["torch", "torchvision", "torchaudio", "triton-windows"]


ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PIP_NO_COLOR="1", PIP_PROGRESS_BAR="off")


def sh(args, cwd=None, timeout=900):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NO_WINDOW, env=ENV)
    return p.returncode, p.stdout, p.stderr


def git(repo, *args, timeout=300):
    return sh(["git", "-C", repo, *args], timeout=timeout)


def out(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def fail(msg, **kw):
    out(dict(ok=False, error=msg, **kw), 1)


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def freeze(py):
    rc, o, e = sh([py, "-m", "pip", "freeze", "--all"], timeout=300)
    if rc:
        fail("pip freeze failed: " + e[-300:])
    return [l for l in o.splitlines() if l.strip()]


def constraints(lines, pins):
    want = {norm(p) for p in pins}
    cons = []
    for l in lines:
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(.+)$", l)
        if m and norm(m.group(1)) in want:
            cons.append(l)
    missing = want - {norm(c.split("==")[0]) for c in cons}
    return cons, sorted(missing)


def describe(repo):
    rc, o, _ = git(repo, "describe", "--tags")
    rc2, h, _ = git(repo, "rev-parse", "HEAD")
    rc3, b, _ = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return {"describe": o.strip() if rc == 0 else None, "head": h.strip(), "branch": b.strip()}


def newest_tag(repo):
    rc, o, e = git(repo, "tag", "-l", "v*")
    tags = [t for t in o.split() if TAG_RE.match(t)]
    if not tags:
        return None
    return max(tags, key=lambda t: tuple(int(x) for x in TAG_RE.match(t).groups()))


def pip_plan(py, req, cons_file):
    rc, o, e = sh([py, "-m", "pip", "install", "--dry-run", "--quiet", "--report", "-", "-r", req, "-c", cons_file],
                  timeout=600)
    if rc:
        return None, (e or o)[-600:]
    try:
        rep = json.loads(o[o.index("{"):])
    except ValueError:
        return None, "pip's report was not JSON"
    return [f"{i['metadata']['name']}=={i['metadata']['version']}" for i in rep.get("install", [])], None


def check(repo, py, pins, tmp_dir):
    rc, o, e = git(repo, "fetch", "origin", "--tags", "--quiet", timeout=300)
    if rc:
        fail("git fetch failed: " + (e or o)[-300:])
    cur = describe(repo)
    tag = newest_tag(repo)
    if not tag:
        fail("no release tags found")
    rc, tag_sha, _ = git(repo, "rev-list", "-n", "1", tag)
    up_to_date = tag_sha.strip() == cur["head"] or git(repo, "merge-base", "--is-ancestor", tag, "HEAD")[0] == 0
    ff = git(repo, "merge-base", "--is-ancestor", "HEAD", tag)[0] == 0
    dirty = bool(git(repo, "status", "--porcelain", "--untracked-files=no")[1].strip())
    res = dict(ok=True, current=cur, latest=tag, up_to_date=up_to_date, fast_forward=ff, dirty=dirty, changes=None)
    if not up_to_date and ff:
        # the requirements of the tag, read without checking it out
        rc, req, _ = git(repo, "show", f"{tag}:requirements.txt")
        lines = freeze(py)
        cons, missing = constraints(lines, pins)
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        rq = Path(tmp_dir) / "requirements-next.txt"
        cf = Path(tmp_dir) / "constraints.txt"
        rq.write_text(req, encoding="utf-8")
        cf.write_text("\n".join(cons) + "\n", encoding="utf-8")
        plan, err = pip_plan(py, str(rq), str(cf))
        res.update(pinned=cons, pins_missing=missing, changes=plan, pip_error=err)
        if err:
            res["ok"] = False
            res["error"] = "pip's dry run failed (a requirement may need a pinned package moved): " + err[-300:]
    out(res, 0 if res["ok"] else 1)


def apply(repo, py, backup_root, tag, pins):
    if not TAG_RE.match(tag):
        fail("not a release tag")
    if git(repo, "status", "--porcelain", "--untracked-files=no")[1].strip():
        fail("ComfyUI's tree has local changes; not updating over them")
    if git(repo, "merge-base", "--is-ancestor", "HEAD", tag)[0] != 0:
        fail(f"HEAD does not fast-forward to {tag}")
    bdir = Path(backup_root) / time.strftime("%Y%m%d-%H%M%S")
    bdir.mkdir(parents=True, exist_ok=False)
    cur = describe(repo)
    lines = freeze(py)
    cons, missing = constraints(lines, pins)
    (bdir / "head.txt").write_text(json.dumps(cur), encoding="utf-8")
    (bdir / "freeze.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (bdir / "constraints.txt").write_text("\n".join(cons) + "\n", encoding="utf-8")
    rc, o, e = git(repo, "merge", "--ff-only", tag)
    if rc:
        fail("git merge --ff-only failed: " + (e or o)[-300:], backup=str(bdir))
    rc, o, e = sh([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(Path(repo) / "requirements.txt"),
                   "-c", str(bdir / "constraints.txt")], timeout=1800)
    (bdir / "pip-install.log").write_text(o + "\n" + e, encoding="utf-8")
    if rc:
        fail("pip install failed (the code is updated; roll back): " + (e or o)[-400:], backup=str(bdir), code_updated=True)
    after = freeze(py)
    changed = sorted(set(after) - set(lines))
    out(dict(ok=True, backup=str(bdir), before=cur, after=describe(repo), changed=changed, pins_missing=missing))


def rollback(repo, py, bdir, pins):
    bdir = Path(bdir)
    head = json.loads((bdir / "head.txt").read_text(encoding="utf-8"))
    rc, o, e = git(repo, "reset", "--hard", head["head"])
    if rc:
        fail("git reset failed: " + (e or o)[-300:])
    want = {norm(p) for p in pins}
    keep = [l for l in (bdir / "freeze.txt").read_text(encoding="utf-8").splitlines()
            if l.strip() and "@ file:" not in l and norm(l.split("==")[0]) not in want]
    rq = bdir / "restore.txt"
    rq.write_text("\n".join(keep) + "\n", encoding="utf-8")
    rc, o, e = sh([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-r", str(rq),
                   "-c", str(bdir / "constraints.txt")], timeout=1800)
    (bdir / "pip-rollback.log").write_text(o + "\n" + e, encoding="utf-8")
    if rc:
        fail("pip restore failed: " + (e or o)[-400:], code_restored=True)
    out(dict(ok=True, restored=head, now=describe(repo)))


def main(a):
    if len(a) < 3:
        fail(__doc__)
    mode, repo, py = a[0], a[1], a[2]
    if mode == "check":
        pins = a[3].split(",") if len(a) > 3 and a[3] else DEFAULT_PINS
        return check(repo, py, pins, str(Path(repo).parent / "update-backups" / "_check"))
    if mode == "apply" and len(a) >= 5:
        pins = a[5].split(",") if len(a) > 5 and a[5] else DEFAULT_PINS
        return apply(repo, py, a[3], a[4], pins)
    if mode == "rollback" and len(a) >= 4:
        pins = a[4].split(",") if len(a) > 4 and a[4] else DEFAULT_PINS
        return rollback(repo, py, a[3], pins)
    fail("bad arguments")


if __name__ == "__main__":
    main(sys.argv[1:])
