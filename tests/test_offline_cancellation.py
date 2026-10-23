"""Cancellation safety, publication races, local Docker and health-scope budgets."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import sqlite3
import tempfile
from threading import Event
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples/offline-seo/isolation'))
import access_control as a
import coordinator as c
import execution_control as x
from identity_store import PersistentAccessDirectory
from runner import DockerLab, IsolationUnavailable
A,B=c.CUSTOMERS


class TestCancellation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=PersistentAccessDirectory(Path(self.tmp.name)/'identity')
        self.addCleanup(self.directory.close)
        for tenant in (A,B): self.directory.set_tenant(a.Tenant(tenant))
        self.directory.set_user(a.User('u',a.Identity('offline-fixture','u')))
        for tenant in (A,B): self.directory.set_membership(a.Membership('u',tenant,a.Role.TENANT_ADMIN))
        self.clock=mock.Mock(return_value=100.)
        self.provider=a.OfflineIdentityProvider(self.directory,clock=self.clock,lifetime_seconds=10)
        self.token=self.provider.issue('u')
        self.directory.set_user(a.User('operator',a.Identity('offline-fixture','operator'),system_admin=True))
        self.directory.set_membership(a.Membership('operator',A,a.Role.READER))
        self.admin=self.provider.issue('operator')
        self.app=c.Coordinator(Path(self.tmp.name)/'tasks')
        self.service=a.AccessService(self.provider,self.directory,self.app)
        self.entered={t:Event() for t in (A,B)}
        self.release={t:Event() for t in (A,B)}
        def execute(tenant):
            self.entered[tenant].set()
            deadline=time.monotonic()+5
            while not self.release[tenant].wait(.01):
                x.checkpoint()
                if time.monotonic()>deadline: raise RuntimeError('Test timeout')
            x.checkpoint()
            return {'result':{'customer_id':tenant,'synthetic':True,'connection':'none','mode':'proposal-only'}}
        self.worker=mock.Mock();self.worker.execute.side_effect=execute
        self.app._specialists['seo.analyze']=self.worker

    def run_task(self,tenant=A,task='job',token=None):
        return self.service.submit(self.token if token is None else token,tenant,task,'seo.analyze')

    def record(self,tenant=A):
        return self.app.peek(c.TrustedCaller('operator',frozenset({tenant})),tenant,'job')

    def cancel_running(self,change):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.run_task)
            try:
                self.assertTrue(self.entered[A].wait(3));change()
                with self.assertRaises(a.AccessDenied):future.result(timeout=3)
            finally:self.release[A].set()
        row=self.record();self.assertEqual(row['status'],'interrupted');self.assertIsNone(row['result'])
        self.admin=self.provider.issue('operator')
        self.assertEqual(self.service.execution_states(self.admin,A)[-1]['state'],'stopped')

    def test_execution_diagnostics_require_admin_and_explicit_tenant_membership(self):
        for credential,tenant in ((self.token,A),(object(),A),(self.admin,B)):
            with self.assertRaises(a.AccessDenied):self.service.execution_states(credential,tenant)
        self.assertEqual(self.service.execution_states(self.admin,A),())

    def test_single_session_revocation_stops_cooperative_job(self):
        self.cancel_running(lambda:self.provider.revoke(self.token))

    def test_account_revocation_stops_cooperative_job(self):
        self.cancel_running(lambda:self.provider.revoke_all('u'))

    def test_membership_removal_does_not_wait_for_task(self):
        self.cancel_running(lambda:self.directory.remove_membership('u',A))

    def test_account_closure_stops_job(self):
        self.cancel_running(lambda:self.directory.set_user(a.User('u',a.Identity('offline-fixture','u'),active=False)))

    def test_role_downgrade_stops_write_authorized_analysis(self):
        self.cancel_running(lambda:self.directory.set_membership(a.Membership('u',A,a.Role.READER)))

    def test_expiry_during_job_stops_at_next_checkpoint(self):
        self.cancel_running(lambda:setattr(self.clock,'return_value',110.))

    def test_one_tenant_revocation_does_not_stop_other_tenant(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa=pool.submit(self.run_task,A);fb=pool.submit(self.run_task,B)
            try:
                self.assertTrue(self.entered[A].wait(3));self.assertTrue(self.entered[B].wait(3))
                self.directory.remove_membership('u',A)
                with self.assertRaises(a.AccessDenied):fa.result(timeout=3)
                self.release[B].set();self.assertEqual(fb.result(timeout=3)['status'],'succeeded')
            finally:
                self.release[A].set();self.release[B].set()
        self.assertIsNone(self.record(A)['result'])
        self.assertEqual(self.record(B)['result']['result']['customer_id'],B)

    def test_single_token_cancel_preserves_other_token_job(self):
        other=self.provider.issue('u')
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa=pool.submit(self.run_task,A,'job',self.token);fb=pool.submit(self.run_task,B,'job',other)
            try:
                self.assertTrue(self.entered[A].wait(3));self.assertTrue(self.entered[B].wait(3))
                self.provider.revoke(self.token)
                with self.assertRaises(a.AccessDenied):fa.result(timeout=3)
                self.release[B].set();self.assertEqual(fb.result(timeout=3)['status'],'succeeded')
            finally:
                self.release[A].set();self.release[B].set()

    def test_cancelling_duplicate_waiter_does_not_stop_execution_owner(self):
        owner=self.provider.issue('u')
        with ThreadPoolExecutor(max_workers=2) as pool:
            active=pool.submit(self.run_task,A,'job',owner)
            try:
                self.assertTrue(self.entered[A].wait(3))
                waiter=pool.submit(self.run_task,A,'job',self.token)
                deadline=time.monotonic()+3
                while len(self.service.execution_states(self.admin,A))<2 and time.monotonic()<deadline:
                    Event().wait(.01)
                self.provider.revoke(self.token)
                with self.assertRaises(a.AccessDenied):waiter.result(timeout=3)
                states=self.service.execution_states(self.admin,A)
                self.assertEqual(len({v['execution_id'] for v in states}),2)
                self.assertTrue(any(v['owns_execution'] and not v['finished'] for v in states))
                self.assertTrue(any(not v['owns_execution'] and v['state']=='stopped' for v in states))
                self.release[A].set();self.assertEqual(active.result(timeout=3)['status'],'succeeded')
            finally:self.release[A].set()
        self.assertEqual(self.worker.execute.call_count,1)

    def test_cancelled_id_is_not_automatically_reexecuted(self):
        self.cancel_running(lambda:self.provider.revoke(self.token))
        fresh=self.provider.issue('u');calls=self.worker.execute.call_count
        result=self.run_task(token=fresh)
        self.assertEqual(result['status'],'interrupted');self.assertEqual(calls,self.worker.execute.call_count)

    def test_health_checks_are_constant_at_thousand_sessions(self):
        self.provider=a.OfflineIdentityProvider(self.directory,clock=self.clock,max_sessions=1000)
        for _ in range(1000): self.provider.issue('u')
        with mock.patch.object(self.directory,'_verify_ownership',wraps=self.directory._verify_ownership) as health:
            with self.assertRaises(a.AccessDenied):self.provider.issue('u')
            self.assertLessEqual(health.call_count,4)
        self.release[A].set();self.run_task()
        with mock.patch.object(self.directory,'_verify_ownership',wraps=self.directory._verify_ownership) as health:
            self.service.status(self.token,A,'job');self.assertLessEqual(health.call_count,6)

    def test_checked_scope_does_not_cache_across_requests(self):
        with self.directory.checked():self.directory._user(self.provider.authenticate(self.token))
        with mock.patch.object(self.directory,'_verify_ownership',side_effect=PermissionError('lease lost')):
            with self.assertRaises(a.AccessDenied):self.provider.authenticate(self.token)
        with self.assertRaises(a.AccessDenied):self.provider.authenticate(self.token)

    def test_checked_scope_detects_health_loss_at_exit(self):
        with self.assertRaises(a.AccessDenied):
            with self.directory.checked():
                self.directory._db.execute('UPDATE metadata SET revision=revision+1')
        with self.assertRaises(a.AccessDenied):self.provider.authenticate(self.token)

    def test_real_sqlite_exclusive_lock_fails_closed(self):
        self.directory._db.execute('PRAGMA busy_timeout=50')
        blocker=sqlite3.connect(str(self.directory.root/'identity.sqlite3'))
        try:
            blocker.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(a.AccessDenied):self.service.status(self.token,A,'unknown')
        finally:
            blocker.rollback();blocker.close()
        with self.assertRaises(a.AccessDenied):self.provider.authenticate(self.token)

    def test_concurrent_reads_and_session_revocation_stress(self):
        self.release[A].set();self.run_task()
        start=Event()
        def reads():
            start.wait(2)
            for _ in range(20):
                try:self.service.status(self.token,A,'job')
                except a.AccessDenied:pass
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs=[pool.submit(reads) for _ in range(8)]
            start.set();self.provider.revoke(self.token)
            for job in jobs:job.result(timeout=5)
        for _ in range(20):
            with self.assertRaises(a.AccessDenied):self.service.report(self.token,A,'job')

    def test_forward_then_backward_clock_never_restores_session(self):
        self.clock.return_value=1000.
        with self.assertRaises(a.AccessDenied):self.provider.authenticate(self.token)
        self.clock.return_value=100.
        with self.assertRaises(a.AccessDenied):self.provider.issue('u')

    def test_sqlite_failure_during_execution_cannot_publish_result(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.run_task)
            try:
                self.assertTrue(self.entered[A].wait(3))
                with mock.patch.object(self.directory,'_verify_ownership',side_effect=PermissionError('lease lost')):
                    with self.assertRaises(a.AccessDenied):future.result(timeout=3)
            finally:self.release[A].set()
        self.assertIsNone(self.record()['result'])

    def test_cancel_and_completion_have_one_linearized_winner(self):
        for _ in range(100):
            control=x.ExecutionControl(A,'race','u',lambda:None)
            ready=Event()
            def publish():
                with control.bind():
                    ready.set()
                    try:
                        with x.publication():pass
                        return True
                    except x.TaskCancelled:return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                job=pool.submit(publish);self.assertTrue(ready.wait(2))
                outcome=control.request();completed=job.result(timeout=2)
            control.finish()
            if completed:self.assertEqual(outcome,'already_completed')
            else:self.assertEqual(control.state,'stopped')

    def test_no_false_stopped_before_noncooperative_adapter_returns(self):
        def execute(tenant):
            self.entered[tenant].set();self.release[tenant].wait(3)
            return {'result':{'customer_id':tenant,'synthetic':True,'connection':'none','mode':'proposal-only'}}
        self.worker.execute.side_effect=execute
        with ThreadPoolExecutor(max_workers=1) as pool:
            job=pool.submit(self.run_task)
            try:
                self.assertTrue(self.entered[A].wait(2));self.provider.revoke(self.token)
                self.assertEqual(self.service.execution_states(self.admin,A)[-1]['state'],'requested')
                self.assertFalse(self.service.execution_states(self.admin,A)[-1]['finished'])
            finally:self.release[A].set()
            with self.assertRaises(a.AccessDenied):job.result(timeout=3)
        self.assertEqual(self.record()['status'],'interrupted')

    def test_stop_failure_has_no_successful_durable_result(self):
        def execute(tenant):
            x.current().request();x.current().failed()
            raise x.StopFailed('synthetic stop failure')
        self.worker.execute.side_effect=execute
        self.assertEqual(self.run_task()['status'],'failed')
        self.assertEqual(self.service.execution_states(self.admin,A)[-1]['state'],'stop_failed')
        self.assertIsNone(self.record()['result'])

    def test_audit_cancellation_never_starts_next_expert(self):
        import hashlib
        from runner import seo
        sources=DockerLab()._customer_snapshots()[A]
        class Scope:
            customer_id=A
            def read(self,name):
                raw=sources[name]
                return json.loads(raw.decode('utf-8-sig')),hashlib.sha256(raw).hexdigest()
        adapters={op:mock.Mock() for op in ('seo.analyze','seo.content','seo.technical')}
        def first(*args):
            result=seo.analyze(Scope())
            self.provider.revoke(self.token)
            return {'result':result}
        adapters['seo.analyze'].execute_snapshot.side_effect=first
        self.app._specialists=adapters
        with self.assertRaises(a.AccessDenied):self.service.submit(self.token,A,'job','seo.audit')
        self.assertEqual(self.record()['status'],'interrupted')
        self.assertIsNone(self.record()['result'])
        adapters['seo.content'].execute_snapshot.assert_not_called()
        adapters['seo.technical'].execute_snapshot.assert_not_called()

    def test_critical_side_effect_gate_never_grants_permission(self):
        operation=x.CriticalOperation(A,'job','wordpress.update','0'*64,'1'*64,'fake-approval',1)
        with self.assertRaises(PermissionError):x.OfflineSideEffectGate().check(operation)

    @unittest.skipUnless(hasattr(os,'fork'),'fork unavailable')
    def test_fork_rejects_copied_checked_scope(self):
        with self.directory.checked():
            pid=os.fork()
            if pid==0:
                try:self.directory._check_process()
                except a.AccessDenied:os._exit(0)
                os._exit(1)
            _,status=os.waitpid(pid,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),0)


class TestDockerCancellation(unittest.TestCase):
    def test_stop_escalation_and_failure_are_verified(self):
        for states,fail in (([True,False,False],False),([True,True,False],False),([True,True,True],True)):
            lab=DockerLab();control=x.ExecutionControl(A,'stop','u',lambda:None)
            calls=[];iterator=iter(states)
            def docker(*args,**kwargs):
                calls.append(args[0])
                if args[0]=='inspect':return json.dumps([{'State':{'Running':next(iterator)}}])
                return ''
            with mock.patch.object(lab,'_docker',side_effect=docker),mock.patch.object(lab,'_validate_config'),control.bind():
                if fail:
                    with self.assertRaises(x.StopFailed):lab._stop_cancelled('a'*64,A,'analyze')
                    self.assertEqual(control.state,'stop_failed')
                else:
                    lab._stop_cancelled('a'*64,A,'analyze');self.assertEqual(control.state,'stopping')
                self.assertIn('stop',calls)
                self.assertEqual('kill' in calls,states[1])

    def test_real_docker_controlled_and_forced_stop_two_tenants(self):
        try:DockerLab()._docker('version','--format','{{.Server.Version}}')
        except (IsolationUnavailable,OSError,RuntimeError) as exc:self.skipTest(str(exc))
        with DockerLab() as lab:
            for customer,ignore in ((A,False),(B,True)):
                control=x.ExecutionControl(customer,'docker','u',lambda:None)
                code=('import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)' if ignore else 'import signal,sys,time; signal.signal(signal.SIGTERM, lambda *args: sys.exit(0)); time.sleep(30)')
                # Test-only worker substitution retains every container security setting.
                exit_codes=[]
                original_stop=lab._stop_cancelled
                def stop(container,tenant,action):
                    original_stop(container,tenant,action)
                    exit_codes.append(json.loads(lab._docker('inspect',container))[0]['State']['ExitCode'])
                with mock.patch.object(lab,'_worker_command',return_value=[str(lab.binary),'-I','-S','-B','-c',code]), mock.patch.object(lab,'_stop_cancelled',side_effect=stop):
                    def run():
                        with control.bind():
                            try:return lab.run(customer,'analyze')
                            finally:control.finish()
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        job=pool.submit(run)
                        try:
                            deadline=time.monotonic()+10
                            running=False
                            while time.monotonic()<deadline:
                                ids=list(lab.containers)
                                if ids:
                                    state=json.loads(lab._docker('inspect',ids[0]))[0]['State']
                                    if state['Running']:
                                        running=True;break
                                Event().wait(.02)
                            self.assertTrue(running)
                            # Ensure ignored SIGTERM has been installed before cancellation.
                            Event().wait(.15)
                            control.request()
                            with self.assertRaises(x.TaskCancelled):job.result(timeout=10)
                            self.assertEqual(control.state,'stopped')
                            self.assertFalse(lab.containers)
                            self.assertEqual(exit_codes,[137 if ignore else 0])
                        finally:
                            control.request()
