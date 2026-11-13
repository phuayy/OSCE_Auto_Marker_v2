"""Start the VM from a terminal with the same GPU fallback as the start page.

    cd deploy/gcp/gate
    uv run --no-project --with google-auth --with requests \\
        python -m osce_gate.cli --project PROJECT --zone asia-southeast1-b --instance osce-marker

Uses your Application Default Credentials (``gcloud auth application-default
login``). ``--profiles`` defaults to ``GATE_MACHINE_PROFILES``, then to an L4
with a T4 fallback. Exit code 0 when the VM is starting (or already up), 1
when no GPU could be had.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from osce_gate.compute import InstanceRef, RestComputeClient
from osce_gate.hardware import parse_profiles, start_preferred

DEFAULT_PROFILES = "g2-standard-8, n1-standard-8+nvidia-tesla-t4"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=os.getenv("GATE_PROJECT"), required=not os.getenv("GATE_PROJECT"))
    parser.add_argument("--zone", default=os.getenv("GATE_ZONE"), required=not os.getenv("GATE_ZONE"))
    parser.add_argument("--instance", default=os.getenv("GATE_INSTANCE"), required=not os.getenv("GATE_INSTANCE"))
    parser.add_argument("--profiles", default=os.getenv("GATE_MACHINE_PROFILES") or DEFAULT_PROFILES,
                        help=f"Machine profiles in order of preference (default: {DEFAULT_PROFILES!r})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    compute = RestComputeClient(InstanceRef(args.project, args.zone, args.instance))
    outcome = start_preferred(compute, parse_profiles(args.profiles), zone=args.zone)
    for label, reason in outcome.skipped:
        print(f"Skipped {label}: {reason}")
    if outcome.error:
        print(outcome.error, file=sys.stderr)
        return 1
    if not outcome.started:
        print(f"Not started: the VM is already {outcome.status}.")
        return 0
    print(f"Starting on {outcome.profile.label if outcome.profile else 'its current machine type'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
