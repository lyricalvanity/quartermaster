"""A service that Docker Compose runs, moved by the docker CLI on the inventory's word alone.

  ComposeService(cfg)           cfg is the service's "compose" block: docker, project, project_dir, files, service,
                                and optionally backup (volume | host | none), backup_root, backup_paths, keep_backups
  .ready()                      (True, "") when the engine answers and the compose file is there; else (False, why)
  .state()                      the container as Docker reports it: "running", "exited 1", "absent", ...
  .start(say, timeout)          up -d --no-recreate: starts the container that is there, or creates one if none is
  .stop(say, timeout)           stop: the container, its logs and its image stay, so Start brings back the same one
  .update(say, check, ...)      size the data, pull, stop, copy, recreate, verify; on a failed start, roll back

Why stop and not down: down removes the container, and the next up builds a new one from whatever the image tag
points at by then. A pull made in between (by hand, or by an update that stopped short) would turn Start into an
upgrade nobody backed up. Start uses --no-recreate for the same reason: only update moves to a new image.

Why a copy at all: the install needs none (the old image stays on disk and is re-tagged), but OpenWebUI migrates
its database forward when a new version starts, one way. A version that migrates and then fails leaves a database
the old version may refuse, so the copy is taken before the new one starts and put back when it does not come up.

Where the copy goes (2026-11-07): by default into a Docker named volume beside the data (qm-backup-<project>-
<service>-<stamp>-<version>, labelled with what it holds), so the database never leaves the perimeter it already
lives in: anyone who can read the copy could read the original. A host folder (backup_root) is opt-in, since a
tarball of the database on the host is readable by the owner's account and everything running as it. `none` takes
no copy, and then a failed update rolls back the image only, saying so.

The copy is made by a throwaway container from the image the service is running (its own tar, as root, network
off, --volumes-from), so ownership and modes survive a Windows host and nothing has to be fetched. A restore
empties those same volumes before unpacking, and only paths that are mounts of the container may be named:
neither ever reaches the image's own files. Every call returns an rc; RC_MEANING says what each one left.

Written 2026-11-07 for the OpenWebUI tile (SPEC.md section 3.3). Stdlib only."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAMP = re.compile(r"^\d{8}-\d{6}-")
MODES = ("volume", "host", "none")
LABEL = "qm.backup"                     # the label every backup volume carries, with the project and service in others

# what each rc leaves behind; the agent says it, the control log ends with it
RC_MEANING = {
    0: "done",
    2: "Docker is not answering; nothing changed",
    3: "the service has no running container; nothing changed",
    4: "the data could not be sized or there is no room for its copy; nothing changed",
    5: "the pull failed; nothing changed",
    6: "the data copy failed; the old version is running again, unchanged",
    7: "the container would not stop; the old version is left as it was",
    8: "the new version did not come up; the old image was put back, and the data copy where one was taken",
    9: "the new version did not come up and the roll-back did not finish: the control log says where the copy is",
    124: "the docker command did not return in time",
}


def short(image_id: str) -> str:
    return (image_id or "").split(":")[-1][:12]


def size(kib: float) -> str:
    return f"{kib / 2**20:.1f} GB" if kib >= 2**20 else f"{kib / 1024:.0f} MB" if kib >= 1024 else f"{kib:.0f} kB"


def safe(s: str) -> str:
    """A piece of a volume name: Docker takes [a-zA-Z0-9][a-zA-Z0-9_.-]."""
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(s or "")).strip("_.-") or "x"


def desktop_data_disk() -> Path | None:
    """Docker Desktop's data disk on Windows, links resolved, or None. Desktop keeps it under the folder its
    settings name (CustomWslDistroDir, or DataFolder for the Hyper-V backend), else %LOCALAPPDATA%\\Docker\\wsl, as
    disk\\docker_data.vhdx (today), data\\ext4.vhdx (older) or main\\ext4.vhdx."""
    if os.name != "nt":
        return None
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA")
    for name in ("settings-store.json", "settings.json"):
        f = Path(appdata) / "Docker" / name if appdata else None
        if f and f.is_file():
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for key in ("CustomWslDistroDir", "customWslDistroDir", "DataFolder", "dataFolder"):
                if isinstance(st.get(key), str) and st[key].strip():
                    bases.append(Path(st[key].strip()))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.append(Path(local) / "Docker" / "wsl")
    for base in bases:
        for rel in ("disk/docker_data.vhdx", "data/ext4.vhdx", "main/ext4.vhdx", "DockerDesktopWSL/disk/docker_data.vhdx",
                    "DockerDesktop.vhdx"):
            p = base / rel
            try:
                if p.is_file():
                    return p.resolve()
            except OSError:
                continue
    return None


class Copy:
    """Where one copy lives: a Docker volume (name) or a host folder (dir). `mount` is the helper's argument for it."""

    def __init__(self, mode: str, name: str = "", dir: Path | None = None):
        self.mode, self.name, self.dir = mode, name, dir

    def __str__(self):
        return f"volume {self.name}" if self.mode == "volume" else str(self.dir)

    def mount(self, readonly: bool = False) -> str:
        ro = ",readonly" if readonly else ""
        if self.mode == "volume":
            return f"type=volume,source={self.name},target=/qm-backup{ro}"
        return f"type=bind,source={self.dir},target=/qm-backup{ro}"


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

    # ---- the copy -----------------------------------------------------------------------------------

    def mode(self) -> str:
        """volume unless the inventory says otherwise; a backup_root alone (the first form of the block) means host."""
        m = str(self.cfg.get("backup") or ("host" if self.cfg.get("backup_root") else "volume")).lower()
        return m if m in MODES else "volume"

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

    def helper(self, cid: str, image: str, entrypoint: str, args: list, copy: Copy | None = None, readonly=False) -> list:
        """A throwaway container from the service's own image that sees the service's volumes and, if given, the
        copy at /qm-backup. Network off, root inside (to read and restore every owner and mode)."""
        argv = [self.docker, "run", "--rm", "--network", "none", "--user", "0:0", "--volumes-from", cid]
        if copy is not None:
            argv += ["--mount", copy.mount(readonly)]
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

    def room_for(self, kib: int, copy: Copy) -> tuple[int, str]:
        """(free bytes, where) for the copy: the host folder, or the disk Docker's volumes live on. On Linux that is
        the filesystem under DockerRootDir. On Docker Desktop it is the drive its data disk grows on, found where
        Desktop keeps it: the folder its settings name (CustomWslDistroDir or DataFolder in
        %APPDATA%\\Docker\\settings-store.json), else %LOCALAPPDATA%\\Docker\\wsl, with links resolved, since an owner
        who moved the disk to another drive did it with a junction or that setting (both seen 2026-11-07)."""
        if copy.mode == "host":
            at = copy.dir if copy.dir.exists() else copy.dir.parent
            return shutil.disk_usage(at).free, str(copy.dir)
        rc, out, _ = self.run([self.docker, "info", "-f", "{{.DockerRootDir}}"], timeout=20)
        root = out.strip() if rc == 0 else ""
        if root and os.name != "nt" and Path(root).exists():
            return shutil.disk_usage(root).free, root
        disk = desktop_data_disk()
        if disk:
            return shutil.disk_usage(disk.anchor).free, f"{disk.anchor} (Docker Desktop's data disk, {disk})"
        drive = Path(os.environ.get("LOCALAPPDATA") or Path.home()).anchor or str(Path.home())
        return shutil.disk_usage(drive).free, f"{drive} (Docker Desktop's data disk was not found; this is a guess at its drive)"

    def new_copy(self, old_ver: str, backup_root: Path | None) -> Copy:
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{safe(old_ver)}"
        if self.mode() == "host":
            return Copy("host", dir=Path(backup_root) / stamp)
        return Copy("volume", name=f"qm-backup-{safe(self.project)}-{safe(self.service)}-{stamp}")

    def make_copy(self, cid: str, image: str, paths: list, copy: Copy, meta: dict) -> str:
        """Create the copy, fill it, verify it. "" when it is whole; else what went wrong. On failure nothing is
        left behind."""
        if copy.mode == "host":
            copy.dir.mkdir(parents=True)
        else:
            rc, _, err = self.run([self.docker, "volume", "create", "--label", f"{LABEL}=1", "--label", f"{LABEL}.project={self.project}",
                                   "--label", f"{LABEL}.service={self.service}", "--label", f"{LABEL}.version={meta['version']}",
                                   "--label", f"{LABEL}.image_id={meta['image_id']}", copy.name], timeout=60)
            if rc != 0:
                return f"the volume could not be made: {err.strip()[:160]}"
        rc, out, err = self.run(self.helper(cid, image, "tar", ["-cf", "/qm-backup/data.tar", "-C", "/", *[p.lstrip("/") for p in paths]], copy),
                                timeout=3600)
        problem = (err or out).strip()[:200] if rc != 0 else self.check_tar(cid, image, copy, paths)
        if not problem:
            doc = json.dumps(meta, indent=2) + "\n"
            if copy.mode == "host":
                try:
                    (copy.dir / "backup.json").write_text(doc, encoding="utf-8")
                except OSError as e:
                    problem = f"the copy's description could not be written: {e}"
            else:
                rc, out, err = self.run(self.helper(cid, image, "sh", ["-c", 'printf "%s" "$1" > /qm-backup/backup.json', "qm-meta", doc], copy), timeout=60)
                if rc != 0:
                    problem = f"the copy's description could not be written: {(err or out).strip()[:160]}"
        if problem:
            self.drop_copy(copy)
        return problem

    def check_tar(self, cid: str, image: str, copy: Copy, paths: list) -> str:
        """"" when the tarball lists every path it should; else what is wrong with it. Read where it lies, by the
        same image's tar, so a volume copy is checked as a host one is."""
        rc, out, err = self.run(self.helper(cid, image, "tar", ["-tf", "/qm-backup/data.tar"], copy, readonly=True), timeout=600)
        if rc != 0:
            return f"the copy does not open: {(err or out).strip()[:160]}"
        names = {n.rstrip("/") for n in out.splitlines()}
        missing = [p for p in paths if p.strip("/") not in names]
        return f"the copy lacks {', '.join(missing)}" if missing else ""

    def copy_size(self, cid: str, image: str, copy: Copy) -> str:
        rc, out, _ = self.run(self.helper(cid, image, "du", ["-sk", "/qm-backup/data.tar"], copy, readonly=True), timeout=120)
        parts = out.split() if rc == 0 else []
        return size(int(parts[0])) if parts and parts[0].isdigit() else "?"

    def drop_copy(self, copy: Copy):
        if copy.mode == "host":
            shutil.rmtree(copy.dir, ignore_errors=True)
        else:
            self.run([self.docker, "volume", "rm", "-f", copy.name], timeout=60)

    def restore_hint(self, cid_hint: str, image_ref: str, paths: list, copy: Copy) -> str:
        """One line a person can run by hand, kept in backup.json."""
        inner = 'for p in "$@"; do find "$p" -mindepth 1 -delete; done; tar -xpf /qm-backup/data.tar -C /'
        return (f"stop the service, docker tag <old image id> {image_ref}, docker compose up --no-start, then: docker run --rm --network none "
                f"--user 0:0 --volumes-from <container> --mount {copy.mount(readonly=True)} --entrypoint sh {image_ref} -c '{inner}' qm-restore "
                + " ".join(paths) + "; then docker compose start")

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

    # ---- update ------------------------------------------------------------------------------------

    def update(self, say, check, healthy, version, backup_root: Path | None = None, keep: int = 3, health_timeout: float = 600) -> int:
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
        mode = self.mode()
        paths: list = []
        kib = 0
        copy: Copy | None = None
        if mode == "none":
            say(f"running {ref} ({short(old_id)}), version {old_ver}; no copy of the data, as the inventory says: a failed "
                "update rolls the image back and the database stays as the new version left it")
        else:
            try:
                paths = self.backup_paths(info)
            except ValueError as e:
                say(str(e))
                return 4
            say(f"running {ref} ({short(old_id)}), version {old_ver}; data {', '.join(paths)}")
            if mode == "host" and "," in str(backup_root):
                say(f"the backup folder's path has a comma, which docker's --mount cannot take: {backup_root}")
                return 4
            if mode == "host" and not backup_root:
                say("backup is host but the inventory names no backup_root")
                return 4
            kib = self.data_kib(cid, old_id, paths)
            if kib is None:
                say("the data could not be sized (the image has no du?); a copy could not be made either")
                return 4
            copy = self.new_copy(old_ver, backup_root)
            if mode == "host":
                copy.dir.parent.mkdir(parents=True, exist_ok=True)
            free, where = self.room_for(kib, copy)
            need = int(kib * 1024 * 1.2) + 256 * 2**20
            if free < need:
                say(f"{size(kib)} of data and {size(free / 1024)} free at {where}: not enough room for the copy")
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

        say(f"new image {short(new_id)}; stopping {self.service}" + (" for a consistent copy" if copy else ""))
        rc = self.stream(self.base() + ["stop", self.service], say, 180)
        if rc != 0:
            self.run([self.docker, "tag", old_id, ref], timeout=30)
            return 7

        if copy:
            say(f"copying {size(kib)} of data to {copy}")
            meta = {"project": self.project, "service": self.service, "image_ref": ref, "image_id": old_id, "version": old_ver,
                    "paths": paths, "kib": kib, "made": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "holds": "the service's data volumes, the database among them: as private as the volumes themselves",
                    "restore": self.restore_hint(cid, ref, paths, copy)}
            problem = self.make_copy(cid, old_id, paths, copy, meta)
            if problem:
                say(f"the copy failed ({problem}); starting the old version again unchanged")
                self.run([self.docker, "tag", old_id, ref], timeout=30)
                self.stream(self.base() + ["up", "-d", "--no-recreate", self.service], say, 180)
                return 6
            say(f"copied ({self.copy_size(cid, old_id, copy)})")

        say(f"recreating {self.service} on {short(new_id)}")
        rc = self.stream(self.base() + ["up", "-d", "--no-deps", self.service], say, 600)
        if rc == 0 and self.wait_up(healthy, health_timeout, say):
            new_ver = version() or "unknown"
            say(f"now {new_ver}; the previous image {short(old_id)} stays on disk, untagged, until you prune it")
            if copy:
                self.prune(copy, backup_root, keep, say)
            return 0

        say(f"rolling back to {old_ver}: the old image" + (", then the data as it was copied" if copy else
                                                            "; the data was not copied, so it stays as the new version left it"))
        return 8 if self.roll_back(old_id, ref, copy, paths, healthy, say, health_timeout) else 9

    def roll_back(self, old_id, ref, copy: Copy | None, paths: list, healthy, say, health_timeout) -> bool:
        self.stream(self.base() + ["stop", self.service], say, 180)
        rc, _, err = self.run([self.docker, "tag", old_id, ref], timeout=30)
        if rc != 0:
            say(f"could not point {ref} back at {short(old_id)}: {err.strip()[:160]}")
            if copy:
                say(f"the data copy is at {copy}")
            return False
        if copy is None:
            if self.stream(self.base() + ["up", "-d", "--no-deps", self.service], say, 300) != 0 or not self.wait_up(healthy, health_timeout, say):
                say("the old version did not come back up either")
                return False
            say("the old version is running again; if it refuses the database, the new version migrated it: go forward, or restore a copy of your own")
            return True
        # a container on the old image, not started, so the data goes back with nothing holding it
        if self.stream(self.base() + ["up", "--no-start", "--no-deps", self.service], say, 300) != 0:
            say(f"could not recreate {self.service} on {short(old_id)}; the data copy is at {copy}")
            return False
        cid = self.container()
        restore = 'set -e; for p in "$@"; do find "$p" -mindepth 1 -delete; done; tar -xpf /qm-backup/data.tar -C /'
        rc, out, err = self.run(self.helper(cid, old_id, "sh", ["-c", restore, "qm-restore", *paths], copy, readonly=True), timeout=3600)
        if rc != 0:
            say(f"putting the data back failed: {(err or out).strip()[:160]}; the copy is at {copy}")
            return False
        say(f"data restored from {copy}")
        if self.stream(self.base() + ["start", self.service], say, 180) != 0 or not self.wait_up(healthy, health_timeout, say):
            say(f"the old version did not come back up either; the data copy is at {copy}")
            return False
        say("the old version is running again")
        return True

    def copies(self, backup_root: Path | None) -> list[str]:
        """This service's copies, newest first: volume names, or host folder names, as this code made them."""
        if self.mode() == "host":
            if not backup_root or not Path(backup_root).is_dir():
                return []
            mine = []
            for d in Path(backup_root).iterdir():
                meta = d / "backup.json"
                if d.is_dir() and STAMP.match(d.name) and meta.exists():
                    try:
                        m = json.loads(meta.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if m.get("project") == self.project and m.get("service") == self.service:
                        mine.append(d.name)
            return sorted(mine, reverse=True)
        rc, out, _ = self.run([self.docker, "volume", "ls", "-q", "--filter", f"label={LABEL}.project={self.project}",
                               "--filter", f"label={LABEL}.service={self.service}"], timeout=30)
        return sorted((n for n in out.split() if n.startswith("qm-backup-")), reverse=True) if rc == 0 else []

    def prune(self, copy: Copy, backup_root: Path | None, keep: int, say):
        """Keep the newest copies of this service; only copies this code made (the stamp, the label or the
        backup.json) go."""
        for name in self.copies(backup_root)[max(1, int(keep)):]:
            self.drop_copy(Copy(copy.mode, name=name, dir=(Path(backup_root) / name) if copy.mode == "host" and backup_root else None))
            say(f"removed the old copy {name}")
