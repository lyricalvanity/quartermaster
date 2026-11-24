# Updating Quartermaster itself: specification (draft, 2026-11-24)

*Status: stages 1 and 2 are built (2026-11-24): the check, the context card and the `self` block; the update and
rollback for paths B and C, through a systemd user unit or by hand. Windows restarts (3), the rewritten-upstream reset
(4) and the agent's own container, hooks and flag steps (5) are design. The owner's decisions are in section 12.*

## 1. Purpose

The engine bands already check and apply updates for what the machine runs (SPEC.md section 7). Quartermaster
itself is still updated by hand, and doing that correctly depends on how it was installed:

- a pull, and nothing more, when only the page changed;
- a restart of the agent when its code changed: through systemd on Linux, by hand on Windows;
- a rebuild of the nginx container when the page is served from it, since the image copies `ui/` at build time;
- a reset and a forced tag refresh when upstream rewrote its history, after proving nothing local is lost;
- a root step for the owner when a template under `contrib/` that this machine has installed changed;
- an advisory when `fonts.css` now names a face or weight the local font map does not cover.

Today those steps live in the owner's head and in install notes. This specification turns them into one
button. It finds out how this installation runs, picks the upgrade path the incoming commits call for, and
shows the whole plan before anything moves. Every step in the plan is labelled with who does it: the agent,
or the owner.

## 2. Rules it keeps

These are SPEC.md section 2, applied to updating Quartermaster itself. Nothing below relaxes them.

1. **No egress without a click.** The check is a `git fetch` (or, for an archive install, one GitHub request)
   on the click. An apply that rebuilds the container may pull its base image; that traffic belongs to the
   apply's click, and the dialog says so beforehand. A scheduled check, if it ever comes, is off by default
   and never applies anything.
2. **The page sends intents.** The ops are `check_self`, `apply_self` and `rollback_self`, plus `confirm`. The
   page never names a remote, a branch, a command or a commit. What the agent may run for itself lives in
   the inventory's `self` block (section 4). The live context is used to verify that block, never to replace it.
3. **It never elevates.** No sudo, no pkexec, no consent prompt. A root step is shown as a command for the owner.
4. **It never rewrites the owner's history.** No commit, push, merge or rebase. The only moves are a
   fast-forward, and a reset to upstream when every local commit is proven present upstream (section 5).
5. **It touches only its own installation.** For an installed copy of a `contrib/` template (OpenWebUI's
   control script, units, polkit rule), it shows the template's change and the install command. It never
   reads, compares or writes the installed copy.
6. **A failed update leaves the old version running.** If the old version cannot be kept running, the agent
   rolls back to it by itself, and says so.

## 3. Context: what the check looks at

Every check probes the context afresh. Apart from the fetch the probe is read-only, and it costs nothing a
click would notice.

| question | Linux | Windows | what it changes |
|---|---|---|---|
| Platform | `platform_host` (Linux, WSL) | `platform_host` | which restart modes exist |
| Install form | `.git` at the root: a checkout; else an archive | same | archives are check-only (path I) |
| Branch and upstream | `git rev-parse --abbrev-ref @{u}` | same | detached HEAD or no upstream: check-only |
| Working tree | tracked changes; untracked files an incoming commit would add; local commits upstream lacks | same | paths F to H |
| Who keeps the agent running | `/proc/self/cgroup` names a user unit, and `INVOCATION_ID` is set; that unit's `MainPID` is this process; its `Restart=` | parent process: the Startup `.cmd` or `run-quartermaster.bat` (pythonw, no supervisor); a console means started by hand | restart mode (section 6) |
| Separate home | `QM_HOME`, `QM_PORT` in the environment | same | carried into the restart |
| Page delivery | always the agent on its port; the container named in `self.container`, through `docker inspect`: running, image created | same, Docker Desktop | rebuild step when the image predates the newest commit touching `ui/` or `docker/` |
| Root-side templates in use | inventory `openwebui.driver` is `systemd` with control units | scheduled tasks: none in the repo | root-step flag |
| Launchers setup wrote | the unit's `FragmentPath`, compared with what setup's template would write now | the `.cmd` and `.bat` | launcher flag |
| Contributor hooks | `.git/hooks/pre-commit` and `commit-msg` exist | same | reinstall when the tool's hook text changed |
| Font map | `ui/fonts/local.css` exists | same | font advisory |
| Other agents on this checkout | a process running this `quartermaster_agent.py` on another port | same | warned; apply refused until they stop |

The check reports the result as the **context card** (section 7). A check that cannot see something says so
("container: docker is not answering"). It never guesses.

## 4. The `self` block

Setup and detect write a `self` block into the inventory from what the probe found. Setup asks before
writing it, as it does for everything else.

```json
"self": {
  "repo": "<the checkout's root>",
  "remote": "origin",
  "branch": "main",
  "restart": { "via": "systemd-user", "unit": "quartermaster.service" },
  "container": { "compose_file": "<root>/docker-compose.yml", "service": "quartermaster" },
  "require_signed": false
}
```

`restart.via` is `systemd-user`, `respawn` (Windows, no supervisor) or `none` (started by hand: the update
stops after the code moves and asks for a restart). `container` is `null` when the page is not served by
the container.

**Without a block,** the check still runs, but apply is refused: "run `qm detect` to record how this
installation restarts".

**When the live context disagrees with the block,** the check names the difference and apply is refused.
Example: the block says `systemd-user`, but this agent was started in a terminal. The owner either fixes how
it runs or runs `qm detect`. The probe never quietly replaces what the block says.

## 5. Upgrade paths

The check compares `HEAD` with `<remote>/<branch>` after the fetch and assigns exactly one path. Flags may be
added on top.

| path | when | the agent does | the owner does |
|---|---|---|---|
| **A. Current** | nothing behind | nothing | nothing |
| **B. Page only** | fast-forward; only `ui/`, docs, `tools/` (except the scrub hook text), `README.md`, `CHANGELOG.md` | fast-forward; rebuild the container if it is named and stale | reload any open page (the page does it itself, section 6) |
| **C. Agent code** | fast-forward; anything under `agent/` or `catalog/` | fast-forward, preflight, restart, watch (section 6) | nothing, unless the restart mode is `none` |
| **D. Rewritten upstream** | `HEAD` is not an ancestor of upstream | only if the tree is clean and every local commit upstream lacks has its tree matched by some upstream commit: reset to upstream, refresh tags with `--force`, then as C | confirm a second, labelled dialog: "upstream rewrote its history" |
| **E. Local work** | local commits whose trees upstream lacks | refuses, and lists them | push them, or set them aside |
| **F. Dirty tree** | tracked files changed | refuses, and lists them | commit, stash or discard |
| **G. Collision** | an untracked file where an incoming commit adds one | refuses, and lists them | move them aside |
| **H. No upstream** | detached HEAD, or a branch without an upstream | check-only, compared against the remote's default branch | update by hand |
| **I. Archive** | no `.git` | check-only: `VERSION` against the newest release tag | download and unpack by hand |

Path D is the check the owner made by hand twice: every unpushed commit's tree was found again under its new
hash, so a reset loses nothing. When the proof fails, D becomes E, and the dialog lists each commit whose
content upstream lacks. A rewrite that also changed file contents (one that scrubbed a file, say) defeats
the proof even when nothing local is lost. That is the safe way to fail: the owner looks before anything
is reset.

### Flags

| flag | raised when | what is shown |
|---|---|---|
| **root step** | a `contrib/` template this machine uses changed | the template's diff and its install command, e.g. `sudo install -m 0755 -o root -g root contrib/openwebui-systemd/owui-ctl /usr/local/libexec/owui-ctl`. If the owner keeps a filled-in copy, they merge the change into it first |
| **launcher** | setup's unit or launcher template now writes something different from the installed file | the diff and `qm setup --launchers` (new, section 11) |
| **font map** | `fonts.css` at the target declares a family and weight that `local.css` maps but does not cover at that weight | "fonts.css now names Perpetua Titling MT at 700; your font map covers it only as a range, so the browser will ask for the Windows file" |
| **schema** | the target's inventory schema is newer | "the agent will migrate the inventory at start and keep the original" |
| **catalog** | records were added | "run `qm detect` afterwards to see whether any of these are installed" |
| **hooks** | the scrub tool's hook text changed, and the hooks are installed | reinstalled by the agent after the move |
| **sensitive paths** | anything under `agent/`, `contrib/`, `docker/`, or `tools/scrub_check.py` | listed in the dialog, so a code change is never mistaken for a page change |

The dialog also shows **what's new**: the `CHANGELOG.md` lines added between the two commits, under their
version headings.

## 6. The plan

`apply_self` targets the commit the check recorded, not whatever upstream holds by then. What the owner
saw is what moves. If upstream moved since, the dialog says the newer commits are not included.

1. **Quiet.** Refuse while another plan, an art batch or a model load is running. Updating Quartermaster
   never touches engines: the resident model stays resident.
2. **Back up** into `logs/self-update/<stamp>/`:
   - the head, branch and `VERSION`;
   - `inventory.json`, `presets.json` and their hashes;
   - a copy of the helper (step 5) taken from the current tree.
3. **Move:** `git merge --ff-only <hash>`. Path D instead runs `git reset --hard <hash>` and then
   `git fetch --tags --force`. Git runs with `GIT_TERMINAL_PROMPT=0`, so missing credentials fail plainly
   rather than wait at an invisible prompt.
4. **Preflight** (path C and D). Run the new code in a child with the agent's own interpreter:
   `quartermaster_agent.py --selftest` (new). It imports every module, loads the catalog, dry-runs the
   inventory load and migration, binds an ephemeral loopback port, serves `/api/selftest` once, and exits.
   On failure: reset to the backed-up head and stop. The running agent was never touched.
5. **Hand over to the helper.** `agent/selfupdate_helper.py` is short, standard library only and rarely
   changed. It runs from the copy in the backup folder, so a broken new tree cannot break recovery. How it
   starts depends on the restart mode:
   - **`systemd-user`:** `systemd-run --user --collect --unit qm-selfupdate-<stamp> <python> <helper> ...`. The
     unit's `KillMode` is `control-group`, so a child of the agent would die in its own restart; a transient
     unit sits outside that cgroup. The helper then runs `systemctl --user restart <unit>`.
   - **`respawn` (Windows):** started detached, in a new process group and breaking away from any job. The
     agent closes its listening socket and exits. The helper waits for the port to come free, then starts
     `pythonw` on the agent with the same environment (`QM_HOME`, `QM_PORT`).
   - **`none`:** there is no helper. The plan ends with "code updated; the running agent is the old version
     until you restart it", and the command.
6. **Watch.** The helper reads the agent key (it is the same user) and polls `/api/state` for up to 60
   seconds, until the agent reports the new head. If it does not:
   - reset to the old head;
   - restore the inventory and presets, unless their hashes show the owner edited them since; then it says
     so and leaves them;
   - restart again, watch again;
   - record "rolled back" with the last 40 lines of the new agent's log or journal.
7. **Finish,** done by the new agent. It reads the record the helper left and carries out what is left:
   - rebuild the container if it is named and stale (`docker compose -f <file> build <service>`, then
     `up -d <service>`);
   - reinstall the hooks if flagged;
   - append the owner's steps (root step, launcher, font map) to the plan, still marked as theirs.
8. **The page.** Its event stream drops during the restart and reconnects; it already retries every 2.5
   seconds. The state carries `self.head`. When that differs from the head the page loaded with, the page
   shows a bar, "Quartermaster was updated to 0.3.0; reload", and waits. It never reloads itself: an open
   dialog, a half-made preset or a log being read stays where it is until the owner chooses (decided,
   section 12). Until then the old page talks to the new agent. If it asks for an op the new agent no
   longer has, the agent answers with its existing 400 "unknown op" rather than guessing. If the state has
   changed shape, the old page may draw part of it wrongly. That is why the bar is drawn first, from
   `self.head` alone, before the page draws anything else from the new state.

## 7. What the page shows

- **The brand** (mark and title) becomes a button that opens the Quartermaster menu: Check for update,
  Update (once a check has found something), Roll back (while the last update's backup exists), and What's
  new.
- **After a check finds something,** the host line gets the same ↑ the engine bands use. There is no
  polling: the arrow appears only because someone clicked.
- **The confirm dialog shows the context card:**

```
Running on      Linux, kernel 7.x
Agent kept by   systemd user unit quartermaster.service (restarts on failure)
Page from       the agent on :8790; container quartermaster on :8800, image older than ui/ (rebuild)
Checkout        main → origin/main, clean, 3 behind, nothing local
Path            C, agent code: fast-forward, preflight, restart, watch
Flags           font map (Perpetua Titling MT 700), sensitive paths: agent/quartermaster_agent.py
Incoming        3 commits: subject, author address, signed or not
What's new      the changelog lines between the two versions
Steps           1 to 7, each marked "agent" or "you"
```

Themes may redress the dialog's title through `DIALOG_COPY`, as they do for Clear the card. The plain
title moves into the body, so what the button does is always stated.

## 8. Ops, state and records

- **Ops:** `check_self`, `apply_self` (`confirm`), and `rollback_self` (`confirm`). Path D carries a second
  flag, `confirm_rewrite`, that the dialog sends only from its labelled second step.
- **State:** `self` holds `{version, head, describe, branch, upstream, context, last_check, last_update}`.
  The version comes from `VERSION` and `git describe`; the agent does not report its own version today.
- **Records:** the last check and the last update sit in `logs/updates.json` under `quartermaster`, as the
  engines' records do. Each update keeps its log, its backup and the helper's record in
  `logs/self-update/<stamp>/`. The newest three are kept.

## 9. Rollback from the page

Available while the last update's backup exists. It resets to the recorded head and restores the inventory
and presets on the same hash rule as step 6. It restarts and watches the same way, and rebuilds the
container again if it was rebuilt. Root steps the owner took are not undone; the rollback lists them.

## 10. What can go wrong

- **The button shortens the path from a push to running code.** Anyone who can push to the remote, or
  take over the account that can, gets code running as the owner after one click. That is true of a manual
  pull too; the button removes the friction that made someone look. The countermeasures:
  - the dialog lists every incoming commit with its author address and flags sensitive paths;
  - `require_signed` makes the check refuse commits git cannot verify against an allowed-signers file;
  - nothing ever applies without a click, a scheduled check included.
- **The helper is the only way back.** That is why it runs from the old tree's copy, is kept small, and
  changes rarely. A change to the helper takes effect on the update after the one that brings it.
- **systemd kills the helper.** Avoided by the transient unit (section 6, step 5). This is the most likely
  first-build bug: a helper started as an ordinary child works in every test that never restarts the real
  unit.
- **The Windows port hand-off.** The new process may race the old one for the port, or antivirus may delay
  `pythonw`. The helper waits for the port to come free, and its 60-second watch covers a slow start.
- **Migrating, then rolling back.** Old code cannot read a schema it predates, so the backed-up inventory is
  restored. Edits the owner made after the update would be lost; the hash rule stops that and says so.
- **Credentials.** A remote that wants a password or a key's passphrase fails with "fetch needs credentials;
  run `git fetch` once yourself", never a hang.
- **A second agent on the same checkout,** such as a test instance with `QM_HOME`, would run code moved
  underneath it. The check finds it, and apply is refused until it stops.
- **The container proxies `/api` to the agent,** so it answers 502 during the restart. The page treats that
  as a dropped stream and reconnects.
- **What the restart loses:** plans in flight (refused anyway, step 1) and the event stream. What survives:
  residency (read back from the engines), presets, the art queue and the update records, all on disk.

## 11. Stages and tests

| stage | content |
|---|---|
| 1 | Context probe, `check_self`, `self` in the state, the brand menu and context card; `qm detect` writes the `self` block. No apply. **Built 2026-11-24**, notes below. |
| 2 | Apply for paths B and C: preflight with `--selftest`, the helper, `systemd-user` and `none`; rollback. **Built 2026-11-24**, notes below. |
| 3 | `respawn` on Windows. |
| 4 | Path D, with the tree-accounting proof. |
| 5 | Container staleness and rebuild; the flags (root step, launcher with `qm setup --launchers`, font map, schema, catalog, hooks, sensitive paths); `require_signed`. |

**Test bench.** A bare repository as the remote and a scratch clone as the install. A second agent runs on
`QM_PORT` and `QM_HOME`: under `systemd-run --user` on Linux, and by respawn on Windows. The bench pushes one
case at a time and drives the button through each:

- a page-only change, and an agent change;
- an agent change that fails to import (caught by preflight);
- one that imports but dies at start (caught by the watch, then rolled back);
- a `contrib/` change;
- a rewrite with every local commit accounted for, and one with a commit unaccounted;
- a dirty tree, an untracked collision, and local work;
- upstream moving between check and apply;
- an unreachable remote;
- a second agent on the same checkout.

The real installation is not used until all of them pass.

### Stage 1, as built (2026-11-24)

**Where the code is.** The probe and the classification are in `agent/selfupdate.py`, beside `updaters.py`, so
the agent and setup share them. The op is `check_self`, an ordinary plan. Its record is kept in
`logs/updates.json` under `quartermaster` and written straight into the published state, so the page and `qm`
read it at once rather than a poll later. `self` in the state comes from file reads every poll; `git
describe` runs only when the head moves.

**What the card shows until stage 2.** Every step carries the command that does it by hand, so stage 1 is
already the manual procedure for this machine.

**Setup and detect.** Setup proposes the block before it writes a new inventory. A fresh install on Linux
assumes the user unit it writes; if the agent turns out to run otherwise, the check says so. Detect compares
only `repo`, `remote`, `branch`, `restart` and `container`, so fields the owner set by hand survive a
re-detect.

**Tested on the bench** (a bare remote, an author's clone, an installation's clone): 28 cases.
- Every path: A (including local commits with nothing behind), B, C, D, E (including a rewrite with one
  commit upstream lacks), F, G (including on a rewrite), H and I.
- Every flag, each with a case where it must not fire.
- Block mismatches; `settle` after a move by hand; a fetch that fails.
- A container found through a stand-in `docker`.
- A second agent on the checkout.

**Tested live, on a Linux laptop.**
- The check, before and after detect wrote the block.
- The brand menu in all 23 themes, at 1920 and 375 wide, and opened by keyboard.
- The card with a hostile commit subject, shown as text.

**Not tested yet:**
- Windows: the supervisor reading, and the query for other agents.
- A fetch that wants credentials. It fails with the message meant for it only if git's wording matches.
- A real container.

## 12. Decisions (2026-11-24)

1. **Path D is applied by the button,** once the tree-accounting proof holds and the owner confirms the
   second, labelled dialog. An unproven rewrite is refused, with each commit upstream lacks listed (decided).
2. **The page never reloads itself.** After an update it shows the bar and waits for the owner (decided).
   This overrides the idle reload first proposed: an update must never interrupt what is open.
3. **`require_signed` comes in stage 5,** with the other flags. It is of no use until the commits pushed
   upstream are signed, and today they are not (decided).
4. **Archive installs are check-only:** `VERSION` against the newest release tag, and the download is the
   owner's. Taken as the default: no installation is an archive today, and an apply would need a signature
   scheme the repository lacks. To be revisited if one appears.
5. **The agent rebuilds the container** when the `self` block names it, as the update's last step. The
   possible base-image pull belongs to the apply's click and is stated in the dialog (decided).

### Stage 2, as built (2026-11-24)

**The ops.** `apply_self` and `rollback_self`, each confirmed. An update runs in this order:
1. re-checks that the check still describes the checkout (`ready`);
2. refuses while units are mid-change;
3. backs up into `logs/self-update/<stamp>-update/`: the record, the inventory and presets, and the helper copied
   from the running tree; the newest three folders are kept;
4. fast-forwards to the checked commit;
5. for path C, runs `--selftest` on the new code beside the serving agent; if that fails, it resets and stops;
6. hands the restart to `agent/selfupdate_helper.py` in a transient `qm-selfupdate-<stamp>` unit (`systemd-run
   --user --collect`), or with restart `none` ends with "restart it yourself".

The helper restarts the unit and watches for up to 60 seconds. If the new head never reports, it resets, restores the
inventory when the update was a migrating one (keeping the changed file as `.after-update`), restarts, and watches for
the old head. A rollback is the same route in reverse, from the last update's record. It restores a migrated inventory
before the old code starts.

**Two things the design did not foresee:**
- **The running head.** The state's `head` is the checkout's, and an update moves the checkout before the restart. A
  watch on it would have passed with the old process still answering. The agent therefore reports `running`, the head
  it started from, and the helper watches that.
- **`restart_due`.** After a page-only update, the checkout is ahead of the running code by design. `restart_due` is
  true only when what lies between them touches `agent/` or `catalog/`. The bar says "restart it" for that, and
  "reload" when only the page changed.

The check, the apply and the rollback each put their outcome into the published state at once. Without that, the page
and `qm` read the old record for up to one poll.

**The page.**
- The brand menu adds Update to … (behind a confirmation that says whether the agent restarts), the reason when
  Update is not offered, Roll back to … after an update, and the state of an update still running.
- The bar is drawn first in `render()`, from the heads alone.
- `qm self update | rollback` does the same from a terminal.

**Tested on a disposable install** with its own agent under a transient unit with `Restart=on-failure`, like the real
one:
- path B, and path C through the real restart (new PID, new head in two seconds, helper unit collected);
- rollback, and forward again;
- a syntax error caught by the preflight, with nothing restarted and the checkout back;
- a version that passes the preflight but refuses to start: the helper rolled back in 64 seconds and kept the
  journal;
- refusals: changes after the check, no `confirm`, another agent on the checkout;
- restart `none`, with an agent started by hand: the code moved, the old process kept serving, `restart_due` was
  set;
- the whole flow in the page: check, Update, confirm, restart, the bar, Reload, Roll back offered.

The stage-1 bench still passes. Not exercised: a real update of the real installation, which waits for a real
upstream commit.

