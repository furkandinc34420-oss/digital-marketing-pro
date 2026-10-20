"""Coordinator policy tests plus a real two-customer Docker round trip."""
import json
import subprocess
import select
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

MODULES = Path(__file__).resolve().parents[1] / 'examples/offline-seo/isolation'
sys.path.insert(0, str(MODULES))
import coordinator as c

A, B = c.CUSTOMERS


def output(customer):
    return {'result': {'customer_id': customer, 'synthetic': True,
                       'mode': 'proposal-only', 'connection': 'none'}}


class TestOfflineCoordinator(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.app = c.Coordinator(self.root)
        self.a = c.TrustedCaller('operator-a', frozenset({A}))
        self.b = c.TrustedCaller('operator-b', frozenset({B}))
        self.adapter = mock.Mock()
        self.adapter.execute.side_effect = output
        self.app._specialists['seo.analyze'] = self.adapter

    def test_two_customers_same_task_id_keep_separate_results_and_databases(self):
        for customer, caller in ((A, self.a), (B, self.b)):
            record = self.app.run(caller, customer, 'same-id')
            self.assertEqual(record['status'], 'succeeded')
            self.assertEqual(record['result']['result']['customer_id'], customer)
            self.assertTrue((self.root / customer / 'tasks.sqlite3').is_file())
        self.assertNotEqual(os.stat(self.root / A / 'tasks.sqlite3').st_ino,
                            os.stat(self.root / B / 'tasks.sqlite3').st_ino)

    def test_unauthorized_customer_cannot_read_or_run_or_create_state(self):
        for method in (self.app.get, self.app.run):
            with self.assertRaises(PermissionError):
                method(self.a, B, 'task')
        self.assertFalse(self.root.exists())
        self.adapter.execute.assert_not_called()

    def test_duplicate_survives_coordinator_restart(self):
        first = self.app.run(self.a, A, 'task')
        restarted = c.Coordinator(self.root)
        with mock.patch.object(c.SeoSpecialist, 'execute') as execute:
            self.assertEqual(first, restarted.run(self.a, A, 'task'))
        execute.assert_not_called()
        self.assertEqual(self.adapter.execute.call_count, 1)

    def test_failed_task_is_recorded_without_secret_and_never_retried(self):
        self.adapter.execute.side_effect = RuntimeError('OTHER-CUSTOMER-SECRET')
        record = self.app.run(self.a, A, 'task')
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('OTHER-CUSTOMER', json.dumps(record))
        self.app.run(self.a, A, 'task')
        self.assertEqual(self.adapter.execute.call_count, 1)

    def test_interrupted_execution_is_not_retried(self):
        self.adapter.execute.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.app.run(self.a, A, 'task')
        record = self.app.run(self.a, A, 'task')
        self.assertEqual(record['status'], 'interrupted')
        self.assertEqual(self.adapter.execute.call_count, 1)

    def test_durable_running_record_exists_before_worker_execution(self):
        def execute(customer):
            with sqlite3.connect(self.root / customer / 'tasks.sqlite3') as db:
                self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0], 'running')
            return output(customer)
        self.adapter.execute.side_effect = execute
        self.app.run(self.a, A, 'task')

    def test_live_lock_prevents_execution_and_false_recovery(self):
        with self.app.store.session(A) as db:
            db.execute("INSERT INTO tasks(task_id,operation,caller,status) VALUES ('live','seo.analyze','a','running')")
            db.commit()
            with self.assertRaises(BlockingIOError):
                self.app.run(self.a, A, 'live')
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0], 'running')
        self.adapter.execute.assert_not_called()

    def test_unknown_operations_and_path_task_ids_rejected_before_state_creation(self):
        with self.assertRaises(ValueError):
            self.app.run(self.a, A, 'task', 'ads.publish')
        for task in ('../escape', 'a/b', '', 'a' * 65):
            with self.assertRaises(ValueError):
                self.app.run(self.a, A, task)
        self.assertFalse(self.root.exists())

    def test_missing_task_cannot_be_read_from_other_customer(self):
        self.app.run(self.a, A, 'a-only')
        with self.assertRaises(KeyError):
            self.app.get(self.b, B, 'a-only')

    def test_foreign_or_live_write_result_rejected(self):
        for report in (output(B), {'result': dict(output(A)['result'], mode='apply')}):
            self.adapter.execute.return_value = report
            self.adapter.execute.side_effect = None
            record = self.app.run(self.a, A, 'task-' + str(self.adapter.execute.call_count))
            self.assertEqual(record['status'], 'failed')
            self.assertIsNone(record['result'])

    def test_symlink_root_tenant_and_database_rejected(self):
        target = Path(self.temp.name) / 'target'
        target.mkdir(mode=0o700)
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            self.app.run(self.a, A, 'task')
        self.root.unlink()
        self.root.mkdir(mode=0o700)
        tenant = self.root / A
        tenant.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            self.app.run(self.a, A, 'task')
        tenant.unlink()
        tenant.mkdir(mode=0o700)
        (tenant / 'tasks.sqlite3').symlink_to(target / 'foreign')
        with self.assertRaises(OSError):
            self.app.run(self.a, A, 'task')
        self.assertFalse((target / 'foreign').exists())

    def test_hardlinked_database_rejected_and_original_unchanged(self):
        self.app.run(self.a, A, 'task')
        database = self.root / A / 'tasks.sqlite3'
        before = database.read_bytes()
        os.link(database, self.root / 'linked.sqlite3')
        with self.assertRaises(PermissionError):
            self.app.get(self.a, A, 'task')
        self.assertEqual(before, database.read_bytes())

    def test_copied_foreign_database_rejected(self):
        self.app.run(self.a, A, 'task')
        (self.root / B).mkdir(mode=0o700)
        shutil.copyfile(self.root / A / 'tasks.sqlite3', self.root / B / 'tasks.sqlite3')
        (self.root / B / 'tasks.sqlite3').chmod(0o600)
        with self.assertRaises(PermissionError):
            self.app.get(self.b, B, 'task')

    def test_state_permissions_and_unsafe_sidecar(self):
        self.app.run(self.a, A, 'task')
        self.assertEqual((self.root.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.root / A / 'tasks.sqlite3').stat().st_mode & 0o777), 0o600)
        sidecar = self.root / A / 'tasks.sqlite3-journal'
        sidecar.symlink_to(self.root / A / 'tasks.sqlite3')
        with self.assertRaises(PermissionError):
            self.app.get(self.a, A, 'task')

    def test_adapter_uses_existing_isolation_without_fallback(self):
        with mock.patch.object(c, 'DockerLab') as lab:
            lab.return_value.__enter__.return_value.run.return_value = output(A)
            self.assertEqual(c.SeoSpecialist().execute(A), output(A))
            lab.return_value.__enter__.return_value.run.assert_called_once_with(A, 'analyze')
            lab.return_value.__exit__.assert_called_once()
        with mock.patch.object(c, 'DockerLab', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                c.SeoSpecialist().execute(A)


class TestOfflineCoordinatorDocker(unittest.TestCase):
    def test_two_real_isolated_seo_tasks_are_persisted_and_not_reexecuted(self):
        # Only an unavailable backend is skipped; worker/policy failures are failures.
        from runner import IsolationUnavailable
        try:
            c.DockerLab()._docker('version', '--format', '{{.Server.Version}}')
        except (IsolationUnavailable, RuntimeError, OSError) as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            app = c.Coordinator(Path(tmp) / 'state')
            caller = c.TrustedCaller('local-test', frozenset({A, B}))
            for customer in (A, B):
                record = app.run(caller, customer, 'real-seo')
                self.assertEqual(record['status'], 'succeeded', record)
                self.assertEqual(record['result']['result']['customer_id'], customer)
                self.assertEqual(record['result']['config']['network'], 'none')
                with mock.patch.object(c.SeoSpecialist, 'execute', side_effect=AssertionError('duplicate')):
                    self.assertEqual(app.run(caller, customer, 'real-seo'), record)


class TestCoordinatorHardening(unittest.TestCase):
    setUp = TestOfflineCoordinator.setUp

    def test_changed_input_conflicts_without_execution(self):
        self.app.run(self.a, A, 'task')
        snapshots = c.DockerLab()._customer_snapshots()
        snapshots[A]['profile.json'] += b'\n'
        with mock.patch.object(c.DockerLab, '_customer_snapshots', return_value=snapshots):
            with self.assertRaises(c.TaskConflict):
                self.app.run(self.a, A, 'task')
        self.assertEqual(self.adapter.execute.call_count, 1)

    def test_cleanup_details_only_in_operator_record(self):
        self.adapter.execute.side_effect = RuntimeError('cleanup failed; label=session-123; resource=foreign-secret')
        result = self.app.run(self.a, A, 'cleanup')
        self.assertNotIn('foreign-secret', json.dumps(result))
        admin = self.app.admin_diagnostics(A, 'cleanup')
        self.assertIn('session-123', admin['message'])
        self.assertEqual(admin['task_id'], 'cleanup')
        self.assertIsNone(self.app.admin_diagnostics(B, 'cleanup'))

    def test_task_and_storage_limits_block_worker(self):
        with mock.patch.object(c, 'MAX_TASKS', 0):
            with self.assertRaises(c.CapacityError):
                self.app.run(self.a, A, 'task')
        with mock.patch.object(c, 'MAX_DB_BYTES', 65536):
            with self.assertRaises(c.CapacityError):
                self.app.run(self.a, A, 'task')
        self.adapter.execute.assert_not_called()

    def test_missing_owner_existing_database_not_adopted(self):
        self.app.run(self.a, A, 'task')
        with sqlite3.connect(self.root / A / 'tasks.sqlite3') as db:
            db.execute('DELETE FROM owner')
        with self.assertRaises(PermissionError):
            self.app.get(self.a, A, 'task')

    def test_missing_owner_table_existing_database_not_adopted(self):
        self.app.run(self.a, A, 'task')
        with sqlite3.connect(self.root / A / 'tasks.sqlite3') as db:
            db.execute('DROP TABLE owner')
        with self.assertRaises(PermissionError):
            self.app.get(self.a, A, 'task')

    def test_stored_foreign_result_is_rejected(self):
        self.app.run(self.a, A, 'task')
        with sqlite3.connect(self.root / A / 'tasks.sqlite3') as db:
            db.execute('UPDATE tasks SET result=?', (json.dumps(output(B)),))
        with self.assertRaises(ValueError):
            self.app.get(self.a, A, 'task')

    def cli(self, task, action='status'):
        return subprocess.run([sys.executable, '-B', str(MODULES / 'coordinator.py'),
                               '--state-root', str(self.root), '--customer', A,
                               '--task-id', task, '--action', action], capture_output=True, text=True, timeout=20)

    def test_cli_failed_interrupted_and_success_exit_codes(self):
        self.app.run(self.a, A, 'ok')
        self.adapter.execute.side_effect = RuntimeError('secret')
        self.app.run(self.a, A, 'failed')
        self.adapter.execute.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.app.run(self.a, A, 'stopped')
        for task, code in (('ok', 0), ('failed', 1), ('stopped', 3), ('missing', 2)):
            result = self.cli(task)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertNotIn('secret', result.stdout + result.stderr)

    def test_cli_input_conflict_exit_code(self):
        self.app.run(self.a, A, 'task')
        with sqlite3.connect(self.root / A / 'tasks.sqlite3') as db:
            db.execute("UPDATE tasks SET input_digest='different'")
        result = self.cli('task', 'run')
        self.assertEqual(result.returncode, 4)
        self.assertIn('task_input_conflict', result.stdout)

    def start_blocked_process(self):
        script = '''
import sys, signal
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import coordinator as c
class Blocked:
    def execute(self, customer):
        print('READY', flush=True)
        signal.pause()
app=c.Coordinator(Path(sys.argv[2]))
app._specialists['seo.analyze']=Blocked()
app.run(c.TrustedCaller('process-a', frozenset({c.CUSTOMERS[0]})), c.CUSTOMERS[0], 'shared')
'''
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(MODULES), str(self.root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.addCleanup(cleanup)
        readable, _, _ = select.select([child.stdout], [], [], 15)
        self.assertTrue(readable, 'Child did not acquire task lock')
        self.assertEqual(child.stdout.readline().strip(), 'READY')
        return child

    def test_real_process_concurrency_does_not_execute_duplicate(self):
        self.start_blocked_process()
        result = self.cli('shared', 'run')
        self.assertEqual(result.returncode, 2)
        self.adapter.execute.assert_not_called()
        with sqlite3.connect(self.root / A / 'tasks.sqlite3') as db:
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0], 'running')

    def test_sigkill_restart_marks_interrupted_without_docker_or_retry(self):
        child = self.start_blocked_process()
        child.kill()
        child.communicate(timeout=10)
        result = self.cli('shared', 'run')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'interrupted')
        self.adapter.execute.assert_not_called()

    def test_docker_context_cleanup_failure_is_recorded(self):
        snapshots = c.DockerLab()._customer_snapshots()
        self.app._specialists['seo.analyze'] = c.SeoSpecialist()
        with mock.patch.object(c, 'DockerLab') as factory:
            factory.return_value._customer_snapshots.return_value = snapshots
            factory.return_value.__enter__.return_value.run.return_value = output(A)
            factory.return_value.__exit__.side_effect = RuntimeError('cleanup label=owned-session resource=owned-resource')
            result = self.app.run(self.a, A, 'cleanup-real-adapter')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('owned-session', json.dumps(result))
        self.assertIn('owned-session', self.app.admin_diagnostics(A, 'cleanup-real-adapter')['message'])

    def test_adapter_packages_fingerprinted_snapshot(self):
        snapshots = c.DockerLab()._customer_snapshots()
        with mock.patch.object(c, 'DockerLab') as factory:
            factory.return_value.__enter__.return_value.run.return_value = output(A)
            c.SeoSpecialist().execute_snapshot(A, snapshots)
            self.assertIs(factory.return_value._customer_snapshots(), snapshots)

    def test_oversized_existing_database_rejected(self):
        self.app.run(self.a, A, 'task')
        with mock.patch.object(c, 'MAX_DB_BYTES', 100):
            with self.assertRaises(c.CapacityError):
                self.app.get(self.a, A, 'task')
