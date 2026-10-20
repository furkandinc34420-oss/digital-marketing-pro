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
from typing import Protocol

from runner import CUSTOMERS, DockerLab, MAX_COMMAND_OUTPUT
from content_schema import validate_content_result


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


class TaskConflict(ValueError):
    """Existing task ID has a different immutable input digest."""


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
        lab = DockerLab()
        # Package the exact bytes fingerprinted by the coordinator, not reopened files.
        lab._customer_snapshots = lambda: snapshots
        with lab as active:
            return active.run(customer, self.action)


class ContentSeoSpecialist(SeoSpecialist):
    action = "content"


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


class Coordinator:
    def __init__(self, state_root):
        self.store = TaskStore(state_root)
        self._specialists: dict[str, Specialist] = {"seo.analyze": SeoSpecialist(), "seo.content": ContentSeoSpecialist()}

    @staticmethod
    def _check(caller, customer, task_id):
        caller.authorize(customer)
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
            raise ValueError("Invalid task ID")

    @staticmethod
    def _record(db, task_id, customer):
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("Task not found in this customer scope")
        record = dict(row)
        if record['result'] is not None and len(record['result'].encode('utf-8')) > MAX_COMMAND_OUTPUT:
            raise ValueError('Stored result exceeds byte budget')
        record["result"] = json.loads(record["result"]) if record["result"] is not None else None
        if record['status'] == 'succeeded' and record['result'] is None:
            raise ValueError('Successful task has no result')
        if record['result'] is not None:
            Coordinator._validate_result(record['result'], customer, record['operation'])
        return record

    @staticmethod
    def _validate_result(result, customer, operation="seo.analyze"):
        if not isinstance(result, dict) or not isinstance(result.get('result'), dict):
            raise ValueError('Invalid worker result envelope')
        report = result["result"]
        if operation == 'seo.content':
            validate_content_result(report, customer)
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

    def get(self, caller, customer, task_id):
        self._check(caller, customer, task_id)
        with self.store.session(customer) as db:
            return self._record(db, task_id, customer)

    def run(self, caller, customer, task_id, operation="seo.analyze"):
        self._check(caller, customer, task_id)
        if operation not in self._specialists:
            raise ValueError("Unsupported operation")
        snapshots = DockerLab()._customer_snapshots()
        digest = hashlib.sha256(operation.encode() + b'\0' + b'\0'.join(
            name.encode() + b'\0' + raw for name, raw in sorted(snapshots[customer].items()))).hexdigest()
        with self.store.session(customer) as db:
            existing = db.execute("SELECT operation,input_digest FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                if existing[0] != operation or existing[1] != digest:
                    raise TaskConflict("Task ID conflicts with operation or source data")
                return self._record(db, task_id, customer)
            if db.execute('SELECT count(*) FROM tasks').fetchone()[0] >= MAX_TASKS:
                raise CapacityError("Customer task count limit reached")
            # Reserve room for a maximal result, bounded diagnostic and SQLite overhead.
            used = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
            if used + MAX_COMMAND_OUTPUT + MAX_DIAGNOSTIC_BYTES + 65536 > MAX_DB_BYTES:
                raise CapacityError("Insufficient customer storage budget")
            db.execute("INSERT INTO tasks(task_id,operation,caller,status,input_digest) VALUES (?,?,?,'running',?)",
                       (task_id, operation, caller.caller_id, digest))
            db.commit()  # Must persist before Docker is invoked.
            try:
                specialist = self._specialists[operation]
                result = (specialist.execute_snapshot(customer, snapshots) if isinstance(specialist, SeoSpecialist)
                          else specialist.execute(customer))
                self._validate_result(result, customer, operation)
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > MAX_COMMAND_OUTPUT:
                    raise ValueError("Result exceeds byte limit")
            except Exception as exc:
                details = json.dumps({'customer_id': customer, 'task_id': task_id,
                                      'type': type(exc).__name__, 'message': str(exc)[:2400], 'message_truncated': len(str(exc)) > 2400}, ensure_ascii=False)
                db.execute('INSERT OR REPLACE INTO admin_errors VALUES (?,?)', (task_id, details))
                # Never persist raw exception messages (may contain another scope's data).
                db.execute("UPDATE tasks SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                           (type(exc).__name__ + ': execution or cleanup failed; operator review required', task_id))
                db.commit()
            else:
                db.execute("UPDATE tasks SET status='succeeded',result=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                           (encoded, task_id))
                db.commit()
            return self._record(db, task_id, customer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--customer", choices=CUSTOMERS, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--action", choices=("run", "status"), default="run")
    parser.add_argument("--operation", choices=("seo.analyze", "seo.content"), default="seo.analyze")
    args = parser.parse_args()
    # Local operator is the trust boundary. These flags are NOT authentication.
    caller = TrustedCaller("local-demo-operator", frozenset({args.customer}))
    coordinator = Coordinator(args.state_root)
    method = coordinator.run if args.action == "run" else coordinator.get
    try:
        record = (method(caller, args.customer, args.task_id, args.operation) if args.action == "run"
                  else method(caller, args.customer, args.task_id))
    except TaskConflict:
        print(json.dumps({'error': 'task_input_conflict'}))
        return 4
    except Exception:
        print(json.dumps({'error': 'coordinator_unavailable_or_invalid_request'}))
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return {'succeeded': 0, 'failed': 1, 'interrupted': 3}.get(record['status'], 2)


if __name__ == "__main__":
    sys.exit(main())
