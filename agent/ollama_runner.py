"""What Ollama's API does not say: how fast it is answering.

Ollama has no /metrics. /api/ps names the loaded models and nothing about the requests; the rates go only to the
client that asked (eval_count and eval_duration in its reply). Two things beside the API carry them:

  Ollama's log           Ollama's runner is a llama-server of its own (lib/ollama/llama-server), and its lines pass
                         through `ollama serve` into Ollama's log: one when a request begins, with the length of
                         its prompt, one for each batch of the prompt read, and two when it ends, with the prompt
                         and the generation in tokens and milliseconds. Read on every poll while a model is
                         loaded. Reading it asks nothing of Ollama. From it: that a request is in flight, how far
                         its prompt is read, and every finished request, exactly.
  the runner's /slots    That llama-server answers GET /slots on the loopback port Ollama gave it: whether a slot
                         is working and how many tokens are out. Asked only while the log says a request is in
                         flight, because every answer costs Ollama's log a line ("all slots are idle" from an idle
                         runner). From it: the tokens generated so far, and so the live rate. A runner in the
                         middle of a batch of prompt answers only when the batch is done, seconds later on a slow
                         card: an unanswered poll while the log says a request is in flight reads as reading.

  Telemetry(port, log)          one per Ollama unit; log is the unit's "log" (a file), else the platform's default
  .read(procs, loaded)          the keys the probe merges into the unit's info: busy, tok_s, live, last_request,
                                recent_requests, totals, engine. Whatever cannot be read is simply absent.

Where the log is: the journal on Linux (whatever `ollama serve` wrote there, found by its pid, so a user unit, a
system unit and a unit of any name all work; a system unit wants an account that may read the system journal),
%LOCALAPPDATA%\\Ollama\\server.log on Windows; a unit's "log" names a file anywhere. Where the log cannot be read
there are no finished requests, and /slots is asked on every poll instead, at the price of that line each time.
An Ollama whose runner is not a llama-server has neither source and reads as before: loaded models, no rates.

The generation rate is Ollama's: tokens over milliseconds, eval_count over eval_duration as it tells the client.
(The runner prints one of its own that leaves out the first token, about one per cent lower.) The prompt rate is
over the tokens actually read. Ollama's reply counts the whole prompt against that time, so a prompt mostly kept
from the last request looks many times faster there than the card is; here it does not, and `reused` says how
much was kept. The totals are those of this run of `ollama serve`, as far back as its log is read.

Only numbers are kept. A log line is matched against two patterns and dropped: none is stored, returned or
written to the agent's log (with OLLAMA_DEBUG set, Ollama's log carries prompts). /slots is read for its
counters; this llama-server does not put the prompt there.

Driven on Linux against Ollama 0.34.1 (2026-11-21). The file branch reads the same lines from a file and was
exercised with a file of them; no Ollama on Windows has been read this way. Stdlib only."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import platform_host as host

RUNNER = host.exe_names(("llama-server",))[0].lower()
SERVE = host.exe_names(("ollama",))[0].lower()
# a request in the runner's lines: where it begins, and the two it ends with
#   slot   operator(): id  0 | task 525 | new prompt, n_ctx_slot = 196608, n_keep = 4, task.n_tokens = 6933
#   slot print_timing: id  0 | task 525 | prompt eval time =    6558.02 ms /   837 tokens (...)
#   slot print_timing: id  0 | task 525 |        eval time =   19561.06 ms /   237 tokens (...)
#   slot print_timing: id  0 | task 525 | prompt processing, n_tokens =    833, progress = 1.00, t =   3.05 s / 272.78 tokens per second
RX_BEGAN = re.compile(r"task (\d+) \| new prompt,.*task\.n_tokens = (\d+)")
RX_READING = re.compile(r"task (\d+) \| prompt processing,.*progress = ([\d.]+)(?:.* / ([\d.]+) tokens per second)?")
RX_TIME = re.compile(r"(?:task (\d+) \| )?\s*(prompt )?eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
JOURNAL_GREP = r"eval time =|new prompt,"          # what a catching-up read lets out of the journal
FIRST_READ = 1024 * 1024                           # of a log file: its last MiB is history enough
PEAK_MIN_TOKENS = 16                               # a peak over fewer tokens is the timer's grain, not the engine
RECENT = 5
MISSES = 8                                         # with no log to say a request is still in flight: polls of patience


def default_log() -> str | None:
    """The file Ollama's own installer has it log to, where it logs to a file."""
    if host.WINDOWS:
        return str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ollama" / "server.log")
    return None


def _rate(n: int, ms: float) -> float | None:
    return round(n * 1000 / ms, 2) if ms > 0 else None


def parse(lines, pending: dict) -> tuple[list[dict], bool]:
    """The finished requests in some log lines, oldest first, and whether the last thing they tell of is a request
    beginning (one is in flight). `pending` carries a request across calls: its prompt's whole length by task, how
    far the one in flight has read ("reading"), and its prompt timing until the generation's follows."""
    out, in_flight = [], False
    for line in lines:
        if "eval time =" in line:
            m = RX_TIME.search(line)
            if not m:
                continue
            task, ms, n = m.group(1), float(m.group(3)), int(m.group(4))
            if m.group(2):
                pending["prefill"] = (task, ms, n)
                continue
            in_flight = False
            pending.pop("reading", None)
            pre = pending.pop("prefill", None)
            if pre and pre[0] != task:             # another task's prompt line: not this request's
                pre = None
            r = {"output_tokens": n, "decode_ms": round(ms, 1), "decode_tok_s": _rate(n, ms)}
            if pre:
                _, pms, pn = pre
                total = pending.get("total", {}).pop(task, None) if task else None
                # prompt_tokens is the whole prompt where the log said it; the rate is over the part actually read
                r.update(prompt_tokens=total if total and total >= pn else pn, prompt_ms=round(pms, 1), prefill_tok_s=_rate(pn, pms))
                if total and total >= pn:
                    r["reused"] = total - pn
            out.append(r)
        elif "new prompt," in line:
            m = RX_BEGAN.search(line)
            if m:
                in_flight = True
                totals = pending.setdefault("total", {})
                if len(totals) > 64:               # requests that never finished (cancelled, crashed) leave theirs
                    totals.clear()
                totals[m.group(1)] = int(m.group(2))
                pending["reading"] = {"prompt_total": int(m.group(2)), "prompt_read": 0}
        elif "prompt processing," in line:
            m = RX_READING.search(line)
            total = pending.get("total", {}).get(m.group(1)) if m else None
            if total:                              # progress is the engine's: what it kept and what it has read, of the whole
                pending["reading"] = {"prompt_total": total, "prompt_read": min(total, round(float(m.group(2)) * total))}
                if m.group(3):
                    pending["reading"]["prefill_tok_s_mean"] = float(m.group(3))
    return out, in_flight


class Telemetry:
    def __init__(self, port: int, log: str | None = None):
        self.port = port
        self.log = log or default_log()
        self.lock = threading.Lock()
        self._runners: dict[int, dict | None] = {}      # pid -> {"port", "kv"} for Ollama's own; None for any other llama-server
        self._resolving = False
        self._serve = None                              # pid of `ollama serve`: whose journal lines to read
        self._slots: dict[tuple, dict] = {}             # (runner port, slot id) -> the task it is on and its samples
        self._loaded = False                            # a model was loaded at the last read
        self._in_flight = False                         # the log has a request begun and not ended
        self._busy = False
        self._held = None                               # the last live block of a working runner
        self._missed = 0
        self._begin()

    def _begin(self):
        """A log read from its start: Ollama's first, or its first since it restarted. The totals are that run's."""
        self._mark = None                               # journal cursor or file offset
        self._log_ok = False
        self._pending: dict = {}
        self._recent: list[dict] = []
        self._totals = {"requests": 0, "prompt_tokens": 0, "output_tokens": 0, "peak_decode": 0.0}

    # -- the log --
    def _serve_pid(self, procs: dict):
        if self._serve and self._serve in ((procs or {}).get(SERVE) or []):
            return self._serve
        pids = host.pid_listening(self.port)
        pid = pids[0] if pids else None
        if pid != self._serve:                          # another `ollama serve`: what it has written is its own
            self._serve = pid
            self._begin()
        return pid

    def _file_lines(self) -> list[str]:
        p = Path(self.log)
        try:
            size = p.stat().st_size
            if isinstance(self._mark, int) and self._mark > size:      # a shorter file is a new one: Ollama restarted
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
            return self._file_lines()
        pid = self._serve_pid(procs)
        if not pid:
            self._log_ok = False
            return []
        if catch_up or self._mark is None:
            # behind by nobody knows how much (the first read, or the first since a model loaded, with every poll's
            # /api/ps line in between): the journal sifts it, and the place kept is the journal's end
            lines, _ = host.journal_read(pid, self._mark, grep=JOURNAL_GREP)
            _, end = host.journal_read(pid, last=1)
            self._log_ok = bool(end)
            self._mark = end or self._mark
            return lines
        lines, cursor = host.journal_read(pid, self._mark)
        self._mark = cursor or self._mark
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

    # -- the runner --
    def _resolve(self, pids):
        found = {}
        try:
            for pid in pids:
                cmd = host.cmdline(pid)
                # Ollama's when the executable sits in Ollama's tree: a llama-server of the owner's serving a blob
                # from Ollama's store names Ollama in its arguments only
                mine = "ollama" in cmd.split(" --", 1)[0].lower()
                port = re.search(r"--port[ =](\d+)", cmd)
                kv = re.search(r"--cache-type-k[ =](\S+)", cmd)
                found[pid] = {"port": int(port.group(1)), "kv": kv.group(1) if kv else None} if mine and port else None
        finally:
            with self.lock:
                self._runners.update(found)
                self._resolving = False

    def _runner_list(self, procs: dict) -> list[dict]:
        pids = set((procs or {}).get(RUNNER) or [])
        for gone in set(self._runners) - pids:
            del self._runners[gone]
        new = sorted(pids - set(self._runners))
        if new and not self._resolving:
            # a command line costs a PowerShell start on Windows: read off the poll, known by the next one
            self._resolving = True
            threading.Thread(target=self._resolve, args=(new,), daemon=True).start()
        return [r for r in self._runners.values() if r]

    @staticmethod
    def _get_slots(port: int):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/slots", timeout=1.0) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            return body if isinstance(body, list) else None
        except Exception:  # noqa: BLE001 - a runner deep in a batch, on its way out, or one that does not answer /slots
            return None

    def _live(self, runners, now: float) -> dict | None:
        """The live block from the runners' slots; None when none of them answered."""
        working, kept, answered = [], set(), False
        for rn in runners:
            slots = self._get_slots(rn["port"])
            if slots is None:
                kept.update(k for k in self._slots if k[0] == rn["port"])
                continue
            answered = True
            for sl in slots:
                if not isinstance(sl, dict) or not sl.get("is_processing"):
                    continue
                key, task = (rn["port"], sl.get("id")), sl.get("id_task")
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
                working.append((sl, tr, n))
        for key in set(self._slots) - kept:
            del self._slots[key]
        if not answered:
            return None
        if not working:
            return {"state": "idle"}
        sl, tr, n = working[0]
        live = {"state": "generating" if n > 0 else "reading", "generated": n}
        if len(working) > 1:
            live["phase"] = f"{len(working)} slots"
        if tr["t0"] is not None and now - tr["t0"] >= 1.0 and n > tr["n0"]:
            live["tok_s_mean"] = round((n - tr["n0"]) / (now - tr["t0"]), 2)
        rates = [w[1]["rate"] for w in working if w[1]["rate"]]
        if rates:
            live["tok_s"] = round(sum(rates), 2)                 # every slot's: what the card is putting out
        elif "tok_s_mean" in live:
            live["tok_s"] = live["tok_s_mean"]
        return live

    # -- what the probe merges --
    def read(self, procs: dict, loaded: bool) -> dict:
        with self.lock:
            if not loaded:                              # no runner, no request: nothing to read until a model loads
                self._loaded = self._busy = self._in_flight = False
                self._held = None
                self._slots.clear()
                self._pending.pop("reading", None)
                return {}
            runners = self._runner_list(procs)
            try:
                lines = self._log_lines(procs, catch_up=not self._loaded)
            except Exception:  # noqa: BLE001 - the rates are decoration; the unit's state stands without them
                lines = []
            self._loaded = True
            finished, began = parse(lines, self._pending)
            self._tally(finished)
            if began or finished:
                self._in_flight = began
            now = time.time()
            live = None
            if self._in_flight or self._busy or not self._log_ok:
                live = self._live(runners, now)
                if live is not None:
                    self._missed = 0
                    if live["state"] == "idle":         # it has answered that nothing is in flight, whatever the log last said
                        self._in_flight = False
                elif self._in_flight and (runners or self._resolving):
                    # the log has the request begun and the runner does not answer: it is deep in a batch of the prompt
                    live = self._held or {"state": "reading", "generated": 0}
                elif self._busy and self._missed < MISSES:
                    self._missed += 1                   # no log to go by: what it last said stands for a while
                    live = self._held
                else:
                    self._in_flight = False
            elif runners:
                live = {"state": "idle"}                # the log is being read and tells of no request
            self._busy = bool(live and live["state"] != "idle")
            self._held = live if self._busy else None
            out: dict = {}
            if live:
                live = dict(live)
                if live["state"] == "reading":
                    live.update(self._pending.get("reading") or {})
                out.update(busy=self._busy, live=live, tok_s=live.get("tok_s") if self._busy else None)
            if self._recent:
                out["last_request"] = dict(self._recent[-1])
                out["recent_requests"] = [dict(r) for r in self._recent]
            if self._totals["requests"]:
                out["totals"] = dict(self._totals)
            kv = sorted({r["kv"] for r in runners if r.get("kv")})
            if len(kv) == 1:
                out["engine"] = {"kv": kv[0]}
            return out
