"""AST guard for the status vocabulary contract (CLAUDE.md, "Session write
contract" — the same discipline applies to jobs, pipeline steps, uploads and
clip exports): every one of those status values lives in ``app/domain/*`` and
is imported, never re-spelled as a bare string literal at a call site.

This scans every module under ``app/`` (excluding ``app/domain/`` itself,
which *is* the vocabulary) for a string literal that collides with one of the
six status enums, used in a way that reads or writes a ``"status"`` field:

  (a) assigned to a subscript whose slice is the constant ``"status"``
      (``x["status"] = "processing"``);
  (b) the value of a ``"status"`` key in a dict literal;
  (c) either operand of a comparison whose other operand is
      ``something["status"]`` or ``something.get("status")``;
  (d) an element of a set/list/tuple literal used as the right operand of
      ``in``/``not in`` whose left operand is a ``["status"]`` subscript or
      ``.get("status")`` call.

The forbidden set is derived from the enums themselves so it can never drift
out of sync with them. The same word ("completed", "failed", "queued", ...)
is a member of several enums at once — the point of the test is that a bare
literal is never used for *any* of them, not that we guess which one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.domain.enums import ClipExportStatus, StepStatus, UploadStatus
from app.domain.jobs import JobStatus
from app.domain.sessions import SessionStatus
from app.domain.users import UserStatus

# fastapi_backend/app
APP_ROOT = Path(__file__).resolve().parents[1] / "app"
# app/domain/ *is* the vocabulary; everything else must import from it.
DOMAIN_ROOT = APP_ROOT / "domain"

FORBIDDEN_LITERALS: frozenset[str] = frozenset(
    str(member.value)
    for enum_cls in (SessionStatus, JobStatus, StepStatus, UploadStatus, ClipExportStatus, UserStatus)
    for member in enum_cls
)

# (relative/path.py, "literal") pairs reviewed and accepted as NOT a
# re-spelling of this app's session/job/step/upload/clip-export vocabulary.
# Every entry names why, so a *new* hit at the same spot is a deliberate
# reversal to review, not a mechanical suppression.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Hatchet's own task-return convention, surfaced only in the Hatchet
        # dashboard/SDK — nothing in this app reads the dict back, so it is
        # not this app's SessionStatus/JobStatus/StepStatus vocabulary.
        ("app/queue/hatchet_tasks.py", "completed"),
    }
)


def _is_status_subscript(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "status"
    )


def _is_status_get_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "status"
    )


def _is_status_access(node: ast.AST) -> bool:
    return _is_status_subscript(node) or _is_status_get_call(node)


class _StatusLiteralVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def _flag(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in FORBIDDEN_LITERALS:
            self.hits.append((node.lineno, node.value))

    def visit_Assign(self, node: ast.Assign) -> None:
        # (a) x["status"] = "literal"
        if isinstance(node.value, ast.Constant):
            for target in node.targets:
                if _is_status_subscript(target):
                    self._flag(node.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        # (b) {"status": "literal", ...}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "status":
                self._flag(value)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for left, op, right in zip(operands, node.ops, operands[1:]):
            if isinstance(op, (ast.In, ast.NotIn)):
                # (d) something["status"] in {"a", "b"} / not in [...]
                if _is_status_access(left) and isinstance(right, (ast.List, ast.Tuple, ast.Set)):
                    for elt in right.elts:
                        self._flag(elt)
                elif _is_status_access(right):
                    self._flag(left)
            else:
                # (c) something["status"] == "literal" (either operand order)
                if _is_status_access(left):
                    self._flag(right)
                if _is_status_access(right):
                    self._flag(left)
        self.generic_visit(node)


def _iter_scanned_files() -> list[Path]:
    files = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.is_relative_to(DOMAIN_ROOT):
            continue
        files.append(path)
    return files


def test_status_vocabulary_is_never_respelled() -> None:
    project_root = APP_ROOT.parent
    failures: list[str] = []
    for path in _iter_scanned_files():
        rel = path.relative_to(project_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _StatusLiteralVisitor()
        visitor.visit(tree)
        for lineno, literal in visitor.hits:
            if (rel, literal) in ALLOWLIST:
                continue
            failures.append(f'{rel}:{lineno}: "{literal}"')

    assert not failures, (
        "Bare status-string literal(s) found where an enum from app/domain "
        "(SessionStatus, JobStatus, StepStatus, UploadStatus, ClipExportStatus, UserStatus) "
        "must be imported and used instead:\n" + "\n".join(failures)
    )
