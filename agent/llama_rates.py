"""What a llama-server says of its requests beside its API: its log, and its /slots.

A llama-server keeps no record of its requests that a third party may ask for: /metrics is off unless it was
started with --metrics, and the rates of a request go to the client that made it. Two things do carry them:

  its log        One line when a request begins, one for each batch of prompt read, the two timing lines when it
                 ends (the prompt and the generation, in tokens and milliseconds) and one when the slot is let go,
                 with how many tokens it then holds. Reading it asks nothing of the server. From it: that a
                 request is in flight, how far its prompt is read, and every finished request, exactly.
  GET /slots     Whether a slot is working and how many tokens are out. From it: the live rate. A server in the
                 middle of a batch of prompt answers only when the batch is done, seconds later on a slow card:
                 an unanswered poll while the log has a request in flight reads as reading.

  Rates(port, log, ...)             one per server whose requests are to be read
  .read(procs, loaded, ports)       the keys a probe merges into a unit's info: busy, tok_s, live, last_request,
                                    recent_requests, totals. Whatever cannot be read is simply absent.
  .recall()                         the finished requests it already has, reading and asking nothing: for a
                                    server that is asleep
  parse(lines, pending, printed)    the finished requests in some log lines

Whose log: the process listening on `port` (its journal on Linux, found by pid, so a user unit, a system unit
and a unit of any name all work; a system unit wants an account that may read the system journal), or the file
`log` names. `prefix` picks one writer's lines out of a log several share: a llama.cpp router relays each child's
lines as "[<child port>] ...". Where the log cannot be read there are no finished requests, and /slots alone
says whether the server is answering.

When /slots is asked: on every poll (always_ask), or only while the log has a request in flight. The second is
for a server an answer costs something. One run at debug verbosity writes its log a line for every answer ("all
slots are idle"); Ollama runs its runner so. One set to sleep when idle (--sleep-idle-seconds) counts the question
as work: it starts the idle clock again and wakes a server that sleeps, so one asked on every poll never sleeps and
never stays asleep. Where the log cannot be read, /slots is asked on every poll all the same (ask_blind), except
of a server that sleeps: of that one nothing is asked, and whether it is answering is not known.

The figures: a rate is the engine's printed one (`printed`, what a llama-server gives its client as
predicted_per_second: the generation's leaves out the first token) or tokens over milliseconds. The prompt rate
is over the tokens actually read; prompt_tokens is the whole prompt, and `reused` the part kept from the request
before, both from what the slot held when it was let go (the prompt and all but the last token generated). The
totals are those of the process, as far back as its log is read.

Only numbers are kept. A log line is matched against a handful of patterns and dropped: none is stored, returned
or written to the agent's log (a log at debug verbosity can carry prompts). /slots is read for its counters.

Driven on Linux (2026-11-21) against llama.cpp b12096 in router mode and against the llama-server Ollama 0.34.1
runs. The file branch reads the same lines from a file and was exercised with a file of them. Stdlib only."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

import platform_host as host

# a request in a llama-server's lines (the last three at any verbosity, "new prompt" only at debug):
#   slot launch_slot_: id  0 | task 525 | processing task, is_child = 0
#   slot   operator(): id  0 | task 525 | new prompt, n_ctx_slot = 196608, n_keep = 4, task.n_tokens = 6933
#   slot print_timing: id  0 | task 525 | prompt processing, n_tokens =    833, progress = 1.00, t =   3.05 s / 272.78 tokens per second
#   slot print_timing: id  0 | task 525 | prompt eval time =    6558.02 ms /   837 tokens (    7.84 ms per token,   127.63 tokens per second)
#   slot print_timing: id  0 | task 525 |        eval time =   19561.06 ms /   237 tokens (   82.89 ms per token,    12.06 tokens per second)
#   slot      release: id  0 | task 525 | stop processing: n_tokens = 7169, truncated = 0
RX_BEGAN = re.compile(r"task (\d+) \| processing task")
RX_TOTAL = re.compile(r"task (\d+) \| new prompt,.*task\.n_tokens = (\d+)")
RX_READING = re.compile(r"task (\d+) \| prompt processing,.*progress = ([\d.]+)(?:.* / ([\d.]+) tokens per second)?")
RX_TIME = re.compile(r"(?:task (\d+) \| )?\s*(prompt )?eval time =\s*([\d.]+) ms /\s*(\d+) tokens"
                     r"(?:\s*\([^)]*?([\d.]+) tokens per second)?")
RX_ENDED = re.compile(r"task (\d+) \| stop processing: n_tokens = (\d+)")
JOURNAL_GREP = r"eval time =|processing task|stop processing|new prompt,"     # what a catching-up read lets out of the journal
FIRST_READ = 1024 * 1024                           # of a log file: its last MiB is history enough
PEAK_MIN_TOKENS = 16                               # a peak over fewer tokens is the timer's grain, not the engine
RECENT = 5
MISSES = 8                                         # with no log to say a request is still in flight: polls of patience


def _rate(n: int, ms: float, printed: str | None = None) -> float | None:
    try:
        return round(float(printed), 2)
    except (TypeError, ValueError):
        return round(n * 1000 / ms, 2) if ms > 0 else None


def parse(lines, pending: dict, printed: bool = False) -> list[dict]:
    """The finished requests in some log lines, oldest first. `pending` carries what is unfinished across calls:
    the tasks in flight ("flying"), how far the latest has read ("reading"), a prompt's whole length where the
    log gave it, a prompt timing until the generation's follows, and a finished request until the slot's last
    line says how much of its prompt was kept."""
    out: list[dict] = []
    flying = pending.setdefault("flying", set())
    waiting = pending.get("done")                  # finished in the read before, its slot's last line not yet seen

    def emit():
        d = pending.pop("done", None)
        if d:
            out.append(d[1])

    for line in lines:
        if "eval time =" in line:
            m = RX_TIME.search(line)
            if not m:
                continue
            task, ms, n = m.group(1), float(m.group(3)), int(m.group(4))
            rate = _rate(n, ms, m.group(5) if printed else None)
            emit()                                 # a request before this one that never got its last line
            if m.group(2):
                pending["prefill"] = (task, ms, n, rate)
                continue
            flying.discard(task)
            pending.pop("reading", None)
            pre = pending.pop("prefill", None)
            if pre and pre[0] != task:             # another task's prompt line: not this request's
                pre = None
            r = {"output_tokens": n, "decode_ms": round(ms, 1), "decode_tok_s": rate}
            if pre:
                _, pms, pn, prate = pre
                r.update(prompt_tokens=pn, prompt_ms=round(pms, 1), prefill_tok_s=prate)
                total = pending.get("total", {}).pop(task, None) if task else None
                if total and total >= pn:          # the log said the prompt's whole length when it began
                    r.update(prompt_tokens=total, reused=total - pn)
            pending["done"] = (task, r)
        elif "stop processing" in line:
            m = RX_ENDED.search(line)
            if not m:
                continue
            flying.discard(m.group(1))
            d = pending.get("done")
            if d and d[0] == m.group(1):
                r = d[1]
                if "prompt_ms" in r and "reused" not in r:
                    # the slot ends holding the prompt and every token generated but the last
                    whole = int(m.group(2)) - r["output_tokens"] + 1
                    if whole >= r["prompt_tokens"]:
                        r.update(reused=whole - r["prompt_tokens"], prompt_tokens=whole)
                emit()
        elif "processing task" in line:
            m = RX_BEGAN.search(line)
            if m:
                emit()
                if len(flying) > 64:               # requests that never finished (cancelled, crashed) leave theirs
                    flying.clear()
                flying.add(m.group(1))
                pending["reading"] = {}
        elif "new prompt," in line:
            m = RX_TOTAL.search(line)
            if m:
                totals = pending.setdefault("total", {})
                if len(totals) > 64:
                    totals.clear()
                totals[m.group(1)] = int(m.group(2))
                pending["reading"] = {"prompt_total": int(m.group(2)), "prompt_read": 0}
        elif "prompt processing," in line:
            m = RX_READING.search(line)
            if not m:
                continue
            # progress is the engine's: what it kept and what it has read, of the whole
            done, total = float(m.group(2)), pending.get("total", {}).get(m.group(1))
            reading = {"prompt_total": total, "prompt_read": min(total, round(done * total))} if total else {"phase": f"{round(done * 100)}%"}
            if m.group(3):
                reading["prefill_tok_s_mean"] = float(m.group(3))
            pending["reading"] = reading
    if waiting is not None and pending.get("done") is waiting:
        emit()                                     # its last line did not come with the next read either: as it stands
    return out


class Rates:
    def __init__(self, port: int, log: str | None = None, proc: str = "llama-server", prefix: str = "",
                 printed: bool = True, always_ask: bool = False, ask_blind: bool = True):
        self.port = port                                # of the process whose log it is
        self.log = log
        self.proc = host.exe_names((proc,))[0].lower()
        self.prefix = prefix
        self.printed = printed
        self.always_ask = always_ask
        self.ask_blind = ask_blind                      # with no log to go by, ask /slots on every poll
        self.lock = threading.Lock()
        self._writer = None                             # pid of the process whose journal lines to read
        self._slots: dict[tuple, dict] = {}             # (port, slot id) -> the task it is on and its samples
        self._loaded = False                            # a model was loaded at the last read
        self._busy = False
        self._held = None                               # the last live block of a working server
        self._missed = 0
        self._begin()

    def _begin(self):
        """A log read from its start: the process's first, or its first since it restarted. The totals are that run's."""
        self._mark = None                               # journal cursor or file offset
        self._log_ok = False
        self._pending: dict = {}
        self._recent: list[dict] = []
        self._totals = {"requests": 0, "prompt_tokens": 0, "output_tokens": 0, "peak_decode": 0.0}

    # -- the log --
    def _writer_pid(self, procs: dict):
        if self._writer and self._writer in ((procs or {}).get(self.proc) or []):
            return self._writer
        pids = host.pid_listening(self.port)
        pid = pids[0] if pids else None
        if pid != self._writer:                         # another process on the port: what it has written is its own
            self._writer = pid
            self._begin()
        return pid

    def _file_lines(self) -> list[str]:
        p = Path(self.log)
        try:
            size = p.stat().st_size
            if isinstance(self._mark, int) and self._mark > size:      # a shorter file is a new one: the server restarted
                self._begin()
            self._log_ok = True
            start = self._mark
            if start == size:
                return []
            tail = start is None or size - start > FIRST_READ           # the first read, or far behind: the end will do
            if tail:
                start = max(0, size - FIRST_READ)
            with p.open("rb") as f:
                f.seek(start)
                chunk = f.read()
        except OSError:
            self._log_ok = False
            return []
        end = chunk.rfind(b"\n") + 1                    # a line still being written waits for the next read
        self._mark = start + end
        lines = chunk[:end].decode("utf-8", "replace").splitlines()
        return lines[1:] if tail and start > 0 else lines       # a tail opens on the end of some line

    def _log_lines(self, procs: dict, catch_up: bool) -> list[str]:
        if self.log:
            lines = self._file_lines()
        else:
            pid = self._writer_pid(procs)
            if not pid:
                self._log_ok = False
                return []
            if catch_up or self._mark is None:
                # behind by nobody knows how much (the first read, or the first since a model loaded, with whatever
                # the process wrote in between): the journal sifts it, and the place kept is the journal's end
                lines, _ = host.journal_read(pid, self._mark, grep=JOURNAL_GREP)
                _, end = host.journal_read(pid, last=1)
                self._log_ok = bool(end)
                self._mark = end or self._mark
            else:
                lines, cursor = host.journal_read(pid, self._mark)
                self._mark = cursor or self._mark
        if self.prefix:
            lines = [l for l in lines if l.startswith(self.prefix)]
        return lines

    def _tally(self, finished: list[dict]):
        for r in finished:
            self._recent.append(r)
            t = self._totals
            t["requests"] += 1
            t["prompt_tokens"] += r.get("prompt_tokens") or 0
            t["output_tokens"] += r["output_tokens"]
            if r["output_tokens"] >= PEAK_MIN_TOKENS and (r["decode_tok_s"] or 0) > t["peak_decode"]:
                t["peak_decode"] = r["decode_tok_s"]
        del self._recent[:-RECENT]

    # -- /slots --
    @staticmethod
    def _get_slots(port: int):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/slots", timeout=1.0) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            return body if isinstance(body, list) else None
        except Exception:  # noqa: BLE001 - a server deep in a batch, on its way out, or one that does not answer /slots
            return None

    def _live(self, ports, now: float) -> dict | None:
        """The live block from the servers' slots; None when none of them answered."""
        working, kept, answered = [], set(), False
        for port in ports:
            slots = self._get_slots(port)
            if slots is None:
                kept.update(k for k in self._slots if k[0] == port)
                continue
            answered = True
            for sl in slots:
                if not isinstance(sl, dict) or not sl.get("is_processing"):
                    continue
                key, task = (port, sl.get("id")), sl.get("id_task")
                kept.add(key)
                nt = sl.get("next_token")
                n = int((nt[0] if isinstance(nt, list) and nt and isinstance(nt[0], dict) else {}).get("n_decoded") or 0)
                tr = self._slots.get(key)
                if not tr or tr["task"] != task or n < tr["n"]:
                    tr = self._slots[key] = {"task": task, "t0": None, "n0": 0, "t": now, "n": n, "rate": None}
                if n > 0 and tr["t0"] is None:
                    tr.update(t0=now, n0=n, t=now, n=n)
                elif n > tr["n"] and now - tr["t"] >= 0.5:      # a plan's own probes come closer together than a poll
                    tr.update(rate=(n - tr["n"]) / (now - tr["t"]), t=now, n=n)
                working.append((tr, n))
        for key in set(self._slots) - kept:
            del self._slots[key]
        if not answered:
            return None
        if not working:
            return {"state": "idle"}
        tr, n = working[0]
        live = {"state": "generating" if n > 0 else "reading", "generated": n}
        if len(working) > 1:
            live["phase"] = f"{len(working)} slots"
        if tr["t0"] is not None and now - tr["t0"] >= 1.0 and n > tr["n0"]:
            live["tok_s_mean"] = round((n - tr["n0"]) / (now - tr["t0"]), 2)
        rates = [w[0]["rate"] for w in working if w[0]["rate"]]
        if rates:
            live["tok_s"] = round(sum(rates), 2)                 # every slot's: what the card is putting out
        elif "tok_s_mean" in live:
            live["tok_s"] = live["tok_s_mean"]
        return live

    # -- what the probe merges --
    def read(self, procs: dict, loaded: bool, ports, more: bool = False) -> dict:
        """ports: where a llama-server of this unit answers /slots; more: there may be one not yet found."""
        with self.lock:
            if not loaded:                              # no model, no request: nothing to read until one loads
                self._loaded = self._busy = False
                self._held = None
                self._slots.clear()
                self._pending.pop("reading", None)
                self._pending.pop("flying", None)
                return {}
            try:
                lines = self._log_lines(procs, catch_up=not self._loaded)
            except Exception:  # noqa: BLE001 - the rates are decoration; the unit's state stands without them
                lines = []
            self._loaded = True
            finished = parse(lines, self._pending, self.printed)
            self._tally(finished)
            flying = self._pending.setdefault("flying", set())
            now = time.time()
            live = None
            if self.always_ask or flying or self._busy or (self.ask_blind and not self._log_ok):
                live = self._live(ports, now)
                if live is not None:
                    self._missed = 0
                    if live["state"] == "idle":         # it has answered that nothing is in flight, whatever the log last said
                        flying.clear()
                elif flying and (ports or more):
                    # the log has a request begun and the server does not answer: it is deep in a batch of the prompt
                    live = self._held or {"state": "reading", "generated": 0}
                elif self._busy and not finished and self._missed < MISSES:
                    self._missed += 1                   # no log to go by: what it last said stands for a while
                    live = self._held
                else:
                    flying.clear()
            elif ports and self._log_ok:
                live = {"state": "idle"}                # the log is being read and tells of no request
            self._busy = bool(live and live["state"] != "idle")
            self._held = live if self._busy else None
            out: dict = {}
            if live:
                live = dict(live)
                if live["state"] == "reading":
                    live.update(self._pending.get("reading") or {})
                out.update(busy=self._busy, live=live, tok_s=live.get("tok_s") if self._busy else None)
            out.update(self._finished())
            return out

    def _finished(self) -> dict:
        out: dict = {}
        if self._recent:
            out["last_request"] = dict(self._recent[-1])
            out["recent_requests"] = [dict(r) for r in self._recent]
        if self._totals["requests"]:
            out["totals"] = dict(self._totals)
        return out

    def recall(self) -> dict:
        """The finished requests it already has. Nothing is read and nothing is asked: a server that is asleep
        writes no line, and a question would wake it."""
        with self.lock:
            return self._finished()

    @property
    def log_ok(self) -> bool:
        """Whether the last read of the log worked; False before the first."""
        return self._log_ok
