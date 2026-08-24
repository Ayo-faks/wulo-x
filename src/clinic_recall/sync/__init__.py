"""Sync worker: external clinic data -> normalised Clinic Recall records."""

from __future__ import annotations

from .base import (
    NormalizedAppointment,
    NormalizedPatient,
    SyncSource,
    make_id,
)
from .cliniko_adapter import (
    CLINIKO_STATUS_MAP,
    ClinikoSyncQuery,
    ClinikoSyncSource,
    materialize_cliniko_source,
)
from .cliniko_availability import (
    ClinikoAvailabilityBinding,
    ClinikoAvailabilityConfigurationError,
    ClinikoAvailabilityProvider,
)
from .cliniko_client import (
    ClinikoAuthenticationError,
    ClinikoClient,
    ClinikoContractError,
    ClinikoNotFoundError,
    ClinikoPaginationError,
    ClinikoRateLimitedError,
    ClinikoServerError,
    ClinikoTransportError,
    ClinikoValidationError,
)
from .csv_adapter import (
    CSV_SCHEMA_VERSION,
    CsvMaterialization,
    CsvSafeError,
    CsvSyncError,
    CsvSyncSource,
    MaterializedCsvSource,
)
from .upsert import FlagMerge, SyncIntegrityError, SyncResult, upsert_source

__all__ = [
    "CLINIKO_STATUS_MAP",
    "ClinikoAuthenticationError",
    "ClinikoAvailabilityBinding",
    "ClinikoAvailabilityConfigurationError",
    "ClinikoAvailabilityProvider",
    "ClinikoClient",
    "ClinikoContractError",
    "ClinikoNotFoundError",
    "ClinikoPaginationError",
    "ClinikoRateLimitedError",
    "ClinikoServerError",
    "ClinikoSyncQuery",
    "ClinikoSyncSource",
    "ClinikoTransportError",
    "ClinikoValidationError",
    "CSV_SCHEMA_VERSION",
    "CsvMaterialization",
    "CsvSafeError",
    "CsvSyncError",
    "CsvSyncSource",
    "FlagMerge",
    "MaterializedCsvSource",
    "NormalizedAppointment",
    "NormalizedPatient",
    "SyncIntegrityError",
    "SyncResult",
    "SyncSource",
    "make_id",
    "materialize_cliniko_source",
    "upsert_source",
]
