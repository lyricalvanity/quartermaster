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
| `detect` | executables, install roots (`~` is the home directory), configuration globs, default ports, health path, version source |
| `drive` | the endpoints the agent uses and what it can and cannot do |
| `launch` | the conventional argv shape and any flag never to add |
| `update` | the recipe and its pinned identifiers; an inventory entry may override it (`"update": {"recipe": "git-check"}` for a custom build) |
| `install` | steps as text, and the commands, for the owner to run; never run by the agent |
| `fits` | what it is good for and what it is not |
| `platforms` | where the record applies |

Recipes (`agent/updaters.py`): `npm-global` and `pnpm-global` (either refuses a package its manager does not
list as global, which would be a second copy), `pip`, `winget`, `git-pull-pip-e`, `git-check` (check only),
`github-release` (check against the newest release; apply only when the record names a `self_cmd`),
`self` (the program updates itself; apply runs its own command), `script` (the owner's script, `--check`
then apply), `comfy` (the ComfyUI procedure in `comfy_update.py`), `driver` (the service's own driver),
`none`. Every check runs on a click; nothing is scheduled.
