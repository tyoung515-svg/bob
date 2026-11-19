"""Single-use transport permits inherited by nested async calls in loop context."""
from contextlib import contextmanager
from contextvars import ContextVar

from .models import ExecutionError

required = ContextVar("execution_reservation_required", default=False)
permit = ContextVar("execution_dispatch_permit", default=None)


@contextmanager
def charged_call(attempt):
    token = permit.set({"attempt_id": attempt, "spent": False, "wire_spent": False})
    try:
        yield
    finally:
        permit.reset(token)


def consume_permit():
    if required.get():
        ticket = permit.get()
        if not ticket or ticket["spent"]:
            raise ExecutionError("unreserved_model_call", 409)
        ticket["spent"] = True


def consume_transport():
    assert_transport_permitted()
    if required.get():
        permit.get()["wire_spent"] = True


def assert_transport_permitted():
    if required.get():
        ticket = permit.get()
        if not ticket or not ticket["spent"] or ticket["wire_spent"]:
            raise ExecutionError("unreserved_model_call", 409)
