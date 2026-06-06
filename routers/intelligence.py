from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Request

from services.intelligence_artifact_store import (
    STORE_ENV_VAR,
    IntelligenceArtifactStoreUnavailable,
    PublicArtifactEnvelope,
    configured_intelligence_artifact_store,
)


router = APIRouter(prefix="/intelligence", tags=["intelligence"])


def _store_unavailable(exc: IntelligenceArtifactStoreUnavailable) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "intelligence_artifact_store_unavailable",
            "message": "Published intelligence artifacts are not available.",
            "config": {"env_var": STORE_ENV_VAR},
        },
    )


def _not_found(
    request: Request,
    *,
    artifact_type: str,
    artifact_id: str | None = None,
) -> HTTPException:
    detail = {
        "request_id": getattr(request.state, "request_id", None),
        "error": "intelligence_artifact_not_found",
        "artifact_type": artifact_type,
    }
    if artifact_id is not None:
        detail["artifact_id"] = artifact_id
    return HTTPException(status_code=404, detail=detail)


def _validate_artifact_id(artifact_id: str) -> str:
    if not artifact_id or "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_artifact_id",
                "message": "artifact_id must be a single manifest artifact identifier.",
            },
        )
    return artifact_id


def _latest_artifact(request: Request, artifact_type: str) -> PublicArtifactEnvelope:
    try:
        artifact = configured_intelligence_artifact_store().get_latest(artifact_type)  # type: ignore[arg-type]
    except IntelligenceArtifactStoreUnavailable as exc:
        raise _store_unavailable(exc) from exc

    if artifact is None:
        raise _not_found(request, artifact_type=artifact_type)
    return artifact


def _artifact_by_id(
    request: Request,
    *,
    artifact_type: str,
    artifact_id: str,
) -> PublicArtifactEnvelope:
    safe_artifact_id = _validate_artifact_id(artifact_id)
    try:
        artifact = configured_intelligence_artifact_store().get_by_id(
            safe_artifact_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]
        )
    except IntelligenceArtifactStoreUnavailable as exc:
        raise _store_unavailable(exc) from exc

    if artifact is None:
        raise _not_found(
            request,
            artifact_type=artifact_type,
            artifact_id=safe_artifact_id,
        )
    return artifact


@router.get(
    "/discovery",
    response_model=PublicArtifactEnvelope,
    response_model_exclude_none=True,
    summary="Latest intelligence artifact discovery metadata",
    description=(
        "Public/free. Returns the latest valid published or publish-ready "
        "discovery_metadata envelope from the "
        "configured public intelligence artifact store. The API reads exported "
        "Agent envelopes only and does not generate artifacts on request."
    ),
)
def intelligence_discovery(request: Request) -> PublicArtifactEnvelope:
    return _latest_artifact(request, "discovery_metadata")


@router.get(
    "/guidance/latest",
    response_model=PublicArtifactEnvelope,
    response_model_exclude_none=True,
    summary="Latest published market guidance artifact",
    description=(
        "Paid intelligence product. Returns the latest valid published or product-grade "
        "market_guidance envelope from the configured public intelligence artifact store. "
        "No Agent runtime code is called."
    ),
)
def intelligence_guidance_latest(request: Request) -> PublicArtifactEnvelope:
    return _latest_artifact(request, "market_guidance")


@router.get(
    "/guidance/{artifact_id}",
    response_model=PublicArtifactEnvelope,
    response_model_exclude_none=True,
    summary="Published market guidance artifact by id",
    description=(
        "Paid intelligence product. Returns a valid published or product-grade "
        "market_guidance envelope by artifact_id. A manifest id for another artifact "
        "type returns 404."
    ),
)
def intelligence_guidance_by_id(
    request: Request,
    artifact_id: str = Path(..., min_length=1),
) -> PublicArtifactEnvelope:
    return _artifact_by_id(
        request,
        artifact_type="market_guidance",
        artifact_id=artifact_id,
    )


@router.get(
    "/research/latest",
    response_model=PublicArtifactEnvelope,
    response_model_exclude_none=True,
    summary="Latest published market research artifact",
    description=(
        "Paid intelligence product. Returns the latest valid published or product-grade "
        "market_research_report envelope from the configured public intelligence "
        "artifact store. No Agent runtime code is called."
    ),
)
def intelligence_research_latest(request: Request) -> PublicArtifactEnvelope:
    return _latest_artifact(request, "market_research_report")


@router.get(
    "/research/{artifact_id}",
    response_model=PublicArtifactEnvelope,
    response_model_exclude_none=True,
    summary="Published market research artifact by id",
    description=(
        "Paid intelligence product. Returns a valid published or product-grade "
        "market_research_report envelope by artifact_id. A manifest id for another "
        "artifact type returns 404."
    ),
)
def intelligence_research_by_id(
    request: Request,
    artifact_id: str = Path(..., min_length=1),
) -> PublicArtifactEnvelope:
    return _artifact_by_id(
        request,
        artifact_type="market_research_report",
        artifact_id=artifact_id,
    )


@router.get(
    "/editorial/latest/preview",
    response_model=PublicArtifactEnvelope,
    response_model_exclude_none=True,
    summary="Latest public editorial preview artifact",
    description=(
        "Public/free. Returns the latest valid published or publish-ready "
        "editorial_preview envelope from the configured public intelligence artifact "
        "store. Preview routes never generate editorial content on request."
    ),
)
def intelligence_editorial_latest_preview(request: Request) -> PublicArtifactEnvelope:
    return _latest_artifact(request, "editorial_preview")
