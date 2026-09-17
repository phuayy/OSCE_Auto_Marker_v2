from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.dependencies import current_actor, get_container
from app.llm import custom as custom_providers
from app.llm.panel import MarkingMode, PanelConfig, parse_marking_mode
from app.llm.routing import LLMTarget
from app.pipeline import person_presets
from app.pipeline import region_focus
from app.repositories.app_settings_repository import (
    LLM_FALLBACKS_KEY,
    LLM_MARKING_MODE_KEY,
    LLM_PANEL_KEY,
    LLM_PRIMARY_KEY,
)
from app.schemas.settings import (
    CustomProviderRequest,
    PatchSettingsRequest,
    SetProviderKeyRequest,
    TestLLMTargetRequest,
    UpdateSettingsRequest,
)
from app.services.container import AppContainer
from app.services.provider_credential_service import CredentialError

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    return {"settings": await container.app_settings.get_all()}


@router.get("/transcription-engines")
async def get_transcription_engines(
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Engines this build ships, their option schemas, this deployment's
    defaults for each, and whether each one can actually run here.

    The settings screen renders its controls from this response, so shipping a
    new engine needs no frontend change.
    """
    return await container.transcription.describe()


@router.get("/segmentation-presets")
async def get_segmentation_presets() -> dict[str, object]:
    """Occupancy presets the person detector offers, and their numbers.

    Same contract as the transcription-engines and llm-providers endpoints: the
    upload screen renders its picker from this response, so retuning a preset
    or adding one is a backend change with no frontend edit. The bounds are
    published alongside so the custom form validates the same range the API
    enforces instead of hardcoding a second copy.
    """
    return {
        "presets": person_presets.describe_presets(),
        "defaultPreset": person_presets.DEFAULT_PRESET,
        "customPreset": person_presets.CUSTOM_PRESET,
        "bounds": {
            "minPeople": list(person_presets.MIN_PEOPLE_RANGE),
            "minBoxHeightRatio": list(person_presets.MIN_BOX_HEIGHT_RATIO_RANGE),
            "minSessionSeconds": list(person_presets.MIN_SESSION_SECONDS_RANGE),
        },
        # Independent of the preset above (which side(s) of the frame count at
        # all, rather than how many people). Same one-source-of-truth contract:
        # the upload screen renders its checkboxes/sliders from this.
        "regionFocus": region_focus.describe_defaults(),
    }


@router.get("/llm-providers")
async def get_llm_providers(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Scoring providers this build ships, their models, and the current routing.

    Same contract as the transcription-engines endpoint: the settings screen
    renders its dropdowns from this response, so a provider added to the
    backend registry appears in the UI with no frontend change. Availability
    reflects whether a key is configured *on this machine* — the response never
    contains a key itself.
    """
    return await container.llm_settings.describe()


@router.post("/llm-providers/test")
async def test_llm_provider(
    payload: TestLLMTargetRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """One live round trip to a provider, so a bad key is found in the settings
    screen rather than forty minutes into a scoring run.

    Always 200: a failed probe is a *result* the screen renders, not an API
    error, and the body carries the provider's own message plus the attempt
    trail. Any credential that appears in that message is redacted first — some
    vendors quote the key they rejected.
    """
    return await container.llm_settings.test_target(
        payload.providerId,
        payload.model,
        api_key=payload.apiKey,
    )


async def _resolve_provider_id(container: AppContainer, provider_id: str) -> str:
    """The provider id, checked against what this *deployment* offers.

    Not against what the build ships: a provider an operator defined is just as
    real as a shipped one, and has the same right to hold a key. The catalogue
    read is cached and normally costs no query.
    """
    resolved = str(provider_id or "").strip()
    catalog = await container.llm_settings.catalog()
    if not catalog.contains(resolved):
        known = ", ".join(catalog.provider_ids())
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown LLM provider '{resolved}'. Available: {known}.",
        )
    return resolved


def _actor(request: Request) -> str:
    """The username recorded as ``updated_by`` on a provider or credential row."""
    actor = current_actor(request)
    return actor.username if actor is not None else ""


@router.post("/llm-providers", status_code=status.HTTP_201_CREATED)
async def create_llm_provider(
    payload: CustomProviderRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Define a scoring provider this build does not ship.

    Everything needed to reach a platform - endpoint, where the credential goes,
    API version, organisation/project/account identifiers, extra headers, query
    parameters and body switches - and deliberately not the model id, which is a
    separate decision with a separate lifetime and already has its own card.

    An ``apiKey`` in the body is forwarded to the same encrypted credential store
    every shipped provider uses and is never written to the provider row, so
    adding a vendor is one action while the key still gets the sealing, the
    write-only contract and the rotation semantics it would get on its own.

    The answer is the full provider description, so the screen refreshes in one
    round trip - and, as everywhere else, without the key travelling back.
    """
    return await _write_custom_provider(container, request, payload, allow_create=True)


@router.put("/llm-providers/{provider_id}")
async def update_llm_provider(
    provider_id: str,
    payload: CustomProviderRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Replace one custom provider's definition.

    A replace, not a merge: the form sends the whole definition, and merging
    would make "clear this header" impossible to express. The id comes from the
    path, so a body that names a different one cannot rename a provider out from
    under the routing that points at it.
    """
    return await _write_custom_provider(
        container, request, payload, allow_create=False, provider_id=provider_id
    )


async def _write_custom_provider(
    container: AppContainer,
    request: Request,
    payload: CustomProviderRequest,
    *,
    allow_create: bool,
    provider_id: str = "",
) -> dict[str, object]:
    if container.custom_providers is None:  # pragma: no cover - always wired by the container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Custom scoring providers are not enabled on this server.",
        )
    actor = _actor(request)
    try:
        spec = await container.custom_providers.save(
            payload.definition(),
            provider_id=provider_id,
            actor=actor,
            allow_create=allow_create,
        )
    except custom_providers.CustomProviderError as error:
        # 422, not 400: this is a well-formed request whose *content* the domain
        # rejects, and it lands next to Pydantic's own report in the screen.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from None

    if payload.apiKey.strip():
        try:
            await container.provider_credentials.set_key(spec.id, payload.apiKey, actor=actor)
        except CredentialError as error:
            # The definition is already stored, so this is reported as a partial
            # success rather than rolled back: the provider is configured and
            # visible, and the operator only has to re-enter the key. Silently
            # discarding a provider they just described would be worse.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Provider '{spec.id}' was saved, but its API key was not: {error} "
                    "Enter the key again in the API keys card."
                ),
            ) from None
    return await container.llm_settings.describe()


@router.delete("/llm-providers/{provider_id}")
async def delete_llm_provider(
    provider_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Remove a custom provider and the key stored for it.

    The credential goes with the definition. Leaving it behind would orphan an
    encrypted row that nothing can ever use again, and would quietly re-arm the
    provider if an id were later reused.

    Routing that still names this provider is not an error: the router already
    drops targets it cannot build and promotes the first usable fallback, which
    is exactly the behaviour wanted when a vendor is decommissioned mid-queue.
    Providers this build ships cannot be deleted - they are a property of the
    release, not of the deployment.
    """
    resolved = str(provider_id or "").strip()
    if container.custom_providers is None:  # pragma: no cover - always wired by the container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Custom scoring providers are not enabled on this server.",
        )
    if await container.custom_providers.get_spec(resolved) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"'{resolved}' is not a custom provider. Providers that ship with this build "
                "cannot be removed."
            ),
        )
    actor = _actor(request)
    await container.custom_providers.delete(resolved, actor=actor)
    await container.provider_credentials.clear_key(resolved, actor=actor)
    return await container.llm_settings.describe()


@router.put("/llm-providers/{provider_id}/key")
async def set_llm_provider_key(
    provider_id: str,
    payload: SetProviderKeyRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Store or rotate one provider's API key.

    Write-only by design: the response is the same provider description every
    other settings read gets, so the screen refreshes in one round trip without
    the key ever travelling back. The new key applies to the next scoring run in
    every process — no restart, no redeploy — which is the whole point of
    letting an operator rotate here.
    """
    resolved = await _resolve_provider_id(container, provider_id)
    actor = _actor(request)
    try:
        await container.provider_credentials.set_key(resolved, payload.apiKey, actor=actor)
    except CredentialError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
    return await container.llm_settings.describe()


@router.delete("/llm-providers/{provider_id}/key")
async def clear_llm_provider_key(
    provider_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Revoke the stored key for one provider.

    Deleting here does not revoke it at the vendor — that is a separate action
    on their dashboard — but it does stop this deployment sending it, which is
    the half an operator can do from this screen during an incident. Whatever
    the environment configures, if anything, takes over again.
    """
    resolved = await _resolve_provider_id(container, provider_id)
    actor = _actor(request)
    await container.provider_credentials.clear_key(resolved, actor=actor)
    return await container.llm_settings.describe()


async def _check_settings_document(container: AppContainer, document: dict[str, Any]) -> None:
    """The rules that span more than one key, run against the document that
    would be stored.

    Spelled once for PUT and PATCH. Every routing target — primary, fallbacks,
    panel markers, adjudicator — must name a provider this *deployment*
    offers, which is a database question a Pydantic validator cannot ask (see
    the schema module). And a panel must be coherent only when ``panel`` is the
    mode that will run, so an operator can build one up under single mode.

    PATCH hands in stored ∪ patch: a patch that flips the mode is judged
    against the panel it will actually run with, and a patch that edits the
    panel is judged under the mode already stored. The tolerant ``from_raw``
    parsers are used because half of that document may be a row written by
    another release.
    """
    catalog = await container.llm_settings.catalog()
    panel = PanelConfig.from_raw(document.get(LLM_PANEL_KEY))
    fallbacks_raw = document.get(LLM_FALLBACKS_KEY)
    targets = [
        LLMTarget.from_raw(document.get(LLM_PRIMARY_KEY)),
        *(LLMTarget.from_raw(item) for item in (fallbacks_raw if isinstance(fallbacks_raw, list) else [])),
        *panel.markers,
        panel.adjudicator,
    ]
    for target in targets:
        # from_raw answers None for a blank row, so anything left names a provider.
        if target is not None and not catalog.contains(target.provider_id):
            known = ", ".join(catalog.provider_ids())
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown LLM provider '{target.provider_id}'. Available: {known}.",
            )
    if parse_marking_mode(document.get(LLM_MARKING_MODE_KEY)) is MarkingMode.PANEL:
        validation = panel.validate()
        if not validation.ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=" ".join(validation.errors),
            )


@router.put("")
async def update_settings(
    payload: UpdateSettingsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Replace the global settings wholesale.

    Kept for API compatibility; the settings screen's cards use PATCH so one
    card cannot carry another card's stale values back. The cross-field rules
    live in ``_check_settings_document`` — same status code, same messages.
    """
    document = payload.model_dump()
    await _check_settings_document(container, document)
    return {"settings": await container.app_settings.set_values(document)}


@router.patch("")
async def patch_settings(
    payload: PatchSettingsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Change some settings and leave the rest exactly as stored.

    The screen is a set of independent cards, each owning a few keys. With
    only a whole-document PUT, every card read the document and sent it all
    back — and a card holding a copy from before another card's save put the
    old values back. Saving the marking mode, then flipping the preprocess
    toggle, reverted the marking mode. A patch names only the keys it
    changes, so no card can carry another's; and a stored key this release
    does not know is left alone instead of failing the write.

    Cross-field rules run against stored ∪ patch — the document that will
    exist afterwards. An empty patch answers with the current settings and
    writes nothing, so it cannot evict every process's cache for no change.
    """
    changes = payload.changes()
    stored = await container.app_settings.get_all()
    if not changes:
        return {"settings": stored}
    await _check_settings_document(container, {**stored, **changes})
    return {"settings": await container.app_settings.set_values(changes)}
