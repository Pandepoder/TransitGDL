from __future__ import annotations

import logging
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

MEXICO_CITY_TZ_NAME = "America/Mexico_City"
MEXICO_CITY_FALLBACK = timezone(timedelta(hours=-6), name=MEXICO_CITY_TZ_NAME)


def get_timezone(name: str = MEXICO_CITY_TZ_NAME) -> tzinfo:
    """
    Resolve an IANA timezone and fall back to a fixed UTC-6 offset on systems
    without tzdata (common on fresh Windows installs).
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == MEXICO_CITY_TZ_NAME:
            logger.warning("Timezone %s not available; using UTC-6 fallback.", name)
            return MEXICO_CITY_FALLBACK
        raise


GDL_TZ = get_timezone()
