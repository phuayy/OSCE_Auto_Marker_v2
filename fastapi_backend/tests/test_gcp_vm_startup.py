"""deploy/gcp/setup/vm-startup.sh: the failure paths that decide whether a GPU VM keeps billing.

The script is sourced with OSCE_STARTUP_SOURCE_ONLY=1, which defines its functions without
running the install. Each scenario then drives the same functions the install does, in the same
order, against a sandbox: a fake app tree, a fake database file, and fake `curl` / `systemctl` /
`uv` / `shutdown` executables that record what was asked of them. The fake app answers
`/api/health/ready` only while its service is "running" and the installed code carries a GOOD
marker, which is how "the new version crash-loops" is expressed.

The orchestration order of the real install (watchdog first, backup before the swap, the
migration flag before the restart) is pinned separately by reading the script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "gcp" / "setup" / "vm-startup.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

FAKE_CURL = r"""#!/bin/bash
# Records guest-attribute writes; answers the readiness probe from the sandbox state.
args="$*"
if [[ "$args" == *"-X PUT"* ]]; then
  data=""; url=""
  while (($#)); do
    case "$1" in --data) data="$2"; shift ;; http*) url="$1" ;; esac
    shift
  done
  echo "${url##*/guest-attributes/} $data" >>"$SANDBOX/guest.log"
  exit 0
fi
if [[ "$args" == *"/api/health/ready"* ]]; then
  if [[ -f "$SANDBOX/running" && -f "$OSCE_APP_DIR/GOOD" ]]; then echo '{"ready": true}'; exit 0; fi
  exit 22
fi
exit 22
"""

FAKE_SYSTEMCTL = r"""#!/bin/bash
echo "systemctl $*" >>"$SANDBOX/calls.log"
case "$1 $2" in
  "stop osce-marker") rm -f "$SANDBOX/running" ;;
  "restart osce-marker")
    touch "$SANDBOX/running"
    # The new code migrates the database when it boots.
    if [[ -f "$OSCE_APP_DIR/MIGRATES" ]]; then echo migrated >"$SANDBOX/db"; fi ;;
esac
exit 0
"""

FAKE_SHUTDOWN = r"""#!/bin/bash
echo "shutdown $*" >>"$SANDBOX/calls.log"
"""

# `uv run --no-sync python scripts/backup_database.py --dest D | --restore F --confirm`, `uv sync ...`
FAKE_UV = r"""#!/bin/bash
echo "uv $*" >>"$SANDBOX/calls.log"
if [[ "$1" == sync ]]; then
  [[ -f "$SANDBOX/sync_fails" ]] && { echo "uv sync failed" >&2; exit 1; }
  exit 0
fi
if [[ "$*" == *"--dest "* ]]; then
  [[ -f "$SANDBOX/backup_fails" ]] && { echo "backup failed" >&2; exit 1; }
  all="$*"; dest="${all##*--dest }"; dest="${dest%% *}"
  cp "$SANDBOX/db" "$dest/osce-db-20260101T000000Z-rev.sqlite3"
  echo '{"ok": true}'
  exit 0
fi
if [[ "$*" == *"--restore "* ]]; then
  # The real script refuses to restore SQLite while the API is serving.
  [[ -f "$SANDBOX/running" ]] && { echo "API is serving" >&2; exit 5; }
  [[ -f "$SANDBOX/restore_fails" ]] && { echo "restore failed" >&2; exit 1; }
  all="$*"; file="${all##*--restore }"; file="${file%% *}"
  cp "$file" "$SANDBOX/db"
  exit 0
fi
exit 99
"""


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = root / "opt" / "osce-marker"
        self.data = root / "var" / "osce-marker"
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.data.mkdir(parents=True)
        for name, body in {
            "curl": FAKE_CURL,
            "systemctl": FAKE_SYSTEMCTL,
            "shutdown": FAKE_SHUTDOWN,
            "uv": FAKE_UV,
        }.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        (root / "db").write_text("old-schema")
        (root / "calls.log").touch()
        (root / "guest.log").touch()

    def installed(self, version: str, *, good: bool = True, migrates: bool = False) -> None:
        self.app.mkdir(parents=True, exist_ok=True)
        (self.app / "VERSION").write_text(version)
        (self.app / ".venv").mkdir(exist_ok=True)
        (self.app / "dist").mkdir(exist_ok=True)
        if good:
            (self.app / "GOOD").touch()
        if migrates:
            (self.app / "MIGRATES").touch()
        (self.root / "running").touch()
        (self.data / "installed-bundle").write_text("gs://b/old-bundle.tar.gz")

    def new_tree(self, version: str, *, good: bool, migrates: bool = False) -> None:
        new = Path(f"{self.app}.new")
        new.mkdir(parents=True)
        (new / "VERSION").write_text(version)
        if good:
            (new / "GOOD").touch()
        if migrates:
            (new / "MIGRATES").touch()

    def flag(self, name: str) -> None:
        (self.root / name).touch()

    def run(self, body: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        driver = self.root / "driver.sh"
        driver.write_text(
            textwrap.dedent(
                f"""\
                export SANDBOX="{self.root}" OSCE_APP_DIR="{self.app}" OSCE_DATA_DIR="{self.data}"
                export OSCE_LOG="{self.root}/install.log" OSCE_STARTUP_SOURCE_ONLY=1
                export OSCE_READY_TRIES=2 OSCE_READY_SLEEP=0 OSCE_UV_BIN="{self.bin}/uv"
                export PATH="{self.bin}:$PATH"
                set -Eeuo pipefail
                source "{SCRIPT}"
                as_user() {{ "$@"; }}
                own() {{ :; }}
                BUNDLE=gs://b/new-bundle.tar.gz
                """
            )
            + textwrap.dedent(body)
        )
        merged = {**os.environ, **(env or {})}
        return subprocess.run([BASH, str(driver)], capture_output=True, text=True, timeout=60, env=merged)

    # --- observations -------------------------------------------------------
    def read(self, relative: str) -> str:
        return (self.root / relative).read_text().strip("\n")

    def calls(self) -> str:
        return self.read("calls.log")

    def guest(self) -> list[str]:
        return self.read("guest.log").splitlines()

    @property
    def state(self) -> str:
        return (self.data / "installed-bundle").read_text().strip()

    def backups(self) -> list[str]:
        directory = self.data / "backups"
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


@pytest.fixture()
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# The install's own sequence for an update, with the steps the tests do not model
# (apt, uv sync of the new code, the frontend build, unit files) left out.
UPDATE = """
    UPDATING=1; OLD_BUNDLE="$(cat "$OSCE_DATA_DIR/installed-bundle")"
    replace_app_code
    MIGRATION_ATTEMPTED=1
    systemctl restart osce-marker
    stage "waiting-for-app"
    finish_install
    trap - ERR
"""

FIRST_INSTALL = """
    replace_app_code
    MIGRATION_ATTEMPTED=1
    systemctl restart osce-marker
    stage "waiting-for-app"
    finish_install
    trap - ERR
"""


# --- H8: updates are transactional -------------------------------------------


def test_successful_update_commits_prunes_and_removes_the_old_code(sandbox: Sandbox) -> None:
    sandbox.installed("old")
    # Four pre-update backups from earlier updates; only the newest three survive.
    for stamp in ("20250101T000000Z", "20250201T000000Z", "20250301T000000Z", "20250401T000000Z"):
        (sandbox.data / "backups" / f"pre-update-{stamp}-x").mkdir(parents=True)
    sandbox.new_tree("new", good=True, migrates=True)

    result = sandbox.run(UPDATE)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (sandbox.app / "VERSION").read_text() == "new"
    assert not Path(f"{sandbox.app}.old").exists()
    assert sandbox.state == "gs://b/new-bundle.tar.gz"
    # The shared environments moved across, not rebuilt.
    assert (sandbox.app / ".venv").is_dir()
    backups = sandbox.backups()
    assert len(backups) == 3
    assert any(name.endswith("-old-bundle.tar.gz") for name in backups), "the new backup names the old bundle"
    assert not any("20250101" in name or "20250201" in name for name in backups)
    assert sandbox.read("db") == "migrated"
    assert "shutdown" not in sandbox.calls()


def test_backup_is_taken_with_the_old_app_stopped_and_before_the_swap(sandbox: Sandbox) -> None:
    sandbox.installed("old")
    sandbox.new_tree("new", good=True)
    result = sandbox.run(UPDATE)
    assert result.returncode == 0, result.stderr
    calls = sandbox.calls().splitlines()
    stop = calls.index("systemctl stop osce-marker")
    backup = next(i for i, line in enumerate(calls) if "--dest" in line)
    restart = calls.index("systemctl restart osce-marker")
    assert stop < backup < restart


def test_backup_failure_aborts_the_update_and_the_old_app_keeps_serving(sandbox: Sandbox) -> None:
    sandbox.installed("old")
    sandbox.new_tree("new", good=True, migrates=True)
    sandbox.flag("backup_fails")

    result = sandbox.run(UPDATE)

    assert result.returncode != 0
    assert (sandbox.app / "VERSION").read_text() == "old", "old code untouched"
    assert (sandbox.root / "running").exists(), "old service restarted"
    assert sandbox.read("db") == "old-schema", "no migration ran"
    assert sandbox.state == "gs://b/old-bundle.tar.gz"
    assert any(line.startswith("osce/install failed backup rolled-back ") for line in sandbox.guest())
    assert not any(line.startswith("osce/boot failed") for line in sandbox.guest()), "gate must not show a failure: the old app serves"
    assert "shutdown" not in sandbox.calls(), "a healthy old app must not be powered off"


def test_failed_migration_rolls_back_code_database_and_serves_the_old_app(sandbox: Sandbox) -> None:
    sandbox.installed("old")
    sandbox.new_tree("new", good=False, migrates=True)  # migrates, then crash-loops

    result = sandbox.run(UPDATE)

    assert result.returncode != 0
    assert (sandbox.app / "VERSION").read_text() == "old"
    assert (sandbox.app / ".venv").is_dir(), "environments handed back to the old tree"
    assert (sandbox.app / "GOOD").exists()
    assert sandbox.read("db") == "old-schema", "database restored from the pre-update backup"
    assert sandbox.state == "gs://b/old-bundle.tar.gz", "the next Start retries the update"
    assert Path(f"{sandbox.app}.failed", "VERSION").read_text() == "new", "failed copy kept for inspection"
    assert not Path(f"{sandbox.app}.old").exists()
    assert (sandbox.root / "running").exists()
    assert any(line.startswith("osce/install failed waiting-for-app rolled-back ") for line in sandbox.guest())
    assert "shutdown" not in sandbox.calls()
    calls = sandbox.calls().splitlines()
    assert any("uv sync --frozen --no-dev" in c for c in calls), "old lock re-synced"
    restore = next(i for i, c in enumerate(calls) if "--restore" in c)
    assert "systemctl stop osce-marker" in calls[:restore], "restore happens with the service stopped"


def test_failure_before_the_app_started_rolls_back_code_without_touching_the_database(sandbox: Sandbox) -> None:
    """A uv sync / npm build failure: the migration flag is not set, so no restore."""
    sandbox.installed("old")
    sandbox.new_tree("new", good=True, migrates=True)
    result = sandbox.run(
        """
        UPDATING=1; OLD_BUNDLE="$(cat "$OSCE_DATA_DIR/installed-bundle")"
        replace_app_code
        CURRENT_STEP=frontend-build
        false
        """
    )
    assert result.returncode != 0
    assert (sandbox.app / "VERSION").read_text() == "old"
    assert "--restore" not in sandbox.calls()
    assert sandbox.read("db") == "old-schema"
    assert any(line.startswith("osce/install failed frontend-build rolled-back ") for line in sandbox.guest())


def test_rollback_that_fails_does_not_loop_and_leaves_everything_for_an_operator(sandbox: Sandbox) -> None:
    sandbox.installed("old")
    sandbox.new_tree("new", good=False, migrates=True)
    sandbox.flag("restore_fails")

    result = sandbox.run(UPDATE)

    assert result.returncode != 0
    guest = sandbox.guest()
    assert any(line.startswith("osce/install failed waiting-for-app rollback-failed ") for line in guest)
    assert any(line.startswith("osce/boot failed ") for line in guest), "the gate is told"
    assert "shutdown -h +15" in sandbox.calls(), "the delayed power-off applies"
    assert (sandbox.data / "update-needs-operator").exists()
    assert sandbox.backups(), "the backup is still there"
    assert Path(f"{sandbox.app}.failed").exists() or Path(f"{sandbox.app}.old").exists()
    # Only one restore attempt: the failure handler did not re-enter itself.
    assert sandbox.calls().count("--restore") == 1


def test_a_later_start_refuses_to_update_over_an_unresolved_rollback(sandbox: Sandbox) -> None:
    sandbox.installed("old")
    (sandbox.data / "update-needs-operator").write_text("details")
    script = SCRIPT.read_text()
    # The guard sits in the main flow, after the fast path and before any download.
    guard = script.index('-f "$NEEDS_OPERATOR"')
    assert script.index("install_boot_watchdog\n") < guard < script.index('stage "system-packages"')
    assert 'report "failed needs-operator"' in script[guard:guard + 700]


# --- H7: a failed start must not leave the VM billing --------------------------


def test_failed_first_install_reports_and_schedules_a_cancellable_power_off(sandbox: Sandbox) -> None:
    # No installed app, no state file: nothing to back up or roll back.
    sandbox.new_tree("new", good=False)
    result = sandbox.run(FIRST_INSTALL)

    assert result.returncode != 0
    assert "--dest" not in sandbox.calls(), "no backup on a first install"
    assert "--restore" not in sandbox.calls()
    guest = sandbox.guest()
    assert any(line.startswith("osce/install failed ") for line in guest)
    assert any(line.startswith("osce/boot failed ") for line in guest)
    assert "shutdown -h +15" in sandbox.calls()
    log = sandbox.read("install.log") if (sandbox.root / "install.log").exists() else ""
    output = result.stdout + log
    assert "sudo shutdown -c" in output
    assert "systemctl stop osce-boot-watchdog.timer" in output
    assert not (sandbox.data / "installed-bundle").exists()


def test_a_failing_step_runs_the_handler_once_even_with_the_error_trap_armed(sandbox: Sandbox) -> None:
    result = sandbox.run(
        """
        CURRENT_STEP=python-dependencies
        false
        echo "NOT REACHED"
        """
    )
    assert result.returncode != 0
    assert "NOT REACHED" not in result.stdout
    assert result.stdout.count("install failed during: python-dependencies") == 1
    assert sandbox.calls().count("shutdown -h +15") == 1
    assert "osce/install failed python-dependencies" in sandbox.read("guest.log")


def test_failure_grace_is_configurable(sandbox: Sandbox) -> None:
    result = sandbox.run(
        """
        FAIL_GRACE_MIN=7
        CURRENT_STEP=secrets
        false
        """
    )
    assert result.returncode != 0
    assert "shutdown -h +7" in sandbox.calls()


# --- the boot watchdog ---------------------------------------------------------


class Watchdog:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        script = sandbox.root / "osce-boot-watchdog"
        extract = subprocess.run(
            [BASH, "-c", f'OSCE_STARTUP_SOURCE_ONLY=1 source "{SCRIPT}"; boot_watchdog_script'],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "OSCE_LOG": str(sandbox.root / "x.log")},
        )
        script.write_text(extract.stdout)
        script.chmod(0o755)
        self.script = script

    def check(self, *, uptime_minutes: float, ready: bool, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        sb = self.sandbox
        uptime = sb.root / "uptime"
        uptime.write_text(f"{uptime_minutes * 60:.2f} 0.00\n")
        for name in ("running",):
            (sb.root / name).unlink(missing_ok=True)
        if ready:
            (sb.root / "running").touch()
            (sb.app / "GOOD").touch()
        else:
            (sb.app / "GOOD").unlink(missing_ok=True)
        env = {
            **os.environ,
            "SANDBOX": str(sb.root),
            "OSCE_APP_DIR": str(sb.app),
            "OSCE_UPTIME_FILE": str(uptime),
            "OSCE_WATCHDOG_STATE_DIR": str(sb.root / "wd"),
            "PATH": f"{sb.bin}:{os.environ['PATH']}",
            **(extra or {}),
        }
        return subprocess.run([BASH, str(self.script)], capture_output=True, text=True, timeout=30, env=env)


@pytest.fixture()
def watchdog(sandbox: Sandbox) -> Watchdog:
    sandbox.app.mkdir(parents=True)
    return Watchdog(sandbox)


def test_watchdog_leaves_a_vm_whose_app_is_ready_alone(sandbox: Sandbox, watchdog: Watchdog) -> None:
    assert watchdog.check(uptime_minutes=120, ready=True).returncode == 0
    assert "shutdown" not in sandbox.calls()


def test_watchdog_waits_out_the_boot_grace(sandbox: Sandbox, watchdog: Watchdog) -> None:
    result = watchdog.check(uptime_minutes=20, ready=False)
    assert result.returncode == 0
    assert "shutdown" not in sandbox.calls()


def test_watchdog_powers_off_a_vm_whose_app_never_became_ready(sandbox: Sandbox, watchdog: Watchdog) -> None:
    watchdog.check(uptime_minutes=20, ready=False)
    watchdog.check(uptime_minutes=61, ready=False)
    assert "shutdown -h now" in sandbox.calls()
    assert any(line.startswith("osce/boot failed ") for line in sandbox.guest()), "the gate is told before the power-off"


def test_watchdog_grace_comes_from_its_environment(sandbox: Sandbox, watchdog: Watchdog) -> None:
    watchdog.check(uptime_minutes=20, ready=False, extra={"OSCE_WATCHDOG_GRACE_MINUTES": "15"})
    assert "shutdown -h now" in sandbox.calls()


def test_watchdog_does_not_fire_later_for_a_healthy_app_but_catches_a_later_crash_loop(
    sandbox: Sandbox, watchdog: Watchdog
) -> None:
    watchdog.check(uptime_minutes=10, ready=True)  # became ready early
    # Past the boot grace, a single miss (a restart in progress) is forgiven ...
    watchdog.check(uptime_minutes=90, ready=False)
    watchdog.check(uptime_minutes=100, ready=False)
    assert "shutdown" not in sandbox.calls()
    # ... and a recovery resets the count ...
    watchdog.check(uptime_minutes=110, ready=True)
    watchdog.check(uptime_minutes=120, ready=False)
    watchdog.check(uptime_minutes=130, ready=False)
    assert "shutdown" not in sandbox.calls()
    # ... but three misses in a row is a crash-loop.
    watchdog.check(uptime_minutes=140, ready=False)
    assert "shutdown -h now" in sandbox.calls()


def test_boot_watchdog_is_installed_as_a_repeating_timer_with_the_metadata_grace(sandbox: Sandbox) -> None:
    systemd, sbin = sandbox.root / "systemd", sandbox.root / "sbin"
    systemd.mkdir()
    sbin.mkdir()
    result = sandbox.run(
        f"""
        SYSTEMD_DIR="{systemd}"; SBIN_DIR="{sbin}"; WATCHDOG_GRACE_MIN=45
        install_boot_watchdog
        """
    )
    assert result.returncode == 0, result.stderr
    assert (sbin / "osce-boot-watchdog").stat().st_mode & 0o111
    service = (systemd / "osce-boot-watchdog.service").read_text()
    assert "Environment=OSCE_WATCHDOG_GRACE_MINUTES=45" in service
    assert f"ExecStart={sbin}/osce-boot-watchdog" in service
    timer = (systemd / "osce-boot-watchdog.timer").read_text()
    assert "OnBootSec=10min" in timer and "OnUnitActiveSec=10min" in timer
    assert "systemctl enable --now osce-boot-watchdog.timer" in sandbox.calls()
    script = SCRIPT.read_text()
    assert "attr osce-watchdog-minutes" in script and "attr osce-failure-grace-minutes" in script


# --- the real install's orchestration -------------------------------------------


def test_install_order_pins_the_safety_properties() -> None:
    script = SCRIPT.read_text()
    main = script[script.index('BUNDLE="$(attr osce-bundle)"'):]

    def at(needle: str) -> int:
        assert needle in main, needle
        return main.index(needle)

    # The watchdog is in place before any step that can fail, and before the fast path returns.
    assert at("install_boot_watchdog") < at("bundle unchanged") < at('stage "system-packages"')
    # The new code is swapped in only through the transaction helper, and the app is
    # restarted (Alembic runs) only after the flag that makes rollback restore the database.
    assert at("replace_app_code") < at("MIGRATION_ATTEMPTED=1") < at("systemctl restart osce-marker")
    assert at("systemctl restart osce-marker") < at("finish_install")
    # The old tree is never deleted by the main flow: only commit_update removes it.
    assert not re.search(r'rm -rf "\$APP_DIR\.old"', main)
    assert "ready=1" not in main, "the readiness loop lives in wait_for_app"
    # backup precedes the swap inside the helper
    helper = script[script.index("replace_app_code() {"):script.index("finish_install() {")]
    assert helper.index("backup_before_update") < helper.index("swap_in_new_app")
