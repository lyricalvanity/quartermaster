"""What Ollama's API does not say: how fast it is answering.

Ollama has no /metrics. /api/ps names the loaded models and nothing about the requests; the rates go only to the
client that asked (eval_count and eval_duration in its reply). But Ollama's runner is a llama-server of its own
(lib/ollama/llama-server) on a loopback port Ollama picks, and a llama-server's log and its /slots carry what the
API leaves out (llama_rates.py, which does the reading). What is Ollama's own is here:

  finding the runner     It is the llama-server whose executable sits in Ollama's tree; its port is in its
                         command line. One per loaded model.
  whose log              The runner's lines pass through `ollama serve` into Ollama's log: the journal on Linux,
                         %LOCALAPPDATA%\\Ollama\\server.log on Windows; a unit's "log" names a file anywhere.
  when /slots is asked   Only while the log has a request in flight. Ollama runs its runner at debug verbosity,
                         where every answer to /slots costs Ollama's log a line ("all slots are idle"): asked on
                         every poll, that is a line every two seconds for as long as a model sits loaded. Where
                         the log cannot be read, /slots is asked on every poll all the same, at that price.
  the figures            The generation rate is Ollama's: tokens over milliseconds, eval_count over eval_duration
                         as it tells the client. (The runner prints one of its own that leaves out the first
                         token, about one per cent lower.) The prompt rate is over the tokens actually read.
                         Ollama's reply counts the whole prompt against that time, so a prompt mostly kept from
                         the last request looks many times faster there than the card is; here it does not, and
                         `reused` says how much was kept.

  Telemetry(port, log)          one per Ollama unit
  .read(procs, loaded)          the keys the probe merges into the unit's info: busy, tok_s, live, last_request,
                                recent_requests, totals, engine. Whatever cannot be read is simply absent.

An Ollama whose runner is not a llama-server has neither source and reads as before: loaded models, no rates.

Driven on Linux against Ollama 0.34.1 (2026-11-21). No Ollama on Windows has been read this way. Stdlib only."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import llama_rates
import platform_host as host

RUNNER = host.exe_names(("llama-server",))[0].lower()


def default_log() -> str | None:
    """The file Ollama's own installer has it log to, where it logs to a file."""
    if host.WINDOWS:
        return str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ollama" / "server.log")
    return None


class Telemetry:
    def __init__(self, port: int, log: str | None = None):
        self.rates = llama_rates.Rates(port, log or default_log(), proc="ollama", printed=False)
        self.lock = threading.Lock()
        self._runners: dict[int, dict | None] = {}      # pid -> {"port", "kv"} for Ollama's own; None for any other llama-server
        self._resolving = False

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

    def _runner_list(self, procs: dict) -> tuple[list[dict], bool]:
        """Ollama's runners, and whether there may be one more whose command line is still being read."""
        with self.lock:
            pids = set((procs or {}).get(RUNNER) or [])
            for gone in set(self._runners) - pids:
                del self._runners[gone]
            new = sorted(pids - set(self._runners))
            if new and not self._resolving:
                # a command line costs a PowerShell start on Windows: read off the poll, known by the next one
                self._resolving = True
                threading.Thread(target=self._resolve, args=(new,), daemon=True).start()
            return [r for r in self._runners.values() if r], self._resolving

    def read(self, procs: dict, loaded: bool) -> dict:
        runners, more = self._runner_list(procs)
        out = self.rates.read(procs, loaded, [r["port"] for r in runners], more)
        kv = sorted({r["kv"] for r in runners if r.get("kv")})
        if loaded and len(kv) == 1:
            out["engine"] = {"kv": kv[0]}
        return out
