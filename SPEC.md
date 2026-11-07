# Quartermaster — specification, version 2 (2026-11-06)

Version 1 (2026-11-03) specified a dashboard for one machine and was built that week. This version
specifies the generalisation: the same dashboard installed on any machine that runs local inference,
finding what is installed, keeping the host's own particulars in one untracked file, and growing from
"model servers" to the four kinds of thing such a machine actually runs. Decisions already taken are
marked **decided**; the rest is design.

## 1. Purpose

One page per machine that answers, at a glance and truthfully, *who holds the card*, and lets the owner
change that with one click. Everything else (telemetry, presets, service tiles, update buttons, the
themes) serves that question.

## 2. What it must not do

These rules shaped version 1 and are not relaxed by anything below.

1. **No egress without a click.** The agent and the page bind loopback. Nothing phones anywhere on a timer:
   no telemetry, no update polling, no CDN. An update *check* is a click. A scheduled check may exist as an
   option; it is **off by default** (decided).
2. **The page sends intents, never commands.** An intent is an op name plus an id. Executables, arguments,
   ports and endpoints live in the inventory on the host, and the page has no way to write that file.
   Presets are the one thing the page may create, and a preset is only a list of existing ops over
   existing ids.
3. **Unload through the engine's API; stop is a separate, labelled, confirmed action.**
4. **Read only what the job needs.** Health, version, load state, VRAM held (from the OS). Never a user's
   workflows, chats, prompts, outputs or credentials. Detectors read configuration files for URLs and
   paths and skip anything named like a secret.
5. **Every mutating call is logged** with who asked (page, CLI, preset) and what happened.
6. **One codebase, zero particulars.** The repository carries the program and an example inventory. A real
   inventory, the bearer key, logs, queues and local notes are untracked, and a scrub check enforces it
   before every commit.

## 3. The model

Version 1 knew "servers" (a process serving one model on one port), "shims", "presets" and two bespoke
blocks for OpenWebUI and ComfyUI. Version 2 names four layers.

### 3.1 Host

The machine: `id`, display name, OS, CPU, RAM, and a **list** of GPUs (name, total memory, index). All of
it detectable; the installer fills it in and the owner may edit the names. Telemetry shows every GPU.
The resident-model state machine stays single-card in this version: placing one model across two cards,
or two models on two cards, is a real design problem and is **deferred** rather than faked. A GPU without
`nvidia-smi` is shown by name with no memory accounting.

### 3.2 Engines and units

An **engine** is an inference runtime installed on the host: Strata, Maya, Ollama, llama.cpp
(`llama-server`), ComfyUI, later vLLM or others. It is a first-class object:

```json
{"id": "maya", "kind": "maya", "name": "Maya", "root": "C:\\Users\\you\\Maya",
 "python": "C:\\Users\\you\\Maya\\.venv\\Scripts\\python.exe",
 "version": {"cmd": ["maya", "--version"]},
 "update": {"recipe": "git-pull-pip-e", "backup": true}}
```

A **unit** is a configured server instance of an engine: a port, a model, a launch recipe, a footprint,
timeouts. Version 1's `servers[]` are units; each gains `engine: "<id>"` and `group` is derived from it.
An engine may have no units (ComfyUI), one, or many (Strata). Ollama's units are whatever it has loaded,
listed live. `legacy: true` on a unit folds it into the collapsed band as now.

Capabilities are declared per **kind**, not per install:

| kind | serves units | holds VRAM | start/stop | load/unload | update |
|---|---|---|---|---|---|
| strata, strata-launch | yes | yes | yes | yes | declared |
| maya | yes | yes | yes | yes | declared |
| ollama | live list | yes | service | per model | declared or self |
| llamacpp | yes | yes | yes | no (one model per process) | declared |
| comfyui | no | yes | yes | free | built in (version 1) |

### 3.3 Services

Things with a health URL and up/down/update but **no VRAM claim**: OpenWebUI, a search engine, a reverse
proxy. One generic tile, driven by a declared **driver**:

- `process`: start argv, stop by pid on the port with a command-line match (as ComfyUI does today);
- `docker-compose`: `up -d` / `down` / `pull` in a named project directory;
- `schtasks`: run named Windows scheduled tasks, pass nothing (version 1's OpenWebUI driver, generalised);
- `windows-service`: `sc start|stop`;
- `systemd` (Linux, later): `systemctl --user`.

Version 1's `openwebui` block becomes a service of driver `schtasks`; its extras (bridge ports, two logs)
stay as driver-specific fields.

### 3.4 Clients

Programs that **consume** the servers: coding agents and chat front ends (Pi, Codex CLI, Claude Code,
Aider, OpenWebUI-as-consumer). A client is not a tile. It contributes three things:

- a **version** (`pi --version`), shown in a compact strip;
- an **update recipe** (`npm i -g <package>`, `pip install -U`, `winget upgrade`), on a click;
- its **bindings**: which local units it points at, read from its configuration (`baseUrl`-shaped fields
  only; files named `auth*`, `*token*`, `*secret*` are never opened).

Bindings are drawn on the unit cards ("used by Pi") and named in the Stop dialog, so the owner sees whose
floor they are about to pull out **(built)**.

## 4. Inventory, schema 2

One file, `agent/inventory.json`, `"schema": 2`. Top level: `host`, `owner`, `gpus[]`, the RAM figures,
`policy`, `engines[]`, `units[]`, `shims[]`, and for now the `openwebui`, `comfyui` and `art` blocks;
`services[]` and `clients[]` arrive with stages 3 and 5. Presets live in **`presets.json`** beside it
(section 6). The agent migrates a schema-1 file on first load (servers become units, their groups become
engines whose version is read from the old label, Ollama's from its `/api/version`) and writes the result
back once, keeping the original as `inventory.schema1.json` **(built)**. An engine declares one version
source: `version`, `version_file`, `version_cmd` or `version_url` + `version_path`; all local, cached ten
minutes, refreshed on reload.

`owner` and `host` feed the header and the themes (`{HOST}`, `{host}`, `{owner}`, `{N}` tokens in theme
copy; themes that address someone fall back to their canon when `owner` is absent) **(decided, built)**.

## 5. Installer and detection

**Decided: a command-line installer, not a first-run page.** The browser never holds the bearer (nginx
injects it), so the page's authority equals reach to its port. A page that could write the inventory could
write argv, which is code execution as the owner. The inventory stays off the HTTP surface.

```
python agent/setup.py              # first run: checklist of kinds, detect, review, write inventory.json
python agent/setup.py detect       # later: re-scan, propose additions, accept at the prompt
```

**(built 2026-11-06.)** `qm setup` and `qm detect` are the same thing. `--yes`, `--kinds`, `--skip`,
`--home DIR` (a separate home the agent reads with `QM_HOME`; `QM_PORT` picks its port) and `--force`
cover scripted runs and tests. A fresh `setup` also writes `presets.json` (clear the card, and one
"resident" preset per unit), the bearer key, the launchers, and offers the Startup copy and the scrub hook.

Each kind ships a **detector** with two techniques:

1. **Known paths and configuration conventions.** `~/Strata` with `strata-*.json`; `~/Maya` with
   `maya-*.json` and `run-*.bat`; Ollama on PATH or in `%LOCALAPPDATA%\Programs\Ollama`; ComfyUI's
   `main.py` beside a `comfy/` directory with a venv alongside; `~/.pi/agent`; `~/.codex`.
2. **Running processes.** Listening ports → owning pids → command lines → recognise the executable →
   propose a unit with that argv as its start recipe. If it is running, it can be described. This is how
   the agent already identifies ComfyUI and is the universal fallback.

Host facts come from the OS: hostname, `nvidia-smi --query-gpu=name,memory.total`, total RAM, CPU name.

Detection proposes; the owner confirms. What detection **cannot** do is invent an update recipe for a
custom build: it proposes the conventional one for the kind and the owner keeps or replaces it. Nor can
it measure a unit's footprint, context or quant: those stay blank for the owner. An engine found with no
unit (llama.cpp installed, no server running) is reported, not proposed, so no band stands empty. A
stale configuration file is proposed like any other: that is what the prompt is for. The installer also
writes the bearer key, the launcher for the Startup folder (Windows) or a user unit (Linux), and offers to
install the scrub hook if the checkout is a git repository.

Acceptance test for the package: a clean Windows machine with only Ollama runs `setup` and gets a working
dashboard showing Ollama and its models. **Passed 2026-11-06** in the form available here: `setup --kinds
ollama --home <dir>` on the original workstation, a second agent on `QM_PORT=8791` against that home,
state and page served, the installed agent untouched.

## 6. Presets

A preset is `{id, name, desc, steps: [{op, target, value}]}` over the agent's whitelisted ops and the
inventory's ids, with values constrained (a context from the unit's `context_choices`, a budget in range).
The page gets an **editor** that only picks from those, and a **Save current** button that snapshots what
is resident, its context, and which shims and ComfyUI are up. The agent validates against the same
whitelist on `POST /api/presets` and writes `presets.json`, a separate file, so the one thing the page may
write is cleanly bounded and the inventory stays read-only to it. **(built 2026-11-06.)** The whitelist is
`PRESET_OPS` in the agent: seventeen ops; not in it are `preset` (no nesting), the updates and the art
queue. A step whose op needs a confirmation (`stop`, `stop_all`, `comfy_stop`) gets it from the preset
having been saved by a person. `GET /api/presets` serves the table with each op's valid targets and the
Maya context choices, so the editor never carries its own copy of the rules; `POST /api/presets/delete`
removes one. Both are logged like actions. The thinking budget is not in the snapshot: the engine does not
report it.

## 7. Updaters

Version 1's ComfyUI update (backup head and `pip freeze`, fast-forward to the newest tag, reinstall with
pins, boot test, roll back by itself) is the pattern. It generalises to `updaters/` with declared recipes:

| recipe | check (click) | apply | backup | boot test |
|---|---|---|---|---|
| `git-pull-pip-e` | `git fetch`, compare tags | `git pull`, `pip install -e .` | head + freeze | start, probe `/health` |
| `pip` | `pip index versions` | `pip install -U` | freeze | import or `--version` |
| `npm-global` | `npm view <pkg> version` | `npm i -g <pkg>` | current version | `--version` |
| `winget` | `winget upgrade --id` | same | installed version | `--version` |
| `script` | owner's script, `--check` | owner's script | owner's | owner's |

An engine band's menu shows **Check for update** and, once a check has found one, **Update**. Neither
runs without a click (section 2.1). Ollama on Windows updates itself through its tray application: its
button is a check and a nudge. A recipe that cannot roll back says so before it runs.

**(built 2026-11-06, as catalog recipes.)** `agent/updaters.py` holds the recipes above plus `git-check`
(check only), `github-release` (check against the newest release; apply only with a `self_cmd`), `self`,
`comfy` and `driver` (the tile's own button), `none`. Ops `check_update`, `apply_update` (confirmed; an
engine's units must be down; a failure part-way rolls back when a backup exists) and `rollback_update`.
The last result per id lives in `logs/updates.json` and rides in the state as `updates`. A rewritten
upstream (tags that would clobber) is tolerated: branches are compared, tags noted as not comparable.
Verified on the workstation: checks for Pi (npm), Claude Code (self), Ollama (GitHub), Strata and Maya
(git fetch); apply and rollback end to end on a `script` recipe against a scratch home. Not run: an
apply on a real engine or client.

## 7a. The catalog

One record per kind, in the repository (`catalog/<kind>.json`), versioned with the code, so its update
path is `git pull`, which is a click. The record is the source of truth for everything Quartermaster knows
about a kind that is not particular to one machine: what it is and its licence; its **tier**; how to
**detect** it (executables, install roots, configuration globs, default ports, health URL, version
source); how it is **driven** (engine, service or client; the endpoints used; what can and cannot be done
to it); the conventional **launch** shape and the flag never to add; its **update** recipe with pinned
identifiers (a winget id, an npm package, a git remote and tag pattern); the **install** steps as text and
as commands for the owner to run; and what it **fits**, honestly. `setup.py` reads the conventions from
here and keeps only the quirks in code; the engine bands and the clients strip take their update recipe
from here unless the inventory overrides it (a custom build says `{"recipe": "git-check"}` and the button
becomes a check).

Tiers say what "supported" means: **A** engines the state machine drives (Strata, Maya, Ollama,
llama.cpp); **B** services with up, down and update (OpenWebUI, and later SearXNG, kiwix, Caddy); **C**
clients with version, update and bindings (Pi, Codex CLI, Claude Code); **D** listed with a guide only.
An entry moves up a tier when someone writes its driver.

**Suggestions** are rules over the host facts and the detected inventory, nothing fetched: a card and no
engine (the trade-offs between Ollama, llama.cpp and the long-context engines, not a ranking); an engine
and no front end; ComfyUI and no art block; several units on one card and no presets; a client bound to a
port no unit serves (a diagnostic, and the most useful of them). "Popular" is editorial, curated here;
Quartermaster never reports what anyone runs. Install automation means generated, shown, and run on the
owner's yes, with pinned sources and never a piped shell script. The page's Catalog panel lists installed
kinds with their tier, suggestions with their one-line reason, and a Guide button per entry; no install
button in the first cut.

**(built 2026-11-06.)** `GET /api/catalog`, `GET /api/suggestions` (the rules in `suggestions()`: no
engine for a card; engines with no front end or client; ComfyUI without the art block; several models and
few presets; a client's port nothing serves; units without a footprint; engines without a version source;
an update a check found) and `POST /api/scan` (runs `setup.detect_all` once, on the click, and returns what
the roster lacks; logged; writes nothing). The panel shows installed kinds with tier and version, the
known-but-absent kinds, suggestions with why and how, the scan's findings, and a guide modal rendered from
the record. `qm suggest` and `qm scan` are the terminal forms. Detectors reading their conventions from the
catalog records, rather than from code, is still to do: the records describe the conventions, `setup.py`
still implements them.

## 8. The page

The card grid stays as it is. What changes is above and around it.

```
HEADER    host · GPUs · clock | VRAM, RAM, utilisation, power | resident line | Clear the card | theme
ENGINES   one band per engine: name, version badge, ▾ (Check for update · Update · Logs · Open folder · Stop all)
          then that engine's unit cards; ComfyUI is a band with a tile; legacy units fold as now
SERVICES  tiles: up/down, version, Start · Stop · Update · Open
CLIENTS   a strip: name, version, ↑ when an update is known; bindings drawn on the unit cards
SIDE      Presets (+ New · Save current) · KPIs · Warnings · Actions log
```

Theme accents are **slots** (`--e1` … `--e6`, `--e0` for legacy) assigned by engine order; a band sets
`--accent` from its slot and its cards inherit it. Themes that name the engines (MAGI's three computers,
HAL's module labels, the park's paddocks) name them by slot **(built)**. The band head carries the version,
unit counts and a menu; today the menu stops every unit of that engine and names the missing update recipe.
ComfyUI still sits in the services row; it becomes a band when the engine kinds table lands.

## 9. Platform

**Decided: Linux support is wanted** (the owner runs more than one kind of box). Windows is first because
that is where it was built; the agent's platform-specific calls are few and are gathered into one module
with a Linux implementation to follow:

| need | Windows | Linux |
|---|---|---|
| total and available RAM | `GlobalMemoryStatusEx` | `/proc/meminfo` |
| per-process GPU memory | `typeperf "\GPU Process Memory(pid_*)"` | `nvidia-smi --query-compute-apps` |
| listening ports → pids | `netstat -ano` | `ss -ltnp` |
| process command line | WMI `Win32_Process` | `/proc/<pid>/cmdline` |
| end a process tree | `taskkill /T /F` | `kill` the group |
| start detached, no console | `CREATE_NO_WINDOW`, `pythonw` | `setsid`, `nohup` |
| scheduled-task driver | `schtasks` | `systemd --user` units |
| elevation | `ShellExecute runas` | `pkexec` / sudo prompt, owner's hands |

Docker-based services get easier on Linux, not harder. The nginx container is unchanged.

**(built 2026-11-06.)** `agent/platform_host.py` holds the table above as functions (`ram_status`,
`nvidia_smi`, `gpu_dedicated_mib`, `process_list`, `pid_listening`, `binds`, `ports_bound_on`, `cmdline`,
`kill_tree`, `kill`, `popen_detached`, `exe_names`, `python_console`); the agent calls those and nothing
platform-specific of its own except the two Windows-only drivers (scheduled tasks, the consent prompt),
which refuse plainly elsewhere. `setup.py` writes a systemd user unit on Linux. The Linux branches were
run under WSL Ubuntu (memory, listeners, command lines, a detached session, a group kill); a Linux box with
engines on it has not run the agent yet. Per-process VRAM on Linux comes from `nvidia-smi`'s compute-apps
list, which, unlike the Windows counter, does not see graphics-only clients.

## 10. Repository hygiene

The claim "nothing of the owner's machine is in the repository" is **enforced, not promised**:

- `.gitignore`: `agent/inventory.json`, `agent/presets.json`, `agent/agent.key`, `logs/`, `artq/`,
  `backup/`, `local/`, the generated launchers, every font in `ui/fonts/` except Michroma (OFL).
- `tools/scrub_check.py` scans tracked and staged files for the current user name, home directory,
  hostname and local addresses (derived at run time), plus the patterns in `local/scrub-denylist.txt`
  (never committed: the list itself would be the leak). It also refuses if any must-ignore path is
  tracked. `--install-hook` makes it the pre-commit hook; it also runs before every push.
- The public history starts clean: the repository was initialised after the scrub, not before.
- A real inventory's narrative comments stay in the real inventory; the example's are neutral.

## 11. Stages

Each stage leaves the original workstation working.

| stage | content | status |
|---|---|---|
| 1 | Scrub, `owner`/`host` tokens, example inventory, MIT, scrub check, repository | **done 2026-11-06** |
| 2 | Schema 2 with migration; engine bands with version and menu; slot colours in the themes | **done 2026-11-06** |
| 3 | `setup` and `detect`: host facts, the detectors, launcher and key | **done 2026-11-06** |
| 4 | Preset editor and Save-current; `presets.json` | **done 2026-11-06** |
| 5 | Updaters as catalog recipes, and the clients strip with bindings | **done 2026-11-06** |
| 6 | Platform module and the Linux implementation | **done 2026-11-06** (WSL-tested; a real Linux host still owed) |
| 7 | Catalog panel, suggestions, guides; detectors read the catalog's conventions | **done 2026-11-06** (panel, suggestions, scan, guides); detectors-from-records still to do |
| later | multi-GPU placement; a fleet view that reads several agents; an optional scheduled update check (off) | |

## 12. Decisions taken (2026-11-06)

- Licence: **MIT** (the most permissive licence with an attribution clause that fits code).
- Installer: **CLI**, for the reason in section 5. Presets are made in the page.
- Update checks: **on click only**; a scheduled check is an option, off by default.
- Owner's name in themes: an inventory field, canon defaults (HAL says Dave, the Matrix says Neo).
- The art queue ships as an **optional** module.
- Linux: wanted; Windows first; the platform table in section 9 is the port.
- Repository name: `quartermaster`.

## 13. Out of scope

Authentication beyond the bearer (the page is for the owner on their own machine; reach beyond loopback is
a deliberate edit and a reverse proxy with its own login). Model downloads. Benchmarking. Anything that
makes the dashboard a client of the models it manages.
