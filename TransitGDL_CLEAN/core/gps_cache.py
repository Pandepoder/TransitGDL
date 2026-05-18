from __future__ import annotations

import json
import logging
import time
from typing import Optional

from .config import CONFIG
from .models import BusPosition

logger = logging.getLogger(__name__)

# Tiempo mínimo entre intentos de reconexión a Redis (segundos).
# Evita bombardear Redis con conexiones si sigue caído.
_REDIS_RECONNECT_INTERVAL = 60.0


class GPSCache:
    """
    Caché de posiciones GPS con Redis opcional y fallback en memoria.
    Si Redis falla, usa memoria local y reintenta reconectar cada
    _REDIS_RECONNECT_INTERVAL segundos en lugar de quedar en memoria
    de forma permanente.
    """

    REDIS_KEY_PREFIX = "gps:vehicle:"
    ROUTE_INDEX_PREFIX = "gps:route:"

    def __init__(self, redis_url: Optional[str] = None):
        self._redis_url = redis_url or CONFIG.redis_url
        self._redis = None
        self._memory: dict[str, BusPosition] = {}
        self._use_memory = False
        self._last_redis_failure: float = 0.0

    async def connect(self) -> "GPSCache":
        if CONFIG.use_memory_cache or not (self._redis_url or "").strip():
            logger.info("GPSCache: modo memoria (USE_MEMORY_CACHE o sin REDIS_URL).")
            self._redis = None
            self._use_memory = True
            return self

        try:
            import redis.asyncio as aioredis  # type: ignore[import-untyped]

            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await self._redis.ping()
            logger.info("GPSCache: conectado a Redis.")
        except Exception as exc:
            logger.warning(
                "GPSCache: Redis no disponible (%s). Usando caché en memoria.",
                exc,
            )
            self._redis = None
            self._use_memory = True
            self._last_redis_failure = time.monotonic()
        return self

    async def _try_reconnect_redis(self) -> bool:
        """
        Intenta reconectar a Redis si ha pasado suficiente tiempo desde
        el último fallo. Devuelve True si la reconexión fue exitosa.
        Usa getattr para tolerar objetos construidos con __new__ en tests.
        """
        redis_url = getattr(self, "_redis_url", None)
        if not redis_url:
            return False

        last_failure = getattr(self, "_last_redis_failure", 0.0)
        if time.monotonic() - last_failure < _REDIS_RECONNECT_INTERVAL:
            return False
        try:
            import redis.asyncio as aioredis  # type: ignore[import-untyped]

            candidate = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await candidate.ping()
            self._redis = candidate
            self._use_memory = False
            logger.info("GPSCache: reconectado a Redis exitosamente.")
            return True
        except Exception as exc:
            logger.debug("GPSCache: intento de reconexión fallido: %s", exc)
            self._last_redis_failure = time.monotonic()
            self._redis = None
            return False

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass

    async def update(self, position: BusPosition) -> None:
        key = f"{self.REDIS_KEY_PREFIX}{position.vehicle_id}"
        payload = json.dumps(self._serialize(position))

        if self._use_memory or self._redis is None:
            # Intentar volver a Redis si ya pasó el intervalo de backoff
            if not await self._try_reconnect_redis():
                self._memory[position.vehicle_id] = position
                stale_ids = [vid for vid, pos in self._memory.items() if pos.is_stale]
                for vid in stale_ids:
                    del self._memory[vid]
                return

        try:
            pipe = self._redis.pipeline()
            pipe.setex(key, CONFIG.gps_position_ttl_seconds, payload)
            route_key = f"{self.ROUTE_INDEX_PREFIX}{position.route_id}"
            pipe.sadd(route_key, position.vehicle_id)
            pipe.expire(route_key, CONFIG.gps_position_ttl_seconds * 2)
            await pipe.execute()
        except Exception as exc:
            logger.warning("GPSCache.update error: %s", exc)
            self._use_memory = True
            self._last_redis_failure = time.monotonic()
            self._memory[position.vehicle_id] = position

    async def update_bulk(self, positions: list[BusPosition]) -> None:
        for pos in positions:
            await self.update(pos)

    async def get_positions_for_route(self, route_id: str) -> list[BusPosition]:
        if self._use_memory or self._redis is None:
            # Intentar volver a Redis antes de servir desde memoria
            if not await self._try_reconnect_redis():
                return [
                    p
                    for p in self._memory.values()
                    if p.route_id == route_id and not p.is_stale
                ]

        try:
            route_key = f"{self.ROUTE_INDEX_PREFIX}{route_id}"
            vehicle_ids = await self._redis.smembers(route_key)
            if not vehicle_ids:
                return []

            keys = [f"{self.REDIS_KEY_PREFIX}{vid}" for vid in vehicle_ids]
            raw_list = await self._redis.mget(keys)

            positions: list[BusPosition] = []
            for raw in raw_list:
                if not raw:
                    continue
                try:
                    pos = self._deserialize(json.loads(raw))
                    if not pos.is_stale:
                        positions.append(pos)
                except Exception:
                    continue
            return positions
        except Exception as exc:
            logger.warning("GPSCache.get_positions_for_route error: %s", exc)
            self._use_memory = True
            self._last_redis_failure = time.monotonic()
            return [
                p
                for p in self._memory.values()
                if p.route_id == route_id and not p.is_stale
            ]

    async def count_active_vehicles(self, route_id: str) -> int:
        return len(await self.get_positions_for_route(route_id))

    async def get_all_positions(self) -> list[BusPosition]:
        if self._use_memory or self._redis is None:
            if not await self._try_reconnect_redis():
                return [p for p in self._memory.values() if not p.is_stale]

        try:
            positions: list[BusPosition] = []
            cursor = 0
            while True:
                cursor, keys = await self._redis.scan(
                    cursor=cursor,
                    match=f"{self.REDIS_KEY_PREFIX}*",
                    count=200,
                )
                if keys:
                    raw_list = await self._redis.mget(keys)
                    for raw in raw_list:
                        if not raw:
                            continue
                        try:
                            pos = self._deserialize(json.loads(raw))
                            if not pos.is_stale:
                                positions.append(pos)
                        except Exception:
                            continue
                if cursor == 0:
                    break
            return positions
        except Exception as exc:
            logger.warning("GPSCache.get_all_positions error: %s", exc)
            self._use_memory = True
            self._last_redis_failure = time.monotonic()
            return [p for p in self._memory.values() if not p.is_stale]

    @staticmethod
    def _serialize(position: BusPosition) -> dict:
        return position.model_dump(mode="json")

    @staticmethod
    def _deserialize(payload: dict) -> BusPosition:
        return BusPosition.model_validate(payload)
