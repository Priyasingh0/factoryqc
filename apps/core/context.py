from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    """IP address and device serial of the request currently being handled."""

    ip_address: str | None = None
    device_serial: str = ""


_current = ContextVar("request_context", default=RequestContext())


def set_request_context(ctx):
    return _current.set(ctx)


def reset_request_context(token):
    _current.reset(token)


def get_request_context():
    return _current.get()
