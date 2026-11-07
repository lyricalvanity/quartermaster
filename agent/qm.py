"""qm: the Quartermaster agent from a terminal.

  qm setup | detect [--yes]      find what this machine runs and write the inventory (setup.py; no agent needed)
  qm status                      who holds the card, every server's state
  qm suggest | scan              what the inventory could use; what detection sees that the roster lacks
  qm resident <id> [--force]     make <id> the resident model (unloads the others first)
  qm unload <id> | all           give the card back
  qm start <id>                  start a server (its model loads at start)
  qm stop <id>                   end a server process (confirmed by the flag itself)
  qm park <id> | unpark <id>     keep 8099 from re-taking the card
  qm context <n>                 Maya's context (32768 ... 1048576)
  qm budget <n>                  Maya's thinking budget (0 or 256..262144)
  qm preset <id>                 run a preset from the inventory
  qm shim <id> [--stop]          start or stop an Ollama-API shim
  qm comfy start | stop | free   ComfyUI: start it, end it, or have it drop its models
  qm comfy update                ComfyUI to its newest release (torch pinned, boot-tested, rolls back by itself)
  qm comfy rollback              undo the newest update from its backup
  qm art submit FILE [--out DIR] [--name N] [--overwrite]
                                 queue an image batch (FILE: a batch object, or a bare job list
                                 with --out); nothing runs yet
  qm art list                    the queue and the last batches run
  qm art run [--then prior|none|<id>] [--force]
                                 park the resident model, run every queued batch in ComfyUI,
                                 put the model back (prior, the default). Blocks until done:
                                 an agent calls this as one tool call at a natural break
  qm art drop <batch id>         take a batch out of the queue
  qm log <id> [n]                engine log tail
  qm history                     the action log
  qm cancel                      cancel the running plan
Ids: see `qm status`. Key: agent.key beside this file, or in QM_HOME; QM_PORT picks the agent's port, as for the agent."""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ["QM_HOME"]).resolve() if os.environ.get("QM_HOME") else HERE
BASE = f"http://127.0.0.1:{int(os.environ.get('QM_PORT') or 8790)}"


def key():
    """Read on the first call, not at import: `qm setup` runs before there is a key."""
    try:
        return (HOME / "agent.key").read_text(encoding="utf-8").strip()
    except OSError:
        sys.exit(f"no agent.key in {HOME}: run `qm setup` first")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def action(op, target=None, args=None):
    code, r = call("POST", "/api/action", {"op": op, "target": target, "args": args or {}, "by": "cli"})
    if code != 202:
        print(f"refused: {r.get('error')}")
        return 1
    pid = r["plan"]["id"]
    shown = 0
    while True:
        time.sleep(1.0)
        _, pl = call("GET", "/api/plans")
        cur = pl.get("current") or {}
        p = cur if cur.get("id") == pid else next((x for x in pl.get("recent", []) if x["id"] == pid), None)
        if not p:
            continue
        for st in p["steps"][shown:]:
            pass
        for i, st in enumerate(p["steps"]):
            if i >= shown and st["status"] != "running":
                mark = {"done": "ok", "failed": "FAILED", "cancelled": "cancelled"}.get(st["status"], st["status"])
                print(f"  [{mark}] {st['name']}  {st.get('detail') or ''}".rstrip())
                shown = i + 1
        if p["status"] not in ("queued", "running"):
            print(p["status"].upper() + (": " + p["error"] if p.get("error") else ""))
            return 0 if p["status"] == "done" else 1


def status():
    _, s = call("GET", "/api/state")
    g, ram = s["gpu"], s["ram"]
    print(f"GPU {g.get('used_mib', 0)/1024:.1f}/{g.get('total_mib', 0)/1024:.1f} GB  util {g.get('util')}%  {g.get('temp_c')} C  {g.get('power_w')} W")
    print(f"RAM {ram['used_gib']}/{ram['total_gib']} GB  commit {ram['commit_used_gib']}/{ram['commit_total_gib']} GB")
    print(f"resident: {s.get('resident') or ('none' if not s.get('holders') else ', '.join(s['holders']))}")
    for sid, i in s["servers"].items():
        extra = ""
        if i.get("models"):
            extra = "  " + ", ".join(m["name"] for m in i["models"])
        if i.get("live") and i["live"].get("state") in ("reading", "generating"):
            extra += f"  {i['live'].get('state')} {i['live'].get('tok_s') or ''}"
        park = "  [parked]" if i.get("parked") else ""
        print(f"  {sid:<14} {i['state']:<10}{park}{extra}")
    c = s.get("comfyui")
    if c:
        held = f"{c['vram_held_mib']} MiB on the card" if c.get("vram_held_mib") is not None else "no VRAM figure"
        print(f"  {'comfyui':<14} " + (f"up  {held}  queue {c.get('queue', 0)}" if c.get("up") else "down"))
    if s.get("plan"):
        print("plan:", s["plan"]["op"], s["plan"].get("target") or "", s["plan"]["status"])
    for w in s.get("warnings", []):
        print("warning:", w)


def _opt(rest, flag):
    if flag in rest:
        i = rest.index(flag)
        if i + 1 < len(rest):
            return rest[i + 1]
    return None


def art(rest, force):
    sub = rest[0] if rest else "list"
    if sub == "submit" and len(rest) > 1:
        doc = json.loads(Path(rest[1]).read_text(encoding="utf-8"))
        out, name = _opt(rest, "--out"), _opt(rest, "--name")
        if isinstance(doc, list):
            if not out:
                print("a bare job list needs --out DIR")
                return 2
            doc = {"jobs": doc}
        if out:
            doc["out_dir"] = str(Path(out).resolve())
        doc.setdefault("name", name or Path(rest[1]).stem)
        if name:
            doc["name"] = name
        if "--overwrite" in rest:
            doc["skip_existing"] = False
        code, r = call("POST", "/api/art/submit", {"batch": doc, "by": "cli"})
        if code != 201:
            print(f"refused: {r.get('error')}")
            return 1
        b = r["batch"]
        print(f"queued {b['id']}  {b['name']}: {b['images']} image(s) to {b['out_dir']}")
        print("run it with: qm art run")
        return 0
    if sub == "list":
        code, r = call("GET", "/api/art")
        if code != 200:
            print(r.get("error"))
            return 1
        print(f"queued: {len(r['queued'])} batch(es), {sum(b['images'] or 0 for b in r['queued'])} image(s)")
        for b in r["queued"]:
            part = f"  ({b['written']} written so far)" if b.get("written") else ""
            print(f"  {b['id']}  {b['name']:<20} {b['images']:>4} image(s)  {b['out_dir']}{part}")
        if r["recent"]:
            print("recent:")
        for b in r["recent"]:
            t = time.strftime("%m-%d %H:%M", time.localtime(b["finished"] or 0))
            print(f"  {t}  {b['status']:<8} {b['name']:<20} {b['written'] or 0} written, {b['skipped'] or 0} skipped, "
                  f"{b['failed'] or 0} failed  {b.get('error') or ''}".rstrip())
        print("out_roots: " + ", ".join(r["out_roots"]))
        return 0
    if sub == "run":
        return action("art_batch", None, {"then": _opt(rest, "--then") or "prior", "force": force})
    if sub == "drop" and len(rest) > 1:
        code, r = call("POST", f"/api/art/{rest[1]}/remove", {"by": "cli"})
        print(r)
        return 0 if code == 200 else 1
    print(__doc__)
    return 2


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in ("setup", "detect"):                 # no agent needed: setup.py does its own work
        import setup as _setup
        return _setup.main([cmd, *rest])
    force = "--force" in rest
    rest = [r for r in rest if r != "--force"]
    if cmd == "status":
        return status()
    if cmd == "suggest":
        _, r = call("GET", "/api/suggestions")
        for s in r.get("suggestions", []):
            print(f"- {s['title']}\n    why: {s['why']}\n    how: {s['how']}")
        if not r.get("suggestions"):
            print("nothing to suggest")
        return 0
    if cmd == "scan":
        _, r = call("POST", "/api/scan", {"by": "cli"})
        if "error" in r:
            print(r["error"])
            return 1
        for n in r.get("notes", []):
            print(f"  - {n}")
        for u in r["units"]:
            print(f"  unit not in the roster: {u['id']} ({u['engine']} :{u['port']}){' running' if u['running'] else ''}")
        for c in r["clients"]:
            print(f"  client not in the roster: {c['id']} {c.get('version') or ''}")
        for e in r["engines"]:
            print(f"  engine not in the roster: {e}")
        for s in r["services"]:
            print(f"  service not in the roster: {s}")
        if not (r["units"] or r["clients"] or r["engines"] or r["services"]):
            print("the roster has everything detection can see")
        else:
            print("`qm detect` adds what you choose")
        return 0
    if cmd == "resident":
        return action("make_resident", rest[0], {"force": force})
    if cmd == "unload":
        return action("unload_all" if rest[0] == "all" else "unload", None if rest[0] == "all" else rest[0], {"force": force})
    if cmd == "start":
        return action("start", rest[0], {"force": force})
    if cmd == "stop":
        return action("stop", rest[0], {"confirm": True})
    if cmd in ("park", "unpark"):
        return action(cmd, rest[0], {"force": force})
    if cmd == "context":
        return action("set_context", "maya-glm", {"value": int(rest[0])})
    if cmd == "budget":
        return action("set_budget", "maya-glm", {"value": int(rest[0])})
    if cmd == "preset":
        return action("preset", rest[0], {"force": force})
    if cmd == "shim":
        return action("stop_shim" if "--stop" in rest else "start_shim", rest[0])
    if cmd == "comfy" and rest and rest[0] in ("start", "stop", "free", "update", "rollback"):
        return action("comfy_" + rest[0], None, {"confirm": True} if rest[0] in ("stop", "update", "rollback") else {})
    if cmd == "art":
        return art(rest, force)
    if cmd == "log":
        n = int(rest[1]) if len(rest) > 1 else 40
        _, r = call("GET", f"/api/log?server={rest[0]}&lines={n}")
        print("\n".join(r.get("lines", [])))
        return 0
    if cmd == "history":
        _, r = call("GET", "/api/history?lines=40")
        for a in r["actions"]:
            dur = f"{a['duration_s']}s" if a.get("duration_s") is not None else ""
            print(f"{a['ts']}  {a['op']:<14} {a.get('target') or '':<14} {a['result']:<8} {dur:<8} {a.get('error') or a.get('detail') or ''}")
        return 0
    if cmd == "cancel":
        print(call("POST", "/api/plans/current/cancel", {})[1])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
