"""Durable fictional policy: atomicity, restart, isolation and safe failure."""
import os
import json
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
MODULES = REPO / 'examples/offline-seo/isolation'
sys.path.insert(0, str(MODULES))
import access_control as a
import identity_store as store
import coordinator as c
from runner import DockerLab, IsolationUnavailable

A, B = c.CUSTOMERS


class TestPersistentIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'identity'
        self.directory = store.PersistentAccessDirectory(self.root)
        self.addCleanup(lambda: self.directory.close())
        self.app = c.Coordinator(Path(self.tmp.name) / 'tasks')
        self.worker = mock.Mock()
        self.worker.execute.side_effect = lambda tenant: {'result': {'customer_id':tenant,
            'synthetic':True,'connection':'none','mode':'proposal-only'}}
        self.app._specialists['seo.analyze'] = self.worker
        self.directory.set_tenant(a.Tenant(A)); self.directory.set_tenant(a.Tenant(B))
        self.directory.set_user(a.User('manager',a.Identity('offline-fixture','manager')))
        self.directory.set_membership(a.Membership('manager',A,a.Role.TENANT_ADMIN))
        self.bind()

    def bind(self):
        self.provider = a.OfflineIdentityProvider(self.directory)
        self.service = a.AccessService(self.provider,self.directory,self.app)
        self.token = self.provider.issue('manager') if self.directory._users['manager'].active else None

    def reopen(self):
        self.directory.close(); self.directory = store.PersistentAccessDirectory(self.root); self.bind()

    def submit(self, tenant=A, task='task'):
        return self.service.submit(self.token,tenant,task,'seo.analyze')

    def test_restart_restores_policy_but_rejects_old_session(self):
        old = self.token; self.submit(); self.reopen()
        self.assertEqual(self.service.report(self.token,A,'task')['result']['customer_id'],A)
        with self.assertRaises(a.AccessDenied): self.service.status(old,A,'task')
        self.assertEqual(self.directory._memberships['manager',A].role,a.Role.TENANT_ADMIN)

    def test_membership_removal_is_durable(self):
        self.submit(); self.directory.remove_membership('manager',A); self.reopen()
        with self.assertRaises(a.AccessDenied): self.service.report(self.token,A,'task')
        with self.assertRaises(a.AccessDenied): self.submit()

    def test_role_downgrade_is_durable(self):
        self.submit(); self.directory.set_membership(a.Membership('manager',A,a.Role.READER)); self.reopen()
        self.service.report(self.token,A,'task')
        with self.assertRaises(a.AccessDenied): self.submit(task='new')

    def test_account_disable_is_durable_and_cannot_issue_session(self):
        old = self.token
        self.directory.set_user(a.User('manager',a.Identity('offline-fixture','manager'),active=False))
        self.reopen()
        with self.assertRaises(a.AccessDenied): self.provider.issue('manager')
        with self.assertRaises(a.AccessDenied): self.service.status(old,A,'task')

    def test_identity_reassignment_does_not_transfer_old_session(self):
        old = self.token
        self.directory.set_user(a.User('manager',a.Identity('offline-fixture','new-subject')))
        self.directory.set_user(a.User('replacement',a.Identity('offline-fixture','manager')))
        self.directory.set_membership(a.Membership('replacement',B,a.Role.TENANT_ADMIN))
        with self.assertRaises(a.AccessDenied): self.service.status(old,B,'task')
        generation = self.directory._generations['manager']
        self.reopen()
        self.assertEqual(self.directory._generations['manager'],generation)
        self.assertEqual(self.provider.authenticate(self.token).user_id,'replacement')

    def test_two_tenants_and_known_task_id_are_isolated(self):
        self.submit()
        for call in (self.service.status,self.service.report,self.service.submit):
            with self.assertRaises(a.AccessDenied): call(self.token,B,'task')
        self.directory.set_membership(a.Membership('manager',B,a.Role.READER)); self.reopen()
        with self.assertRaises(a.AccessDenied): self.submit(B)
        self.service.report(self.token,A,'task')

    def test_unknown_user_and_empty_restart_default_to_denied(self):
        with self.assertRaises(a.AccessDenied): self.provider.issue('unknown')
        empty = store.PersistentAccessDirectory(Path(self.tmp.name)/'empty')
        try:
            self.assertEqual(empty._users,{})
            with self.assertRaises(a.AccessDenied): a.OfflineIdentityProvider(empty).issue('admin')
        finally: empty.close()

    def test_second_owner_in_same_or_another_process_is_rejected(self):
        with self.assertRaises(BlockingIOError): store.PersistentAccessDirectory(self.root)
        script = "import sys;sys.path.insert(0,sys.argv[1]);from identity_store import PersistentAccessDirectory;PersistentAccessDirectory(sys.argv[2])"
        result = subprocess.run([sys.executable,'-B','-c',script,str(MODULES),str(self.root)],capture_output=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'BlockingIOError',result.stderr)

    def test_commit_failure_rolls_back_and_denies_until_reopen(self):
        with mock.patch.object(self.directory,'_commit',side_effect=sqlite3.OperationalError('injected failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.directory.set_membership(a.Membership('manager',B,a.Role.TENANT_ADMIN))
        with self.assertRaises(a.AccessDenied): self.submit()
        self.reopen()
        self.assertNotIn(('manager',B),self.directory._memberships)
        self.assertEqual(self.submit()['status'],'succeeded')

    def test_partial_save_rolls_back_without_partial_grant(self):
        save = self.directory._save
        def fail():
            save(); raise sqlite3.OperationalError('failure after SQL before commit')
        with mock.patch.object(self.directory,'_save',side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError): self.directory.remove_membership('manager',A)
        self.reopen()
        self.assertEqual(self.directory._memberships['manager',A].role,a.Role.TENANT_ADMIN)

    def test_invalid_policy_does_not_poison_valid_database(self):
        with self.assertRaises(ValueError): self.directory.set_membership(a.Membership('unknown',A,a.Role.READER))
        with self.assertRaises(ValueError): self.directory.set_user(a.User('duplicate',a.Identity('offline-fixture','manager')))
        self.assertEqual(self.submit()['status'],'succeeded'); self.reopen()
        self.assertNotIn('duplicate',self.directory._users)

    def test_schema_version_and_corruption_rejected_without_reset(self):
        self.directory.close(); path=self.root/'identity.sqlite3'
        with sqlite3.connect(path) as db: db.execute('PRAGMA user_version=99')
        before=path.read_bytes()
        with self.assertRaises(ValueError): store.PersistentAccessDirectory(self.root)
        self.assertEqual(before,path.read_bytes())
        path.write_bytes(b'corrupted database')
        with self.assertRaises(sqlite3.DatabaseError): store.PersistentAccessDirectory(self.root)
        self.assertEqual(path.read_bytes(),b'corrupted database')

    def test_schema_constraints_prevent_id_reuse_and_invalid_memberships(self):
        for sql,args in [("UPDATE users SET user_id='other' WHERE user_id='manager'",()),
                         ("DELETE FROM users WHERE user_id='manager'",()),
                         ('INSERT INTO memberships VALUES(?,?,?)',('unknown',A,'reader')),
                         ('INSERT INTO memberships VALUES(?,?,?)',('manager',B,'system_admin'))]:
            with self.assertRaises(sqlite3.IntegrityError): self.directory._db.execute(sql,args)
            self.directory._db.rollback()

    def test_unsafe_paths_links_and_database_budget_are_rejected(self):
        self.directory.close(); path=self.root/'identity.sqlite3'; saved=self.root/'saved'
        path.rename(saved); path.symlink_to(saved)
        with self.assertRaises(OSError): store.PersistentAccessDirectory(self.root)
        path.unlink(); os.link(saved,path)
        with self.assertRaises(PermissionError): store.PersistentAccessDirectory(self.root)
        path.unlink(); saved.rename(path)
        with mock.patch.object(store,'MAX_POLICY_BYTES',1):
            with self.assertRaises(PermissionError): store.PersistentAccessDirectory(self.root)
        path.chmod(0o644)
        with self.assertRaises(PermissionError): store.PersistentAccessDirectory(self.root)

    def test_sessions_and_security_events_are_not_persisted(self):
        secret='private-session-credential'
        with self.assertRaises(a.AccessDenied): self.service.status(secret,B,secret)
        self.directory.close()
        raw=(self.root/'identity.sqlite3').read_bytes()
        self.assertNotIn(secret.encode(),raw)
        self.directory=store.PersistentAccessDirectory(self.root)
        self.assertEqual(len(self.directory._events),0)
        with self.assertRaises(a.AccessDenied): self.provider.authenticate(self.token)

    @unittest.skipUnless(hasattr(os,'fork'),'fork is unavailable')
    def test_forked_policy_mutation_and_close_fail_before_lock(self):
        read,write=os.pipe();pid=os.fork()
        if pid==0:
            os.close(read)
            try:
                denied=0
                for call in (lambda:self.directory.remove_membership('manager',A),self.directory.close,
                             lambda:self.service.status(self.token,A,'task')):
                    try:call()
                    except a.AccessDenied:denied+=1
                os.write(write,str(denied).encode())
            finally:os._exit(0)
        os.close(write)
        try:
            ready,_,_=select.select([read],[],[],5);self.assertTrue(ready)
            self.assertEqual(os.read(read,100),b'3')
        finally:
            os.close(read)
            done,_=os.waitpid(pid,os.WNOHANG)
            if done==0:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
        self.assertEqual(self.submit()['status'],'succeeded')

    def test_all_four_operations_two_tenants_real_docker(self):
        try: DockerLab()._docker('version','--format','{{.Server.Version}}')
        except (IsolationUnavailable,OSError,RuntimeError) as exc:self.skipTest(str(exc))
        self.app._specialists['seo.analyze']=c.SeoSpecialist()
        self.directory.set_membership(a.Membership('manager',B,a.Role.TENANT_ADMIN));self.reopen()
        for tenant in (A,B):
            for operation in ('seo.analyze','seo.content','seo.technical','seo.audit'):
                task=operation.replace('.','-')
                self.assertEqual(self.service.submit(self.token,tenant,task,operation)['status'],'succeeded')
                self.assertEqual(self.service.report(self.token,tenant,task)['result']['customer_id'],tenant)

    def test_sigkill_mid_transaction_does_not_grant_admin(self):
        self.directory.close()
        script = '''
import sys
sys.path.insert(0,sys.argv[1])
from identity_store import PersistentAccessDirectory
owner=PersistentAccessDirectory(sys.argv[2])
owner._db.execute('BEGIN IMMEDIATE')
owner._db.execute('UPDATE users SET system_admin=1')
print('READY',flush=True)
sys.stdin.read()
'''
        child=subprocess.Popen([sys.executable,'-B','-c',script,str(MODULES),str(self.root)],
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            ready,_,_=select.select([child.stdout],[],[],10);self.assertTrue(ready)
            self.assertEqual(child.stdout.readline().strip(),'READY')
            child.kill();child.communicate(timeout=10)
            self.reopen()
            self.assertFalse(self.directory._users['manager'].system_admin)
            self.assertEqual(self.submit()['status'],'succeeded')
        finally:
            if child.poll() is None:child.kill();child.communicate(timeout=10)

    def test_threaded_policy_changes_remain_durable_and_consistent(self):
        from concurrent.futures import ThreadPoolExecutor
        def configure(i):
            uid='fictional-'+str(i)
            self.directory.set_user(a.User(uid,a.Identity('offline-fixture',uid)))
            self.directory.set_membership(a.Membership(uid,A if i%2 else B,a.Role.READER))
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(configure,range(12)))
        self.reopen()
        for i in range(12):
            uid='fictional-'+str(i)
            self.assertIn((uid,A if i%2 else B),self.directory._memberships)
            self.assertNotIn((uid,B if i%2 else A),self.directory._memberships)

    def test_existing_empty_or_modified_schema_is_never_adopted(self):
        self.directory.close();path=self.root/'identity.sqlite3';original=path.read_bytes()
        path.write_bytes(b'')
        with self.assertRaises(ValueError):store.PersistentAccessDirectory(self.root)
        self.assertEqual(path.read_bytes(),b'')
        path.write_bytes(original)
        with sqlite3.connect(path) as db:db.execute('DROP TRIGGER immutable_user_id')
        before=path.read_bytes()
        with self.assertRaises(ValueError):store.PersistentAccessDirectory(self.root)
        self.assertEqual(path.read_bytes(),before)

    def test_close_failure_still_disables_cached_access(self):
        with mock.patch.object(self.directory,'_dispose',side_effect=OSError('injected close failure')):
            with self.assertRaises(OSError):self.directory.close()
        with self.assertRaises(a.AccessDenied):self.submit()
        with self.assertRaises(a.AccessDenied):self.provider.issue('manager')

    def test_corrupt_unique_index_passes_quick_check_but_is_rejected(self):
        self.directory.set_user(a.User('useraaaa',a.Identity('offline-fixture','subject-a')))
        self.directory.set_user(a.User('userbbbb',a.Identity('offline-fixture','subject-b')))
        self.directory.set_membership(a.Membership('useraaaa',A,a.Role.READER))
        self.directory.set_membership(a.Membership('userbbbb',B,a.Role.TENANT_ADMIN))
        self.directory.close();path=self.root/'identity.sqlite3'
        raw=path.read_bytes();self.assertIn(b'userbbbb',raw)
        corrupted=raw.replace(b'userbbbb',b'useraaaa');path.write_bytes(corrupted)
        with sqlite3.connect(':memory:') as db:
            db.deserialize(corrupted)
            self.assertEqual(db.execute('PRAGMA quick_check').fetchall(),[('ok',)])
            self.assertNotEqual(db.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
        with self.assertRaises(ValueError):store.PersistentAccessDirectory(self.root)
        self.assertEqual(path.read_bytes(),corrupted)

    def test_duplicate_load_keys_rejected_before_any_cache_mutation(self):
        queries = {
            'SELECT user_id,issuer,subject,system_admin,active,generation FROM users':[
                ('uid-one','offline-fixture','one',0,1,1),('uid-two','offline-fixture','two',0,1,1)],
            'SELECT tenant_id,active FROM tenants':[(A,1),(B,1)],
            'SELECT user_id,tenant_id,role FROM memberships':[('uid-one',A,'reader')]}
        variations=[(0, [('same','offline-fixture','one',0,1,1),('same','offline-fixture','two',0,1,1)]),
                    (0, [('one','offline-fixture','same',0,1,1),('two','offline-fixture','same',0,1,1)]),
                    (1, [(A,1),(A,0)]),
                    (2, [('uid-one',A,'reader'),('uid-one',A,'tenant_admin')])]
        original=self.directory._db
        before=tuple(dict(getattr(self.directory,n)) for n in ('_users','_generations','_tenants','_memberships'))
        for which,rows in variations:
            fake=mock.Mock();current=dict(queries);current[list(queries)[which]]=rows
            fake.execute.side_effect=lambda sql:mock.Mock(fetchall=lambda:current[sql])
            self.directory._db=fake
            try:
                with self.assertRaises(ValueError):self.directory._load()
                self.assertEqual(before,tuple(dict(getattr(self.directory,n)) for n in ('_users','_generations','_tenants','_memberships')))
            finally:self.directory._db=original

    def test_deleted_lock_poisoned_owner_cannot_restore_removed_membership(self):
        (self.root/'identity.lock').unlink()
        second=store.PersistentAccessDirectory(self.root)
        try:
            second.remove_membership('manager',A)
            with self.assertRaises(a.AccessDenied):self.directory.set_tenant(a.Tenant(B,False))
            with self.assertRaises(a.AccessDenied):self.submit()
            self.assertEqual(second._db.execute('SELECT count(*) FROM memberships').fetchone()[0],0)
        finally:second.close()
        self.reopen()
        self.assertNotIn(('manager',A),self.directory._memberships)
        with self.assertRaises(a.AccessDenied):self.submit()

    def test_replaced_lock_and_database_paths_disable_access(self):
        lock=self.root/'identity.lock';lock.rename(self.root/'previous.lock')
        lock.touch(mode=0o600)
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen()
        path=self.root/'identity.sqlite3';replacement=self.root/'replacement.sqlite3'
        replacement.write_bytes(path.read_bytes());replacement.chmod(0o600);os.replace(replacement,path)
        with self.assertRaises(a.AccessDenied):self.provider.issue('manager')
        with self.assertRaises(a.AccessDenied):self.directory.set_tenant(a.Tenant(B,False))

    def test_directory_replacement_or_permission_change_disables_access(self):
        self.root.rename(self.root.with_name('moved-identity'));self.root.mkdir(mode=0o700)
        with self.assertRaises(a.AccessDenied):self.submit()
        self.directory.close()
        self.root.rmdir();self.root.with_name('moved-identity').rename(self.root);self.reopen()
        (self.root/'identity.sqlite3').chmod(0o644)
        with self.assertRaises(a.AccessDenied):self.provider.issue('manager')

    def test_lost_kernel_lock_with_competing_holder_is_rejected(self):
        import fcntl
        fcntl.flock(self.directory._lock_fd,fcntl.LOCK_UN)
        fd=os.open(self.root/'identity.lock',os.O_RDWR)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(a.AccessDenied):self.submit()
        finally:os.close(fd)

    def test_revision_change_between_guard_and_write_cannot_restore_cache(self):
        original=self.directory._begin_write
        def race():
            with sqlite3.connect(self.root/'identity.sqlite3') as db:
                db.execute('DELETE FROM memberships')
                db.execute('UPDATE metadata SET revision=revision+1')
            original()
        with mock.patch.object(self.directory,'_begin_write',side_effect=race),mock.patch.object(self.directory,'_save') as save:
            with self.assertRaises(ValueError):self.directory.set_tenant(a.Tenant(B,False))
            save.assert_not_called()
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen();self.assertNotIn(('manager',A),self.directory._memberships)

    def test_lock_replacement_between_guard_and_write_is_rejected(self):
        original=self.directory._begin_write
        def race():
            (self.root/'identity.lock').unlink();(self.root/'identity.lock').touch(mode=0o600)
            original()
        with mock.patch.object(self.directory,'_begin_write',side_effect=race),mock.patch.object(self.directory,'_save') as save:
            with self.assertRaises(PermissionError):self.directory.set_tenant(a.Tenant(B,False))
            save.assert_not_called()
        with self.assertRaises(a.AccessDenied):self.submit()

    def test_revision_is_monotonic_and_live_external_change_denies_reads(self):
        initial=self.directory._revision
        self.directory.set_tenant(a.Tenant(B,False));self.assertEqual(self.directory._revision,initial+1)
        self.directory.set_tenant(a.Tenant(B,True));self.assertEqual(self.directory._revision,initial+2)
        with sqlite3.connect(self.root/'identity.sqlite3') as db:db.execute('UPDATE metadata SET revision=revision+1')
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen();self.assertEqual(self.directory._revision,initial+3)

    def test_replace_conflicts_cannot_delete_or_reassign_accounts(self):
        self.assertEqual(self.directory._db.execute('PRAGMA recursive_triggers').fetchone()[0],1)
        attempts=[('manager','offline-fixture','replacement',1,1,1),
                  ('new-user','offline-fixture','manager',1,1,1)]
        for row in attempts:
            with self.assertRaises(sqlite3.IntegrityError):self.directory._db.execute('INSERT OR REPLACE INTO users VALUES(?,?,?,?,?,?)',row)
            self.directory._db.rollback()
        self.reopen();self.assertFalse(self.directory._users['manager'].system_admin)
        self.assertNotIn('new-user',self.directory._users)

    def test_committed_then_reported_error_denies_until_revalidated(self):
        original=self.directory._commit
        def ambiguous():original();raise OSError('commit outcome reported as unknown')
        with mock.patch.object(self.directory,'_commit',side_effect=ambiguous):
            with self.assertRaises(OSError):self.directory.remove_membership('manager',A)
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen();self.assertNotIn(('manager',A),self.directory._memberships)

    def test_rollback_failure_keeps_all_access_closed(self):
        with mock.patch.object(self.directory,'_save',side_effect=sqlite3.OperationalError('write error')),mock.patch.object(self.directory,'_rollback',side_effect=sqlite3.OperationalError('rollback error')):
            with self.assertRaises(sqlite3.OperationalError):self.directory.remove_membership('manager',A)
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen();self.assertIn(('manager',A),self.directory._memberships)

    def test_real_sqlite_full_is_atomic_and_poisoned(self):
        pages=self.directory._db.execute('PRAGMA page_count').fetchone()[0]
        self.directory._db.execute('PRAGMA max_page_count='+str(pages))
        failed=None
        for i in range(100):
            uid='capacity-'+str(i)
            try:self.directory.set_user(a.User(uid,a.Identity('x'*256,'subject-'+str(i))))
            except sqlite3.OperationalError as exc:
                self.assertEqual(exc.sqlite_errorcode,sqlite3.SQLITE_FULL);failed=uid;break
        self.assertIsNotNone(failed,'The page budget was not exhausted')
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen();self.assertNotIn(failed,self.directory._users)
        self.assertEqual(self.submit()['status'],'succeeded')

    def test_real_sqlite_busy_write_keeps_access_closed(self):
        other=sqlite3.connect(self.root/'identity.sqlite3')
        self.directory._db.execute('PRAGMA busy_timeout=20')
        try:
            other.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError) as caught:self.directory.remove_membership('manager',A)
            self.assertEqual(caught.exception.sqlite_errorcode,sqlite3.SQLITE_BUSY)
            with self.assertRaises(a.AccessDenied):self.submit()
        finally:other.rollback();other.close()
        self.reopen();self.assertIn(('manager',A),self.directory._memberships)

    def test_durable_identity_aba_and_reenabled_account_require_new_session(self):
        old=self.token
        self.directory.set_user(a.User('manager',a.Identity('offline-fixture','temporary')))
        self.directory.set_user(a.User('manager',a.Identity('offline-fixture','manager')))
        with self.assertRaises(a.AccessDenied):self.submit()
        generation=self.directory._generations['manager'];self.reopen()
        self.assertEqual(self.directory._generations['manager'],generation)
        self.assertEqual(self.submit()['status'],'succeeded')
        with self.assertRaises(a.AccessDenied):self.service.status(old,A,'task')
        self.directory.set_user(a.User('manager',a.Identity('offline-fixture','manager'),active=False))
        self.directory.set_user(a.User('manager',a.Identity('offline-fixture','manager')))
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen();self.assertEqual(self.submit()['status'],'succeeded')

    def test_lock_loss_after_sql_changes_is_detected_before_commit(self):
        original=self.directory._save
        def lose_lock():
            original()
            (self.root/'identity.lock').unlink()
            (self.root/'identity.lock').touch(mode=0o600)
        before=self.directory._revision
        with mock.patch.object(self.directory,'_save',side_effect=lose_lock):
            with self.assertRaises(PermissionError):self.directory.remove_membership('manager',A)
        with self.assertRaises(a.AccessDenied):self.submit()
        self.reopen()
        self.assertEqual(self.directory._revision,before)
        self.assertIn(('manager',A),self.directory._memberships)
