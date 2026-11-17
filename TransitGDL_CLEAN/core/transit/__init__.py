"""Base de transporte: construcción desde GTFS y acceso en memoria."""

from .builder import BuildReport, build_transit_db, ensure_transit_db, needs_rebuild
from .store import (
    Landmark,
    Pattern,
    Place,
    ServiceWindow,
    StopOnPattern,
    TransitStore,
)

__all__ = [
    "BuildReport",
    "Landmark",
    "Pattern",
    "Place",
    "ServiceWindow",
    "StopOnPattern",
    "TransitStore",
    "build_transit_db",
    "ensure_transit_db",
    "needs_rebuild",
]
