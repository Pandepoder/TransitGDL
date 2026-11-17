"""Validación del sistema experto contra una persona con experiencia en la operación.

1. `generar` arma 12 escenarios sobre rutas reales, guarda las respuestas del
   sistema en `escenarios.json` y escribe un formulario imprimible (sin esas
   respuestas) y una plantilla CSV para capturar lo que conteste la persona.
2. `evaluar` compara sus respuestas con las del sistema: porcentaje de acuerdo
   por pregunta, kappa de Cohen y desacuerdos con su comentario.

Uso:
    python scripts/validacion_experto.py generar
    python scripts/validacion_experto.py evaluar docs/validacion/respuestas.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import os
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SALIDA = ROOT / "docs" / "validacion"

# (ruta, tipo de día, hora, clima): laboral y fin de semana, hora pico y valle,
# buen clima, lluvia y tormenta, camión, tren y BRT.
PLAN = [
    ("T15", "laboral", 8, "clear"),
    ("T15", "laboral", 14, "rain"),
    ("T15", "domingo", 14, "clear"),
    ("T11B", "laboral", 19, "thunderstorm"),
    ("T11B", "sabado", 11, "clear"),
    ("MT_L3", "laboral", 8, "rain"),
    ("MT_L3", "festivo", 19, "clear"),
    ("MC-L1", "laboral", 19, "clear"),
    ("MC-L1", "domingo", 23, "clear"),
    ("C38-V1", "laboral", 14, "thunderstorm"),
    ("T04A-1", "laboral", 23, "clear"),
    ("T04A-1", "sabado", 8, "rain"),
]

DIAS = {
    "laboral": "Entre semana",
    "sabado": "Sábado",
    "domingo": "Domingo",
    "festivo": "Día festivo",
}
CLIMAS = {
    "clear": "Despejado",
    "cloudy": "Nublado",
    "rain": "Lluvia",
    "thunderstorm": "Tormenta",
    "fog": "Niebla",
}
MODOS = {
    "tren": "Tren",
    "brt": "Macrobús (BRT)",
    "trolebus": "Trolebús",
    "bus": "Camión",
}
NIVELES = ("baja", "normal", "alta", "critica")
FLOTA = ("agregar", "mantener", "retirar")
# Acciones que la persona puede elegir como la principal.
ACCIONES = (
    "ninguna",
    "agregar_unidades",
    "reducir_unidades",
    "reforzar_frecuencia",
    "emitir_alerta_operativa",
    "reducir_velocidad_operativa",
    "sugerir_ruta_alterna",
    "ampliar_ventana_eta",
    "informar_sin_servicio",
)
SINONIMOS = {
    "critico": "critica",
    "reducir": "retirar",
    "quitar": "retirar",
    "menos": "retirar",
    "aumentar": "agregar",
    "mas": "agregar",
    "igual": "mantener",
    "sin cambio": "mantener",
    "ninguno": "ninguna",
    "": "",
}


# ── Funciones puras ──────────────────────────────────────────────────────────


def normalizar(texto: str | None) -> str:
    """Minúsculas, sin acentos ni espacios sobrantes; aplica sinónimos."""
    base = unicodedata.normalize("NFKD", (texto or "").strip().lower())
    base = "".join(c for c in base if not unicodedata.combining(c))
    base = " ".join(base.split())
    return SINONIMOS.get(base, base)


def normalizar_accion(texto: str | None, descripciones: dict[str, str]) -> str:
    """Acepta el id de la acción o su descripción ("Agregar unidades a la ruta")."""
    valor = normalizar(texto).replace(" ", "_")
    if valor in descripciones or valor in ACCIONES:
        return valor
    por_texto = {normalizar(d): a for a, d in descripciones.items()}
    return por_texto.get(normalizar(texto), valor)


def acuerdo(pares: list[tuple[str, str]]) -> float | None:
    """Proporción de pares iguales (None si no hay pares)."""
    return sum(a == b for a, b in pares) / len(pares) if pares else None


def kappa(a: list[str], b: list[str]) -> float | None:
    """Kappa de Cohen entre dos listas de etiquetas del mismo largo."""
    if not a or len(a) != len(b):
        return None
    n = len(a)
    observado = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    esperado = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if esperado >= 1:
        return 1.0 if observado == 1 else 0.0
    return (observado - esperado) / (1 - esperado)


def interpretar_kappa(k: float | None) -> str:
    """Escala de Landis y Koch (1977)."""
    if k is None:
        return "sin datos"
    for limite, texto in (
        (0, "pobre"),
        (0.2, "leve"),
        (0.4, "aceptable"),
        (0.6, "moderado"),
        (0.8, "sustancial"),
    ):
        if k <= limite:
            return texto
    return "casi perfecto"


def leer_respuestas(ruta: str | Path) -> dict[str, dict[str, str]]:
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        return {
            fila["id"].strip(): fila for fila in csv.DictReader(fh) if fila.get("id")
        }


def comparar(
    escenarios: list[dict],
    respuestas: dict[str, dict[str, str]],
    descripciones: dict[str, str],
) -> dict:
    filas = []
    for esc in escenarios:
        r = respuestas.get(esc["id"])
        if not r or not any(
            (r.get(k) or "").strip()
            for k in ("nivel_demanda", "decision_flota", "accion")
        ):
            continue
        persona = {
            "nivel_demanda": normalizar(r.get("nivel_demanda")),
            "decision_flota": normalizar(r.get("decision_flota")),
            "accion": normalizar_accion(r.get("accion"), descripciones),
        }
        filas.append(
            {
                "escenario": esc,
                "persona": persona,
                "comentario": (r.get("comentario") or "").strip(),
            }
        )
    resultado = {"respondidos": len(filas), "total": len(escenarios), "preguntas": {}}
    for campo in ("nivel_demanda", "decision_flota", "accion"):
        pares = [
            (f["escenario"]["sistema"][campo], f["persona"][campo])
            for f in filas
            if f["persona"][campo]
        ]
        resultado["preguntas"][campo] = {
            "acuerdo": acuerdo(pares),
            "kappa": kappa([a for a, _ in pares], [b for _, b in pares]),
            "n": len(pares),
        }
    resultado["confusion_demanda"] = Counter(
        (f["escenario"]["sistema"]["nivel_demanda"], f["persona"]["nivel_demanda"])
        for f in filas
        if f["persona"]["nivel_demanda"]
    )
    resultado["desacuerdos"] = [
        f
        for f in filas
        if any(
            f["persona"][c] and f["persona"][c] != f["escenario"]["sistema"][c]
            for c in f["persona"]
        )
    ]
    return resultado


# ── Generación ───────────────────────────────────────────────────────────────


def _motor():
    """TransitEngine sin simulador, sin Redis y con bases de operación en memoria."""
    os.environ.setdefault("USE_MEMORY_CACHE", "true")
    os.environ["DEMO_SIMULATOR"] = "false"
    from core import TransitEngine
    from core.config import CONFIG
    from core.feedback_store import FeedbackStore
    from core.incident_store import IncidentStore
    from core.prediction_learner import PredictionLearner
    from core.transit import TransitStore

    engine = TransitEngine()
    engine._incident_store = IncidentStore(":memory:")
    engine._feedback_store = FeedbackStore(":memory:")
    engine._learner = PredictionLearner(engine._feedback_store)
    store = TransitStore(
        CONFIG.gtfs_path, CONFIG.transit_db_path, CONFIG.landmarks_path
    ).load()
    return engine.initialize_with_loader(store)


async def generar_escenarios(engine, plan=PLAN) -> list[dict]:
    escenarios = []
    for ruta, dia, hora, clima in plan:
        d = await engine.diagnose_route(
            ruta, overrides={"dia": dia, "hora": hora, "clima": clima}
        )
        if d is None:
            continue
        h = d.hechos_iniciales
        # Como la conoce quien opera: clave con número de siempre y su nombre.
        route = engine._gtfs.find_route(ruta)
        nombre = " · ".join(
            x for x in (route.label, route.branch_name or route.route_long_name) if x
        )
        principal = d.acciones[0].id if d.acciones else "ninguna"
        flota = {"agregar": "agregar", "reducir": "retirar"}.get(
            d.decision_flota or "", "mantener"
        )
        escenarios.append(
            {
                "id": f"E{len(escenarios) + 1:02d}",
                "ruta": ruta,
                "nombre": nombre or d.route_name or ruta,
                "modo": MODOS.get(route.mode, "Camión"),
                "dia": dia,
                "hora": hora,
                "clima": clima,
                "frecuencia_min": h.get("frecuencia_min"),
                "unidades_necesarias": h.get("unidades_necesarias"),
                "horario_reducido": h.get("horario_reducido"),
                "sin_servicio": bool(h.get("sin_servicio_programado")),
                "sistema": {
                    "nivel_demanda": d.nivel_demanda,
                    "decision_flota": flota,
                    "accion": principal,
                    "acciones": [a.id for a in d.acciones],
                },
            }
        )
    return escenarios


def _casillas(nombre: str, opciones: list[tuple[str, str]]) -> str:
    return "".join(
        f'<label class="op"><span class="box"></span>{html.escape(texto)}</label>'
        for _, texto in opciones
    )


def formulario_html(escenarios: list[dict], descripciones: dict[str, str]) -> str:
    """Formulario imprimible: contexto de cada escenario, sin las respuestas del sistema."""
    niveles = list(zip(NIVELES, ("Baja", "Normal", "Alta", "Crítica"), strict=True))
    flota = list(
        zip(FLOTA, ("Agregar unidades", "Mantener", "Retirar unidades"), strict=True)
    )
    acciones = [("ninguna", "Ninguna")] + [
        (a, descripciones.get(a, a)) for a in ACCIONES[1:]
    ]
    bloques = []
    for e in escenarios:
        detalles = [
            f"{e['modo']} · {e['nombre']}",
            f"{DIAS[e['dia']]}, {e['hora']:02d}:00 · {CLIMAS.get(e['clima'], e['clima'])}",
        ]
        if e["sin_servicio"]:
            detalles.append("La ruta no tiene salidas programadas a esa hora")
        else:
            if e["frecuencia_min"]:
                detalles.append(
                    f"Pasa cada {e['frecuencia_min']:g} min según el horario oficial"
                )
            if e["unidades_necesarias"]:
                detalles.append(f"El horario pide {e['unidades_necesarias']} unidades")
        bloques.append(f"""
      <section class="esc">
        <h2>{e['id']} · Ruta {html.escape(e['ruta'])}</h2>
        <p class="ctx">{'<br>'.join(html.escape(x) for x in detalles)}</p>
        <div class="preg"><b>1. ¿Cómo estará la demanda frente a lo que programa el horario?</b>{_casillas('nivel', niveles)}</div>
        <div class="preg"><b>2. ¿Qué haría con la flota?</b>{_casillas('flota', flota)}</div>
        <div class="preg"><b>3. ¿Cuál sería su acción principal?</b>{_casillas('accion', acciones)}</div>
        <div class="preg"><b>Comentario</b><div class="linea"></div><div class="linea"></div></div>
      </section>""")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Validación con experto · TransitGDL</title>
<style>
  body {{ font-family: Calibri, Arial, sans-serif; color: #1b2430; margin: 32px; font-size: 13px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .intro {{ max-width: 760px; line-height: 1.5; }}
  .datos {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px 24px; margin: 16px 0 20px; max-width: 760px; }}
  .datos div {{ border-bottom: 1px solid #8a96a3; padding-bottom: 2px; }}
  .esc {{ border: 1px solid #c3cbd5; border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; page-break-inside: avoid; max-width: 760px; }}
  .esc h2 {{ font-size: 15px; margin: 0 0 4px; }}
  .ctx {{ margin: 0 0 8px; color: #3c4a5a; }}
  .preg {{ margin: 6px 0; }}
  .preg b {{ display: block; margin-bottom: 3px; }}
  .op {{ display: inline-flex; align-items: center; gap: 5px; margin: 2px 16px 2px 0; }}
  .box {{ width: 12px; height: 12px; border: 1.5px solid #1b2430; border-radius: 2px; display: inline-block; }}
  .linea {{ border-bottom: 1px solid #8a96a3; height: 20px; }}
  @media print {{ body {{ margin: 12mm; }} }}
</style></head>
<body>
  <h1>Validación del sistema experto TransitGDL</h1>
  <p class="intro">Gracias por su ayuda. Para cada situación, marque lo que usted esperaría y lo que haría si
  estuviera a cargo de la ruta. No hay respuestas correctas: nos interesa su criterio. Responda con lo que sabe
  de la operación; si una situación no aplica, déjela en blanco. Toma unos 15 minutos.</p>
  <div class="datos"><div>Nombre:</div><div>Puesto:</div><div>Años de experiencia:</div><div>Fecha:</div></div>
  {''.join(bloques)}
</body></html>
"""


def plantilla_csv(escenarios: list[dict]) -> str:
    filas = ["id,nivel_demanda,decision_flota,accion,comentario"]
    filas += [f"{e['id']},,,," for e in escenarios]
    return "\n".join(filas) + "\n"


def reporte_md(resultado: dict, escenarios: list[dict]) -> str:
    nombres = {
        "nivel_demanda": "Nivel de demanda",
        "decision_flota": "Decisión de flota",
        "accion": "Acción principal",
    }
    lineas = [
        "# Validación con experto",
        "",
        f"Respuestas de la persona en {resultado['respondidos']} de {resultado['total']} escenarios "
        f"(calculado el {date.today():%d/%m/%Y}).",
        "",
        "| Pregunta | Respuestas | Acuerdo | Kappa de Cohen |",
        "|---|---|---|---|",
    ]
    for campo, datos in resultado["preguntas"].items():
        ac = "—" if datos["acuerdo"] is None else f"{100 * datos['acuerdo']:.0f} %"
        k = datos["kappa"]
        kt = "—" if k is None else f"{k:.2f} ({interpretar_kappa(k)})"
        lineas.append(f"| {nombres[campo]} | {datos['n']} | {ac} | {kt} |")
    lineas += [
        "",
        "El kappa descuenta el acuerdo que se daría por azar; la escala es la de Landis y Koch (1977).",
        "",
        "## Matriz de confusión del nivel de demanda",
        "",
        "| Sistema \\ Persona | " + " | ".join(NIVELES) + " |",
        "|---|" + "---|" * len(NIVELES),
    ]
    for s in NIVELES:
        lineas.append(
            f"| {s} | "
            + " | ".join(str(resultado["confusion_demanda"][(s, p)]) for p in NIVELES)
            + " |"
        )
    lineas += ["", "## Desacuerdos", ""]
    if not resultado["desacuerdos"]:
        lineas.append("Ninguno.")
    for f in resultado["desacuerdos"]:
        e = f["escenario"]
        partes = [
            f"{nombres[c]}: sistema «{e['sistema'][c]}», persona «{f['persona'][c]}»"
            for c in f["persona"]
            if f["persona"][c] and f["persona"][c] != e["sistema"][c]
        ]
        comentario = f" Comentario: {f['comentario']}" if f["comentario"] else ""
        lineas.append(
            f"- **{e['id']}** ({e['ruta']}, {DIAS[e['dia']].lower()} {e['hora']:02d}:00, {CLIMAS.get(e['clima'], e['clima']).lower()}): "
            + "; ".join(partes)
            + "."
            + comentario
        )
    return "\n".join(lineas) + "\n"


# ── Línea de comandos ────────────────────────────────────────────────────────


def _descripciones() -> dict[str, str]:
    from core.expert import SistemaExperto

    return dict(SistemaExperto().base.acciones)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validación del sistema experto con una persona."
    )
    sub = parser.add_subparsers(dest="comando", required=True)
    gen = sub.add_parser("generar", help="Escenarios, formulario y plantilla")
    gen.add_argument("--salida", default=str(SALIDA))
    ev = sub.add_parser("evaluar", help="Compara las respuestas con el sistema")
    ev.add_argument("respuestas")
    ev.add_argument("--escenarios", default=str(SALIDA / "escenarios.json"))
    ev.add_argument("--salida", default=str(SALIDA / "resultado.md"))
    args = parser.parse_args(argv)

    descripciones = _descripciones()
    if args.comando == "generar":
        escenarios = asyncio.run(generar_escenarios(_motor()))
        salida = Path(args.salida)
        salida.mkdir(parents=True, exist_ok=True)
        (salida / "escenarios.json").write_text(
            json.dumps(escenarios, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (salida / "formulario_experto.html").write_text(
            formulario_html(escenarios, descripciones), encoding="utf-8"
        )
        (salida / "respuestas_plantilla.csv").write_text(
            plantilla_csv(escenarios), encoding="utf-8"
        )
        print(f"{len(escenarios)} escenarios en {salida}")
        return 0

    escenarios = json.loads(Path(args.escenarios).read_text(encoding="utf-8"))
    resultado = comparar(escenarios, leer_respuestas(args.respuestas), descripciones)
    texto = reporte_md(resultado, escenarios)
    Path(args.salida).write_text(texto, encoding="utf-8")
    print(texto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
