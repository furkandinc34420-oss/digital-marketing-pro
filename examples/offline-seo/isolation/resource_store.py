"""Trusted-host durable Docker provenance; not an authorization API or supervisor."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass
import os
import stat
import sqlite3
import fcntl
from pathlib import Path
import re
import tempfile
import uuid

_SCOPE = ContextVar('docker_resource_scope', default=None)
MAX_RECORDS = 10000
MAX_DB_BYTES = 64 * 1024 * 1024
META_SCHEMA = "CREATE TABLE resource_meta (kind TEXT PRIMARY KEY, customer TEXT NOT NULL, version INTEGER NOT NULL)"
STORE_KIND = "dmp-docker-ownership"
LABEL_PREFIX = 'dmp-owner-'
FIELDS = ('customer', 'task', 'execution', 'attempt', 'proof', 'kind')
SCHEMA = """CREATE TABLE resource_ownership (
 attempt TEXT PRIMARY KEY, customer TEXT NOT NULL, task TEXT NOT NULL,
 execution TEXT NOT NULL, proof TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 state TEXT NOT NULL, resource TEXT UNIQUE,
 CHECK(kind IN ('image','container')),
 CHECK(state IN ('not_sent','sent_unconfirmed','identified','removed')),
 CHECK((state IN ('identified','removed') AND resource IS NOT NULL)
    OR (state IN ('not_sent','sent_unconfirmed') AND resource IS NULL)))"""


class OwnershipError(RuntimeError):
    """Generic fail-closed host diagnostic; contains no source, secret or path."""


@dataclass(frozen=True)
class ResourceScope:
    root: Path
    customer: str
    task: str
    execution: str


@contextmanager
def resource_scope(root, customer, task, execution):
    token = _SCOPE.set(ResourceScope(Path(root), customer, task, execution))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_scope():
    return _SCOPE.get()


def default_root():
    # Persistent across process restart, NOT a durable backup: /tmp may be evicted.
    return Path(tempfile.gettempdir()) / ('dmp-offline-resources-v1-' + str(os.getuid()))


class ResourceStore:
    def __init__(self, root):
        self.root = Path(root)
        self.pid = os.getpid()
        self.poisoned = False

    @contextmanager
    def _files(self, customer):
        from coordinator import _private
        if customer not in ('demo-musteri', 'diger-demo'):
            raise OwnershipError('Unknown resource customer')
        if not self.root.is_absolute() or '..' in self.root.parts or self.root == Path('/'):
            raise OwnershipError('Invalid resource root')
        fds = [os.open('/', os.O_RDONLY | os.O_DIRECTORY)]
        try:
            for i, part in enumerate(self.root.parts[1:]):
                if i == len(self.root.parts[1:]) - 1:
                    try: os.mkdir(part, 0o700, dir_fd=fds[-1])
                    except FileExistsError: pass
                fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            _private(os.fstat(fds[-1]))
            def refuse_task_files(directory):
                for name in ('tasks.sqlite3','tasks.lock'):
                    try: os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError: continue
                    raise OwnershipError('Task or legacy store cannot be adopted')
            refuse_task_files(fds[-1])
            for tenant in ('demo-musteri','diger-demo'):
                try: other = os.open(tenant, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
                except FileNotFoundError: continue
                try:
                    _private(os.fstat(other)); refuse_task_files(other)
                finally: os.close(other)
            try: os.mkdir(customer, 0o700, dir_fd=fds[-1])
            except FileExistsError: pass
            fds.append(os.open(customer, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            directory = fds[-1]; _private(os.fstat(directory)); refuse_task_files(directory)
            def open_file(name, create=False):
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                created = False
                if create:
                    try:
                        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                        created = True
                    except FileExistsError: fd = os.open(name, flags, dir_fd=directory)
                else: fd = os.open(name, flags, dir_fd=directory)
                fds.append(fd); info = os.fstat(fd); _private(info)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_DB_BYTES:
                    raise OwnershipError('Unsafe resource state file')
                return fd, created
            # Reject wrong-role databases BEFORE creating lock/journal files.
            try: dbfd, new = open_file('resources.sqlite3')
            except FileNotFoundError: dbfd, new = None, True
            if dbfd is not None: self._preflight(dbfd, customer)
            lock, _ = open_file('resources.lock', True)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if dbfd is None: dbfd, new = open_file('resources.sqlite3', True)
            if not new: self._preflight(dbfd, customer)  # Recheck after admission.
            for name in ('resources.sqlite3-journal','resources.sqlite3-wal','resources.sqlite3-shm'):
                try: open_file(name)
                except FileNotFoundError: pass
            yield directory, dbfd, new
        finally:
            for fd in reversed(fds): os.close(fd)

    def _preflight(self, fd, customer):
        # Inspect bounded bytes in RAM: a wrong-role file cannot be recovered,
        # migrated or modified by opening an on-disk SQLite connection.
        before = os.fstat(fd); os.lseek(fd, 0, os.SEEK_SET)
        raw = bytearray()
        while len(raw) <= MAX_DB_BYTES:
            chunk = os.read(fd, min(65536, MAX_DB_BYTES + 1 - len(raw)))
            if not chunk: break
            raw.extend(chunk)
        after = os.fstat(fd)
        if not raw or len(raw) > MAX_DB_BYTES or (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise OwnershipError('Invalid resource database snapshot')
        with closing(sqlite3.connect(':memory:')) as snapshot:
            snapshot.deserialize(bytes(raw))
            snapshot.execute('PRAGMA trusted_schema=OFF')
            snapshot.execute('PRAGMA query_only=ON')
            self._schema(snapshot, customer)

    @staticmethod
    def _schema(db, customer):
        objects = {(r[0],r[1]) for r in db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')")}
        if objects != {('table','resource_meta'),('table','resource_ownership')}:
            raise OwnershipError('Wrong resource database type')
        definitions = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        if definitions != {'resource_meta':META_SCHEMA, 'resource_ownership':SCHEMA}:
            raise OwnershipError('Invalid resource schema')
        if db.execute('PRAGMA user_version').fetchone()[0] != 1 or [tuple(r) for r in db.execute('SELECT * FROM resource_meta')] != [(STORE_KIND,customer,1)]:
            raise OwnershipError('Invalid resource store marker')

    @contextmanager
    def _session(self, customer):
        if os.getpid() != self.pid or self.poisoned:
            raise OwnershipError('Resource ownership store unavailable')
        try:
            with self._files(customer) as (directory, fd, new):
                db = sqlite3.connect(f'/proc/self/fd/{directory}/resources.sqlite3', timeout=5)
                try:
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA trusted_schema=OFF')
                    db.execute('PRAGMA journal_mode=DELETE')
                    db.execute('PRAGMA synchronous=FULL')
                    db.execute(f"PRAGMA max_page_count={MAX_DB_BYTES // db.execute('PRAGMA page_size').fetchone()[0]}")
                    if new:
                        db.execute('BEGIN IMMEDIATE'); db.execute(META_SCHEMA); db.execute(SCHEMA)
                        db.execute('INSERT INTO resource_meta VALUES (?,?,1)', (STORE_KIND,customer))
                        db.execute('PRAGMA user_version=1'); db.commit()
                    self._schema(db,customer)
                    if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok']:
                        raise OwnershipError('Invalid resource ownership store')
                    records = db.execute('SELECT * FROM resource_ownership LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
                    if len(records) > MAX_RECORDS: raise OwnershipError('Resource record budget exceeded')
                    resources, attempts, proofs = set(), set(), set()
                    for row in records:
                        self._validate(dict(row),customer)
                        if row['attempt'] in attempts or row['proof'] in proofs or row['resource'] is not None and row['resource'] in resources:
                            raise OwnershipError('Conflicting resource records')
                        attempts.add(row['attempt']); proofs.add(row['proof'])
                        if row['resource'] is not None: resources.add(row['resource'])
                    yield db
                finally: db.close()
        except BaseException as exc:
            self.poisoned = True
            if isinstance(exc, (KeyboardInterrupt,SystemExit)): raise
            raise OwnershipError('Resource ownership operation failed; operator review required') from None

    @staticmethod
    def _validate(row, customer):
        if row['customer'] != customer or customer not in ('demo-musteri', 'diger-demo'):
            raise OwnershipError('Resource customer mismatch')
        if not isinstance(row['task'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', row['task']):
            raise OwnershipError('Invalid resource task')
        if any(not isinstance(row[k], str) or not re.fullmatch('[a-f0-9]{32}', row[k]) for k in ('execution','attempt','proof')):
            raise OwnershipError('Invalid resource binding')
        if row['kind'] not in ('image','container') or row['state'] not in ('not_sent','sent_unconfirmed','identified','removed'):
            raise OwnershipError('Invalid resource lifecycle')
        identified = row['state'] in ('identified','removed')
        pattern = r'sha256:[a-f0-9]{64}' if row['kind'] == 'image' else r'[a-f0-9]{64}'
        if identified != (row['resource'] is not None) or identified and not re.fullmatch(pattern, row['resource']):
            raise OwnershipError('Invalid resource identity')

    def intent(self, customer, task, execution, kind):
        row = dict(customer=customer, task=task, execution=execution,
                   attempt=uuid.uuid4().hex, proof=uuid.uuid4().hex, kind=kind,
                   state='not_sent', resource=None)
        self._validate(row, customer)
        with self._session(customer) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM resource_ownership').fetchone()[0] >= MAX_RECORDS:
                raise OwnershipError('Resource record budget exceeded')
            db.execute('INSERT INTO resource_ownership VALUES (:attempt,:customer,:task,:execution,:proof,:kind,:state,:resource)', row)
            self._commit(db)
        return row

    def _commit(self, db):
        db.commit()

    def transition(self, binding, before, after, resource=None):
        customer = binding['customer']
        allowed = {('not_sent','sent_unconfirmed'), ('sent_unconfirmed','not_sent'),
                   ('sent_unconfirmed','identified'), ('identified','removed')}
        if (before, after) not in allowed:
            raise OwnershipError('Invalid ownership transition')
        with self._session(customer) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM resource_ownership WHERE attempt=?', (binding['attempt'],)).fetchone()
            if row is None or any(row[k] != binding[k] for k in FIELDS) or row['state'] != before:
                raise OwnershipError('Resource binding conflict')
            value = resource if after in ('identified','removed') else None
            updated = dict(row); updated.update(state=after, resource=value)
            self._validate(updated, customer)
            db.execute('UPDATE resource_ownership SET state=?,resource=? WHERE attempt=? AND state=?',
                       (after, value, binding['attempt'], before))
            self._commit(db)
        return updated

    def records(self, customer):
        """Trusted local operator only, never exposed as a customer endpoint."""
        with self._session(customer) as db:
            return [dict(row) for row in db.execute('SELECT * FROM resource_ownership ORDER BY attempt')]

    @staticmethod
    def labels(binding):
        return {LABEL_PREFIX + k: binding[k] for k in FIELDS}

    def verify(self, binding, resource, config):
        # Docker inspect is an additional independent host observation, not the
        # worker response. A hostile daemon/host administrator remains trusted.
        if not isinstance(config, dict) or config.get('Id') != resource:
            raise OwnershipError('Docker resource identity mismatch')
        labels = config.get('Config', {}).get('Labels')
        if not isinstance(labels, dict) or any(labels.get(k) != v for k,v in self.labels(binding).items()):
            raise OwnershipError('Docker resource ownership mismatch')
        if binding['kind'] == 'container' and 'HostConfig' not in config or binding['kind'] == 'image' and 'HostConfig' in config:
            raise OwnershipError('Docker resource kind mismatch')
        rows = self.records(binding['customer'])
        stored = next((r for r in rows if r['attempt'] == binding['attempt']), None)
        if stored is None or any(stored[k] != binding[k] for k in FIELDS):
            raise OwnershipError('Stored resource binding mismatch')
        if stored['state'] == 'sent_unconfirmed':
            return self.transition(binding, 'sent_unconfirmed', 'identified', resource)
        if stored['state'] != 'identified' or stored['resource'] != resource:
            raise OwnershipError('Resource ownership is not active')
        return stored
