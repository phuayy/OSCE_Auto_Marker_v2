"""The account vocabulary: normalisation and the password policy, as pure functions."""

from __future__ import annotations

import pytest

from app.domain.users import (
    PASSWORD_MAX_BYTES,
    PASSWORD_MIN_LENGTH,
    UserRole,
    UserStatus,
    UserVocabularyError,
    normalize_display_name,
    normalize_email,
    normalize_login_identifier,
    normalize_username,
    parse_role,
    parse_status,
    password_policy_errors,
)


def test_email_is_lowercased_trimmed_and_shape_checked() -> None:
    assert normalize_email("  Dr.Marker@Example.EDU ") == "dr.marker@example.edu"
    for bad in ("", "   ", "no-at-sign", "two@@example.edu", "spaces in@example.edu", "no-dot@localhost"):
        with pytest.raises(UserVocabularyError):
            normalize_email(bad)


def test_username_is_lowercased_and_restricted() -> None:
    assert normalize_username(" Admin ") == "admin"
    assert normalize_username("marker@example.edu") == "marker@example.edu"
    for bad in ("", "-leading-dash", "has space", "semi;colon", "a" * 200):
        with pytest.raises(UserVocabularyError):
            normalize_username(bad)


def test_login_identifier_never_raises() -> None:
    assert normalize_login_identifier("  ADMIN ") == "admin"
    assert normalize_login_identifier(None) == ""
    assert len(normalize_login_identifier("x" * 1000)) == 320


def test_display_name_collapses_whitespace_and_caps_length() -> None:
    assert normalize_display_name("  Dr   Jane\tMarker ") == "Dr Jane Marker"
    assert len(normalize_display_name("n" * 500)) == 120
    assert normalize_display_name(None) == ""


def test_roles_and_statuses_parse_case_insensitively_with_a_default() -> None:
    assert parse_role(" Admin ") is UserRole.ADMIN
    assert parse_role("", default=UserRole.MARKER) is UserRole.MARKER
    assert parse_status("DISABLED") is UserStatus.DISABLED
    with pytest.raises(UserVocabularyError):
        parse_role("superuser")
    with pytest.raises(UserVocabularyError):
        parse_role("")
    with pytest.raises(UserVocabularyError):
        parse_status("archived")


def test_password_policy_is_length_over_complexity() -> None:
    assert password_policy_errors("a" * PASSWORD_MIN_LENGTH) == []
    assert any("at least" in error for error in password_policy_errors("a" * (PASSWORD_MIN_LENGTH - 1)))
    # bcrypt reads 72 bytes; refuse what it would silently truncate.
    assert any("at most" in error for error in password_policy_errors("a" * (PASSWORD_MAX_BYTES + 1)))
    assert any("at most" in error for error in password_policy_errors("é" * 40))  # 80 bytes
    # No character-class rules: a long lowercase phrase is fine.
    assert password_policy_errors("correct horse battery staple") == []


def test_password_may_not_be_the_accounts_own_identifier() -> None:
    forbidden = ("marker@example.edu", "marker")
    assert password_policy_errors("Marker@Example.edu", forbidden=forbidden)
    assert password_policy_errors("marker@example.edu ", forbidden=forbidden)
    assert password_policy_errors("marker@example.edu-plus", forbidden=forbidden) == []
    # A blank forbidden entry (no email) is ignored rather than matching "".
    assert password_policy_errors("x" * 12, forbidden=("", None)) == []
