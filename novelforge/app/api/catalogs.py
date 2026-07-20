"""Backend-owned writing profile and genre catalogs."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...writing_profiles import (
    CATALOG_VERSION,
    SUPPORTED_CONTENT_LANGUAGES,
    SUPPORTED_MARKETS,
    list_genres,
    list_writing_profiles,
)


router = APIRouter(prefix="/catalogs", tags=["catalogs"])


@router.get("/writing-profiles")
def writing_profile_catalog(
    language: str | None = None,
    publication_format: str | None = Query(default=None, alias="format"),
    market: str | None = None,
):
    if language and language not in SUPPORTED_CONTENT_LANGUAGES:
        raise HTTPException(status_code=422, detail=f"unsupported language: {language}")
    if market and market not in SUPPORTED_MARKETS:
        raise HTTPException(status_code=422, detail=f"unsupported market: {market}")
    return {
        "catalog_version": CATALOG_VERSION,
        "items": list_writing_profiles(
            language=language,
            publication_format=publication_format,
            market=market,
        ),
    }


@router.get("/genres")
def genre_catalog(language: str = "zh-CN", market: str | None = None):
    if language not in SUPPORTED_CONTENT_LANGUAGES:
        raise HTTPException(status_code=422, detail=f"unsupported language: {language}")
    return {
        "catalog_version": CATALOG_VERSION,
        "items": list_genres(language=language, market=market),
    }
