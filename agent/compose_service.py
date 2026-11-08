"""A service that Docker Compose runs, moved by the docker CLI on the inventory's word alone.

  ComposeService(cfg)           cfg is the service's "compose" block: docker, project, project_dir, files, service,
                                and optionally backup_paths, backup_root, keep_backups
  .ready()                      (True, "") when the engine answers and the compose file is there; else (False, why)
  .state()                      the container as Docker reports it: "running", "exited 1", "absent", ...
  .start(say, timeout)          up -d --no-recreate: starts the container that is there, or creates one if none is
  .stop(say, timeout)           stop: the container, its logs and its image stay, so Start brings back the same one
  .update(say, check, ...)      size the data, pull, stop, back up, recreate, verify; on a failed start, roll back

Why stop and not down: down removes the container, and the next up builds a new one from whatever the image tag
points at by then. A pull made in between (by hand, or by an update that stopped short) would turn Start into an
upgrade nobody backed up. Start uses --no-recreate for the same reason: only update moves to a new image.

The backup is a tarball of the container's own volumes, made by a throwaway container from the image the service
is running (its own tar, as root, network off), so ownership and modes survive a Windows host and nothing has to be
fetched. A restore empties those same volumes before unpacking, and only paths that are mounts of the container may
be named: neither ever reaches the image's own files. Every call returns an rc; RC_MEANING says what each one left.

Written 2026-11-07 for the OpenWebUI tile (SPEC.md section 3.3). Stdlib only."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAMP = re.compile(r"^\d{8}-\d{6}-")

# what each rc leaves behind; the agent says it, the control log ends with it
RC_MEANING = {
    0: "done",
    2: "Docker is not answering; nothing changed",
    3: "the service has no running container; nothing changed",
    4: "the data could not be sized or there is no room for its copy; nothing changed",
    5: "the pull failed; nothing changed",
    6: "the data copy failed; the old version is running again, unchanged",
    7: "the container would not stop; the old version is left as it was",
    8: "the new version did not come up; the old image and the data copy were put back",
    9: "the new version did not come up and the roll-back did not finish: the control log says where the copy is",
    124: "the docker command did not return in time",
}


def short(image_id: str) -> str:
    return (image_id or "").split(":")[-1][:12]


def size(kib: float) -> str:
    return f"{kib / 2**20:.1f} GB" if kib >= 2**20 else f"{kib / 1024:.0f} MB" if kib >= 1024 else f"{kib:.0f} kB"


class ComposeService:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.docker = cfg.get("docker") or shutil.which("docker") or ""
        self.project = cfg.get("project") or ""
        self.project_dir = cfg.get("project_dir") or ""
        self.files = [f for f in (cfg.get("files") or []) if f]
        self.service = cfg.get("service") or ""
        self._ready = (0.0, False, "")

    # ---- talking to docker -------------------------------------------------------------------

    def base(self) -> list[str]:
        argv = [self.docker, "compose", "--progress", "plain"]
        for f in self.files:
            argv += ["-f", f]
        if self.project_dir:
            argv += ["--project-directory", self.project_dir]
        if self.project:
            argv += ["-p", self.project]
        return argv

    def run(self, argv, timeout=60) -> tuple[int, str, str]:
        try:
            p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
            return p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", f"{argv[1] if len(argv) > 1 else argv[0]}: no answer in {timeout} s"
        except OSError as e:
            return 1, "", str(e)

    def stream(self, argv, say, timeout) -> int:
        """Run a docker command, handing each line of its output to say as it comes. Returns its exit code."""
        try:
            p = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                 creationflags=NO_WINDOW)
        except OSError as e:
            say(str(e))
            return 1

        def pump():
            for line in p.stdout:
                line = ANSI.sub("", line).strip()
                if line:
                    say(line)
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            say(f"no end in {int(timeout)} s; stopped waiting")
            rc = 124
        t.join(timeout=5)
        return rc

    def container(self) -> str:
        """The service's container id, running or not; "" when it has none."""
        rc, out, _ = self.run(self.base() + ["ps", "-a", "-q", self.service], timeout=30)
        ids = out.split() if rc == 0 else []
        return ids[0] if ids else ""

    def inspect(self, cid: str) -> dict:
        rc, out, _ = self.run([self.docker, "inspect", cid], timeout=30)
        try:
            data = json.loads(out) if rc == 0 else []
        except ValueError:
            data = []
        return data[0] if data else {}

    def image_id(self, ref: str) -> str:
        rc, out, _ = self.run([self.docker, "image", "inspect", "-f", "{{.Id}}", ref], timeout=30)
        return out.strip() if rc == 0 else ""

    # ---- what the tile reads ---------------------------------------------------------------------

    def ready(self, fresh: bool = False) -> tuple[bool, str]:
        """Can the controls run: a docker CLI, an engine that answers, the compose file, a service named."""
        at, ok, why = self._ready
        if not fresh and time.time() - at < 15:
            return ok, why
        if not self.docker or not (Path(self.docker).is_file() or shutil.which(self.docker)):
            ok, why = False, f"no docker CLI at {self.docker or 'any place on PATH'} (openwebui.compose.docker names it)"
        elif not self.service:
            ok, why = False, "openwebui.compose names no service"
        elif self.files and not all(Path(f).exists() for f in self.files):
            ok, why = False, f"the compose file is not there: {', '.join(f for f in self.files if not Path(f).exists())}"
        else:
            rc, out, err = self.run([self.docker, "version", "-f", "{{.Server.Version}}"], timeout=15)
            ok = rc == 0 and bool(out.strip())
            why = "" if ok else "Docker is not answering (is Docker Desktop running?)"
        self._ready = (time.time(), ok, why)
        return ok, why

    def state(self) -> str:
        cid = self.container()
        if not cid:
            return "absent"
        s = (self.inspect(cid).get("State") or {})
        status = s.get("Status") or "unknown"
        return f"{status} {s.get('ExitCode')}" if status == "exited" else status

    # ---- start and stop ------------------------------------------------------------------------

    def start(self, say, timeout=180) -> int:
        ok, why = self.ready(fresh=True)
        if not ok:
            say(why)
            return 2
        say(f"docker compose up -d --no-recreate {self.service}")
        return self.stream(self.base() + ["up", "-d", "--no-recreate", self.service], say, timeout)

    def stop(self, say, timeout=180) -> int:
        ok, why = self.ready(fresh=True)
        if not ok:
            say(why)
            return 2
        say(f"docker compose stop {self.service} (the container stays; Start brings it back)")
        return self.stream(self.base() + ["stop", self.service], say, timeout)

    # ---- update ------------------------------------------------------------------------------------

    def backup_paths(self, info: dict) -> list[str]:
        """The paths a copy reads and a restore empties: the inventory's backup_paths, else every named volume the
        container mounts. Each must be one of the container's mounts, so neither ever touches the image's files."""
        mounts = info.get("Mounts") or []
        dests = {m.get("Destination") for m in mounts}
        paths = list(self.cfg.get("backup_paths") or [m.get("Destination") for m in mounts if m.get("Type") == "volume"])
        if not paths:
            raise ValueError("the container mounts no volume and openwebui.compose.backup_paths names none")
        for p in paths:
            if not (isinstance(p, str) and p.startswith("/") and p.strip("/") and ".." not in p.split("/")):
                raise ValueError(f"backup path {p!r} is not an absolute path below /")
            if p not in dests:
                raise ValueError(f"backup path {p} is not a mount of the container ({', '.join(sorted(d for d in dests if d)) or 'none'})")
        return paths

    def helper(self, cid: str, image: str, entrypoint: str, args: list, backup_dir: Path | None = None, readonly=False) -> list:
        """A throwaway container from the service's own image that sees the service's volumes and, if given, the
        backup folder at /qm-backup. Network off, root inside (to read and restore every owner and mode)."""
        argv = [self.docker, "run", "--rm", "--network", "none", "--user", "0:0", "--volumes-from", cid]
        if backup_dir is not None:
            argv += ["--mount", f"type=bind,source={backup_dir},target=/qm-backup" + (",readonly" if readonly else "")]
        return argv + ["--entrypoint", entrypoint, image] + list(args)

    def data_kib(self, cid: str, image: str, paths: list) -> int | None:
        rc, out, _ = self.run(self.helper(cid, image, "du", ["-sk", *paths]), timeout=300)
        if rc != 0:
            return None
        total = 0
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].isdigit():
                total += int(parts[0])
        return total

    def wait_up(self, healthy, timeout, say) -> bool:
        """Until the health check answers, the container exits, or the time runs out."""
        deadline = time.time() + timeout
        last_said = time.time()
        while time.time() < deadline:
            try:
                if healthy():
                    return True
            except Exception:  # noqa: BLE001 - not up yet
                pass
            st = self.state()
            if st.startswith("exited") or st in ("dead", "absent"):
                rc, out, err = self.run([self.docker, "logs", "--tail", "6", self.container() or "none"], timeout=30)
                say(f"the container is {st}; its last lines:")
                for line in (out + err).strip().splitlines()[-6:]:
                    say("  " + ANSI.sub("", line)[:150])
                return False
            if time.time() - last_said > 30:
                say(f"waiting for /health ({int(deadline - time.time())} s left; container {st})")
                last_said = time.time()
            time.sleep(3)
        say(f"/health did not answer in {int(timeout)} s")
        return False

    def update(self, say, check, healthy, version, backup_root: Path, keep: int = 3, health_timeout: float = 600) -> int:
        """check() is called only while nothing has changed: once the container is stopped, the update runs to its
        end or to a roll-back, whatever is asked of it."""
        ok, why = self.ready(fresh=True)
        if not ok:
            say(why)
            return 2
        cid = self.container()
        info = self.inspect(cid) if cid else {}
        if (info.get("State") or {}).get("Status") != "running":
            say(f"{self.service} has no running container ({self.state()}); start it first")
            return 3
        old_id, ref = info.get("Image") or "", (info.get("Config") or {}).get("Image") or ""
        old_ver = version() or "unknown"
        try:
            paths = self.backup_paths(info)
        except ValueError as e:
            say(str(e))
            return 4
        say(f"running {ref} ({short(old_id)}), version {old_ver}; data {', '.join(paths)}")

        backup_root = Path(backup_root)
        if "," in str(backup_root):
            say(f"the backup folder's path has a comma, which docker's --mount cannot take: {backup_root}")
            return 4
        kib = self.data_kib(cid, old_id, paths)
        if kib is None:
            say("the data could not be sized (the image has no du?); a copy could not be made either")
            return 4
        backup_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(backup_root).free
        need = int(kib * 1024 * 1.2) + 256 * 2**20
        if free < need:
            say(f"{size(kib)} of data and {size(free / 1024)} free at {backup_root}: not enough room for the copy")
            return 4
        check()

        say(f"docker compose pull {self.service}")
        rc = self.stream(self.base() + ["pull", self.service], say, 1800)
        if rc != 0:
            self.run([self.docker, "tag", old_id, ref], timeout=30)       # a pull that died part way moves nothing, but make sure
            return 124 if rc == 124 else 5
        new_id = self.image_id(ref)
        if not new_id:
            say(f"the pull ended but {ref} names no image now; nothing else was touched")
            self.run([self.docker, "tag", old_id, ref], timeout=30)
            return 5
        if new_id == old_id:
            say(f"already current: {ref} is still {short(old_id)}")
            return 0
        check()                                                # the last point where stopping changes nothing worth undoing
        # from here on there is no cancel: the update finishes or rolls back

        say(f"new image {short(new_id)}; stopping {self.service} for a consistent copy")
        rc = self.stream(self.base() + ["stop", self.service], say, 180)
        if rc != 0:
            self.run([self.docker, "tag", old_id, ref], timeout=30)
            return 7

        dest = backup_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^0-9A-Za-z.]+', '_', old_ver)}"
        dest.mkdir(parents=True)
        say(f"copying {size(kib)} of data to {dest}")
        rc, out, err = self.run(self.helper(cid, old_id, "tar", ["-cf", "/qm-backup/data.tar", "-C", "/",
                                                                 *[p.lstrip("/") for p in paths]], dest), timeout=3600)
        problem = (err or out).strip()[:200] if rc != 0 else self.check_tar(dest / "data.tar", paths)
        if problem:
            say(f"the copy failed ({problem}); starting the old version again unchanged")
            shutil.rmtree(dest, ignore_errors=True)
            self.run([self.docker, "tag", old_id, ref], timeout=30)
            self.stream(self.base() + ["up", "-d", "--no-recreate", self.service], say, 180)
            return 6
        (dest / "backup.json").write_text(json.dumps({
            "project": self.project, "service": self.service, "image_ref": ref, "image_id": old_id, "version": old_ver,
            "paths": paths, "kib": kib, "made": time.strftime("%Y-%m-%d %H:%M:%S"),
            "restore": "stop the service, docker tag <image_id> <image_ref>, then empty each path and untar data.tar at /"
        }, indent=2) + "\n", encoding="utf-8")
        say(f"copied ({size((dest / 'data.tar').stat().st_size / 1024)})")

        say(f"recreating {self.service} on {short(new_id)}")
        rc = self.stream(self.base() + ["up", "-d", "--no-deps", self.service], say, 600)
        if rc == 0 and self.wait_up(healthy, health_timeout, say):
            new_ver = version() or "unknown"
            say(f"now {new_ver}; the previous image {short(old_id)} stays on disk, untagged, until you prune it")
            self.prune(backup_root, keep, say)
            return 0

        say(f"rolling back to {old_ver}: the old image, then the data as it was copied")
        return 8 if self.roll_back(old_id, ref, dest, paths, healthy, say, health_timeout) else 9

    def check_tar(self, tar_path: Path, paths: list) -> str:
        """"" when the tarball opens and holds every path it should; else what is wrong with it."""
        try:
            with tarfile.open(tar_path) as t:
                names = {n.rstrip("/") for n in t.getnames()}
        except (OSError, tarfile.TarError) as e:
            return f"the copy does not open: {e}"
        missing = [p for p in paths if p.strip("/") not in names]
        return f"the copy lacks {', '.join(missing)}" if missing else ""

    def roll_back(self, old_id, ref, dest: Path, paths: list, healthy, say, health_timeout) -> bool:
        self.stream(self.base() + ["stop", self.service], say, 180)
        rc, _, err = self.run([self.docker, "tag", old_id, ref], timeout=30)
        if rc != 0:
            say(f"could not point {ref} back at {short(old_id)}: {err.strip()[:160]}")
            say(f"the data copy is at {dest}")
            return False
        # a container on the old image, not started, so the data goes back with nothing holding it
        if self.stream(self.base() + ["up", "--no-start", "--no-deps", self.service], say, 300) != 0:
            say(f"could not recreate {self.service} on {short(old_id)}; the data copy is at {dest}")
            return False
        cid = self.container()
        restore = 'set -e; for p in "$@"; do find "$p" -mindepth 1 -delete; done; tar -xpf /qm-backup/data.tar -C /'
        rc, out, err = self.run(self.helper(cid, old_id, "sh", ["-c", restore, "qm-restore", *paths], dest, readonly=True),
                                timeout=3600)
        if rc != 0:
            say(f"putting the data back failed: {(err or out).strip()[:160]}; the copy is at {dest}")
            return False
        say(f"data restored from {dest}")
        if self.stream(self.base() + ["start", self.service], say, 180) != 0 or not self.wait_up(healthy, health_timeout, say):
            say(f"the old version did not come back up either; the data copy is at {dest}")
            return False
        say("the old version is running again")
        return True

    def prune(self, backup_root: Path, keep: int, say):
        """Keep the newest copies of this service; only folders this code made (a stamp and a backup.json) go."""
        mine = []
        for d in backup_root.iterdir() if backup_root.is_dir() else []:
            meta = d / "backup.json"
            if d.is_dir() and STAMP.match(d.name) and meta.exists():
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if m.get("project") == self.project and m.get("service") == self.service:
                    mine.append(d)
        for d in sorted(mine, key=lambda x: x.name, reverse=True)[max(1, int(keep)):]:
            shutil.rmtree(d, ignore_errors=True)
            say(f"removed the old copy {d.name}")
