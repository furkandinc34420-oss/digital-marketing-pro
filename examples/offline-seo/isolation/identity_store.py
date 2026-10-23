"""Single-owner durable demo policy. Trusted host administration only.

No sessions, passwords or customer reports are stored here. A lifetime flock
rejects competing owners; PID checks reject inherited objects before locks.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sqlite3
import stat

from access_control import AccessDirectory, AccessDenied, User, Identity, Tenant, Membership, Role
from coordinator import _private, _file

MAX_POLICY_BYTES = 16 * 1024 * 1024
MAX_USERS = 1000
APPLICATION_ID = 0x444D5049
SCHEMA_VERSION = 1
DDL = (
    "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=0))",
    "CREATE TABLE roles (name TEXT PRIMARY KEY CHECK(name IN ('system_admin','tenant_admin','reader')))",
    """CREATE TABLE users (user_id TEXT PRIMARY KEY NOT NULL,
       issuer TEXT NOT NULL, subject TEXT NOT NULL, system_admin INTEGER NOT NULL CHECK(system_admin IN (0,1)),
       active INTEGER NOT NULL CHECK(active IN (0,1)), generation INTEGER NOT NULL CHECK(generation>=1),
       UNIQUE(issuer,subject))""",
    """CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY NOT NULL
       CHECK(tenant_id IN ('demo-musteri','diger-demo')), active INTEGER NOT NULL CHECK(active IN (0,1)))""",
    """CREATE TABLE memberships (user_id TEXT NOT NULL REFERENCES users(user_id),
       tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
       role TEXT NOT NULL REFERENCES roles(name) CHECK(role IN ('tenant_admin','reader')),
       PRIMARY KEY(user_id,tenant_id))""",
    """CREATE TRIGGER immutable_user_id BEFORE UPDATE OF user_id ON users
       WHEN NEW.user_id != OLD.user_id BEGIN SELECT RAISE(ABORT,'Immutable account ID'); END""",
    """CREATE TRIGGER no_user_delete BEFORE DELETE ON users
       BEGIN SELECT RAISE(ABORT,'Disable accounts instead of deleting IDs'); END""",
)


def _signature(db):
    return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


def _expected_signature():
    reference = sqlite3.connect(':memory:')
    try:
        for sql in DDL:
            reference.execute(sql)
        return _signature(reference)
    finally:
        reference.close()


class PersistentAccessDirectory(AccessDirectory):
    """Same service interface; explicit trusted lifecycle and serialized writes.

    Caller must close before another owner opens the directory. No automatic
    memory-fixture import, schema migration, account deletion or DB repair.
    """
    def __init__(self, root):
        super().__init__()
        self._closed = False
        self._poisoned = False
        self._fds = []
        self._db = None
        self._ready = False
        self._anchors = []
        self.root = Path(root)
        if not self.root.is_absolute() or '..' in self.root.parts or self.root == Path('/'):
            raise ValueError('Use an absolute trusted identity state directory')
        try:
            self._fds.append(os.open('/', os.O_RDONLY | os.O_DIRECTORY))
            for i, part in enumerate(self.root.parts[1:]):
                if i == len(self.root.parts[1:]) - 1:
                    try: os.mkdir(part, 0o700, dir_fd=self._fds[-1])
                    except FileExistsError: pass
                parent = self._fds[-1]
                self._fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent))
                self._anchor(self._fds[-1], parent, part, 'directory', i == len(self.root.parts[1:]) - 1)
            directory = self._fds[-1]; _private(os.fstat(directory))
            self._fds.append(_file(directory, 'identity.lock'))
            self._anchor(self._fds[-1], directory, 'identity.lock', 'file', True)
            self._lock_fd = self._fds[-1]
            fcntl.flock(self._fds[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fd = os.open('identity.sqlite3', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                created = True
            except FileExistsError:
                fd = os.open('identity.sqlite3', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                created = False
            self._fds.append(fd); info = os.fstat(fd); _private(info)
            self._anchor(fd, directory, 'identity.sqlite3', 'file', True)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_POLICY_BYTES:
                raise PermissionError('Unsafe identity database')
            if not created and info.st_size == 0:
                raise ValueError('Existing empty identity database requires operator review')
            for name in ('identity.sqlite3-journal','identity.sqlite3-wal','identity.sqlite3-shm'):
                try: info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError: continue
                _private(info)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_POLICY_BYTES:
                    raise PermissionError('Unsafe identity journal')
            self._verify_ownership()
            self._db = sqlite3.connect(f'/proc/self/fd/{directory}/identity.sqlite3', check_same_thread=False, timeout=5)
            self._verify_ownership()
            self._db.execute('PRAGMA trusted_schema=OFF')
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA recursive_triggers=ON')
            if self._db.execute('PRAGMA recursive_triggers').fetchone()[0] != 1:
                raise ValueError('Recursive trigger protection unavailable')
            self._db.execute('PRAGMA synchronous=FULL')
            if created:
                self._db.execute('PRAGMA journal_mode=DELETE')
                self._db.execute('BEGIN IMMEDIATE')
                for sql in DDL: self._db.execute(sql)
                self._db.execute('INSERT INTO metadata VALUES(1,0)')
                self._db.executemany('INSERT INTO roles VALUES(?)', [(r.value,) for r in Role])
                self._db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                self._db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                self._commit()
            self._db.execute('BEGIN')
            self._validate_database()
            self._load()
            self._revision = self._db.execute('SELECT revision FROM metadata WHERE id=1').fetchone()[0]
            self._db.rollback()  # End the consistent, read-only opening snapshot.
            size = self._db.execute('PRAGMA page_size').fetchone()[0]
            self._db.execute(f'PRAGMA max_page_count={MAX_POLICY_BYTES // size}')
            self._ready = True
            self._check_process()
        except BaseException:
            self._dispose()
            self._closed = True
            raise

    def _check_process(self):
        super()._check_process()
        if self._closed or self._poisoned:
            raise AccessDenied()
        if self._ready:
            if getattr(self._health_scope, 'depth', 0):
                return  # Same thread still owns the checked policy lock.
            with self._lock:
                try:
                    self._verify_ownership()
                    if self._db.execute('SELECT revision FROM metadata WHERE id=1').fetchall() != [(self._revision,)]:
                        raise ValueError('Policy revision changed')
                except Exception:
                    self._poisoned = True
                    raise AccessDenied() from None

    def _anchor(self, fd, parent, name, kind, private):
        info = os.fstat(fd)
        self._anchors.append((fd,parent,name,kind,private,info.st_dev,info.st_ino,info.st_uid))

    def _verify_ownership(self):
        # Descriptor-relative comparisons also detect a renamed ancestor/root.
        # Reassert the lease without waiting; SQLite CAS remains necessary if
        # a path changes after this observation and before the commit syscall.
        for fd,parent,name,kind,private,device,inode,uid in self._anchors:
            opened = os.fstat(fd)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            for info in (opened,current):
                if (info.st_dev,info.st_ino,info.st_uid) != (device,inode,uid):
                    raise PermissionError('Policy file identity changed')
                if private: _private(info)
                if kind == 'directory':
                    if not stat.S_ISDIR(info.st_mode) or info.st_nlink < 1:
                        raise PermissionError('Policy directory changed')
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_POLICY_BYTES:
                    raise PermissionError('Policy state file changed')
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _validate_database(self):
        db = self._db
        if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                or db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                or _signature(db) != _expected_signature()):
            raise ValueError('Unsupported identity schema; explicit migration required')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Identity database integrity failure')
        metadata = db.execute('SELECT id,revision FROM metadata').fetchall()
        if len(metadata) != 1 or metadata[0][0] != 1 or type(metadata[0][1]) is not int or metadata[0][1] < 0:
            raise ValueError('Invalid identity metadata')
        roles = db.execute('SELECT name FROM roles').fetchall()
        if len(roles) != len(Role) or {r[0] for r in roles} != {r.value for r in Role}:
            raise ValueError('Invalid role catalog')

    def _load(self):
        # Reuse all existing configuration validation; never seed an admin.
        rows = self._db.execute('SELECT user_id,issuer,subject,system_admin,active,generation FROM users').fetchall()
        if len(rows) > MAX_USERS:
            raise ValueError('Identity user capacity exceeded')
        tenants = self._db.execute('SELECT tenant_id,active FROM tenants').fetchall()
        memberships = self._db.execute('SELECT user_id,tenant_id,role FROM memberships').fetchall()
        collections = ([row[0] for row in rows], [(row[1],row[2]) for row in rows],
                       [row[0] for row in tenants], [(row[0],row[1]) for row in memberships])
        if any(len(keys) != len(set(keys)) for keys in collections):
            raise ValueError('Duplicate policy key; refusing to merge records')
        for uid, issuer, subject, admin, active, generation in rows:
            if type(generation) is not int or generation < 1 or admin not in (0,1) or active not in (0,1):
                raise ValueError('Invalid durable user')
            AccessDirectory.set_user(self, User(uid, Identity(issuer,subject),bool(admin),bool(active)))
            self._generations[uid] = generation
        for tid, active in tenants:
            AccessDirectory.set_tenant(self, Tenant(tid,bool(active)))
        for uid, tid, role in memberships:
            AccessDirectory.set_membership(self, Membership(uid,tid,Role(role)))

    def _commit(self):
        self._verify_ownership()
        self._db.commit()
        self._verify_ownership()

    def _begin_write(self):
        self._db.execute('BEGIN IMMEDIATE')

    def _rollback(self):
        self._db.rollback()

    def _save(self):
        db = self._db
        if len(self._users) > MAX_USERS:
            raise ValueError('Identity user capacity exceeded')
        for uid, u in self._users.items():
            db.execute('''INSERT INTO users VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                issuer=excluded.issuer,subject=excluded.subject,system_admin=excluded.system_admin,
                active=excluded.active,generation=excluded.generation''',
                (uid,u.identity.issuer,u.identity.subject,int(u.system_admin),int(u.active),self._generations[uid]))
        for tid, t in self._tenants.items():
            db.execute('INSERT INTO tenants VALUES(?,?) ON CONFLICT(tenant_id) DO UPDATE SET active=excluded.active', (tid,int(t.active)))
        db.execute('DELETE FROM memberships')
        db.executemany('INSERT INTO memberships VALUES(?,?,?)',
                       [(m.user_id,m.tenant_id,m.role.value) for m in self._memberships.values()])

    def _change(self, method, *args):
        self._check_process()
        with self._lock:
            old = tuple(dict(getattr(self,n)) for n in ('_users','_generations','_tenants','_memberships'))
            names = ('_users','_generations','_tenants','_memberships')
            try:
                method(self,*args)
                if len(self._users) > MAX_USERS: raise ValueError('Identity capacity exceeded')
            except BaseException:
                for name, value in zip(names, old): setattr(self,name,value)
                raise
            try:
                self._begin_write()
                self._verify_ownership()
                changed = self._db.execute('UPDATE metadata SET revision=revision+1 WHERE id=1 AND revision=? AND revision<9223372036854775807', (self._revision,))
                if changed.rowcount != 1:
                    raise ValueError('Stale policy revision; refusing cached write')
                self._save()
                self._commit()
                self._revision += 1
            except BaseException:
                self._poisoned = True  # Unknown durable outcome: deny until reopen.
                try: self._rollback()
                finally:
                    for name, value in zip(names, old): setattr(self,name,value)
                raise

    def set_user(self, user): self._change(AccessDirectory.set_user,user)
    def set_tenant(self, tenant): self._change(AccessDirectory.set_tenant,tenant)
    def set_membership(self, membership): self._change(AccessDirectory.set_membership,membership)
    def remove_membership(self, user_id, tenant_id): self._change(AccessDirectory.remove_membership,user_id,tenant_id)

    def close(self):
        # Allow closing a poisoned owner, but never inherited handles in a child.
        if os.getpid() != self._pid: raise AccessDenied()
        with self._lock:
            self._closed = True
            self._dispose()

    def _dispose(self):
        try:
            if self._db is not None:
                self._db.close(); self._db = None
        finally:
            for fd in reversed(self._fds): os.close(fd)
            self._fds.clear()
