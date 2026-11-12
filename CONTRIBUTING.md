# Contributing

Pull requests are welcome. A few things to know before you open one.

**The rules in README "What it will not do" are not up for negotiation.** A change that makes the agent run
something the inventory did not name, lets the page write the inventory, or reaches the network without a
click will not be merged, however useful.

**Nothing of your machine goes in.** `inventory.json`, `presets.json`, `agent.key`, `logs/`, `artq/`,
`backup/` and `local/` are ignored. `tools/scrub_check.py` runs as the pre-commit and commit-msg hooks
(`python tools/scrub_check.py --install-hook`) and refuses a commit whose files or message name your user,
host, home directory or local addresses; put your own patterns in `local/scrub-denylist.txt`, one regular
expression per line, which is never committed. Put the names of your other machines there too: the hook
derives only this machine's. A commit's author line is published with it, so the hooks also refuse an
author or committer address that is not a no-reply one: set `git config user.email` to your forge's (on
GitHub, `ID+login@users.noreply.github.com`), or, if you mean to publish yours, say so once with
`git config scrub.allowAuthorEmail true`. Run with no arguments, the tool reads the whole history as well.

**Adding a kind** is a catalog record (`catalog/<kind>.json`, see `catalog/README.md`) and, so that `qm setup`
finds it, a detector in `agent/setup.py`; without one the owner writes the unit by hand. For an engine the
state machine should drive, the record names its **driver**: a fork of a known engine names its parent
(`"driver": "strata"`); an engine with an HTTP or command-line control surface takes `http` or `cli` and a
`table` of the calls; only an engine neither can express gets probe, load, unload and stop branches of its
own in `agent/quartermaster_agent.py`. A record alone, with no driver, is tier D: listed with a guide.
Fill `tested` only with a machine the kind has actually been driven from; a record from the documentation
ships with `"tested": {}` and says so in the panel until someone reports it working.

**Adding a theme** is one stylesheet scoped to `:root[data-theme="<id>"]`, a line in `ui/theme.js`, and a
`<link>` in `ui/index.html`. Colour the engine bands by slot (`--e1`..`--e6`, `--e0` for legacy), never by
an engine's name; fill host, owner and the card's number from the `{HOST}`, `{host}`, `{owner}`, `{OWNER}`
and `{N}` tokens in theme copy; the start-up screen addresses the owner, with the theme's canon as the
fallback in `OWNER_DEFAULT`. Everything that moves stops under `prefers-reduced-motion`. Name no real person.

**Platform code** goes in `agent/platform_host.py`, with a Windows and a Linux branch.

Standard library only in the agent; no build step in the page.

**Versions** are `MAJOR.MINOR.PATCH` in `VERSION`, with a `## x.y.z - date` section in `CHANGELOG.md` and an
annotated tag `vx.y.z` on the commit. Before 1.0: a patch for fixes, a minor for a new kind, driver, recipe
or inventory field. Changes land under `## Unreleased` until someone cuts a version.
