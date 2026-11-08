"""Refuse to publish this machine's particulars.

    python tools/scrub_check.py                 scan every tracked file (working tree) and every staged file
    python tools/scrub_check.py --staged        scan what is about to be committed (the pre-commit hook)
    python tools/scrub_check.py --message FILE  scan a commit message (the commit-msg hook)
    python tools/scrub_check.py --install-hook  make this the repository's pre-commit and commit-msg hooks

What it looks for, in every tracked or staged text file, and in a commit message:
  - this machine's identifiers, derived when the script runs: the user name, the home directory (every
    slash spelling), the host name, and the local IPv4 addresses;
  - every pattern in local/scrub-denylist.txt, one regular expression per line, case-insensitive, '#'
    comments. That file is never committed (local/ is ignored): the list itself would be the leak.
It also refuses if any path that must stay untracked (the real inventory, the key, logs, queues, the
Microsoft fonts) is tracked or staged.

Exit 0 and a one-line summary when clean; exit 1 and one line per hit otherwise. Stdlib only."""
from __future__ import annotations

import getpass
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DENYLIST = ROOT / "local" / "scrub-denylist.txt"
# attribution lives here on purpose; the owner's name is the licence's point
EXEMPT = {"LICENSE"}
MUST_IGNORE = ("agent/inventory.json", "agent/inventory.schema1.json", "agent/presets.json", "agent/agent.key")
MUST_IGNORE_DIRS = ("logs/", "artq/", "backup/", "local/")
FONT_OK = {"ui/fonts/Michroma-Regular.ttf"}
BINARY = {".ttf", ".otf", ".woff", ".woff2", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc", ".zip"}
HOOK = """#!/bin/sh
# Quartermaster: refuse a commit that carries this machine's particulars (tools/scrub_check.py).
exec python "$(git rev-parse --show-toplevel)/tools/scrub_check.py" --staged
"""
# a commit message is published too, and the file hook never saw it: a host name reached public main in one
# on 2026-11-07, caught by a reader on another machine
MSG_HOOK = """#!/bin/sh
# Quartermaster: refuse a commit message that carries this machine's particulars (tools/scrub_check.py).
exec python "$(git rev-parse --show-toplevel)/tools/scrub_check.py" --message "$1"
"""


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)}: {out.stderr.strip()}")
    return [l for l in out.stdout.splitlines() if l.strip()]


def machine_patterns() -> list[tuple[str, re.Pattern]]:
    pats: list[tuple[str, re.Pattern]] = []
    user = getpass.getuser()
    if len(user) >= 3:
        pats.append((f"user name {user!r}", re.compile(rf"\b{re.escape(user)}\b", re.I)))
    home = str(Path.home())
    for spelled in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        pats.append((f"home directory {spelled!r}", re.compile(re.escape(spelled), re.I)))
    host = socket.gethostname()
    if len(host) >= 3:
        pats.append((f"host name {host!r}", re.compile(rf"\b{re.escape(host)}\b", re.I)))
    try:
        for ip in socket.gethostbyname_ex(host)[2]:
            if not ip.startswith("127."):
                pats.append((f"local address {ip}", re.compile(re.escape(ip))))
    except OSError:
        pass
    return pats


def denylist_patterns() -> list[tuple[str, re.Pattern]]:
    if not DENYLIST.exists():
        return []
    pats = []
    for n, line in enumerate(DENYLIST.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pats.append((f"denylist line {n} /{line}/", re.compile(line, re.I)))
        except re.error as e:
            sys.exit(f"{DENYLIST}:{n}: bad pattern: {e}")
    return pats


def staged_paths() -> list[str]:
    return git("diff", "--cached", "--name-only", "--diff-filter=ACMR")


def content(path: str, staged: bool) -> str | None:
    if Path(path).suffix.lower() in BINARY:
        return None
    if staged:
        out = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True)
        data = out.stdout if out.returncode == 0 else b""
    else:
        try:
            data = (ROOT / path).read_bytes()
        except OSError:
            return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def main() -> int:
    if "--install-hook" in sys.argv:
        for name, text in (("pre-commit", HOOK), ("commit-msg", MSG_HOOK)):
            hook = ROOT / ".git" / "hooks" / name
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text(text, encoding="utf-8", newline="\n")
            try:
                os.chmod(hook, 0o755)
            except OSError:
                pass
            print("installed", hook)
        return 0
    if "--message" in sys.argv:
        msg_path = Path(sys.argv[sys.argv.index("--message") + 1])
        pats = machine_patterns() + denylist_patterns()
        problems = []
        for n, line in enumerate(msg_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.startswith("#"):                    # git's own commentary in the editor template
                continue
            for what, rx in pats:
                m = rx.search(line)
                if m:
                    a, b = max(0, m.start() - 30), min(len(line), m.end() + 30)
                    problems.append(f"message line {n}: {what}: ...{line[a:b].strip()}...")
                    break
        if problems:
            print(f"scrub check: the commit message names this machine ({len(problems)} line(s)); reword it", file=sys.stderr)
            for pr in problems:
                print("  " + pr, file=sys.stderr)
            return 1
        return 0
    staged = "--staged" in sys.argv
    tracked = set(git("ls-files"))
    stg = set(staged_paths())
    paths = sorted(stg if staged else tracked | stg)
    problems: list[str] = []

    for p in sorted(tracked | stg):
        if p in MUST_IGNORE or p.startswith(MUST_IGNORE_DIRS) or (p.startswith("ui/fonts/") and p not in FONT_OK and not p.endswith(".txt")):
            problems.append(f"{p}: must not be in the repository (see .gitignore)")

    pats = machine_patterns() + denylist_patterns()
    for p in paths:
        if p in EXEMPT:
            continue
        text = content(p, staged and p in stg)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for what, rx in pats:
                m = rx.search(line)
                if m:
                    a, b = max(0, m.start() - 30), min(len(line), m.end() + 30)
                    problems.append(f"{p}:{n}: {what}: ...{line[a:b].strip()}...")
                    break

    if problems:
        print(f"scrub check: {len(problems)} problem(s)", file=sys.stderr)
        for pr in problems:
            print("  " + pr, file=sys.stderr)
        return 1
    print(f"scrub check: clean ({len(paths)} files, {len(pats)} patterns{'' if DENYLIST.exists() else ', no local denylist'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
