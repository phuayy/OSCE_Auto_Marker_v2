from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.schemas.corpora import CorpusPayload
from app.services.container import AppContainer

router = APIRouter(prefix="/corpora", tags=["corpora"])


@router.get("")
async def list_corpora(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    return {"corpora": await container.corpora.list_rows()}


@router.post("")
async def create_corpus(
    payload: CorpusPayload,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        corpus = await container.corpora.create(payload.name, payload.terms)
    except IntegrityError as error:
        raise AppError("A corpus with this name already exists.", status_code=409) from error
    return {"corpus": corpus}


@router.put("/{corpus_id}")
async def update_corpus(
    corpus_id: str,
    payload: CorpusPayload,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        corpus = await container.corpora.update(corpus_id, payload.name, payload.terms)
    except IntegrityError as error:
        raise AppError("A corpus with this name already exists.", status_code=409) from error
    if corpus is None:
        raise AppError("Corpus not found.", status_code=404)
    return {"corpus": corpus}


@router.delete("/{corpus_id}")
async def delete_corpus(
    corpus_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if not await container.corpora.delete(corpus_id):
        raise AppError("Corpus not found.", status_code=404)
    return {"deleted": True}
