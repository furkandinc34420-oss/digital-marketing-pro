"""Offline identity/tenant authorization; no network or real accounts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import json
import select
import signal
import warnings
import sqlite3
import sys
import tempfile
from threading import Event
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples/offline-seo/isolation'))
import access_control as a
import coordinator as c

A, B = c.CUSTOMERS


class TestOfflineAccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app = c.Coordinator(Path(self.tmp.name) / 'state')
        self.adapter = mock.Mock()
        self.adapter.execute.side_effect = lambda tenant: {'result': {
            'customer_id': tenant, 'synthetic': True, 'mode': 'proposal-only', 'connection': 'none'}}
        self.app._specialists['seo.analyze'] = self.adapter
        self.directory = a.AccessDirectory(); self.provider = a.OfflineIdentityProvider(self.directory)
        self.service = a.AccessService(self.provider, self.directory, self.app)
        for tenant in (A, B): self.directory.set_tenant(a.Tenant(tenant))
        self.tokens = {}
        for name, system in [('admin', True), ('manager', False), ('reader', False), ('other', False)]:
            user = a.User(name, a.Identity('offline-fixture', name), system_admin=system)
            self.directory.set_user(user); self.tokens[name] = self.provider.issue(name)
        for name, tenant, role in [('manager', A, a.Role.TENANT_ADMIN), ('reader', A, a.Role.READER),
                                  ('other', B, a.Role.TENANT_ADMIN)]:
            self.directory.set_membership(a.Membership(name, tenant, role))

    def submit(self, user='manager', tenant=A, task='same'):
        return self.service.submit(self.tokens[user], tenant, task, 'seo.analyze')

    def test_authorized_manager_creates_reads_and_checks_status(self):
        self.assertEqual(self.submit()['status'], 'succeeded')
        self.assertEqual(self.service.status(self.tokens['manager'], A, 'same')['status'], 'succeeded')
        self.assertEqual(self.service.report(self.tokens['manager'], A, 'same')['result']['customer_id'], A)

    def test_reader_can_read_but_cannot_submit_even_cached_task(self):
        self.submit()
        self.assertEqual(self.service.status(self.tokens['reader'], A, 'same')['status'], 'succeeded')
        self.service.report(self.tokens['reader'], A, 'same')
        with self.assertRaises(a.AccessDenied): self.submit('reader')
        self.assertEqual(self.adapter.execute.call_count, 1)

    def test_known_foreign_task_id_never_grants_access(self):
        self.submit('other', B)
        for method in (self.service.status, self.service.report):
            with self.assertRaises(a.AccessDenied): method(self.tokens['manager'], B, 'same')
        with self.assertRaises(a.AccessDenied): self.submit('manager', B)

    def test_system_admin_requires_explicit_tenant_membership(self):
        with self.assertRaises(a.AccessDenied): self.submit('admin')
        self.directory.set_membership(a.Membership('admin', A, a.Role.READER))
        self.assertEqual(self.submit('admin')['status'], 'succeeded')
        with self.assertRaises(a.AccessDenied): self.submit('admin', B)

    def test_removed_membership_blocks_next_read_and_submit(self):
        self.submit(); self.directory.remove_membership('manager', A)
        for method in (self.service.status, self.service.report, self.service.submit):
            with self.assertRaises(a.AccessDenied): method(self.tokens['manager'], A, 'same')

    def test_role_downgrade_applies_without_new_session(self):
        self.submit(); self.directory.set_membership(a.Membership('manager', A, a.Role.READER))
        with self.assertRaises(a.AccessDenied): self.submit(task='new')
        self.service.report(self.tokens['manager'], A, 'same')

    def test_disabled_user_is_denied_immediately(self):
        self.submit()
        self.directory.set_user(a.User('manager', a.Identity('offline-fixture', 'manager'), active=False))
        with self.assertRaises(a.AccessDenied): self.service.report(self.tokens['manager'], A, 'same')

    def test_disabled_tenant_blocks_all_members(self):
        self.directory.set_tenant(a.Tenant(A, active=False))
        with self.assertRaises(a.AccessDenied): self.submit()

    def test_revoked_session_is_denied(self):
        self.provider.revoke(self.tokens['manager'])
        with self.assertRaises(a.AccessDenied): self.submit()

    def test_forged_identity_role_or_token_not_accepted(self):
        for credential in (object(), {'subject': 'admin', 'role': 'system_admin'},
                           a.Identity('offline-fixture', 'manager'), c.TrustedCaller('admin', frozenset({A})),
                           'admin', None):
            with self.assertRaises(a.AccessDenied): self.service.submit(credential, A, 'same')
        self.adapter.execute.assert_not_called()

    def test_token_from_other_provider_is_rejected(self):
        token = a.OfflineIdentityProvider(self.directory).issue('manager')
        with self.assertRaises(a.AccessDenied): self.service.status(token, A, 'same')

    def test_unknown_identity_cannot_self_register(self):
        with self.assertRaises(a.AccessDenied): self.provider.issue('unregistered')

    def test_multi_tenant_roles_are_independent(self):
        self.directory.set_membership(a.Membership('manager', B, a.Role.READER))
        self.submit(); self.submit('other', B)
        self.assertEqual(self.service.report(self.tokens['manager'], B, 'same')['result']['customer_id'], B)
        with self.assertRaises(a.AccessDenied): self.submit('manager', B, 'new')
        self.directory.remove_membership('manager', B)
        self.service.report(self.tokens['manager'], A, 'same')

    def test_unauthorized_request_never_touches_coordinator_or_filesystem(self):
        with mock.patch.object(self.app, 'peek') as get, mock.patch.object(self.app, 'run') as run:
            with self.assertRaises(a.AccessDenied): self.service.report(self.tokens['manager'], B, 'secret')
            with self.assertRaises(a.AccessDenied): self.submit('reader')
            get.assert_not_called(); run.assert_not_called()
        self.assertFalse(self.app.store.root.exists())

    def test_denial_messages_and_audit_do_not_echo_sensitive_request(self):
        secret = '/private/OTHER-TENANT-SECRET'
        with self.assertRaises(a.AccessDenied) as caught:
            self.service.report(self.tokens['manager'], secret, secret)
        self.assertNotIn(secret, str(caught.exception))
        events = self.service.audit_events(self.tokens['admin'])
        self.assertEqual(events[0].outcome, 'denied')
        self.assertNotIn(secret, repr(events))
        self.assertNotIn('manager', repr(events))

    def test_only_system_admin_reads_audit_log(self):
        for user in ('manager', 'reader', 'other'):
            with self.assertRaises(a.AccessDenied): self.service.audit_events(self.tokens[user])
        self.assertEqual(len(self.service.audit_events(self.tokens['admin'])), 4)

    def test_audit_log_is_bounded_and_snapshots_immutable(self):
        for _ in range(1100):
            with self.assertRaises(a.AccessDenied): self.service.status(object(), A, 'x')
        events = self.service.audit_events(self.tokens['admin'])
        self.assertEqual(len(events), 1000)
        with self.assertRaises(AttributeError): events[0].outcome = 'allowed'
        self.assertGreater(events[0].sequence, 1)

    def test_backend_exception_is_sanitized_and_recorded(self):
        with mock.patch.object(self.app, 'peek', side_effect=OSError('/private/SECRET')):
            with self.assertRaises(a.ServiceUnavailable) as caught:
                self.service.status(self.tokens['reader'], A, 'x')
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(self.service.audit_events(self.tokens['admin'])[0].outcome, 'failed')

    def test_concurrent_same_id_deduplicates_and_tenants_stay_separate(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.submit, 'manager' if i % 2 else 'other', A if i % 2 else B)
                       for i in range(12)]
            self.assertTrue(all(f.result()['status'] == 'succeeded' for f in futures))
        self.assertEqual(self.adapter.execute.call_count, 2)
        self.assertEqual(self.service.report(self.tokens['manager'], A, 'same')['result']['customer_id'], A)
        self.assertEqual(self.service.report(self.tokens['other'], B, 'same')['result']['customer_id'], B)

    def test_revocation_after_active_request_blocks_following_requests(self):
        entered, release = Event(), Event()
        original = self.adapter.execute.side_effect
        def blocked(tenant):
            entered.set()
            if not release.wait(10): raise RuntimeError('test timeout')
            return original(tenant)
        self.adapter.execute.side_effect = blocked
        with ThreadPoolExecutor(max_workers=2) as pool:
            active = pool.submit(self.submit)
            self.assertTrue(entered.wait(5))
            revoked = pool.submit(self.directory.remove_membership, 'manager', A)
            release.set(); self.assertEqual(active.result()['status'], 'succeeded'); revoked.result()
        with self.assertRaises(a.AccessDenied): self.service.report(self.tokens['manager'], A, 'same')

    def test_invalid_policy_configuration_is_rejected(self):
        with self.assertRaises(ValueError): self.directory.set_tenant(a.Tenant('real-site'))
        with self.assertRaises(ValueError): self.directory.set_membership(a.Membership('reader', A, a.Role.SYSTEM_ADMIN))
        with self.assertRaises(ValueError): self.directory.set_membership(a.Membership('unknown', A, a.Role.READER))
        with self.assertRaises(ValueError): self.directory.set_user(a.User('other-id', a.Identity('offline-fixture', 'manager')))

    def test_provider_failure_is_fail_closed(self):
        with mock.patch.object(self.provider, 'authenticate', side_effect=RuntimeError('SECRET')):
            with self.assertRaises(a.AccessDenied): self.submit()
        self.adapter.execute.assert_not_called()

    def test_real_docker_through_authorized_facade_for_two_tenants(self):
        from runner import DockerLab, IsolationUnavailable
        try:
            DockerLab()._docker('version', '--format', '{{.Server.Version}}')
        except (IsolationUnavailable, OSError, RuntimeError) as exc:
            self.skipTest(str(exc))
        self.app._specialists['seo.analyze'] = c.SeoSpecialist()
        for user, tenant in (('manager', A), ('other', B)):
            for operation in ('seo.analyze', 'seo.content', 'seo.technical', 'seo.audit'):
                result = self.service.submit(self.tokens[user], tenant, operation.replace('.', '-'), operation)
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(self.service.report(self.tokens[user], tenant, operation.replace('.', '-'))['result']['customer_id'], tenant)
        with self.assertRaises(a.AccessDenied): self.service.report(self.tokens['manager'], B, 'same')

    def test_identity_reassignment_never_transfers_old_session(self):
        old = self.tokens['manager']
        self.directory.set_user(a.User('manager', a.Identity('offline-fixture', 'manager-new')))
        self.directory.set_user(a.User('new-account', a.Identity('offline-fixture', 'manager')))
        self.directory.set_membership(a.Membership('new-account', B, a.Role.TENANT_ADMIN))
        for tenant in (A, B):
            with self.assertRaises(a.AccessDenied): self.service.status(old, tenant, 'same')
        fresh = self.provider.issue('manager')
        self.assertEqual(self.provider.authenticate(fresh).user_id, 'new-account')

    def test_identity_change_and_return_does_not_resurrect_session(self):
        self.directory.set_user(a.User('manager', a.Identity('offline-fixture', 'temporary')))
        self.directory.set_user(a.User('manager', a.Identity('offline-fixture', 'manager')))
        with self.assertRaises(a.AccessDenied): self.submit()
        self.tokens['manager'] = self.provider.issue('manager')
        self.assertEqual(self.submit()['status'], 'succeeded')

    def test_disable_and_reenable_requires_new_session(self):
        for active in (False, True):
            self.directory.set_user(a.User('manager', a.Identity('offline-fixture', 'manager'), active=active))
        with self.assertRaises(a.AccessDenied): self.submit()

    def test_fresh_registry_rejects_pre_restart_sessions(self):
        fresh = a.AccessDirectory()
        fresh.set_tenant(a.Tenant(A))
        fresh.set_user(a.User('manager', a.Identity('offline-fixture', 'manager')))
        fresh.set_membership(a.Membership('manager', A, a.Role.TENANT_ADMIN))
        service = a.AccessService(a.OfflineIdentityProvider(fresh), fresh, self.app)
        with self.assertRaises(a.AccessDenied): service.status(self.tokens['manager'], A, 'same')

    def snapshot(self):
        root = self.app.store.root
        if not root.exists(): return {}
        return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_ino,
                p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns,
                p.read_bytes() if p.is_file() else None)
                for p in [root, *root.rglob('*')]}

    def test_readonly_success_and_missing_task_leave_all_files_unchanged(self):
        self.submit(); before = self.snapshot()
        for method in (self.service.report, self.service.status):
            method(self.tokens['reader'], A, 'same')
            with self.assertRaises(a.ServiceUnavailable): method(self.tokens['reader'], A, 'missing')
        self.assertEqual(before, self.snapshot())

    def test_readonly_missing_state_creates_nothing(self):
        for method in (self.service.report, self.service.status):
            with self.assertRaises(a.ServiceUnavailable): method(self.tokens['reader'], A, 'missing')
        self.assertFalse(self.app.store.root.exists())

    def test_interrupted_audit_preview_does_not_persist_recovery(self):
        import audit_report
        hashes = {'profile.json': '0'*64, 'pages.json': '1'*64}
        experts = {op: {'status': 'pending', 'result': None, 'error': None} for op in audit_report.OPERATIONS}
        experts['seo.analyze']['status'] = 'running'
        report = audit_report.compose(A, hashes, {'1': 'https://demo.invalid/'}, experts, 'running')
        with self.app.store.session(A) as db:
            db.execute("INSERT INTO tasks(task_id,operation,caller,status,result,input_digest) VALUES(?,?,?,'running',?,?)",
                ('interrupted', 'seo.audit', 'local', json.dumps({'result': report}),
                 c.input_digest('seo.audit', hashes, manifest=True)))
            db.commit()
        before = self.snapshot()
        self.assertEqual(self.service.status(self.tokens['reader'], A, 'interrupted')['status'], 'interrupted')
        preview = self.service.report(self.tokens['reader'], A, 'interrupted')['result']
        self.assertFalse(preview['complete'])
        self.assertEqual(preview['experts']['seo.analyze']['status'], 'interrupted')
        self.assertEqual(before, self.snapshot())
        with sqlite3.connect(self.app.store.root / A / 'tasks.sqlite3') as db:
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0], 'running')
        for user in ('reader', 'manager', 'admin'):
            with self.assertRaises(a.AccessDenied): self.service.recover(self.tokens[user], A, 'interrupted')
        self.directory.set_membership(a.Membership('admin', A, a.Role.READER))
        self.assertEqual(self.service.recover(self.tokens['admin'], A, 'interrupted')['status'], 'interrupted')
        self.assertNotEqual(before, self.snapshot())

    def test_readonly_rejects_journal_and_unsafe_database_links(self):
        self.submit(); root = self.app.store.root / A
        journal = root / 'tasks.sqlite3-journal'
        journal.write_bytes(b'unfinished')
        before = self.snapshot()
        with self.assertRaises(a.ServiceUnavailable): self.service.report(self.tokens['reader'], A, 'same')
        self.assertEqual(before, self.snapshot())
        journal.unlink()
        database = root / 'tasks.sqlite3'; saved = root / 'saved.sqlite3'
        database.rename(saved); database.symlink_to(saved)
        with self.assertRaises(a.ServiceUnavailable): self.service.status(self.tokens['reader'], A, 'same')
        database.unlink(); os.link(saved, database)
        with self.assertRaises(a.ServiceUnavailable): self.service.status(self.tokens['reader'], A, 'same')

    def test_readonly_enforces_database_owner_and_byte_budget(self):
        self.submit(); before = self.snapshot()
        with mock.patch.object(c, 'MAX_DB_BYTES', 1):
            with self.assertRaises(a.ServiceUnavailable): self.service.report(self.tokens['reader'], A, 'same')
        self.assertEqual(before, self.snapshot())
        with self.app.store.session(A) as db:
            db.execute('UPDATE owner SET customer=?', (B,)); db.commit()
        before = self.snapshot()
        with self.assertRaises(a.ServiceUnavailable): self.service.report(self.tokens['reader'], A, 'same')
        self.assertEqual(before, self.snapshot())

    def test_session_revocation_racing_active_request_blocks_next_request(self):
        entered, release = Event(), Event(); original = self.adapter.execute.side_effect
        def blocked(tenant):
            entered.set()
            if not release.wait(10): raise RuntimeError('test timeout')
            return original(tenant)
        self.adapter.execute.side_effect = blocked
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.submit)
            try:
                self.assertTrue(entered.wait(5))
                self.provider.revoke(self.tokens['manager'])
            finally: release.set()
            self.assertEqual(running.result()['status'], 'succeeded')
        with self.assertRaises(a.AccessDenied): self.service.status(self.tokens['manager'], A, 'same')

    @unittest.skipUnless(hasattr(os, 'fork'), 'fork is not supported on this platform')
    def test_fork_rejects_auth_policy_audit_and_service_before_locks(self):
        held, release = Event(), Event()
        def lock_holder():
            with self.directory._lock, self.provider._lock:
                held.set(); release.wait(15)
        with ThreadPoolExecutor(max_workers=1) as pool:
            owner = pool.submit(lock_holder); self.assertTrue(held.wait(5))
            read, write = os.pipe()
            # Deliberate regression: inherited locks are held by another thread.
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', category=DeprecationWarning, message='.*multi-threaded.*')
                pid = os.fork()
            if pid == 0:
                os.close(read)
                try:
                    calls = [lambda: self.provider.authenticate(self.tokens['manager']),
                             lambda: self.provider.issue('manager'), lambda: self.provider.revoke(self.tokens['manager']),
                             lambda: self.directory.set_tenant(a.Tenant(A)),
                             lambda: self.directory.set_user(a.User('x', a.Identity('offline-fixture', 'x'))),
                             lambda: self.directory.set_membership(a.Membership('manager', A, a.Role.READER)),
                             lambda: self.directory.remove_membership('manager', A),
                             lambda: self.directory._event('x', 'x'),
                             lambda: self.service.audit_events(self.tokens['admin']),
                             lambda: self.service.status(self.tokens['manager'], A, 'same'),
                             lambda: self.service.recover(self.tokens['admin'], A, 'same')]
                    denied = 0
                    for call in calls:
                        try: call()
                        except a.AccessDenied: denied += 1
                    os.write(write, str(denied).encode())
                except BaseException:
                    os.write(write, b'error')
                finally:
                    os._exit(0)
            os.close(write); release.set(); owner.result()
            try:
                ready, _, _ = select.select([read], [], [], 5)
                self.assertTrue(ready, 'Fork child blocked on inherited lock')
                self.assertEqual(os.read(read, 100), b'11')
            finally:
                os.close(read)
                finished, _ = os.waitpid(pid, os.WNOHANG)
                if finished == 0:
                    os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
