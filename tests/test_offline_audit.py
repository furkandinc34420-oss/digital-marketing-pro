"""Unified reports: provenance, failures, tenant boundaries and Docker regression."""
import copy
import hashlib
import json
import select
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'examples/offline-seo/isolation'))
import coordinator as c
import audit_report as audit
import content_seo
import technical_seo
import offline_seo
from runner import CUSTOMERS, DockerLab, IsolationUnavailable

A, B = CUSTOMERS


class TestOfflineAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'; self.app = c.Coordinator(self.root)
        self.caller = c.TrustedCaller('local-test', frozenset(CUSTOMERS))
        self.sources = DockerLab()._customer_snapshots()
        # Fake experts only for orchestration tests; real isolation is exercised below.
        class Scope:
            def __init__(self, tenant, snapshots):
                self.customer_id = tenant; self.snapshots = snapshots
            def read(self, name):
                raw = self.snapshots[self.customer_id][name]
                return json.loads(raw.decode('utf-8-sig')), hashlib.sha256(raw).hexdigest()
        modules = {'seo.analyze': offline_seo, 'seo.content': content_seo, 'seo.technical': technical_seo}
        self.adapters = {}
        for op, module in modules.items():
            adapter = mock.Mock()
            adapter.execute_snapshot.side_effect = lambda tenant, snapshots, module=module: {'result': module.analyze(Scope(tenant, snapshots))}
            self.adapters[op] = adapter
            self.app._specialists[op] = adapter

    def test_two_customers_complete_reports_and_deduplication(self):
        for customer in CUSTOMERS:
            record = self.app.run(self.caller, customer, 'audit', 'seo.audit')
            self.assertEqual(record['status'], 'succeeded', record)
            report = record['result']['result']
            self.assertTrue(report['complete']); self.assertEqual(report['customer_id'], customer)
            self.assertEqual(set(report['experts']), set(audit.OPERATIONS))
            self.assertIn('Tamamlanan uzman: 3/3', report['summary_tr'])
            self.assertEqual(self.app.run(self.caller, customer, 'audit', 'seo.audit'), record)
            audit.validate_audit(report, customer)
        for adapter in self.adapters.values():
            self.assertEqual(adapter.execute_snapshot.call_count, 2)

    def test_shared_snapshot_and_source_hashes_match_all_experts(self):
        record = self.app.run(self.caller, A, 'audit', 'seo.audit')
        report = record['result']['result']
        for item in report['experts'].values():
            self.assertEqual(item['result']['source_hashes'], report['source_hashes'])
        self.assertEqual(len({id(x.execute_snapshot.call_args.args[1]) for x in self.adapters.values()}), 1)
        with self.assertRaises(TypeError):
            self.adapters['seo.content'].execute_snapshot.call_args.args[1][A]['pages.json'] = b'changed'

    def test_equivalent_findings_merge_without_losing_sources(self):
        snapshots = copy.deepcopy(self.sources)
        pages = json.loads(snapshots[A]['pages.json'])
        pages['pages'][0]['html'] = '<html><head></head><body><h1>Başlık</h1><p>Metin</p></body></html>'
        snapshots[A]['pages.json'] = json.dumps(pages, ensure_ascii=False).encode()
        with mock.patch.object(DockerLab, '_customer_snapshots', return_value=snapshots):
            report = self.app.run(self.caller, A, 'audit', 'seo.audit')['result']['result']
        found = [f for f in report['findings'] if f['page_id'] == 1 and f['code'] == 'title_missing']
        self.assertEqual(len(found), 1)
        self.assertEqual({s['operation'] for s in found[0]['sources']}, {'seo.analyze', 'seo.content'})
        self.assertGreaterEqual(len(found[0]['recommendations']), 2)
        self.assertTrue(any('issue_index' in source for source in found[0]['sources']))
        canonical = [f for f in report['findings'] if f['page_id'] == 1 and f['code'] == 'canonical_missing']
        self.assertEqual(len(canonical), 1)
        self.assertEqual({s['operation'] for s in canonical[0]['sources']}, {'seo.analyze', 'seo.technical'})
        self.assertEqual(report['findings'], sorted(report['findings'], key=lambda f: (f['fix_order'], {'high': 0, 'medium': 1, 'low': 2}[f['severity']], f['page_id'], f['code'])))

    def test_failure_is_partial_and_other_experts_continue_without_secret(self):
        self.adapters['seo.content'].execute_snapshot.side_effect = RuntimeError('PRIVATE-OTHER-CUSTOMER')
        record = self.app.run(self.caller, A, 'partial', 'seo.audit')
        self.assertEqual(record['status'], 'failed')
        report = record['result']['result']; self.assertFalse(report['complete'])
        self.assertEqual(report['experts']['seo.technical']['status'], 'succeeded')
        self.assertNotIn('PRIVATE-OTHER-CUSTOMER', json.dumps(record))
        self.assertIn('PRIVATE-OTHER-CUSTOMER', json.dumps(self.app.admin_diagnostics(A, 'partial')))
        self.assertEqual(self.app.run(self.caller, A, 'partial', 'seo.audit'), record)
        self.adapters['seo.content'].execute_snapshot.assert_called_once()
        cli = subprocess.run([sys.executable, '-B', str(REPO / 'examples/offline-seo/isolation/coordinator.py'),
                              '--state-root', str(self.root), '--customer', A, '--task-id', 'partial',
                              '--operation', 'seo.audit'], capture_output=True, text=True, timeout=20)
        self.assertEqual(cli.returncode, 1, cli.stderr)
        self.assertFalse(json.loads(cli.stdout)['result']['result']['complete'])

    def test_malformed_foreign_and_wrong_source_expert_outputs_fail_closed(self):
        original = self.adapters['seo.content'].execute_snapshot.side_effect
        for index, change in enumerate((lambda r: r.update(customer_id=B), lambda r: r.update(operation='seo.technical'),
                                        lambda r: r.update(source_hashes={'profile.json':'0'*64,'pages.json':'1'*64}),
                                        lambda r: r.pop('pages'))):
            def broken(tenant, snapshots, change=change):
                result = original(tenant, snapshots); change(result['result']); return result
            self.adapters['seo.content'].execute_snapshot.side_effect = broken
            record = self.app.run(self.caller, A, 'bad-' + str(index), 'seo.audit')
            self.assertEqual(record['status'], 'failed')
            self.assertIsNone(record['result']['result']['experts']['seo.content']['result'])

    def test_interrupt_keeps_checkpoint_and_never_reexecutes(self):
        self.adapters['seo.content'].execute_snapshot.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.app.run(self.caller, A, 'cut', 'seo.audit')
        record = self.app.get(self.caller, A, 'cut')
        self.assertEqual(record['status'], 'interrupted')
        report = record['result']['result']; self.assertFalse(report['complete'])
        self.assertEqual(report['execution_state'], 'interrupted')
        self.assertEqual(report['experts']['seo.analyze']['status'], 'succeeded')
        self.assertEqual(report['experts']['seo.content']['status'], 'interrupted')
        self.assertEqual(report['experts']['seo.technical']['status'], 'pending')
        self.app.run(self.caller, A, 'cut', 'seo.audit')
        self.adapters['seo.content'].execute_snapshot.assert_called_once()
        self.adapters['seo.technical'].execute_snapshot.assert_not_called()

    def test_authorization_and_changed_input_conflict(self):
        only_a = c.TrustedCaller('a', frozenset({A}))
        with self.assertRaises(PermissionError):
            self.app.run(only_a, B, 'audit', 'seo.audit')
        self.assertFalse(self.root.exists())
        self.app.run(self.caller, A, 'audit', 'seo.audit')
        changed = copy.deepcopy(self.sources); changed[A]['pages.json'] += b'\n'
        with mock.patch.object(DockerLab, '_customer_snapshots', return_value=changed):
            with self.assertRaises(c.TaskConflict):
                self.app.run(self.caller, A, 'audit', 'seo.audit')

    def test_oversized_expert_becomes_failed_not_complete(self):
        original = self.adapters['seo.analyze'].execute_snapshot.side_effect
        def huge(tenant, snapshots):
            result = original(tenant, snapshots); result['result']['padding'] = 'x' * audit.MAX_EXPERT_BYTES
            return result
        self.adapters['seo.analyze'].execute_snapshot.side_effect = huge
        record = self.app.run(self.caller, A, 'large', 'seo.audit')
        self.assertEqual(record['status'], 'failed')
        self.assertFalse(record['result']['result']['complete'])
        self.assertIsNone(record['result']['result']['experts']['seo.analyze']['result'])
        with self.assertRaises(ValueError):
            audit.bounded({'padding': 'x' * audit.MAX_AUDIT_BYTES})

    def test_corrupt_cached_summary_or_customer_is_rejected(self):
        record = self.app.run(self.caller, A, 'audit', 'seo.audit')
        for change in (lambda r: r.update(complete=False), lambda r: r.update(customer_id=B), lambda r: r.update(summary_tr='Sahte başarı')):
            corrupted = copy.deepcopy(record['result']); change(corrupted['result'])
            with sqlite3.connect(self.root / A / 'tasks.sqlite3') as db:
                db.execute('UPDATE tasks SET result=?', (json.dumps(corrupted),))
            with self.assertRaises(ValueError):
                self.app.get(self.caller, A, 'audit')


class TestAuditDocker(unittest.TestCase):
    def test_two_customers_real_audit_and_existing_operations_and_cli(self):
        try:
            DockerLab()._docker('version', '--format', '{{.Server.Version}}')
        except (IsolationUnavailable, RuntimeError, OSError) as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            app = c.Coordinator(Path(tmp) / 'state')
            caller = c.TrustedCaller('local-test', frozenset(CUSTOMERS))
            for customer in CUSTOMERS:
                record = app.run(caller, customer, 'combined', 'seo.audit')
                self.assertEqual(record['status'], 'succeeded', record)
                report = record['result']['result']; self.assertTrue(report['complete'])
                self.assertTrue(all(x['result']['customer_id'] == customer for x in report['experts'].values()))
                self.assertNotIn('OTHER_CUSTOMER_SENTINEL' if customer == A else 'kurgusal SEO testi', json.dumps(report, ensure_ascii=False))
                cli = subprocess.run([sys.executable, '-B', str(REPO / 'examples/offline-seo/isolation/coordinator.py'),
                                      '--state-root', str(Path(tmp) / 'state'), '--customer', customer,
                                      '--task-id', 'combined', '--operation', 'seo.audit'], capture_output=True, text=True, timeout=20)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertEqual(json.loads(cli.stdout)['result']['result'], report)


class TestAuditAdditionalBoundaries(unittest.TestCase):
    setUp = TestOfflineAudit.setUp

    def test_combined_budget_rejects_only_the_overflowing_result(self):
        original = self.adapters['seo.analyze'].execute_snapshot.side_effect
        def padded(tenant, snapshots):
            result = original(tenant, snapshots)
            result['result']['padding'] = 'x' * 100000
            return result
        self.adapters['seo.analyze'].execute_snapshot.side_effect = padded
        # Each normal expert fits, but the padded first result cannot be checkpointed.
        with mock.patch.object(audit, 'MAX_AUDIT_BYTES', 100000):
            record = self.app.run(self.caller, A, 'budget', 'seo.audit')
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['result']['result']['experts']['seo.analyze']['result'])
        self.assertFalse(record['result']['result']['complete'])

    def test_bad_legacy_metadata_and_coverage_are_not_accepted(self):
        original = self.adapters['seo.analyze'].execute_snapshot.side_effect
        def broken(tenant, snapshots):
            result = original(tenant, snapshots); result['result']['pages'][0]['meta'] = {}
            return result
        self.adapters['seo.analyze'].execute_snapshot.side_effect = broken
        record = self.app.run(self.caller, A, 'bad-legacy', 'seo.audit')
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['result']['result']['experts']['seo.analyze']['result'])

    def test_independent_task_does_not_collide_or_reuse_audit_record(self):
        report = self.app.run(self.caller, A, 'audit', 'seo.audit')
        # An independent specialist remains separately executable with its own ID.
        adapter = self.app._specialists['seo.content']
        valid = adapter.execute_snapshot(A, self.sources)
        with mock.patch.object(c.ContentSeoSpecialist, 'execute_snapshot', return_value=valid):
            self.app._specialists['seo.content'] = c.ContentSeoSpecialist()
            separate = self.app.run(self.caller, A, 'standalone', 'seo.content')
        self.assertEqual(separate['status'], 'succeeded')
        with self.assertRaises(c.TaskConflict):
            self.app.run(self.caller, A, 'audit', 'seo.content')
        self.assertTrue(report['result']['result']['complete'])

    def test_sigkill_recovers_checkpoint_without_docker_or_retry(self):
        script = '''
import sys, signal, json, hashlib
from pathlib import Path
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import coordinator as c, offline_seo
class Scope:
    customer_id='demo-musteri'
    def __init__(self, snapshots): self.snapshots=snapshots
    def read(self,name):
        raw=self.snapshots[self.customer_id][name]
        return json.loads(raw.decode('utf-8-sig')), hashlib.sha256(raw).hexdigest()
class First:
    def execute_snapshot(self,customer,snapshots):
        return {'result':offline_seo.analyze(Scope(snapshots))}
class Blocked:
    def execute_snapshot(self,customer,snapshots):
        print('READY',flush=True); signal.pause()
app=c.Coordinator(Path(sys.argv[3]))
app._specialists['seo.analyze']=First()
app._specialists['seo.content']=Blocked()
app.run(c.TrustedCaller('child',frozenset({'demo-musteri'})),'demo-musteri','killed','seo.audit')
'''
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(REPO / 'examples/offline-seo/isolation'),
                                  str(REPO / 'scripts'), str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 15)
            self.assertTrue(ready, 'Child checkpoint was not reached')
            self.assertEqual(child.stdout.readline().strip(), 'READY')
            child.kill(); child.communicate(timeout=10)
            record = self.app.get(self.caller, A, 'killed')
            self.assertEqual(record['status'], 'interrupted')
            report = record['result']['result']; self.assertFalse(report['complete'])
            self.assertEqual(report['experts']['seo.analyze']['status'], 'succeeded')
            self.assertEqual(report['experts']['seo.content']['status'], 'interrupted')
            self.app.run(self.caller, A, 'killed', 'seo.audit')
            for adapter in self.adapters.values():
                adapter.execute_snapshot.assert_not_called()
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


class TestAuditReviewRegressions(unittest.TestCase):
    setUp = TestOfflineAudit.setUp

    def test_cached_report_swapped_between_same_customer_tasks_is_rejected(self):
        first = self.app.run(self.caller, A, 'first', 'seo.audit')
        sources = copy.deepcopy(self.sources)
        data = json.loads(sources[A]['pages.json'])
        data['pages'][0]['html'] += '<!-- changed snapshot -->'
        sources[A]['pages.json'] = json.dumps(data).encode()
        with mock.patch.object(DockerLab, '_customer_snapshots', return_value=sources):
            second = self.app.run(self.caller, A, 'second', 'seo.audit')
        self.assertNotEqual(first['input_digest'], second['input_digest'])
        with self.app.store.session(A) as db:
            db.execute('UPDATE tasks SET result=? WHERE task_id=?', (json.dumps(second['result']), 'first'))
            db.commit()
        with self.assertRaises(ValueError):
            self.app.get(self.caller, A, 'first')
        self.assertEqual(self.app.get(self.caller, A, 'second')['status'], 'succeeded')

    def test_manifest_and_raw_fingerprint_share_one_canonical_rule(self):
        hashes = {n: hashlib.sha256(v).hexdigest() for n, v in self.sources[A].items()}
        self.assertEqual(c.input_digest('seo.audit', self.sources[A]),
                         c.input_digest('seo.audit', hashes, manifest=True))
        expected = hashlib.sha256(b'seo.analyze\0' + b'\0'.join(
            n.encode() + b'\0' + raw for n, raw in sorted(self.sources[A].items()))).hexdigest()
        self.assertEqual(c.input_digest('seo.analyze', self.sources[A]), expected)

    def test_contradictory_legacy_proposals_and_measurements(self):
        original = self.adapters['seo.analyze'].execute_snapshot.side_effect
        mutations = [lambda r: r['proposals'][0].update(reason='missing'),
                     lambda r: r['proposals'][0].update(expected_before={}),
                     lambda r: r['proposals'][0].update(proposed_value=[]),
                     lambda r: r['proposals'][0].update(status='applied'),
                     lambda r: r['pages'][0]['meta'].update(title_status='missing'),
                     lambda r: r['pages'][0]['meta'].update(title_length=999),
                     lambda r: r['proposals'].pop()]
        for i, change in enumerate(mutations):
            def bad(tenant, snapshots, change=change):
                result = original(tenant, snapshots); change(result['result']); return result
            self.adapters['seo.analyze'].execute_snapshot.side_effect = bad
            record = self.app.run(self.caller, A, 'contradiction-' + str(i), 'seo.audit')
            self.assertEqual(record['status'], 'failed')
            self.assertFalse(record['result']['result']['complete'])

    def test_distinct_issues_are_never_filtered_by_words(self):
        original = self.adapters['seo.analyze'].execute_snapshot.side_effect
        messages = ['Canonical issue mentioning title', 'Robots issue mentioning description',
                    'Content issue mentioning title', 'Independent title issue']
        def extra(tenant, snapshots):
            result = original(tenant, snapshots)
            result['result']['pages'][0]['issues'].extend({'message': m} for m in messages)
            return result
        self.adapters['seo.analyze'].execute_snapshot.side_effect = extra
        report = self.app.run(self.caller, A, 'distinct', 'seo.audit')['result']['result']
        codes = {'legacy_' + hashlib.sha256(m.encode()).hexdigest()[:12] for m in messages}
        self.assertTrue(codes <= {f['code'] for f in report['findings']})
        self.assertEqual(len(codes), 4)

    def test_invalid_parent_child_state_combinations_rejected(self):
        report = self.app.run(self.caller, A, 'states', 'seo.audit')['result']['result']
        invalid = [('interrupted', ['succeeded', 'running', 'pending']),
                   ('failed', ['succeeded', 'running', 'pending']),
                   ('running', ['pending', 'succeeded', 'pending']),
                   ('running', ['running', 'running', 'pending']),
                   ('succeeded', ['succeeded', 'failed', 'pending']),
                   ('running', ['interrupted', 'pending', 'pending'])]
        for state, statuses in invalid:
            experts = copy.deepcopy(report['experts'])
            for op, status in zip(audit.OPERATIONS, statuses):
                if status != 'succeeded':
                    experts[op] = {'status': status, 'result': None, 'error': None}
            with self.assertRaises(ValueError):
                audit.compose(A, report['source_hashes'], report['page_catalog'], experts, state)

    def test_sqlite_checkpoint_write_and_commit_failures_never_succeed(self):
        original = self.app._run_audit
        for method in ('execute', 'commit'):
            class Fault:
                def __init__(self, db): self.db = db; self.hit = False
                def execute(self, sql, *args):
                    if method == 'execute' and not self.hit and sql.startswith('UPDATE tasks SET result='):
                        self.hit = True; raise sqlite3.OperationalError('injected write failure')
                    return self.db.execute(sql, *args)
                def commit(self):
                    if method == 'commit' and not self.hit:
                        self.hit = True; raise sqlite3.OperationalError('injected commit failure')
                    return self.db.commit()
            with mock.patch.object(self.app, '_run_audit', side_effect=lambda db, *args: original(Fault(db), *args)):
                record = self.app.run(self.caller, A, 'fault-' + method, 'seo.audit')
            self.assertEqual(record['status'], 'failed')
            self.assertTrue(record['result'] is None or not record['result']['result']['complete'])

    def test_interrupt_at_each_checkpoint_is_incomplete_and_not_retried(self):
        compose = audit.compose
        for stop in range(1, 8):
            count = 0
            def interrupted(*args, **kwargs):
                nonlocal count
                count += 1
                if count == stop: raise KeyboardInterrupt()
                return compose(*args, **kwargs)
            task = 'checkpoint-' + str(stop)
            with mock.patch.object(audit, 'compose', side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    self.app.run(self.caller, A, task, 'seo.audit')
            record = self.app.get(self.caller, A, task)
            self.assertEqual(record['status'], 'interrupted')
            if record['result']:
                self.assertFalse(record['result']['result']['complete'])
                self.assertNotIn('running', [v['status'] for v in record['result']['result']['experts'].values()])
            before = sum(x.execute_snapshot.call_count for x in self.adapters.values())
            self.app.run(self.caller, A, task, 'seo.audit')
            self.assertEqual(before, sum(x.execute_snapshot.call_count for x in self.adapters.values()))

    def test_real_processes_audit_lock_and_customer_separation(self):
        import multiprocessing
        context = multiprocessing.get_context('fork')
        ready, release = context.Event(), context.Event()
        original = self.adapters['seo.analyze'].execute_snapshot.side_effect
        def block(tenant, snapshots):
            ready.set()
            if not release.wait(15): raise RuntimeError('test release timeout')
            return original(tenant, snapshots)
        self.adapters['seo.analyze'].execute_snapshot.side_effect = block
        process = context.Process(target=self.app.run, args=(self.caller, A, 'parallel', 'seo.audit'))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.assertRaises(BlockingIOError):
                self.app.run(self.caller, A, 'parallel', 'seo.audit')
            self.adapters['seo.analyze'].execute_snapshot.side_effect = original
            other = self.app.run(self.caller, B, 'parallel', 'seo.audit')
            self.assertEqual(other['status'], 'succeeded')
            release.set(); process.join(15)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(self.app.get(self.caller, A, 'parallel')['status'], 'succeeded')
        finally:
            release.set()
            if process.is_alive(): process.terminate()
            process.join(10)

    def test_actual_utf8_byte_boundaries(self):
        for limit in (audit.MAX_EXPERT_BYTES, audit.MAX_AUDIT_BYTES):
            overhead = len(json.dumps({'x': ''}, ensure_ascii=False).encode())
            value = {'x': 'x' * (limit - overhead)}
            audit.bounded(value, limit)
            value['x'] += 'ş'
            with self.assertRaises(ValueError): audit.bounded(value, limit)

    def test_persistent_sqlite_commit_failure_recovers_as_interrupted(self):
        # Simulate failure of the outer error-record commit too: the exception
        # escapes, and the already durable running row is recovered on restart.
        from contextlib import contextmanager
        session = self.app.store.session
        @contextmanager
        def broken_session(customer):
            with session(customer) as db:
                class Connection:
                    commits = 0
                    def execute(self, *args): return db.execute(*args)
                    def commit(self):
                        self.commits += 1
                        if self.commits > 1: raise sqlite3.OperationalError('persistent commit failure')
                        db.commit()
                yield Connection()
        with mock.patch.object(self.app.store, 'session', side_effect=broken_session):
            with self.assertRaises(sqlite3.OperationalError):
                self.app.run(self.caller, A, 'persistent', 'seo.audit')
        recovered = self.app.get(self.caller, A, 'persistent')
        self.assertEqual(recovered['status'], 'interrupted')
        self.assertIsNone(recovered['result'])


class TestAuditLegacyCompatibility(unittest.TestCase):
    setUp = TestOfflineAudit.setUp

    def legacy(self):
        self.app.run(self.caller, A, 'legacy', 'seo.audit')
        old = hashlib.sha256(b'seo.audit\0' + b'\0'.join(
            n.encode() + b'\0' + raw for n, raw in sorted(self.sources[A].items()))).hexdigest()
        with self.app.store.session(A) as db:
            db.execute('UPDATE tasks SET input_digest=? WHERE task_id=?', (old, 'legacy')); db.commit()

    def cli(self, action):
        return subprocess.run([sys.executable, '-B', str(REPO / 'examples/offline-seo/isolation/coordinator.py'),
            '--state-root', str(self.root), '--customer', A, '--task-id', 'legacy',
            '--operation', 'seo.audit', '--action', action], capture_output=True, text=True, timeout=20)

    def test_proven_legacy_run_and_status_have_safe_turkish_error(self):
        self.legacy()
        for action in ('run', 'status'):
            result = self.cli(action)
            self.assertEqual(result.returncode, 5, result.stderr)
            body = json.loads(result.stdout)
            self.assertEqual(body['error'], 'audit_input_version_incompatible')
            self.assertIn('Yeni bir görev kimliği', body['message_tr'])
            self.assertNotIn(str(self.root), result.stdout)
        for adapter in self.adapters.values():
            self.assertEqual(adapter.execute_snapshot.call_count, 1)

    def test_corruption_and_foreign_customer_are_not_legacy(self):
        self.legacy()
        with self.app.store.session(A) as db:
            raw = db.execute('SELECT result FROM tasks WHERE task_id=?', ('legacy',)).fetchone()[0]
        for change in (lambda r: r.update(customer_id=B), lambda r: r.update(summary_tr='corrupt')):
            report = json.loads(raw); change(report['result'])
            with self.app.store.session(A) as db:
                db.execute('UPDATE tasks SET result=? WHERE task_id=?', (json.dumps(report), 'legacy')); db.commit()
            for action in ('run', 'status'):
                result = self.cli(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn(json.loads(result.stdout)['error'], {'audit_access_denied', 'audit_record_or_request_invalid'})

    def test_unknown_digest_or_unavailable_old_source_is_not_legacy(self):
        self.legacy()
        with self.app.store.session(A) as db:
            db.execute('UPDATE tasks SET input_digest=?', ('f'*64,)); db.commit()
        for action in ('run', 'status'):
            self.assertEqual(self.cli(action).returncode, 2)
        with mock.patch.object(DockerLab, '_customer_snapshots', side_effect=OSError('private path')):
            with self.assertRaises(OSError):
                self.app.get(self.caller, A, 'legacy')

    def test_changed_sources_conflict_and_all_standalone_digests_preserved(self):
        self.app.run(self.caller, A, 'legacy', 'seo.audit')
        changed = copy.deepcopy(self.sources); changed[A]['pages.json'] += b'\n'
        with mock.patch.object(DockerLab, '_customer_snapshots', return_value=changed):
            with self.assertRaises(c.TaskConflict): self.app.run(self.caller, A, 'legacy', 'seo.audit')
        for op in audit.OPERATIONS:
            expected = hashlib.sha256(op.encode()+b'\0'+b'\0'.join(
                n.encode()+b'\0'+raw for n,raw in sorted(self.sources[A].items()))).hexdigest()
            self.assertEqual(c.input_digest(op,self.sources[A]), expected)
