"""Regression tests pinning the Windows host deployment assets under
``deploy/windows/`` and ``deploy/cloudflared/`` (see the task that created
them). These are static/textual checks — they do not execute PowerShell
business logic — but they stop the documented contracts (env keys existing
somewhere real, the hatchet job-queue backend, the tunnel-then-services
stop/start order, the SHA256SUMS verification, the junction-safe removal,
the operator hand-off branch) from silently drifting out of sync with the
scripts.

Run: cd fastapi_backend && uv run --no-sync pytest tests/test_deploy_assets.py -q
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.database.orm import OrmDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WINDOWS = REPO_ROOT / "deploy" / "windows"
DEPLOY_CLOUDFLARED = REPO_ROOT / "deploy" / "cloudflared"
CONFIG_PY = REPO_ROOT / "fastapi_backend" / "app" / "core" / "config.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
PROD_ENV_EXAMPLE = DEPLOY_WINDOWS / "production.env.example"
DEPLOYMENT_VM_MD = REPO_ROOT / "docs" / "deployment-vm.md"

# Keys that are intentionally not yet in .env.example (being added by a
# separate change) or are read directly by a third-party library rather than
# this app's own config.py.
KNOWN_EXTERNAL_KEYS = {"TRUSTED_CLIENT_IP_HEADER", "HF_HOME"}


def _parse_env_keys(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="module")
def prod_env_values() -> dict[str, str]:
    return _parse_env_keys(PROD_ENV_EXAMPLE)


@pytest.fixture(scope="module")
def dev_env_keys() -> set[str]:
    return set(_parse_env_keys(ENV_EXAMPLE).keys())


@pytest.fixture(scope="module")
def config_py_text() -> str:
    return CONFIG_PY.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) every key in production.env.example exists somewhere real
# ---------------------------------------------------------------------------


def test_production_env_keys_exist_in_dotenv_example_or_config(
    prod_env_values: dict[str, str], dev_env_keys: set[str], config_py_text: str
) -> None:
    missing: list[str] = []
    for key in prod_env_values:
        if key in dev_env_keys:
            continue
        if key in KNOWN_EXTERNAL_KEYS:
            continue
        if f'"{key}"' in config_py_text or f"'{key}'" in config_py_text:
            continue
        missing.append(key)
    assert not missing, (
        f"production.env.example keys not found in .env.example or config.py: {missing}"
    )


# ---------------------------------------------------------------------------
# (b) pinned production values
# ---------------------------------------------------------------------------


def test_production_env_pins_core_security_and_runtime_values(
    prod_env_values: dict[str, str],
) -> None:
    assert prod_env_values["API_HOST"] == "127.0.0.1"
    assert prod_env_values["SERVE_FRONTEND"] == "true"
    assert prod_env_values["ENVIRONMENT"] == "production"
    assert prod_env_values["DB_AUTO_MIGRATE"] == "false"
    assert prod_env_values["JOB_QUEUE_BACKEND"] == "hatchet"
    assert prod_env_values["PROTECT_MEDIA_ENDPOINTS"] == "true"
    assert prod_env_values["API_DOCS_ENABLED"] == "false"
    assert "127.0.0.1" in prod_env_values["TRUSTED_PROXY_IPS"]
    assert prod_env_values["APP_PUBLIC_URL"].startswith("https://")
    assert int(prod_env_values["UPLOAD_PART_SIZE_MB"]) < 100


def test_production_env_has_hatchet_settings(prod_env_values: dict[str, str]) -> None:
    for key in (
        "HATCHET_CLIENT_TOKEN",
        "HATCHET_CLIENT_HOST_PORT",
        "HATCHET_CLIENT_TLS_STRATEGY",
        "HATCHET_WORKER_NAME",
        "HATCHET_POSTGRES_PASSWORD",
    ):
        assert key in prod_env_values, f"{key} missing from production.env.example"
    assert prod_env_values["HATCHET_CLIENT_HOST_PORT"] == "localhost:7077"
    assert prod_env_values["HATCHET_CLIENT_TLS_STRATEGY"] == "none"
    # HATCHET_CLIENT_TOKEN must be a placeholder, not a real-looking secret.
    token_value = prod_env_values["HATCHET_CLIENT_TOKEN"]
    assert token_value == "" or "<" in token_value


def test_production_env_no_real_looking_secrets(prod_env_values: dict[str, str]) -> None:
    secret_like_keys = [
        key
        for key in prod_env_values
        if key in {"AUTH_SECRET", "CREDENTIAL_ENCRYPTION_KEY", "HATCHET_CLIENT_TOKEN", "HATCHET_POSTGRES_PASSWORD"}
        or key.endswith("_API_KEY")
        or key.endswith("_PASSWORD")
    ]
    assert secret_like_keys, "expected at least one secret-shaped key to check"
    for key in secret_like_keys:
        value = prod_env_values[key]
        assert value == "" or "<" in value, (
            f"{key} in production.env.example looks like a real secret value, not a placeholder: {value!r}"
        )


def test_production_env_database_url_uses_a_scheme_normalize_url_accepts(
    prod_env_values: dict[str, str],
) -> None:
    """F2: OrmDatabase._normalize_url only recognises postgres(ql),
    postgresql+psycopg (this repo installs psycopg, not asyncpg), sqlite and
    sqlite+aiosqlite; anything else — "postgresql+asyncpg://...", a typo —
    falls through to its final line, which treats the WHOLE URL STRING as a
    SQLite file path. That failure is silent: the app boots against a fresh,
    empty local SQLite file instead of refusing to start or reaching
    Postgres. Pinning the exact example value here (not just "contains
    postgresql") is deliberate: a fix that swaps the scheme back to something
    else recognised-but-wrong (sqlite, say) would still pass a looser check.
    """
    url = prod_env_values["APP_DATABASE_URL"]
    assert url.startswith("postgresql+psycopg://"), (
        f"production.env.example's APP_DATABASE_URL is not postgresql+psycopg://...: {url!r}"
    )
    normalized = OrmDatabase._normalize_url(url)
    assert not normalized.startswith("sqlite"), (
        "this URL normalised to a SQLite driver -- it would silently fall back to a local "
        f"SQLite file instead of reaching Postgres: {normalized!r}"
    )
    assert normalized.startswith("postgresql+psycopg://")


def test_deployment_vm_doc_recommends_a_scheme_normalize_url_accepts() -> None:
    """The docs' own example URL is exactly as load-bearing as the .env one —
    an operator copies it verbatim. See the sibling test above for why an
    unrecognised scheme fails silently rather than loudly."""
    text = DEPLOYMENT_VM_MD.read_text(encoding="utf-8")
    assert "postgresql+asyncpg" not in text, (
        "docs/deployment-vm.md recommends postgresql+asyncpg://, which this repo cannot use "
        "(psycopg is installed, not asyncpg) and which OrmDatabase._normalize_url silently "
        "mistakes for a SQLite file path"
    )
    example_urls = re.findall(r"postgresql\+\w+://\S+", text)
    assert example_urls, "expected at least one postgresql+<driver>:// example URL in the doc"
    for url in example_urls:
        normalized = OrmDatabase._normalize_url(url.rstrip("`.,)"))
        assert not normalized.startswith("sqlite"), f"{url!r} normalised to a SQLite driver"


def test_no_asyncpg_url_scheme_anywhere_under_deploy_or_docs() -> None:
    """Broader net: grep the whole deploy surface for the *scheme*
    ("postgresql+asyncpg://"), not just the two files already known to have
    used it as a live example, so a third copy pasted from an older example
    does not slip back in unnoticed. Deliberately not a bare "asyncpg"
    substring search: this module's own fix comments above now name
    "asyncpg" in prose, explaining why not to use it, and a blanket word-ban
    would flag its own warning."""
    searched = list(DEPLOY_WINDOWS.rglob("*")) + [DEPLOYMENT_VM_MD, ENV_EXAMPLE, PROD_ENV_EXAMPLE]
    offenders = []
    for path in searched:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "postgresql+asyncpg://" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"'postgresql+asyncpg://' still used as a live example in: {offenders}"


# ---------------------------------------------------------------------------
# (c) cloudflared config
# ---------------------------------------------------------------------------


def test_cloudflared_config_ingress_rules(prod_env_values: dict[str, str]) -> None:
    yaml = pytest.importorskip("yaml")
    config_path = DEPLOY_CLOUDFLARED / "config.yml.example"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    ingress = data["ingress"]
    assert ingress, "ingress list must not be empty"
    assert ingress[-1]["service"] == "http_status:404", "last ingress rule must be the catch-all 404"

    api_port = prod_env_values.get("API_PORT", "8787")
    for rule in ingress[:-1]:
        service = rule["service"]
        assert re.match(rf"^http://(127\.0\.0\.1|localhost):{re.escape(api_port)}$", service), (
            f"ingress rule service '{service}' does not point at loopback:{api_port}"
        )


# ---------------------------------------------------------------------------
# (d) every .ps1/.psm1 parses cleanly
# ---------------------------------------------------------------------------


def _find_powershell() -> str | None:
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


@pytest.mark.parametrize(
    "script_path",
    sorted((DEPLOY_WINDOWS).glob("*.ps1")) + sorted((DEPLOY_WINDOWS).glob("*.psm1")),
    ids=lambda p: p.name,
)
def test_powershell_scripts_parse_without_errors(script_path: Path) -> None:
    exe = _find_powershell()
    if exe is None:
        pytest.skip("no powershell/pwsh available on this machine")
    command = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script_path.as_posix()}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } else { exit 0 }"
    )
    result = subprocess.run(
        [exe, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{script_path.name} failed to parse:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# (e) WinSW template + Install-OsceService.ps1 mapping
# ---------------------------------------------------------------------------


def test_winsw_template_shape() -> None:
    template = (DEPLOY_WINDOWS / "osce-marker-service.xml.template").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in template
    assert "{{SCRIPT}}" in template, "template must be parameterised per-service, not hardcode one script"
    match = re.search(r"<stoptimeout>(\d+)\s*sec</stoptimeout>", template)
    assert match, "stoptimeout not found in WinSW template"
    assert int(match.group(1)) >= 15


def test_install_service_never_builds_a_path_under_repo_dir() -> None:
    """F6: the generated WinSW XML + exe copy used to land at
    repoDir\\deploy-service\\<id> — inside the checkout, not gitignored — so
    Test-OsceGitTreeClean (git status --porcelain, which lists untracked
    files) rejected every subsequent deploy on that host, permanently, from
    the moment Install-OsceService.ps1 first ran. It must resolve its
    per-service directory from a servicesDir host-config key (with a sensible
    default OUTSIDE repoDir), never by joining onto $config.repoDir."""
    text = (DEPLOY_WINDOWS / "Install-OsceService.ps1").read_text(encoding="utf-8")
    assert "servicesDir" in text
    assert "deploy-service" not in text
    assert "Join-Path $config.repoDir" not in text, (
        "Install-OsceService.ps1 must not build any path by joining onto $config.repoDir"
    )


def test_host_config_example_declares_services_dir_outside_repo_dir() -> None:
    import json

    config = json.loads((DEPLOY_WINDOWS / "host.config.example.json").read_text(encoding="utf-8"))
    assert "servicesDir" in config
    repo_dir = config["repoDir"].rstrip("\\")
    services_dir = config["servicesDir"].rstrip("\\")
    assert not services_dir.startswith(repo_dir + "\\"), (
        f"servicesDir ({services_dir!r}) must not live under repoDir ({repo_dir!r})"
    )


def test_install_service_script_maps_both_services_to_their_entrypoints() -> None:
    text = (DEPLOY_WINDOWS / "Install-OsceService.ps1").read_text(encoding="utf-8")
    assert "scripts\\run_api.py" in text
    assert "scripts\\run_hatchet_worker.py" in text
    assert "workerServiceName" in text


# ---------------------------------------------------------------------------
# (f) load-bearing calls + ordering pins
# ---------------------------------------------------------------------------


def test_deploy_release_contains_load_bearing_calls() -> None:
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    module_text = (DEPLOY_WINDOWS / "OsceDeploy.psm1").read_text(encoding="utf-8")
    combined = text + "\n" + module_text

    assert "--require-drained" in combined
    assert "backup_database.py" in combined
    assert "alembic" in combined and "upgrade" in combined and "head" in combined
    assert "--frozen" in text
    assert "--no-dev" in text
    assert "SHA256SUMS" in text
    assert "Get-FileHash" in text


def test_junction_removal_never_uses_remove_item_recurse() -> None:
    module_text = (DEPLOY_WINDOWS / "OsceDeploy.psm1").read_text(encoding="utf-8")
    assert "cmd /c rmdir" in module_text, "junction removal must go through rmdir, not Remove-Item -Recurse"
    # Guard against the destructive alternative creeping back in anywhere the
    # junction path variables are used.
    for forbidden in ("Remove-Item -LiteralPath $LinkPath -Recurse", "Remove-Item $LinkPath -Recurse"):
        assert forbidden not in module_text


def test_deploy_release_stops_tunnel_before_api_and_worker() -> None:
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    # Find the main deploy try-block's stop sequence (not the rollback
    # helper's, which has its own copy in the same order for the same
    # reason) by looking at first occurrences in document order.
    tunnel_stop_idx = text.index(f"Stop-Service -Name $tunnelService")
    worker_stop_idx = text.index("Stop-OsceServiceAndWait -Name $config.workerServiceName")
    api_stop_idx = text.index("Stop-OsceServiceAndWait -Name $config.serviceName")
    assert tunnel_stop_idx < worker_stop_idx < api_stop_idx, (
        "expected stop order: tunnel, then worker, then API"
    )


def test_deploy_release_starts_api_then_worker_then_tunnel() -> None:
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    # Restrict to the main success path (before the catch block) so this
    # doesn't accidentally match the rollback helper's own (differently
    # ordered by necessity) start sequence.
    catch_idx = text.index("} catch {")
    main_path = text[:catch_idx]
    api_start_idx = main_path.index("Start-OsceServiceAndWait -Name $config.serviceName")
    worker_start_idx = main_path.index("Start-OsceServiceAndWait -Name $config.workerServiceName")
    tunnel_start_idx = main_path.index(f"Start-Service -Name $tunnelService")
    assert api_start_idx < worker_start_idx < tunnel_start_idx, (
        "expected start order: API, then worker, then tunnel"
    )


def test_deploy_release_has_needs_operator_branch() -> None:
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    assert "needs_operator" in text
    assert "Invoke-NeedsOperatorStop" in text
    assert "tunnelReopened" in text


def test_start_osce_stack_starts_api_before_worker_and_only_two_hatchet_services() -> None:
    text = (DEPLOY_WINDOWS / "Start-OsceStack.ps1").read_text(encoding="utf-8")
    api_idx = text.index("Start-Service -Name $config.serviceName")
    worker_idx = text.index("Start-Service -Name $config.workerServiceName")
    assert api_idx < worker_idx, "Start-OsceStack.ps1 must start the API before the worker"

    module_text = (DEPLOY_WINDOWS / "OsceDeploy.psm1").read_text(encoding="utf-8")
    compose_call = re.search(
        r"Arguments @\('compose', '-f', \$composeFile, 'up', '-d', ([^)]+)\)", module_text
    )
    assert compose_call, "Start-OsceHatchetStack compose invocation not found in OsceDeploy.psm1"
    services_arg = compose_call.group(1)
    assert "'hatchet-postgres'" in services_arg
    assert "'hatchet-lite'" in services_arg
    assert "app-postgres" not in services_arg


def test_stop_osce_stack_stops_worker_before_api() -> None:
    text = (DEPLOY_WINDOWS / "Stop-OsceStack.ps1").read_text(encoding="utf-8")
    worker_idx = text.index("Stop-Service -Name $config.workerServiceName")
    api_idx = text.index("Stop-Service -Name $config.serviceName")
    assert worker_idx < api_idx, "Stop-OsceStack.ps1 must stop the worker before the API"


def test_deploy_windows_scripts_are_pure_ascii() -> None:
    scripts = sorted(DEPLOY_WINDOWS.glob("*.ps1")) + sorted(DEPLOY_WINDOWS.glob("*.psm1"))
    assert scripts, "expected at least one .ps1/.psm1 under deploy/windows"
    bad: list[str] = []
    for script in scripts:
        raw = script.read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            bad.append(f"{script.name}: {exc}")
    assert not bad, (
        "deploy/windows scripts must be pure ASCII (PowerShell 5.1's legacy codepage "
        f"breaks on em-dashes/curly quotes/etc.): {bad}"
    )


def test_invoke_uv_python_runs_venv_python_directly() -> None:
    module_text = (DEPLOY_WINDOWS / "OsceDeploy.psm1").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in module_text
    assert "uv run" not in module_text
    # Invoke-OscePython itself must invoke the venv python, not `uv run`.
    match = re.search(r"function Invoke-OscePython \{.*?\n\}\n", module_text, re.S)
    assert match, "Invoke-OscePython function body not found"
    body = match.group(0)
    assert "Get-OsceVenvPython" in body
    assert "& $pythonExe" in body
    assert "& uv" not in body


def test_no_uv_run_anywhere_in_deploy_windows() -> None:
    for script in sorted(DEPLOY_WINDOWS.glob("*.ps1")) + sorted(DEPLOY_WINDOWS.glob("*.psm1")):
        text = script.read_text(encoding="utf-8")
        assert "uv run" not in text, f"{script.name} still invokes 'uv run'  --  runtime helpers must use the venv python directly"


def test_uv_sync_confined_to_deploy_and_rollback_scripts() -> None:
    # Preflight.ps1 only *mentions* 'uv sync' in an advisory message (run it
    # once, by hand, if the venv is missing); it never executes it.
    allowed = {"Deploy-Release.ps1", "Rollback-Release.ps1", "OsceDeploy.psm1", "README.md", "Preflight.ps1"}
    for script in sorted(DEPLOY_WINDOWS.glob("*.ps1")) + sorted(DEPLOY_WINDOWS.glob("*.psm1")):
        text = script.read_text(encoding="utf-8")
        if "uv sync" in text or re.search(r"&\s*\$uvExe", text):
            assert script.name in allowed, (
                f"{script.name} runs 'uv sync'  --  that step is reserved for the interactive "
                "Deploy-Release.ps1 / Rollback-Release.ps1 environment sync, per CLAUDE.md "
                "'uv sync is exact, not additive'"
            )


def test_deploy_and_rollback_resolve_uv_explicitly() -> None:
    module_text = (DEPLOY_WINDOWS / "OsceDeploy.psm1").read_text(encoding="utf-8")
    assert "function Get-OsceUvCommand" in module_text
    assert "Get-Command uv" in module_text
    for name in ("Deploy-Release.ps1", "Rollback-Release.ps1"):
        text = (DEPLOY_WINDOWS / name).read_text(encoding="utf-8")
        assert "Get-OsceUvCommand" in text, f"{name} must resolve uv via Get-OsceUvCommand before invoking it"


def test_alembic_invoked_as_module_not_uv_run() -> None:
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    assert "'-m', 'alembic', 'upgrade', 'head'" in text
    assert "'run', '--no-sync', 'alembic'" not in text


def test_install_service_requires_task_credential_and_no_system_principal() -> None:
    text = (DEPLOY_WINDOWS / "Install-OsceService.ps1").read_text(encoding="utf-8")
    assert re.search(r"\[Parameter\(Mandatory\)\]\[System\.Management\.Automation\.PSCredential\]\$TaskCredential", text), (
        "Install-OsceService.ps1 must declare a mandatory [pscredential] -TaskCredential parameter"
    )
    assert "'SYSTEM'" not in text
    assert "-Password $taskPassword" in text
    assert "GetNetworkCredential().Password" in text
    assert "-RunLevel Highest" in text


def test_install_service_registers_wsl_keepalive_task() -> None:
    text = (DEPLOY_WINDOWS / "Install-OsceService.ps1").read_text(encoding="utf-8")
    assert "WslKeepalive" in text
    assert "sleep infinity" in text
    assert "[TimeSpan]::Zero" in text, "keepalive task must set an unlimited ExecutionTimeLimit"
    assert "MultipleInstances IgnoreNew" in text
    assert "RestartCount 999" in text


def test_install_service_sets_bounded_execution_time_limits() -> None:
    text = (DEPLOY_WINDOWS / "Install-OsceService.ps1").read_text(encoding="utf-8")
    assert "New-TimeSpan -Minutes 30" in text, "boot task should cap ExecutionTimeLimit around 30 minutes"
    assert "New-TimeSpan -Hours 6" in text, "backup task should cap ExecutionTimeLimit around 6 hours"


def test_preflight_checks_boot_task_account_and_wsl_distro() -> None:
    text = (DEPLOY_WINDOWS / "Preflight.ps1").read_text(encoding="utf-8")
    assert "Principal.UserId" in text
    assert "wsl.exe -l -q" in text
    assert "-replace \"`0\", ''" in text or "-replace '`0', ''" in text or "NUL" in text or "`0" in text
    assert ".venv\\Scripts\\python.exe" in text


def test_start_osce_stack_starts_keepalive_before_docker() -> None:
    text = (DEPLOY_WINDOWS / "Start-OsceStack.ps1").read_text(encoding="utf-8")
    keepalive_idx = text.index("WslKeepalive")
    docker_idx = text.index("Start-OsceHatchetStack")
    assert keepalive_idx < docker_idx, "the keepalive task must be started before Docker is brought up"


def test_invoke_osce_restore_database_throws_on_nonzero_exit() -> None:
    """F4 static guard: Invoke-OsceRestoreDatabase must check the exit code
    itself and throw rather than silently returning a failed result for its
    callers to (as they used to) pipe straight to Out-Null. A real
    powershell.exe subprocess mock of this was attempted and found too
    brittle -- Invoke-OscePython calls made from another function DEFINED IN
    THE SAME MODULE did not pick up a same-named function injected into the
    module's scope afterward via `& (Get-Module ...) { function ... }` in
    this PowerShell version, so this stays a static assertion on the
    function's own source instead."""
    module_text = (DEPLOY_WINDOWS / "OsceDeploy.psm1").read_text(encoding="utf-8")
    match = re.search(r"function Invoke-OsceRestoreDatabase \{.*?\n\}\n", module_text, re.S)
    assert match, "Invoke-OsceRestoreDatabase function body not found"
    body = match.group(0)
    assert "ExitCode -ne 0" in body
    assert re.search(r"\bthrow\b", body), "must throw on a non-zero restore exit code"


def test_deploy_release_guards_backup_result_before_property_access() -> None:
    """F3 static guard: $backupResult is $null for any failure before the
    backup step runs. Set-StrictMode Latest turns $backupResult.path on a
    $null object into a terminating error INSIDE the catch block itself,
    before Invoke-FullRollback / Invoke-NeedsOperatorStop is ever called --
    which is a worse failure than the one being rolled back from, since it
    means no rollback is attempted at all."""
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    # rindex, not index: Invoke-FullRollback has its own internal try/catch
    # earlier in the file; the one this test cares about is the main
    # script's, at the bottom.
    catch_idx = text.rindex("} catch {")
    catch_block = text[catch_idx:]
    # The one place $backupResult.path may legitimately appear is inside a
    # guard's true-branch (`if ($backupResult) { $backupResult.path } else
    # { $null }`); what must never appear again is either call site handing
    # that unguarded expression straight to a function argument.
    assert "-BackupPathIfMigrated $backupResult.path" not in catch_block, (
        "a call site is still passing $backupResult.path directly as an argument -- guard it first "
        "(e.g. `$backupPath = if ($backupResult) { $backupResult.path } else { $null }`, then pass $backupPath)"
    )
    assert re.search(r"if\s*\(\s*\$backupResult\s*\)\s*\{\s*\$backupResult\.path\s*\}\s*else", catch_block), (
        "expected a null-guard deriving $backupPath from $backupResult before it is used"
    )
    assert "-BackupPathIfMigrated $backupPath" in catch_block, (
        "expected both call sites to pass the guarded $backupPath, not $backupResult.path directly"
    )


def test_deploy_release_migration_attempted_flag_gates_rollback_restore() -> None:
    """F5 static guard: the restore decision on the rollback path must key
    off "did a migration START" (set before alembic runs), not "did it
    fully complete" (set only after alembic succeeds) -- a migration that
    fails partway through can leave real schema changes behind on SQLite,
    whose batch-mode ALTER is not one atomic transaction."""
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    assert "$migrationAttempted = $true" in text
    # The flag must be set before the alembic invocation, not after.
    attempted_idx = text.index("$migrationAttempted = $true")
    alembic_idx = text.index("'-m', 'alembic', 'upgrade', 'head'")
    assert attempted_idx < alembic_idx, "$migrationAttempted must be set BEFORE invoking alembic, not after"
    catch_idx = text.rindex("} catch {")  # the main script's catch, not Invoke-FullRollback's own
    catch_block = text[catch_idx:]
    assert "-Migrated $migrationAttempted" in catch_block
    assert "-Migrated $migrated" not in catch_block, (
        "the rollback-decision branches must use $migrationAttempted, not the completion-only $migrated"
    )


def test_rollback_release_defaults_to_current_tag_when_head_mismatches_state() -> None:
    """F7 (second half): if the checked-out HEAD does not match
    state.currentCommit (a prior failed deploy left the checkout on
    unfinished code), the default rollback target must be state.currentTag
    (the last release that actually completed), not state.previousTag (which
    would skip it)."""
    text = (DEPLOY_WINDOWS / "Rollback-Release.ps1").read_text(encoding="utf-8")
    assert "git rev-parse HEAD" in text
    assert "$state.currentCommit" in text
    assert "$targetTag = $state.currentTag" in text
    assert "$targetTag = $state.previousTag" in text


def test_deploy_release_prints_explicit_to_tag_in_rollback_command() -> None:
    """F7 (first half): the printed manual-rollback command must name the
    tag that was actually running before this failed attempt, not rely on
    Rollback-Release's own default (which is one release further back)."""
    text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    assert "-ToTag $LastGoodTag" in text
    assert "$previousState.currentTag" in text


def test_restore_paths_log_backup_and_restore_timestamps() -> None:
    deploy_text = (DEPLOY_WINDOWS / "Deploy-Release.ps1").read_text(encoding="utf-8")
    rollback_text = (DEPLOY_WINDOWS / "Rollback-Release.ps1").read_text(encoding="utf-8")
    for text in (deploy_text, rollback_text):
        assert "restoredAt" in text or "restoreStartedAt" in text
    # Deploy-Release.ps1 tracks the backup's own createdAt (from
    # backup_database.py's JSON) so a restore's JSONL entry can show backup
    # time vs. restore time, per the needs_operator / auto-rollback report.
    assert "backupCreatedAt" in deploy_text or "BackupCreatedAt" in deploy_text
