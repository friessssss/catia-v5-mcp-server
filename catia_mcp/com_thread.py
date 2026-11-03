"""COM setup for the single CATIA worker thread.

The worker is initialised as a single-threaded apartment once, when the
executor starts it, rather than lazily inside connect(): anything that
touches COM before the first connect (diagnostics, a dialog probe) then
runs on an initialised thread, and disconnect never has to uninitialise
an apartment whose proxies tool modules may still hold.

An OLE message filter is registered on that thread. Without one, a call
made while CATIA is inside a command, loading a document or running its
own modal loop fails at once with RPC_E_CALL_REJECTED /
RPC_E_SERVERCALL_RETRYLATER. The filter makes COM retry such calls for up
to REJECTED_RETRY_MS before giving up. Standard OLE practice for
automation clients; not yet field-validated against CATIA.
"""

from __future__ import annotations

import logging
from typing import ClassVar

logger = logging.getLogger("catia_mcp")

REJECTED_RETRY_MS = 30_000
_RETRY_DELAY_MS = 250

# HRESULTs of a call CATIA refused because it is busy.
RPC_E_CALL_REJECTED = -2147418111  # 0x80010001
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A
BUSY_HRESULTS = {RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER}

_SERVERCALL_ISHANDLED = 0
_SERVERCALL_REJECTED = 1
_SERVERCALL_RETRYLATER = 2
_PENDINGMSG_WAITDEFPROCESS = 2


class MessageFilter:
    """IMessageFilter: retry calls CATIA rejects while it is busy."""

    _public_methods_: ClassVar[list[str]] = [
        "HandleInComingCall",
        "RetryRejectedCall",
        "MessagePending",
    ]

    def HandleInComingCall(self, call_type, task_caller, tick_count, interface_info):
        return _SERVERCALL_ISHANDLED

    def RetryRejectedCall(self, task_callee, tick_count, reject_type):
        if reject_type in (_SERVERCALL_REJECTED, _SERVERCALL_RETRYLATER) and (
            tick_count < REJECTED_RETRY_MS
        ):
            return _RETRY_DELAY_MS
        return -1  # give up: the call fails with the rejection HRESULT

    def MessagePending(self, task_callee, tick_count, pending_type):
        return _PENDINGMSG_WAITDEFPROCESS


def init_worker() -> None:
    """ThreadPoolExecutor initializer for the CATIA worker."""
    try:
        import pythoncom
    except ImportError:
        return  # not Windows: the offline tests use fakes
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        from win32com.server.util import wrap

        filter_ = MessageFilter()
        filter_._com_interfaces_ = [pythoncom.IID_IMessageFilter]
        pythoncom.CoRegisterMessageFilter(wrap(filter_, pythoncom.IID_IMessageFilter))
    except Exception:
        logger.warning("Could not register the COM message filter", exc_info=True)


def hresult(error: BaseException) -> int | None:
    value = getattr(error, "hresult", None)
    if value is None and getattr(error, "args", None):
        first = error.args[0]
        value = first if isinstance(first, int) else None
    return value
