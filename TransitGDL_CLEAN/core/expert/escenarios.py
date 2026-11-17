"""Escenarios aleatorios e invariantes del sistema experto.

Los usan el barrido (`scripts/barrido_experto.py`) y las pruebas: generan
hechos iniciales con los mismos rangos que reúne `facts.py` para una ruta real y
revisan que ningún diagnóstico caiga en una contradicción de sentido.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from typing import Any

from core.expert.inference import ResultadoInferencia

CLIMAS = ("clear", "cloudy", "rain", "thunderstorm", "fog")
FRECUENCIAS = (3, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40)
SEVERIDADES = ("low", "medium", "high", "critical")

# Sin servicio programado solo se informa, se avisa de incidencias, se sugiere
# otra ruta y se audita el horario; nada de flota, GPS ni ajustes al ETA.
ACCIONES_SIN_SERVICIO = frozenset(
    {
        "informar_sin_servicio",
        "notificar_usuarios",
        "sugerir_ruta_alterna",
        "ajustar_horario_publicado",
        "revisar_regularidad",
    }
)
ACCIONES_FLOTA = frozenset(
    {"agregar_unidades", "reducir_unidades", "reforzar_frecuencia"}
)


def escenario(rng: random.Random) -> dict[str, Any]:
    """Hechos iniciales de un escenario aleatorio (los ausentes no se incluyen)."""
    fin_de_semana = rng.random() < 2 / 7
    festivo = not fin_de_semana and rng.random() < 0.08
    necesarias = rng.randint(4, 60)
    en_servicio = rng.randint(0, 80) if rng.random() < 1 / 3 else None
    incidentes = rng.choice([0, 0, 0, 1, 2])
    hechos: dict[str, Any] = {
        "clima": rng.choice(CLIMAS),
        "hora": rng.randint(0, 23),
        "es_fin_de_semana": fin_de_semana,
        "es_festivo": festivo,
        "tipo_ruta": rng.choice([0, 1, 3, 3, 3]),
        "vehiculos_en_ruta": rng.randint(0, 8),
        "gps_esperado": rng.random() < 0.5,
        "frecuencia_min": rng.choice(FRECUENCIAS),
        "incidentes_activos": incidentes,
        "hay_ruta_alterna": rng.random() < 0.8,
        "muestras_feedback": rng.randint(0, 8),
        "error_medio_min": round(rng.uniform(-3, 9), 1),
        "desviacion_error_min": round(rng.uniform(0, 14), 1),
        "unidades_necesarias": necesarias,
        "horario_reducido": rng.random() < 1 / 3,
    }
    if incidentes:
        hechos["severidad_max_incidente"] = rng.choice(SEVERIDADES)
    if en_servicio is not None:
        hechos["unidades_en_servicio"] = en_servicio
        hechos["cobertura_flota"] = round(en_servicio / necesarias, 2)
    pico_ruta = rng.choice([None, None, True, False])
    if pico_ruta is not None:
        hechos["es_hora_pico_ruta"] = pico_ruta
    if rng.random() < 0.05:
        hechos["sin_servicio_programado"] = True
    return hechos


def escenarios(n: int, semilla: int) -> Iterator[dict[str, Any]]:
    rng = random.Random(semilla)
    for _ in range(n):
        yield escenario(rng)


def _acciones(resultado: ResultadoInferencia) -> set[str]:
    return {str(h.valor) for h in resultado.memoria.valores("accion")}


def _dia_no_laboral(hechos: dict[str, Any]) -> bool:
    return bool(hechos.get("es_fin_de_semana") or hechos.get("es_festivo"))


def _agregar_y_retirar(hechos, resultado) -> bool:
    return {"agregar_unidades", "reducir_unidades"} <= _acciones(resultado)


def _hora_pico_en_dia_no_laboral(hechos, resultado) -> bool:
    memoria = resultado.memoria
    return _dia_no_laboral(hechos) and (
        memoria.obtener("hora_pico_general", True) is not None
        or memoria.obtener("pico_no_cubierto", True) is not None
    )


def _acciones_sin_servicio(hechos, resultado) -> bool:
    return bool(
        hechos.get("sin_servicio_programado")
        and _acciones(resultado) - ACCIONES_SIN_SERVICIO
    )


def _flota_sin_servicio(hechos, resultado) -> bool:
    return bool(
        hechos.get("sin_servicio_programado") and _acciones(resultado) & ACCIONES_FLOTA
    )


def _pico_no_cubierto_con_demanda_baja(hechos, resultado) -> bool:
    memoria = resultado.memoria
    nivel = memoria.mejor("nivel_demanda")
    return (
        memoria.obtener("pico_no_cubierto", True) is not None
        and nivel is not None
        and nivel.valor == "baja"
    )


Invariante = Callable[[dict[str, Any], ResultadoInferencia], bool]

# Nombre → función que devuelve True si el diagnóstico la viola.
INVARIANTES: dict[str, Invariante] = {
    "Agregar y retirar unidades a la vez": _agregar_y_retirar,
    "Hora pico de la ciudad en fin de semana o festivo": _hora_pico_en_dia_no_laboral,
    "Ruta sin servicio con acciones distintas de informar o avisar": _acciones_sin_servicio,
    "Ruta sin servicio con acciones de flota": _flota_sin_servicio,
    "Pico no cubierto con demanda baja": _pico_no_cubierto_con_demanda_baja,
}


def violaciones(hechos: dict[str, Any], resultado: ResultadoInferencia) -> list[str]:
    """Invariantes que viola un diagnóstico (vacía si ninguna)."""
    return [nombre for nombre, viola in INVARIANTES.items() if viola(hechos, resultado)]
