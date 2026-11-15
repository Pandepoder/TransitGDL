from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from core.i18n import get_preferred_locale
from core.i18n import translate as t


class SlidingWindowRateLimiter:
    """
    Limitador de tasa por clave usando ventana deslizante en memoria.

    Thread-safety: el GIL de CPython protege las operaciones sobre deque
    y dict para la granularidad que necesitamos aquí (lecturas + appends
    individuales). No se requiere Lock adicional para este uso.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._check_count = 0

    def check(self, key: str, locale: str) -> None:
        """
        Verifica que `key` no haya superado el límite.
        Lanza HTTPException 429 si lo supera, si no registra la solicitud.
        """
        now = time.monotonic()
        window = self._windows[key]
        cutoff = now - self.window_seconds

        # Purgar timestamps fuera de la ventana
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self.max_requests:
            raise HTTPException(
                status_code=429,
                detail=t("errors.too_many_requests", locale=locale),
                headers={"Retry-After": str(self.window_seconds)},
            )

        window.append(now)

        # Cada 500 checks, eliminar IPs con ventana vacía para liberar memoria
        self._check_count += 1
        if self._check_count % 500 == 0:
            empty = [ip for ip, w in self._windows.items() if not w]
            for ip in empty:
                del self._windows[ip]


# Instancia compartida: 10 envíos de feedback por IP por minuto.
# Ajustar en producción según el volumen esperado.
_feedback_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)


def check_feedback_rate_limit(request: Request) -> None:
    """
    Dependencia FastAPI para el endpoint POST /feedback.
    Lee X-Forwarded-For cuando existe (detrás de proxy/load balancer)
    para identificar la IP real del cliente, no la del proxy.
    Solo usa el primer hop del header para evitar IP spoofing.
    """
    locale = get_preferred_locale(
        lang=request.query_params.get("lang"),
        accept_language=request.headers.get("accept-language"),
    )
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"
    _feedback_limiter.check(ip, locale)
