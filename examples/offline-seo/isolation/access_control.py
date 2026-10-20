"""Offline access prototype. Trusted local setup only; no login/network/JWT.

The host, provider and policy administrator are trusted. Opaque demo sessions
are process-local capabilities, not evidence of a real person's identity.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import hashlib
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


class OfflineIdentityProvider(ProcessBound):
    """Trusted fixture issuer ONLY. Never expose issue()/revoke() as login APIs.

    Tokens have no serializable identity claims. Object identity is checked by a
    private registry. Restart discards all tokens; another provider rejects them.
    """
    def __init__(self, directory):
        self._bind_process()
        self._directory = directory
        self._lock = RLock()
        self._sessions = {}

    def issue(self, subject: str) -> object:
        self._check_process(); self._directory._check_process()
        _identifier(subject)
        token = object()
        self._check_process()
        directory = self._directory
        directory._check_process()
        with directory._lock:
            identity = Identity('offline-fixture', subject)
            user = next((u for u in directory._users.values() if u.identity == identity and u.active), None)
            if user is None:
                raise AccessDenied()
            binding = VerifiedAccount(user.user_id, identity, directory._generations[user.user_id])
            with self._lock:
                self._sessions[token] = binding
        return token

    def revoke(self, token):
        self._check_process()
        with self._lock:
            self._sessions.pop(token, None)

    def authenticate(self, credential):
        self._check_process(); self._directory._check_process()
        # Reject arbitrary user objects before hashing/equality can execute code.
        if type(credential) is not object:
            raise AccessDenied()
        with self._lock:
            identity = self._sessions.get(credential)
            if identity is None:
                raise AccessDenied()
            return identity


class AccessDirectory(ProcessBound):
    """In-memory trusted policy administration; not a customer-facing API.

    The lock covers each request through completion. Revocation applies to the
    next admitted request, not retroactively to an already admitted Docker job.
    This deliberately serializes prototype requests in one process.
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

    def set_tenant(self, tenant: Tenant):
        self._check_process()
        if type(tenant) is not Tenant or tenant.tenant_id not in CUSTOMERS or type(tenant.active) is not bool:
            raise ValueError('Only fictional demo tenants are supported')
        with self._lock:
            self._tenants[tenant.tenant_id] = tenant

    def set_membership(self, membership: Membership):
        self._check_process()
        if type(membership) is not Membership or membership.role not in (Role.TENANT_ADMIN, Role.READER):
            raise ValueError('Invalid tenant membership role')
        with self._lock:
            if membership.user_id not in self._users or membership.tenant_id not in self._tenants:
                raise ValueError('Unknown user or tenant')
            self._memberships[membership.user_id, membership.tenant_id] = membership

    def remove_membership(self, user_id, tenant_id):
        self._check_process()
        with self._lock:
            self._memberships.pop((user_id, tenant_id), None)

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

    def _request(self, credential, tenant, task, action, operation=None):
        self._check_process()
        directory = self._directory
        directory._check_process()
        with directory._lock:
            user = None
            try:
                user = directory._user(self._provider.authenticate(credential))
                directory._authorize(user, tenant, action in {'submit', 'maintenance'})
                if action == 'maintenance' and not user.system_admin:
                    raise AccessDenied()
            except Exception:
                directory._event(action, 'denied', user)
                raise AccessDenied() from None
            caller = TrustedCaller(user.user_id, frozenset({tenant}))
            try:
                record = (self._coordinator.run(caller, tenant, task, operation) if action == 'submit'
                          else self._coordinator.get(caller, tenant, task) if action == 'maintenance'
                          else self._coordinator.peek(caller, tenant, task))
            except Exception:
                directory._event(action, 'failed', user)
                raise ServiceUnavailable() from None
            directory._event(action, 'allowed', user)
            if action == 'status':
                return {'task_id': record['task_id'], 'status': record['status']}
            return record['result'] if action == 'report' else {'task_id': record['task_id'], 'status': record['status']}

    def submit(self, credential, tenant_id, task_id, operation='seo.audit'):
        return self._request(credential, tenant_id, task_id, 'submit', operation)

    def status(self, credential, tenant_id, task_id):
        return self._request(credential, tenant_id, task_id, 'status')

    def report(self, credential, tenant_id, task_id):
        return self._request(credential, tenant_id, task_id, 'report')

    def recover(self, credential, tenant_id, task_id):
        """Explicit persistent recovery: system admin AND tenant membership."""
        return self._request(credential, tenant_id, task_id, 'maintenance')

    def audit_events(self, credential):
        self._check_process()
        directory = self._directory
        directory._check_process()
        with directory._lock:
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
