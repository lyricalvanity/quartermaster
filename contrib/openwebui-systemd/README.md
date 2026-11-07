# OpenWebUI controls for systemd

The dashboard's OpenWebUI tile drives three controls by name and passes them nothing: start, stop and
update. On Windows they are scheduled tasks; on Linux they are units, and the agent asks systemd to start
them with `--no-ask-password`. What a control does, and how OpenWebUI is walled off, lives beneath it and
is the owner's. The dashboard sees the units' names, their state, and the lines they write to one log.

This folder is one such arrangement: OpenWebUI as a **system** unit under a user of its own, from a
virtualenv (pip or uv), driven by three root-owned oneshot units. Another arrangement (podman, docker, a
different backup) keeps the three unit names and the log lines and rewrites `owui-ctl`.

| file | installed as | what it does |
|---|---|---|
| `owui-ctl` | `/usr/local/libexec/owui-ctl`, root, 0755 | start or stop the app unit; update: stop, copy the data directory, upgrade, start, put the old version back if the upgrade fails, keep the newest copies |
| `owui-ctl-{start,stop,update}.service` | `/etc/systemd/system/`, root, 0644 | oneshots that run `owui-ctl` with one fixed word; never enabled |
| `50-quartermaster-owui.rules` | `/etc/polkit-1/rules.d/`, root, 0644 | lets one user start those three units, without a password, and nothing else |

The log contract, which the agent follows to know a control has finished and how:

    === owui-ctl <verb> start <time>
    === owui-ctl <verb> done rc=<n>

## What it costs

The polkit rule gives every process running as that user, not only the agent, the power to start those
three units. The worst any of them can do is take OpenWebUI down, start it, or run an update: a stop, a
copy of the data, an upgrade from PyPI. None of them can pass an argument, read the data, enter the
service's account, or touch OpenWebUI's own unit. The database a new release migrates stays migrated;
the copy in the backup folder is how it comes back, by hand.

`owui-ctl` runs as root. It reads the running service's `DATA_DIR` from its environment and logs nothing
of that environment. For a venv uv built (no pip inside), it uses `uv` only from a root-owned path no one
else can write (`/usr/bin/uv` from the distribution's package), never a copy in a user's home: whoever can
replace that file would be running code as root.

## Install

Edit the block at the top of `owui-ctl` (unit name, venv, backup folder) and put the dashboard user's login
name in place of `OWNER` in the rules file. Then, as root:

    install -m 0755 -o root -g root owui-ctl /usr/local/libexec/owui-ctl
    install -m 0644 -o root -g root owui-ctl-start.service owui-ctl-stop.service owui-ctl-update.service /etc/systemd/system/
    install -m 0644 -o root -g root 50-quartermaster-owui.rules /etc/polkit-1/rules.d/
    install -m 0644 -o root -g root /dev/null /var/log/owui-ctl.log
    systemctl daemon-reload

`/usr/local/libexec` may need creating first (`install -d -m 0755 /usr/local/libexec`). polkit reads the
rule at once. Then, as the dashboard's user, with no password asked:

    systemctl start --no-ask-password owui-ctl-stop.service
    systemctl start --no-ask-password owui-ctl-start.service
    tail /var/log/owui-ctl.log

`agent/setup.py detect` (or setup itself) writes the tile with these units; the agent picks them up within
20 seconds of their being installed.

## Remove

    rm /etc/polkit-1/rules.d/50-quartermaster-owui.rules
    rm /etc/systemd/system/owui-ctl-{start,stop,update}.service /usr/local/libexec/owui-ctl
    systemctl daemon-reload

The tile goes back to health and version, with its controls marked not installed.
