"""Audit 2026-09-29 finding 1: two custom provider ids that spell the same
``OSCE_LLM_KEY_<ID>`` variable must never share it.

``clinic-a``, ``clinic_a`` and ``clinic.a`` are all valid ids and all map to
``OSCE_LLM_KEY_CLINIC_A``. If both reached one subprocess's routing, the later
key overwrote the earlier and each vendor was sent the other's credential.
Refused at the write boundary; dropped at catalogue construction for rows
stored before the check existed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.database.orm import OrmDatabase
from app.llm.catalog import builtin_catalog
from app.llm.credentials import credential_env_for, resolve_credentials
from app.llm.custom import CustomProviderError, CustomProviderSpec, key_env_name
from app.repositories.custom_provider_repository import CustomProviderRepository
from app.services.custom_provider_service import CustomProviderService


@pytest.fixture(autouse=True)
def _fake_endpoint_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.custom.resolve_hostname", lambda host: ["203.0.113.10"])


def _spec(provider_id: str, host: str) -> CustomProviderSpec:
    return CustomProviderSpec.from_raw(
        {"id": provider_id, "label": provider_id, "baseUrl": f"https://{host}.example.com/v1"}
    )


def test_the_colliding_ids_really_do_share_one_variable_name() -> None:
    # The premise of this file; if the encoding ever becomes injective the
    # remaining tests still hold, but this one documents why they exist.
    assert key_env_name("clinic-a") == key_env_name("clinic_a") == key_env_name("clinic.a")


def test_the_catalogue_never_holds_two_providers_on_one_key_variable() -> None:
    catalog = builtin_catalog().with_custom([_spec("clinic_a", "b"), _spec("clinic-a", "a")])
    custom_ids = [pid for pid in catalog.provider_ids() if catalog.is_custom(pid)]
    assert len(custom_ids) == 1
    # Deterministic regardless of the order the rows arrived in.
    assert custom_ids == ["clinic-a"]
    reversed_catalog = builtin_catalog().with_custom([_spec("clinic-a", "a"), _spec("clinic_a", "b")])
    assert [pid for pid in reversed_catalog.provider_ids() if reversed_catalog.is_custom(pid)] == ["clinic-a"]


def test_no_vendor_is_handed_another_vendors_key() -> None:
    ids = ["clinic-a", "clinic_a"]
    catalog = builtin_catalog().with_custom([_spec(ids[0], "a"), _spec(ids[1], "b")])
    forwarded = credential_env_for(
        ids, env={}, overrides={ids[0]: "FAKE_KEY_A", ids[1]: "FAKE_KEY_B"}, catalog=catalog
    )
    assert resolve_credentials("clinic-a", forwarded, catalog=catalog).api_key == "FAKE_KEY_A"
    # The dropped provider is unreachable, so it resolves nothing — never "A".
    assert resolve_credentials("clinic_a", forwarded, catalog=catalog).api_key != "FAKE_KEY_A"
    assert "FAKE_KEY_B" not in forwarded.values()


def test_distinct_variable_names_are_unaffected() -> None:
    ids = ["clinic-a", "clinic-b"]
    catalog = builtin_catalog().with_custom([_spec(ids[0], "a"), _spec(ids[1], "b")])
    forwarded = credential_env_for(ids, env={}, overrides={ids[0]: "KA", ids[1]: "KB"}, catalog=catalog)
    assert resolve_credentials("clinic-a", forwarded, catalog=catalog).api_key == "KA"
    assert resolve_credentials("clinic-b", forwarded, catalog=catalog).api_key == "KB"


def _service(tmp_path: Path) -> CustomProviderService:
    database = OrmDatabase(tmp_path / "providers.sqlite3")
    asyncio.run(database.initialize())
    return CustomProviderService(CustomProviderRepository(database))


def test_saving_an_id_whose_key_variable_is_taken_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    asyncio.run(service.save({"id": "clinic-a", "label": "A", "baseUrl": "https://a.example.com/v1"}))
    with pytest.raises(CustomProviderError) as error:
        asyncio.run(service.save({"id": "clinic_a", "label": "B", "baseUrl": "https://b.example.com/v1"}))
    assert "OSCE_LLM_KEY_CLINIC_A" in str(error.value)


def test_updating_a_provider_does_not_collide_with_itself(tmp_path: Path) -> None:
    service = _service(tmp_path)
    asyncio.run(service.save({"id": "clinic-a", "label": "A", "baseUrl": "https://a.example.com/v1"}))
    updated = asyncio.run(
        service.save(
            {"id": "clinic-a", "label": "A2", "baseUrl": "https://a.example.com/v1"},
            provider_id="clinic-a",
            allow_create=False,
        )
    )
    assert updated.label == "A2"


def test_a_disabled_row_still_reserves_its_variable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    asyncio.run(
        service.save({"id": "clinic-a", "label": "A", "baseUrl": "https://a.example.com/v1", "enabled": False})
    )
    with pytest.raises(CustomProviderError):
        asyncio.run(service.save({"id": "clinic.a", "label": "B", "baseUrl": "https://b.example.com/v1"}))
