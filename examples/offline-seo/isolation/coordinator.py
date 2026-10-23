"""Local trusted coordinator; no authentication service or unisolated fallback."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import sys
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from threading import Lock
from typing import Protocol

from runner import CUSTOMERS, DockerLab, MAX_COMMAND_OUTPUT
from content_schema import validate_content_result
from technical_schema import validate_technical_result
import audit_report
from execution_control import checkpoint, current, TaskCancelled, publication


@dataclass(frozen=True)
class TrustedCaller:
    """Construct ONLY from trusted host configuration, never a request body."""
    caller_id: str
    customers: frozenset[str]

    def authorize(self, customer):
        if not self.caller_id or customer not in CUSTOMERS or customer not in self.customers:
            raise PermissionError("Caller is not authorized for this demo customer")


MAX_TASKS = 1000
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 16384


def input_digest(operation, files, *, manifest=False):
    """One canonical task fingerprint. Audit manifests contain raw-file SHA256s.

    Existing standalone fingerprints remain byte-for-byte compatible. Audit v1
    uses file hashes so durable reports can be checked without reopening sources.
    """
    if operation == 'seo.audit':
        files = files if manifest else {n: hashlib.sha256(raw).hexdigest() for n, raw in files.items()}
        if set(files) != {'profile.json', 'pages.json'} or any(
                not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in files.values()):
            raise ValueError('Invalid source manifest')
        values = ((n, v.encode('ascii')) for n, v in sorted(files.items()))
    else:
        if manifest:
            raise ValueError('Standalone tasks require raw bytes')
        values = sorted(files.items())
    return hashlib.sha256(operation.encode() + b'\0' + b'\0'.join(
        name.encode() + b'\0' + raw for name, raw in values)).hexdigest()


class TaskConflict(ValueError):
    """Existing task ID has a different immutable input digest."""


class LegacyAuditInput(ValueError):
    """Validated report and available raw sources prove the old fingerprint rule."""


class AuditScopeMismatch(PermissionError, ValueError):
    """Stored audit belongs to a different customer."""


class AuditRecordInvalid(ValueError):
    """Stored audit data is malformed; no raw details for the CLI."""


class TaskNotFound(KeyError):
    """No task exists in the authorized tenant scope."""


class CapacityError(RuntimeError):
    """Per-customer state budget exhausted."""


class Specialist(Protocol):
    """Trusted adapters receive an authorized scope; no user-selected imports."""
    def execute(self, customer: str) -> dict: ...


class SeoSpecialist:
    action = "analyze"

    def execute(self, customer):
        return self.execute_snapshot(customer, DockerLab()._customer_snapshots())

    def execute_snapshot(self, customer, snapshots):
        checkpoint()
        lab = DockerLab()
        # Package the exact bytes fingerprinted by the coordinator, not reopened files.
        lab._customer_snapshots = lambda: snapshots
        with lab as active:
            checkpoint()
            return active.run(customer, self.action)


class ContentSeoSpecialist(SeoSpecialist):
    action = "content"


class TechnicalSeoSpecialist(SeoSpecialist):
    action = "technical"


def _private(info):
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("State must be owned by this user and private")


def _file(directory, name):
    fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        _private(info)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("State file must be regular and single-link")
        return fd
    except BaseException:
        os.close(fd)
        raise


class TaskStore:
    """Trusted-host state; private directories do not isolate hostile same-UID code."""
    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("Use an absolute trusted state root")

    @contextmanager
    def session(self, customer):
        if customer not in CUSTOMERS:
            raise ValueError("Unknown demo customer")
        directories = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
        files = []
        connection = None
        try:
            # Descriptor-relative walk rejects symlinks in every component.
            parts = self.root.parts[1:]
            if not parts:
                raise ValueError("Do not use filesystem root for state")
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    try:
                        os.mkdir(part, 0o700, dir_fd=directories[-1])
                    except FileExistsError:
                        pass
                directories.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=directories[-1]))
            _private(os.fstat(directories[-1]))
            try:
                os.mkdir(customer, 0o700, dir_fd=directories[-1])
            except FileExistsError:
                pass
            directories.append(os.open(customer, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directories[-1]))
            directory = directories[-1]
            _private(os.fstat(directory))
            files.append(_file(directory, "tasks.lock"))
            # Nonblocking, process-wide tenant lock; never recover a live task.
            fcntl.flock(files[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
            files.append(_file(directory, "tasks.sqlite3"))
            # Journals also live inside the pinned private customer directory.
            for name in ("tasks.sqlite3-journal", "tasks.sqlite3-wal", "tasks.sqlite3-shm"):
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                _private(info)
                if info.st_size > MAX_DB_BYTES:
                    raise CapacityError("SQLite sidecar exceeds storage budget")
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PermissionError("Unsafe SQLite sidecar")
            new_database = os.fstat(files[-1]).st_size == 0
            if os.fstat(files[-1]).st_size > MAX_DB_BYTES:
                raise CapacityError("Task database exceeds storage budget")
            connection = sqlite3.connect(f"/proc/self/fd/{directory}/tasks.sqlite3", timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(f"PRAGMA max_page_count={MAX_DB_BYTES // connection.execute('PRAGMA page_size').fetchone()[0]}")
            if not new_database:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'owner' not in tables:
                    raise PermissionError("Existing database has no customer ownership")
            connection.execute("CREATE TABLE IF NOT EXISTS owner (customer TEXT PRIMARY KEY)")
            owners = connection.execute("SELECT customer FROM owner").fetchall()
            if owners and [row[0] for row in owners] != [customer]:
                raise PermissionError("Task database customer mismatch")
            if not owners and not new_database:
                raise PermissionError("Existing database has no customer ownership")
            if not owners:
                connection.execute("INSERT INTO owner VALUES (?)", (customer,))
            connection.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, operation TEXT NOT NULL, caller TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, error TEXT, input_digest TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(tasks)')}
            if 'input_digest' not in columns:
                raise PermissionError("Legacy database requires explicit operator migration")
            connection.execute("CREATE TABLE IF NOT EXISTS admin_errors (task_id TEXT PRIMARY KEY, details TEXT NOT NULL)")
            connection.execute("""UPDATE tasks SET status='interrupted',
                error='Previous execution ended without a durable outcome; no automatic retry',
                updated_at=CURRENT_TIMESTAMP WHERE status='running'""")
            connection.commit()
            yield connection
        finally:
            if connection is not None:
                connection.close()
            for fd in reversed(files + directories):
                os.close(fd)


    @contextmanager
    def readonly(self, customer):
        """Pinned existing files only; inspect a bounded SQLite copy in RAM.

        No SQLite connection to disk means no journal recovery, sidecar creation,
        migrations, or metadata updates. Fail closed on unfinished disk journals.
        """
        if customer not in CUSTOMERS or self.root == Path('/'):
            raise ValueError('Invalid customer state scope')
        fds = [os.open('/', os.O_RDONLY | os.O_DIRECTORY)]
        db = None
        try:
            for part in (*self.root.parts[1:], customer):
                fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            _private(os.fstat(fds[-2])); _private(os.fstat(fds[-1]))
            directory = fds[-1]
            for name in ('tasks.lock', 'tasks.sqlite3'):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                fds.append(fd)
                info = os.fstat(fd); _private(info)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_DB_BYTES:
                    raise PermissionError('Unsafe read-only state file')
                if name == 'tasks.lock':
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            for name in ('tasks.sqlite3-journal', 'tasks.sqlite3-wal', 'tasks.sqlite3-shm'):
                try:
                    os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise PermissionError('State requires explicit maintenance')
            raw = bytearray()
            while True:
                chunk = os.read(fds[-1], min(65536, MAX_DB_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_DB_BYTES:
                    raise CapacityError('Read-only state exceeds byte budget')
            db = sqlite3.connect(':memory:')
            db.deserialize(bytes(raw))
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA query_only=ON')
            if [row[0] for row in db.execute('SELECT customer FROM owner')] != [customer]:
                raise PermissionError('Read-only database customer mismatch')
            yield db
        finally:
            if db is not None:
                db.close()
            for fd in reversed(fds):
                os.close(fd)


class Coordinator:
    def __init__(self, state_root):
        self.store = TaskStore(state_root)
        self._pid = os.getpid()
        self._run_locks = {customer: Lock() for customer in CUSTOMERS}
        self._specialists: dict[str, Specialist] = {
            "seo.analyze": SeoSpecialist(), "seo.content": ContentSeoSpecialist(),
            "seo.technical": TechnicalSeoSpecialist()}

    @staticmethod
    def _check(caller, customer, task_id):
        caller.authorize(customer)
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
            raise ValueError("Invalid task ID")

    @staticmethod
    def _record(db, task_id, customer, *, persist=True):
        try:
            return Coordinator._read_record(db, task_id, customer, persist=persist)
        except (LegacyAuditInput, TaskNotFound, AuditScopeMismatch):
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise AuditRecordInvalid('Invalid stored task result') from exc

    @staticmethod
    def _read_record(db, task_id, customer, *, persist=True):
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound("Task not found in this customer scope")
        record = dict(row)
        if not persist and record['status'] == 'running':
            record['status'] = 'interrupted'
            record['error'] = 'Incomplete durable outcome; read-only preview, maintenance required'
        if record['result'] is not None and len(record['result'].encode('utf-8')) > MAX_COMMAND_OUTPUT:
            raise ValueError('Stored result exceeds byte budget')
        record["result"] = json.loads(record["result"]) if record["result"] is not None else None
        if record['status'] == 'succeeded' and record['result'] is None:
            raise ValueError('Successful task has no result')
        if record['operation'] == 'seo.audit' and record['result'] is not None:
            report = record['result']['result']
            if isinstance(report, dict) and report.get('customer_id') != customer:
                raise AuditScopeMismatch('Audit customer scope mismatch')
            audit_report.validate_audit(report, customer)
            if input_digest('seo.audit', report['source_hashes'], manifest=True) != record['input_digest']:
                # A mismatch alone never proves a legacy record. Require a valid
                # report, valid lifecycle, and exact old digest of available bytes.
                if (record['status'] == report['execution_state'] or
                        record['status'] in {'interrupted', 'failed'} and report['execution_state'] == 'running'):
                    files = DockerLab()._customer_snapshots()[customer]
                    hashes = {n: hashlib.sha256(raw).hexdigest() for n, raw in files.items()}
                    old = hashlib.sha256(b'seo.audit\0' + b'\0'.join(
                        n.encode() + b'\0' + raw for n, raw in sorted(files.items()))).hexdigest()
                    if hashes == report['source_hashes'] and old == record['input_digest']:
                        raise LegacyAuditInput('Legacy audit fingerprint is incompatible')
                raise ValueError('Stored audit source does not match task input')
            if record['status'] in {'interrupted', 'failed'} and report['execution_state'] == 'running':
                outcomes = {op: dict(item) for op, item in report['experts'].items()}
                for item in outcomes.values():
                    if item['status'] == 'running':
                        item['status'] = 'interrupted'
                report = audit_report.compose(customer, report['source_hashes'], report['page_catalog'], outcomes, record['status'])
                record['result'] = {'result': report}
                if persist:
                    db.execute('UPDATE tasks SET result=? WHERE task_id=?', (json.dumps(record['result'], ensure_ascii=False), task_id))
                    db.commit()
            if report['execution_state'] != record['status']:
                raise ValueError('Audit task and report status mismatch')
        if record['result'] is not None:
            Coordinator._validate_result(record['result'], customer, record['operation'])
        return record

    @staticmethod
    def _validate_result(result, customer, operation="seo.analyze"):
        if not isinstance(result, dict) or not isinstance(result.get('result'), dict):
            raise ValueError('Invalid worker result envelope')
        report = result["result"]
        if operation == 'seo.audit':
            audit_report.validate_audit(report, customer)
        elif operation == 'seo.content':
            validate_content_result(report, customer)
        elif operation == 'seo.technical':
            validate_technical_result(report, customer)
        elif operation != 'seo.analyze' or report.get('operation') not in (None, 'seo.analyze'):
            raise ValueError('Unexpected result operation')
        if (report.get("customer_id") != customer or report.get("synthetic") is not True
                or report.get("mode") != "proposal-only" or report.get("connection") != "none"):
            raise ValueError("Worker result identity or offline policy mismatch")
        for page in report.get('pages', []):
            if 'customer_id' in page and page['customer_id'] != customer:
                raise ValueError("Foreign page result")
        for proposal in report.get('proposals', []):
            if proposal.get('customer_id') != customer:
                raise ValueError("Foreign proposal result")

    def admin_diagnostics(self, customer, task_id):
        """Trusted local operator ONLY. Never expose through a customer endpoint."""
        with self.store.session(customer) as db:
            row = db.execute('SELECT details FROM admin_errors WHERE task_id=?', (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def peek(self, caller, customer, task_id):
        """No persistent recovery or state creation; conservative interruption view."""
        self._check(caller, customer, task_id)
        with self.store.readonly(customer) as db:
            return self._record(db, task_id, customer, persist=False)

    def get(self, caller, customer, task_id):
        self._check(caller, customer, task_id)
        with self.store.session(customer) as db:
            return self._record(db, task_id, customer)

    def run(self, caller, customer, task_id, operation="seo.analyze"):
        if os.getpid() != self._pid:
            # Raw TrustedCaller CLI has no session; retain its legacy fork use.
            # Inherited authenticated execution contexts must never be reused.
            if current() is not None: raise PermissionError('Inherited execution context')
            self._pid = os.getpid()
            self._run_locks = {tenant: Lock() for tenant in CUSTOMERS}
        self._check(caller, customer, task_id)
        lock = self._run_locks[customer]
        deadline = time.monotonic() + 180
        while not lock.acquire(timeout=0.1):
            checkpoint()
            if time.monotonic() >= deadline: raise CapacityError('Customer execution is busy')
        try:
            checkpoint()
            return self._run_once(caller, customer, task_id, operation)
        finally: lock.release()

    def _run_once(self, caller, customer, task_id, operation="seo.analyze"):
        self._check(caller, customer, task_id)
        if operation not in self._specialists and operation != "seo.audit":
            raise ValueError("Unsupported operation")
        checkpoint()
        snapshots = DockerLab()._customer_snapshots()
        digest = input_digest(operation, snapshots[customer])
        with self.store.session(customer) as db:
            checkpoint()
            existing = db.execute("SELECT operation,input_digest FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                if existing[0] != operation or existing[1] != digest:
                    if existing[0] == operation == 'seo.audit':
                        self._record(db, task_id, customer)  # Classify only proven legacy records.
                    raise TaskConflict("Task ID conflicts with operation or source data")
                record = self._record(db, task_id, customer)
                control = current()
                if control is not None:
                    with control._lock:
                        if control.state is not None: control.state = 'already_completed'
                        control.finished = True
                        control._session = None
                        control._authorize = None
                return record
            if db.execute('SELECT count(*) FROM tasks').fetchone()[0] >= MAX_TASKS:
                raise CapacityError("Customer task count limit reached")
            # Reserve room for a maximal result, bounded diagnostic and SQLite overhead.
            used = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
            if used + MAX_COMMAND_OUTPUT + MAX_DIAGNOSTIC_BYTES + 65536 > MAX_DB_BYTES:
                raise CapacityError("Insufficient customer storage budget")
            db.execute("INSERT INTO tasks(task_id,operation,caller,status,input_digest) VALUES (?,?,?,'running',?)",
                       (task_id, operation, caller.caller_id, digest))
            db.commit()  # Must persist before Docker is invoked.
            if current() is not None: current().owns_execution = True
            try:
                checkpoint()
                if operation == 'seo.audit':
                    result = self._run_audit(db, customer, task_id, snapshots)
                else:
                    specialist = self._specialists[operation]
                    result = (specialist.execute_snapshot(customer, snapshots) if isinstance(specialist, SeoSpecialist)
                              else specialist.execute(customer))
                checkpoint()
                self._validate_result(result, customer, operation)
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > MAX_COMMAND_OUTPUT:
                    raise ValueError("Result exceeds byte limit")
            except TaskCancelled:
                control = current()
                status = 'failed' if control is not None and control.state == 'stop_failed' else 'interrupted'
                db.execute("UPDATE tasks SET status=?,result=NULL,error=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                           (status, 'Cancellation: ' + ('stop_failed' if status == 'failed' else 'stopped'), task_id))
                db.commit()
            except Exception as exc:
                details = json.dumps({'customer_id': customer, 'task_id': task_id,
                                      'type': type(exc).__name__, 'message': str(exc)[:2400], 'message_truncated': len(str(exc)) > 2400}, ensure_ascii=False)
                db.execute('INSERT OR REPLACE INTO admin_errors VALUES (?,?)', (task_id, details))
                # Never persist raw exception messages (may contain another scope's data).
                db.execute("UPDATE tasks SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                           (type(exc).__name__ + ': execution or cleanup failed; operator review required', task_id))
                db.commit()
            else:
                final_status = result['result']['execution_state'] if operation == 'seo.audit' else 'succeeded'
                try:
                    with publication():
                        db.execute("UPDATE tasks SET status=?,result=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                                   (final_status, encoded, "Birleşik denetim eksik; uzman durumlarını inceleyin." if final_status == "failed" else None, task_id))
                        db.commit()
                except TaskCancelled:
                    db.execute("UPDATE tasks SET status='interrupted',result=NULL,error='Cancellation: stopped',updated_at=CURRENT_TIMESTAMP WHERE task_id=?", (task_id,))
                    db.commit()
            return self._record(db, task_id, customer)


    def _run_audit(self, db, customer, task_id, snapshots):
        # No recursive run() calls: the parent's tenant lock covers all three steps.
        from types import MappingProxyType
        snapshots = MappingProxyType({tenant: MappingProxyType(dict(files)) for tenant, files in snapshots.items()})
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshots[customer].items()}
        payload = json.loads(snapshots[customer]['pages.json'].decode('utf-8-sig'))
        catalog = {str(p['id']): p['url'] for p in payload['pages']}
        if len(catalog) != len(payload['pages']) or any(type(p['id']) is not int or p['id'] <= 0 for p in payload['pages']):
            raise ValueError('Invalid audit source page IDs')
        outcomes = {op: {'status': 'pending', 'result': None, 'error': None} for op in audit_report.OPERATIONS}
        diagnostics = []

        def persist_checkpoint():
            checkpoint()
            report = audit_report.compose(customer, hashes, catalog, outcomes, 'running')
            # Leave space for failure/interruption labels and bounded diagnostics.
            audit_report.bounded(report, audit_report.MAX_AUDIT_BYTES - 65536)
            db.execute('UPDATE tasks SET result=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?',
                       (json.dumps({'result': report}, ensure_ascii=False), task_id))
            db.commit()

        persist_checkpoint()
        for op in audit_report.OPERATIONS:
            checkpoint()
            try:
                checkpoint()
                outcomes[op] = {'status': 'running', 'result': None, 'error': None}
                persist_checkpoint()
                specialist = self._specialists[op]
                response = specialist.execute_snapshot(customer, snapshots)
                checkpoint()
                self._validate_result(response, customer, op)
                report = response['result']
                audit_report.validate_expert(op, report, customer, hashes, catalog)
                outcomes[op] = {'status': 'succeeded', 'result': report, 'error': None}
                persist_checkpoint()  # Only retain a result if the whole aggregate fits.
            except TaskCancelled:
                raise
            except Exception as exc:
                outcomes[op] = {'status': 'failed', 'result': None,
                                'error': 'Analiz tamamlanamadı; yönetici incelemesi gerekiyor.'}
                diagnostics.append({'operation': op, 'type': type(exc).__name__, 'message': str(exc)[:1000]})
                db.execute('INSERT OR REPLACE INTO admin_errors VALUES (?,?)',
                           (task_id, json.dumps({'expert_errors': diagnostics}, ensure_ascii=False)))
                persist_checkpoint()
        state = 'succeeded' if all(x['status'] == 'succeeded' for x in outcomes.values()) else 'failed'
        return {'result': audit_report.compose(customer, hashes, catalog, outcomes, state)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--customer", choices=CUSTOMERS, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--action", choices=("run", "status"), default="run")
    parser.add_argument("--operation", choices=("seo.analyze", "seo.content", "seo.technical", "seo.audit"), default="seo.analyze")
    args = parser.parse_args()
    # Local operator is the trust boundary. These flags are NOT authentication.
    caller = TrustedCaller("local-demo-operator", frozenset({args.customer}))
    coordinator = Coordinator(args.state_root)
    method = coordinator.run if args.action == "run" else coordinator.get
    try:
        record = (method(caller, args.customer, args.task_id, args.operation) if args.action == "run"
                  else method(caller, args.customer, args.task_id))
    except LegacyAuditInput:
        print(json.dumps({'error': 'audit_input_version_incompatible', 'message_tr':
            'Eski denetim kaydı yeni kaynak özeti kuralıyla uyumsuz. Yeni bir görev kimliğiyle yeniden analiz başlatın.'}, ensure_ascii=False))
        return 5
    except TaskConflict:
        print(json.dumps({'error': 'task_input_conflict'}))
        return 4
    except (PermissionError, TaskNotFound, ValueError, OSError, sqlite3.Error) as exc:
        code = ('audit_access_denied' if isinstance(exc, PermissionError) else
                'audit_task_not_found' if isinstance(exc, TaskNotFound) else
                'audit_record_or_request_invalid' if isinstance(exc, ValueError) else
                'audit_storage_or_runtime_unavailable')
        print(json.dumps({'error': code if args.operation == 'seo.audit' else 'coordinator_unavailable_or_invalid_request'}))
        return 2
    except Exception:
        print(json.dumps({'error': 'coordinator_unavailable_or_invalid_request'}))
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return {'succeeded': 0, 'failed': 1, 'interrupted': 3}.get(record['status'], 2)


if __name__ == "__main__":
    sys.exit(main())
