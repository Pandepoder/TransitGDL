"""Verificación del sistema experto sobre las rutas reales del AMG.

Complementa al barrido (`scripts/barrido_experto.py`), que usa escenarios
inventados: aquí los hechos salen del GTFS real, igual que en la aplicación.

1. Red real: todas las rutas × 4 tipos de día × 6 horas × 3 climas. Revisa que
   no haya errores ni contradicciones, que cada acción traiga su cadena de
   reglas y que el mismo escenario dé siempre el mismo diagnóstico.
2. Pruebas metamórficas: relaciones que deben cumplirse aunque no se conozca la
   respuesta exacta (un clima peor nunca baja la demanda, una incidencia grave
   nunca mejora el servicio, etc.).
3. Casos de aceptación: 12 escenarios con el resultado esperado escrito antes.

Uso:
    python scripts/verificacion_experto.py --salida docs/verificacion_experto.md
    python scripts/verificacion_experto.py --reglas otra_base.yaml

Termina con código 1 si algo falla.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from validacion_experto import _motor  # noqa: E402

from core.expert.escenarios import ACCIONES_FLOTA, ACCIONES_SIN_SERVICIO  # noqa: E402
from core.expert.system import SistemaExperto  # noqa: E402

DIAS = ("laboral", "sabado", "domingo", "festivo")
HORAS = (3, 8, 11, 14, 19, 23)
CLIMAS = ("clear", "rain", "thunderstorm")
DEMANDA = {"baja": 0, "normal": 1, "alta": 2, "critica": 3}
SERVICIO = {"normal": 0, "degradado": 1, "critico": 2}
CLIMA_TEXTO = {"clear": "despejado", "rain": "lluvia", "thunderstorm": "tormenta"}

MR_CLIMA_DEMANDA = "Un clima peor nunca baja la demanda"
MR_CLIMA_SERVICIO = "Un clima peor nunca mejora el servicio"
MR_LLUVIA_TREN = "La lluvia no degrada trenes ni BRT de vía exclusiva"
MR_MAL_CLIMA_FLOTA = "Con lluvia o tormenta ninguna ruta retira unidades"
MR_CALENDARIO = (
    "Con el mismo horario, domingo o festivo nunca tiene más demanda que un día laboral"
)
MR_INCIDENCIA_SERVICIO = "Una incidencia grave nunca mejora el servicio"
MR_INCIDENCIA_AVISO = "Una incidencia grave siempre se avisa a los usuarios"


def _acciones(d) -> set[str]:
    return {a.id for a in d.acciones}


def _sin_servicio(d) -> bool:
    return bool(d.hechos_iniciales.get("sin_servicio_programado"))


def problemas(d) -> list[str]:
    """Contradicciones y faltas de trazabilidad de un diagnóstico real."""
    h = d.hechos_iniciales
    ids = _acciones(d)
    disparadas = {r["regla"] for r in d.reglas_disparadas}
    derivados = {(x["hecho"], x["valor"]) for x in d.hechos_derivados}
    p = []
    if {"agregar_unidades", "reducir_unidades"} <= ids:
        p.append("Agregar y retirar unidades a la vez")
    no_laboral = h.get("es_fin_de_semana") or h.get("es_festivo")
    if no_laboral and disparadas & {"R01", "R02", "R03"}:
        p.append("Hora pico de la ciudad en fin de semana o festivo")
    if _sin_servicio(d) and not ids <= ACCIONES_SIN_SERVICIO:
        p.append("Ruta sin servicio con acciones de operación")
    if _sin_servicio(d) and ids & ACCIONES_FLOTA:
        p.append("Ruta sin servicio con acciones de flota")
    if ("pico_no_cubierto", True) in derivados and d.nivel_demanda == "baja":
        p.append("«Pico no cubierto» con demanda baja")
    if any(not a.reglas or not 0 < a.certeza <= 1 for a in d.acciones):
        p.append("Acción sin cadena de reglas o con certeza fuera de rango")
    n, rec = d.unidades_necesarias, d.vehiculos_recomendados
    if n and rec is not None and d.vehiculos_actuales is None:
        if d.decision_flota == "agregar" and rec < n:
            p.append("Agregar con menos unidades de las que pide el horario")
        if d.decision_flota == "reducir" and rec > n:
            p.append("Retirar con más unidades de las que pide el horario")
    return p


PROBLEMAS = (
    "Agregar y retirar unidades a la vez",
    "Hora pico de la ciudad en fin de semana o festivo",
    "Ruta sin servicio con acciones de operación",
    "Ruta sin servicio con acciones de flota",
    "«Pico no cubierto» con demanda baja",
    "Acción sin cadena de reglas o con certeza fuera de rango",
    "Agregar con menos unidades de las que pide el horario",
    "Retirar con más unidades de las que pide el horario",
)


class Metamorficas:
    def __init__(self):
        self.casos: Counter = Counter()
        self.fallas: Counter = Counter()
        self.ejemplo: dict[str, str] = {}
        self.excluidos: Counter = Counter()

    def revisar(self, nombre: str, ok: bool, caso: str) -> None:
        self.casos[nombre] += 1
        if not ok:
            self.fallas[nombre] += 1
            self.ejemplo.setdefault(nombre, caso)


async def red_real(engine, rutas) -> tuple[dict, list[str]]:
    res, errores = {}, []
    for r in rutas:
        for dia in DIAS:
            for hora in HORAS:
                for clima in CLIMAS:
                    escenario = {"dia": dia, "hora": hora, "clima": clima}
                    try:
                        d = await engine.diagnose_route(r.route_id, overrides=escenario)
                    except Exception as exc:  # lo que se quiere medir
                        errores.append(f"{r.route_id} {dia} {hora} h {clima}: {exc!r}")
                        continue
                    res[(r.route_id, dia, hora, clima)] = d
    return res, errores


async def metamorficas(engine, rutas, res) -> Metamorficas:
    mr = Metamorficas()
    for r in rutas:
        via_exclusiva = r.route_type in (0, 1, 2)
        for dia in DIAS:
            for hora in HORAS:
                ds = [res.get((r.route_id, dia, hora, c)) for c in CLIMAS]
                if None in ds or _sin_servicio(ds[0]):
                    continue
                dem = [DEMANDA[d.nivel_demanda] for d in ds]
                ser = [SERVICIO[d.estado_servicio] for d in ds]
                caso = f"{r.label} · {dia} {hora}:00"
                mr.revisar(MR_CLIMA_DEMANDA, dem[0] <= dem[1] <= dem[2], caso)
                mr.revisar(MR_CLIMA_SERVICIO, ser[0] <= ser[1] <= ser[2], caso)
                if via_exclusiva:
                    mr.revisar(MR_LLUVIA_TREN, ser[1] == ser[0], caso)
                mr.revisar(
                    MR_MAL_CLIMA_FLOTA,
                    not any("reducir_unidades" in _acciones(d) for d in ds[1:]),
                    caso,
                )
        for hora in HORAS:
            for clima in CLIMAS:
                lab = res.get((r.route_id, "laboral", hora, clima))
                if lab is None or _sin_servicio(lab):
                    continue
                for dia in ("domingo", "festivo"):
                    otro = res.get((r.route_id, dia, hora, clima))
                    if otro is None or _sin_servicio(otro):
                        continue
                    # El nivel de demanda se mide contra el horario de ese día:
                    # solo se comparan días con el mismo tipo de horario.
                    if otro.hechos_iniciales.get(
                        "horario_reducido"
                    ) != lab.hechos_iniciales.get("horario_reducido"):
                        mr.excluidos[MR_CALENDARIO] += 1
                        continue
                    mr.revisar(
                        MR_CALENDARIO,
                        DEMANDA[otro.nivel_demanda] <= DEMANDA[lab.nivel_demanda],
                        f"{r.label} · {dia} {hora}:00, {CLIMA_TEXTO[clima]}",
                    )
        for hora in (8, 14):
            base = res.get((r.route_id, "laboral", hora, "clear"))
            if base is None:
                continue
            con = await engine.diagnose_route(
                r.route_id,
                overrides={
                    "dia": "laboral",
                    "hora": hora,
                    "clima": "clear",
                    "incidentes_activos": 1,
                    "severidad_max_incidente": "critical",
                },
            )
            caso = f"{r.label} · laboral {hora}:00"
            mr.revisar(
                MR_INCIDENCIA_SERVICIO,
                SERVICIO[con.estado_servicio] >= SERVICIO[base.estado_servicio],
                caso,
            )
            mr.revisar(
                MR_INCIDENCIA_AVISO, "notificar_usuarios" in _acciones(con), caso
            )
    return mr


def _resumen(d) -> str:
    flota = d.decision_flota or "sin cambio"
    if d.decision_flota in ("agregar", "reducir") and d.vehiculos_recomendados:
        flota += f" ({d.unidades_necesarias} → {d.vehiculos_recomendados})"
    acciones = ", ".join(a.id for a in d.acciones) or "ninguna"
    return (
        f"demanda {d.nivel_demanda}, servicio {d.estado_servicio}, flota {flota}; "
        f"acciones: {acciones}"
    )


async def aceptacion(engine) -> list[dict]:
    t15 = engine._gtfs.find_route("T15").route_id
    l1 = engine._gtfs.find_route("L1").route_id
    casos: list[dict] = []

    async def caso(nombre, esperado, ruta, cumple, obtenido=None, **escenario):
        vehicles = escenario.pop("vehicles", None)
        por_que_no = escenario.pop("por_que_no", None)
        d = await engine.diagnose_route(
            ruta, vehicles=vehicles, overrides=escenario, por_que_no=por_que_no
        )
        casos.append(
            {
                "caso": nombre,
                "esperado": esperado,
                "obtenido": obtenido(d) if obtenido else _resumen(d),
                "ok": bool(cumple(d)),
            }
        )

    def reglas(d):
        return {x["regla"] for x in d.reglas_disparadas}

    await caso(
        "T15, laboral 8:00, tormenta",
        "Demanda crítica; agregar unidades y alerta operativa",
        t15,
        lambda d: d.nivel_demanda == "critica"
        and {"agregar_unidades", "emitir_alerta_operativa"} <= _acciones(d),
        dia="laboral",
        hora=8,
        clima="thunderstorm",
    )
    await caso(
        "T15, laboral 8:00, lluvia",
        "Demanda alta, no crítica; servicio degradado; ampliar la ventana de llegada",
        t15,
        lambda d: d.nivel_demanda == "alta"
        and d.estado_servicio == "degradado"
        and "ampliar_ventana_eta" in _acciones(d),
        dia="laboral",
        hora=8,
        clima="rain",
    )
    await caso(
        "T15, laboral 11:00, despejado",
        "Hora valle: demanda normal y sin acciones de flota",
        t15,
        lambda d: d.nivel_demanda == "normal" and not _acciones(d) & ACCIONES_FLOTA,
        dia="laboral",
        hora=11,
        clima="clear",
    )
    await caso(
        "T15, festivo 8:00, despejado",
        "Sin hora pico: demanda baja y no agregar unidades",
        t15,
        lambda d: d.nivel_demanda == "baja"
        and "agregar_unidades" not in _acciones(d)
        and not {"R01", "R17B"} & reglas(d),
        dia="festivo",
        hora=8,
        clima="clear",
    )
    await caso(
        "T15, domingo 14:00, despejado",
        "Sin «pico no cubierto»; no agregar unidades",
        t15,
        lambda d: "R17B" not in reglas(d) and "agregar_unidades" not in _acciones(d),
        dia="domingo",
        hora=14,
        clima="clear",
    )
    await caso(
        "Tren L1, laboral 8:00, lluvia",
        "La lluvia no degrada el tren: servicio normal y sin agregar unidades",
        l1,
        lambda d: d.estado_servicio == "normal"
        and "agregar_unidades" not in _acciones(d),
        dia="laboral",
        hora=8,
        clima="rain",
    )
    await caso(
        "Tren L1, laboral 3:00",
        "Sin servicio: solo informar que no opera",
        l1,
        lambda d: _acciones(d) == {"informar_sin_servicio"},
        dia="laboral",
        hora=3,
        clima="clear",
    )
    await caso(
        "T15, laboral 11:00, niebla",
        "Velocidad reducida por seguridad",
        t15,
        lambda d: "reducir_velocidad_operativa" in _acciones(d),
        dia="laboral",
        hora=11,
        clima="fog",
    )
    await caso(
        "T15, laboral 11:00, incidencia crítica",
        "Servicio crítico; avisar a los usuarios y sugerir una ruta alterna",
        t15,
        lambda d: d.estado_servicio == "critico"
        and {"notificar_usuarios", "sugerir_ruta_alterna"} <= _acciones(d),
        dia="laboral",
        hora=11,
        clima="clear",
        incidentes_activos=1,
        severidad_max_incidente="critical",
    )
    await caso(
        "T15, retraso crónico (6 calificaciones, 7.5 min tarde)",
        "Ajustar el horario publicado y auditar la regularidad",
        t15,
        lambda d: {"ajustar_horario_publicado", "revisar_regularidad"} <= _acciones(d),
        dia="laboral",
        hora=11,
        clima="clear",
        muestras_feedback=6,
        error_medio_min=7.5,
        desviacion_error_min=12.0,
    )
    await caso(
        "T15, tormenta 8:00 con 120 unidades en servicio",
        "Ya hay más de las que pide: mantener, sin agregar",
        t15,
        lambda d: d.decision_flota == "mantener"
        and "agregar_unidades" not in _acciones(d),
        obtenido=lambda d: f"decisión {d.decision_flota}; {d.nota_flota}",
        vehicles=120,
        dia="laboral",
        hora=8,
        clima="thunderstorm",
    )
    await caso(
        "¿Por qué no retirar unidades? (T15, tormenta 8:00)",
        "Explica que R62 necesita «reducir» y se concluyó «agregar»",
        t15,
        lambda d: bool(d.por_que_no)
        and "R62" in d.por_que_no[0]
        and "agregar" in d.por_que_no[0],
        obtenido=lambda d: " ".join(d.por_que_no),
        por_que_no="reducir_unidades",
        dia="laboral",
        hora=8,
        clima="thunderstorm",
    )
    return casos


async def verificar(reglas: str | None = None) -> dict:
    inicio = time.perf_counter()
    engine = _motor()
    if reglas:
        engine._expert = SistemaExperto(reglas)
    rutas = engine._gtfs.get_routes()
    res, errores = await red_real(engine, rutas)

    faltas: Counter = Counter()
    ejemplos: dict[str, str] = {}
    usadas: set[str] = set()
    for (ruta, dia, hora, clima), d in res.items():
        for p in problemas(d):
            faltas[p] += 1
            ejemplos.setdefault(p, f"{ruta} · {dia} {hora}:00, {CLIMA_TEXTO[clima]}")
        usadas |= {x["regla"] for x in d.reglas_disparadas}

    claves = list(res)[:: max(1, len(res) // 300)]
    distintos = 0
    for ruta, dia, hora, clima in claves:
        otra = await engine.diagnose_route(
            ruta, overrides={"dia": dia, "hora": hora, "clima": clima}
        )
        if otra.model_dump(mode="json") != res[(ruta, dia, hora, clima)].model_dump(
            mode="json"
        ):
            distintos += 1

    mr = await metamorficas(engine, rutas, res)
    casos = await aceptacion(engine)
    return {
        "version": engine._expert.base.version,
        "total_reglas": len(engine._expert.base.reglas),
        "rutas": len(rutas),
        "modos": Counter(r.mode for r in rutas),
        "diagnosticos": len(res),
        "errores": errores,
        "sin_servicio": sum(_sin_servicio(d) for d in res.values()),
        "faltas": faltas,
        "ejemplos": ejemplos,
        "reglas_usadas": len(usadas),
        "repetidos": len(claves),
        "distintos": distintos,
        "mr": mr,
        "casos": casos,
        "segundos": time.perf_counter() - inicio,
    }


def hay_fallas(r: dict) -> bool:
    return bool(
        r["errores"]
        or sum(r["faltas"].values())
        or r["distintos"]
        or sum(r["mr"].fallas.values())
        or not all(c["ok"] for c in r["casos"])
    )


def reporte(r: dict) -> str:
    mr: Metamorficas = r["mr"]
    nombres = {"bus": "camión", "brt": "BRT", "tren": "tren"}
    modos = ", ".join(
        f"{n} de {nombres.get(m, m)}" for m, n in r["modos"].most_common()
    )
    ok_casos = sum(c["ok"] for c in r["casos"])
    lineas = [
        "# Verificación del sistema experto sobre las rutas reales",
        "",
        f"Base de conocimiento versión {r['version']}, generado el "
        f"{date.today():%d/%m/%Y} en {r['segundos']:.0f} s.",
        "",
        "```bash",
        "python scripts/verificacion_experto.py --salida docs/verificacion_experto.md",
        "```",
        "",
        "## 1. Diagnósticos sobre la red real",
        "",
        f"- **Rutas:** {r['rutas']} ({modos}).",
        f"- **Escenarios:** {len(DIAS)} tipos de día × {len(HORAS)} horas "
        f"({', '.join(f'{h}:00' for h in HORAS)}) × {len(CLIMAS)} climas "
        f"= {r['diagnosticos']:,} diagnósticos, {r['sin_servicio']:,} de ellos "
        "en rutas sin salidas a esa hora.",
        f"- **Errores:** {len(r['errores'])}.",
        f"- **Mismo escenario, mismo diagnóstico:** {r['repetidos']} repetidos, "
        f"{r['distintos']} distintos.",
        f"- **Reglas que se activaron:** {r['reglas_usadas']} de {r['total_reglas']}. "
        "Las demás necesitan datos que este recorrido no trae: incidencias, un "
        "conteo de flota o calificaciones de usuarios. Las cubren el barrido y los "
        "casos de aceptación.",
        "",
        "| Contradicción o falta de trazabilidad | Diagnósticos | Ejemplo |",
        "|---|---|---|",
    ]
    for nombre in PROBLEMAS:
        n = r["faltas"][nombre]
        lineas.append(f"| {nombre} | {n:,} | {r['ejemplos'].get(nombre, '—')} |")
    lineas += [
        "",
        "## 2. Pruebas metamórficas",
        "",
        "Cada relación compara dos escenarios que solo difieren en una cosa y revisa "
        "que el cambio vaya en el sentido lógico, sin necesidad de conocer la "
        "respuesta exacta.",
        "",
        "| Relación | Casos | Fallas | Ejemplo de falla |",
        "|---|---|---|---|",
    ]
    for nombre in (
        MR_CLIMA_DEMANDA,
        MR_CLIMA_SERVICIO,
        MR_LLUVIA_TREN,
        MR_MAL_CLIMA_FLOTA,
        MR_CALENDARIO,
        MR_INCIDENCIA_SERVICIO,
        MR_INCIDENCIA_AVISO,
    ):
        lineas.append(
            f"| {nombre} | {mr.casos[nombre]:,} | {mr.fallas[nombre]:,} "
            f"| {mr.ejemplo.get(nombre, '—')} |"
        )
    if mr.excluidos[MR_CALENDARIO]:
        lineas += [
            "",
            f"La relación de calendario deja fuera {mr.excluidos[MR_CALENDARIO]:,} "
            "pares en los que el domingo o el festivo ya traen un horario reducido y "
            "el día laboral no. El nivel de demanda se mide contra el servicio que el "
            "horario programa: si el horario ya bajó, la demanda puede quedar "
            "«normal» aunque en el día laboral, con más salidas, sea «baja».",
        ]
    lineas += [
        "",
        "## 3. Casos de aceptación",
        "",
        f"El resultado esperado se escribió antes de correr cada caso. "
        f"Aprobados: {ok_casos} de {len(r['casos'])}.",
        "",
        "| Caso | Esperado | Obtenido | Resultado |",
        "|---|---|---|---|",
    ]
    for c in r["casos"]:
        estado = "Aprobado" if c["ok"] else "Falla"
        lineas.append(f"| {c['caso']} | {c['esperado']} | {c['obtenido']} | {estado} |")
    lineas += [
        "",
        "## Lo que esta verificación no demuestra",
        "",
        "Demuestra que el sistema está bien construido y que razona de forma "
        "coherente con su base de conocimiento sobre toda la red. No demuestra que "
        "sus conclusiones coincidan con lo que pasa en la calle: para eso hace falta "
        "comparar con una persona que conozca la operación (`docs/validacion/`) o "
        "con conteos reales de pasajeros.",
    ]
    if r["errores"]:
        lineas += ["", "## Errores", ""] + [f"- {e}" for e in r["errores"][:20]]
    return "\n".join(lineas) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verificación sobre las rutas reales.")
    parser.add_argument("--reglas", default=None, help="Base de conocimiento a usar")
    parser.add_argument("--salida", default=None, help="Archivo Markdown del reporte")
    args = parser.parse_args(argv)

    r = asyncio.run(verificar(args.reglas))
    texto = reporte(r)
    if args.salida:
        Path(args.salida).write_text(texto, encoding="utf-8")
    print(texto)
    return 1 if hay_fallas(r) else 0


if __name__ == "__main__":
    sys.exit(main())
