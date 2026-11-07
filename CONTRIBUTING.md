# Contributing

Pull requests are welcome. A few things to know before you open one.

**The rules in README "What it will not do" are not up for negotiation.** A change that makes the agent run
something the inventory did not name, lets the page write the inventory, or reaches the network without a
click will not be merged, however useful.

**Nothing of your machine goes in.** `inventory.json`, `presets.json`, `agent.key`, `logs/`, `artq/`,
`backup/` and `local/` are ignored. `tools/scrub_check.py` runs as the pre-commit hook
(`python tools/scrub_check.py --install-hook`) and refuses a commit that names your user, host, home
directory or local addresses; put your own patterns in `local/scrub-denylist.txt`, one regular expression
per line, which is never committed.

**Adding a kind** is a catalog record (`catalog/<kind>.json`, see `catalog/README.md`), a detector in
`agent/setup.py`, and, for an engine the state machine should drive, the probe, load, unload and stop
branches in `agent/quartermaster_agent.py`. A record alone is tier D: listed with a guide.

**Adding a theme** is one stylesheet scoped to `:root[data-theme="<id>"]`, a line in `ui/theme.js`, and a
`<link>` in `ui/index.html`. Colour the engine bands by slot (`--e1`..`--e6`, `--e0` for legacy), never by
an engine's name; fill host, owner and the card's number from the `{HOST}`, `{host}`, `{owner}` and `{N}`
tokens in theme copy. Everything that moves stops under `prefers-reduced-motion`. Name no real person.

**Platform code** goes in `agent/platform_host.py`, with a Windows and a Linux branch.

Standard library only in the agent; no build step in the page.
