"""Dispatch ordering and resource lifecycle faults. No external API or network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples/offline-seo/isolation'))
import runner
import execution_control as x
import access_control as a
import test_offline_cancellation as cancellation_tests
import test_offline_isolation as isolation_tests
A,B=runner.CUSTOMERS


class TestDispatchSafety(unittest.TestCase):
    def test_create_cancel_before_dispatch_has_no_pending_resource(self):
        lab=runner.DockerLab();lab.images[A]='sha256:'+'a'*64
        control=x.ExecutionControl(A,'create','u',lambda:None);control.request()
        with control.bind(),mock.patch.object(runner.subprocess,'Popen') as spawn, mock.patch.object(lab,'_cleanup') as cleanup:
            with self.assertRaises(x.TaskCancelled):lab.run(A,'analyze')
            spawn.assert_not_called();cleanup.assert_called_once_with('container')
        control.finish()
        self.assertEqual(control.state,'stopped')
        self.assertFalse(lab.pending_creations)
        self.assertEqual(lab.creation_states['container'],'not_sent')

    def test_import_cancel_before_dispatch_has_no_pending_resource(self):
        lab=runner.DockerLab();control=x.ExecutionControl(A,'import','u',lambda:None);control.request()
        with control.bind(),mock.patch.object(runner.subprocess,'Popen') as spawn:
            with self.assertRaises(x.TaskCancelled):lab._docker('import','unused-test-archive')
            spawn.assert_not_called()
        self.assertEqual(lab.creation_states['image'],'not_sent')
        self.assertFalse(lab.pending_creations)

    def test_cancellation_between_authorization_and_spawn_prevents_real_spawn(self):
        entered,release=Event(),Event()
        def authorize():
            entered.set()
            if not release.wait(3):raise RuntimeError('test timeout')
        control=x.ExecutionControl(A,'cancel-first','u',authorize)
        original=subprocess.Popen
        with mock.patch.object(runner.subprocess,'Popen',wraps=original) as spawn:
            with ThreadPoolExecutor(max_workers=1) as pool:
                def run():
                    with control.bind():
                        return runner.bounded_command([sys.executable,'-B','-c','pass'],{'PATH':os.defpath},5,dispatch=control.dispatch)
                future=pool.submit(run)
                try:
                    self.assertTrue(entered.wait(3));self.assertEqual(control.request(),'requested')
                finally:release.set()
                with self.assertRaises(x.TaskCancelled):future.result(timeout=3)
            spawn.assert_not_called()

    def test_spawn_wins_and_cancellation_does_not_wait_for_command(self):
        entered=Event();processes=[];original=subprocess.Popen
        control=x.ExecutionControl(A,'spawn-first','u',lambda:None)
        def spawn(*args,**kwargs):
            process=original(*args,**kwargs);processes.append(process);entered.set();return process
        def run():
            with control.bind():
                return runner.bounded_command([sys.executable,'-B','-c','import time;time.sleep(30)'],
                    {'PATH':os.defpath},35,dispatch=control.dispatch,checkpoint=control.checkpoint)
        with mock.patch.object(runner.subprocess,'Popen',side_effect=spawn),ThreadPoolExecutor(max_workers=2) as pool:
            job=pool.submit(run)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(pool.submit(control.request).result(timeout=2),'requested')
                with self.assertRaises(x.TaskCancelled):job.result(timeout=3)
            finally:control.request()
        self.assertEqual(len(processes),1)
        self.assertIsNotNone(processes[0].poll())

    def test_real_spawn_races_are_linearized(self):
        original=subprocess.Popen
        for _ in range(30):
            control=x.ExecutionControl(A,'race','u',lambda:None);events=[]
            def spawn(*args,**kwargs):
                events.append('spawn');return original(*args,**kwargs)
            def run():
                with control.bind():
                    try:runner.bounded_command([sys.executable,'-B','-c','pass'],{'PATH':os.defpath},5,dispatch=control.dispatch,checkpoint=control.checkpoint)
                    except x.TaskCancelled:pass
            def cancel():control.request();events.append('cancel')
            with mock.patch.object(runner.subprocess,'Popen',side_effect=spawn),ThreadPoolExecutor(max_workers=2) as pool:
                job=pool.submit(run);revocation=pool.submit(cancel)
                job.result(timeout=5);revocation.result(timeout=5)
            if 'spawn' in events:self.assertLess(events.index('spawn'),events.index('cancel'))

    def test_dispatched_timeout_remains_unconfirmed_for_create_and_import(self):
        original=subprocess.Popen
        for operation,kind in (('create','container'),('import','image')):
            with self.subTest(operation=operation):
                lab=runner.DockerLab()
                def spawn(command,**kwargs):
                    return original([sys.executable,'-B','-c','import time;time.sleep(30)'],**kwargs)
                with mock.patch.object(runner.subprocess,'Popen',side_effect=spawn):
                    with self.assertRaises(subprocess.TimeoutExpired):lab._docker(operation,'synthetic',timeout=.03)
                self.assertEqual(lab.creation_states[kind],'sent_unconfirmed')
                self.assertIn(kind,lab.pending_creations)
                with mock.patch.object(lab,'_docker',return_value=''):
                    with self.assertRaisesRegex(RuntimeError,'Unconfirmed creation'):lab._cleanup(kind)

    def test_definite_popen_failure_stays_not_sent(self):
        lab=runner.DockerLab()
        with mock.patch.object(runner.subprocess,'Popen',side_effect=FileNotFoundError('synthetic')):
            with self.assertRaises(FileNotFoundError):lab._docker('create','synthetic')
        self.assertFalse(lab.pending_creations)
        self.assertEqual(lab.creation_states['container'],'not_sent')

    def test_confirmed_container_identity_is_recorded(self):
        fixture=isolation_tests.TestIsolationSecurity();fixture.lab=runner.DockerLab()
        lab=fixture.lab;config=fixture.good_config();config['State']={'Running':False,'ExitCode':0}
        backend=isolation_tests.FakeDocker(lab);cid='b'*64
        def docker(*args,**kwargs):
            if args[0]=='create':backend.containers.add(cid);return cid
            if args[0]=='inspect':return json.dumps([config])
            if args[0]=='start':return json.dumps({'synthetic':True})
            return backend(*args,**kwargs)
        with mock.patch.object(lab,'_docker',side_effect=docker):lab.run(A,'probe')
        self.assertEqual(lab.creation_states['container'],'identified')
        self.assertFalse(lab.containers)

    def test_invalid_docker_stop_states_are_rejected(self):
        for state in ({},{'Running':None},{'Running':0},{'Running':'false'},
                      {'Running':False,'Restarting':'false'},{'Running':False,'Restarting':True}):
            with self.subTest(state=state):
                lab=runner.DockerLab();control=x.ExecutionControl(A,'state','u',lambda:None)
                with control.bind(),mock.patch.object(lab,'_validate_config'),mock.patch.object(lab,'_docker',return_value=json.dumps([{'State':state}])):
                    with self.assertRaises(x.StopFailed):lab._stop_cancelled('a'*64,A,'analyze')
                self.assertEqual(control.state,'stop_failed')

    def test_stop_and_cleanup_failure_never_become_stopped(self):
        lab=runner.DockerLab();lab.images[A]='sha256:'+'a'*64;cid='b'*64
        key,value=lab.label.split('=',1)
        config={'Config':{'Labels':{key:value}},'State':{'Running':True}}
        control=x.ExecutionControl(A,'stop-failure','u',lambda:None);calls=[]
        def docker(*args,**kwargs):
            calls.append(args[0])
            if args[0]=='create':return cid
            if args[0]=='inspect':return json.dumps([config])
            if args[0]=='ps':return cid
            if args[0]=='start':control.request();raise x.TaskCancelled('synthetic')
            raise RuntimeError('synthetic daemon failure')
        with control.bind(),mock.patch.object(lab,'_docker',side_effect=docker),mock.patch.object(lab,'_validate_config'):
            with self.assertRaises(RuntimeError) as caught:lab.run(A,'analyze')
        control.finish()
        self.assertEqual(control.state,'stop_failed')
        self.assertIn(cid,str(caught.exception));self.assertIn(lab.label,str(caught.exception))
        for operation in ('stop','kill','rm'):self.assertIn(operation,calls)

    def test_sqlite_cancellation_commit_failure_and_ambiguous_commit(self):
        for committed in (False,True):
            with self.subTest(committed=committed):
                fixture=cancellation_tests.TestCancellation();fixture.setUp()
                try:
                    original=fixture.app.store.session
                    class Proxy:
                        def __init__(self,db):self.db=db;self.commits=0
                        def __getattr__(self,key):return getattr(self.db,key)
                        def commit(self):
                            self.commits+=1
                            if self.commits==2:
                                if committed:self.db.commit()
                                raise sqlite3.OperationalError('synthetic commit acknowledgement failure')
                            self.db.commit()
                    @contextmanager
                    def session(customer):
                        with original(customer) as db:yield Proxy(db)
                    def execute(tenant):
                        x.current().request()
                        return {'result':{'customer_id':tenant,'synthetic':True,'connection':'none','mode':'proposal-only'}}
                    fixture.worker.execute.side_effect=execute
                    with mock.patch.object(fixture.app.store,'session',side_effect=session):
                        with self.assertRaises(a.ServiceUnavailable):fixture.run_task()
                    record=fixture.record();self.assertEqual(record['status'],'interrupted');self.assertIsNone(record['result'])
                    with fixture.app.store.readonly(A) as db:
                        row=db.execute("SELECT status,result FROM tasks WHERE task_id='job'").fetchone()
                        self.assertEqual(row['status'],'interrupted' if committed else 'running')
                        self.assertIsNone(row['result'])
                    count=fixture.worker.execute.call_count
                    self.assertEqual(fixture.run_task()['status'],'interrupted')
                    self.assertEqual(count,fixture.worker.execute.call_count)
                finally:fixture.doCleanups()
