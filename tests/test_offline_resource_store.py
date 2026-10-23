"""Durable host provenance, crash boundaries and genuine local Docker checks."""
import json
import os
from pathlib import Path
import signal
import select
import tarfile
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples/offline-seo/isolation'))
from resource_store import ResourceStore, OwnershipError, resource_scope
from coordinator import Coordinator
from runner import DockerLab, CUSTOMERS, IsolationUnavailable
import runner

A, B = CUSTOMERS
EXEC = 'e' * 32
CID = 'c' * 64
IID = 'sha256:' + 'a' * 64


class TestResourceOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'resources'
        self.store = ResourceStore(self.root)

    def intent(self, customer=A, kind='container'):
        return self.store.intent(customer, 'same-task', EXEC, kind)

    def config(self, row, resource=CID):
        result = {'Id':resource, 'Config':{'Labels':self.store.labels(row)}}
        if row['kind'] == 'container': result['HostConfig'] = {}
        return result

    def sent(self, row):
        return self.store.transition(row, 'not_sent', 'sent_unconfirmed')

    def test_two_customers_same_task_have_distinct_provenance(self):
        first, second = self.intent(A), self.intent(B)
        self.assertNotEqual(first['proof'],second['proof'])
        self.assertEqual(self.store.records(A),[first]);self.assertEqual(self.store.records(B),[second])
        self.sent(first)
        with self.assertRaises(OwnershipError):self.store.verify(first,CID,self.config(second))

    def test_durable_lifecycle_and_restart(self):
        row=self.intent();self.assertEqual(ResourceStore(self.root).records(A)[0]['state'],'not_sent')
        self.sent(row)
        store=ResourceStore(self.root)
        verified=store.verify(row,CID,self.config(row))
        self.assertEqual(verified['state'],'identified')
        store.transition(row,'identified','removed',CID)
        self.assertEqual(ResourceStore(self.root).records(A)[0]['state'],'removed')
        with self.assertRaises(OwnershipError):store.verify(row,CID,self.config(row))

    def test_resource_identity_labels_kind_and_binding_must_match(self):
        for change in ('id','customer','task','execution','attempt','proof','kind','missing-kind'):
            with self.subTest(change=change):
                row=self.intent();self.sent(row);cfg=self.config(row)
                if change=='id':cfg['Id']='d'*64
                elif change=='missing-kind':cfg.pop('HostConfig')
                else:cfg['Config']['Labels']['dmp-owner-'+change]='forged'
                with self.assertRaises(OwnershipError):self.store.verify(row,CID,cfg)

    def test_cannot_verify_before_send_or_rebind_verified_id(self):
        row=self.intent()
        with self.assertRaises(OwnershipError):self.store.verify(row,CID,self.config(row))
        self.sent(row);self.store.verify(row,CID,self.config(row))
        other='d'*64
        with self.assertRaises(OwnershipError):self.store.verify(row,other,self.config(row,other))

    def test_resource_collision_and_duplicate_transition_fail_closed(self):
        one,two=self.intent(),self.intent();self.sent(one);self.sent(two)
        self.store.verify(one,CID,self.config(one))
        with self.assertRaises(OwnershipError):self.store.verify(two,CID,self.config(two))
        self.assertTrue(self.store.poisoned)
        self.assertEqual(ResourceStore(self.root).records(A)[1]['customer'],A)

    def test_record_manipulation_wrong_tenant_schema_and_corruption_rejected(self):
        for change in ('tenant','schema','trigger','corrupt'):
            with self.subTest(change=change):
                root=Path(self.tmp.name)/change;store=ResourceStore(root);store.intent(A,'job',EXEC,'image')
                dbpath=root/A/'resources.sqlite3'
                if change=='corrupt':dbpath.write_bytes(b'not a sqlite database')
                else:
                    with sqlite3.connect(dbpath) as db:
                        if change=='tenant':db.execute("UPDATE resource_ownership SET customer=?",(B,))
                        elif change=='trigger':db.execute("CREATE TRIGGER unexpected AFTER UPDATE ON resource_ownership BEGIN SELECT 1; END")
                        else:db.execute('PRAGMA user_version=999')
                before=dbpath.read_bytes()
                with self.assertRaises(OwnershipError):ResourceStore(root).records(A)
                # Rejection must not reset or silently repair the resource rows.
                self.assertEqual(before,dbpath.read_bytes())

    def test_commit_error_and_lost_ack_never_allow_owner_retry(self):
        for committed in (False,True):
            with self.subTest(committed=committed):
                store=ResourceStore(Path(self.tmp.name)/str(committed))
                def commit(db):
                    if committed:db.commit()
                    raise sqlite3.OperationalError('private diagnostic')
                with mock.patch.object(store,'_commit',side_effect=commit):
                    with self.assertRaises(OwnershipError) as caught:store.intent(A,'job',EXEC,'image')
                self.assertNotIn('private diagnostic',str(caught.exception))
                with self.assertRaises(OwnershipError):store.intent(A,'job',EXEC,'image')
                rows=ResourceStore(store.root).records(A)
                self.assertEqual(len(rows),int(committed))

    def test_send_commit_failure_prevents_process_start(self):
        with resource_scope(self.root,A,'job',EXEC):lab=DockerLab()
        original=lab.resource_store._commit
        calls=0
        def commit(db):
            nonlocal calls
            calls+=1
            if calls==2:raise sqlite3.OperationalError('synthetic')
            original(db)
        with mock.patch.object(lab.resource_store,'_commit',side_effect=commit),mock.patch.object(runner.subprocess,'Popen') as spawn:
            with self.assertRaises(OwnershipError):lab._docker('import','fake.tar')
            spawn.assert_not_called()
        self.assertEqual(ResourceStore(self.root).records(A)[0]['state'],'not_sent')

    def test_cancel_before_dispatch_leaves_durable_not_sent(self):
        import execution_control as x
        control=x.ExecutionControl(A,'job','u',lambda:None);control.request()
        with resource_scope(self.root,A,'job',EXEC):lab=DockerLab()
        with control.bind(),mock.patch.object(runner.subprocess,'Popen') as spawn:
            with self.assertRaises(x.TaskCancelled):lab._docker('import','fake.tar')
            spawn.assert_not_called()
        self.assertEqual(ResourceStore(self.root).records(A)[0]['state'],'not_sent')
        self.assertFalse(lab.pending_creations)

    def test_lost_docker_reply_remains_uncertain_on_restart(self):
        with resource_scope(self.root,A,'job',EXEC):lab=DockerLab()
        def lose_reply(command,env,timeout,**options):
            options['before_spawn']();options['on_started']()
            raise subprocess.TimeoutExpired('synthetic',1)
        with mock.patch.object(runner,'bounded_command',side_effect=lose_reply):
            with self.assertRaises(subprocess.TimeoutExpired):lab._docker('import','fake.tar')
        row=ResourceStore(self.root).records(A)[0]
        self.assertEqual(row['state'],'sent_unconfirmed');self.assertIsNone(row['resource'])
        self.assertEqual(lab.pending_creations,{'image'})

    def test_false_ownership_cleanup_is_refused(self):
        with resource_scope(self.root,A,'job',EXEC):lab=DockerLab()
        row=lab.resource_store.intent(A,'job',EXEC,'container');lab._bindings[row['attempt']]=row
        lab.resource_store.transition(row,'not_sent','sent_unconfirmed')
        calls=[]
        def docker(*args,**kwargs):
            calls.append(args[0])
            if args[0]=='ps':return CID
            if args[0]=='inspect':return json.dumps([{'Id':CID,'Config':{'Labels':dict([lab.label.split('=',1)])},'HostConfig':{}}])
            raise AssertionError('destructive action forbidden')
        with mock.patch.object(lab,'_docker',side_effect=docker):
            with self.assertRaises(RuntimeError):lab._cleanup('container')
        self.assertNotIn('rm',calls)
        # Even when Docker deletion succeeds, a failed ownership commit must
        # remain an explicit error rather than being swallowed by a retry.
        row=lab.resource_store.verify(row,CID,self.config(row))
        config=self.config(row);config['Config']['Labels'].update(dict([lab.label.split('=',1)]))
        def owned_docker(*args,**kwargs):
            if args[0]=='ps':return CID
            if args[0]=='inspect':return json.dumps([config])
            if args[0]=='rm':return CID
            raise AssertionError('unexpected command')
        with mock.patch.object(lab,'_docker',side_effect=owned_docker),mock.patch.object(lab.resource_store,'_commit',side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(OwnershipError):lab._cleanup('container')
        self.assertTrue(lab.resource_store.poisoned)
        with self.assertRaises(OwnershipError):lab._cleanup('container')

    def test_unknown_record_or_wrong_task_binding_is_refused(self):
        row=self.intent();self.sent(row)
        for field in ('task','customer','execution','proof'):
            binding=dict(row);binding[field]= B if field=='customer' else 'f'*32
            with self.assertRaises(OwnershipError):self.store.verify(binding,CID,self.config(binding))

    def test_scope_restricts_cross_customer_creation(self):
        with resource_scope(self.root,A,'job',EXEC):lab=DockerLab()
        lab.images[B]=IID
        with mock.patch.object(lab,'_docker') as docker:
            with self.assertRaises(OwnershipError):lab._create(B,'analyze')
            docker.assert_not_called()
        lab._resource_customer=B
        with mock.patch.object(runner.subprocess,'Popen') as spawn:
            with self.assertRaises(OwnershipError):lab._docker('import','fake.tar')
            spawn.assert_not_called()
        # A valid but foreign image must not be accepted merely because its ID
        # was accidentally placed in the current customer's in-memory map.
        lab=DockerLab(ownership_root=self.root)
        foreign=lab.resource_store.intent(B,'local-demo',lab.resource_execution,'image')
        lab.resource_store.transition(foreign,'not_sent','sent_unconfirmed')
        cfg=self.config(foreign,IID);lab.resource_store.verify(foreign,IID,cfg)
        lab._bindings[foreign['attempt']]=foreign;lab.images[A]=IID
        with mock.patch.object(lab,'_docker',return_value=json.dumps([cfg])) as docker:
            with self.assertRaises(OwnershipError):lab._create(A,'analyze')
            self.assertEqual(docker.call_count,1)
            self.assertEqual(docker.call_args.args,('inspect',IID))

    def test_unsafe_path_and_forked_store_fail_closed(self):
        alias=Path(self.tmp.name)/'alias';alias.symlink_to(self.root)
        self.intent()
        with mock.patch('resource_store.MAX_DB_BYTES',1):
            with self.assertRaises(OwnershipError):ResourceStore(self.root).records(A)
        with self.assertRaises(OwnershipError):ResourceStore(alias).records(A)
        with mock.patch('resource_store.os.getpid',return_value=-1):
            with self.assertRaises(OwnershipError):self.store.records(A)

    @unittest.skipUnless(hasattr(os,'fork'),'requires Linux fork')
    def test_sigkill_at_persistence_boundaries_never_creates_success(self):
        for boundary in ('intent','sending','identified'):
            with self.subTest(boundary=boundary):
                root=Path(self.tmp.name)/boundary
                read,write=os.pipe();pid=os.fork()
                if pid==0:
                    os.close(read)
                    try:
                        store=ResourceStore(root);row=store.intent(A,'job',EXEC,'container')
                        if boundary!='intent':store.transition(row,'not_sent','sent_unconfirmed')
                        if boundary=='identified':store.verify(row,CID,{'Id':CID,'Config':{'Labels':store.labels(row)},'HostConfig':{}})
                        os.write(write,b'1');signal.pause()
                    finally:os._exit(2)
                os.close(write)
                try:
                    self.assertTrue(select.select([read],[],[],10)[0]);self.assertEqual(os.read(read,1),b'1')
                finally:
                    os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
                    os.close(read)
                state=ResourceStore(root).records(A)[0]['state']
                self.assertEqual(state,{'intent':'not_sent','sending':'sent_unconfirmed','identified':'identified'}[boundary])

    def test_ambiguous_send_commit_never_spawns_and_survives_restart(self):
        with resource_scope(self.root,A,'job',EXEC):lab=DockerLab()
        original=lab.resource_store._commit
        calls=0
        def commit(db):
            nonlocal calls
            calls+=1
            original(db)
            if calls==2:raise sqlite3.OperationalError('lost commit acknowledgement')
        with mock.patch.object(lab.resource_store,'_commit',side_effect=commit),mock.patch.object(runner.subprocess,'Popen') as spawn:
            with self.assertRaises(OwnershipError):lab._docker('import','fake.tar')
            spawn.assert_not_called()
        self.assertEqual(ResourceStore(self.root).records(A)[0]['state'],'sent_unconfirmed')

    def test_deleted_schema_and_record_budget_are_rejected(self):
        self.intent()
        with sqlite3.connect(self.root/A/'resources.sqlite3') as db:db.execute('DROP TABLE resource_ownership')
        with self.assertRaises(OwnershipError):ResourceStore(self.root).records(A)
        root=Path(self.tmp.name)/'budget';store=ResourceStore(root)
        with mock.patch('resource_store.MAX_RECORDS',1):
            store.intent(A,'job',EXEC,'image')
            with self.assertRaises(OwnershipError):store.intent(A,'next',EXEC,'image')

    def test_task_root_rejected_without_touching_any_existing_file(self):
        from coordinator import TaskStore
        root=Path(self.tmp.name)/'task-state'
        with TaskStore(root).session(A) as db:
            db.execute("INSERT INTO tasks(task_id,operation,caller,status) VALUES ('job','seo.analyze','trusted','running')")
            db.commit()
        snapshot=lambda:{str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        before=snapshot()
        for tenant in (A,B):
            with self.assertRaises(OwnershipError):ResourceStore(root).records(tenant)
            self.assertEqual(before,snapshot())
        with sqlite3.connect(root/A/'tasks.sqlite3') as db:
            self.assertEqual(db.execute("SELECT status FROM tasks").fetchone()[0],'running')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],0)

    def test_task_database_under_resource_filename_rejected_before_lock_creation(self):
        from coordinator import TaskStore
        origin=Path(self.tmp.name)/'origin'
        with TaskStore(origin).session(A):pass
        root=Path(self.tmp.name)/'wrong';(root/A).mkdir(parents=True,mode=0o700);root.chmod(0o700)
        target=root/A/'resources.sqlite3';target.write_bytes((origin/A/'tasks.sqlite3').read_bytes());target.chmod(0o600)
        before=target.read_bytes()
        with self.assertRaises(OwnershipError):ResourceStore(root).records(A)
        self.assertEqual(before,target.read_bytes())
        self.assertEqual(sorted(p.name for p in (root/A).iterdir()),['resources.sqlite3'])

    def test_wrong_marker_and_legacy_schema_rejected_without_writes(self):
        self.intent()
        target=self.root/A/'resources.sqlite3'
        with sqlite3.connect(target) as db:db.execute("UPDATE resource_meta SET kind='unrelated'")
        before=target.read_bytes()
        with self.assertRaises(OwnershipError):ResourceStore(self.root).records(A)
        self.assertEqual(before,target.read_bytes())
        from coordinator import TaskStore
        with self.assertRaises(PermissionError):
            with TaskStore(self.root).session(A):pass
        self.assertEqual(before,target.read_bytes())
        self.assertFalse((self.root/A/'tasks.sqlite3').exists())
        legacy=Path(self.tmp.name)/'legacy';(legacy/A).mkdir(parents=True,mode=0o700);legacy.chmod(0o700)
        old=legacy/A/'tasks.sqlite3';old.write_bytes(before);old.chmod(0o600)
        with self.assertRaises(OwnershipError):ResourceStore(legacy).records(A)
        self.assertEqual(before,old.read_bytes())
        self.assertFalse((legacy/A/'resources.sqlite3').exists())

    def test_fresh_lab_flag_cannot_disable_create_or_cleanup_ownership(self):
        for value in (False,True):
            lab=DockerLab(ownership_root=self.root);lab._persistent=value
            lab.images[A]=IID
            key,label=lab.label.split('=',1)
            cfg={'Id':IID,'Config':{'Labels':{key:label,'dmp-owner-customer':B}}}
            with mock.patch.object(lab,'_docker',return_value=json.dumps([cfg])) as docker:
                with self.assertRaises(OwnershipError):lab._create(A,'probe')
                self.assertEqual([c.args[0] for c in docker.call_args_list],['inspect'])
            calls=[]
            def docker(*args,**kwargs):
                calls.append(args[0])
                if args[0]=='ps':return CID
                if args[0]=='inspect':return json.dumps([{**cfg,'Id':CID,'HostConfig':{}}])
                raise AssertionError('must not delete')
            with mock.patch.object(lab,'_docker',side_effect=docker):
                with self.assertRaises(OwnershipError):lab._cleanup('container')
            self.assertNotIn('rm',calls)

    def test_native_mutations_require_complete_proof_before_sending(self):
        for command in (('start','--attach',CID),('stop','--time','2',CID),('kill',CID),('rm','--force',CID),('image','rm',IID)):
            for change in ('missing','foreign','identity'):
                lab=DockerLab(ownership_root=self.root);kind='image' if command[0]=='image' else 'container'
                row=lab.resource_store.intent(A,lab.resource_task,lab.resource_execution,kind)
                lab.resource_store.transition(row,'not_sent','sent_unconfirmed');lab._bindings[row['attempt']]=row
                cfg=self.config(row,command[-1]);cfg['Config']['Labels'].update(dict([lab.label.split('=',1)]))
                if change=='missing':cfg['Config']['Labels'].pop('dmp-owner-proof')
                elif change=='foreign':cfg['Config']['Labels']['dmp-owner-customer']=B
                else:cfg['Id']='d'*64
                def native(args,env,timeout,**options):
                    self.assertEqual(args[3],'inspect')
                    return subprocess.CompletedProcess(args,0,json.dumps([cfg]),'')
                with mock.patch.object(runner,'bounded_command',side_effect=native) as call:
                    with self.assertRaises(OwnershipError):lab._docker(*command)
                    self.assertEqual(call.call_count,1)

        for alias in (('container','rm',CID),('rmi',IID),('image','import','source'),('exec',CID,'sh')):
            lab=DockerLab(ownership_root=self.root)
            with mock.patch.object(runner,'bounded_command') as native:
                with self.assertRaises(OwnershipError):lab._docker(*alias)
                native.assert_not_called()

    def test_native_create_rechecks_image_even_with_fixed_request(self):
        lab=DockerLab(ownership_root=self.root);lab._persistent=False
        lab._active_create=('create','synthetic')
        with mock.patch.object(runner,'bounded_command') as native:
            with self.assertRaises(OwnershipError):lab._docker('create','synthetic')
            native.assert_not_called()

    def test_clean_process_recommendations_do_not_need_scripts_path(self):
        code = """from runner import DockerLab, seo
from coordinator import input_digest
from evidence_recommendations import from_record
from pathlib import Path
import sys
assert 'offline_seo' not in sys.modules
lab=DockerLab();customer='demo-musteri';sources=lab._customer_snapshots()[customer]
report=seo.analyze(seo.TenantScope(lab.fixture_root,customer))
record={'operation':'seo.analyze','status':'succeeded','input_digest':input_digest('seo.analyze',sources),'result':{'result':report}}
result=from_record(record,customer,sources)
assert result['customer_id']==customer and result['applied'] is False
assert 'offline_seo' not in sys.modules
print('independent recommendations ok')
"""
        root=Path(__file__).resolve().parents[1]
        env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join((str(root/'tests/offline_guard'),str(root/'examples/offline-seo/isolation')))
        result=subprocess.run([sys.executable,'-B','-c',code],env=env,cwd=self.tmp.name,text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('independent recommendations ok',result.stdout)


class TestRealDockerOwnership(unittest.TestCase):
    def test_two_tenants_four_operations_and_recommendations_keep_provenance(self):
        try:DockerLab()._docker('version','--format','{{.Server.Version}}')
        except (IsolationUnavailable,RuntimeError,OSError):self.skipTest('local Docker unavailable')
        from access_control import AccessDirectory,OfflineIdentityProvider,AccessService,User,Identity,Tenant,Membership,Role
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'tasks';app=Coordinator(root)
            directory=AccessDirectory();provider=OfflineIdentityProvider(directory);service=AccessService(provider,directory,app)
            for tenant in CUSTOMERS:
                directory.set_user(User(tenant,Identity('offline-fixture',tenant)))
                directory.set_tenant(Tenant(tenant));directory.set_membership(Membership(tenant,tenant,Role.TENANT_ADMIN))
                token=provider.issue(tenant)
                for op in ('seo.analyze','seo.content','seo.technical','seo.audit'):
                    task=op.replace('.','-')
                    self.assertEqual(service.submit(token,tenant,task,op)['status'],'succeeded')
                    self.assertEqual(service.recommendations(token,tenant,task)['customer_id'],tenant)
                rows=ResourceStore(root/'docker-resources').records(tenant)
                self.assertEqual(len(rows),12) # 3 standalone + 3 audit experts: image + container each.
                self.assertTrue(all(r['customer']==tenant and r['state']=='removed' for r in rows))
                self.assertEqual(len({r['execution'] for r in rows}),4)
                count=len(rows)
                restarted=AccessService(provider,directory,Coordinator(root))
                restarted.submit(token,tenant,'seo-audit','seo.audit')
                self.assertEqual(len(ResourceStore(root/'docker-resources').records(tenant)),count)


    @unittest.skipUnless(hasattr(os,'fork'),'requires Linux fork')
    def test_real_create_import_sigkill_and_lost_reply_boundaries(self):
        try:DockerLab()._docker('version','--format','{{.Server.Version}}')
        except (IsolationUnavailable,RuntimeError,OSError):self.skipTest('local Docker unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'ownership';bundle=Path(tmp)/'fixture.tar'
            with tarfile.open(bundle,'w') as archive:
                info=tarfile.TarInfo('synthetic-marker');info.size=4;archive.addfile(info,io.BytesIO(b'demo'))
            for kind in ('image','container'):
                for boundary in ('intent','before_spawn','reply','identified'):
                    with self.subTest(kind=kind,boundary=boundary):
                        task=kind+'-'+boundary
                        with resource_scope(root,A,task,EXEC):lab=DockerLab()
                        # Container need not run: a tiny fixture image suffices.
                        if kind=='container':lab.images[A]=lab._docker('import',str(bundle))
                        read,write=os.pipe();pid=os.fork()
                        if pid==0:
                            os.close(read)
                            # A new process must open its own trusted store.
                            lab.resource_store=ResourceStore(root)
                            def pause():os.write(write,b'1');signal.pause()
                            original_intent=lab.resource_store.intent
                            original_verify=lab.resource_store.verify
                            original_command=runner.bounded_command
                            def intent(*args):
                                value=original_intent(*args)
                                if boundary=='intent':pause()
                                return value
                            def verify(*args):
                                value=original_verify(*args)
                                if boundary=='identified' and args[0]['kind']==kind:pause()
                                return value
                            def command(command,env,timeout,**options):
                                if boundary=='before_spawn' and 'before_spawn' in options:
                                    original=options['before_spawn']
                                    def before():original();pause()
                                    options['before_spawn']=before
                                value=original_command(command,env,timeout,**options)
                                if boundary=='reply' and command[3] in ('create','import'):pause()
                                return value
                            try:
                                with mock.patch.object(lab.resource_store,'intent',side_effect=intent),mock.patch.object(lab.resource_store,'verify',side_effect=verify),mock.patch.object(runner,'bounded_command',side_effect=command):
                                    if kind=='image':lab._docker('import',str(bundle))
                                    else:lab._create(A,'analyze')
                            finally:os._exit(3)
                        os.close(write)
                        try:
                            self.assertTrue(select.select([read],[],[],15)[0]);self.assertEqual(os.read(read,1),b'1')
                        finally:
                            os.kill(pid,signal.SIGKILL);os.waitpid(pid,0);os.close(read)
                            # Explicit test-only cleanup, no supervisor or task retry.
                            # Query by this lab's random label, then match persistent
                            # provenance before any removal. Parent image is also owned.
                            store=ResourceStore(root)
                            rows=[r for r in store.records(A) if r['task']==task]
                            target=[r for r in rows if r['kind']==kind][-1]
                            expected={'intent':'not_sent','before_spawn':'sent_unconfirmed','reply':'sent_unconfirmed','identified':'identified'}[boundary]
                            self.assertEqual(target['state'],expected)
                            for resource_kind in ('container','image'):
                                args=('ps','-a','--no-trunc','--filter','label='+lab.label,'--format','{{.ID}}') if resource_kind=='container' else ('image','ls','--no-trunc','--quiet','--filter','label='+lab.label)
                                for resource in lab._docker(*args).splitlines():
                                    cfg=json.loads(lab._docker('inspect',resource))[0]
                                    row=next(r for r in rows if r['attempt']==cfg['Config']['Labels']['dmp-owner-attempt'])
                                    owned=store.verify(row,resource,cfg)
                                    lab._bindings[row['attempt']]=row
                                    if resource_kind=='container':lab._docker('rm','--force',resource)
                                    else:lab._docker('image','rm',resource)
                                    store.transition(owned,'identified','removed',resource)
