from __future__ import annotations
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from core.config import CONFIG
from core.models import Incident, IncidentSeverity

logger = logging.getLogger(__name__)

_REDIS_SESSION_PREFIX = "wa:session:"
_REGIONAL_LINE_NUMBERS = frozenset({"55", "231", "646", "615"})


class State(str, Enum):
    IDLE = "idle"
    AWAIT_ROUTE = "await_route"
    AWAIT_STOP = "await_stop"
    AWAIT_ROUTES_STOP = "await_routes_stop"
    AWAIT_INCIDENT = "await_incident"
    AWAIT_FLEET_VEHICLES = "await_fleet_vehicles"
    AWAIT_FEEDBACK = "await_feedback"

@dataclass
class Session:
    state: State = State.IDLE
    route: Optional[str] = None
    stop: Optional[str] = None
    last_active: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pending_prediction_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "route": self.route,
            "stop": self.stop,
            "last_active": self.last_active.isoformat(),
            "pending_prediction_id": self.pending_prediction_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        last_active_raw = data.get("last_active")
        try:
            last_active = datetime.fromisoformat(last_active_raw) if last_active_raw else datetime.now(timezone.utc)
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last_active = datetime.now(timezone.utc)
        return cls(
            state=State(data.get("state", State.IDLE.value)),
            route=data.get("route"),
            stop=data.get("stop"),
            last_active=last_active,
            pending_prediction_id=data.get("pending_prediction_id"),
        )


class ConversationHandler:
    """
    Maneja el flujo conversacional del bot de WhatsApp.
    Las sesiones se persisten en Redis cuando está disponible, con fallback
    transparente a memoria si Redis no responde.
    """

    def __init__(self, engine, session_timeout_minutes: int | None = None):
        self.engine = engine
        timeout_minutes = session_timeout_minutes or CONFIG.session_timeout_minutes
        self.session_timeout = timedelta(minutes=timeout_minutes)
        self._session_ttl_seconds = int(self.session_timeout.total_seconds())
        # Fallback en memoria: se usa cuando Redis no está disponible
        self._memory_sessions: dict[str, Session] = {}
        self._redis = None
        self._use_memory = False

    async def connect_redis(self) -> None:
        """
        Conecta al Redis compartido con GPSCache.
        Llamar desde el lifespan de FastAPI junto con engine.initialize().
        Si falla, el handler opera en modo memoria sin lanzar excepción.
        """
        if CONFIG.use_memory_cache or not (CONFIG.redis_url or "").strip():
            self._redis = None
            self._use_memory = True
            logger.info("ConversationHandler: modo memoria (USE_MEMORY_CACHE o sin REDIS_URL).")
            return

        try:
            import redis.asyncio as aioredis  # type: ignore[import-untyped]
            client = aioredis.from_url(
                CONFIG.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await client.ping()
            self._redis = client
            self._use_memory = False
            logger.info("ConversationHandler: sesiones respaldadas en Redis.")
        except Exception as exc:
            logger.warning(
                "ConversationHandler: Redis no disponible (%s). Sesiones en memoria.", exc
            )
            self._redis = None
            self._use_memory = True

    async def _get_session(self, sender: str) -> Session:
        now = datetime.now(timezone.utc)

        if not self._use_memory and self._redis is not None:
            try:
                key = f"{_REDIS_SESSION_PREFIX}{sender}"
                raw = await self._redis.get(key)
                if raw:
                    session = Session.from_dict(json.loads(raw))
                    # TTL se renueva en _save_session; si expiró Redis ya lo eliminó
                    return session
                return Session()
            except Exception as exc:
                logger.warning("ConversationHandler: fallo lectura Redis (%s). Usando memoria.", exc)
                self._use_memory = True

        # Fallback a memoria
        session = self._memory_sessions.get(sender)
        if session is None or now - session.last_active > self.session_timeout:
            session = Session()
        return session

    async def _save_session(self, sender: str, session: Session) -> None:
        session.last_active = datetime.now(timezone.utc)

        if not self._use_memory and self._redis is not None:
            try:
                key = f"{_REDIS_SESSION_PREFIX}{sender}"
                await self._redis.setex(key, self._session_ttl_seconds, json.dumps(session.to_dict()))
                return
            except Exception as exc:
                logger.warning("ConversationHandler: fallo escritura Redis (%s). Usando memoria.", exc)
                self._use_memory = True

        # Fallback a memoria con limpieza periódica de sesiones expiradas
        self._memory_sessions[sender] = session
        now = datetime.now(timezone.utc)
        if len(self._memory_sessions) > 1000:
            self._memory_sessions = {
                k: v for k, v in self._memory_sessions.items()
                if now - v.last_active <= self.session_timeout
            }

    async def _reset_session(self, sender: str) -> None:
        """Elimina la sesión del sender en Redis o memoria."""
        new_session = Session()
        if not self._use_memory and self._redis is not None:
            try:
                await self._redis.delete(f"{_REDIS_SESSION_PREFIX}{sender}")
            except Exception:
                pass
        self._memory_sessions.pop(sender, None)
        await self._save_session(sender, new_session)

    def active_sessions_count(self) -> int:
        """Cuenta sesiones activas. En modo Redis devuelve el conteo en memoria (aproximado)."""
        now = datetime.now(timezone.utc)
        return sum(
            1 for s in self._memory_sessions.values()
            if now - s.last_active <= self.session_timeout
        )

    async def handle(self, sender: str, message: str) -> str:
        text = (message or "").strip()
        lower = self._normalize_text(text)
        session = await self._get_session(sender)

        if not text:
            await self._save_session(sender, session)
            return "No entendí tu mensaje.\n\n" + self._menu()

        # --- CHEAT CODE PARA EL HACKATHON (solo en desarrollo) ---
        if lower.startswith("dev bus") and CONFIG.environment != "production":
            route = text.split(" ", 2)[-1].strip().upper()
            from core.models import BusPosition
            # Creamos un camión falso cerca de la Glorieta Minerva
            fake_bus = BusPosition(
                vehicle_id=f"DEMO_{route}",
                route_id=route,
                lat=20.6695, 
                lon=-103.3870,
                speed_kmh=45.0
            )
            await self.engine._gps.update(fake_bus)
            return f"🛠️ [DEV MODE] Camión inyectado mágicamente en la ruta {route} a 45 km/h. Pide tu ETA ahora."
        # -----------------------------------------------------------

        if lower in {"0", "reset", "reiniciar", "menu", "menú"}:
            await self._reset_session(sender)
            return "Sesión reiniciada.\n\n" + self._menu()

        if self._looks_like_greeting(lower) and session.state == State.IDLE and not session.route:
            await self._save_session(sender, session)
            return self._welcome()

        # VÁLVULA DE ESCAPE: Solo palabras clave del menú sacan al usuario de feedback.
        # Los números 1-5 y frases de tiempo son ratings válidos y deben procesarse
        # como feedback, no como comandos de escape. _handle_feedback los gestiona.
        if session.state == State.AWAIT_FEEDBACK:
            if self._looks_like_new_intent(lower, text):
                session.state = State.IDLE
                session.pending_prediction_id = None
                await self._save_session(sender, session)
                return await self.handle(sender, message)
            if lower in {"menu", "menú", "clima", "lluvia", "incidencia", "rutas", "paradas"}:
                session.state = State.IDLE
                session.pending_prediction_id = None
                await self._save_session(sender, session)
                return await self.handle(sender, message)
            reply = self._handle_feedback(sender, session, lower)
            await self._save_session(sender, session)
            return reply

        if session.state == State.AWAIT_ROUTE:
            session.route = self._resolve_route_input(text)
            if not session.route:
                await self._save_session(sender, session)
                return (
                    "No entendí la ruta. Escribe el ID o nombre, por ejemplo: "
                    "*MACRO01_R*, *MT_L1* o *ST_L3*."
                )
            session.state = State.AWAIT_STOP
            await self._save_session(sender, session)
            return f"Perfecto. Ahora escribe la parada o referencia para la ruta *{session.route}*."

        if session.state == State.AWAIT_STOP:
            result = await self.engine.get_eta(session.route or text, text)
            session.state = State.IDLE
            session.route = None
            session.stop = None
            if result:
                session.pending_prediction_id = result.prediction_id
                session.state = State.AWAIT_FEEDBACK
                await self._save_session(sender, session)
                return result.to_whatsapp_message() + "\n\n" + self._feedback_prompt()
            await self._save_session(sender, session)
            return "No encontré ETA para esa combinación. Prueba con una parada más exacta."

        if session.state == State.AWAIT_ROUTES_STOP:
            session.state = State.IDLE
            await self._save_session(sender, session)
            return self._routes_for_stop(text)

        if session.state == State.AWAIT_INCIDENT:
            cleaned = text.strip()
            if len(cleaned) < 5:
                await self._save_session(sender, session)
                return "Describe la incidencia con un poco más de detalle."
            incident = self.engine.report_incident(
                Incident(
                    description=cleaned,
                    affected_routes=[],
                    severity=IncidentSeverity.MEDIUM,
                )
            )
            session.state = State.IDLE
            await self._save_session(sender, session)
            return f"Incidencia registrada: {incident.description}"

        if session.state == State.AWAIT_FLEET_VEHICLES:
            try:
                vehicles = int(text)
            except ValueError:
                await self._save_session(sender, session)
                return "Escribe un número de vehículos, por ejemplo: 3"
            recommendation = await self.engine.get_fleet_recommendation(session.route or "", vehicles)
            session.state = State.IDLE
            await self._save_session(sender, session)
            return recommendation.summary()

        # COMANDOS GLOBALES
        if lower in {"1", "eta", "tiempo", "cuanto falta"}:
            session.state = State.AWAIT_ROUTE
            await self._save_session(sender, session)
            return "Escribe la ruta (ID o nombre), por ejemplo: *MACRO01_R* o *MT_L1*"

        if lower in {"2", "clima", "lluvia", "retrasos"}:
            await self._save_session(sender, session)
            return await self.engine.ask("¿Hay retrasos por lluvia?")

        if lower in {"3", "rutas", "paradas"}:
            session.state = State.AWAIT_ROUTES_STOP
            await self._save_session(sender, session)
            return "Escribe una parada y te digo qué rutas pasan por ahí."

        if lower in {"4", "incidencia", "reporte"}:
            session.state = State.AWAIT_INCIDENT
            await self._save_session(sender, session)
            return "Describe la incidencia en una sola frase."

        if lower in {"5", "diagnostico", "experto"}:
            await self._save_session(sender, session)
            return "Escribe *diagnostico* y la ruta, por ejemplo: *diagnostico 646*"

        if lower.startswith(("diagnostico ", "experto ")):
            await self._save_session(sender, session)
            return await self._expert_diagnosis(text.split(" ", 1)[1])

        # Ruta sola: "646", "231", "RG_646" (no nombres de parada)
        route_only = self._resolve_route_input(text)
        if route_only and self._message_is_route_only(text):
            gtfs = getattr(self.engine, "_gtfs", None)
            route_obj = gtfs.find_route(route_only) if gtfs else None
            if route_obj is not None:
                session.route = route_obj.route_id
                session.state = State.AWAIT_STOP
                await self._save_session(sender, session)
                return (
                    f"Ruta *{route_obj.route_short_name}* ({route_obj.route_id}).\n"
                    "¿En qué parada? Ej: *CUCEI*, *Loma Dorada*, *Tonaltecas*."
                )

        # Parada sola en zona regional (sin verbos de consulta)
        if session.state == State.IDLE and self._message_is_stop_only(text):
            stop_reply = await self._handle_stop_only_query(text)
            if stop_reply:
                await self._save_session(sender, session)
                return stop_reply

        # FLUJO NATURAL MEJORADO
        if any(keyword in lower for keyword in ["cuanto", "cuando", "tarda", "llega", "viene", "eta", "ruta", "camion", "hora pasa"]):
            route = self._resolve_route_input(text) or self._extract_route(text)
            stop = self._extract_stop(text)

            if route:
                result = await self.engine.get_eta(route, stop or "")
                if result:
                    session.pending_prediction_id = result.prediction_id
                    session.state = State.AWAIT_FEEDBACK
                    await self._save_session(sender, session)
                    return result.to_whatsapp_message() + "\n\n" + self._feedback_prompt()
            
            reply = await self.engine.ask(text)
            if reply:
                await self._save_session(sender, session)
                return reply

        if any(keyword in lower for keyword in ["que rutas", "rutas pasan", "pasan por"]):
            stop = self._extract_stop(text)
            if stop:
                await self._save_session(sender, session)
                return self._routes_for_stop(stop)
            await self._save_session(sender, session)
            return "Escribe el nombre de la parada."

        if self._looks_like_direct_eta(lower):
            reply = await self.engine.ask(text)
            if reply:
                await self._save_session(sender, session)
                return reply

        await self._save_session(sender, session)
        return "No entendí tu mensaje.\n\n" + self._menu()

    async def _expert_diagnosis(self, route_text: str) -> str:
        route_id = self._resolve_route_input(route_text)
        diagnostico = await self.engine.diagnose_route(route_id) if route_id else None
        if diagnostico is None:
            return "No encontré esa ruta para diagnosticar. Ejemplo: *diagnostico 646*"
        return diagnostico.to_chat_message()

    @staticmethod
    def _looks_like_greeting(text: str) -> bool:
        return any(word in text for word in ["hola", "buenas", "hey", "saludos"])

    @staticmethod
    def _looks_like_new_intent(lower: str, text: str) -> bool:
        """Detecta consulta nueva (no calificación de ETA anterior)."""
        if any(
            phrase in lower
            for phrase in [
                "cuanto falta",
                "cuando pasa",
                "cuando llega",
                "que rutas",
                "rutas pasan",
                "pasan por",
                "hola",
                "menu",
                "omitir",
                "clima",
                "lluvia",
                "incidencia",
                "diagnostico",
                "experto",
            ]
        ):
            return True
        if re.search(r"\b(55|231|646|615)\b", lower):
            return True
        if re.fullmatch(r"[A-Za-z0-9 _.-]{3,40}", text.strip()) and not re.fullmatch(
            r"[1-5]", text.strip()
        ):
            return True
        return False

    @staticmethod
    def _looks_like_direct_eta(text: str) -> bool:
        keywords = ["cuanto falta", "cuando pasa", "cuando llega", "cuanto tarda", "hora pasa", "ya viene", "donde viene", "tarda", "llega"]
        return any(word in text.lower() for word in keywords)

    @staticmethod
    def _normalize_text(text: str) -> str:
        ascii_like = unicodedata.normalize("NFKD", text)
        ascii_like = "".join(ch for ch in ascii_like if not unicodedata.combining(ch))
        return ascii_like.lower()

    def _resolve_route_input(self, text: str) -> Optional[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return None

        gtfs = getattr(self.engine, "_gtfs", None)
        if gtfs is not None and getattr(gtfs, "loaded", False):
            route = gtfs.find_route(cleaned)
            if route is not None:
                return route.route_id

        token = self._extract_route(cleaned)
        if token and gtfs is not None and getattr(gtfs, "loaded", False):
            route = gtfs.find_route(token)
            if route is not None:
                return route.route_id
        return token

    @staticmethod
    def _extract_route(text: str) -> Optional[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return None

        patterns = [
            r"\b([A-Za-z]{2,}[\w-]*\d[\w-]*)\b",
            r"\b([A-Za-z]+-\w+)\b",
            r"\b(\d{1,4}\w*)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.IGNORECASE)
            if match:
                return match.group(1)

        if len(cleaned) <= 32 and re.fullmatch(r"[A-Za-z0-9 _-]+", cleaned):
            return cleaned
        return None

    @staticmethod
    def _extract_stop(text: str) -> Optional[str]:
        # Intento 1: Buscar conectores explícitos
        match = re.search(r"(?:en|por|parada|hacia|a la|el)\s+(.+)$", text, re.IGNORECASE)
        if match:
            return match.group(1).strip(" ?.")
        
        # Intento 2: Limpieza profunda de NLU
        cleaned = re.sub(r"(?:ruta|l[íi]nea|cami[oó]n|bus|la|el|cuanto|cuando|falta|tarda|llega|viene|hora|pasa|eta|en|por)\b", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\d+\w*", "", cleaned) # Quita números de ruta
        cleaned = cleaned.strip(" ?.,-")
        
        return cleaned if len(cleaned) > 2 else None

    @staticmethod
    def _welcome() -> str:
        return (
            "Hola. Soy el asistente de *TransitGDL* (zona CUCEI / Tonalá).\n\n"
            "*Menú rápido*\n"
            "1 · ETA (¿cuánto falta?)\n"
            "2 · Clima y retrasos\n"
            "3 · Rutas en una parada\n"
            "4 · Reportar incidencia\n"
            "5 · Diagnóstico experto de una ruta\n\n"
            "*Ejemplos directos*\n"
            "· *646* → luego escribe la parada\n"
            "· *¿Cuánto falta la 646 en CUCEI?*\n"
            "· *¿Qué rutas pasan por CUCEI?*\n"
            "· *Loma Dorada* (lista rutas)\n\n"
            "Líneas demo: *55*, *231*, *646*, *615*."
        )

    @classmethod
    def _menu(cls) -> str:
        return cls._welcome()

    def _routes_for_stop(self, stop_query: str) -> str:
        if hasattr(self.engine, "describe_routes_at_stop"):
            reply = self.engine.describe_routes_at_stop(stop_query)
            if isinstance(reply, str):
                return reply
        matches = self.engine.search_stops(stop_query)
        if not matches:
            return "No encontré esa parada."
        routes = self.engine.get_routes_at_stop(matches[0].stop_id)
        if not routes:
            return f"No encontré rutas activas para *{matches[0].stop_name}*."
        route_names = ", ".join(route.route_short_name for route in routes)
        return f"En *{matches[0].stop_name}* pasan: {route_names}"

    @staticmethod
    def _message_is_route_only(text: str) -> bool:
        cleaned = text.strip()
        if re.fullmatch(r"\d{1,4}[A-Za-z]?", cleaned):
            return True
        if re.fullmatch(r"RG_\d{1,4}[A-Za-z]?", cleaned, re.IGNORECASE):
            return True
        return cleaned.lower() in _REGIONAL_LINE_NUMBERS

    @staticmethod
    def _message_is_stop_only(text: str) -> bool:
        lower = ConversationHandler._normalize_text(text)
        if any(
            k in lower
            for k in [
                "cuanto",
                "cuando",
                "falta",
                "ruta",
                "pasa",
                "eta",
                "clima",
                "incidencia",
                "?",
            ]
        ):
            return False
        if re.search(r"\b(55|231|646|615)\b", lower):
            return False
        cleaned = text.strip()
        if not (3 <= len(cleaned) <= 48):
            return False
        if len(cleaned) > 14 and " " not in cleaned:
            return False
        if not re.search(r"[aeiou]", lower):
            return False
        return True

    async def _handle_stop_only_query(self, text: str) -> Optional[str]:
        """Responde a paradas escritas solas: CUCEI, Loma Dorada, Tonaltecas."""
        if hasattr(self.engine, "search_stops"):
            if not self.engine.search_stops(text, limit=1):
                return None
        if hasattr(self.engine, "describe_routes_at_stop"):
            reply = self.engine.describe_routes_at_stop(text)
            if "No encontré" not in reply:
                return (
                    reply
                    + "\n\nPara ETA: *¿Cuánto falta la [ruta] en "
                    + text.strip()
                    + "?* (ej. 646, 55, 615)"
                )
        return None

    def _handle_feedback(self, sender: str, session: Session, lower: str) -> str:
        """Procesa la calificación del usuario sobre la predicción más reciente."""
        prediction_id = session.pending_prediction_id
        session.pending_prediction_id = None
        session.state = State.IDLE

        if lower in {"0", "omitir", "skip", "no", "cancelar"}:
            return "Entendido. " + self._menu()

        rating_map = {
            "1": 1, "antes": 1, "temprano": 1,
            "2": 2, "puntual": 2, "exacto": 2, "si": 2, "sí": 2, "ok": 2,
            "3": 3, "poco": 3, "casi": 3,
            "4": 4, "tarde": 4, "retraso": 4,
            "5": 5, "mucho": 5, "muy tarde": 5, "no llego": 5, "no llegó": 5,
        }

        rating: Optional[int] = None
        digit_match = re.search(r"(?:^|\s)([1-5])(?:\s|$)", lower)
        if digit_match:
            rating = int(digit_match.group(1))
        else:
            word_ratings = {
                k: v for k, v in rating_map.items() if len(k) > 1
            }
            for keyword, value in word_ratings.items():
                if re.search(rf"\b{re.escape(keyword)}\b", lower):
                    rating = value
                    break

        if rating is None:
            match = re.search(r"(\d+)\s*min", lower)
            if match and prediction_id:
                actual = float(match.group(1))
                if hasattr(self.engine, "record_feedback"):
                    self.engine.record_feedback(prediction_id, actual_minutes=actual)
                return f"Anotado: llegó en {round(actual)} min. ¡Gracias! \n\n" + self._menu()

            return (
                "No entendí tu calificación. Responde con:\n"
                "1=llegó antes | 2=puntual | 3=poco tarde | 4=tarde | 5=muy tarde\n"
                "o escribe los minutos reales (ej: *12 min*)\n"
                "o *omitir* para saltarte esto."
            )

        if prediction_id and hasattr(self.engine, "record_feedback"):
            self.engine.record_feedback(prediction_id, rating=rating)

        responses = {
            1: "¡Qué bien, llegó antes de lo esperado!",
            2: "Perfecto, llegó puntual.",
            3: "Gracias. Tardó un poco más de lo estimado.",
            4: "Gracias. Llegó bastante tarde, lo tomaremos en cuenta.",
            5: "Lamentamos el retraso. Tu calificación ayuda a mejorar las estimaciones.",
        }
        return responses.get(rating, "Gracias por tu calificación.") + "\n\n" + self._menu()

    @staticmethod
    def _feedback_prompt() -> str:
        return (
            "⭐ Cuando llegue el camión, califica esta predicción:\n"
            "1️⃣ Llegó antes | 2️⃣ Puntual | 3️⃣ Poco tarde\n"
            "4️⃣ Bastante tarde | 5️⃣ Muy tarde | 0️⃣ Omitir"
        )