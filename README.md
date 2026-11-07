# Quartermaster

A dashboard for one machine that runs local inference: it shows what is on the GPU and in RAM, and makes a
model **resident** with one click, unloading whatever held the card first. It drives the engines through their
own APIs (Strata, Maya, Ollama), starts and stops the processes you have told it about, keeps ComfyUI and
OpenWebUI in view, runs **presets** (a whole working arrangement in one button), and dresses itself in
nineteen themes because a status page you look at every day should be a pleasure to look at.

It is being generalised from a single workstation into something you can install on yours. What ships today
works on **Windows with one NVIDIA GPU**; the plan for detection, an installer, engine update buttons and
Linux is in [SPEC.md](SPEC.md).

## How it is built

Two pieces, by design:

- **The host agent** (`agent/quartermaster_agent.py`): one Python process, standard library only, on
  `127.0.0.1:8790`. It polls the card (`nvidia-smi`), the RAM and every server in the inventory every two
  seconds, serves that state as JSON and as a Server-Sent-Events stream, and executes *plans*
  (`make_resident`, `unload`, `stop`, `start`, `park`, `set_context`, `set_budget`, `preset`, the ComfyUI and
  OpenWebUI ops, the art queue). Every mutating call needs a bearer key and is appended to
  `logs/actions.jsonl`.
- **The page** (`ui/`): static HTML, CSS and JavaScript, no build step. The agent serves it itself at 8790;
  `docker-compose.yml` also builds an nginx container on `127.0.0.1:8800` that serves the page and proxies
  `/api` to the agent, injecting the bearer key from a read-only mount so the browser never holds it.

The one rule that shapes everything: **the page sends intents, never commands.** An intent is an op name
and a server id. What that means on this machine (which executable, which arguments, which port, which
endpoint) is written in `agent/inventory.json` and nowhere else. The agent never starts anything that is not
in the inventory, and arguments come from the file, never from the request.

## Install (Windows, today)

Requirements: Windows 10 or 11, Python 3.11 or newer (nothing to `pip install`), an NVIDIA driver
(`nvidia-smi` is how the card is read), and optionally Docker Desktop for the nginx container.

```bash
git clone https://github.com/lyricalvanity/quartermaster.git
```

```bash
cd quartermaster
```

1. Copy `agent/inventory.example.json` to `agent/inventory.json` and describe your machine: host name,
   GPU, RAM, each engine, and each unit (its kind, port, how to start it, where its log is). The example
   has one of each kind with comments. Copy `agent/presets.example.json` to `agent/presets.json` too.
   Both are in `.gitignore`: they describe your machine and stay there.
2. Make the bearer key (one line, any shell):

```bash
python -c "import secrets, pathlib; pathlib.Path('agent/agent.key').write_text(secrets.token_hex(32))"
```

3. Start the agent, in a console the first time so you see what it finds:

```bash
python agent/quartermaster_agent.py --debug
```

   Open <http://127.0.0.1:8790/>. That is the whole dashboard. For the container form:

```bash
docker compose up -d --build
```

   and open <http://127.0.0.1:8800/>. Rebuild after any edit under `ui/` (the page is baked into the
   image); the agent's own page at 8790 serves the live files.

4. To start the agent at logon, put a `.cmd` in your Startup folder (`shell:startup`) with the absolute
   paths of your checkout:

```bat
start "" /D "C:\path\to\quartermaster\agent" pythonw.exe "C:\path\to\quartermaster\agent\quartermaster_agent.py"
```

After editing the inventory: `POST /api/reload` with the bearer, or restart the agent.

## The inventory

Schema 2. A schema-1 file (`servers[]` with a `group` each, one `gpu`, presets inside) is migrated the
first time the agent loads it: the original is kept as `inventory.schema1.json` and the presets move to
`presets.json` beside it.

| key | what it is |
|---|---|
| `host`, `owner` | names shown in the header and used by the themes (`owner` is optional) |
| `gpus[]` | index, name, total MiB, and `free_card_mib`: below this much used, the card counts as free. The first one is the card the resident-model state machine manages |
| `ram_total_gib`, `pin_budget_gib`, `commit_warn_gib` | the RAM gauge and its warning lines |
| `policy` | `unload_ollama_on_resident`, `free_comfyui_on_resident`, `park_8099_default`, `humans_only` |
| `engines[]` | one per runtime, in the order the bands appear: `id`, `kind` (`strata`, `maya`, `ollama`, …), `name`, optional `root`, and one version source: `version` (a string), `version_file`, `version_cmd` (argv; the first version-looking token of its output) or `version_url` + `version_path` (a dotted JSON path). All local; read on a click or a reload, cached ten minutes |
| `units[]` | one per model server: `engine` names its engine; `kind` is `strata`, `strata-launch`, `maya` or `ollama`; `start.argv` and `cwd`; `log`; `timeouts`; `footprint`; `legacy: true` folds it into the collapsed band |
| `shims[]` | small proxies that front a unit on another port (an Ollama-API shim for an OpenAI-style server, say) |
| `openwebui` | optional tile; this driver runs three named Windows scheduled tasks (`tasks`) and reads health, version and two logs |
| `comfyui` | optional tile: start argv, port, the command-line text that identifies the process, an optional `update` section |
| `art` | optional queue of image batches that wait for the card (needs `comfyui`) |

`presets.json`: `{"presets": [{id, name, desc, steps: [{op, target, value}]}]}`. Each engine's band shows its
version and unit counts and has a menu (stop every unit of that engine; the update entry appears once an
engine declares a recipe). Themes colour the bands by slot (`--e1` … `--e6` in engine order, `--e0` for the
legacy band), so a theme never needs to know an engine's name.

## From a terminal

`agent/qm.py` talks to the agent with the same key:

```bash
python agent/qm.py status
```

`qm resident <id>`, `qm unload <id>|all`, `qm start|stop|park|unpark <id>`, `qm context <n>`,
`qm budget <n>`, `qm preset <id>`, `qm shim <id> [--stop]`, `qm comfy start|stop|free|update|rollback`,
`qm art submit|list|run|drop`, `qm log <id>`, `qm history`, `qm cancel`. The docstring at the top of the
file is the manual.

## What it will not do

- **Leave the machine.** Both ports bind loopback. There is no telemetry, no update check and no fetch of
  any kind that you did not click for; the engine update buttons, when they land, run on a click and never
  on a schedule (a scheduled check will be an option, off by default).
- **Run anything it was not told about.** Executables and arguments come from the inventory file; the
  request supplies an op and an id. The page cannot edit the inventory.
- **Read what is not its business.** From ComfyUI it reads `/system_stats` and the queue count, never
  workflows, queue contents or outputs. From OpenWebUI it reads `/health` and `/api/version`. How much of
  the card a process holds comes from the operating system, not from the process.
- **Guess.** "Stop" ends a process and is a separate, labelled action the page sends with `confirm: true`.
  A server is unloaded through its own API, never by killing it.

The key is readable by any process running as you; it keeps the browser honest, not a hostile user. See
[SPEC.md](SPEC.md) for the threat model and what is deliberately out of scope.

## Themes

Pick one from the header; it is remembered per browser (`?theme=<id>` in the URL also works). Each theme is
one stylesheet scoped to `:root[data-theme="<id>"]`, so adding one touches nothing else. Several redress the
confirmation dialog, the start-up screen and the notices in their own voice; a few draw things (falling code,
rain, a honeycomb that pulses, a tracker sweep). Everything that moves stops under `prefers-reduced-motion`.

`aperture`, `bladerunner`, `castlevania`, `codec`, `concord`, `cortex`, `cyberpunk`, `gits`, `hal`,
`jurassic`, `lcars`, `magi`, `matrix`, `muthur`, `ocp`, `precrime`, `t800`, `tron`, `wopr`, and plain.

Fonts: the themes name display faces that ship with Windows and Office (Agency FB, OCR A Extended, Felix
Titling, Old English Text MT, Bahnschrift, Cascadia). They are used from the viewer's machine; if a browser
hides installed fonts from pages, drop your own copies into `ui/fonts/` (ignored by git) and `fonts.css`
will serve them. Michroma is included under the SIL Open Font License.

The themes are homages. The names and marks they allude to belong to their owners and are used here as
fan reference only.

## Status and roadmap

Built on one workstation in November 2026 and in daily use there. The generalisation (a four-layer model of
host, engines, services and clients; a CLI installer with detectors; presets made in the page; declared
update recipes with backup and rollback; a platform shim so Linux is a second implementation rather than a
port) is specified in [SPEC.md](SPEC.md) and lands in stages. Issues and pull requests welcome; the rules
under "What it will not do" are not up for negotiation.

## Licence

MIT. See [LICENSE](LICENSE).
