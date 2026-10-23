"""Process-local cooperative cancellation. No network or external write permits."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import uuid
from threading import RLock

_CURRENT = ContextVar('offline_execution_control', default=None)


class TaskCancelled(RuntimeError):
    pass


class StopFailed(RuntimeError):
    pass


class ExecutionControl:
    def __init__(self, customer, task, account, authorize):
        self.customer, self.task, self.account = customer, task, account
        self.execution_id = uuid.uuid4().hex
        self.owns_execution = False
        self._authorize = authorize
        self._pid = os.getpid()
        self._lock = RLock()
        self.state = None
        self.finished = False

    def _process(self):
        if os.getpid() != self._pid:
            raise PermissionError('Inherited execution control')

    def request(self):
        self._process()
        with self._lock:
            if self.finished:
                return 'already_completed' if self.state is None else self.state
            if self.state is None: self.state = 'requested'
            return self.state

    def checkpoint(self):
        self._process()
        with self._lock:
            if self.finished or self.state is not None:
                raise TaskCancelled('Execution closed or cancellation requested')
            authorize = self._authorize
        try: authorize()
        except Exception: self.request()
        with self._lock:
            if self.state is not None:
                raise TaskCancelled('Task authorization withdrawn or expired')

    def dispatch(self, launch):
        """Order request() against only OS process creation, never command wait.

        Authorization runs outside the control lock to preserve lock ordering.
        The private launch callback must only create the process/record dispatch;
        it must not acquire policy locks or run user code.
        """
        self.checkpoint()
        with self._lock:
            if self.finished or self.state is not None:
                raise TaskCancelled('Cancellation won process dispatch race')
            return launch()

    def stopping(self):
        self._process()
        with self._lock: self.state = 'stopping'

    def failed(self):
        self._process()
        with self._lock: self.state = 'stop_failed'

    def finish(self):
        self._process()
        with self._lock:
            if self.state in ('requested', 'stopping'): self.state = 'stopped'
            self.finished = True
            self._session = None
            self._authorize = None  # Do not retain credentials in terminal history.

    @contextmanager
    def bind(self):
        self._process()
        token = _CURRENT.set(self)
        try: yield self
        finally: _CURRENT.reset(token)


def current():
    return _CURRENT.get()


def checkpoint():
    control = current()
    if control is not None: control.checkpoint()


@dataclass(frozen=True)
class CriticalOperation:
    customer: str
    task: str
    operation: str
    input_digest: str
    exact_change_digest: str
    approval_id: str
    maximum_units: int


class OfflineSideEffectGate:
    """Future executor contract: offline prototype never issues a write permit.

    Future implementation MUST verify live authorization, cancellation, exact
    single-use human approval and limits immediately before dispatch. A check
    cannot atomically revoke an operation already submitted to an external API.
    """
    def check(self, operation: CriticalOperation):
        checkpoint()
        raise PermissionError('External side effects are disabled in the offline prototype')

@contextmanager
def publication():
    """Linearize cancellation versus durable terminal publication in this process."""
    control = current()
    if control is None:
        yield
        return
    control.checkpoint()
    with control._lock:
        if control.state is not None: raise TaskCancelled('Cancellation won publication race')
        try: yield
        except BaseException:
            control.failed()
            raise
        control.finished = True
        control._session = None
        control._authorize = None
