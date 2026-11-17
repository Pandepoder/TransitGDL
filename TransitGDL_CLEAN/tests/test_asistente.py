"""Pruebas de la fase C: asistente, búsqueda, tarjeta de llegada y escenarios."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from bot.whatsapp_handler import ConversationHandler
from core.calendar_utils import dia_del_tipo, es_festivo
from core.eta_engine import ETAEngine
from core.intents import parse_route_at_stop
from core.models import ETAResult, WeatherCondition
from core.timezone_utils import GDL_TZ
from core.transit.builder import _clean_headsign
from core.weather_client import WeatherState

# ── Intenciones ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("231 en CUCEI", ("231", "CUCEI")),
        ("la 646 hacia Tonalá Centro", ("646", "Tonalá Centro")),
        ("¿ruta 55 por Minerva?", ("55", "Minerva")),
        ("Línea 3 en Juárez", ("Línea 3", "Juárez")),
        ("tren 3 por Cucei", ("tren 3", "Cucei")),
        ("Glorieta Minerva", None),
        ("231 en", None),
    ],
)
def test_route_at_stop_intent(text, expected):
    parsed = parse_route_at_stop(text)
    assert (tuple(parsed) if parsed else None) == expected


# ── Asistente ────────────────────────────────────────────────────────────────


def _chat(engine, *messages: str) -> list[str]:
    handler = ConversationHandler(engine)

    async def run():
        return [await handler.handle("+5215550000000", m) for m in messages]

    return asyncio.run(run())


def test_bot_understands_route_at_stop_like_the_web(engine):
    (reply,) = _chat(engine, "231 en CUCEI")
    assert "T15 (antes 231)" in reply
    assert "Cucei" in reply
    assert "No entendí" not in reply


def test_bot_and_search_understand_line_names(engine, gtfs_loader):
    (reply,) = _chat(engine, "Línea 3 en CUCEI")
    assert "Tren L3" in reply
    assert gtfs_loader.find_routes("Línea 3")[0].route_id == "MT_L3"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¿Por qué no agregar unidades a la 231?", ("agregar_unidades", "231")),
        ("por que no retirar unidades de la T15", ("reducir_unidades", "T15")),
        ("¿Por qué no hay alerta en la 646?", ("emitir_alerta_operativa", "646")),
        ("por qué no sugerir ruta alterna para L3", ("sugerir_ruta_alterna", "L3")),
        ("por que no reforzar frecuencia de la 55", ("reforzar_frecuencia", "55")),
        ("por qué no", None),
        ("¿Cuánto falta la 231 en CUCEI?", None),
    ],
)
def test_bot_parses_why_not_questions(text, expected):
    lower = ConversationHandler._normalize_text(text)
    assert ConversationHandler._parse_why_not(lower, text) == expected


def test_bot_explains_why_not_with_the_expert(engine):
    (reply,) = _chat(engine, "¿Por qué no agregar unidades a la 231?")
    assert "¿Por qué no «agregar unidades a la ruta»?" in reply
    assert "R61 · Agregar unidades" in reply
    assert "No entendí" not in reply


def test_bot_tells_branches_apart_by_destination(engine):
    (reply,) = _chat(engine, "646")
    assert "T04A-1* — Vallarta Patria ⇄ Tonalá Centro" in reply
    assert "T04A-2* — Mercado de Abastos ⇄ Tonalá Centro" in reply


def test_bot_examples_use_existing_routes(engine):
    (menu_eta,) = _chat(engine, "1")
    assert "MACRO01_R" not in menu_eta
    assert "T15" in menu_eta


def test_bot_has_no_fake_bus_command(engine):
    reply, eta = _chat(engine, "dev bus T15", "¿Cuánto falta la T15 en Cucei?")
    assert "DEV MODE" not in reply
    assert "unidad en vivo" not in eta


def test_bot_diagnosis_does_not_invent_a_fleet(engine):
    engine._weather.set_demo_override(WeatherCondition.CLEAR)
    try:
        (reply,) = _chat(engine, "diagnostico 231")
    finally:
        engine._weather.clear_demo_override()
    assert "en servicio" not in reply
    assert "Según el horario hacen falta" in reply


def test_whatsapp_message_includes_data_notes():
    result = ETAResult(
        route_number="T15",
        stop_id="S1",
        stop_name="Cucei",
        estimated_minutes=4,
        data_notes=["La 231C (T15-C06) está en el catálogo oficial…"],
    )
    assert "Datos: La 231C (T15-C06)" in result.to_whatsapp_message()


# ── Búsqueda y nombres ───────────────────────────────────────────────────────


def test_route_search_accepts_filler_words_and_system_names(gtfs_loader):
    assert [r.route_id for r in gtfs_loader.find_routes("ruta 231")] == ["T15"]
    assert [r.route_id for r in gtfs_loader.find_routes("la 646")] == [
        "T04A-1",
        "T04A-2",
    ]
    assert gtfs_loader.find_routes("linea 3")[0].route_id == "MT_L3"
    assert gtfs_loader.find_routes("tren ligero")[0].route_id == "MT_L3"
    assert gtfs_loader.find_routes("macrobus")[0].route_id == "MC-L1"
    assert gtfs_loader.get_route("MT_L3").label == "Tren L3"


def test_uppercase_headsigns_are_cleaned():
    assert _clean_headsign("AUDITORIO") == "Auditorio"
    assert _clean_headsign("PERIFERICO SUR") == "Periferico Sur"
    assert _clean_headsign("La Coronilla") == "La Coronilla"


# ── Tarjeta de llegada ───────────────────────────────────────────────────────


def test_confidence_is_limited_by_data_quality():
    limit = ETAEngine._limit_by_data_quality
    assert limit(0.9, "alta", ["tiempos_estimados"]) == (0.55, "baja")
    assert limit(0.9, "alta", ["solo_terminales"]) == (0.75, "media")
    assert limit(0.55, "baja", ["trazo_aproximado"]) == (0.55, "baja")
    assert limit(0.9, "alta", []) == (0.9, "alta")


def test_schedule_range_grows_with_the_wait(engine):
    """Una salida a varias horas no puede tener el mismo rango que una inminente."""
    madrugada = datetime(2026, 11, 18, 3, 0, tzinfo=GDL_TZ)
    options = engine._eta.estimate_options(
        "T15", "Cucei", [], WeatherState(), [], madrugada
    )
    far = next(o for o in options if o.method == "horario")
    half = (far.window_end_minutes - far.window_start_minutes) / 2
    assert far.estimated_minutes > 60
    assert half >= 0.05 * far.estimated_minutes
    assert far.reliability_label != "alta"


# ── Escenarios del experto ───────────────────────────────────────────────────


def test_day_types_for_scenarios():
    today = date(2026, 11, 18)  # miércoles
    assert dia_del_tipo("laboral", today) == today
    assert dia_del_tipo("sabado", today).weekday() == 5
    assert dia_del_tipo("domingo", today).weekday() == 6
    assert es_festivo(dia_del_tipo("festivo", today))
    with pytest.raises(ValueError):
        dia_del_tipo("feriado", today)


def test_scenario_day_and_weather_change_the_facts(engine):
    domingo = asyncio.run(
        engine.diagnose_route("T15", overrides={"dia": "domingo", "hora": 9})
    )
    festivo = asyncio.run(
        engine.diagnose_route("T15", overrides={"dia": "festivo", "hora": 9})
    )
    despejado = asyncio.run(
        engine.diagnose_route("T15", overrides={"dia": "laboral", "clima": "clear"})
    )
    tormenta = asyncio.run(
        engine.diagnose_route(
            "T15", overrides={"dia": "laboral", "clima": "thunderstorm"}
        )
    )
    assert domingo.hechos_iniciales["es_fin_de_semana"] is True
    assert "dia" not in domingo.hechos_iniciales
    assert festivo.hechos_iniciales["es_festivo"] is True
    # La tormenta alarga el ciclo: hacen falta más unidades para la misma frecuencia.
    assert tormenta.hechos_iniciales["clima"] == "thunderstorm"
    assert (
        tormenta.hechos_iniciales["unidades_necesarias"]
        > despejado.hechos_iniciales["unidades_necesarias"]
    )
