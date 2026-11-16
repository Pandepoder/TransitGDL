"""Pruebas del sistema experto: motor de inferencia, base de conocimiento y API."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.expert import (
    ErrorBaseConocimiento,
    MotorInferencia,
    SistemaExperto,
    cargar_base,
    construir_base,
)
from core.models import Incident, IncidentSeverity, WeatherCondition

HECHOS_NORMALES = {
    "clima": "clear",
    "hora": 11,
    "es_fin_de_semana": False,
    "es_festivo": False,
    "tipo_ruta": 3,
    "vehiculos_en_ruta": 4,
    "unidades_en_servicio": 4,
    "frecuencia_min": 12,
    "incidentes_activos": 0,
    "hay_ruta_alterna": True,
    "muestras_feedback": 0,
}


def _hechos(**cambios):
    return {**HECHOS_NORMALES, **cambios}


def _motor(reglas: list[dict]) -> MotorInferencia:
    base = construir_base(
        {
            "hechos_iniciales": {"a": "", "b": "", "n": ""},
            "acciones": {},
            "reglas": reglas,
        }
    )
    return MotorInferencia(list(base.reglas))


@pytest.fixture(scope="module")
def experto() -> SistemaExperto:
    return SistemaExperto()


# ── Motor de inferencia ──────────────────────────────────────────────────────


def test_encadenamiento_hacia_adelante_en_varios_pasos():
    motor = _motor(
        [
            {
                "id": "A",
                "si": [{"hecho": "a", "valor": True}],
                "entonces": [{"hecho": "x", "valor": True}],
            },
            {
                "id": "B",
                "si": [{"hecho": "x", "valor": True}],
                "entonces": [{"hecho": "y", "valor": True}],
            },
            {
                "id": "C",
                "si": [{"hecho": "y", "valor": True}],
                "entonces": [{"hecho": "z", "valor": "fin"}],
            },
        ]
    )
    resultado = motor.inferir({"a": True})

    assert [d.regla.id for d in resultado.disparos] == ["A", "B", "C"]
    assert resultado.memoria.mejor("z").valor == "fin"
    assert [d.regla.id for d in resultado.cadena("z", "fin")] == ["A", "B", "C"]


def test_regla_no_se_dispara_si_falta_un_hecho():
    motor = _motor(
        [
            {
                "id": "A",
                "si": [{"hecho": "a", "valor": True}, {"hecho": "b", "valor": True}],
                "entonces": [{"hecho": "x", "valor": True}],
            }
        ]
    )
    assert motor.inferir({"a": True}).disparos == []


def test_factores_de_certeza_se_propagan_y_combinan():
    motor = _motor(
        [
            {
                "id": "A",
                "certeza": 0.6,
                "si": [{"hecho": "a", "valor": True}],
                "entonces": [{"hecho": "x", "valor": True}],
            },
            {
                "id": "B",
                "certeza": 0.5,
                "si": [{"hecho": "b", "valor": True}],
                "entonces": [{"hecho": "x", "valor": True}],
            },
            {
                "id": "C",
                "certeza": 0.5,
                "prioridad": 10,
                "si": [{"hecho": "x", "valor": True}],
                "entonces": [{"hecho": "y", "valor": True}],
            },
        ]
    )
    memoria = motor.inferir({"a": True, "b": True}).memoria

    # 0.6 + 0.5 × (1 − 0.6) = 0.8 ; la conclusión hereda 0.8 × 0.5 = 0.4
    assert memoria.obtener("x", True).certeza == pytest.approx(0.8)
    assert memoria.obtener("y", True).certeza == pytest.approx(0.4)


def test_certeza_minima_bloquea_evidencia_debil():
    motor = _motor(
        [
            {
                "id": "A",
                "certeza": 0.5,
                "si": [{"hecho": "a", "valor": True}],
                "entonces": [{"hecho": "x", "valor": True}],
            },
            {
                "id": "B",
                "prioridad": 10,
                "si": [{"hecho": "x", "valor": True, "certeza_min": 0.7}],
                "entonces": [{"hecho": "y", "valor": True}],
            },
        ]
    )
    assert motor.inferir({"a": True}).memoria.mejor("y") is None


def test_resolucion_de_conflictos_por_prioridad_y_especificidad():
    motor = _motor(
        [
            {
                "id": "GENERAL",
                "si": [{"hecho": "a", "valor": True}],
                "entonces": [{"hecho": "x", "valor": 1}],
            },
            {
                "id": "ESPECIFICA",
                "si": [{"hecho": "a", "valor": True}, {"hecho": "b", "valor": True}],
                "entonces": [{"hecho": "x", "valor": 2}],
            },
            {
                "id": "URGENTE",
                "prioridad": 90,
                "si": [{"hecho": "b", "valor": True}],
                "entonces": [{"hecho": "x", "valor": 3}],
            },
        ]
    )
    orden = [d.regla.id for d in motor.inferir({"a": True, "b": True}).disparos]
    assert orden == ["URGENTE", "ESPECIFICA", "GENERAL"]


def test_operadores_numericos_y_de_pertenencia():
    motor = _motor(
        [
            {
                "id": "MAYOR",
                "si": [{"hecho": "n", "op": ">=", "valor": 10}],
                "entonces": [{"hecho": "grande", "valor": True}],
            },
            {
                "id": "EN",
                "si": [{"hecho": "a", "op": "en", "valor": ["rojo", "azul"]}],
                "entonces": [{"hecho": "color_primario", "valor": True}],
            },
            {
                "id": "ALGUNA",
                "si_alguna": [
                    {"hecho": "b", "valor": True},
                    {"hecho": "n", "op": "<", "valor": 0},
                ],
                "entonces": [{"hecho": "alguna", "valor": True}],
            },
        ]
    )
    memoria = motor.inferir({"n": 12, "a": "Azul", "b": True}).memoria
    assert memoria.mejor("grande") is not None
    assert memoria.mejor("color_primario") is not None
    assert memoria.mejor("alguna") is not None

    vacia = motor.inferir({"n": 3, "a": "verde", "b": False}).memoria
    assert vacia.mejor("grande") is None
    assert vacia.mejor("color_primario") is None
    assert vacia.mejor("alguna") is None


# ── Base de conocimiento ─────────────────────────────────────────────────────


def test_base_de_conocimiento_carga_y_es_consistente():
    base = cargar_base()
    assert len(base.reglas) >= 40
    assert len({regla.id for regla in base.reglas}) == len(base.reglas)
    assert all(regla.explicacion for regla in base.reglas)


def test_base_rechaza_hecho_no_declarado():
    with pytest.raises(ErrorBaseConocimiento, match="fantasma"):
        construir_base(
            {
                "hechos_iniciales": {"a": ""},
                "reglas": [
                    {
                        "id": "A",
                        "si": [{"hecho": "fantasma", "valor": True}],
                        "entonces": [{"hecho": "x", "valor": True}],
                    }
                ],
            }
        )


def test_base_exige_decidir_univaluados_antes_de_consultarlos():
    with pytest.raises(ErrorBaseConocimiento, match="prioridad"):
        construir_base(
            {
                "hechos_iniciales": {"a": ""},
                "univaluados": ["nivel"],
                "reglas": [
                    {
                        "id": "A",
                        "si": [{"hecho": "a", "valor": True}],
                        "entonces": [{"hecho": "nivel", "valor": "alto"}],
                    },
                    {
                        "id": "B",
                        "si": [{"hecho": "nivel", "valor": "alto"}],
                        "entonces": [{"hecho": "x", "valor": True}],
                    },
                ],
            }
        )


def test_operadores_conocido_y_desconocido():
    motor = _motor(
        [
            {
                "id": "SIN_DATO",
                "si": [{"hecho": "a", "op": "desconocido"}],
                "entonces": [{"hecho": "x", "valor": "falta"}],
            },
            {
                "id": "CON_DATO",
                "si": [{"hecho": "b", "op": "conocido"}],
                "entonces": [{"hecho": "x", "valor": "hay"}],
            },
        ]
    )
    solo_b = motor.inferir({"b": 3}).memoria
    assert {h.valor for h in solo_b.valores("x")} == {"falta", "hay"}
    solo_a = motor.inferir({"a": False}).memoria
    # Un dato presente, aunque sea falso, no es desconocido.
    assert solo_a.valores("x") == []


def test_base_rechaza_negacion_sobre_hechos_derivados():
    reglas = [
        {
            "id": "A",
            "si": [{"hecho": "a", "valor": True}],
            "entonces": [{"hecho": "derivado", "valor": True}],
        },
        {
            "id": "B",
            "si": [{"hecho": "derivado", "op": "desconocido"}],
            "entonces": [{"hecho": "x", "valor": True}],
        },
    ]
    with pytest.raises(ErrorBaseConocimiento, match="solo puede usarse sobre datos"):
        construir_base({"hechos_iniciales": {"a": ""}, "reglas": reglas})

    con_valor = {
        "id": "C",
        "si": [{"hecho": "a", "op": "desconocido", "valor": True}],
        "entonces": [{"hecho": "x", "valor": True}],
    }
    with pytest.raises(ErrorBaseConocimiento, match="no lleva 'valor'"):
        construir_base({"hechos_iniciales": {"a": ""}, "reglas": [con_valor]})


def test_base_rechaza_accion_no_declarada_y_reglas_duplicadas():
    regla = {
        "id": "A",
        "si": [{"hecho": "a", "valor": True}],
        "entonces": [{"hecho": "accion", "valor": "inventada"}],
    }
    with pytest.raises(ErrorBaseConocimiento, match="inventada"):
        construir_base({"hechos_iniciales": {"a": ""}, "reglas": [regla]})

    valida = {**regla, "entonces": [{"hecho": "x", "valor": True}]}
    with pytest.raises(ErrorBaseConocimiento, match="duplicada"):
        construir_base({"hechos_iniciales": {"a": ""}, "reglas": [valida, valida]})


# ── Escenarios del dominio ───────────────────────────────────────────────────


def test_operacion_normal_no_recomienda_acciones(experto):
    d = experto.diagnosticar(_hechos())
    assert d.nivel_demanda == "normal"
    assert d.estado_servicio == "normal"
    assert d.acciones == []
    assert d.vehiculos_recomendados == 4


def test_hora_pico_en_dia_laboral_pide_mas_unidades(experto):
    # Ruta de frecuencia fija: su horario no sube el servicio en la hora pico.
    d = experto.diagnosticar(_hechos(hora=8))
    assert d.nivel_demanda == "alta"
    assert [a.id for a in d.acciones] == ["agregar_unidades"]
    assert d.decision_flota == "agregar"
    assert d.vehiculos_recomendados == 5
    assert d.acciones[0].reglas == ["R01", "R17B", "R20", "R41", "R47", "R61"]


def test_hora_pico_que_el_horario_ya_cubre_no_duplica_la_flota(experto):
    # La ruta ya opera con su frecuencia de hora pico y tiene la flota completa.
    d = experto.diagnosticar(
        _hechos(
            hora=8,
            frecuencia_min=5,
            es_hora_pico_ruta=True,
            unidades_necesarias=20,
            unidades_en_servicio=20,
            cobertura_flota=1.0,
        )
    )
    assert d.nivel_demanda == "normal"
    assert d.acciones == []
    assert d.vehiculos_recomendados == 20

    # Con lluvia sí hay demanda por encima de lo programado.
    lluvia = experto.diagnosticar(
        _hechos(
            hora=8,
            clima="rain",
            frecuencia_min=5,
            es_hora_pico_ruta=True,
            unidades_necesarias=20,
            unidades_en_servicio=20,
            cobertura_flota=1.0,
        )
    )
    assert "agregar_unidades" in [a.id for a in lluvia.acciones]
    assert lluvia.vehiculos_recomendados == 25


def test_la_hora_pico_de_la_ruta_manda_sobre_el_reloj(experto):
    # 20 h es pico de la ciudad, pero esta ruta ya bajó su frecuencia y le sobran
    # unidades: solo se recomienda retirar, nunca agregar y retirar a la vez.
    d = experto.diagnosticar(
        _hechos(
            hora=20,
            es_hora_pico_ruta=False,
            unidades_necesarias=3,
            unidades_en_servicio=8,
            cobertura_flota=2.67,
        )
    )
    ids = [a.id for a in d.acciones]
    assert ids == ["reducir_unidades"]
    assert d.decision_flota == "reducir"
    assert d.vehiculos_recomendados == 3
    assert d.acciones[0].reglas == ["R19", "R43", "R62"]


def test_ruta_sin_servicio_solo_informa(experto):
    d = experto.diagnosticar(
        _hechos(
            hora=19,
            clima="rain",
            vehiculos_en_ruta=0,
            unidades_en_servicio=0,
            frecuencia_min=None,
            sin_servicio_programado=True,
        )
    )
    ids = [a.id for a in d.acciones]
    assert "informar_sin_servicio" in ids
    for accion in (
        "agregar_unidades",
        "verificar_gps",
        "ampliar_ventana_eta",
        "reducir_velocidad_operativa",
    ):
        assert accion not in ids
    assert d.vehiculos_recomendados == 0


def test_la_cifra_de_flota_coincide_con_la_accion(experto):
    # Lluvia en pico: el motor decide agregar, pero ya circulan 12 de 6 necesarias.
    d = experto.diagnosticar(
        _hechos(
            hora=9,
            clima="rain",
            unidades_necesarias=6,
            unidades_en_servicio=12,
            cobertura_flota=2.0,
        )
    )
    assert d.decision_flota == "mantener"
    assert d.vehiculos_recomendados == 12
    assert "agregar_unidades" not in [a.id for a in d.acciones]
    assert "ya cubre" in d.nota_flota
    assert d.nota_flota in d.to_chat_message()


def test_barrido_sin_acciones_de_flota_contradictorias(experto):
    import random

    rng = random.Random(3)
    for _ in range(1500):
        necesarias = rng.randint(2, 30)
        actuales = round(necesarias * rng.uniform(0.3, 1.8))
        hechos = _hechos(
            hora=rng.randrange(24),
            clima=rng.choice(["clear", "cloudy", "rain", "thunderstorm", "fog"]),
            es_fin_de_semana=rng.random() < 0.3,
            frecuencia_min=rng.choice([4, 6, 10, 15, 25]),
            unidades_necesarias=necesarias,
            unidades_en_servicio=actuales,
            cobertura_flota=round(actuales / necesarias, 2),
        )
        if rng.random() < 0.4:
            hechos["es_hora_pico_ruta"] = rng.random() < 0.3
        d = experto.diagnosticar(hechos)
        ids = {a.id for a in d.acciones}
        assert not {"agregar_unidades", "reducir_unidades"} <= ids, hechos
        if "agregar_unidades" in ids:
            assert d.vehiculos_recomendados > actuales, hechos
        if "reducir_unidades" in ids:
            assert d.vehiculos_recomendados < actuales, hechos


def test_sin_conteo_de_flota_no_se_compara_con_el_gps(experto):
    # Ninguna unidad reporta GPS y no hay conteo del operador: no es "flota 0".
    hechos = _hechos(frecuencia_min=6, unidades_necesarias=20, vehiculos_en_ruta=0)
    del hechos["unidades_en_servicio"]
    d = experto.diagnosticar(hechos)
    assert d.vehiculos_actuales is None
    assert d.vehiculos_con_gps == 0
    assert d.decision_flota is None
    assert d.acciones == []
    assert d.vehiculos_recomendados == 20
    assert "Según el horario hacen falta 20 unidades" in d.to_chat_message()
    assert "en servicio" not in d.to_chat_message()


def test_gps_solo_se_revisa_cuando_la_ruta_deberia_reportar(experto):
    base = _hechos(hora=8, vehiculos_en_ruta=0)
    sin_fuente = experto.diagnosticar({**base, "gps_esperado": False})
    con_fuente = experto.diagnosticar({**base, "gps_esperado": True})
    assert "verificar_gps" not in [a.id for a in sin_fuente.acciones]
    assert "verificar_gps" in [a.id for a in con_fuente.acciones]


def test_el_motivo_explica_la_decision_y_no_su_traduccion(experto):
    d = experto.diagnosticar(_hechos(hora=8))
    agregar = next(a for a in d.acciones if a.id == "agregar_unidades")
    r47 = next(r for r in experto.base.reglas if r.id == "R47")
    assert agregar.motivo == r47.explicacion
    assert d.motivo_flota == r47.explicacion
    assert f"Porque: {r47.explicacion}" in d.to_chat_message()


def test_por_que_no_explica_lo_que_falto(experto):
    d = experto.diagnosticar(_hechos(), por_que_no="agregar_unidades")
    assert d.por_que_no
    linea = d.por_que_no[0]
    assert linea.startswith("R61 · Agregar unidades: no se cumple decision_flota")
    assert "R47 requiere nivel_demanda en [alta, critica]" in linea

    aplicada = experto.diagnosticar(_hechos(hora=8), por_que_no="agregar_unidades")
    assert aplicada.por_que_no == ["R61 · Agregar unidades: sí se aplicó."]
    assert experto.diagnosticar(_hechos(), por_que_no="volar").por_que_no == [
        "«volar» no es una acción de la base de conocimiento."
    ]


def test_hora_pico_en_festivo_no_eleva_la_demanda(experto):
    d = experto.diagnosticar(_hechos(hora=8, es_festivo=True))
    assert d.nivel_demanda == "baja"
    assert "agregar_unidades" not in [a.id for a in d.acciones]


def test_tormenta_en_hora_pico_es_demanda_critica(experto):
    d = experto.diagnosticar(_hechos(hora=19, clima="thunderstorm"))
    ids = [a.id for a in d.acciones]
    assert d.nivel_demanda == "critica"
    assert "emitir_alerta_operativa" in ids
    assert "reducir_velocidad_operativa" in ids
    assert d.vehiculos_recomendados == 6


def test_no_recomienda_acciones_contradictorias(experto):
    # Tormenta (sube la demanda) en fin de semana (la baja): gana un solo nivel.
    d = experto.diagnosticar(
        _hechos(hora=19, clima="thunderstorm", es_fin_de_semana=True, frecuencia_min=6)
    )
    ids = [a.id for a in d.acciones]
    assert d.nivel_demanda == "alta"
    assert "agregar_unidades" in ids
    assert "reducir_unidades" not in ids


def test_lluvia_afecta_menos_al_tren_que_al_camion(experto):
    camion = experto.diagnosticar(_hechos(clima="rain", tipo_ruta=3))
    tren = experto.diagnosticar(_hechos(clima="rain", tipo_ruta=1))
    assert camion.estado_servicio == "degradado"
    assert tren.estado_servicio == "normal"


def test_incidencia_grave_sugiere_ruta_alterna_y_aviso(experto):
    d = experto.diagnosticar(
        _hechos(incidentes_activos=1, severidad_max_incidente="critical")
    )
    ids = [a.id for a in d.acciones]
    assert d.estado_servicio == "critico"
    assert {"notificar_usuarios", "sugerir_ruta_alterna", "ampliar_ventana_eta"} <= set(
        ids
    )

    sin_alterna = experto.diagnosticar(
        _hechos(
            incidentes_activos=1,
            severidad_max_incidente="critical",
            hay_ruta_alterna=False,
        )
    )
    assert "sugerir_ruta_alterna" not in [a.id for a in sin_alterna.acciones]


def test_baja_demanda_con_mucha_frecuencia_retira_unidades(experto):
    d = experto.diagnosticar(
        _hechos(hora=23, frecuencia_min=6, unidades_en_servicio=10)
    )
    assert d.nivel_demanda == "baja"
    assert "reducir_unidades" in [a.id for a in d.acciones]
    assert d.vehiculos_recomendados == 9


def test_aprendizaje_alimenta_al_experto(experto):
    d = experto.diagnosticar(
        _hechos(muestras_feedback=6, error_medio_min=7.5, desviacion_error_min=12.0)
    )
    ids = [a.id for a in d.acciones]
    assert "ajustar_horario_publicado" in ids
    assert "revisar_regularidad" in ids


def test_peor_escenario_activa_plan_de_contingencia(experto):
    d = experto.diagnosticar(
        _hechos(
            hora=8,
            clima="thunderstorm",
            frecuencia_min=25,
            incidentes_activos=2,
            severidad_max_incidente="high",
        )
    )
    ids = [a.id for a in d.acciones]
    assert "activar_plan_contingencia" in ids
    assert "reforzar_frecuencia" in ids
    plan = next(a for a in d.acciones if a.id == "activar_plan_contingencia")
    # La explicación recorre la cadena completa, desde los datos hasta la acción.
    assert {"R01", "R06", "R13", "R40", "R45", "R70"} <= set(plan.reglas)
    assert all(texto for texto in plan.porque)


def test_explicacion_incluye_hechos_y_reglas(experto):
    d = experto.diagnosticar(_hechos(hora=8))
    assert d.reglas_disparadas[0]["ciclo"] == 1
    assert all(r["texto"].startswith("SI ") for r in d.reglas_disparadas)
    derivados = {(h["hecho"], h["valor"]) for h in d.hechos_derivados}
    assert ("hora_pico", True) in derivados
    assert "Demanda: *alta*" in d.to_chat_message()


# ── Integración con TransitEngine ────────────────────────────────────────────


def test_diagnose_route_sin_gps_ni_conteo_no_pide_unidades(engine):
    """Sin fuente GPS ni conteo del operador, no hay "flota insuficiente"."""
    engine._weather.set_demo_override(WeatherCondition.CLEAR)
    try:
        for ruta in engine._gtfs.get_routes():
            d = asyncio.run(engine.diagnose_route(ruta.route_id))
            derivados = {h["hecho"] for h in d.hechos_derivados}
            assert "cobertura_flota" not in d.hechos_iniciales
            assert d.hechos_iniciales["gps_esperado"] is False
            assert "flota_insuficiente" not in derivados
            assert "sin_cobertura_gps" not in derivados
            assert d.vehiculos_actuales is None
    finally:
        engine._weather.clear_demo_override()


def test_diagnose_route_con_motor_real(engine):
    engine._weather.set_demo_override(WeatherCondition.CLEAR)
    try:
        ruta = engine._gtfs.get_routes()[0]
        d = asyncio.run(
            engine.diagnose_route(ruta.route_id, vehicles=3, overrides={"hora": 11})
        )
        assert d.route_id == ruta.route_id
        assert d.hechos_iniciales["clima"] == "clear"
        # `vehicles` es la flota que informa el operador; el GPS se cuenta aparte.
        assert d.hechos_iniciales["unidades_en_servicio"] == 3
        assert d.vehiculos_actuales == 3
        assert "vehiculos_en_ruta" in d.hechos_iniciales
        assert "tipo_ruta" in d.hechos_iniciales

        engine.report_incident(
            Incident(
                description="Choque cierra el carril",
                affected_routes=[ruta.route_id],
                severity=IncidentSeverity.CRITICAL,
            )
        )
        grave = asyncio.run(
            engine.diagnose_route(ruta.route_id, vehicles=3, overrides={"hora": 11})
        )
        assert grave.hechos_iniciales["severidad_max_incidente"] == "critical"
        assert grave.estado_servicio == "critico"
        assert "notificar_usuarios" in [a.id for a in grave.acciones]
    finally:
        engine._weather.clear_demo_override()
        for incidente in engine.get_active_incidents():
            engine.resolve_incident(incidente.incident_id)


def test_diagnose_route_ruta_inexistente(engine):
    assert asyncio.run(engine.diagnose_route("NO_EXISTE_999")) is None


# ── API ──────────────────────────────────────────────────────────────────────


def _cliente(monkeypatch, experto, diagnostico=None):
    engine = MagicMock()
    engine._initialized = True
    engine.initialize = AsyncMock()
    engine.close = AsyncMock()
    engine.cache_backend = "memory"
    engine.expert = experto
    engine.diagnose_route = AsyncMock(return_value=diagnostico)
    monkeypatch.setattr("api.main.get_engine", lambda: engine)
    monkeypatch.setattr("api.routes.expert.get_engine", lambda: engine)
    return engine


def test_api_diagnose_devuelve_diagnostico_y_escenario(monkeypatch, experto):
    diagnostico = experto.diagnosticar(_hechos(hora=8), route_id="RG_646")
    engine = _cliente(monkeypatch, experto, diagnostico)

    with TestClient(app) as client:
        response = client.get(
            "/expert/diagnose",
            params={"route_id": "RG_646", "clima": "rain", "hora": 8},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["nivel_demanda"] == "alta"
    assert payload["escenario"] == {"clima": "rain", "hora": 8}
    engine.diagnose_route.assert_awaited_once_with(
        "RG_646", vehicles=None, overrides={"clima": "rain", "hora": 8}, por_que_no=None
    )


def test_api_diagnose_ruta_inexistente_y_parametros_invalidos(monkeypatch, experto):
    _cliente(monkeypatch, experto, None)

    with TestClient(app) as client:
        assert client.get("/expert/diagnose?route_id=NOPE").status_code == 404
        assert client.get("/expert/diagnose?route_id=X&hora=30").status_code == 422
        assert client.get("/expert/diagnose?route_id=X&clima=lava").status_code == 422
        assert (
            client.get("/expert/diagnose?route_id=X&por_que_no=volar").status_code
            == 400
        )


def test_api_infer_y_rules(monkeypatch, experto):
    _cliente(monkeypatch, experto)

    with TestClient(app) as client:
        ok = client.post(
            "/expert/infer", json={"hechos": _hechos(hora=19, clima="thunderstorm")}
        )
        malo = client.post("/expert/infer", json={"hechos": {"inventado": 1}})
        reglas = client.get("/expert/rules")

    assert ok.status_code == 200
    assert ok.json()["nivel_demanda"] == "critica"
    assert malo.status_code == 400
    assert "inventado" in malo.json()["detail"]
    assert reglas.json()["total_reglas"] == len(experto.base.reglas)


# ── Bot ──────────────────────────────────────────────────────────────────────


def test_bot_responde_diagnostico(experto):
    from bot.whatsapp_handler import ConversationHandler

    diagnostico = experto.diagnosticar(
        _hechos(hora=8), route_id="RG_646", route_name="646"
    )
    engine = MagicMock()
    engine._gtfs.loaded = True
    engine._gtfs.find_route.return_value = MagicMock(route_id="RG_646")
    engine.diagnose_route = AsyncMock(return_value=diagnostico)
    handler = ConversationHandler(engine)

    respuesta = asyncio.run(handler.handle("tester", "diagnostico 646"))
    ayuda = asyncio.run(handler.handle("tester", "5"))

    assert "Diagnóstico experto · ruta 646" in respuesta
    assert "Agregar unidades" in respuesta
    engine.diagnose_route.assert_awaited_once_with("RG_646")
    assert "diagnostico 646" in ayuda
