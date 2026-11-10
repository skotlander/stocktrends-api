"""Live freshness for the public AI context, separate from static discovery."""

import logging
from datetime import date, datetime
from threading import Lock
from time import monotonic
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine
from payments import policy_provider
from services.intelligence_artifact_availability import (
    IntelligenceArtifactAvailabilityStatus,
    match_paid_intelligence_artifact_route,
)
from services.intelligence_artifact_store import (
    IntelligenceArtifactStoreUnavailable,
    PublicArtifactEnvelope,
    configured_intelligence_artifact_store,
    latest_artifact_by_type,
)


logger = logging.getLogger(__name__)
_DATASET_CACHE_SECONDS = 60
_dataset_cache_lock = Lock()
_RESEARCH_PATH = "/v1/intelligence/research"


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
    research_endpoint_for_latest_market_data_week: str | None = Field(
        default=None,
        description=(
            "Paid research by-ID path for a serveable report covering the latest observed market-data week. "
            "Null when a matching report cannot be identified by ID."
        ),
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


def _publication_weekdate(artifact: PublicArtifactEnvelope | None) -> date | None:
    if artifact is None:
        return None
    try:
        return date.fromisoformat(artifact.weekdate)
    except ValueError:
        return None


def _publication_metadata(
    artifact: PublicArtifactEnvelope | None, *, weekdate: date | None, store_available: bool, endpoint: str
) -> PublicationFreshness:
    status = IntelligenceArtifactAvailabilityStatus.STORE_UNAVAILABLE
    if store_available:
        status = IntelligenceArtifactAvailabilityStatus.ARTIFACT_NOT_FOUND
    if artifact is not None:
        status = IntelligenceArtifactAvailabilityStatus.AVAILABLE

    return PublicationFreshness(
        status=status,
        weekdate=weekdate,
        published_at=artifact.published_at if artifact is not None else None,
        endpoint=endpoint,
        payment_required_if_available=not policy_provider.is_public_intelligence_path(endpoint),
    )


def _is_addressable_research(artifact: PublicArtifactEnvelope) -> bool:
    target = match_paid_intelligence_artifact_route("GET", f"{_RESEARCH_PATH}/{artifact.artifact_id}")
    return target is not None and target.artifact_id == artifact.artifact_id


def read_intelligence_publication(market_week: date | None) -> IntelligencePublication:
    try:
        artifacts = configured_intelligence_artifact_store().list_valid_artifacts()
    except IntelligenceArtifactStoreUnavailable:
        artifacts = None
    except Exception as exc:
        logger.warning("Intelligence publication metadata unavailable (%s)", type(exc).__name__)
        artifacts = None

    store_available = artifacts is not None
    reports = [artifact for artifact in artifacts or [] if artifact.artifact_type == "market_research_report"]
    report_weekdates = {report.artifact_id: _publication_weekdate(report) for report in reports}
    research = latest_artifact_by_type(reports, "market_research_report")
    editorial = latest_artifact_by_type(artifacts or [], "editorial_preview")
    research_available = None
    research_endpoint = None
    if store_available and market_week is not None:
        matching_reports = [report for report in reports if report_weekdates[report.artifact_id] == market_week]
        research_available = bool(matching_reports)
        addressable_report = latest_artifact_by_type(
            [report for report in matching_reports if _is_addressable_research(report)],
            "market_research_report",
        )
        if addressable_report is not None:
            research_endpoint = f"{_RESEARCH_PATH}/{quote(addressable_report.artifact_id, safe='')}"

    return IntelligencePublication(
        research=_publication_metadata(
            research,
            weekdate=report_weekdates[research.artifact_id] if research is not None else None,
            store_available=store_available,
            endpoint=f"{_RESEARCH_PATH}/latest",
        ),
        editorial_preview=_publication_metadata(
            editorial,
            weekdate=_publication_weekdate(editorial),
            store_available=store_available,
            endpoint="/v1/intelligence/editorial/latest/preview",
        ),
        research_available_for_latest_market_data_week=research_available,
        research_endpoint_for_latest_market_data_week=research_endpoint,
    )
