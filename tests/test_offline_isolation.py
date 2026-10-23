"""Real per-customer OS isolation tests, using local Docker only.

Eight integration checks share four disposable worker runs. No mock is used
to assert cross-customer file denial. Unsupported backends are explicit skips.
"""
import hashlib
import copy
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "offline_isolation_controller", REPO / "examples/offline-seo/isolation/runner.py")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class TestOfflineIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = controller.DockerLab()
        cls.before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in cls.lab.fixture_root.rglob("*.json")}
        try:
            cls.lab.__enter__()
        except controller.IsolationUnavailable as exc:
            raise unittest.SkipTest(str(exc)) from exc
        cls.addClassCleanup(cls.lab.close)
        # A parent-only marker must never enter a worker environment.
        with mock.patch.dict(os.environ, {"DMP_PARENT_SENTINEL": "fake-not-a-secret"}):
            cls.runs = {customer: {action: cls.lab.run(customer, action)
                                   for action in ("analyze", "probe")}
                        for customer in controller.CUSTOMERS}

    def test_each_customer_analysis_reuses_existing_seo(self):
        for customer, runs in self.runs.items():
            with self.subTest(customer=customer):
                result = runs["analyze"]["result"]
                self.assertEqual(result["customer_id"], customer)
                self.assertEqual(result["mode"], "proposal-only")
                self.assertEqual(result["connection"], "none")
                self.assertTrue(result["synthetic"])
                self.assertTrue(all(not p["applied"] for p in result["proposals"]))
                self.assertEqual(len(result["pages"]), 3 if customer == "demo-musteri" else 1)

    def test_foreign_customer_read_write_and_symlink_are_denied(self):
        for customer, runs in self.runs.items():
            probe = runs["probe"]["result"]
            self.assertEqual(probe["visible_customers"], [customer])
            for operation in ("foreign_read", "foreign_write", "foreign_symlink_read"):
                with self.subTest(customer=customer, operation=operation):
                    self.assertTrue(probe[operation]["denied"])
                    self.assertIn(probe[operation]["errno"], (2, 13, 30))

    def test_inputs_readonly_and_own_output_writable(self):
        for customer, runs in self.runs.items():
            probe = runs["probe"]["result"]
            self.assertTrue(probe["own_input_write"]["denied"])
            self.assertEqual(probe["own_output"], customer)
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.lab.fixture_root.rglob("*.json")}
        self.assertEqual(self.before, after)

    def test_host_files_and_docker_socket_are_not_exposed(self):
        for customer, runs in self.runs.items():
            with self.subTest(customer=customer):
                probe = runs["probe"]["result"]
                self.assertTrue(probe["host_fixture_read"]["denied"])
                self.assertTrue(probe["docker_socket"]["path_absent"])
                self.assertTrue(probe["docker_socket"]["connect_attempted"])
                self.assertTrue(probe["docker_socket"]["connect_denied"])
                self.assertFalse(any(m["Type"] == "bind" for m in runs["probe"]["config"]["mounts"]))

    def test_no_network_and_no_parent_credentials(self):
        for runs in self.runs.values():
            probe = runs["probe"]["result"]
            self.assertEqual(probe["network_interfaces"], ["lo"])
            self.assertEqual(runs["probe"]["config"]["network"], "none")
            self.assertNotIn("DMP_PARENT_SENTINEL", probe["environment_names"])
            self.assertLessEqual(set(probe["environment_names"]), {
                "HOME", "HOSTNAME", "PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "LC_CTYPE"})

    def test_worker_has_no_root_privileges_or_shared_host_pid_namespace(self):
        for runs in self.runs.values():
            probe = runs["probe"]["result"]
            config = runs["probe"]["config"]
            self.assertEqual((probe["uid"], probe["gid"]), (65534, 65534))
            self.assertEqual(int(probe["status"]["CapEff"], 16), 0)
            self.assertEqual(probe["status"]["NoNewPrivs"], "1")
            self.assertEqual(config["user"], "65534:65534")
            self.assertFalse(config["privileged"])
            self.assertNotEqual(config["pid_mode"], "host")
            self.assertIn("ALL", config["cap_drop"])
            self.assertTrue(config["readonly"])
            self.assertEqual(config["memory"], 256 * 1024 * 1024)
            self.assertEqual(config["pids_limit"], 32)

    def test_missing_backend_never_falls_back_to_unrestricted_analysis(self):
        lab = controller.DockerLab()
        with mock.patch.object(lab, "_docker", side_effect=RuntimeError("offline daemon")):
            with self.assertRaises(controller.IsolationUnavailable):
                lab.__enter__()
        self.assertEqual(lab.images, {})
        self.assertEqual(lab.containers, set())

    def test_unprepared_customer_traversal_and_unknown_actions_are_rejected(self):
        lab = controller.DockerLab()
        for customer, action in (("../diger-demo", "analyze"), ("demo-musteri", "shell")):
            with self.subTest(customer=customer, action=action), self.assertRaises(ValueError):
                lab.run(customer, action)
        with self.assertRaises(controller.IsolationUnavailable):
            lab.run("demo-musteri", "analyze")


def register_owned(lab, kind, resource, customer='demo-musteri'):
    """Test fixture: real SQLite provenance, synthetic Docker inspect response."""
    from resource_store import ResourceStore
    if not hasattr(lab, '_fixture_state'):
        lab._fixture_state = tempfile.TemporaryDirectory(prefix='dmp-owned-fixture-')
        unittest.addModuleCleanup(lab._fixture_state.cleanup)
        lab.resource_store = ResourceStore(Path(lab._fixture_state.name)/'resources')
    existing = next((r for r in lab.resource_store.records(customer) if r['resource']==resource),None)
    if existing is None:
        row=lab.resource_store.intent(customer,lab.resource_task,lab.resource_execution,kind)
        lab.resource_store.transition(row,'not_sent','sent_unconfirmed')
    else: row=existing
    lab._bindings[row['attempt']]=row
    key,value=lab.label.split('=',1)
    config={'Id':resource,'Config':{'Labels':{**lab.resource_store.labels(row),key:value}}}
    if kind=='container':config['HostConfig']={}
    if existing is None:lab.resource_store.verify(row,resource,config)
    return config


class FakeDocker:
    """Fault injection only. Actual cross-customer integration tests stay above."""
    def __init__(self, lab):
        self.lab = lab
        self.containers, self.images = set(), set()
        self.calls = []
        self.fail_remove = set()
        self.lost_remove_reply = set()
        self.foreign = set()

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "version":
            return "fake-local-version"
        if args[0] == "ps":
            return "\n".join(sorted(self.containers))
        if args[:2] == ("image", "ls"):
            return "\n".join(sorted(self.images))
        if args[0] == "inspect":
            resource = args[1]
            if resource not in self.images | self.containers:
                raise RuntimeError("No such object")
            config=register_owned(self.lab,"image" if resource.startswith("sha256:") else "container",resource)
            key,value=self.lab.label.split("=",1)
            if resource in self.foreign:config["Config"]["Labels"][key]="foreign"
            return json.dumps([config])
        if args[0] == "rm" or args[:2] == ("image", "rm"):
            resource = args[-1]
            if resource in self.fail_remove:
                raise subprocess.TimeoutExpired("fake-removal", 45)
            self.containers.discard(resource)
            self.images.discard(resource)
            if resource in self.lost_remove_reply:
                self.lost_remove_reply.remove(resource)
                raise subprocess.TimeoutExpired("fake-lost-reply", 45)
            return resource
        raise AssertionError("Unexpected fake operation: " + repr(args))


class TestIsolationSecurity(unittest.TestCase):
    """Always run: these security regressions do not require Docker."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dmp-security-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lab = controller.DockerLab()
        self.lab.fixture_root = self.root / "customers"
        shutil.copytree(REPO / "examples/offline-seo/customers", self.lab.fixture_root)

    def alter(self, filename, update):
        path = self.lab.fixture_root / "demo-musteri" / filename
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        update(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def fake_backend(self):
        backend = FakeDocker(self.lab)
        self.lab._docker = backend
        return backend

    def good_config(self):
        customer, action = "demo-musteri", "probe"
        self.lab.images[customer] = "sha256:" + "a" * 64
        key, value = self.lab.label.split("=", 1)
        return {"Image": self.lab.images[customer], "Mounts": [], "Config": {
            "Labels": {key: value}, "Cmd": self.lab._worker_command(customer, action),
            "Entrypoint": None, "User": "65534:65534", "Volumes": None,
            "Env": ["LANG=C.UTF-8", "HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"]},
            "HostConfig": {"NetworkMode": "none", "PortBindings": {}, "PublishAllPorts": False,
            "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"], "PidMode": "", "IpcMode": "private",
            "CgroupnsMode": "private", "UsernsMode": "", "Binds": [], "Mounts": [],
            "Devices": [], "DeviceRequests": [], "Tmpfs": {"/tmp": controller.TMPFS, "/output": controller.TMPFS},
            "Memory": 256 * 1024 * 1024, "MemorySwap": 256 * 1024 * 1024, "PidsLimit": 32,
            "NanoCpus": 500_000_000, "ShmSize": 8 * 1024 * 1024, "LogConfig": {"Type": "none"}}}

    def test_customer_symlinks_and_hardlinks_rejected_before_import(self):
        path = self.lab.fixture_root / "demo-musteri/profile.json"
        original = path.read_bytes()
        target = self.lab.fixture_root / "diger-demo/profile.json"
        for kind in ("symlink", "hardlink"):
            path.unlink()
            if kind == "symlink":
                path.symlink_to(target)
            else:
                os.link(target, path)
            with self.subTest(kind=kind), mock.patch.object(self.lab, "_docker") as docker:
                with self.assertRaises(controller.seo.SandboxError):
                    self.lab._prepare()
                docker.assert_not_called()
            path.unlink()
            path.write_bytes(original)
        # The same reader protects packaged project code, including parent dirs.
        (self.root / "code").mkdir()
        code = self.root / "code/worker.py"
        for kind in ("symlink", "hardlink"):
            if kind == "symlink":
                code.symlink_to(target)
            else:
                os.link(target, code)
            with self.subTest(source_kind=kind), self.assertRaises(controller.seo.SandboxError):
                controller.seo.read_bounded_file(self.root, ("code", "worker.py"))
            code.unlink()

    def test_customer_and_page_identity_and_synthetic_flags_checked_before_import(self):
        original = (self.lab.fixture_root / "demo-musteri/pages.json").read_bytes()
        mutations = [lambda d: d.update(customer_id="diger-demo"), lambda d: d.update(synthetic=False),
                     lambda d: d["pages"][0].update(customer_id="diger-demo")]
        for update in mutations:
            self.alter("pages.json", update)
            with mock.patch.object(self.lab, "_docker") as docker:
                with self.assertRaises(RuntimeError):
                    self.lab._prepare()
                docker.assert_not_called()
            (self.lab.fixture_root / "demo-musteri/pages.json").write_bytes(original)

    def test_packaged_bytes_are_validated_snapshot_not_reopened_paths(self):
        snapshots = self.lab._customer_snapshots()
        self.alter("profile.json", lambda d: d.update(customer_id="diger-demo"))
        items = {"data/customers/demo-musteri/" + name: (raw, 0o644)
                 for name, raw in snapshots["demo-musteri"].items()}
        bundle = self.root / "snapshot.tar"
        self.lab._write_bundle(bundle, items)
        with tarfile.open(bundle) as archive:
            self.assertTrue(all(info.isfile() for info in archive.getmembers()))
            for name, raw in snapshots["demo-musteri"].items():
                self.assertEqual(archive.extractfile("data/customers/demo-musteri/" + name).read(), raw)
            self.assertNotIn("diger-demo", "\n".join(archive.getnames()))

    def test_file_size_limit_precedes_runtime_collection_and_import(self):
        path = self.lab.fixture_root / "demo-musteri/pages.json"
        path.write_bytes(b" " * (controller.seo.MAX_FILE_BYTES + 1))
        with mock.patch.object(self.lab, "_runtime_files") as runtime, mock.patch.object(self.lab, "_docker") as docker:
            with self.assertRaises(controller.seo.SandboxError):
                self.lab._prepare()
            runtime.assert_not_called()
            docker.assert_not_called()

    def test_archive_budget_checked_before_file_creation_and_during_writes(self):
        bundle = self.root / "too-big.tar"
        with mock.patch.object(controller, "MAX_ARCHIVE", 10240):
            with self.assertRaises(RuntimeError):
                self.lab._write_bundle(bundle, {"data/page.json": (b"x", 0o644)})
        self.assertFalse(bundle.exists())
        stream = io.BytesIO()
        writer = controller.ArchiveLimitWriter(stream, 3)
        with self.assertRaises(RuntimeError):
            writer.write(b"1234")
        self.assertEqual(stream.getvalue(), b"")
        runtime = self.root / "runtime"
        runtime.write_bytes(b"1234")
        with mock.patch.object(controller, "MAX_RUNTIME_FILE", 3):
            with self.assertRaises(RuntimeError):
                controller.snapshot_runtime(runtime)

    def test_command_stdout_stderr_combined_budget_and_timeout(self):
        env = {"PATH": os.defpath}
        prefix = [sys.executable, "-I", "-S", "-B", "-c"]
        for code in ("import os; os.write(1,b'x'*2000)", "import os; os.write(2,b'x'*2000)",
                     "import os; os.write(1,b'x'*600); os.write(2,b'x'*600)"):
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, "output exceeds"):
                controller.bounded_command(prefix + [code], env, 5, limit=1000)
        with self.assertRaises(subprocess.TimeoutExpired):
            controller.bounded_command(prefix + ["import time; time.sleep(10)"], env, 0.1)
        result = controller.bounded_command(prefix + ["print('ok')"], env, 5, limit=1000)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_socket_check_distinguishes_missing_accessible_and_regular_paths(self):
        spec = importlib.util.spec_from_file_location("isolated_socket_probe", REPO / "examples/offline-seo/isolation/worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        path = self.root / "probe.sock"
        self.assertTrue(worker.socket_access(path)["path_absent"])
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            result = worker.socket_access(path)
            self.assertFalse(result["path_absent"])
            self.assertTrue(result["connect_attempted"])
            self.assertFalse(result["connect_denied"])
            accepted, _ = server.accept()
            accepted.close()
        path.unlink()
        path.write_text("not a socket", encoding="utf-8")
        self.assertFalse(worker.socket_access(path)["path_absent"])

    def test_cleanup_continues_after_timeout_and_reports_residual_id(self):
        backend = self.fake_backend()
        failed, healthy = "a" * 64, "b" * 64
        image = "sha256:" + "c" * 64
        backend.containers.update({failed, healthy})
        backend.images.add(image)
        backend.fail_remove.add(failed)
        self.lab.containers.update(backend.containers)
        self.lab.images["demo-musteri"] = image
        with self.assertRaises(RuntimeError) as error:
            self.lab.close()
        self.assertIn(failed, str(error.exception))
        self.assertIn(self.lab.label, str(error.exception))
        self.assertEqual(backend.containers, {failed})
        self.assertFalse(backend.images)

    def test_cleanup_reconciles_successful_delete_with_lost_reply(self):
        backend = self.fake_backend()
        resource = "a" * 64
        backend.containers.add(resource)
        backend.lost_remove_reply.add(resource)
        self.lab.containers.add(resource)
        self.lab.close()
        self.assertFalse(self.lab.containers)
        self.assertFalse(backend.containers)

    def test_cleanup_recovers_unknown_container_and_image_by_exact_label(self):
        backend = self.fake_backend()
        backend.containers.add("a" * 64)
        backend.images.add("sha256:" + "b" * 64)
        self.lab.pending_creations.update({"container", "image"})
        self.lab.close()
        self.assertFalse(backend.containers | backend.images)
        self.assertFalse(self.lab.pending_creations)

    def test_cleanup_reports_unconfirmed_creation_and_refuses_foreign_resource(self):
        backend = self.fake_backend()
        self.lab.pending_creations.add("image")
        resource = "a" * 64
        self.lab.containers.add(resource)
        backend.containers.add(resource)
        backend.foreign.add(resource)
        with self.assertRaises(RuntimeError) as error:
            self.lab.close()
        self.assertIn("ownership mismatch", str(error.exception))
        self.assertIn("Unconfirmed creation", str(error.exception))
        self.assertTrue(backend.containers)
        self.assertFalse(any(call[0] == "rm" for call in backend.calls))

    def test_lost_create_reply_is_cleaned_without_starting_worker(self):
        backend = self.fake_backend()
        self.lab.images["demo-musteri"] = "sha256:" + "a" * 64
        def lost(*args):
            backend.containers.add("b" * 64)
            raise subprocess.TimeoutExpired("fake-create", 45)
        with mock.patch.object(self.lab, "_create", side_effect=lost):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.lab.run("demo-musteri", "analyze")
        self.assertFalse(backend.containers)
        self.assertFalse(self.lab.pending_creations)
        self.assertFalse(any(call[0] == "start" for call in backend.calls))

    def test_lost_import_reply_is_cleaned_and_original_failure_preserved(self):
        backend = self.fake_backend()
        def operation(*args, **kwargs):
            if args[0] == "import":
                backend.images.add("sha256:" + "a" * 64)
                raise subprocess.TimeoutExpired("fake-import", 45)
            return backend(*args, **kwargs)
        self.lab._docker = operation
        with mock.patch.object(self.lab, "_runtime_files", return_value=set()):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.lab.__enter__()
        self.assertFalse(backend.images)
        self.assertFalse(self.lab.pending_creations)

    def test_security_policy_rejects_unsafe_and_missing_settings(self):
        safe = self.good_config()
        self.lab._validate_config(safe, "demo-musteri", "probe")
        mutations = [("HostConfig", "NetworkMode", "host"), ("HostConfig", "ReadonlyRootfs", False),
                     ("Config", "User", "0:0"), ("HostConfig", "Privileged", True),
                     ("HostConfig", "Binds", ["/:/host"]), ("HostConfig", "PidMode", "container:other"),
                     ("HostConfig", "CapAdd", ["SYS_ADMIN"]), ("HostConfig", "SecurityOpt", ["seccomp=unconfined"]),
                     ("HostConfig", "NanoCpus", 0), ("HostConfig", "PidsLimit", 0),
                     ("HostConfig", "Memory", 0), ("HostConfig", "Tmpfs", {}),
                     ("HostConfig", "LogConfig", {"Type": "json-file"}),
                     ("Config", "Env", ["HOME=/tmp", "API_TOKEN=synthetic-test-marker"])]
        for section, field, value in mutations:
            config = copy.deepcopy(safe)
            config[section][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.lab._validate_config(config, "demo-musteri", "probe")
        with self.assertRaises(RuntimeError):
            self.lab._validate_config({}, "demo-musteri", "probe")

    def test_unsafe_configuration_is_rejected_before_docker_start(self):
        config = self.good_config()
        config["HostConfig"]["NetworkMode"] = "host"
        with mock.patch.object(self.lab, "_create", return_value="b" * 64), \
                mock.patch.object(self.lab, "_docker", return_value=json.dumps([config])) as docker, \
                mock.patch.object(self.lab, "_cleanup") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "Unsafe container"):
                self.lab.run("demo-musteri", "probe")
            self.assertFalse(any(call.args[0] == "start" for call in docker.call_args_list))
            cleanup.assert_called_once_with("container")


if __name__ == "__main__":
    unittest.main()
