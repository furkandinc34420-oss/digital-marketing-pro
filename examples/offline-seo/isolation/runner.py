"""Trusted local controller for disposable, per-customer Docker test workers.

Uses only the existing Python runtime, stdlib, system libraries and demo files.
No registry pulls, host bind mounts, credentials or package installation.
The controller has Docker privileges; never expose it to tenant-supplied code.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import selectors
import signal
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import uuid
from execution_control import checkpoint, current, TaskCancelled, StopFailed

REPO = Path(__file__).resolve().parents[3]
CUSTOMERS = ("demo-musteri", "diger-demo")
SOCKET = "unix:///var/run/docker.sock"
MAX_RUNTIME_FILE = 64 * 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
TMPFS = "rw,noexec,nosuid,nodev,size=8m,mode=0700,uid=65534,gid=65534"

_spec = importlib.util.spec_from_file_location("_dmp_snapshot_reader", REPO / "scripts/offline_seo.py")
seo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = seo
_spec.loader.exec_module(seo)


class IsolationUnavailable(RuntimeError):
    """No supported local isolation backend; never run an unisolated fallback."""


def bounded_command(command, env, timeout, limit=MAX_COMMAND_OUTPUT, checkpoint=None,
                    dispatch=None, on_started=None):
    """Bound combined stdout/stderr in memory, killing the CLI group on failure."""
    deadline = time.monotonic() + timeout
    output = {"out": bytearray(), "err": bytearray()}
    def launch():
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        except OSError:
            # Native Popen reports exec/fork failure after reaping its failed
            # child: the CLI did not execute. Keep the not_sent state.
            raise
        except BaseException:
            # An interruption during spawn has an uncertain outcome.
            if on_started is not None: on_started()
            raise
        if on_started is not None: on_started()
        return process
    process = dispatch(launch) if dispatch is not None else launch()
    try:
        with selectors.DefaultSelector() as selected:
            selected.register(process.stdout, selectors.EVENT_READ, "out")
            selected.register(process.stderr, selectors.EVENT_READ, "err")
            while selected.get_map():
                if checkpoint is not None: checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selected.select(min(remaining, 0.25)):
                    chunk = os.read(key.fd, min(65536, limit + 1 - sum(map(len, output.values()))))
                    if not chunk:
                        selected.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    if sum(map(len, output.values())) > limit:
                        raise RuntimeError("Command output exceeds byte limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(command, process.returncode,
                                           output["out"].decode("utf-8", errors="replace"),
                                           output["err"].decode("utf-8", errors="replace"))
    finally:
        # Also close descendant CLI processes retaining pipes; not Docker workers.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


class ArchiveLimitWriter:
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    def write(self, data):
        if self.stream.tell() + len(data) > self.limit:
            raise RuntimeError("Archive exceeds byte limit")
        return self.stream.write(data)

    def tell(self):
        return self.stream.tell()


def snapshot_runtime(path):
    """Trusted runtime paths may be aliases; resolve once, then snapshot the fd."""
    canonical = path.resolve(strict=True)
    fd = seo.open_descriptor(canonical, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RUNTIME_FILE:
            raise RuntimeError("Invalid or oversized runtime/source file")
        chunks = bytearray()
        while len(chunks) <= MAX_RUNTIME_FILE:
            chunk = os.read(fd, min(65536, MAX_RUNTIME_FILE + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(fd)
        if len(chunks) > MAX_RUNTIME_FILE or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError("Runtime/source changed or exceeded limit during read")
        return bytes(chunks), 0o755 if before.st_mode & 0o111 else 0o644
    finally:
        os.close(fd)


class DockerLab:
    def __init__(self):
        self.label = "dmp-offline-isolation=" + uuid.uuid4().hex
        self.images = {}
        self.containers = set()
        self.orphan_images = set()
        self.pending_creations = set()
        self.creation_states = {"container": "not_sent", "image": "not_sent"}
        self.fixture_root = REPO / "examples/offline-seo/customers"
        self.binary = Path(sys.executable).resolve()
        self.docker = shutil.which("docker")

    def _docker(self, *args, timeout=45):
        if not self.docker:
            raise IsolationUnavailable("Docker executable unavailable")
        # Explicit local socket: ignore remote contexts and Docker credential config.
        env = {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C.UTF-8"}
        guarded = bool(args and args[0] in ('create', 'import', 'start'))
        control = current() if guarded else None
        kind = {'create':'container', 'import':'image'}.get(args[0]) if args else None
        if kind is not None: self.creation_states[kind] = 'not_sent'
        def started():
            self.pending_creations.add(kind)
            self.creation_states[kind] = 'sent_unconfirmed'
        options = {}
        if control is not None: options['dispatch'] = control.dispatch
        if kind is not None: options['on_started'] = started
        if control is not None and args[:2] == ('start', '--attach'):
            options['checkpoint'] = control.checkpoint
        result = bounded_command([self.docker, "--host", SOCKET, *args], env, timeout, **options)
        if result.returncode:
            raise RuntimeError("Local Docker operation failed: " + result.stderr.strip()[:800])
        return result.stdout.strip()

    def __enter__(self):
        if platform.system() != "Linux":
            raise IsolationUnavailable("This test backend requires Linux")
        try:
            self._docker("version", "--format", "{{.Server.Version}}")
        except (RuntimeError, OSError) as exc:
            raise IsolationUnavailable("Local Docker daemon is unavailable") from exc
        try:
            self._prepare()
        except BaseException as original:
            try:
                self.close()
            except RuntimeError as cleanup:
                if current() is not None: current().failed()
                raise RuntimeError(f"Setup failed: {original}; cleanup failed: {cleanup}") from original
            raise
        return self

    def _runtime_files(self):
        stdlib = Path(sysconfig.get_path("stdlib")).resolve()
        runtime = {self.binary}
        for directory, dirs, files in os.walk(stdlib):
            dirs[:] = [d for d in dirs if d not in {
                "site-packages", "__pycache__", "test", "tests", "ensurepip"}]
            for name in files:
                path = Path(directory) / name
                if path.is_file():
                    runtime.add(path)
        # Include dependencies for stdlib extension modules, not host /etc or home.
        libraries = set()
        for path in [self.binary, *sorted(p for p in runtime if p.suffix == ".so")]:
            result = bounded_command(["ldd", str(path)], {"PATH": os.defpath}, 10)
            if result.returncode:
                raise RuntimeError("Cannot collect local Python shared libraries")
            for match in re.findall(r"(?:=>\s*)?(/[^\s()]+)", result.stdout):
                library = Path(match)
                if library.is_file():
                    libraries.add(library)
        return runtime | libraries

    def _customer_snapshots(self):
        snapshots = {}
        for customer in CUSTOMERS:
            scope = seo.TenantScope(self.fixture_root, customer)
            raw = {name: scope.read_bytes(name) for name in ("profile.json", "pages.json")}
            data = {name: json.loads(content.decode("utf-8-sig")) for name, content in raw.items()}
            for record in data.values():
                if (not isinstance(record, dict) or record.get("customer_id") != customer
                        or record.get("synthetic") is not True or record.get("source") != "wordpress-fixture"):
                    raise RuntimeError("Customer fixture identity or synthetic marker mismatch")
            pages = data["pages.json"].get("pages")
            if not isinstance(pages, list) or not 1 <= len(pages) <= seo.MAX_PAGES:
                raise RuntimeError("Invalid page count")
            host = seo._fixture_url(data["profile.json"].get("site_url"))
            for page in pages:
                if (not isinstance(page, dict) or page.get("customer_id") != customer
                        or seo._fixture_url(page.get("url")) != host
                        or not isinstance(page.get("html"), str)
                        or not 1 <= len(page["html"]) <= seo.MAX_HTML_CHARS):
                    raise RuntimeError("Page identity, site or HTML limit mismatch")
            snapshots[customer] = raw
        return snapshots

    @staticmethod
    def _write_bundle(bundle, items):
        planned = []
        estimate = 10240
        for target, (raw, mode) in items.items():
            if target.startswith("/") or ".." in Path(target).parts:
                raise RuntimeError("Unsafe archive target")
            info = tarfile.TarInfo(target)
            info.size, info.mode, info.uid, info.gid = len(raw), mode, 0, 0
            estimate += len(info.tobuf(format=tarfile.PAX_FORMAT)) + ((len(raw) + 511) // 512) * 512
            if estimate > MAX_ARCHIVE:
                raise RuntimeError("Planned archive exceeds byte limit")
            planned.append((info, raw))
        # No source paths are reopened here: only the already validated snapshots.
        with bundle.open("wb") as stream:
            with tarfile.open(fileobj=ArchiveLimitWriter(stream, MAX_ARCHIVE), mode="w",
                              format=tarfile.PAX_FORMAT) as archive:
                for info, raw in planned:
                    archive.addfile(info, io.BytesIO(raw))

    def _prepare(self):
        snapshots = self._customer_snapshots()  # Before creating any Docker resource.
        common = {}
        total = 0
        for path in sorted(self._runtime_files()):
            content = snapshot_runtime(path)
            total += len(content[0])
            if total > MAX_ARCHIVE:
                raise RuntimeError("Runtime snapshots exceed total byte limit")
            common[str(path).lstrip("/")] = content
        for name in ("offline_seo.py", "tech-seo-auditor.py", "_common.py"):
            common["app/" + name] = (seo.read_bounded_file(REPO, ("scripts", name), MAX_RUNTIME_FILE), 0o644)
        for name in ("worker.py", "content_seo.py", "content_schema.py", "technical_seo.py", "technical_schema.py", "technical_rules.py"):
            common["app/" + name] = (seo.read_bounded_file(
                REPO, ("examples", "offline-seo", "isolation", name), MAX_RUNTIME_FILE), 0o644)
        with tempfile.TemporaryDirectory(prefix="dmp-isolation-") as tmp:
            for customer in CUSTOMERS:
                bundle = Path(tmp) / (customer + ".tar")
                items = dict(common)
                items.update({"data/customers/" + customer + "/" + name: (raw, 0o644)
                              for name, raw in snapshots[customer].items()})
                self._write_bundle(bundle, items)
                image = self._docker("import", "--change", "LABEL " + self.label, str(bundle))
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                    raise RuntimeError("Unexpected local image identity")
                self.images[customer] = image
                self.creation_states["image"] = "identified"
                self.pending_creations.discard("image")

    def _worker_command(self, customer, action):
        return [str(self.binary), "-I", "-S", "-B", "/app/worker.py",
                "--customer", customer, "--action", action, "--host-fixtures", str(self.fixture_root)]

    def _create(self, customer, action):
        return self._docker(
            "create", "--pull=never", "--label", self.label,
            "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--pids-limit=32", "--memory=256m",
            "--memory-swap=256m", "--log-driver=none", "--shm-size=8m",
            "--cpus=0.5", "--ipc=private", "--cgroupns=private",
            "--tmpfs", "/output:" + TMPFS,
            "--tmpfs", "/tmp:" + TMPFS,
            "--env=LANG=C.UTF-8", "--env=HOME=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1",
            self.images[customer], *self._worker_command(customer, action))

    def _validate_config(self, config, customer, action):
        try:
            h, c = config["HostConfig"], config["Config"]
            key, value = self.label.split("=", 1)
            env = dict(item.split("=", 1) for item in c["Env"])
            expected_env = {"LANG": "C.UTF-8", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"}
            if "PATH" in env:
                expected_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            checks = {
                "ownership/image": c["Labels"].get(key) == value and config["Image"] == self.images[customer],
                "command": c["Cmd"] == self._worker_command(customer, action) and not c.get("Entrypoint"),
                "environment": env == expected_env and len(env) == len(c["Env"]),
                "network": h["NetworkMode"] == "none" and not h.get("PortBindings") and not h["PublishAllPorts"],
                "readonly/user": h["ReadonlyRootfs"] is True and c["User"] == "65534:65534",
                "privileges": h["Privileged"] is False and h["CapDrop"] == ["ALL"] and not h.get("CapAdd"),
                "security options": h["SecurityOpt"] == ["no-new-privileges:true"],
                "namespaces": h["PidMode"] in ("", "private") and h["IpcMode"] == "private"
                              and h["CgroupnsMode"] == "private" and h["UsernsMode"] == "",
                "mounts/devices": not config["Mounts"] and not h.get("Binds") and not h.get("Mounts")
                                  and not c.get("Volumes") and not h.get("Devices") and not h.get("DeviceRequests"),
                "tmpfs": h["Tmpfs"] == {"/tmp": TMPFS, "/output": TMPFS},
                "resources": h["Memory"] == 256 * 1024 * 1024 and h["MemorySwap"] == h["Memory"]
                             and h["PidsLimit"] == 32 and h["NanoCpus"] == 500_000_000
                             and h["ShmSize"] == 8 * 1024 * 1024,
                "logging": h["LogConfig"]["Type"] == "none"}
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError("Incomplete container security configuration") from exc
        failed = [name for name, valid in checks.items() if not valid]
        if failed:
            raise RuntimeError("Unsafe container configuration: " + ", ".join(failed))

    def run(self, customer, action):
        if customer not in CUSTOMERS or action not in {"analyze", "content", "technical", "probe"}:
            raise ValueError("Unknown customer or action")
        if customer not in self.images:
            raise IsolationUnavailable("Customer environment has not been prepared")
        try:
            container = self._create(customer, action)
            if not re.fullmatch(r"[a-f0-9]{64}", container):
                raise RuntimeError("Unexpected container identity")
            self.containers.add(container)
            self.creation_states["container"] = "identified"
            self.pending_creations.discard("container")
            config = json.loads(self._docker("inspect", container))[0]
            self._validate_config(config, customer, action)
            try:
                output = self._docker("start", "--attach", container)
            except TaskCancelled:
                self._stop_cancelled(container, customer, action)
                raise
            state = json.loads(self._docker("inspect", container))[0]["State"]
            if state["ExitCode"] != 0:
                raise RuntimeError("Isolated worker failed: " + output[:800])
            result = {"result": json.loads(output), "config": {
                "network": config["HostConfig"]["NetworkMode"],
                "readonly": config["HostConfig"]["ReadonlyRootfs"],
                "user": config["Config"]["User"], "mounts": config["Mounts"],
                "privileged": config["HostConfig"]["Privileged"],
                "pid_mode": config["HostConfig"]["PidMode"],
                "cap_drop": config["HostConfig"]["CapDrop"],
                "memory": config["HostConfig"]["Memory"],
                "pids_limit": config["HostConfig"]["PidsLimit"],
                "security_opt": config["HostConfig"]["SecurityOpt"]}}
        except BaseException as original:
            try:
                self._cleanup("container")
            except RuntimeError as cleanup:
                if current() is not None: current().failed()
                raise RuntimeError(f"Worker failed: {original}; cleanup failed: {cleanup}") from original
            raise
        try: self._cleanup("container")
        except BaseException:
            if current() is not None: current().failed()
            raise
        return result

    def _stop_cancelled(self, container, customer, action):
        control = current()
        if control is not None: control.stopping()
        try:
            def state_of(config):
                state = config['State']
                if (not isinstance(state, dict) or type(state.get('Running')) is not bool
                        or type(state.get('Restarting', False)) is not bool):
                    raise StopFailed('Invalid Docker stop state')
                return state['Running'] or state.get('Restarting', False)
            config = json.loads(self._docker('inspect', container))[0]
            self._validate_config(config, customer, action)
            if state_of(config):
                try: self._docker('stop', '--time', '2', container, timeout=5)
                except Exception: pass  # Inspect reality, not the CLI's exit code.
                if state_of(json.loads(self._docker('inspect', container))[0]):
                    try: self._docker('kill', container, timeout=5)
                    except Exception: pass
            if state_of(json.loads(self._docker('inspect', container))[0]):
                raise StopFailed('Container still running')
        except Exception:
            if control is not None: control.failed()
            raise StopFailed('Container stop could not be verified') from None

    def _cleanup(self, kind):
        # Discover by the exact per-session label, even after a lost create/import reply.
        for attempt in range(2):
            errors = []
            known = set(self.containers) if kind == "container" else set(self.images.values()) | self.orphan_images
            discovered = None
            try:
                if kind == "container":
                    found = self._docker("ps", "-a", "--no-trunc", "--filter", "label=" + self.label,
                                         "--format", "{{.ID}}")
                else:
                    found = self._docker("image", "ls", "--no-trunc", "--quiet", "--filter", "label=" + self.label)
                discovered = set(found.splitlines())
                if discovered - known:
                    self.pending_creations.discard(kind)
                known.update(discovered)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                errors.append("discovery: " + str(exc))
            for resource in sorted(known):
                try:
                    if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", resource):
                        raise RuntimeError("Invalid resource identity")
                    config = json.loads(self._docker("inspect", resource))[0]
                    key, value = self.label.split("=", 1)
                    if config["Config"]["Labels"].get(key) != value:
                        raise RuntimeError("Resource ownership mismatch; removal refused")
                    if kind == "container":
                        self._docker("rm", "--force", resource)
                        self.containers.discard(resource)
                    else:
                        self._docker("image", "rm", resource)
                        self.orphan_images.discard(resource)
                        self.images = {k: v for k, v in self.images.items() if v != resource}
                except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError, KeyError,
                        TypeError, IndexError, AttributeError) as exc:
                    if (discovered is not None and resource not in discovered
                            and isinstance(exc, RuntimeError)
                            and any(s in str(exc) for s in ("No such object", "No such container", "No such image"))):
                        # A removal may have succeeded even though its reply was lost.
                        self.containers.discard(resource)
                        self.orphan_images.discard(resource)
                        self.images = {k: v for k, v in self.images.items() if v != resource}
                        continue
                    errors.append(resource + ": " + str(exc))
                    if kind == "container":
                        self.containers.add(resource)
                    else:
                        self.orphan_images.add(resource)
            if kind in self.pending_creations:
                errors.append("Unconfirmed creation; a delayed resource may still appear")
            if not errors:
                return
        raise RuntimeError("Unresolved " + kind + " cleanup; label=" + self.label + "; " + "; ".join(errors))

    def close(self):
        errors = []
        for kind in ("container", "image"):
            try:
                self._cleanup(kind)
            except RuntimeError as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("Owned resources may remain: " + "; ".join(errors))

    def __exit__(self, exc_type, original, traceback):
        try:
            self.close()
        except RuntimeError as cleanup:
            if current() is not None: current().failed()
            raise RuntimeError(f"Original failure: {original}; cleanup failed: {cleanup}") from original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        with DockerLab() as lab:
            result = {customer: {action: lab.run(customer, action)
                                 for action in ("analyze", "probe")} for customer in CUSTOMERS}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (IsolationUnavailable, RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"isolation": "unavailable_or_failed", "error": str(exc)}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
