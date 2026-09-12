"""Bound synchronous transports, including SDKs with only inactivity timeouts."""

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import BoundedSemaphore, Thread
from time import monotonic
from typing import Callable

from app.llm.base import ChatResponse, LLMTransportError

_TRANSPORT_SLOTS = BoundedSemaphore(8)


def bounded_completion(call: Callable[[], ChatResponse], timeout: float) -> ChatResponse:
    deadline = monotonic() + timeout
    if not _TRANSPORT_SLOTS.acquire(timeout=max(0.0, timeout)):
        raise LLMTransportError("LLM transport capacity wait timed out.")
    future: Future[ChatResponse] = Future()

    def run() -> None:
        try:
            future.set_result(call())
        except BaseException as error:
            future.set_exception(error)
        finally:
            _TRANSPORT_SLOTS.release()

    try:
        Thread(target=run, name="llm-transport", daemon=True).start()
    except BaseException:
        _TRANSPORT_SLOTS.release()
        raise
    try:
        return future.result(timeout=max(0.0, deadline - monotonic()))
    except FutureTimeoutError as error:
        raise LLMTransportError("LLM attempt exceeded its wall-clock timeout.") from error
