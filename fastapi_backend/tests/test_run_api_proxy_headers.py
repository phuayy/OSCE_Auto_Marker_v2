"""A1: uvicorn's own proxy-header trust must never come back on by accident.

``uvicorn.run(...)`` defaults ``proxy_headers=True`` with a fixed, trusted
peer of ``127.0.0.1`` — a rewrite of the ASGI scope's client address from
``X-Forwarded-For`` that runs *before* and independently of this app's own
``TRUSTED_PROXY_COUNT`` / ``TRUSTED_PROXY_IPS`` model
(``client_ip()`` in ``app/api/dependencies.py``). Since ``API_HOST`` defaults
to loopback and the documented nginx-on-the-same-VM deployment proxies
through loopback too, that default silently applies to nearly every real
deployment unless ``scripts/run_api.py`` explicitly opts out.

There is no way to assert this against a *running* server without actually
binding a port and driving a real TCP connection from a spoofed peer, which
is the kind of test nobody keeps green in CI. An AST check on the one call
site — the same discipline ``test_status_vocabulary.py`` applies to status
literals — is what actually stays enforced: it fails the moment someone
"simplifies" the ``uvicorn.run(...)`` call and drops the keyword, rather than
waiting for someone to notice a rate limiter behaving strangely behind a
loopback relay.
"""

from __future__ import annotations

import ast
from pathlib import Path

# fastapi_backend/tests -> repo root -> scripts/run_api.py
RUN_API_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_api.py"


def _find_uvicorn_run_call(tree: ast.AST) -> ast.Call:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "uvicorn"
        ):
            return node
    raise AssertionError("No uvicorn.run(...) call found in scripts/run_api.py")


def test_uvicorn_run_disables_its_own_proxy_header_trust() -> None:
    source = RUN_API_PATH.read_text(encoding="utf-8")
    call = _find_uvicorn_run_call(ast.parse(source, filename=str(RUN_API_PATH)))

    proxy_headers_kwargs = [kw for kw in call.keywords if kw.arg == "proxy_headers"]
    assert proxy_headers_kwargs, (
        "uvicorn.run(...) in scripts/run_api.py no longer passes proxy_headers explicitly. "
        "uvicorn defaults this to True with a trusted peer of 127.0.0.1, which rewrites the "
        "client address from X-Forwarded-For before app.api.dependencies.client_ip() ever runs "
        "— see the 'HTTP middleware stack' section of CLAUDE.md."
    )
    value = proxy_headers_kwargs[0].value
    assert isinstance(value, ast.Constant) and value.value is False, (
        "proxy_headers must be the literal False: this app's own trusted-proxy resolution "
        "(client_ip() in api/dependencies.py) is meant to be the only thing that ever honours "
        "X-Forwarded-For — see CLAUDE.md's 'HTTP middleware stack' section."
    )
