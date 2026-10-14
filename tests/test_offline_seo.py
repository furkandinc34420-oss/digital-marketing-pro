"""Offline SEO proposals and customer boundary regression tests; no network."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import builtins
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from _helpers import PLUGIN_ROOT, import_script, run_script

seo = import_script("offline_seo.py")
FIXTURES = PLUGIN_ROOT / "examples" / "offline-seo" / "customers"


class TestOfflineSEO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "customers"
        shutil.copytree(FIXTURES, self.root)
        self.scope = seo.TenantScope(self.root, "demo-musteri")

    def change(self, filename, transform):
        path = self.root / "demo-musteri" / filename
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        transform(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_findings_and_proposals_without_network_or_input_changes(self):
        sentinel = Path(self.tmp.name) / "outside.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        (self.root / "demo-musteri" / "notes.txt").write_text("unchanged", encoding="utf-8")
        def snapshot():
            return {str(p.relative_to(self.tmp.name)): p.read_bytes() if p.is_file() else None
                    for p in Path(self.tmp.name).rglob("*")}
        before = snapshot()
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")):
            result = seo.analyze(self.scope)
        self.assertEqual(result["customer_name"], "Demo Müşteri")
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["connection"], "none")
        self.assertEqual(result["pages"][0]["meta"]["title_status"], "too_short")
        self.assertEqual(result["pages"][0]["meta"]["description_status"], "missing")
        self.assertEqual(result["pages"][1]["issues"], [])
        self.assertTrue(result["pages"][2]["meta"]["noindex"])
        self.assertEqual(len(result["proposals"]), 4)
        self.assertTrue(all(p["human_approval_required"] and not p["applied"]
                            for p in result["proposals"]))
        self.assertEqual(result["proposals"][0]["expected_before"], "Demo")
        self.assertEqual(result["source_sha256"], hashlib.sha256(
            before["customers/demo-musteri/pages.json"]).hexdigest())
        self.assertEqual(before, snapshot())
        real_open, real_io_open, real_builtin_open = os.open, io.open, builtins.open
        forbidden = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        def readonly_os_open(path, flags, *args, **kwargs):
            if flags & forbidden:
                raise AssertionError("write open forbidden")
            return real_open(path, flags, *args, **kwargs)
        def readonly_text_open(original):
            def guarded(path, mode="r", *args, **kwargs):
                if any(flag in mode for flag in "wax+"):
                    raise AssertionError("write open forbidden")
                return original(path, mode, *args, **kwargs)
            return guarded
        with mock.patch.object(seo, "open_descriptor", side_effect=readonly_os_open) as opened, \
                mock.patch.object(io, "open", side_effect=readonly_text_open(real_io_open)), \
                mock.patch.object(builtins, "open", side_effect=readonly_text_open(real_builtin_open)):
            with self.assertRaises(AssertionError):
                sentinel.write_text("must be blocked", encoding="utf-8")
            seo.analyze(self.scope)
        self.assertTrue(all(not call.args[1] & forbidden for call in opened.call_args_list))
        self.assertEqual(before, snapshot())

    def test_customers_with_same_page_id_remain_separate(self):
        demo = seo.analyze(self.scope)
        other = seo.analyze(seo.TenantScope(self.root, "diger-demo"))
        self.assertEqual(demo["pages"][0]["page_id"], other["pages"][0]["page_id"])
        self.assertNotIn("other.invalid", json.dumps(demo))
        self.assertNotIn("demo.invalid", json.dumps(other))
        self.assertTrue(all(p["customer_id"] == "demo-musteri" for p in demo["proposals"]))

    def test_rejects_traversal_and_ambiguous_customer_ids(self):
        for customer in ("../diger-demo", "demo-musteri/../diger-demo", "", "/tmp", "Demo Müşteri"):
            with self.subTest(customer=customer), self.assertRaises(seo.SandboxError):
                seo.TenantScope(self.root, customer)

    def test_bound_scope_cannot_switch_customer(self):
        with self.assertRaises(FrozenInstanceError):
            self.scope.customer_id = "diger-demo"
        with self.assertRaises(seo.SandboxError):
            self.scope.read("../diger-demo/pages.json")

    def test_rejects_cross_customer_payloads(self):
        for filename in ("profile.json", "pages.json"):
            with self.subTest(filename=filename):
                original = (self.root / "demo-musteri" / filename).read_bytes()
                self.change(filename, lambda d: d.update(customer_id="diger-demo"))
                with self.assertRaises(seo.SandboxError):
                    seo.analyze(self.scope)
                (self.root / "demo-musteri" / filename).write_bytes(original)

    def test_rejects_cross_customer_page(self):
        self.change("pages.json", lambda d: d["pages"][0].update(customer_id="diger-demo"))
        with self.assertRaises(seo.SandboxError):
            seo.analyze(self.scope)

    def test_rejects_cross_site_and_real_urls(self):
        for url in ("https://other.invalid/", "https://example.com/", "file:///tmp/data",
                    "https://user:secret@demo.invalid/", "https://demo.invalid:443/"):
            with self.subTest(url=url):
                self.change("pages.json", lambda d: d["pages"][0].update(url=url))
                with self.assertRaises(seo.SandboxError):
                    seo.analyze(self.scope)
        for url in ("https://demo.invalid:0/", "https://@demo.invalid/",
                    "https://demo.invalid:/", "https://demo.invalid:65536/",
                    "https://demo.invalid:-1/", "https://demo.invalid:abc/",
                    "https://:pass@demo.invalid/", "https://demo.invalid/\n",
                    "https://bad_name.invalid/", "https://-bad.invalid/"):
            with self.subTest(url=url), self.assertRaises(seo.SandboxError):
                seo._fixture_url(url)

    def test_rejects_non_synthetic_data(self):
        self.change("pages.json", lambda d: d.update(synthetic=False))
        with self.assertRaises(seo.SandboxError):
            seo.analyze(self.scope)

    def test_input_limits_read_mutation_and_duplicate_ids(self):
        self.change("pages.json", lambda d: d["pages"][1].update(id=1))
        with self.assertRaises(seo.SandboxError):
            seo.analyze(self.scope)
        for count in (0, seo.MAX_PAGES + 1):
            self.change("pages.json", lambda d: d.update(pages=[{}] * count))
            with self.subTest(count=count), self.assertRaises(seo.SandboxError):
                seo.analyze(self.scope)
        shutil.copyfile(FIXTURES / "demo-musteri" / "pages.json", self.root / "demo-musteri" / "pages.json")
        self.change("pages.json", lambda d: d["pages"][0].update(html="x" * seo.MAX_HTML_CHARS))
        self.assertEqual(len(seo.analyze(self.scope)["pages"]), 3)
        self.change("pages.json", lambda d: d["pages"][0].update(html="x" * (seo.MAX_HTML_CHARS + 1)))
        with self.assertRaises(seo.SandboxError):
            seo.analyze(self.scope)
        path = self.root / "demo-musteri" / "pages.json"
        path.write_bytes(b" " * (seo.MAX_FILE_BYTES + 1))
        with mock.patch.object(os, "read", side_effect=AssertionError("oversized file read")):
            with self.assertRaises(seo.SandboxError):
                self.scope.read("pages.json")
        shutil.copyfile(FIXTURES / "demo-musteri" / "pages.json", path)
        read = os.read
        changed = False
        def change_during_read(fd, size):
            nonlocal changed
            chunk = read(fd, size)
            if not changed:
                changed = True
                with path.open("ab") as stream:
                    stream.write(b" ")
            return chunk
        with mock.patch.object(os, "read", side_effect=change_during_read):
            with self.assertRaises(seo.SandboxError):
                self.scope.read("pages.json")

    def test_symlinks_hardlinks_and_file_replacement_race(self):
        path = self.root / "demo-musteri" / "pages.json"
        path.unlink()
        path.symlink_to(self.root / "diger-demo" / "pages.json")
        with self.assertRaises(seo.SandboxError):
            seo.analyze(self.scope)
        path.unlink()
        shutil.copyfile(FIXTURES / "demo-musteri" / "pages.json", path)
        os.link(path, self.root / "diger-demo" / "shared.json")
        with self.assertRaises(seo.SandboxError):
            seo.analyze(self.scope)
        (self.root / "diger-demo" / "shared.json").unlink()
        read = os.read
        replaced = False
        def swap_after_open(fd, size):
            nonlocal replaced
            if not replaced:
                replaced = True
                path.unlink()
                path.symlink_to(self.root / "diger-demo" / "pages.json")
            return read(fd, size)
        with mock.patch.object(os, "read", side_effect=swap_after_open):
            with self.assertRaises(seo.SandboxError):
                self.scope.read("pages.json")
        shutil.rmtree(self.root / "demo-musteri")
        (self.root / "demo-musteri").symlink_to(self.root / "diger-demo", target_is_directory=True)
        with self.assertRaises(seo.SandboxError):
            seo.analyze(self.scope)

    def test_no_ambient_customer_selection(self):
        with mock.patch.dict("os.environ", {"CLAUDE_MARKETING_HOME": str(self.root / "diger-demo")}):
            self.assertEqual(seo.analyze(self.scope)["customer_id"], "demo-musteri")

    def test_repeatable_and_source_bound(self):
        result = seo.analyze(self.scope)
        self.assertEqual(result, seo.analyze(self.scope))
        self.change("pages.json", lambda d: d["pages"][0].update(html="<title>Changed</title>"))
        self.assertNotEqual(result["source_sha256"], seo.analyze(self.scope)["source_sha256"])
        for filename in ("pages.json", "profile.json"):
            path = self.root / "demo-musteri" / filename
            raw = path.read_bytes()
            previous = seo.analyze(self.scope)["source_hashes"][filename]
            altered = b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")
            path.write_bytes(altered)
            updated = seo.analyze(self.scope)
            self.assertEqual(updated["source_hashes"][filename], hashlib.sha256(altered).hexdigest())
            self.assertNotEqual(previous, updated["source_hashes"][filename])
            self.assertTrue(all(p["source_hashes"] == updated["source_hashes"]
                                for p in updated["proposals"]))

    def test_cli_readonly_mode_and_network_guard(self):
        ok = run_script("offline_seo.py", "--root", self.root, "--customer", "demo-musteri")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["mode"], "proposal-only")
        bad = run_script("offline_seo.py", "--root", self.root, "--customer", "../diger-demo")
        self.assertEqual(bad.returncode, 1)
        apply = run_script("offline_seo.py", "--root", self.root, "--customer", "demo-musteri", "--apply")
        self.assertEqual(apply.returncode, 2)
        # Actual audit-hook checks in a fresh child; denials precede DNS/send.
        import subprocess
        import sys
        guard = PLUGIN_ROOT / "tests" / "offline_guard"
        code = '''
import socket, sitecustomize
for action in (lambda: socket.getaddrinfo("blocked.invalid", 443),
               lambda: socket.socket().connect(("192.0.2.1", 80)),
               lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.1", 9))):
    try:
        action()
    except (socket.gaierror, ConnectionRefusedError):
        pass
    else:
        raise AssertionError("external operation allowed")
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(b"local", receiver.getsockname())
    assert receiver.recv(10) == b"local"
with socket.socket() as server:
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with socket.socket() as client:
        client.connect(server.getsockname())
        connection, _ = server.accept()
        connection.close()
'''
        child = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                               text=True, env=dict(os.environ, PYTHONPATH=str(guard)), timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)


if __name__ == "__main__":
    unittest.main()
