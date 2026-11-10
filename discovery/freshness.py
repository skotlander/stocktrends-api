"""Live freshness for the public AI context, separate from static discovery."""

import logging
from datetime import date, datetime
from threading import Lock
from time import monotonic
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine
from services.intelligence_artifact_availability import IntelligenceArtifactAvailabilityStatus
from services.intelligence_artifact_store import (
    IntelligenceArtifactStoreUnavailable,
    PublicArtifactEnvelope,
    configured_intelligence_artifact_store,
    latest_artifact_by_type,
)


logger = logging.getLogger(__name__)
_DATASET_CACHE_SECONDS = 60
_dataset_cache_lock = Lock()


class DatasetFreshness(BaseModel, frozen=True):
    status: Literal["observed", "empty", "unavailable"] = Field(
        description="Whether weekdates were observed, absent in the source, or could not be read."
    )
    latest_market_data_weekdate: date | None = Field(
        default=None,
        description="Newest week-ending date observed in the core weekly market dataset. Bounds are cached for 60 seconds.",
    )
    historical_start_weekdate: date | None = Field(default=None, description="Oldest observed week-ending date.")
    historical_end_weekdate: date | None = Field(default=None, description="Newest observed week-ending date.")
    coverage_scope: str = Field(
        default=(
            "Core weekly market observations used for indicators and price context. "
            "History can have gaps and vary by instrument and exchange. "
            "ST-IM, selections, market aggregates, and portfolios have their own sources, "
            "filters, and coverage."
        ),
        description="Scope and limits of the observed coverage bounds.",
    )
    update_frequency: Literal["weekly"] = Field(default="weekly", description="Expected dataset cadence.")
    completeness_status: Literal["unverified"] = Field(
        default="unverified", description="Verifying a completed weekly load requires a separate ingestion marker."
    )
    latest_successful_refresh_at: datetime | None = Field(
        default=None, description="Successful dataset refresh timestamp, null without an authoritative marker."
    )


class PublicationFreshness(BaseModel):
    status: IntelligenceArtifactAvailabilityStatus = Field(
        description="Availability in the active API artifact store after serveability checks."
    )
    weekdate: date | None = Field(default=None, description="Week-ending date covered by this publication.")
    published_at: str | None = Field(
        default=None, description="Publication timestamp from the served envelope, independent of dataset refresh."
    )
    endpoint: str = Field(description="API route serving the latest artifact when available.")
    payment_required_if_available: bool = Field(
        description="Whether retrieving the artifact requires paid access when it is available."
    )


class IntelligencePublication(BaseModel):
    research: PublicationFreshness = Field(description="Latest activated market research report metadata.")
    editorial_preview: PublicationFreshness = Field(description="Latest public editorial preview metadata.")
    research_available_for_latest_market_data_week: bool | None = Field(
        description=(
            "Whether any serveable research report covers the observed latest market-data week. "
            "Null if the dataset weekdate or store availability is unknown."
        )
    )


class AiContextFreshnessResponse(BaseModel, extra="allow"):
    """Freshness fields alongside the existing AI context metadata and guidance."""

    last_update: date | None = Field(description="Legacy alias of dataset_freshness.latest_market_data_weekdate.")
    update_frequency: Literal["weekly"] = Field(description="Legacy dataset cadence field.")
    dataset_freshness: DatasetFreshness
    intelligence_publication: IntelligencePublication


_dataset_cache: tuple[float, DatasetFreshness] | None = None


def read_dataset_freshness() -> DatasetFreshness:
    """Read both bounds from one database snapshot, at most once per TTL per worker."""
    global _dataset_cache
    with _dataset_cache_lock:
        if _dataset_cache is not None and monotonic() < _dataset_cache[0]:
            return _dataset_cache[1]

        try:
            with get_engine().connect() as conn:
                first_week, latest_week = conn.execute(
                    text("SELECT /*+ MAX_EXECUTION_TIME(2000) */ MIN(weekdate), MAX(weekdate) FROM st_data")
                ).fetchone()
            freshness = DatasetFreshness(
                status="observed" if latest_week is not None else "empty",
                latest_market_data_weekdate=latest_week,
                historical_start_weekdate=first_week,
                historical_end_weekdate=latest_week,
            )
        except Exception as exc:
            logger.warning("Market dataset freshness unavailable (%s)", type(exc).__name__)
            freshness = DatasetFreshness(status="unavailable")

        _dataset_cache = (monotonic() + _DATASET_CACHE_SECONDS, freshness)
        return freshness


def _publication_metadata(
    artifact: PublicArtifactEnvelope | None, *, store_available: bool, endpoint: str, paid: bool
) -> PublicationFreshness:
    status = IntelligenceArtifactAvailabilityStatus.STORE_UNAVAILABLE
    if store_available:
        status = IntelligenceArtifactAvailabilityStatus.ARTIFACT_NOT_FOUND
    if artifact is not None:
        status = IntelligenceArtifactAvailabilityStatus.AVAILABLE

    weekdate = None
    if artifact is not None:
        try:
            weekdate = date.fromisoformat(artifact.weekdate)
        except ValueError:
            pass
    return PublicationFreshness(
        status=status,
        weekdate=weekdate,
        published_at=artifact.published_at if artifact is not None else None,
        endpoint=endpoint,
        payment_required_if_available=paid,
    )


def read_intelligence_publication(market_week: date | None) -> IntelligencePublication:
    try:
        artifacts = configured_intelligence_artifact_store().list_valid_artifacts()
    except IntelligenceArtifactStoreUnavailable:
        artifacts = None

    store_available = artifacts is not None
    research = latest_artifact_by_type(artifacts or [], "market_research_report")
    editorial = latest_artifact_by_type(artifacts or [], "editorial_preview")
    matching_research = None
    if artifacts is not None and market_week is not None:
        market_weekdate = market_week.isoformat()
        matching_research = any(
            artifact.artifact_type == "market_research_report" and artifact.weekdate == market_weekdate
            for artifact in artifacts
        )
    return IntelligencePublication(
        research=_publication_metadata(
            research,
            store_available=store_available,
            endpoint="/v1/intelligence/research/latest",
            paid=True,
        ),
        editorial_preview=_publication_metadata(
            editorial,
            store_available=store_available,
            endpoint="/v1/intelligence/editorial/latest/preview",
            paid=False,
        ),
        research_available_for_latest_market_data_week=matching_research,
    )
