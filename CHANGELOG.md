# Changelog

## Unreleased

- A unit's **driver** is now separate from its kind. The kind is what a thing is (its record, its band, its
  update recipe); the driver is how the agent moves it, and comes from the unit, else its kind's catalog
  record, else the kind itself. Every existing inventory resolves exactly as before. What it buys: a fork is a
  record naming its parent's driver (`"driver": "strata"`) and needs no code.
- Two **table drivers**, `http` and `cli`: an engine whose control surface is a path to call and a key to read
  (vLLM's sleep mode, SGLang, TabbyAPI, KoboldCpp, oobabooga) or a command line (`lms`, `foundry`) is a
  record with a `table` of those calls, which a unit may override key by key (`catalog/README.md`). Probe,
  loaded, busy, load, unload, start, stop, a process sweep, and a header read from a file for servers that
  want an admin key. Proven against stand-ins for both shapes, including evicting one for the other; no real
  engine of either shape has been driven yet.
- Every catalog record says where it has been **tested** (`{platform: date}`); the panel marks a tier A or B
  record nobody has driven *untested* and the guide says the record is from the documentation.
- The card's process list now includes every catalog record's executables and any `procs` a table names, so an
  engine added by record alone is seen on the card.
- A hand-written unit may omit `timeouts`; the agent fills defaults (start 120 s, load 600 s, unload 90 s).
- Six kinds added by record, each from its project's source or current documentation and none yet driven
  (`tested: {}`, marked *untested* in the panel): **PolyStrata** (driven as Strata: its `/health`, `/metrics`
  and `/v1/load`, `/v1/unload` are Strata's, confirmed in `serve/server.py`), **ik_llama.cpp** (driven as
  llama.cpp; told apart from mainline by the folder its `llama-server` runs from), **KoboldCpp** (`http`:
  `/api/v1/model`, `/api/extra/perf`, the admin `reload_config` with `unload_model`), **TabbyAPI** (`http`:
  `/v1/model`, `/v1/model/load` held open for the load, the admin key read from `api_tokens.yml`),
  **textgen** (oobabooga's text-generation-webui under its 2026 name; `http`: `/v1/internal/model/*`) and
  **Foundry Local** (`cli`: `foundry server start --port`, `foundry model load|unload`, the daemon on a fixed
  port). Each has a detector in `setup.py`; on a machine without them detection is unchanged.
- Two of the six then driven on Windows the same night and marked so: **KoboldCpp** 1.124.2 with a 20 GB GGUF
  (probe, the admin unload and reload, busy, start, stop; the process on the port turned out to be a
  multiprocessing child carrying none of the launch flags, so its detector reads them from the parent, and Stop
  ends every KoboldCpp process because the parent would relaunch the child) and **PolyStrata** 0.1.45-poly.8's
  server fronting a Strata engine and pack (detection from its run config and launcher, start, the Strata
  probe's engine and live blocks, `/v1/unload`, `/v1/load`, busy, stop). Both installs were removed afterwards.
- An engine id is no longer derived from a kind by cutting at the first dash (`ik-llamacpp` and
  `foundry-local` are their own engines); the two sub-kinds, `strata-launch` and `llamacpp-router`, still
  fold into their parents.
- A model Ollama reports on the card no longer reads as the card **taken by something unnamed**. Ollama is never
  resident, so it was not among the holders, and on a machine where it is the only engine every model it loaded
  raised the warning and hid the page's own Ollama line. The card now counts as claimed while Ollama reports a
  model on it and its own processes are the only engines there; another engine beside it still warns. The
  resident line says how much of the card is Ollama's instead of calling all of it the desktop's.
- An OpenWebUI in **Docker Compose** gets its tile: the `docker-compose` driver that SPEC.md section 3.3 had
  planned. Setup reads the project, compose files, service and named volumes from the running container's
  labels (before, it noted "a later stage" and wrote nothing). Start is `up -d --no-recreate`, stop is `stop`,
  and update copies the data, pulls, recreates, waits for `/health` and rolls image and data back if the new
  version does not come up. Proven on a throwaway project served from a local registry: stop and start keep
  the same container, an update with nothing new says so, a real one copies and moves, a version that never
  answers is rolled back with its own writes gone and ownership and modes intact, a cancel before the stop
  changes nothing, and old copies are pruned to `keep_backups`. Read-only against a real OpenWebUI:
  detection, readiness, and the sizing helper on its image; it has not been stopped or updated by the driver.

## 0.2.0 - 2026-11-07

The first install on a second machine, a Linux laptop with Ollama and a llama.cpp router on Vulkan, and what
it taught. A minor version because it adds a unit kind, a service driver and an update recipe; the inventory
format is unchanged and 0.1.0 inventories load as they are.

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
