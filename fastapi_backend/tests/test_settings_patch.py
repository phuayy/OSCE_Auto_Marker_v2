"""PATCH /api/settings: merge-only writes, and the bug they fix.

Each settings card used to GET the whole document, change the two or three
keys it owns, and PUT the lot back — so a card holding a copy from before
another card's save reverted that save the moment its own toggle flipped.
PATCH removes the read-modify-write race entirely: a card sends only the
keys it changed, and the server merges them over whatever is stored.

These tests build a bare ``FastAPI()`` with only the settings router mounted
(see ``build_client`` in ``test_llm_settings_api``), so none of ``main.py``'s
exception handlers are installed. A validation failure therefore surfaces as
FastAPI's own default: 422 with ``{"detail": ...}`` — a string for an
``HTTPException`` raised in the route, a list of error objects for a Pydantic
shape failure (e.g. an unknown key under ``extra="forbid"``). Production
answers the latter as 400 via ``main.py``'s ``RequestValidationError``
handler; that remapping is not exercised here, only the status/shape the
route itself produces.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_llm_settings_api import build_client
from tests.test_marking_plan import PANEL


def test_a_patch_writes_only_the_keys_it_names(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    client.put(
        "/api/settings",
        json={
            "llmTranscriptPreprocess": False,
            "transcriptionEngine": "canary-qwen",
            "llmPrimary": {"providerId": "openai", "model": "gpt-4.1"},
        },
    )

    patched = client.patch("/api/settings", json={"llmTranscriptPreprocess": True})

    assert patched.status_code == 200, patched.text
    settings = patched.json()["settings"]
    assert settings["llmTranscriptPreprocess"] is True
    assert settings["transcriptionEngine"] == "canary-qwen"
    assert settings["llmPrimary"] == {"providerId": "openai", "model": "gpt-4.1"}

    fetched = client.get("/api/settings").json()["settings"]
    assert fetched["llmTranscriptPreprocess"] is True
    assert fetched["transcriptionEngine"] == "canary-qwen"
    assert fetched["llmPrimary"] == {"providerId": "openai", "model": "gpt-4.1"}


def test_a_card_save_no_longer_reverts_another_cards_save(tmp_path: Path) -> None:
    """The exact regression: marking mode saved as "panel", then the
    preprocess toggle flipped, used to revert the mode back to "single"."""
    client = build_client(tmp_path)

    saved_panel = client.patch("/api/settings", json={"llmMarkingMode": "panel", "llmPanel": PANEL})
    assert saved_panel.status_code == 200, saved_panel.text

    toggled = client.patch("/api/settings", json={"llmTranscriptPreprocess": True})
    assert toggled.status_code == 200, toggled.text

    settings = client.get("/api/settings").json()["settings"]
    assert settings["llmMarkingMode"] == "panel"
    assert settings["llmPanel"] == PANEL
    assert settings["llmTranscriptPreprocess"] is True


def test_a_nested_value_in_a_patch_is_stored_whole(tmp_path: Path) -> None:
    """Top-level partial, nested whole: a panel sent with one field present
    is stored with every other panel field at its schema default, not
    omitted."""
    client = build_client(tmp_path)

    saved = client.patch("/api/settings", json={"llmPanel": {"markers": [{"providerId": "nvidia"}]}})

    assert saved.status_code == 200, saved.text
    panel = saved.json()["settings"]["llmPanel"]
    assert panel["adjudicator"] == {"providerId": "", "model": ""}
    assert panel["tieBreak"] == "lenient"
    assert panel["markers"] == [{"providerId": "nvidia", "model": ""}]


def test_a_patch_with_an_unknown_key_is_refused(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    before = client.get("/api/settings").json()["settings"]

    rejected = client.patch("/api/settings", json={"bogus": 1})

    assert rejected.status_code == 422
    assert client.get("/api/settings").json()["settings"] == before


def test_switching_to_panel_mode_is_checked_against_the_stored_panel(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    client.put(
        "/api/settings",
        json={
            "llmTranscriptPreprocess": False,
            "llmMarkingMode": "single",
            "llmPanel": {"markers": PANEL["markers"][:1]},
        },
    )

    rejected = client.patch("/api/settings", json={"llmMarkingMode": "panel"})

    assert rejected.status_code == 422
    assert "at least 2 markers" in rejected.json()["detail"]
    assert client.get("/api/settings").json()["settings"]["llmMarkingMode"] == "single"


def test_a_panel_patched_under_panel_mode_must_stay_coherent(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    client.put(
        "/api/settings",
        json={"llmTranscriptPreprocess": False, "llmMarkingMode": "panel", "llmPanel": PANEL},
    )

    broken = client.patch("/api/settings", json={"llmPanel": {**PANEL, "markers": PANEL["markers"][:1]}})
    assert broken.status_code == 422
    assert client.get("/api/settings").json()["settings"]["llmPanel"] == PANEL

    coherent = client.patch("/api/settings", json={"llmPanel": {**PANEL, "tieBreak": "strict"}})
    assert coherent.status_code == 200, coherent.text
    assert coherent.json()["settings"]["llmPanel"]["tieBreak"] == "strict"


def test_a_patch_naming_an_unknown_provider_is_refused(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    bad_primary = client.patch(
        "/api/settings", json={"llmPrimary": {"providerId": "not-a-vendor", "model": "x"}}
    )
    assert bad_primary.status_code == 422
    assert "Unknown LLM provider 'not-a-vendor'" in bad_primary.json()["detail"]

    bad_adjudicator = client.patch(
        "/api/settings",
        json={"llmPanel": {**PANEL, "adjudicator": {"providerId": "acme", "model": "x"}}},
    )
    assert bad_adjudicator.status_code == 422


def test_an_empty_patch_answers_with_the_current_settings_and_writes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    client = build_client(tmp_path)
    before = client.get("/api/settings").json()["settings"]

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("set_values must not be called for an empty patch")

    monkeypatch.setattr(client.app.state.container.app_settings, "set_values", _must_not_be_called)

    empty = client.patch("/api/settings", json={})

    assert empty.status_code == 200
    assert empty.json()["settings"] == before


def test_a_patch_tolerates_a_stored_key_this_release_does_not_know(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    container = client.app.state.container
    asyncio.run(container.app_settings.set_values({"futureKey": 1}))

    current = client.get("/api/settings").json()["settings"]
    assert current["futureKey"] == 1

    # extra="forbid" means a whole-document PUT of a row carrying a key this
    # release does not declare is refused outright.
    full_replace = client.put("/api/settings", json=current)
    assert full_replace.status_code == 422

    patched = client.patch("/api/settings", json={"llmTranscriptPreprocess": True})
    assert patched.status_code == 200, patched.text
    assert patched.json()["settings"]["futureKey"] == 1

    fetched = client.get("/api/settings").json()["settings"]
    assert fetched["futureKey"] == 1
    assert fetched["llmTranscriptPreprocess"] is True
