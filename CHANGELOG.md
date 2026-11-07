# Changelog

## 0.1.0 - 2026-11-07

First public version. Built on one Windows workstation between 2026-11-03 and 2026-11-06 and generalised
on the last day; in daily use there.

- A host agent (Python, standard library only) that reads the card, the RAM and every unit, makes one
  model resident with one click, and executes only what the inventory says.
- A static page served by the agent itself or by an nginx container that holds the bearer key for it.
- Inventory schema 2: engines (with a version source each), units, shims, clients, services; `setup.py`
  finds Strata, Maya, Ollama, llama.cpp, ComfyUI, OpenWebUI, Pi, Codex CLI and Claude Code and writes it;
  `detect` proposes what is new.
- Presets made in the page, checked and written by the agent.
- A catalog of kinds with update recipes (`npm-global`, `pip`, `winget`, `git-pull-pip-e`, `git-check`,
  `github-release`, `self`, `script`), checks on a click only, apply behind a confirmation with backup
  and rollback where the recipe keeps one; a clients strip with bindings; "used by" on the unit cards.
- A catalog panel with suggestions, a scan and a guide per kind.
- ComfyUI tile with a guarded update; OpenWebUI tile through a scheduled-task driver; an art queue that
  runs image batches between resident models.
- Nineteen themes.
- A platform module with Linux branches (run under WSL; not yet on a Linux host with engines).
