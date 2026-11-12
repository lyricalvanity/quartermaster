"""Refuse to publish this machine's particulars.

    python tools/scrub_check.py                 scan every tracked file (working tree), every staged file,
                                                and the history every ref reaches
    python tools/scrub_check.py --staged        scan what is about to be committed (the pre-commit hook)
    python tools/scrub_check.py --message FILE  scan a commit message (the commit-msg hook)
    python tools/scrub_check.py --install-hook  make this the repository's pre-commit and commit-msg hooks

What it looks for, in every tracked or staged text file, and in a commit message:
  - this machine's identifiers, derived when the script runs: the user name, the home directory (every
    slash spelling), the host name, the local IPv4 addresses, and the addresses git is set to sign
    commits with (global and this repository's) unless they are no-reply ones;
  - every pattern in local/scrub-denylist.txt, one regular expression per line, case-insensitive, '#'
    comments. That file is never committed (local/ is ignored): the list itself would be the leak.
It also refuses if any path that must stay untracked (the real inventory, the key, logs, queues, the
Microsoft fonts) is tracked or staged.

A commit's header is published with it, so both hooks also refuse a commit whose author or committer
address is not a no-reply one (GitHub: ID+login@users.noreply.github.com). A contributor who means to
publish their address says so once: git config scrub.allowAuthorEmail true. The full scan reads the
history too: every author, committer and tagger address and every message line that any ref reaches.

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
# a commit's author line is published as well, and until 2026-11-12 neither hook looked at it
NOREPLY = re.compile(r"noreply", re.I)
ADDRESS_HELP = ("use a no-reply address (git config user.email <it>; on GitHub it is "
                "ID+login@users.noreply.github.com), or say you mean to publish yours "
                "(git config scrub.allowAuthorEmail true)")


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)}: {out.stderr.strip()}")
    return [l for l in out.stdout.splitlines() if l.strip()]


def quiet(*args: str) -> str:
    """git's output, or '' where it fails (a key that is not set, a repository with no commits yet)."""
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout if out.returncode == 0 else ""


def address_allowed() -> bool:
    return quiet("config", "--bool", "scrub.allowAuthorEmail").strip() == "true"


def git_addresses() -> list[str]:
    """The addresses git signs commits with here, the global one and this repository's, less no-reply ones."""
    found: list[str] = []
    for scope in ("--global", "--local"):
        addr = quiet("config", scope, "user.email").strip()
        if addr and not NOREPLY.search(addr) and addr not in found:
            found.append(addr)
    return found


def first(text: str, pats: list[tuple[str, re.Pattern]]) -> tuple[str, str] | None:
    """(which pattern, the text around the match) for the first pattern the text matches."""
    for what, rx in pats:
        m = rx.search(text)
        if m:
            a, b = max(0, m.start() - 30), min(len(text), m.end() + 30)
            return what, text[a:b].strip()
    return None


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
    if not address_allowed():
        for addr in git_addresses():
            pats.append((f"git identity address {addr!r}", re.compile(re.escape(addr), re.I)))
    return pats


def ident_problems(pats: list[tuple[str, re.Pattern]]) -> list[str]:
    """The author and committer the commit being made will carry (git exports them to commit-msg, so an
    --author or an amended commit's original author is seen there)."""
    who: dict[str, list[str]] = {}
    for role in ("author", "committer"):
        m = re.search(r"<([^>]*)>", quiet("var", f"GIT_{role.upper()}_IDENT"))
        if m:
            who.setdefault(m.group(1), []).append(role)
    allowed = address_allowed()
    problems = []
    for addr, roles in who.items():
        label = f"{' and '.join(roles)} address {addr!r}"
        if not allowed and not NOREPLY.search(addr):
            problems.append(f"{label} would be published with the commit: {ADDRESS_HELP}")
        else:
            found = first(addr, pats)
            if found:
                problems.append(f"{label}: {found[0]}")
    return problems


def history_problems(pats: list[tuple[str, re.Pattern]]) -> tuple[list[str], int]:
    """What every ref reaches: author, committer and tagger addresses, and every message line. Names are
    left alone, as LICENSE is. Returns the problems and how many commits were read."""
    problems: list[str] = []
    carried: dict[str, list[str]] = {}
    commits = 0
    for rec in quiet("log", "--all", "--format=%h%x00%ae%x00%ce%x00%B%x01").split("\x01"):
        rec = rec.lstrip("\n")
        if rec.count("\x00") < 3:
            continue
        commits += 1
        h, author, committer, body = rec.split("\x00", 3)
        for addr in {author, committer}:
            found = first(addr, pats)
            if found:
                carried.setdefault(found[0], []).append(h)
        for n, line in enumerate(body.splitlines(), 1):
            found = first(line, pats)
            if found:
                problems.append(f"commit {h} message line {n}: {found[0]}: ...{found[1]}...")
    for line in quiet("for-each-ref", "refs/tags", "--format=%(refname:short)%00%(taggeremail)").splitlines():
        tag, _, addr = line.partition("\x00")
        found = first(addr, pats) if addr else None
        if found:
            carried.setdefault(found[0], []).append(f"tag {tag}")
    for what, where in carried.items():
        shown = ", ".join(where[:6]) + (f" and {len(where) - 6} more" if len(where) > 6 else "")
        problems.append(f"history: {what} is an author, committer or tagger address in {len(where)} place(s): {shown}")
    return problems, commits


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
            found = first(line, pats)
            if found:
                problems.append(f"message line {n}: {found[0]}: ...{found[1]}...")
        if problems:
            print(f"scrub check: the commit message names this machine ({len(problems)} line(s)); reword it", file=sys.stderr)
            for pr in problems:
                print("  " + pr, file=sys.stderr)
        who = ident_problems(pats)
        if who:
            print("scrub check: the commit's author line", file=sys.stderr)
            for pr in who:
                print("  " + pr, file=sys.stderr)
        return 1 if problems or who else 0
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
            found = first(line, pats)
            if found:
                problems.append(f"{p}:{n}: {found[0]}: ...{found[1]}...")

    problems += ident_problems(pats)
    commits = 0
    if not staged:
        past, commits = history_problems(pats)
        problems += past

    if problems:
        print(f"scrub check: {len(problems)} problem(s)", file=sys.stderr)
        for pr in problems:
            print("  " + pr, file=sys.stderr)
        return 1
    read = f"{len(paths)} files, " + ("" if staged else f"{commits} commits, ")
    print(f"scrub check: clean ({read}{len(pats)} patterns{'' if DENYLIST.exists() else ', no local denylist'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
