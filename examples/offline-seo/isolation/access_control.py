"""Offline access prototype. Trusted local setup only; no login/network/JWT.

The host, provider and policy administrator are trusted. Opaque demo sessions
are process-local capabilities, not evidence of a real person's identity.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from threading import local
from dataclasses import dataclass
from enum import Enum
import hashlib
import math
import weakref
import time
import re
import os
from threading import RLock
from typing import Protocol

from coordinator import Coordinator, TrustedCaller
from runner import CUSTOMERS


class AccessDenied(PermissionError):
    def __init__(self):
        super().__init__('Erişim reddedildi.')


class ServiceUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('İşlem tamamlanamadı; güvenilir yönetici incelemesi gerekiyor.')


class ProcessBound:
    def _bind_process(self):
        self._pid = os.getpid()

    def _listen_cancel(self, callback):
        self._check_process()
        self._cancel_listeners = [r for r in self._cancel_listeners if r() is not None]
        self._cancel_listeners.append(weakref.WeakMethod(callback))

    def _notify_cancel(self, **scope):
        self._check_process()
        for ref in tuple(self._cancel_listeners):
            callback = ref()
            if callback is not None: callback(**scope)

    def _check_process(self):
        # Check BEFORE acquiring inherited locks: another thread may have owned
        # them at fork. Never attempt to repair a copied security registry.
        if os.getpid() != self._pid:
            raise AccessDenied()


@dataclass(frozen=True)
class VerifiedAccount:
    user_id: str
    identity: Identity
    generation: int


class Role(Enum):
    SYSTEM_ADMIN = 'system_admin'
    TENANT_ADMIN = 'tenant_admin'
    READER = 'reader'


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str


class IdentityProvider(Protocol):
    def authenticate(self, credential: object) -> VerifiedAccount:
        """Validate credentials; never accept caller-supplied identity/role claims.

        A future OIDC adapter must validate signature, issuer, audience, expiry,
        nonce/replay and revocation BEFORE binding to a stable account/generation.
        """
        ...


@dataclass(frozen=True)
class User:
    user_id: str
    identity: Identity
    system_admin: bool = False
    active: bool = True


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    active: bool = True


@dataclass(frozen=True)
class Membership:
    user_id: str
    tenant_id: str
    role: Role


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    action: str
    outcome: str
    actor_ref: str | None


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', value):
        raise ValueError('Invalid local identifier')


@dataclass(frozen=True, repr=False)
class _Session:
    account: VerifiedAccount
    expires_at: float


class OfflineIdentityProvider(ProcessBound):
    """Trusted fixture issuer, not real login. No JWT/password/serialized token.

    Absolute, non-sliding lifetime; process-local opaque object capabilities.
    Registry and clock configuration are trusted host inputs, never API fields.
    """
    def __init__(self, directory, *, lifetime_seconds=900, max_sessions=1000,
                 clock=time.monotonic):
        if (type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 3600
                or type(max_sessions) is not int or not 1 <= max_sessions <= 10000
                or not callable(clock)):
            raise ValueError('Invalid offline session policy')
        self._bind_process()
        self._directory = directory
        self._lock = RLock()
        self._sessions = {}
        self._cancel_listeners = []
        self._lifetime = lifetime_seconds
        self._capacity = max_sessions
        self._clock = clock
        self._last_time = None
        self._clock_failed = False

    def _now(self):
        # Called under the provider lock. A broken/backwards clock permanently
        # closes this provider; it must never extend existing credentials.
        try:
            if self._clock_failed:
                raise ValueError('Clock unavailable')
            now = self._clock()
            if (type(now) not in (float, int) or not math.isfinite(now) or now < 0
                    or self._last_time is not None and now < self._last_time):
                raise ValueError('Invalid session clock')
            self._last_time = now
            return now
        except Exception:
            self._clock_failed = True
            self._sessions.clear()
            raise AccessDenied() from None

    def issue(self, subject: str) -> object:
        """Trusted fixture setup ONLY; knowing a subject is not authentication."""
        self._check_process(); self._directory._check_process()
        _identifier(subject)
        directory = self._directory
        with directory.checked():
            directory._check_process()
            identity = Identity('offline-fixture', subject)
            user = next((u for u in directory._users.values() if u.identity == identity and u.active), None)
            if user is None:
                raise AccessDenied()
            binding = VerifiedAccount(user.user_id, identity, directory._generations[user.user_id])
            with self._lock:
                now = self._now()
                # Bounded sweep also drops disabled/rebound accounts. Policy is
                # locked before the provider, consistently with authenticate.
                for token, session in list(self._sessions.items()):
                    if now >= session.expires_at:
                        del self._sessions[token]
                        continue
                    try: directory._user(session.account)
                    except AccessDenied: del self._sessions[token]
                directory._user(binding)  # Recheck after the sweep, including store health.
                if len(self._sessions) >= self._capacity:
                    raise AccessDenied()
                token = object()
                self._sessions[token] = _Session(binding, now + self._lifetime)
                return token

    def revoke(self, token):
        self._check_process()
        if type(token) is not object:
            raise AccessDenied()
        with self._lock:
            self._sessions.pop(token, None)
        self._notify_cancel(session=token)

    def revoke_all(self, user_id):
        """Trusted local administrator only; no customer endpoint."""
        self._check_process()
        _identifier(user_id)
        with self._lock:
            for token, session in list(self._sessions.items()):
                if session.account.user_id == user_id:
                    del self._sessions[token]
        self._notify_cancel(account=user_id)

    def authenticate(self, credential):
        self._check_process(); self._directory._check_process()
        if type(credential) is not object:
            raise AccessDenied()
        directory = self._directory
        with directory.checked():
            directory._check_process()
            with self._lock:
                now = self._now()
                session = self._sessions.get(credential)
                if session is None:
                    raise AccessDenied()
                if now >= session.expires_at:
                    del self._sessions[credential]
                    raise AccessDenied()
                try: directory._user(session.account)
                except AccessDenied:
                    del self._sessions[credential]
                    raise
                return session.account


class AccessDirectory(ProcessBound):
    """In-memory trusted policy administration; not a customer-facing API.

    checked() bounds health reuse to one held policy lock in one thread.
    Task execution releases this lock; revocations signal scoped controls.
    """
    def __init__(self):
        self._bind_process()
        self._lock = RLock()
        self._users = {}
        self._generations = {}
        self._tenants = {}
        self._memberships = {}
        self._events = deque(maxlen=1000)
        self._sequence = 0
        self._health_scope = local()
        self._cancel_listeners = []

    @contextmanager
    def checked(self):
        """Health is shared only inside this thread's held policy lock."""
        self._check_process()
        with self._lock:
            self._check_process()
            depth = getattr(self._health_scope, 'depth', 0)
            self._health_scope.depth = depth + 1
            try: yield
            finally:
                self._health_scope.depth = depth
                if depth == 0: self._check_process()

    def set_user(self, user: User):
        self._check_process()
        if type(user) is not User or type(user.identity) is not Identity:
            raise ValueError('Invalid user')
        _identifier(user.user_id); _identifier(user.identity.subject)
        if (not isinstance(user.identity.issuer, str) or not 1 <= len(user.identity.issuer) <= 256
                or type(user.active) is not bool or type(user.system_admin) is not bool):
            raise ValueError('Invalid user identity or flags')
        with self._lock:
            if any(u.identity == user.identity and u.user_id != user.user_id for u in self._users.values()):
                raise ValueError('Identity already bound')
            previous = self._users.get(user.user_id)
            if previous is None:
                self._generations[user.user_id] = 1
            elif previous.identity != user.identity or previous.active != user.active:
                self._generations[user.user_id] += 1
            self._users[user.user_id] = user
            if previous is not None and (previous.identity != user.identity or previous.active != user.active):
                self._notify_cancel(account=user.user_id)

    def set_tenant(self, tenant: Tenant):
        self._check_process()
        if type(tenant) is not Tenant or tenant.tenant_id not in CUSTOMERS or type(tenant.active) is not bool:
            raise ValueError('Only fictional demo tenants are supported')
        with self._lock:
            self._tenants[tenant.tenant_id] = tenant
            if not tenant.active: self._notify_cancel(tenant=tenant.tenant_id)

    def set_membership(self, membership: Membership):
        self._check_process()
        if type(membership) is not Membership or membership.role not in (Role.TENANT_ADMIN, Role.READER):
            raise ValueError('Invalid tenant membership role')
        with self._lock:
            if membership.user_id not in self._users or membership.tenant_id not in self._tenants:
                raise ValueError('Unknown user or tenant')
            self._memberships[membership.user_id, membership.tenant_id] = membership
            if membership.role is Role.READER:
                self._notify_cancel(account=membership.user_id, tenant=membership.tenant_id)

    def remove_membership(self, user_id, tenant_id):
        self._check_process()
        with self._lock:
            self._memberships.pop((user_id, tenant_id), None)
            self._notify_cancel(account=user_id, tenant=tenant_id)

    def _event(self, action, outcome, user=None):
        self._check_process()
        self._sequence += 1
        ref = hashlib.sha256(user.user_id.encode()).hexdigest()[:16] if user else None
        self._events.append(AuditEvent(self._sequence, action, outcome, ref))

    def _user(self, identity):
        self._check_process()
        if type(identity) is not VerifiedAccount:
            raise AccessDenied()
        user = self._users.get(identity.user_id)
        if (user is None or not user.active or user.identity != identity.identity
                or self._generations.get(user.user_id) != identity.generation):
            raise AccessDenied()
        return user

    def _authorize(self, user, tenant_id, write):
        self._check_process()
        if not isinstance(tenant_id, str):
            raise AccessDenied()
        tenant = self._tenants.get(tenant_id)
        membership = self._memberships.get((user.user_id, tenant_id))
        if (tenant is None or not tenant.active or membership is None or
                write and not (user.system_admin or membership.role is Role.TENANT_ADMIN)):
            raise AccessDenied()


class AccessService(ProcessBound):
    """Only this facade is intended for a future authenticated transport.

    No customer method exposes raw coordinator, diagnostics or policy mutators.
    Python private attributes are not a sandbox against hostile host code.
    """
    def __init__(self, provider: IdentityProvider, directory: AccessDirectory, coordinator: Coordinator):
        self._bind_process()
        self._provider, self._directory, self._coordinator = provider, directory, coordinator
        self._controls_lock = RLock()
        self._controls = []
        directory._listen_cancel(self._cancel_matching)
        if isinstance(provider, OfflineIdentityProvider): provider._listen_cancel(self._cancel_matching)

    def _cancel_matching(self, *, account=None, tenant=None, session=None):
        self._check_process()
        with self._controls_lock:
            for control in self._controls:
                if (not control.finished and (account is None or control.account == account)
                        and (tenant is None or control.customer == tenant)
                        and (session is None or control._session is session)):
                    control.request()

    def _authorized(self, credential, tenant, write=False, maintenance=False):
        user = self._directory._user(self._provider.authenticate(credential))
        self._directory._authorize(user, tenant, write)
        if maintenance and not user.system_admin: raise AccessDenied()
        return user

    def _request(self, credential, tenant, task, action, operation=None):
        from execution_control import ExecutionControl, TaskCancelled
        self._check_process()
        directory = self._directory
        user = None
        with directory.checked():
            try:
                user = self._authorized(credential, tenant, action in {'submit', 'maintenance'}, action == 'maintenance')
            except Exception:
                directory._event(action, 'denied', user)
                raise AccessDenied() from None
        caller = TrustedCaller(user.user_id, frozenset({tenant}))
        control = None
        if action == 'submit':
            def authorize():
                self._check_process()
                with directory.checked(): self._authorized(credential, tenant, True)
            control = ExecutionControl(tenant, task, user.user_id, authorize)
            with self._controls_lock:
                active = [c for c in self._controls if not c.finished]
                if len(active) >= 1000: raise ServiceUnavailable()
                self._controls = active + [c for c in self._controls if c.finished][-128:]
                control._session = credential
                self._controls.append(control)
        try:
            if control is not None:
                with control.bind():
                    control.checkpoint()
                    record = self._coordinator.run(caller, tenant, task, operation)
            else:
                record = (self._coordinator.get(caller, tenant, task) if action == 'maintenance'
                          else self._coordinator.peek(caller, tenant, task))
        except TaskCancelled:
            with directory.checked(): directory._event(action, 'denied', user)
            raise AccessDenied() from None
        except Exception:
            with directory.checked(): directory._event(action, 'failed', user)
            raise ServiceUnavailable() from None
        finally:
            if control is not None:
                control.finish()
                with self._controls_lock:
                    self._controls = ([c for c in self._controls if not c.finished]
                                      + [c for c in self._controls if c.finished][-128:])
        with directory.checked():
            try:
                user = self._authorized(credential, tenant, action in {'submit', 'maintenance'}, action == 'maintenance')
            except Exception:
                directory._event(action, 'denied', user)
                raise AccessDenied() from None
            directory._event(action, 'allowed', user)
        if action == 'recommendation_source': return record
        if action == 'status': return {'task_id': record['task_id'], 'status': record['status']}
        return record['result'] if action == 'report' else {'task_id': record['task_id'], 'status': record['status']}

    def execution_states(self, credential, tenant):
        """Administrative diagnostics: active system admin AND tenant membership."""
        self._check_process()
        directory = self._directory
        user = None
        with directory.checked():
            try:
                user = self._authorized(credential, tenant)
                if not user.system_admin: raise AccessDenied()
            except Exception:
                directory._event('execution_states', 'denied', user)
                raise AccessDenied() from None
            with self._controls_lock:
                result = tuple({'customer':c.customer, 'task':c.task,
                                'execution_id':c.execution_id, 'owns_execution':c.owns_execution,
                                'state':('already_completed' if c.finished and c.state is None else c.state),
                                'finished':c.finished} for c in self._controls if c.customer == tenant)
            directory._event('execution_states', 'allowed', user)
            return result

    def submit(self, credential, tenant_id, task_id, operation='seo.audit'):
        return self._request(credential, tenant_id, task_id, 'submit', operation)

    def status(self, credential, tenant_id, task_id):
        return self._request(credential, tenant_id, task_id, 'status')

    def report(self, credential, tenant_id, task_id):
        return self._request(credential, tenant_id, task_id, 'report')

    def recommendations(self, credential, tenant_id, task_id):
        """Authorized read-only derivation; no approval or application endpoint."""
        record = self._request(credential, tenant_id, task_id, 'recommendation_source')
        # Bounded derivation does not need the shared policy lock. Recheck the
        # exact session/account and current membership before releasing data.
        try:
            from evidence_recommendations import from_record
            from runner import DockerLab
            result = from_record(record, tenant_id, DockerLab()._customer_snapshots()[tenant_id])
        except Exception:
            self._check_process()
            with self._directory._lock:
                self._directory._check_process()
                self._directory._event('recommendations', 'failed', None)
            raise ServiceUnavailable() from None
        self._check_process()
        directory = self._directory
        directory._check_process()
        with directory.checked():
            user = None
            try:
                user = directory._user(self._provider.authenticate(credential))
                directory._authorize(user, tenant_id, False)
            except Exception:
                directory._event('recommendations', 'denied', user)
                raise AccessDenied() from None
            directory._event('recommendations', 'allowed', user)
            return result

    def recover(self, credential, tenant_id, task_id):
        """Explicit persistent recovery: system admin AND tenant membership."""
        return self._request(credential, tenant_id, task_id, 'maintenance')

    def audit_events(self, credential):
        self._check_process()
        directory = self._directory
        directory._check_process()
        with directory.checked():
            user = None
            try:
                user = directory._user(self._provider.authenticate(credential))
                if not user.system_admin:
                    raise AccessDenied()
            except Exception:
                directory._event('audit_read', 'denied', user)
                raise AccessDenied() from None
            directory._event('audit_read', 'allowed', user)
            return tuple(directory._events)
