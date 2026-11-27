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
- The compose driver's copy of the data now lives in a **Docker volume beside the data** by default
  (`qm-backup-<project>-<service>-<stamp>-<version>`, labelled, with its `backup.json` inside), not in a folder
  under `logs/`. The copy exists for one reason, OpenWebUI's one-way database migration at start, and it is the
  database: on the host it was readable by the agent's account and everything running as it; in a volume it
  has exactly the perimeter the data already had. `compose.backup` is `volume` (default), `host` (a tarball
  under `backup_root`, as before, now opt-in) or `none` (no copy; a failed update rolls the image back and says
  the database stays as the new version left it). The tarball is verified where it lies by the image's own
  `tar`, pruning works on volumes by label, and the update step and the control log say which mode ran.
  Proven on a throwaway Compose project (a stand-in that migrates its volume at start; only the pull faked):
  a good move, a broken release rolled back from a volume with its migration gone and a uid-1000 mode-600
  file intact, `none` leaving the data as the new version left it, `host` restoring from a bind-mounted
  folder on Windows, and pruning to `keep_backups`. Setup writes `backup: volume` into new compose blocks.
- The compose driver's room check for a volume copy measures the drive Docker Desktop's data disk actually
  lives on: the folder its settings name (`CustomWslDistroDir` or `DataFolder`), else the default under
  `%LOCALAPPDATA%\Docker\wsl`, with junctions resolved. Before, it assumed the default drive, which on a
  machine whose disk was moved read the wrong free space (caught by a second-machine reader).
- The scrub tool scans commit messages too, as a `commit-msg` hook `--install-hook` now installs beside the
  pre-commit one: a message is published like a file, and one carrying a machine's name reached main before
  a reader elsewhere caught it. Re-run `--install-hook` to get the second hook.
- The systemd driver's `owui-ctl` keeps the data's file names out of its log. That log is readable by every
  account, which is how the dashboard follows it, and an update whose copy failed wrote cp's messages there:
  one per file, with the data directory's path and the file's name, which for an upload is the name it was
  uploaded under. They now go to `copy-errors.txt` in the backup folder. The script makes that folder
  root-only on every run, not only when it creates it. An update now measures the room for the copy before it
  stops the service, and refuses with rc 9, changing nothing, when there is not enough.
- A commit's **author line** is published like its files and its message, and the scrub tool now reads it.
  Both hooks refuse a commit whose author or committer address is not a no-reply one
  (`git config scrub.allowAuthorEmail true` for a contributor who means to publish theirs); the addresses git
  is configured with on the machine join the identifiers the tool derives; and the full scan reads the
  history every ref reaches, every author, committer and tagger address and every message line. The hooks
  call the script in the tree, so they need no reinstall.
- **History was rewritten on 2026-11-12** so that every commit carries a no-reply address. Trees, dates and
  messages are as they were; every hash is new, and both release tags moved with their commits. A clone from
  before then needs `git fetch origin --tags --force` and `git reset --hard origin/main`.
- Two themes, **GoldenEye** and **Hexen**, which makes twenty-one. GoldenEye is the Nintendo 64 game's two
  screens at once: the units are sheets in the MI6 dossier (typed, punched, the state a rubber stamp, the
  resident model stamped 00 AGENT, every guide a briefing marked for your eyes only) and the header, the side
  panels and the line at the foot are the wristwatch, with its segmented health and armour bars; the view
  down the barrel turns behind the page. It is the first theme with a light ground under part of the page:
  the paper surfaces redefine the palette for what sits on them. Hexen is the status bar in stone: blue and
  green mana vials for VRAM and RAM, the artifact box for the resident model, a mana crystal on every unit
  (dull, blue, green, and the red of combined mana while it works), and the life chain along the header's
  foot, lit as far as the card is full, the gem riding its end between two gargoyles.
- The page keeps how full the card is in `--gpu-full` (0 to 1) on the root element, for a theme that places
  something by it.
- **Cynosure**, which makes twenty-two: MU/TH/UR 6000 turned from green to the Blackwall's red (Cyberpunk 2077:
  Phantom Liberty). Its faces, raster, rolling bar, vitals trace and free-memory counter are MU/TH/UR's; the
  facility's mark is on the ground, in the header and on the start-up screen (`tools/make_cynosure.py` draws
  it). Red is the text, so failure is hot pink, caution stays yellow and ready stays green. One new thing
  moves: the Blackwall's residue (`#residue`, drawn by `app.js`), upright wireframe growths near the window's
  edges, laid over the page. A click passes through it, and each piece is drawn at full strength on open
  ground and as a faint trace over anything to be read or pressed. It stops, with the rest, under
  `prefers-reduced-motion`. `fonts.css` gains Perpetua Titling MT (Office) for the title. Looked at in
  Firefox and Chromium on Windows against the example inventory with every server down; the ready, busy
  and resident dress was checked by setting those classes by hand, not by driving an engine. The README's
  list of themes gains GoldenEye and Hexen with it.
- **Ollama now has telemetry.** Its unit showed loaded models and nothing else: Ollama has no `/metrics`, the
  probe read only `/api/ps`, and the Telemetry panel showed only a resident model, which Ollama never is. Its
  runner is a llama-server of its own, and two things beside the API carry what the API leaves out
  (`agent/llama_rates.py` reads them; what is Ollama's own is in `agent/ollama_runner.py`). **Ollama's log**
  has a line when a request begins, one for each batch of prompt
  read, and the runner's timing lines when it ends: read on every poll while a model is loaded (the journal on
  Linux, found by the pid of `ollama serve`; `%LOCALAPPDATA%\Ollama\server.log` on Windows; a unit's `log`
  names a file anywhere), it gives busy, how far the prompt is read, and every finished request exactly. **The
  runner's `/slots`** gives the tokens generated so far, and so the live rate; it is asked only while the log
  says a request is in flight, because each answer costs Ollama's log a line. The unit reads *busy* while it
  answers, its card and the sparkline carry the live rate, and with no resident model the Telemetry panel shows
  the Ollama that has a model loaded: decode, prefill, state, context, KV type, the last five requests, the
  count and the peak since `ollama serve` started. The decode rate is the one Ollama gives the client. The
  prefill rate is over the tokens actually read, where Ollama's reply counts the whole prompt against that
  time: a prompt mostly kept from the last request shows as `1905 · 13 new` with the true rate for those 13,
  not thousands of tokens a second. Only numbers leave the log; no line is kept. Where the log cannot be read
  (a system unit whose journal this account may not read) there are no finished requests and `/slots` is asked
  on every poll. An Ollama whose runner is not a llama-server reads as before. Driven on Linux against Ollama
  0.34.1, where the token counts and times of every request sent matched those in Ollama's own reply; the file
  branch was fed the same lines from a file, and no Ollama on Windows has been read this way.
- **A llama.cpp router's models have telemetry too.** A router unit showed busy and nothing more. The reader
  written for Ollama's runner is a llama-server's in general (`agent/llama_rates.py`), and a router's child is
  one: the router relays each child's lines into its own log under the child's port (`[58951] slot
  print_timing: ...`), and the child answers `/slots`. A loaded router unit now shows reading or generating,
  the live rate (summed over its slots when several answer at once), and each finished request's prefill and
  decode rates, the same figures the server gives its client as `prompt_per_second` and
  `predicted_per_second`. The whole prompt and the part of it kept from the request before are worked out from
  what the slot held when it was let go, so they are there at a server's usual verbosity, where its log does
  not state the prompt's length; that reckoning was checked against every request in a log that does state it.
  `/slots` is asked on every poll, as it was for busy: at a router's usual verbosity an answer costs its log
  nothing. A model loaded again is a new child, and its count starts over. Driven on Linux against llama.cpp
  b12096 with seven requests through a router: three compared in full with the figures in the server's reply
  (one of them a prompt mostly kept), and two pairs sent at once compared by their token counts and decode
  rates. A single llama-server (kind `llamacpp`) is not read this way yet.
- **A router model set to sleep when idle is let sleep, and has a state of its own.** llama-server's
  `--sleep-idle-seconds` releases a model's memory after that long without work and wakes it on the next
  request. To it a question to `/slots` is work: the agent, asking every poll, kept such a model awake for
  good, and one question woke a model that slept. Where a router's child carries the flag (the agent reads it
  from the argv in the router's `GET /models`), the log now leads, as it does for Ollama: `/slots` is asked
  only while the log has a request in flight, and nothing is asked of a model the router reports as
  `sleeping`. A child without the flag is asked on every poll, as before. A sleeping unit reads **asleep**:
  not loaded, not a holder of the card, its finished requests still shown from what was already read. Unload
  works on it (the router ends its child). Make resident wakes it by asking its `/slots`, because the router
  counts a sleeping model as running and answers a load with 400. A loaded unit that will sleep says after how
  long. Where the router's log cannot be read, a model that sleeps is not asked at all: whether it is
  answering is then not known, and the unit's info says so. Driven on Linux against llama.cpp b12096 with a
  model set to sleep after 20 seconds and the agent polling every 2: it slept on time and stayed asleep for
  the 40 seconds watched, a request woke it with its figures matching the server's and its count carried on,
  and Make resident and Unload were each run on it asleep. The page was looked at in one theme.
- **Cynosure's residue grows from the window's edge, or ragged at both ends.** A growth used to stand on a flat
  base a little in from the edge it belonged to, in mid-air. Now it is one of two kinds. Rooted: its foot is
  the very top or bottom edge of the window, and its columns start a little beyond it, so the edge cuts them
  and no base shows. Free: somewhere in a margin, with no base; each column is one unbroken stack running both
  ways from where the growth starts, to lengths of its own, with hairlines and wiring out of both halves, and
  an arc may crawl either way. Of four thousand seeded on a 1920 by 1080 window, about 45% were rooted at the
  bottom, 14% at the top and 41% free; every rooted column ran off the window at its edge, and a free growth's
  ends were as ragged below as above. What it lies over, how strongly it is drawn and how often it comes are
  unchanged. Looked at running in a bare page under the theme, not over the dashboard's cards.
- **MU/TH/UR 6000 and Cynosure have a lit ground.** The open ground behind the cards was kept near black in
  both. Seen by itself, as a desktop wallpaper, that was too dark to be the screen it is drawn from. Now
  MU/TH/UR's ground is 3.1 times as bright (`#061f10` where it took `--bg`) and Cynosure's field 2.2 times
  (its six colours each multiplied), and each emblem is as much stronger: the opacity in `weyland-dim.svg`
  is 0.62 where it was 0.2, in `cynosure-dim.svg` 0.748 where it was 0.34 (`tools/make_weyland.py` and
  `tools/make_cynosure.py` draw them so by default now). Cards, the bar and every control keep `--bg` and
  its neighbours, so nothing read on them changed. Measured on the dashboard with the old ground laid back
  over it for comparison, 126 pieces of lettering in each theme: no colour's lowest reading moved by more
  than 0.05:1, and one piece of MU/TH/UR's faintest green on the open ground went under 4.5:1 that was just
  over it (three of nine are under, where two were). Both themes had lettering under 4.5:1 before this,
  which is not touched here.
- **Quartermaster checks itself for updates** (docs/self-update.md, stage 1). The name at the top left opens
  a menu: its version, and Check for an update. A check fetches the checkout's upstream on that click, then reads
  how this installation runs and names one of nine upgrade paths (current, no restart, agent restart, rewritten
  upstream, local work, uncommitted changes, files in the way, no upstream, archive). A rewritten upstream is
  accepted only when every local commit's content is found upstream under some commit.
  Flags mark the steps that stay the owner's:
  - a changed root-side template in use here;
  - a changed launcher;
  - a font-map gap at a weight `fonts.css` now declares;
  - a newer inventory schema;
  - new catalog records;
  - changed scrub hooks;
  - code outside the page.

  A context card lists the incoming commits (subject, author address, date), the changelog lines and every step
  with who takes it and the command that does it by hand until stage 2 applies it. An ↑ beside the host line
  shows only after a check found something. `qm self [check]` is the same from a terminal. The agent reports
  its own version and head in the state as `self`.
- **Quartermaster updates itself** (stage 2), for an update that fast-forwards. Update to … in its menu (or
  `qm self update`) does, in order:
  1. backs up into `logs/self-update/`;
  2. moves the checkout to the commit the check found;
  3. when agent code changed, test-runs it with the new `quartermaster_agent.py --selftest` while the old agent still
     serves;
  4. restarts through the systemd user unit, from a helper in a transient unit of its own (the agent's unit kills its
     own control group);
  5. watches for the new version, and puts the old one back by itself if it does not come up within a minute.

  With nothing supervising the agent, it moves the code and says to restart by hand. Roll back to … (or `qm self
  rollback`) undoes the last update the same way. The state reports `running`, the head the agent started from, and
  `restart_due`. A bar asks for a reload when the page changed, or for a restart when the code changed and nothing
  restarted it. Windows restarts and a rewritten upstream are still by hand.
- The page's own files (HTML, scripts, stylesheets) are served `no-store`, by the agent and by the container's
  nginx. With `no-cache`, a Firefox-based browser restoring a session after a restart showed a tab's stored copy
  without asking the server, so the tab ran the page from before an update against the updated agent. Fonts and
  images stay `no-cache`.
- The inventory gains an optional `self` block: the checkout, its remote and branch, how the agent restarts
  (`systemd-user` with its unit, `respawn` on Windows, or `none`), and the container that serves the page.
  Setup and detect write it from the running agent, asking first, and detect keeps fields it cannot see.
  The check reports where the block no longer matches how the agent runs, and never replaces it.
- **Stop ComfyUI** says what it found when the process on ComfyUI's port will not show its command line. Windows
  hides an elevated process's command line from an unelevated one, so a ComfyUI started from an elevated prompt
  was refused as *is not ComfyUI (no command line readable)*. When the port answers `/system_stats` with the
  inventory's `match` among its arguments, the refusal now says it is ComfyUI running with more privilege than
  the agent (elevated on Windows, another user's on Linux) and where to end it. The rule is unchanged: the agent
  ends nothing it cannot identify by command line, and never elevates.

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
