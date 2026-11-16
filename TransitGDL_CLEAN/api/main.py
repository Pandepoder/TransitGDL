from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.deps import get_bot_handler, get_engine
from api.routes.chat import router as chat_router
from api.routes.demo import router as demo_router
from api.routes.eta import router as eta_router
from api.routes.expert import router as expert_router
from api.routes.feedback import router as feedback_router
from api.routes.fleet import router as fleet_router
from api.routes.incidents import router as incidents_router
from api.routes.map import router as map_router
from api.routes.routes import router as routes_router
from api.routes.search import router as search_router
from api.routes.whatsapp import router as whatsapp_router
from core import TransitEngine
from core.config import CONFIG
from core.i18n import get_preferred_locale
from core.i18n import translate as t

logging.basicConfig(level=getattr(logging, CONFIG.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    engine = get_engine()
    if not engine._initialized:
        await engine.initialize()
    elif isinstance(engine, TransitEngine):
        await engine.ensure_simulator()
    # Conectar Redis para sesiones del bot (fallback a memoria si Redis no está)
    bot = get_bot_handler()
    await bot.connect_redis()
    try:
        yield
    finally:
        await engine.close()


app = FastAPI(title="TransitGDL API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
async def health():
    engine = get_engine()
    return {
        "status": "ok" if engine._initialized else "starting",
        "initialized": engine._initialized,
        "cache_backend": engine.cache_backend,
        "service_mode": "schedule_first",
        "active_incidents": getattr(engine, "active_incidents_count", 0),
        "simulator": engine.simulator_active,
        "live_vehicles": engine.live_vehicle_count,
        "environment": CONFIG.environment,
        "version": app.version,
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", uuid4().hex)
    locale = get_preferred_locale(
        lang=request.query_params.get("lang"),
        accept_language=request.headers.get("accept-language"),
    )
    detail = exc.detail
    if exc.status_code == 404 and str(detail).lower() in ("not found", "not found."):
        detail = t("errors.not_found", locale=locale)
    elif exc.status_code == 405 and str(detail).lower() in (
        "method not allowed",
        "method not allowed.",
    ):
        detail = t("errors.method_not_allowed", locale=locale)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


def _serialize_validation_errors(errors: list[dict]) -> list[dict]:
    serialized_errors: list[dict] = []
    for error in errors:
        serialized_error: dict = {}
        for key, value in error.items():
            if key == "ctx" and isinstance(value, dict):
                serialized_error[key] = {
                    ctx_key: (
                        str(ctx_value)
                        if not isinstance(
                            ctx_value,
                            (str, int, float, bool, type(None), dict, list),
                        )
                        else ctx_value
                    )
                    for ctx_key, ctx_value in value.items()
                }
            else:
                serialized_error[key] = value
        serialized_errors.append(serialized_error)
    return serialized_errors


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", uuid4().hex)
    locale = get_preferred_locale(
        lang=request.query_params.get("lang"),
        accept_language=request.headers.get("accept-language"),
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": t("errors.invalid_request", locale=locale),
            "errors": _serialize_validation_errors(exc.errors()),
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", uuid4().hex)
    locale = get_preferred_locale(
        lang=request.query_params.get("lang"),
        accept_language=request.headers.get("accept-language"),
    )
    logger.exception("Unhandled error request_id=%s", request_id)
    return JSONResponse(
        status_code=500,
        content={
            "detail": t("errors.internal_server_error", locale=locale),
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


app.include_router(demo_router)
app.include_router(eta_router)
app.include_router(feedback_router)
app.include_router(fleet_router)
app.include_router(expert_router)
app.include_router(incidents_router)
app.include_router(routes_router)
app.include_router(map_router)
app.include_router(chat_router)
app.include_router(whatsapp_router)
app.include_router(search_router)

app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)

_STATIC_DIR = Path(__file__).parent.parent / "static"
_DASHBOARD_HTML = _STATIC_DIR / "index.html"
# Los recursos de la interfaz (estilos y módulos JS) se sirven desde /static.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
# Respuestas grandes (trazos del mapa) viajan comprimidas.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    """Sirve el dashboard web. Lee el archivo en cada request para facilitar desarrollo."""
    if not _DASHBOARD_HTML.exists():
        locale = get_preferred_locale(
            lang=request.query_params.get("lang"),
            accept_language=request.headers.get("accept-language"),
        )
        raise HTTPException(
            status_code=404,
            detail=t("errors.dashboard_not_found", locale=locale),
        )
    return HTMLResponse(content=_DASHBOARD_HTML.read_text(encoding="utf-8"))
