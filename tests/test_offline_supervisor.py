"""Independent process, kernel lease and exact Docker ownership regressions."""
import copy
from contextlib import contextmanager
import fcntl
import select
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ISOLATION=Path(__file__).resolve().parents[1]/'examples/offline-seo/isolation'
sys.path.insert(0,str(ISOLATION))
from resource_store import ResourceStore, OwnershipError, META_SCHEMA, SCHEMA, STORE_KIND
from runner import DockerLab, CUSTOMERS
from supervisor import Supervisor
import runner

A,B=CUSTOMERS
EXEC='e'*32
CID='c'*64


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'resources'
        self.store=ResourceStore(self.root)
        self.supervisor=Supervisor(self.root)

    def resource(self,lease=True):
        def create():
            row=self.store.intent(A,'job',EXEC,'container')
            self.store.transition(row,'not_sent','sent_unconfirmed')
            cfg={'Id':CID,'Config':{'Labels':{**self.store.labels(row),'dmp-offline-isolation':'f'*32}},
                 'HostConfig':{},'State':{'Running':True,'Restarting':False}}
            self.store.verify(row,CID,cfg)
            return row,cfg
        if lease:
            with self.store.execution_lease(A,'job',EXEC):return create()
        return create()

    def backend(self,cfg,stop=True,kill=True,daemon=True):
        calls=[];removed=False
        def docker(lab,*args,**kwargs):
            nonlocal removed
            calls.append(args[0])
            if not daemon:raise RuntimeError('synthetic sensitive error must not persist')
            if args[0]=='inspect':return json.dumps([cfg])
            if args[0]=='stop':cfg['State']['Running']=not stop;return ''
            if args[0]=='kill':cfg['State']['Running']=not kill;return ''
            if args[0]=='rm':removed=True;return ''
            if args[0]=='ps':return '' if removed else CID
            raise AssertionError(args)
        return calls,docker

    def test_orphan_stop_and_repeat_are_idempotent(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg)
        with mock.patch.object(DockerLab,'_docker',backend):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'removed')
            self.assertEqual(self.supervisor.scan(A),{})
        self.assertEqual(calls.count('rm'),1)
        self.assertEqual(self.store.supervisor_outcomes(A)[row['attempt']],'removed')

    def test_live_lease_is_not_pid_or_age_based(self):
        with self.store.execution_lease(A,'job',EXEC):
            row,cfg=self.resource(False)
            with mock.patch.object(DockerLab,'_docker') as docker:
                self.assertEqual(self.supervisor.scan(A)[row['attempt']],'active')
                docker.assert_not_called()
        self.assertFalse(self.store.supervisor_outcomes(A))

    def test_missing_legacy_lease_is_review_not_permission_to_stop(self):
        row,cfg=self.resource(False)
        with mock.patch.object(DockerLab,'_docker') as docker:
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
            docker.assert_not_called()

    def test_replaced_unlinked_symlinked_lease_is_rejected(self):
        for change in ('replace','unlink','symlink'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                self.root=Path(tmp)/'state';self.store=ResourceStore(self.root);self.supervisor=Supervisor(self.root)
                row,cfg=self.resource();path=self.root/A/('execution-'+EXEC+'.lock')
                # Keep old inode alive to exclude immediate inode-number recycling.
                with path.open('rb'):
                    path.unlink()
                    if change=='replace':path.write_bytes(b'');path.chmod(0o600)
                    if change=='symlink':path.symlink_to(self.root/A/'resources.lock')
                    with mock.patch.object(DockerLab,'_docker') as docker:
                        self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
                        docker.assert_not_called()

    def test_forged_full_id_and_all_ownership_fields_rejected(self):
        row,cfg=self.resource()
        for field in ('Id','customer','task','execution','attempt','proof','kind','missing'):
            with sqlite3.connect(self.root/A/'resources.sqlite3') as db:db.execute('DELETE FROM supervisor_results')
            mutated=copy.deepcopy(cfg)
            if field=='Id':mutated['Id']='d'*64
            elif field=='missing':mutated['Config']['Labels'].pop('dmp-owner-proof')
            else:mutated['Config']['Labels']['dmp-owner-'+field]='forged'
            calls,backend=self.backend(mutated)
            with mock.patch.object(DockerLab,'_docker',backend):
                self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
            self.assertNotIn('stop',calls);self.assertNotIn('rm',calls)

    def test_forced_stop_then_verified_removal(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg,stop=False)
        with mock.patch.object(DockerLab,'_docker',backend):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'removed')
        self.assertIn('kill',calls)

    def test_failed_stop_preserves_review_and_identified(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg,False,False)
        with mock.patch.object(DockerLab,'_docker',backend):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
        self.assertNotIn('rm',calls);self.assertEqual(self.store.records(A)[0]['state'],'identified')

    def test_daemon_unavailable_never_records_success_or_raw_error(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg,daemon=False)
        with mock.patch.object(DockerLab,'_docker',backend):self.supervisor.scan(A)
        self.assertEqual(self.store.supervisor_outcomes(A),{row['attempt']:'review'})
        self.assertNotIn(b'sensitive',(self.root/A/'resources.sqlite3').read_bytes())

    def test_malformed_docker_state_rejected(self):
        row,cfg=self.resource();cfg['State']['Running']='false';calls,backend=self.backend(cfg)
        with mock.patch.object(DockerLab,'_docker',backend):self.supervisor.scan(A)
        self.assertNotIn('rm',calls);self.assertNotIn('stop',calls)

    def test_sql_commit_failure_prevents_stop(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg)
        with mock.patch.object(DockerLab,'_docker',backend),mock.patch.object(ResourceStore,'_commit',side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(OwnershipError):self.supervisor.scan(A)
        self.assertNotIn('stop',calls)

    def test_sent_unconfirmed_discovery_requires_exact_evidence(self):
        row,cfg=self.resource()
        with sqlite3.connect(self.root/A/'resources.sqlite3') as db:
            db.execute("UPDATE resource_ownership SET state='sent_unconfirmed',resource=NULL")
        calls,backend=self.backend(cfg)
        with mock.patch.object(DockerLab,'_docker',backend):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'removed')

    def test_unknown_pending_creation_stays_uncertain(self):
        row,cfg=self.resource()
        with sqlite3.connect(self.root/A/'resources.sqlite3') as db:
            db.execute("UPDATE resource_ownership SET state='sent_unconfirmed',resource=NULL")
        with mock.patch.object(DockerLab,'_docker',return_value=''):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
        self.assertEqual(self.store.records(A)[0]['state'],'sent_unconfirmed')

    def test_exact_v1_migration_preserves_ownership_and_no_lease_is_invented(self):
        self.store.records(A)
        path=self.root/A/'resources.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE execution_leases');db.execute('DROP TABLE supervisor_results')
            db.execute('UPDATE resource_meta SET version=1');db.execute('PRAGMA user_version=1')
        self.assertEqual(ResourceStore(self.root).leases(A),[])
        with sqlite3.connect(path) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)

    def test_execution_cannot_be_reused(self):
        with self.store.execution_lease(A,'job',EXEC):pass
        with self.assertRaises((OwnershipError,OSError)):
            with self.store.execution_lease(A,'another',EXEC):pass

    def test_second_supervisor_claim_is_exclusive(self):
        self.resource();lease=self.store.leases(A)[0]
        with self.store.orphan_claim(A,lease) as first:
            self.assertTrue(first)
            with ResourceStore(self.root).orphan_claim(A,lease) as second:self.assertFalse(second)


    def test_timeout_is_persisted_and_not_automatically_retried(self):
        row,cfg=self.resource()
        with mock.patch.object(DockerLab,'_docker',side_effect=subprocess.TimeoutExpired('synthetic',1)) as docker:
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
            count=docker.call_count
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
            self.assertEqual(docker.call_count,count)

    def test_lost_stop_reply_uses_inspect_before_success(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg)
        def lose_reply(lab,*args,**kwargs):
            result=backend(lab,*args,**kwargs)
            if args[0]=='stop':raise subprocess.TimeoutExpired('synthetic',1)
            return result
        with mock.patch.object(DockerLab,'_docker',lose_reply):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'removed')
        self.assertGreaterEqual(calls.count('inspect'),2)

    def test_lost_commit_ack_never_stops_without_durable_decision(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg)
        def committed_then_failed(store,db):
            db.commit();raise sqlite3.OperationalError('lost acknowledgement')
        with mock.patch.object(DockerLab,'_docker',backend),mock.patch.object(ResourceStore,'_commit',committed_then_failed):
            with self.assertRaises(OwnershipError):self.supervisor.scan(A)
        self.assertNotIn('stop',calls)
        self.assertEqual(ResourceStore(self.root).supervisor_outcomes(A)[row['attempt']],'stopping')

    def test_lease_wrong_task_does_not_stop(self):
        row,cfg=self.resource()
        with sqlite3.connect(self.root/A/'resources.sqlite3') as db:db.execute("UPDATE execution_leases SET task='other'")
        with mock.patch.object(DockerLab,'_docker') as docker:
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
            docker.assert_not_called()

    def test_removed_reply_without_actual_removal_is_not_success(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg)
        def not_removed(lab,*args,**kwargs):
            if args[0]=='ps':return CID
            return backend(lab,*args,**kwargs)
        with mock.patch.object(DockerLab,'_docker',not_removed):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
        self.assertEqual(self.store.records(A)[0]['state'],'identified')

    def test_different_supervisor_customer_does_not_access_foreign_resource(self):
        self.resource()
        with mock.patch.object(DockerLab,'_docker') as docker:
            self.assertEqual(self.supervisor.scan(B),{})
            docker.assert_not_called()

    def test_new_database_rejects_malformed_supervisor_records(self):
        row,cfg=self.resource()
        for outcome in ('success','removed'):
            with sqlite3.connect(self.root/A/'resources.sqlite3') as db:
                db.execute('DELETE FROM supervisor_results')
                db.execute('INSERT INTO supervisor_results VALUES (?,?)',(row['attempt'],outcome))
            with mock.patch.object(DockerLab,'_docker') as docker:
                with self.assertRaises(OwnershipError):self.supervisor.scan(A)
                docker.assert_not_called()


    def test_separate_process_cannot_claim_live_owner_even_without_pid_metadata(self):
        with self.store.execution_lease(A,'job',EXEC):
            self.resource(False)
            code="from resource_store import ResourceStore;import sys; s=ResourceStore(sys.argv[1]); lease=s.leases('demo-musteri')[0]; context=s.orphan_claim('demo-musteri',lease); print(context.__enter__()); context.__exit__(None,None,None)"
            env=dict(os.environ);env['PYTHONPATH']=str(ISOLATION)
            result=subprocess.run([sys.executable,'-B','-c',code,str(self.root)],env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'False')
            with mock.patch('os.kill',side_effect=AssertionError('PID must not decide owner death')):
                self.assertEqual(set(self.supervisor.scan(A).values()),{'active'})

    def test_inherited_store_cannot_claim_after_process_change(self):
        self.resource();lease=self.store.leases(A)[0]
        with mock.patch('resource_store.os.getpid',return_value=-1):
            with self.assertRaises(OwnershipError):
                with self.store.orphan_claim(A,lease):pass

    def test_invalid_v1_record_is_not_migrated(self):
        self.resource(False);path=self.root/A/'resources.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE execution_leases');db.execute('DROP TABLE supervisor_results')
            db.execute('UPDATE resource_meta SET version=1');db.execute('PRAGMA user_version=1')
            db.execute("UPDATE resource_ownership SET task='../invalid'")
        before=path.read_bytes()
        with self.assertRaises(OwnershipError):ResourceStore(self.root).records(A)
        self.assertEqual(before,path.read_bytes())


    def test_tenant_lock_contention_is_retryable_without_review(self):
        row,cfg=self.resource();original=ResourceStore.orphan_claim
        @contextmanager
        def contention(store,customer,lease):
            fd=os.open(store.root/customer/'resources.lock',os.O_RDWR)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:
                with original(store,customer,lease) as claim:yield claim
            finally:os.close(fd)
        with mock.patch.object(ResourceStore,'orphan_claim',contention),mock.patch.object(DockerLab,'_docker') as docker:
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'busy')
            docker.assert_not_called()
        self.assertEqual(self.store.supervisor_outcomes(A),{})
        calls,backend=self.backend(cfg)
        with mock.patch.object(DockerLab,'_docker',backend):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'removed')

    def test_review_commits_while_execution_claim_is_still_held(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg,False,False)
        original=ResourceStore.supervisor_result;available=[]
        def observe(store,binding,outcome):
            if outcome=='review':
                probe=ResourceStore(store.root)
                with probe.orphan_claim(A,probe.leases(A)[0]) as claim:available.append(claim)
            return original(store,binding,outcome)
        with mock.patch.object(DockerLab,'_docker',backend),mock.patch.object(ResourceStore,'supervisor_result',observe):
            self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
        self.assertEqual(available,[False])

    def test_removal_and_supervisor_outcome_commit_atomically(self):
        row,cfg=self.resource();calls,backend=self.backend(cfg)
        original=ResourceStore._commit;pairs=[]
        def observe(store,db):
            state=db.execute('SELECT state FROM resource_ownership WHERE attempt=?',(row['attempt'],)).fetchone()[0]
            outcome=db.execute('SELECT outcome FROM supervisor_results WHERE attempt=?',(row['attempt'],)).fetchone()
            if state=='removed':pairs.append((state,outcome[0] if outcome else None))
            return original(store,db)
        with mock.patch.object(DockerLab,'_docker',backend),mock.patch.object(ResourceStore,'_commit',observe):
            self.supervisor.scan(A)
        self.assertEqual(pairs,[('removed','removed')])

    def test_terminal_commit_error_and_lost_ack_leave_no_half_terminal_pair(self):
        for committed in (False,True):
            with self.subTest(committed=committed),tempfile.TemporaryDirectory() as tmp:
                self.root=Path(tmp)/'state';self.store=ResourceStore(self.root);self.supervisor=Supervisor(self.root)
                row,cfg=self.resource();calls,backend=self.backend(cfg);original=ResourceStore._commit
                def fail(store,db):
                    state=db.execute('SELECT state FROM resource_ownership').fetchone()[0]
                    if state=='removed':
                        if committed:db.commit()
                        raise sqlite3.OperationalError('synthetic terminal commit error')
                    return original(store,db)
                with mock.patch.object(DockerLab,'_docker',backend),mock.patch.object(ResourceStore,'_commit',fail):
                    with self.assertRaises(OwnershipError):self.supervisor.scan(A)
                restarted=ResourceStore(self.root)
                pair=(restarted.records(A)[0]['state'],restarted.supervisor_outcomes(A)[row['attempt']])
                self.assertEqual(pair,('removed','removed') if committed else ('identified','stopping'))

    @unittest.skipUnless(hasattr(os,'fork'),'requires Linux fork')
    def test_supervisor_sigkill_at_sqlite_boundaries_preserves_consistent_pairs(self):
        for boundary in ('stopping','before_terminal_commit','after_terminal_commit'):
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as tmp:
                self.root=Path(tmp)/'state';self.store=ResourceStore(self.root);self.supervisor=Supervisor(self.root)
                row,cfg=self.resource();read,write=os.pipe();pid=os.fork()
                if pid==0:
                    os.close(read)
                    try:
                        original=ResourceStore._commit;calls,backend=self.backend(cfg)
                        def pause():os.write(write,b'1');signal.pause()
                        def commit(store,db):
                            state=db.execute('SELECT state FROM resource_ownership').fetchone()[0]
                            outcome=db.execute('SELECT outcome FROM supervisor_results').fetchone()
                            if boundary=='before_terminal_commit' and state=='removed':pause()
                            original(store,db)
                            if (boundary=='stopping' and outcome and outcome[0]=='stopping'
                                    or boundary=='after_terminal_commit' and state=='removed'):pause()
                        with mock.patch.object(DockerLab,'_docker',backend),mock.patch.object(ResourceStore,'_commit',commit):
                            self.supervisor.scan(A)
                    finally:os._exit(3)
                os.close(write)
                try:
                    self.assertTrue(select.select([read],[],[],10)[0]);self.assertEqual(os.read(read,1),b'1')
                finally:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0);os.close(read)
                restarted=ResourceStore(self.root)
                pair=(restarted.records(A)[0]['state'],restarted.supervisor_outcomes(A)[row['attempt']])
                self.assertEqual(pair,('removed','removed') if boundary=='after_terminal_commit' else ('identified','stopping'))

    @unittest.skipUnless(hasattr(os,'fork'),'requires Linux fork')
    def test_fork_inherited_owner_lock_prevents_false_orphan_until_child_exits(self):
        read,write=os.pipe();pid=None
        try:
            with self.store.execution_lease(A,'job',EXEC):
                self.resource(False);pid=os.fork()
                if pid==0:
                    os.close(read)
                    os.write(write,b'1');signal.pause();os._exit(3)
                os.close(write)
                self.assertTrue(select.select([read],[],[],10)[0]);self.assertEqual(os.read(read,1),b'1')
            lease=self.store.leases(A)[0]
            with self.store.orphan_claim(A,lease) as claim:self.assertFalse(claim)
            os.kill(pid,signal.SIGKILL);os.waitpid(pid,0);pid=None
            with self.store.orphan_claim(A,lease) as claim:self.assertTrue(claim)
        finally:
            if pid is not None:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
            os.close(read)

    def test_native_identity_change_before_stop_or_remove_prevents_mutation(self):
        row,cfg=self.resource()
        for running in (True,False):
            with sqlite3.connect(self.root/A/'resources.sqlite3') as db:db.execute('DELETE FROM supervisor_results')
            inspections=[];mutations=[]
            def native(command,env,timeout,**options):
                operation=command[3]
                if operation=='inspect':
                    current=copy.deepcopy(cfg);current['State']['Running']=running
                    inspections.append(1)
                    if len(inspections)>1:current['Config']['Labels']['dmp-owner-proof']='forged'
                    return subprocess.CompletedProcess(command,0,json.dumps([current]),'')
                mutations.append(operation)
                return subprocess.CompletedProcess(command,0,'','')
            with mock.patch.object(runner,'bounded_command',native):
                self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
            self.assertGreaterEqual(len(inspections),2)
            self.assertEqual(mutations,[])

    def test_duplicate_pending_matches_and_remove_errors_never_record_success(self):
        row,cfg=self.resource()
        for failure in ('duplicate','remove_error','lost_remove_reply'):
            with sqlite3.connect(self.root/A/'resources.sqlite3') as db:
                db.execute('DELETE FROM supervisor_results')
                db.execute('UPDATE resource_ownership SET state=?,resource=?',
                           ('sent_unconfirmed',None) if failure=='duplicate' else ('identified',CID))
            calls,backend=self.backend(copy.deepcopy(cfg))
            def native(lab,*args,**kwargs):
                if failure=='duplicate' and args[0]=='ps':return CID+'\n'+CID
                if failure=='remove_error' and args[0]=='rm':raise RuntimeError('synthetic removal error')
                value=backend(lab,*args,**kwargs)
                if failure=='lost_remove_reply' and args[0]=='rm':raise subprocess.TimeoutExpired('synthetic',1)
                return value
            with mock.patch.object(DockerLab,'_docker',native):
                self.assertEqual(self.supervisor.scan(A)[row['attempt']],'review')
            self.assertNotEqual(self.store.records(A)[0]['state'],'removed')


# Executed by a fresh interpreter. Coordinator is real; only its trusted test
# specialist uses a deliberately slow, no-network Python fixture to make death
# observable. Production runner policy/packaging/provenance are unchanged.
COORDINATOR_CODE=r'''
import signal, sys
from pathlib import Path
from coordinator import Coordinator, TrustedCaller, SeoSpecialist
from runner import DockerLab
class SlowLab(DockerLab):
    def _worker_command(self,customer,action):
        return [str(self.binary),'-I','-S','-B','-c',
                'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(90)']
class SlowSeo(SeoSpecialist):
    def execute_snapshot(self,customer,snapshots):
        with SlowLab() as lab: return lab.run(customer,'analyze')
app=Coordinator(Path(sys.argv[1]));app._specialists['seo.analyze']=SlowSeo()
app.run(TrustedCaller('local-fixture',frozenset([sys.argv[2]])),sys.argv[2],'same-task')
'''


# Deterministic test-only crash point: a real monitor process commits stopping,
# then waits to be SIGKILLed. No production bypass or crash flag is introduced.
CRASH_SUPERVISOR_CODE=r'''
import signal,sys
from resource_store import ResourceStore
from supervisor import main
original=ResourceStore.supervisor_result
def gate(store,binding,outcome):
    original(store,binding,outcome)
    if outcome=='stopping':
        print('STOPPING_COMMITTED',flush=True)
        signal.pause()
ResourceStore.supervisor_result=gate
sys.argv=['supervisor','--ownership-root',sys.argv[1],'--customer',sys.argv[2]]
raise SystemExit(main())
'''


class TestRealSupervisor(unittest.TestCase):
    def test_independent_supervisor_survives_sigkill_preserves_healthy_tenant(self):
        try:DockerLab()._docker('version','--format','{{.Server.Version}}')
        except (RuntimeError,OSError):self.skipTest('local Docker unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'tasks';state.mkdir(mode=0o700)
            root=state/'docker-resources';processes=[];labs={};monitors=[]
            env=dict(os.environ);env['PYTHONPATH']=str(ISOLATION);env['PYTHONDONTWRITEBYTECODE']='1'
            def rows(tenant):return ResourceStore(root).records(tenant)
            def outcome(tenant):
                try:return ResourceStore(root).supervisor_outcomes(tenant).get(labs[tenant][0]['attempt'])
                except OwnershipError:return None # retry observation after concurrent metadata writes
            def wait_running(tenant):
                deadline=time.monotonic()+45
                while time.monotonic()<deadline:
                    try:
                        for row in rows(tenant):
                            if row['kind']=='container' and row['state']=='identified':
                                lab=DockerLab(ownership_root=root)
                                cfg=json.loads(lab._docker('inspect',row['resource']))[0]
                                if cfg['State']['Running']:return row,cfg
                    except (RuntimeError,OSError):pass
                    time.sleep(.1)
                self.fail('fixture worker did not start')
            def restore(tenant):
                lab=DockerLab(ownership_root=root);lab.resource_task='same-task'
                for row in sorted(rows(tenant),key=lambda r:r['kind']!='container'):
                    if row['state']=='removed':continue
                    if row['state'] not in ('identified','sent_unconfirmed'):continue
                    if row['resource'] is None:continue
                    cfg=json.loads(lab._docker('inspect',row['resource']))[0]
                    ResourceStore(root).verify(row,row['resource'],cfg)
                    lab.resource_execution=row['execution'];lab._resource_customer=tenant
                    lab.label='dmp-offline-isolation='+cfg['Config']['Labels']['dmp-offline-isolation']
                    lab._bindings[row['attempt']]=row
                    # Only exact test-owned resources, never general Docker cleanup.
                    if row['kind']=='container':lab._docker('rm','--force',row['resource'])
                    else:lab._docker('image','rm',row['resource'])
                    ResourceStore(root).transition(row,'identified','removed',row['resource'])
            try:
                for tenant in (A,B):
                    processes.append(subprocess.Popen([sys.executable,'-B','-c',COORDINATOR_CODE,str(state),tenant],
                                                     env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True))
                    labs[tenant]=wait_running(tenant)
                for tenant in (A,B):
                    command=([sys.executable,'-B','-c',CRASH_SUPERVISOR_CODE,str(root),tenant] if tenant==A else
                             [sys.executable,'-B',str(ISOLATION/'supervisor.py'),
                              '--ownership-root',str(root),'--customer',tenant])
                    monitors.append(subprocess.Popen(command,env=env,
                        stdout=subprocess.PIPE if tenant==A else subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,start_new_session=True))
                time.sleep(1.3)
                self.assertTrue(all(p.poll() is None for p in monitors))
                # Healthy owners retain a kernel lease even though PID values
                # are never recorded/consulted by Supervisor.
                self.assertTrue(json.loads(DockerLab()._docker('inspect',labs[B][0]['resource']))[0]['State']['Running'])
                started=time.monotonic();processes[0].kill();processes[0].wait(timeout=5)
                self.assertTrue(select.select([monitors[0].stdout],[],[],8)[0])
                self.assertEqual(monitors[0].stdout.readline(),b'STOPPING_COMMITTED\n')
                self.assertIsNone(monitors[0].poll()) # survived coordinator's death
                self.assertTrue(json.loads(DockerLab()._docker('inspect',labs[A][0]['resource']))[0]['State']['Running'])
                monitors[0].kill();monitors[0].wait(timeout=5);monitors[0].stdout.close()
                self.assertEqual(ResourceStore(root).supervisor_outcomes(A)[labs[A][0]['attempt']],'stopping')
                monitors[0]=subprocess.Popen([sys.executable,'-B',str(ISOLATION/'supervisor.py'),
                    '--ownership-root',str(root),'--customer',A],env=env,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
                deadline=started+15
                while time.monotonic()<deadline:
                    if outcome(A)=='removed':break
                    time.sleep(.1)
                else:self.fail('orphan not verified removed')
                elapsed=time.monotonic()-started
                print('SUPERVISOR_SIGKILL_SECONDS=%.3f'%elapsed,flush=True)
                ids=DockerLab()._docker('ps','-a','--no-trunc','--filter','id='+labs[A][0]['resource'],'--format','{{.ID}}')
                self.assertEqual(ids,'')
                self.assertTrue(all(p.poll() is None for p in monitors))
                self.assertIsNone(processes[1].poll())
                self.assertTrue(json.loads(DockerLab()._docker('inspect',labs[B][0]['resource']))[0]['State']['Running'])
                # A second tenant's independent owner can subsequently die too.
                processes[1].kill();processes[1].wait(timeout=5)
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    if outcome(B)=='removed':break
                    time.sleep(.1)
                else:self.fail('second tenant orphan not removed')
                self.assertEqual(Supervisor(root).scan(A),{})
                self.assertEqual(Supervisor(root).scan(B),{})
            finally:
                for p in monitors+processes:
                    if p.poll() is None:p.kill()
                    p.wait(timeout=10)
                    if p.stdout is not None:p.stdout.close()
                for tenant in (A,B):
                    if (root/tenant/'resources.sqlite3').exists():restore(tenant)
