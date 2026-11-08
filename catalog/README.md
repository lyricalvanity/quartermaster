# The catalog

One record per kind of thing Quartermaster knows how to find, drive or update: what it is, its licence,
its tier, how to detect it, how it is driven, how it updates, how to install it, and what it fits. These
records are versioned with the code; `git pull` is how the catalog updates. Nothing here is fetched at
run time. See SPEC.md section 7a.

| field | what it holds |
|---|---|
| `id`, `name`, `what`, `licence`, `home` | the kind, one paragraph, its licence, its upstream |
| `tier` | A: an engine the state machine drives; B: a service with up/down/update; C: a client with version, update and bindings; D: listed with a guide only |
| `role` | `engine`, `service` or `client` |
| `driver` | how the agent moves a unit of this kind: `strata`, `maya`, `ollama`, `llamacpp`, `llamacpp-router` (code paths of their own), or `http` / `cli` (read the record's `table`). Absent: the kind drives as itself. A fork names its parent's driver and needs no code |
| `table` | for `http` and `cli`: the calls and the keys, below. A unit's own `table` overrides it key by key |
| `tested` | `{platform: date}` for each machine someone has driven the kind from. A tier A or B record without one was written from the documentation; the panel marks it *untested* and says so in the guide |
| `detect` | executables, install roots (`~` is the home directory), configuration globs, default ports, health path, version source |
| `drive` | the endpoints the agent uses and what it can and cannot do, in words, for the guide |
| `launch` | the conventional argv shape and any flag never to add |
| `update` | the recipe and its pinned identifiers; an inventory entry may override it (`"update": {"recipe": "git-check"}` for a custom build) |
| `install` | steps as text, and the commands, for the owner to run; never run by the agent |
| `fits` | what it is good for and what it is not |
| `platforms` | where the record applies |

## The table (`driver: http` and `driver: cli`)

An engine whose API is "a path to call and a key to read" is a record, not code. Every string takes `{model}`
(the unit's `model_id`, else its `model`) and `{port}`.

| key | http | cli |
|---|---|---|
| `probe` | `{path, status}`: answering is up (default `GET /health`, 200) | the same, or absent: anything listening on the port is up |
| `loaded` | `true` (up means loaded: one model for the life of the process, the default) or `{path?, status?, <pred>}` over a path's JSON | — |
| `status` | — | `{argv, json, <pred>}` or `{argv, loaded_re}`: the command whose answer says what is loaded; absent means up is loaded |
| `busy` | `{path?, <pred>}`: answering a request; guards an unload | — |
| `load`, `unload` | `{method, path, body, timeout_s, hold}`; `hold` when the server answers only once done (Strata's `/v1/load`); no `unload` means unloading is stopping the process | `{argv, timeout_s, cwd}` |
| `start`, `stop` | — (the unit's `start.argv`, and the process on the port) | `{argv}`: the program's own commands, used instead |
| `procs` | process names swept after a stop, when an engine child outlives its server | the same |
| `auth` | `{header, prefix, key_file, key_re}`: a header the server wants, read from a file; the key itself never goes in a record or the inventory | — |

A predicate (`<pred>`) is `key` (a dotted path; a list on the way means any element) with one of `equals`,
`not_equals`, `in`, `truthy`, `nonempty`; a key alone tests that it exists; no predicate at all means the call
answering (its `status`, default 2xx) is the test. Examples:

```
vLLM:      "loaded": {"path": "/is_sleeping", "key": "is_sleeping", "equals": false},
           "load": {"path": "/wake_up"}, "unload": {"path": "/sleep?level=2"}
TabbyAPI:  "loaded": {"path": "/v1/model", "status": 200},
           "load": {"path": "/v1/model/load", "body": {"model_name": "{model}"}}, "unload": {"path": "/v1/model/unload"},
           "auth": {"header": "x-admin-key", "key_file": "~/tabbyAPI/api_tokens.yml", "key_re": "^admin_key:\\s*(\\S+)"}
LM Studio: "status": {"argv": ["lms", "ps", "--json"], "json": true, "key": "identifier", "equals": "{model}"},
           "load": {"argv": ["lms", "load", "{model}", "-y"]}, "unload": {"argv": ["lms", "unload", "{model}"]},
           "start": {"argv": ["lms", "server", "start", "--port", "{port}"]}, "stop": {"argv": ["lms", "server", "stop"]}
```

Those three are shapes, not records: no one has driven them yet, and a record for any of them lands with
`"tested": {}` until someone does.

Recipes (`agent/updaters.py`): `npm-global` and `pnpm-global` (either refuses a package its manager does not
list as global, which would be a second copy), `pip`, `winget`, `git-pull-pip-e`, `git-check` (check only),
`github-release` (check against the newest release; apply only when the record names a `self_cmd`),
`self` (the program updates itself; apply runs its own command), `script` (the owner's script, `--check`
then apply), `comfy` (the ComfyUI procedure in `comfy_update.py`), `driver` (the service's own driver),
`none`. Every check runs on a click; nothing is scheduled.
