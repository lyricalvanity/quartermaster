# Changelog

## Unreleased

From the first install on a second machine, a Linux laptop with Ollama and a llama.cpp router on Vulkan:

- A llama.cpp router (`llama-server --models-dir`) is driven as one unit per model it offers (kind
  `llamacpp-router`): make resident and unload through `POST /models/load` and `/models/unload`, busy from the
  child's `/slots`, and the router itself never stopped from the page. Before, its `/health` read as a
  loaded model forever and Clear the card ended the router.
- Setup no longer proposes Ollama's runner (its own `llama-server`) or a router's children as llama.cpp
  units; a server a systemd user unit keeps is started and stopped through `systemctl --user`.
- llama.cpp's version is read so that it compares with its release tags, which are now `v0.x.y`.
- `qm` reads its key on first use (so `qm setup` works on a fresh clone) and honours `QM_HOME` and `QM_PORT`.
- `quartermaster.service`, which setup writes with local paths, is ignored by git.
- A `pnpm-global` update recipe beside `npm-global`. Setup and detect choose it for a client whose executable
  is pnpm's global shim for its package; either recipe refuses to apply to a package its manager does not
  list as global, rather than install a second copy.
- The page loads an optional `ui/fonts/local.css` (ignored by git) after `fonts.css`, so a machine without the
  Windows and Office faces can map those names to faces it has; the agent and the container serve an empty
  one when it is absent. The agent sends `.ttf` and `.otf` as fonts.
- OpenWebUI has a systemd driver on Linux: each of start, stop and update names a unit the agent starts by
  name with `--no-ask-password`, either a sealed control unit or the app's own unit. Setup writes it for a
  system unit (three `owui-ctl-*` units, in `contrib/openwebui-systemd` with their script and a polkit rule)
  and for a user unit (start and stop directly). The tile's and the steps' words are driver-neutral, with
  `steps` and `update_note` in the inventory for an installation's own.
- OpenWebUI's start, stop and update plans named an undefined `OWUI_TASKS` while waiting on the control log,
  so they failed after the task had been asked to run.
- A `pnpm-global` update failed from the agent with `ERR_PNPM_GLOBAL_BIN_DIR_NOT_IN_PATH`: pnpm refuses a
  global add while its global bin directory is off PATH, and an agent a service manager started never read the
  shell profile that puts it there. The add and the rollback get that directory appended to PATH, read from
  pnpm's config; setup's shim test reads it the same way, since `pnpm bin -g` refuses too.
- An update record a version moved outside Quartermaster has caught up with is served as current: the band
  or chip no longer shows `↑` with the version it already runs, the menu no longer offers it, and apply
  refuses it.
- A band's or chip's menu stays inside its band and the window: one that would cross an edge slides back, one
  wider than the room wraps, and it fits again when a theme's face loads after it opened.
- The brand mark no longer shrinks when the title beside it is wide (it had been an oval in every theme, a
  sliver in OCP), and the radar marks of MU/TH/UR, Global Cortex and Nerves Concord turn only their sweep,
  with the glow trailing it, while the ring, crosshair and centre stay put. OCP's title is set a size smaller
  to fit beside a whole mark.
- Every theme's start-up screen addresses the installation's owner (inventory `owner`) in its own idiom: a
  crew manifest, a pilot line, a command authorization, "Wake up, …", "Greetings Professor …". Without an
  owner each falls back to whoever its canon addressed. HAL and the Matrix had done this; the rest did not.

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
