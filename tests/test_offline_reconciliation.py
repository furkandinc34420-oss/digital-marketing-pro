"""Safe, bounded, tenant-scoped reconciliation and persistent operator conditions."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import signal
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

ISOLATION=Path(__file__).resolve().parents[1]/'examples/offline-seo/isolation'
sys.path.insert(0,str(ISOLATION))
from resource_store import ResourceStore, OwnershipError, RECONCILIATION_CODES
from reconciliation import Reconciler, MESSAGES
from runner import DockerLab, CUSTOMERS
import test_offline_supervisor as supervisor_fixtures
import runner
import reconciliation

A,B=CUSTOMERS


class DockerFixture:
    def __init__(self):
        self.configs={};self.calls=[];self.failure=None

    def command(self,lab,*args,**options):
        self.calls.append(args)
        if self.failure=='daemon':raise RuntimeError('sensitive-path-token-never-persist')
        if args[0]=='version':return 'fixture-version'
        if args[0]=='inspect':return json.dumps([copy.deepcopy(self.configs[args[-1]])])
        if args[0]=='ps':
            ids=[rid for rid,cfg in self.configs.items() if 'HostConfig' in cfg]
        elif args[:2]==('image','ls'):
            ids=[rid for rid,cfg in self.configs.items() if 'HostConfig' not in cfg]
        else:ids=None
        if ids is not None:
            for value in args:
                if value.startswith('id='):ids=[rid for rid in ids if rid==value[3:]]
                if value.startswith('label=dmp-owner-attempt='):
                    attempt=value.split('=',2)[-1]
                    ids=[rid for rid in ids if self.configs[rid]['Config']['Labels']['dmp-owner-attempt']==attempt]
            return '\n'.join(sorted(ids))
        if args[0] in ('stop','kill'):
            if self.failure!='stop':self.configs[args[-1]]['State']['Running']=False
            return ''
        if args[0]=='rm' or args[:2]==('image','rm'):
            if self.failure=='remove':raise RuntimeError('synthetic removal failure')
            del self.configs[args[-1]]
            if self.failure=='lost_remove_reply':raise subprocess.TimeoutExpired('fixture',1)
            return ''
        raise AssertionError(args)

    def mutations(self):
        return [c for c in self.calls if c[0] in ('stop','kill','rm') or c[:2]==('image','rm')]


class TestReconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'resources';self.store=ResourceStore(self.root)
        self.reconciler=Reconciler(self.root);self.docker=DockerFixture()
        fixture=self.docker
        def command(lab,*args,**kwargs):return fixture.command(lab,*args,**kwargs)
        self.patch=mock.patch.object(DockerLab,'_docker',command);self.patch.start();self.addCleanup(self.patch.stop)

    def row(self,customer=A,kind='container',state='identified',execution=None):
        execution=execution or uuid.uuid4().hex
        row=self.store.intent(customer,'same-task',execution,kind)
        if state=='not_sent':return row
        self.store.transition(row,'not_sent','sent_unconfirmed')
        rid=('sha256:' if kind=='image' else '')+uuid.uuid4().hex*2
        cfg={'Id':rid,'Config':{'Labels':{**self.store.labels(row),'dmp-offline-isolation':'f'*32}}}
        if kind=='container':cfg.update(HostConfig={},State={'Running':True,'Restarting':False})
        self.docker.configs[rid]=cfg
        if state=='sent_unconfirmed':return row
        return self.store.verify(row,rid,cfg)

    def seed(self,customer=A,kind='container',state='identified'):
        execution=uuid.uuid4().hex
        with self.store.execution_lease(customer,'same-task',execution):
            return self.row(customer,kind,state,execution)

    def item(self,result,row):
        return next(i for i in result['items'] if i['attempt_id']==row['attempt'])

    def test_default_observes_only_and_repetition_is_byte_idempotent(self):
        row=self.seed();first=self.reconciler.scan(A)
        before=(self.root/A/'resources.sqlite3').read_bytes()
        second=Reconciler(self.root).scan(A)
        self.assertEqual(first,second);self.assertEqual(before,(self.root/A/'resources.sqlite3').read_bytes())
        self.assertEqual(self.item(first,row)['code'],'orphan_present');self.assertEqual(self.docker.mutations(),[])

    def test_apply_removes_only_verified_orphan_and_is_idempotent(self):
        row=self.seed();first=self.reconciler.scan(A,apply=True)
        self.assertEqual(self.item(first,row)['code'],'removed')
        count=len(self.docker.mutations())
        self.assertEqual(first,self.reconciler.scan(A,apply=True));self.assertEqual(count,len(self.docker.mutations()))

    def test_active_execution_is_never_touched(self):
        execution=uuid.uuid4().hex
        with self.store.execution_lease(A,'same-task',execution):
            row=self.row(execution=execution)
            result=self.reconciler.scan(A,apply=True,retry_reviewed=True)
        self.assertEqual(self.item(result,row)['code'],'busy');self.assertEqual(self.docker.mutations(),[])

    def test_two_customers_same_task_stay_separate(self):
        one=self.seed(A);two=self.seed(B)
        result=self.reconciler.scan(A,apply=True)
        self.assertTrue(all(i['customer_id']==A for i in result['items']))
        self.assertIn(two['resource'],self.docker.configs)
        self.assertEqual(self.store.observations(B),[])
        self.assertNotIn(two['execution'],json.dumps(result))

    def test_daemon_outage_persists_without_raw_error_and_recovers(self):
        row=self.seed();self.docker.failure='daemon'
        result=self.reconciler.scan(A,apply=True)
        self.assertEqual(self.item(result,row)['code'],'docker_unavailable')
        self.assertNotIn('sensitive',json.dumps(result))
        self.assertNotIn(b'sensitive',(self.root/A/'resources.sqlite3').read_bytes())
        self.assertEqual(self.store.records(A)[0]['state'],'identified')
        self.docker.failure=None
        self.assertEqual(self.item(self.reconciler.scan(A),row)['code'],'orphan_present')

    def test_missing_known_id_is_not_absence_on_daemon_failure(self):
        row=self.seed();del self.docker.configs[row['resource']];self.docker.failure='daemon'
        self.reconciler.scan(A,apply=True)
        self.assertEqual(self.store.records(A)[0]['state'],'identified')

    def test_lost_remove_reply_requires_explicit_review_retry_then_reconciles_absence(self):
        row=self.seed();self.docker.failure='lost_remove_reply'
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'cleanup_failed')
        self.assertEqual(self.store.records(A)[0]['state'],'identified')
        self.docker.failure=None
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'review_required')
        result=self.reconciler.scan(A,apply=True,retry_reviewed=True)
        self.assertEqual(self.item(result,row)['code'],'removed')
        self.assertEqual(self.store.supervisor_outcomes(A)[row['attempt']],'removed')

    def test_unknown_pending_creation_never_assumed_absent(self):
        row=self.seed(state='sent_unconfirmed');self.docker.configs.clear()
        result=self.reconciler.scan(A,apply=True,retry_reviewed=True)
        self.assertEqual(self.item(result,row)['code'],'pending_unresolved')
        self.assertEqual(self.store.records(A)[0]['state'],'sent_unconfirmed')

    def test_pending_id_binds_only_after_all_proof_matches(self):
        row=self.seed(state='sent_unconfirmed')
        cfg=next(iter(self.docker.configs.values()));cfg['Config']['Labels']['dmp-owner-customer']=B
        result=self.reconciler.scan(A,apply=True)
        self.assertEqual(self.item(result,row)['code'],'ownership_mismatch')
        self.assertEqual(self.store.records(A)[0]['state'],'sent_unconfirmed');self.assertEqual(self.docker.mutations(),[])

    def test_pending_valid_resource_can_be_resolved_and_removed(self):
        row=self.seed(state='sent_unconfirmed')
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'removed')

    def test_stop_and_remove_failures_never_become_success(self):
        row=self.seed()
        for failure in ('stop','remove'):
            self.docker.failure=failure
            result=self.reconciler.scan(A,apply=True,retry_reviewed=True)
            self.assertEqual(self.item(result,row)['code'],'cleanup_failed')
            self.assertEqual(self.store.records(A)[0]['state'],'identified')

    def test_review_latch_preserved_until_operator_retry(self):
        row=self.seed();self.store.supervisor_result(row,'review')
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'review_required')
        self.assertEqual(self.docker.mutations(),[])
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True,retry_reviewed=True),row)['code'],'removed')

    def test_image_not_removed_before_execution_containers_resolved(self):
        execution=uuid.uuid4().hex
        with self.store.execution_lease(A,'same-task',execution):
            image=self.row(kind='image',execution=execution)
            container=self.row(state='sent_unconfirmed',execution=execution)
        self.docker.configs={image['resource']:self.docker.configs[image['resource']]}
        result=self.reconciler.scan(A,apply=True)
        self.assertEqual(self.item(result,image)['code'],'image_blocked')
        self.assertFalse(any(c[:2]==('image','rm') for c in self.docker.mutations()))

    def test_orphan_image_cleanup_uses_native_nonforce_command(self):
        row=self.seed(kind='image')
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'removed')
        self.assertIn(('image','rm',row['resource']),self.docker.mutations())
        self.assertFalse(any('--force' in c for c in self.docker.mutations()))

    def test_missing_lease_and_replaced_lock_are_alerts_without_mutation(self):
        row=self.row()
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'missing_lease')
        self.assertEqual(self.docker.mutations(),[])

    def test_removed_record_with_present_resource_is_not_deleted(self):
        row=self.seed();self.store.transition(row,'identified','removed',row['resource'])
        result=self.reconciler.scan(A,apply=True,retry_reviewed=True)
        self.assertEqual(self.item(result,row)['code'],'removed_but_present');self.assertEqual(self.docker.mutations(),[])

    def test_not_sent_stays_not_sent(self):
        row=self.seed(state='not_sent')
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'not_sent')
        self.assertEqual(self.store.records(A)[0]['state'],'not_sent')

    def test_warning_survives_clean_python_restart(self):
        row=self.seed();self.docker.failure='daemon';expected=self.reconciler.scan(A)
        env=dict(os.environ);env['PYTHONPATH']=str(ISOLATION)
        code='import json,sys;from reconciliation import Reconciler;print(json.dumps(Reconciler(sys.argv[1]).report(sys.argv[2])))'
        result=subprocess.run([sys.executable,'-B','-c',code,str(self.root),A],env=env,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout),self.reconciler.report(A))

    def test_stable_event_identity_changes_only_with_condition(self):
        row=self.seed();one=self.item(self.reconciler.scan(A),row)
        self.docker.failure='daemon';two=self.item(self.reconciler.scan(A),row)
        self.assertEqual(one['alert_id'],two['alert_id']);self.assertNotEqual(one['event_id'],two['event_id'])
        self.assertEqual(two['revision'],one['revision']+1)
        self.assertEqual(two,self.item(self.reconciler.scan(A),row))

    def test_pagination_and_fixed_code_schema_are_bounded(self):
        self.assertEqual(set(MESSAGES),RECONCILIATION_CODES)
        for _ in range(3):self.seed()
        result=self.reconciler.scan(A);page=self.reconciler.report(A,limit=1)
        self.assertEqual(page['total'],3);self.assertTrue(page['has_more']);self.assertEqual(len(page['items']),1)
        self.assertNotIn('proof',json.dumps(result))
        for limit in (0,501):
            with self.assertRaises(ValueError):self.reconciler.report(A,limit=limit)

    def test_sqlite_intent_commit_failure_prevents_cleanup(self):
        row=self.seed()
        with mock.patch.object(ResourceStore,'_commit',side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(OwnershipError):self.reconciler.scan(A,apply=True)
        self.assertEqual(self.docker.mutations(),[])

    def test_lost_intent_commit_ack_is_durable_and_no_cleanup_sent(self):
        row=self.seed()
        def lost(store,db):db.commit();raise sqlite3.OperationalError('lost acknowledgement')
        with mock.patch.object(ResourceStore,'_commit',lost):
            with self.assertRaises(OwnershipError):self.reconciler.scan(A,apply=True)
        self.assertEqual(self.docker.mutations(),[])
        self.assertEqual(ResourceStore(self.root).observations(A)[0]['code'],'cleanup_pending')

    def test_cross_customer_observation_and_absence_completion_are_refused(self):
        row=self.seed(A);changed=dict(row,customer=B)
        with self.assertRaises(OwnershipError):self.store.observation(changed,'removed')
        with self.assertRaises(OwnershipError):ResourceStore(self.root).reconcile_absent(changed)
        self.assertEqual(ResourceStore(self.root).records(A)[0]['state'],'identified')

    def test_corrupt_observation_is_rejected_without_repair(self):
        row=self.seed();self.reconciler.scan(A)
        path=self.root/A/'resources.sqlite3'
        for code in ('injected-secret','removed'):
            with sqlite3.connect(path) as db:db.execute('UPDATE reconciliation_observations SET code=?',(code,))
            before=path.read_bytes()
            with self.assertRaises(OwnershipError):self.reconciler.scan(A,apply=True)
            self.assertEqual(before,path.read_bytes());self.assertEqual(self.docker.mutations(),[])

    def test_exact_v2_migration_preserves_all_ownership_rows(self):
        row=self.seed();path=self.root/A/'resources.sqlite3';before=self.store.records(A)
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE reconciliation_observations');db.execute('UPDATE resource_meta SET version=2');db.execute('PRAGMA user_version=2')
        restarted=ResourceStore(self.root)
        self.assertEqual(restarted.records(A),before);self.assertEqual(restarted.observations(A),[])

    def test_invalid_retry_selection_and_report_tenant_are_refused(self):
        self.seed()
        with self.assertRaises(ValueError):self.reconciler.scan(A,retry_reviewed=True)
        with self.assertRaises(OwnershipError):self.reconciler.report('unknown')


    def test_absence_commit_error_and_lost_ack_keep_consistent_pair(self):
        row=self.seed();self.store.supervisor_result(row,'review');self.docker.configs.clear()
        original=ResourceStore._commit
        for committed in (False,True):
            def fail(store,db):
                state=db.execute('SELECT state FROM resource_ownership').fetchone()[0]
                if state=='removed':
                    if committed:db.commit()
                    raise sqlite3.OperationalError('synthetic lost terminal commit')
                return original(store,db)
            with mock.patch.object(ResourceStore,'_commit',fail):
                with self.assertRaises(OwnershipError):self.reconciler.scan(A,apply=True,retry_reviewed=True)
            restarted=ResourceStore(self.root)
            pair=(restarted.records(A)[0]['state'],restarted.supervisor_outcomes(A)[row['attempt']])
            self.assertEqual(pair,('removed','removed') if committed else ('identified','review'))

    def test_native_image_remove_rechecks_ownership_before_sending(self):
        row=self.seed(kind='image');cfg=self.docker.configs[row['resource']];inspects=[];mutations=[]
        self.patch.stop()
        def native(command,env,timeout,**options):
            args=command[3:]
            if args[0]=='version':output='fixture'
            elif args[:2]==['image','ls']:output=row['resource']
            elif args[0]=='inspect':
                current=copy.deepcopy(cfg);inspects.append(1)
                if len(inspects)>1:current['Config']['Labels']['dmp-owner-customer']=B
                output=json.dumps([current])
            else:mutations.append(args);output=''
            return subprocess.CompletedProcess(command,0,output,'')
        with mock.patch.object(runner,'bounded_command',native),mock.patch.object(runner.shutil,'which',return_value='/fixture/docker'):
            result=self.reconciler.scan(A,apply=True)
        self.assertEqual(self.item(result,row)['code'],'cleanup_failed')
        self.assertEqual(mutations,[]);self.assertGreaterEqual(len(inspects),2)

    def test_invalid_v2_metadata_is_not_migrated(self):
        self.seed();path=self.root/A/'resources.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE reconciliation_observations');db.execute('UPDATE resource_meta SET version=2');db.execute('PRAGMA user_version=2')
            db.execute("UPDATE execution_leases SET task='../invalid'")
        before=path.read_bytes()
        with self.assertRaises(OwnershipError):ResourceStore(self.root).records(A)
        self.assertEqual(before,path.read_bytes())

    def test_multiple_pending_matches_are_not_adopted(self):
        row=self.seed(state='sent_unconfirmed');cfg=copy.deepcopy(next(iter(self.docker.configs.values())))
        other='d'*64;cfg['Id']=other;self.docker.configs[other]=cfg
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'pending_unresolved')
        self.assertEqual(self.docker.mutations(),[])

    def test_cli_invalid_retry_does_not_create_state(self):
        env=dict(os.environ);env['PYTHONPATH']=str(ISOLATION)
        result=subprocess.run([sys.executable,'-B',str(ISOLATION/'reconciliation.py'),'--ownership-root',str(self.root),
            '--customer',A,'--retry-reviewed'],env=env,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,2);self.assertFalse(self.root.exists())

    @unittest.skipUnless(hasattr(os,'fork'),'requires Linux fork')
    def test_sigkill_after_durable_cleanup_intent_survives_restart(self):
        row=self.seed();read,write=os.pipe();pid=os.fork()
        if pid==0:
            os.close(read)
            try:
                original=ResourceStore._commit
                def pause_after_intent(store,db):
                    original(store,db)
                    code=db.execute('SELECT code FROM reconciliation_observations').fetchone()
                    if code and code[0]=='cleanup_pending':os.write(write,b'1');signal.pause()
                with mock.patch.object(ResourceStore,'_commit',pause_after_intent):self.reconciler.scan(A,apply=True)
            finally:os._exit(3)
        os.close(write)
        try:
            self.assertTrue(select.select([read],[],[],10)[0]);self.assertEqual(os.read(read,1),b'1')
        finally:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0);os.close(read)
        self.assertEqual(ResourceStore(self.root).observations(A)[0]['code'],'cleanup_pending')
        self.assertEqual(ResourceStore(self.root).records(A)[0]['state'],'identified')
        self.assertEqual(self.docker.mutations(),[])


    def test_scan_batch_is_bounded_and_remaining_work_is_explicit(self):
        for _ in range(3):self.seed()
        first=self.reconciler.scan(A,limit=1)
        self.assertEqual(first['scanned'],1);self.assertTrue(first['scan_has_more']);self.assertEqual(first['next_offset'],1)
        self.assertEqual(len(self.store.observations(A)),1)
        second=self.reconciler.scan(A,limit=1,offset=1)
        self.assertEqual(len(self.store.observations(A)),2);self.assertEqual(second['next_offset'],2)
        third=self.reconciler.scan(A,limit=1,offset=2)
        self.assertFalse(third['scan_has_more']);self.assertEqual(len(self.store.observations(A)),3)
        with self.assertRaises(ValueError):self.reconciler.scan(A,limit=101)


    @unittest.skipUnless(hasattr(os,'fork'),'requires Linux fork')
    def test_competing_process_claim_cannot_close_pending_alert(self):
        row=self.seed();self.store.observation(row,'cleanup_pending');lease=self.store.leases(A)[0]
        read,write=os.pipe();pid=os.fork()
        if pid==0:
            os.close(read)
            try:
                with ResourceStore(self.root).orphan_claim(A,lease) as claimed:
                    if claimed:os.write(write,b'1');signal.pause()
            finally:os._exit(3)
        os.close(write)
        try:
            self.assertTrue(select.select([read],[],[],10)[0]);self.assertEqual(os.read(read,1),b'1')
            result=self.reconciler.scan(A,apply=True)
            self.assertEqual(self.store.observations(A)[0]['code'],'cleanup_pending')
            self.assertGreaterEqual(result['busy_count'],1)
            self.assertEqual(self.docker.mutations(),[])
        finally:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0);os.close(read)
        self.assertEqual(self.item(self.reconciler.scan(A,apply=True),row)['code'],'removed')

    def test_stale_not_sent_snapshot_cannot_close_new_cleanup_alert(self):
        row=self.seed(state='not_sent');original=self.docker.command;injected=False
        def advance(lab,*args,**kwargs):
            nonlocal injected
            if args[0]=='version' and not injected:
                injected=True
                self.store.transition(row,'not_sent','sent_unconfirmed')
                rid='c'*64;cfg={'Id':rid,'Config':{'Labels':{**self.store.labels(row),'dmp-offline-isolation':'f'*32}},
                    'HostConfig':{},'State':{'Running':True,'Restarting':False}}
                self.docker.configs[rid]=cfg
                owned=self.store.verify(row,rid,cfg)
                self.store.observation(owned,'cleanup_pending')
            return original(lab,*args,**kwargs)
        with mock.patch.object(self.docker,'command',advance):
            with self.assertRaises(OwnershipError):self.reconciler.scan(A)
        self.assertEqual(ResourceStore(self.root).observations(A)[0]['code'],'cleanup_pending')
        self.assertEqual(self.docker.mutations(),[])

    def test_nonboolean_apply_and_review_flags_are_rejected_before_mutation(self):
        row=self.seed();self.store.supervisor_result(row,'review')
        for apply,retry in (('false','false'),(True,'false'),(1,True),(True,1),(None,False)):
            with self.subTest(apply=apply,retry=retry):
                with self.assertRaises(ValueError):self.reconciler.scan(A,apply=apply,retry_reviewed=retry)
        self.assertEqual(self.docker.mutations(),[])

    def test_report_snapshot_stays_consistent_with_concurrent_new_observation(self):
        self.seed();self.reconciler.scan(A);original=ResourceStore._session;injected=False
        @contextmanager
        def after_read(store,customer):
            nonlocal injected
            with original(store,customer) as db:yield db
            if not injected:
                injected=True
                new=ResourceStore(store.root).intent(customer,'new-task',uuid.uuid4().hex,'container')
                ResourceStore(store.root).observation(new,'not_sent')
        with mock.patch.object(ResourceStore,'_session',after_read):
            result=self.reconciler.report(A)
        self.assertTrue(injected)
        self.assertEqual(result['total'],1)
        self.assertEqual(self.reconciler.report(A)['total'],2)

    def test_default_pending_observation_has_no_docker_mutations(self):
        row=self.seed(state='sent_unconfirmed')
        result=self.reconciler.scan(A)
        self.assertEqual(self.item(result,row)['code'],'orphan_present')
        self.assertEqual(self.docker.mutations(),[])
        # Documented: observation records/provenance are maintenance writes.
        self.assertEqual(self.store.records(A)[0]['state'],'identified')


    def test_empty_daemon_failure_cannot_return_success(self):
        self.docker.failure='daemon'
        argv=['reconciliation','--ownership-root',str(self.root),'--customer',A]
        with mock.patch.object(sys,'argv',argv),mock.patch('builtins.print'):
            self.assertEqual(reconciliation.main(),2)
        result=self.reconciler.scan(A)
        self.assertTrue(result['health_check_failed'])
        self.assertEqual(result['scan_total'],0) # no fabricated resource-scoped alert


class TestRealReconciliation(unittest.TestCase):
    def test_real_two_tenants_orphan_cleanup_and_persistent_restart(self):
        try:DockerLab()._docker('version','--format','{{.Server.Version}}')
        except (RuntimeError,OSError):self.skipTest('local Docker unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'tasks';state.mkdir(mode=0o700);root=state/'docker-resources'
            processes=[];workers={};env=dict(os.environ);env['PYTHONPATH']=str(ISOLATION)
            def rows(customer):return ResourceStore(root).records(customer)
            try:
                for customer in CUSTOMERS:
                    processes.append(subprocess.Popen([sys.executable,'-B','-c',supervisor_fixtures.COORDINATOR_CODE,str(state),customer],
                        env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True))
                    deadline=time.monotonic()+45
                    while time.monotonic()<deadline:
                        try:
                            found=[r for r in rows(customer) if r['kind']=='container' and r['state']=='identified']
                            if found:
                                cfg=json.loads(DockerLab()._docker('inspect',found[0]['resource']))[0]
                                if cfg['State']['Running']:workers[customer]=found[0];break
                        except (RuntimeError,OSError):pass
                        time.sleep(.1)
                    else:self.fail('fixture worker did not start')
                processes[0].kill();processes[0].wait(timeout=5)
                task_bytes=(state/A/'tasks.sqlite3').read_bytes()
                reconciler=Reconciler(root)
                observed=reconciler.scan(A)
                self.assertTrue(any(i['code']=='orphan_present' for i in observed['items']))
                self.assertTrue(json.loads(DockerLab()._docker('inspect',workers[A]['resource']))[0]['State']['Running'])
                cleaned=reconciler.scan(A,apply=True)
                self.assertEqual(cleaned['unresolved'],0)
                self.assertEqual(task_bytes,(state/A/'tasks.sqlite3').read_bytes())
                self.assertTrue(all(r['state']=='removed' for r in rows(A)))
                self.assertEqual(cleaned,Reconciler(root).scan(A,apply=True))
                self.assertIsNone(processes[1].poll())
                self.assertTrue(json.loads(DockerLab()._docker('inspect',workers[B]['resource']))[0]['State']['Running'])
                self.assertTrue(all(i['code']=='busy' for i in reconciler.scan(B,apply=True)['items']))
                with sqlite3.connect(state/A/'tasks.sqlite3') as db:
                    self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id='same-task'").fetchone()[0],'running')
                processes[1].kill();processes[1].wait(timeout=5)
                self.assertEqual(reconciler.scan(B,apply=True)['unresolved'],0)
                self.assertTrue(all(r['state']=='removed' for r in rows(B)))
            finally:
                for process in processes:
                    if process.poll() is None:process.kill()
                    process.wait(timeout=10)
                # Only known fixtures: use the same lease + proof protocol.
                for customer in CUSTOMERS:
                    if (root/customer/'resources.sqlite3').exists():
                        result=Reconciler(root).scan(customer,apply=True,retry_reviewed=True)
                        self.assertEqual(result['unresolved'],0,'test-owned cleanup needs operator review')


    @unittest.skipUnless(shutil.which('docker'),'requires local Docker CLI')
    def test_real_unreachable_socket_never_becomes_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state';store=ResourceStore(root);execution=uuid.uuid4().hex
            with store.execution_lease(A,'job',execution):
                row=store.intent(A,'job',execution,'container')
                store.transition(row,'not_sent','sent_unconfirmed')
                store.verify(row,'c'*64,{'Id':'c'*64,'Config':{'Labels':store.labels(row)},'HostConfig':{}})
            with mock.patch.object(runner,'SOCKET','unix://'+str(Path(tmp)/'nonexistent.sock')):
                result=Reconciler(root).scan(A,apply=True)
            self.assertEqual(result['items'][0]['code'],'docker_unavailable')
            self.assertEqual(ResourceStore(root).records(A)[0]['state'],'identified')
