"""Barrido del sistema experto: escenarios aleatorios, cobertura de reglas e invariantes.

Corre N escenarios con semilla (los rangos de `core/expert/escenarios.py`) y
cuenta cuántas veces se usa cada regla, cuántos diagnósticos violan alguna
invariante y cómo se reparten los niveles de demanda y las decisiones de flota.

Uso:
    python scripts/barrido_experto.py --n 20000 --salida docs/barrido_experto.md
    python scripts/barrido_experto.py --comparar-con reglas_anteriores.yaml
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.expert.escenarios import INVARIANTES, escenarios, violaciones  # noqa: E402
from core.expert.system import NIVEL_POR_OMISION, SistemaExperto  # noqa: E402

NIVELES = ("baja", "normal", "alta", "critica")


def barrer(sistema: SistemaExperto, n: int, semilla: int) -> dict:
    usos: Counter = Counter()
    faltas: Counter = Counter()
    niveles: Counter = Counter()
    decisiones: Counter = Counter()
    acciones: Counter = Counter()
    ciclos = 0
    inicio = time.perf_counter()
    for hechos in escenarios(n, semilla):
        resultado = sistema.inferir(hechos)
        ciclos += resultado.ciclos
        usos.update(d.regla.id for d in resultado.disparos)
        faltas.update(violaciones(hechos, resultado))
        nivel = resultado.memoria.mejor("nivel_demanda")
        niveles[nivel.valor if nivel else NIVEL_POR_OMISION] += 1
        decision = resultado.memoria.mejor("decision_flota")
        decisiones[decision.valor if decision else "sin cambio"] += 1
        acciones.update(str(h.valor) for h in resultado.memoria.valores("accion"))
    segundos = time.perf_counter() - inicio
    return {
        "n": n,
        "reglas": [r.id for r in sistema.base.reglas],
        "version": sistema.base.version,
        "usos": usos,
        "faltas": faltas,
        "niveles": niveles,
        "decisiones": decisiones,
        "acciones": acciones,
        "ciclos_medios": ciclos / n,
        "ms_por_escenario": 1000 * segundos / n,
    }


def _pct(valor: int, total: int) -> str:
    return f"{100 * valor / total:.1f} %"


def reporte(actual: dict, semilla: int, anterior: dict | None = None) -> str:
    n = actual["n"]
    sin_uso = [r for r in actual["reglas"] if not actual["usos"][r]]
    lineas = [
        "# Barrido del sistema experto",
        "",
        f"{n:,} escenarios aleatorios (semilla {semilla}) sobre la base de conocimiento "
        f"versión {actual['version']}, generados el {date.today():%d/%m/%Y}.",
        "",
        "```bash",
        f"python scripts/barrido_experto.py --n {n} --semilla {semilla}",
        "```",
        "",
        "## Invariantes",
        "",
        "Cada fila cuenta los diagnósticos que caen en una contradicción de sentido.",
        "",
    ]
    if anterior:
        lineas += [
            f"| Invariante | Base {anterior['version']} (antes) | Base {actual['version']} |",
            "|---|---|---|",
        ]
        for nombre in INVARIANTES:
            lineas.append(
                f"| {nombre} | {anterior['faltas'][nombre]:,} | {actual['faltas'][nombre]:,} |"
            )
    else:
        lineas += ["| Invariante | Escenarios |", "|---|---|"]
        for nombre in INVARIANTES:
            lineas.append(f"| {nombre} | {actual['faltas'][nombre]:,} |")
    usadas = len(actual["reglas"]) - len(sin_uso)
    menos = sorted(actual["reglas"], key=lambda r: actual["usos"][r])[:5]
    lineas += [
        "",
        "## Cobertura de la base",
        "",
        f"Se usan {usadas} de {len(actual['reglas'])} reglas"
        + (f"; sin usar: {', '.join(sin_uso)}." if sin_uso else "."),
        "Las menos usadas: "
        + ", ".join(f"{r} ({actual['usos'][r]:,})" for r in menos)
        + ".",
        "",
        f"En promedio cada diagnóstico tarda {actual['ms_por_escenario']:.2f} ms y "
        f"dispara {actual['ciclos_medios']:.1f} reglas.",
        "",
        "## Diagnósticos",
        "",
        "| Nivel de demanda | Escenarios |",
        "|---|---|",
    ]
    for nivel in NIVELES:
        lineas.append(f"| {nivel} | {_pct(actual['niveles'][nivel], n)} |")
    lineas += ["", "| Decisión de flota | Escenarios |", "|---|---|"]
    for decision, cuenta in actual["decisiones"].most_common():
        lineas.append(f"| {decision} | {_pct(cuenta, n)} |")
    lineas += ["", "| Acción recomendada | Escenarios |", "|---|---|"]
    for accion, cuenta in actual["acciones"].most_common():
        lineas.append(f"| {accion} | {_pct(cuenta, n)} |")
    return "\n".join(lineas) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Barrido del sistema experto.")
    parser.add_argument("--n", type=int, default=20000, help="Número de escenarios")
    parser.add_argument("--semilla", type=int, default=11)
    parser.add_argument("--reglas", default=None, help="Base de conocimiento a barrer")
    parser.add_argument(
        "--comparar-con", default=None, help="Otra base (por ejemplo, la anterior)"
    )
    parser.add_argument("--salida", default=None, help="Archivo Markdown del reporte")
    args = parser.parse_args(argv)

    actual = barrer(SistemaExperto(args.reglas), args.n, args.semilla)
    anterior = (
        barrer(SistemaExperto(args.comparar_con), args.n, args.semilla)
        if args.comparar_con
        else None
    )
    texto = reporte(actual, args.semilla, anterior)
    if args.salida:
        Path(args.salida).write_text(texto, encoding="utf-8")
    print(texto)
    return 1 if sum(actual["faltas"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
