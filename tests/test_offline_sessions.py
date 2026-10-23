"""Offline session lifetime and durable policy integration; no network/login."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Event
import unittest
from unittest import mock

MODULES = Path(__file__).resolve().parents[1] / 'examples/offline-seo/isolation'
sys.path.insert(0, str(MODULES))
import access_control as a
from identity_store import PersistentAccessDirectory
import coordinator as c

A, B = c.CUSTOMERS


class TestOfflineSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'identity'
        self.directory = PersistentAccessDirectory(self.root)
        self.addCleanup(lambda: self.directory.close())
        for tenant in (A, B): self.directory.set_tenant(a.Tenant(tenant))
        for uid, tenant in (('alice', A), ('bob', B)):
            self.directory.set_user(a.User(uid, a.Identity('offline-fixture', uid)))
            self.directory.set_membership(a.Membership(uid, tenant, a.Role.TENANT_ADMIN))
        self.clock = mock.Mock(return_value=100.0)
        self.provider = a.OfflineIdentityProvider(self.directory, lifetime_seconds=10, clock=self.clock)
        self.token = self.provider.issue('alice')
        self.app = c.Coordinator(Path(self.tmp.name) / 'tasks')
        self.worker = mock.Mock()
        self.worker.execute.side_effect = lambda tenant: {'result': {'customer_id': tenant,
            'synthetic': True, 'connection': 'none', 'mode': 'proposal-only'}}
        self.app._specialists['seo.analyze'] = self.worker
        self.service = a.AccessService(self.provider, self.directory, self.app)

    def submit(self):
        return self.service.submit(self.token, A, 'task', 'seo.analyze')

    def test_absolute_expiry_does_not_slide_and_boundary_denies(self):
        for now in (100.0, 105.0, 109.999):
            self.clock.return_value = now
            self.assertEqual(self.provider.authenticate(self.token).user_id, 'alice')
        self.clock.return_value = 110.0
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)
        with self.assertRaises(a.AccessDenied): self.submit()
        self.worker.execute.assert_not_called()

    def test_revoking_one_session_preserves_other_session(self):
        second = self.provider.issue('alice')
        self.provider.revoke(self.token); self.provider.revoke(self.token)
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)
        self.assertEqual(self.provider.authenticate(second).user_id, 'alice')

    def test_account_wide_revocation_keeps_other_tenant_session(self):
        second = self.provider.issue('alice'); bob = self.provider.issue('bob')
        self.provider.revoke_all('alice')
        for token in (self.token, second):
            with self.assertRaises(a.AccessDenied): self.provider.authenticate(token)
        self.assertEqual(self.provider.authenticate(bob).user_id, 'bob')
        with self.assertRaises(a.AccessDenied): self.service.submit(bob, A, 'task')

    def test_expired_sessions_free_capacity_without_evicting_active_sessions(self):
        provider = a.OfflineIdentityProvider(self.directory, lifetime_seconds=10, max_sessions=1, clock=self.clock)
        old = provider.issue('alice')
        with self.assertRaises(a.AccessDenied): provider.issue('bob')
        self.assertEqual(provider.authenticate(old).user_id, 'alice')
        self.clock.return_value = 110.0
        fresh = provider.issue('bob')
        with self.assertRaises(a.AccessDenied): provider.authenticate(old)
        self.assertEqual(provider.authenticate(fresh).user_id, 'bob')

    def test_invalid_clock_permanently_closes_provider(self):
        for invalid in (99.0, -1, float('nan'), float('inf'), True, '110'):
            with self.subTest(invalid=invalid):
                clock = mock.Mock(return_value=100.0)
                provider = a.OfflineIdentityProvider(self.directory, clock=clock)
                token = provider.issue('alice'); clock.return_value = invalid
                with self.assertRaises(a.AccessDenied): provider.authenticate(token)
                clock.return_value = 101.0
                with self.assertRaises(a.AccessDenied): provider.issue('alice')

    def test_clock_exception_denies_without_error_details(self):
        self.clock.side_effect = RuntimeError('PRIVATE-CLOCK-DETAIL')
        with self.assertRaises(a.AccessDenied) as caught: self.provider.authenticate(self.token)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_policy_configuration_is_bounded(self):
        for kwargs in ({'lifetime_seconds':0}, {'lifetime_seconds':3601}, {'lifetime_seconds':True},
                       {'max_sessions':0}, {'max_sessions':10001}, {'clock':None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                a.OfflineIdentityProvider(self.directory, **kwargs)

    def test_closed_then_reopened_account_needs_fresh_session(self):
        identity = a.Identity('offline-fixture', 'alice')
        self.directory.set_user(a.User('alice', identity, active=False))
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)
        self.directory.set_user(a.User('alice', identity))
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)
        self.assertEqual(self.provider.authenticate(self.provider.issue('alice')).user_id, 'alice')

    def test_identity_reassignment_and_aba_never_restore_old_session(self):
        self.directory.set_user(a.User('alice', a.Identity('offline-fixture', 'moved')))
        self.directory.set_user(a.User('new-account', a.Identity('offline-fixture', 'alice')))
        self.directory.set_membership(a.Membership('new-account', B, a.Role.TENANT_ADMIN))
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)
        self.directory.set_user(a.User('new-account', a.Identity('offline-fixture', 'new-subject')))
        self.directory.set_user(a.User('alice', a.Identity('offline-fixture', 'alice')))
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)

    def test_live_role_and_membership_changes_are_not_session_claims(self):
        self.submit()
        self.directory.set_membership(a.Membership('alice', A, a.Role.READER))
        self.service.report(self.token, A, 'task')
        with self.assertRaises(a.AccessDenied): self.submit()
        self.directory.remove_membership('alice', A)
        with self.assertRaises(a.AccessDenied): self.service.report(self.token, A, 'task')
        self.assertEqual(self.provider.authenticate(self.token).user_id, 'alice')

    def test_restart_preserves_policy_but_not_sessions(self):
        old_provider = self.provider
        self.directory.close(); self.directory = PersistentAccessDirectory(self.root)
        fresh = a.OfflineIdentityProvider(self.directory, clock=self.clock)
        with self.assertRaises(a.AccessDenied): fresh.authenticate(self.token)
        with self.assertRaises(a.AccessDenied): old_provider.authenticate(self.token)
        self.assertEqual(fresh.authenticate(fresh.issue('alice')).user_id, 'alice')
        self.assertIn(('alice', A), self.directory._memberships)

    def test_commit_error_denies_even_direct_provider_authentication(self):
        with mock.patch.object(self.directory, '_commit', side_effect=sqlite3.OperationalError('PRIVATE-SQL')):
            with self.assertRaises(sqlite3.OperationalError): self.directory.remove_membership('alice', A)
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)
        with self.assertRaises(a.AccessDenied): self.provider.issue('alice')
        with self.assertRaises(a.AccessDenied): self.submit()

    def test_expiry_during_execution_blocks_response_without_rerunning_job(self):
        original = self.worker.execute.side_effect
        def execute(tenant):
            self.clock.return_value = 110.0
            return original(tenant)
        self.worker.execute.side_effect = execute
        with self.assertRaises(a.AccessDenied): self.submit()
        self.assertEqual(self.worker.execute.call_count, 1)
        fresh = self.provider.issue('alice')
        self.assertEqual(self.service.status(fresh, A, 'task')['status'], 'interrupted')

    def test_concurrent_revocation_blocks_result(self):
        entered, release = Event(), Event()
        original = self.worker.execute.side_effect
        def execute(tenant):
            entered.set()
            if not release.wait(5): raise RuntimeError('timeout')
            return original(tenant)
        self.worker.execute.side_effect = execute
        with ThreadPoolExecutor(max_workers=2) as pool:
            job = pool.submit(self.submit)
            try:
                self.assertTrue(entered.wait(5)); self.provider.revoke_all('alice')
            finally: release.set()
            with self.assertRaises(a.AccessDenied): job.result(timeout=5)

    def test_credentials_and_identity_are_absent_from_audit_log(self):
        self.submit(); self.provider.revoke(self.token)
        with self.assertRaises(a.AccessDenied): self.service.report(self.token, A, 'task')
        log = repr(tuple(self.directory._events))
        for secret in (repr(self.token), 'offline-fixture', 'alice', 'identity.sqlite3'):
            self.assertNotIn(secret, log)

    def test_untrusted_revoke_argument_cannot_execute_hash(self):
        class Evil:
            def __hash__(self): raise AssertionError('must not hash')
        with self.assertRaises(a.AccessDenied): self.provider.revoke(Evil())

    @unittest.skipUnless(hasattr(os, 'fork'), 'fork unavailable')
    def test_fork_rejects_new_session_operations(self):
        pid = os.fork()
        if pid == 0:
            try: self.provider.revoke_all('alice')
            except a.AccessDenied: os._exit(0)
            except Exception: os._exit(2)
            os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
