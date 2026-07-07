from __future__ import annotations

import asyncio
import signal
import sys


def configure_windows_selector_event_loop_policy() -> None:
    """Use an event loop policy compatible with psycopg async connections on Windows."""
    if sys.platform != "win32":
        return

    policy_factory = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_factory is None:
        return

    current_policy = asyncio.get_event_loop_policy()
    if isinstance(current_policy, policy_factory):
        return

    asyncio.set_event_loop_policy(policy_factory())


def configure_windows_signal_compatibility() -> None:
    """Expose signal compatibility for libraries that assume POSIX asyncio support."""
    if sys.platform != "win32":
        return

    sigbreak = getattr(signal, "SIGBREAK", None)
    if not hasattr(signal, "SIGQUIT") and sigbreak is not None:
        signal.SIGQUIT = sigbreak

    add_signal_handler = asyncio.AbstractEventLoop.add_signal_handler
    if getattr(add_signal_handler, "_osce_windows_noop", False):
        return

    def add_signal_handler_noop(self: asyncio.AbstractEventLoop, sig: int, callback, *args) -> None:
        return None

    add_signal_handler_noop._osce_windows_noop = True
    asyncio.AbstractEventLoop.add_signal_handler = add_signal_handler_noop
