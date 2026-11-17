"""Pruebas del kit de validación con experto (`scripts/validacion_experto.py`)."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

RUTA = Path(__file__).resolve().parents[1] / "scripts" / "validacion_experto.py"
_spec = importlib.util.spec_from_file_location("validacion_experto", RUTA)
validacion = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validacion)

DESCRIPCIONES = {
    "agregar_unidades": "Agregar unidades a la ruta",
    "reducir_unidades": "Retirar unidades y reasignarlas a rutas con más demanda",
}


def test_kappa_valores_conocidos():
    assert validacion.kappa(["a", "b", "a"], ["a", "b", "a"]) == pytest.approx(1.0)
    # Acuerdo igual al que daría el azar: kappa 0.
    assert validacion.kappa(
        ["s", "s", "n", "n"], ["s", "n", "s", "n"]
    ) == pytest.approx(0.0)
    assert validacion.kappa(["x", "x"], ["x", "x"]) == 1.0
    assert validacion.kappa([], []) is None
    assert validacion.interpretar_kappa(0.7) == "sustancial"
    assert validacion.interpretar_kappa(-0.1) == "pobre"


def test_acuerdo_y_normalizacion():
    assert validacion.acuerdo([("alta", "alta"), ("baja", "normal")]) == 0.5
    assert validacion.acuerdo([]) is None
    assert validacion.normalizar(" Crítica ") == "critica"
    assert validacion.normalizar("Reducir") == "retirar"
    assert validacion.normalizar("sin cambio") == "mantener"
    assert (
        validacion.normalizar_accion("Agregar unidades a la ruta", DESCRIPCIONES)
        == "agregar_unidades"
    )
    assert (
        validacion.normalizar_accion("reducir_unidades", DESCRIPCIONES)
        == "reducir_unidades"
    )


def _escenario(id_, nivel, flota, accion):
    return {
        "id": id_,
        "ruta": "T15",
        "dia": "laboral",
        "hora": 8,
        "clima": "clear",
        "sistema": {"nivel_demanda": nivel, "decision_flota": flota, "accion": accion},
    }


def test_evaluar_desde_csv(tmp_path):
    escenarios = [
        _escenario("E01", "alta", "agregar", "agregar_unidades"),
        _escenario("E02", "baja", "retirar", "reducir_unidades"),
        _escenario("E03", "normal", "mantener", "ninguna"),
    ]
    csv_path = tmp_path / "respuestas.csv"
    csv_path.write_text(
        "id,nivel_demanda,decision_flota,accion,comentario\n"
        "E01,Alta,Agregar,Agregar unidades a la ruta,\n"
        "E02,Normal,mantener,ninguna,El domingo sí hay paseantes\n"
        "E03,,,,\n",
        encoding="utf-8",
    )
    resultado = validacion.comparar(
        escenarios, validacion.leer_respuestas(csv_path), DESCRIPCIONES
    )
    assert resultado["respondidos"] == 2
    assert resultado["preguntas"]["nivel_demanda"]["acuerdo"] == 0.5
    assert resultado["preguntas"]["decision_flota"]["n"] == 2
    texto = validacion.reporte_md(resultado, escenarios)
    assert "| Nivel de demanda | 2 | 50 % |" in texto
    assert "El domingo sí hay paseantes" in texto


def test_generar_no_revela_las_respuestas(engine):
    rutas = [r.route_id for r in engine._gtfs.get_routes()][:2]
    plan = [(rutas[0], "laboral", 8, "rain"), (rutas[-1], "domingo", 14, "clear")]
    escenarios = asyncio.run(validacion.generar_escenarios(engine, plan))
    assert [e["id"] for e in escenarios] == ["E01", "E02"]
    assert all(e["sistema"]["nivel_demanda"] in validacion.NIVELES for e in escenarios)
    # Determinista: la misma entrada da las mismas respuestas.
    assert asyncio.run(validacion.generar_escenarios(engine, plan)) == escenarios
    pagina = validacion.formulario_html(escenarios, DESCRIPCIONES)
    assert "E02 · Ruta" in pagina
    assert "sistema" not in pagina.lower().replace("sistema experto", "")
    assert validacion.plantilla_csv(escenarios).splitlines()[1] == "E01,,,,"
