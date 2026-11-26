"""deploy/gcp/setup.sh: GPU quota accounting.

The script is sourced with OSCE_SETUP_SOURCE_ONLY=1 (which defines its functions without running
``main``) against a fake ``gcloud`` on PATH that answers from environment variables, so the
scenarios drive the same functions the real run does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "gcp" / "setup.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

# Answers the handful of gcloud calls these functions make. Quotas and the VM come from the
# environment; the install guest attribute from a file the test writes.
FAKE_GCLOUD = r"""#!/bin/bash
args="$*"
case "$args" in
  *"regions describe"*"quotas.metric=NVIDIA_L4_GPUS"*)  printf '%s\t%s\n' "$L4_LIMIT" "$L4_USAGE" ;;
  *"regions describe"*"quotas.metric=NVIDIA_T4_GPUS"*)  printf '%s\t%s\n' "$T4_LIMIT" "$T4_USAGE" ;;
  *"regions describe"*) exit 0 ;;
  *"project-info describe"*) printf '%s\t%s\n' "$ALL_LIMIT" "$ALL_USAGE" ;;
  *"accelerator-types list"*"nvidia-l4"*) echo "$ZONE" ;;
  *"accelerator-types list"*"nvidia-tesla-t4"*) echo "$ZONE" ;;
  *"instances describe"*"guestAccelerators"*) [[ -n "${VM_ACCEL:-}" ]] && printf '%s\t1\n' "$VM_ACCEL"; exit 0 ;;
  *"instances describe"*"value(status)"*) [[ -n "${VM_STATUS:-}" ]] || exit 1; echo "$VM_STATUS" ;;
  *"instances describe"*) [[ -n "${VM_STATUS:-}" ]] || exit 1 ;;
  *"get-guest-attributes"*) cat "$SANDBOX/install_value" 2>/dev/null || true ;;
esac
exit 0
"""


@pytest.fixture()
def sandbox(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD),):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    def run(body: str, **env: str) -> subprocess.CompletedProcess[str]:
        driver = tmp_path / "driver.sh"
        driver.write_text(
            textwrap.dedent(
                f"""\
                export SANDBOX="{tmp_path}" OSCE_SETUP_SOURCE_ONLY=1 PATH="{bin_dir}:$PATH"
                source "{SCRIPT}"
                PROJECT=p REGION=r-1 ZONE=r-1-a INSTANCE=osce
                OSCE_L4_PROFILE=g2-standard-8+nvidia-l4; OSCE_T4_PROFILE=n1-standard-8+nvidia-tesla-t4
                """
            )
            + textwrap.dedent(body)
        )
        merged = {
            "L4_LIMIT": "1", "L4_USAGE": "0", "T4_LIMIT": "0", "T4_USAGE": "0",
            "ALL_LIMIT": "1", "ALL_USAGE": "0", "ZONE": "r-1-a",
            **os.environ, **env,
        }
        return subprocess.run([BASH, str(driver)], capture_output=True, text=True, timeout=60, env=merged)

    run.root = tmp_path  # type: ignore[attr-defined]
    return run


# --- M42: a re-run must not count the VM's own GPU as taken ---------------------


def test_running_vm_holding_the_only_l4_keeps_the_l4_profile(sandbox) -> None:
    result = sandbox(
        "choose_region_and_gpu >/dev/null; echo \"profiles=$MACHINE_PROFILES\"",
        L4_USAGE="1", ALL_USAGE="1", VM_STATUS="RUNNING", VM_ACCEL="nvidia-l4",
    )
    assert result.returncode == 0, result.stderr
    assert "profiles=g2-standard-8+nvidia-l4" in result.stdout


def test_gpu_quota_for_a_stopped_vm_is_limit_minus_usage(sandbox) -> None:
    result = sandbox(
        "echo \"l4=$(gpu_quota NVIDIA_L4_GPUS nvidia-l4) all=$(global_gpu_quota)\"",
        L4_LIMIT="2", L4_USAGE="1", ALL_USAGE="1", VM_STATUS="TERMINATED", VM_ACCEL="nvidia-l4",
    )
    assert "l4=1 all=0" in result.stdout


def test_gpu_quota_with_no_vm_is_limit_minus_usage(sandbox) -> None:
    result = sandbox("echo \"l4=$(gpu_quota NVIDIA_L4_GPUS nvidia-l4)\"", L4_LIMIT="1", L4_USAGE="1")
    assert "l4=0" in result.stdout


def test_another_vms_usage_is_still_excluded(sandbox) -> None:
    # Two L4s in use, ours holds one of them: the other VM's one stays taken.
    result = sandbox(
        "echo \"l4=$(gpu_quota NVIDIA_L4_GPUS nvidia-l4) all=$(global_gpu_quota)\"",
        L4_LIMIT="2", L4_USAGE="2", ALL_LIMIT="2", ALL_USAGE="2", VM_STATUS="RUNNING", VM_ACCEL="nvidia-l4",
    )
    assert "l4=1 all=1" in result.stdout


def test_a_vms_other_accelerator_is_not_credited_to_this_metric(sandbox) -> None:
    result = sandbox(
        "echo \"l4=$(gpu_quota NVIDIA_L4_GPUS nvidia-l4) t4=$(gpu_quota NVIDIA_T4_GPUS nvidia-tesla-t4)\"",
        L4_LIMIT="1", L4_USAGE="0", T4_LIMIT="1", T4_USAGE="1", VM_STATUS="RUNNING", VM_ACCEL="nvidia-tesla-t4",
    )
    assert "l4=1 t4=1" in result.stdout


