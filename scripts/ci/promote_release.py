"""Verification promote.yml runs before it re-publishes a build under a version tag.

promote.yml's job is narrow and mechanical: a ``vX.Y.Z`` tag names a commit;
that commit must already have a ``build-<sha12>`` prerelease (ci.yml's
release job, on every push to main, publishes one for every commit that
lands there); promoting is downloading that release's three assets
*unchanged* and re-publishing them under the version tag. "Unchanged" is not
just a wish — this module is what proves it before the workflow ever calls
``gh release create --verify-tag``: SHA256SUMS.txt actually verifies against
the downloaded files, and release.json's own ``commit`` field actually
equals the tag's commit. Either check failing means the build-<sha12>
release does not describe what it claims to, and promoting it would ship the
wrong build under a version number someone might build a deployment on.

The network side (resolving the tag to a commit, calling ``gh release
download`` / ``gh release create``) stays in promote.yml's own shell steps —
nothing here talks to GitHub. This module only verifies files already on
disk, which is what makes it testable without a live repo or a token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# build_release.py's build_tag / validate_commit_sha are the same rules
# ci.yml used to name the build-<sha12> release in the first place; importing
# rather than re-typing them is what keeps the two workflows unable to
# disagree about what a commit's build tag is.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from build_release import ReleaseBuildError, build_tag, validate_commit_sha  # noqa: E402


class PromotionError(RuntimeError):
    """A verification failure meant for a CI log, not a traceback."""


def resolve_build_tag(commit_sha: str) -> str:
    """The build-<sha12> tag a version tag's commit must already have a release for."""
    try:
        return build_tag(commit_sha)
    except ReleaseBuildError as exc:
        raise PromotionError(str(exc)) from exc


def parse_sums_file(text: str) -> dict[str, str]:
    """Parse ``sha256sum``-format text (``<hex>  <filename>``, two spaces —
    the text-mode convention ``build_release.build_sums_text`` writes) into
    ``{filename: hex}``."""
    entries: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        digest, separator, name = line.partition("  ")
        if not separator or not digest or not name:
            raise PromotionError(f"Malformed SHA256SUMS.txt line: {raw_line!r}")
        entries[name] = digest
    return entries


def verify_checksums(assets_dir: Path, sums_filename: str = "SHA256SUMS.txt") -> dict[str, str]:
    """Raise :class:`PromotionError` unless every file SHA256SUMS.txt names is
    present in ``assets_dir`` and hashes to exactly what it claims. Returns
    the parsed {filename: hex} on success."""
    sums_path = assets_dir / sums_filename
    if not sums_path.is_file():
        raise PromotionError(f"{sums_path} not found among the downloaded assets.")
    entries = parse_sums_file(sums_path.read_text(encoding="utf-8"))
    if not entries:
        raise PromotionError(f"{sums_path} names no files.")

    problems: list[str] = []
    for name, expected_digest in entries.items():
        candidate = assets_dir / name
        if not candidate.is_file():
            problems.append(f"{name}: file missing")
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected_digest:
            problems.append(f"{name}: checksum mismatch (expected {expected_digest}, got {actual})")
    if problems:
        raise PromotionError("SHA256SUMS.txt verification failed:\n" + "\n".join(problems))
    return entries


def verify_manifest_commit(assets_dir: Path, expected_commit: str, manifest_filename: str = "release.json") -> dict:
    """Raise :class:`PromotionError` unless release.json's own ``commit``
    field equals the tag's resolved commit. Returns the parsed manifest."""
    expected_commit = validate_commit_sha(expected_commit)
    manifest_path = assets_dir / manifest_filename
    if not manifest_path.is_file():
        raise PromotionError(f"{manifest_path} not found among the downloaded assets.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_commit = str(manifest.get("commit", "")).lower()
    if actual_commit != expected_commit:
        raise PromotionError(
            f"release.json's commit ({actual_commit!r}) does not match the tag's own commit "
            f"({expected_commit!r}); refusing to promote a build that does not describe this commit."
        )
    return manifest


def verify_release_assets(assets_dir: Path, expected_commit: str) -> dict:
    """Both checks, in the order that fails fastest and cheapest: checksums
    first (pure hashing, no interpretation needed), then the semantic
    commit-match check that only makes sense once the files are known-good."""
    verify_checksums(assets_dir)
    return verify_manifest_commit(assets_dir, expected_commit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", required=True, type=Path, help="Directory the build-<sha12> assets were downloaded into.")
    parser.add_argument("--commit", required=True, help="The tag's resolved 40-character commit sha.")
    args = parser.parse_args(argv)

    try:
        manifest = verify_release_assets(args.assets_dir, args.commit)
    except PromotionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"ok": True, "buildTag": manifest.get("buildTag"), "commit": manifest.get("commit")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
