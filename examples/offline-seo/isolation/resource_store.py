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

LEASE_SCHEMA = "CREATE TABLE execution_leases (execution TEXT PRIMARY KEY, task TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL)"
RESULT_SCHEMA = "CREATE TABLE supervisor_results (attempt TEXT PRIMARY KEY REFERENCES resource_ownership(attempt), outcome TEXT NOT NULL)"
OUTCOMES = {'stopping', 'removed', 'review'}

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
        version = db.execute('PRAGMA user_version').fetchone()[0]
        extra = {('table','execution_leases'),('table','supervisor_results')} if version == 2 else set()
        if objects != {('table','resource_meta'),('table','resource_ownership')} | extra:
            raise OwnershipError('Wrong resource database type')
        definitions = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        expected = {'resource_meta':META_SCHEMA, 'resource_ownership':SCHEMA}
        if version == 2: expected.update(execution_leases=LEASE_SCHEMA, supervisor_results=RESULT_SCHEMA)
        if definitions != expected:
            raise OwnershipError('Invalid resource schema')
        if version not in (1,2) or [tuple(r) for r in db.execute('SELECT * FROM resource_meta')] != [(STORE_KIND,customer,version)]:
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
                        db.execute(LEASE_SCHEMA); db.execute(RESULT_SCHEMA)
                        db.execute('INSERT INTO resource_meta VALUES (?,?,2)', (STORE_KIND,customer))
                        db.execute('PRAGMA user_version=2'); db.commit()
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
                    if db.execute('PRAGMA user_version').fetchone()[0] == 1:
                        # Only an exact, previously validated v1 store is upgraded.
                        if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok']:
                            raise OwnershipError('Invalid legacy resource store')
                        db.execute('BEGIN IMMEDIATE')
                        db.execute(LEASE_SCHEMA); db.execute(RESULT_SCHEMA)
                        db.execute('UPDATE resource_meta SET version=2')
                        db.execute('PRAGMA user_version=2'); self._commit(db)
                    leases = db.execute('SELECT * FROM execution_leases LIMIT ?', (MAX_RECORDS+1,)).fetchall()
                    if len(leases) > MAX_RECORDS: raise OwnershipError('Lease budget exceeded')
                    for lease in leases:
                        if (not isinstance(lease['execution'],str) or not re.fullmatch('[a-f0-9]{32}',lease['execution'])
                                or not isinstance(lease['task'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',lease['task'])
                                or type(lease['device']) is not int or lease['device'] < 0
                                or type(lease['inode']) is not int or lease['inode'] <= 0):
                            raise OwnershipError('Invalid execution lease')
                    outcomes = db.execute('SELECT * FROM supervisor_results LIMIT ?', (MAX_RECORDS+1,)).fetchall()
                    ownership = {r['attempt']:r for r in records}
                    if len(outcomes)>MAX_RECORDS or any(
                            r['outcome'] not in OUTCOMES or r['attempt'] not in attempts
                            or r['outcome']=='removed' and ownership[r['attempt']]['state']!='removed'
                            for r in outcomes):
                        raise OwnershipError('Invalid supervisor outcome')
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

    def transition(self, binding, before, after, resource=None, *, supervisor_outcome=None):
        if supervisor_outcome is not None and (before,after,supervisor_outcome)!=('identified','removed','removed'):
            raise OwnershipError('Invalid atomic supervisor transition')
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
            if supervisor_outcome is not None:
                prior=db.execute('SELECT outcome FROM supervisor_results WHERE attempt=?',(binding['attempt'],)).fetchone()
                if resource!=row['resource'] or prior is None or prior[0]!='stopping':
                    raise OwnershipError('Supervisor completion conflicts with recorded decision')
            value = resource if after in ('identified','removed') else None
            updated = dict(row); updated.update(state=after, resource=value)
            self._validate(updated, customer)
            db.execute('UPDATE resource_ownership SET state=?,resource=? WHERE attempt=? AND state=?',
                       (after, value, binding['attempt'], before))
            if supervisor_outcome is not None:
                db.execute('UPDATE supervisor_results SET outcome=? WHERE attempt=?',
                           (supervisor_outcome,binding['attempt']))
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


    def supervisor_result(self, binding, outcome):
        if outcome not in OUTCOMES: raise OwnershipError('Invalid supervisor outcome')
        with self._session(binding['customer']) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM resource_ownership WHERE attempt=?',(binding['attempt'],)).fetchone()
            if row is None or any(row[k] != binding[k] for k in FIELDS):
                raise OwnershipError('Supervisor binding mismatch')
            db.execute('INSERT INTO supervisor_results VALUES (?,?) ON CONFLICT(attempt) DO UPDATE SET outcome=excluded.outcome',
                       (binding['attempt'],outcome))
            self._commit(db)

    def supervisor_outcomes(self, customer):
        with self._session(customer) as db:
            return dict(db.execute('SELECT attempt,outcome FROM supervisor_results'))

    def leases(self, customer):
        with self._session(customer) as db:
            return [dict(r) for r in db.execute('SELECT * FROM execution_leases')]

    @contextmanager
    def execution_lease(self, customer, task, execution):
        # No PID/clock-based death inference. No re-adoption of an execution ID.
        probe = dict(customer=customer,task=task,execution=execution,attempt='a'*32,
                     proof='b'*32,kind='container',state='not_sent',resource=None)
        self._validate(probe,customer)
        self.leases(customer)  # Initialize/validate schema before opening the lease file.
        fd = None
        try:
            with self._files(customer) as (directory, _, __):
                fd = os.open('execution-'+execution+'.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                info = os.fstat(fd)
            with self._session(customer) as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT count(*) FROM execution_leases').fetchone()[0]>=MAX_RECORDS:
                    raise OwnershipError('Lease budget exceeded')
                db.execute('INSERT INTO execution_leases VALUES (?,?,?,?)',(execution,task,info.st_dev,info.st_ino))
                self._commit(db)
            yield
        finally:
            if fd is not None: os.close(fd)

    @contextmanager
    def orphan_claim(self, customer, lease):
        from coordinator import _private
        if os.getpid()!=self.pid or self.poisoned:
            raise OwnershipError('Resource ownership store unavailable')
        fd = None
        try:
            with self._files(customer) as (directory, _, __):
                fd = os.open('execution-'+lease['execution']+'.lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                info=os.fstat(fd); _private(info)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                        or (info.st_dev,info.st_ino)!=(lease['device'],lease['inode'])):
                    raise OwnershipError('Execution lease identity changed')
                try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd);fd=None
            yield fd is not None
        finally:
            if fd is not None: os.close(fd)
