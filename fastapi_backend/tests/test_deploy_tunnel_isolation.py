"""Audit 2026-09-29 finding 7 (+ the drain-gate follow-up): ingress isolation
during a deploy must be verified, never assumed.

The automatic database restore is only safe while nothing external could have
written anything. That was inferred from ``$tunnelReopened -eq $false``, but
the tunnel stop discarded its own verification (``... | Out-Null``) and never
threw, so a tunnel that failed to stop left the site live through a window the
script then treated as closed. Static checks, in the style of
``test_deploy_assets.py``; the scripts' parse check lives there.
"""

from __future__ import annotations

import re
from pathlib import Path

DEPLOY_WINDOWS = Path(__file__).resolve().parents[2] / "deploy" / "windows"
DEPLOY = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
ROLLBACK = (DEPLOY_WINDOWS / "Rollback-Release.ps1").read_text(encoding="utf-8")

_DISCARDED_STOP_CHECK = re.compile(
    r"Wait-OsceServiceStatus\s+-Name\s+\$\S+\s+-Status\s+'Stopped'[^\n]*\|\s*Out-Null"
)


def _main_flow(text: str) -> str:
    """Everything after the last function definition: the script body."""
    last_function = max(m.start() for m in re.finditer(r"^function ", text, re.MULTILINE))
    body_start = text.index("\n}\n", last_function)
    return text[body_start:]


def test_no_service_stop_verification_is_discarded() -> None:
    for name, text in (("Deploy-Release.ps1", DEPLOY), ("Rollback-Release.ps1", ROLLBACK)):
        offenders = _DISCARDED_STOP_CHECK.findall(text)
        assert not offenders, f"{name} discards a stop verification: {offenders}"


def test_the_deploy_closes_the_tunnel_with_a_verified_stop() -> None:
    body = _main_flow(DEPLOY)
    assert "Stop-OsceServiceAndWait -Name $tunnelService" in body


def test_the_tunnel_is_closed_before_the_rollback_region_opens() -> None:
    """A tunnel that will not close means nothing has changed yet: the deploy
    aborts with the site as it was, rather than entering the try/catch whose
    failure branch may restore the database."""
    body = _main_flow(DEPLOY)
    close_idx = body.index("Stop-OsceServiceAndWait -Name $tunnelService")
    try_idx = body.index("$tunnelReopened = $false")
    assert close_idx < try_idx


def test_the_drain_gate_is_rechecked_after_the_tunnel_closes() -> None:
    """Work admitted between the first drain snapshot and the tunnel closing
    must be waited for too."""
    body = _main_flow(DEPLOY)
    close_idx = body.index("Stop-OsceServiceAndWait -Name $tunnelService")
    worker_stop_idx = body.index("Stop-OsceServiceAndWait -Name $config.workerServiceName")
    assert "-RequireDrained" in body[close_idx:worker_stop_idx] or "Wait-OsceDrained" in body[close_idx:worker_stop_idx]


def test_reopening_counts_as_reopened_before_start_service_is_called() -> None:
    """Start-Service can succeed late: a tunnel that reaches Running after the
    30 s wait gave up still served traffic. Isolation is lost the moment a
    start is attempted, not when it is confirmed."""
    body = _main_flow(DEPLOY)
    reopen_step = body[body.index("end of maintenance window") :]
    reopened_idx = reopen_step.index("$tunnelReopened = $true")
    start_idx = reopen_step.index("Start-Service -Name $tunnelService")
    assert reopened_idx < start_idx


def test_the_automatic_rollback_verifies_the_tunnel_is_closed_before_restoring() -> None:
    start = DEPLOY.index("function Invoke-FullRollback")
    rollback = DEPLOY[start : DEPLOY.index("\nfunction ", start + 1)]
    stop_idx = rollback.index("Stop-OsceServiceAndWait -Name $tunnelService")
    restore_idx = rollback.index("Invoke-OsceRestoreDatabase")
    assert stop_idx < restore_idx


def test_the_manual_rollback_verifies_every_stop_before_touching_anything() -> None:
    checkout_idx = ROLLBACK.index("& git checkout")  # the call, not a comment mentioning it
    before = ROLLBACK[:checkout_idx]
    for service in ("$tunnelService", "$config.workerServiceName", "$config.serviceName"):
        stop = re.search(
            rf"Stop-Service -Name {re.escape(service)}[^\n]*\n\s*if \(-not \(Wait-OsceServiceStatus -Name {re.escape(service)} -Status 'Stopped'",
            before,
        ) or (f"Stop-OsceServiceAndWait -Name {service}" in before)
        assert stop, f"Rollback-Release.ps1 does not verify {service} stopped before checkout/restore"
