"""The hand-over for updating Quartermaster itself (docs/self-update.md, stage 2).

The agent copies this file, from the tree it is still running, into the update's backup folder, and starts that copy
in a transient systemd user unit of its own. Two reasons. The agent's unit kills its whole control group when it
restarts, so a child of the agent would die with it. And a copy of the old tree's helper cannot be broken by the new
tree, which is the one under test.

  python selfupdate_helper.py <backup folder>

It reads <backup folder>/record.json: the checkout, the unit, the port and key, the head to move from and to. It
restarts the unit and watches the agent's state for up to `watch_s` seconds until it reports the head it was meant
to. If that head never comes, it puts the old head back (and the inventory and presets, when the update was one
that migrates them), restarts again, and watches for the old head. Every line it does goes to helper.log beside the
record, and the outcome into the record: state done, result updated | rolled back | rollback failed.

Stdlib only, and nothing imported from the agent. Written 2026-11-24."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}


class Run:
    def __init__(self, folder: Path):
        self.dir = folder
        self.file = folder / "record.json"
        self.rec = json.loads(self.file.read_text(encoding="utf-8"))
        self.logf = folder / "helper.log"

    def say(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.logf.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")
        self.rec.setdefault("log", []).append(f"{stamp} {line}")

    def save(self, **kw) -> None:
        self.rec.update(kw)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.rec, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)


def run(args, env=None, timeout=120) -> tuple[int, str]:
    try:
        p = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def head_reported(rec: dict) -> str | None:
    """The head the agent on the record's port runs (it started from), or None while it is not answering. The
    checkout's own head moves with the update; only a process that started after it reports the new one here."""
    try:
        key = Path(rec["key_file"]).read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"http://127.0.0.1:{rec['port']}/api/state", headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=5) as r:
            st = json.loads(r.read().decode("utf-8", "replace"))
        me = st.get("self") or {}
        return me.get("running") or me.get("head_full")       # the head the answering process started from, not the checkout's
    except Exception:  # noqa: BLE001
        return None


def watch(r: Run, want: str, seconds: float) -> bool:
    end = time.time() + seconds
    last = None
    while time.time() < end:
        got = head_reported(r.rec)
        if got == want:
            r.say(f"the agent reports {want[:7]}")
            return True
        if got != last:
            r.say(f"the agent reports {got[:7] if got else 'nothing yet'}")
            last = got
        time.sleep(1.5)
    return False


def restart(r: Run) -> bool:
    unit = r.rec["restart"]["unit"]
    rc, out = run(["systemctl", "--user", "restart", unit], timeout=90)
    r.say(f"systemctl --user restart {unit}: rc {rc}{(' · ' + out[-200:]) if out else ''}")
    return rc == 0


def journal_tail(r: Run) -> list[str]:
    unit = r.rec["restart"]["unit"]
    rc, out = run(["journalctl", "--user", "-u", unit, "-n", "40", "--no-pager", "-o", "cat"], timeout=30)
    return out.splitlines()[-40:] if rc == 0 else [f"journalctl: {out[-200:]}"]


def sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def restore_files(r: Run) -> None:
    """Put the inventory and presets back as they were before the update, when the update was one that migrates them
    (the old code may not read what the new one wrote). The file as it is now is kept beside the backup first, as
    <name>.after-update, so an edit made in the minute between the update and the rollback is not lost either."""
    if not r.rec.get("restore_files"):
        return
    for f in r.rec.get("files") or []:
        live, keep = Path(f["path"]), r.dir / f["copy"]
        if sha(live) == f["sha256"]:
            continue
        if not keep.exists():
            r.say(f"{live.name}: changed, and no copy to restore from")
            continue
        if live.exists():
            shutil.copy2(live, r.dir / f"{live.name}.after-update")
        shutil.copy2(keep, live)
        r.say(f"{live.name}: restored from the backup (the changed one kept as {live.name}.after-update)")


def main(folder: str) -> int:
    r = Run(Path(folder))
    rec = r.rec
    want, back = rec["to"], rec["from"]
    r.save(state="restarting", helper_pid=os.getpid())
    time.sleep(float(rec.get("settle_s", 1.5)))               # let the agent's plan finish and the page see it done
    r.say(f"moving the running agent from {back[:7]} to {want[:7]}")
    if restart(r) and watch(r, want, float(rec.get("watch_s", 60))):
        r.save(state="done", result="updated", ended=time.time())
        return 0
    r.say(f"the agent did not report {want[:7]} in time: putting {back[:7]} back")
    r.save(state="rolling back", failure_log=journal_tail(r))
    rc, out = run(["git", "-C", rec["root"], "reset", "--hard", back], env=GIT_ENV)
    r.say(f"git reset --hard {back[:7]}: rc {rc}{(' · ' + out[-200:]) if out else ''}")
    restore_files(r)
    if rc == 0 and restart(r) and watch(r, back, float(rec.get("watch_s", 60))):
        r.save(state="done", result="rolled back", ended=time.time())
        return 1
    r.save(state="done", result="rollback failed", ended=time.time(), failure_log_after=journal_tail(r))
    r.say("the old version did not come back either: the agent needs a hand (see failure_log)")
    return 2


if __name__ == "__main__":
    if len(sys.argv) != 2 or not (Path(sys.argv[1]) / "record.json").exists():
        sys.exit("usage: selfupdate_helper.py <backup folder holding record.json>")
    sys.exit(main(sys.argv[1]))
