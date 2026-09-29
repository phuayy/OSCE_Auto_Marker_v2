"""Structural checks on the GitHub Actions workflows under .github/workflows/.

These parse the YAML rather than trust it, because a workflow file has no
compiler: a typo in an `npm run` script name, a `needs:` list missing a job,
or a permission block that quietly reverted to read-only are all things that
only surface when the workflow actually runs on GitHub — expensive, slow
feedback for something a plain YAML parse catches in milliseconds.

``pytest.importorskip`` rather than a hard dependency: PyYAML is not a
declared project dependency (it arrives transitively today), so a lockfile
change that drops it should skip this file rather than fail the whole suite
on an unrelated dependency being pulled in.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CI_YML = WORKFLOWS_DIR / "ci.yml"
PROMOTE_YML = WORKFLOWS_DIR / "promote.yml"
PACKAGE_JSON = REPO_ROOT / "package.json"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"

# build_release.py's asset-name constants — loaded the same way
# test_release_manifest.py does, so the two files can never independently
# drift from the source of truth.
_SCRIPT = REPO_ROOT / "scripts" / "ci" / "build_release.py"
_spec = importlib.util.spec_from_file_location("build_release", _SCRIPT)
build_release = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = build_release
_spec.loader.exec_module(build_release)


def _load_yaml(path: Path) -> dict:
    # PyYAML parses YAML 1.1's "on" as the boolean True, not the string "on",
    # for an unquoted top-level key — which is exactly the workflow trigger
    # key every workflow file uses. safe_load handles both spellings (`on:`
    # or `"on":`) the same way, so this only has to know to look up the
    # boolean key when the string key is absent.
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _on_triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True, {}))


def _package_json_scripts() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]


def _all_npm_run_targets(text: str) -> set[str]:
    """Every ``npm run <script>`` invocation appearing anywhere in a workflow
    file's raw text — not just in ``run:`` steps parsed as YAML, since a
    multi-line ``run: |`` block is itself just a string to the YAML parser
    and the command inside it is what actually matters here."""
    return set(re.findall(r"npm run ([\w:-]+)", text))


# --- npm scripts referenced actually exist -----------------------------


@pytest.mark.parametrize("workflow_path", [CI_YML, PROMOTE_YML])
def test_every_npm_run_target_exists_in_package_json(workflow_path: Path) -> None:
    if not workflow_path.is_file():
        pytest.skip(f"{workflow_path} does not exist yet")
    scripts = _package_json_scripts()
    targets = _all_npm_run_targets(workflow_path.read_text(encoding="utf-8"))
    missing = sorted(target for target in targets if target not in scripts)
    assert not missing, f"{workflow_path.name} runs undefined npm scripts: {missing}"


def test_ci_actually_references_the_scripts_it_should() -> None:
    """A sanity check on the extraction itself: if this ever finds zero npm
    run targets, the regex broke, not the workflow — ci.yml's frontend job
    is specified to lint, test and build the frontend."""
    targets = _all_npm_run_targets(CI_YML.read_text(encoding="utf-8"))
    assert {"lint:ui", "test:ui", "build"} <= targets


# --- ci.yml job wiring ---------------------------------------------------


def test_ci_workflow_exists_and_parses() -> None:
    workflow = _load_yaml(CI_YML)
    assert "jobs" in workflow


def test_release_job_only_runs_on_push_to_main() -> None:
    workflow = _load_yaml(CI_YML)
    release = workflow["jobs"]["release"]
    condition = release.get("if", "")
    assert "push" in condition
    assert "main" in condition


def test_release_job_needs_every_test_job() -> None:
    workflow = _load_yaml(CI_YML)
    jobs = workflow["jobs"]
    release = jobs["release"]
    needs = release.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    other_jobs = {name for name in jobs if name != "release"}
    assert other_jobs, "ci.yml should define at least one non-release job for release to depend on"
    missing = other_jobs - set(needs)
    assert not missing, f"release job does not `needs:` these jobs: {sorted(missing)}"


def _pyproject_pin(package: str) -> str:
    text = PYPROJECT_TOML.read_text(encoding="utf-8")
    match = re.search(rf'"{re.escape(package)}==([^"]+)"', text)
    assert match, f"{package} is not pinned as \"{package}==...\" in pyproject.toml"
    return match.group(1)


def test_release_job_dependency_pins_match_pyproject() -> None:
    """The release job builds release assets in an ephemeral ``uv run
    --no-project --with alembic==X --with SQLAlchemy==Y`` environment rather
    than the full project venv (see that step's own comment: build_release.py
    only needs those two, and skipping the rest avoids the CUDA torch
    download every other job pays for). That means the two versions are
    hand-typed into the workflow file and can silently drift from
    pyproject.toml's own pins — this keeps them equal without having to run
    the workflow to find out."""
    text = CI_YML.read_text(encoding="utf-8")
    alembic_in_workflow = re.search(r"--with alembic==([0-9][\w.]*)", text)
    sqlalchemy_in_workflow = re.search(r"--with SQLAlchemy==([0-9][\w.]*)", text)
    assert alembic_in_workflow, "ci.yml's release job no longer pins alembic via --with"
    assert sqlalchemy_in_workflow, "ci.yml's release job no longer pins SQLAlchemy via --with"
    assert alembic_in_workflow.group(1) == _pyproject_pin("alembic")
    assert sqlalchemy_in_workflow.group(1) == _pyproject_pin("SQLAlchemy")


def test_ci_triggers_cover_pr_push_and_manual_dispatch() -> None:
    workflow = _load_yaml(CI_YML)
    triggers = _on_triggers(workflow)
    assert "pull_request" in triggers
    assert "push" in triggers
    assert "workflow_dispatch" in triggers
    assert triggers["pull_request"].get("branches") == ["main"]
    assert triggers["push"].get("branches") == ["main"]


# --- permissions: read by default, write only where a release is cut ----


def test_ci_top_level_permissions_are_read_only() -> None:
    workflow = _load_yaml(CI_YML)
    assert workflow["permissions"]["contents"] == "read"
    # No job other than release should escalate back to write.
    for name, job in workflow["jobs"].items():
        if name == "release":
            continue
        assert "permissions" not in job or job["permissions"].get("contents") != "write", (
            f"job {name!r} should not hold contents:write — only release does, and only "
            "because it publishes a GitHub Release"
        )


def test_ci_release_job_has_contents_write() -> None:
    workflow = _load_yaml(CI_YML)
    release = workflow["jobs"]["release"]
    assert release["permissions"]["contents"] == "write"


def test_promote_job_has_contents_write() -> None:
    workflow = _load_yaml(PROMOTE_YML)
    jobs = workflow["jobs"]
    # Whichever job in promote.yml calls `gh release create` needs write —
    # pinned at the workflow's only job rather than a job named "promote" by
    # convention alone, so a rename does not silently un-pin this check.
    assert len(jobs) >= 1
    write_jobs = [
        name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"
    ]
    assert write_jobs, "promote.yml has no job with contents: write, but it must publish a release"


def test_promote_top_level_permissions_are_not_write() -> None:
    """The workflow-level default should stay conservative; only the
    publishing job escalates, the same shape ci.yml uses for `release`."""
    workflow = _load_yaml(PROMOTE_YML)
    top_level = workflow.get("permissions")
    if top_level is not None:
        assert top_level.get("contents") != "write"


# --- release asset filenames agree with build_release.py's constants ----


@pytest.mark.parametrize(
    "constant_name",
    ["DIST_ZIP_NAME", "MANIFEST_NAME", "SUMS_NAME"],
)
def test_release_asset_filenames_appear_in_ci_workflow(constant_name: str) -> None:
    filename = getattr(build_release, constant_name)
    text = CI_YML.read_text(encoding="utf-8")
    assert filename in text, f"ci.yml never references {constant_name} ({filename!r})"


@pytest.mark.parametrize(
    "constant_name",
    ["DIST_ZIP_NAME", "MANIFEST_NAME", "SUMS_NAME"],
)
def test_release_asset_filenames_appear_in_promote_workflow(constant_name: str) -> None:
    filename = getattr(build_release, constant_name)
    text = PROMOTE_YML.read_text(encoding="utf-8")
    assert filename in text, f"promote.yml never references {constant_name} ({filename!r})"


# --- promote.yml triggers only on version tags ---------------------------


def test_promote_triggers_only_on_version_tags() -> None:
    workflow = _load_yaml(PROMOTE_YML)
    triggers = _on_triggers(workflow)
    assert "push" in triggers
    tags = triggers["push"].get("tags", [])
    assert tags, "promote.yml should trigger on tag pushes"
    assert any("v" in pattern for pattern in tags)
    # Must not also trigger on branch pushes — that would make it race ci.yml
    # for every ordinary merge to main.
    assert "branches" not in triggers["push"]


# --- dependabot.yml, lightly ------------------------------------------


def test_dependabot_config_covers_the_three_ecosystems() -> None:
    dependabot_path = REPO_ROOT / ".github" / "dependabot.yml"
    if not dependabot_path.is_file():
        pytest.skip("dependabot.yml does not exist yet")
    config = _load_yaml(dependabot_path)
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert {"npm", "github-actions", "uv"} <= ecosystems
