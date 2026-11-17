"""
Base de conocimiento: carga y valida las reglas declaradas en `rules.yaml`.

Las reglas se editan en el YAML, sin tocar código Python. Al cargar se valida
que cada regla esté bien formada, que todo hecho usado en una premisa sea un
dato inicial declarado o la conclusión de alguna otra regla, y que las
negaciones (`!=`, `no_en`, `desconocido`) solo se apliquen a datos iniciales.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .inference import (
    OPERADORES,
    OPERADORES_NEGATIVOS,
    OPERADORES_SIN_VALOR,
    UMBRAL_CERTEZA,
    Conclusion,
    Condicion,
    Regla,
)

RUTA_REGLAS = Path(__file__).with_name("rules.yaml")


class ErrorBaseConocimiento(ValueError):
    """La base de conocimiento está mal formada."""


@dataclass(frozen=True)
class BaseConocimiento:
    version: str
    hechos_iniciales: dict[str, str]
    acciones: dict[str, str]
    reglas: tuple[Regla, ...]
    univaluados: frozenset[str] = frozenset()

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "total_reglas": len(self.reglas),
            "hechos_iniciales": dict(self.hechos_iniciales),
            "acciones": dict(self.acciones),
            "univaluados": sorted(self.univaluados),
            "reglas": [
                {
                    "id": regla.id,
                    "nombre": regla.nombre,
                    "prioridad": regla.prioridad,
                    "certeza": regla.certeza,
                    "texto": regla.texto(),
                    "explicacion": regla.explicacion,
                }
                for regla in self.reglas
            ],
        }


def cargar_base(ruta: str | Path | None = None) -> BaseConocimiento:
    path = Path(ruta) if ruta else RUTA_REGLAS
    with path.open(encoding="utf-8") as archivo:
        datos = yaml.safe_load(archivo) or {}
    return construir_base(datos)


def construir_base(datos: dict[str, Any]) -> BaseConocimiento:
    if not isinstance(datos, dict):
        raise ErrorBaseConocimiento("La base de conocimiento debe ser un mapa YAML.")

    hechos_iniciales = dict(datos.get("hechos_iniciales") or {})
    acciones = dict(datos.get("acciones") or {})
    crudas = datos.get("reglas") or []
    if not crudas:
        raise ErrorBaseConocimiento("La base de conocimiento no tiene reglas.")

    reglas: list[Regla] = []
    ids: set[str] = set()
    for orden, cruda in enumerate(crudas):
        regla = _construir_regla(cruda, orden)
        if regla.id in ids:
            raise ErrorBaseConocimiento(f"Regla duplicada: {regla.id}")
        ids.add(regla.id)
        reglas.append(regla)

    univaluados = frozenset(str(n) for n in datos.get("univaluados") or [])
    _validar_referencias(reglas, hechos_iniciales, acciones)
    _validar_estratos(reglas, univaluados)
    return BaseConocimiento(
        version=str(datos.get("version", "1")),
        hechos_iniciales=hechos_iniciales,
        acciones=acciones,
        reglas=tuple(reglas),
        univaluados=univaluados,
    )


def _construir_regla(cruda: Any, orden: int) -> Regla:
    if not isinstance(cruda, dict) or not cruda.get("id"):
        raise ErrorBaseConocimiento(f"Regla #{orden + 1} sin 'id'.")
    regla_id = str(cruda["id"])

    todas = tuple(_condicion(c, regla_id) for c in cruda.get("si") or [])
    alguna = tuple(_condicion(c, regla_id) for c in cruda.get("si_alguna") or [])
    if not todas and not alguna:
        raise ErrorBaseConocimiento(f"{regla_id}: la regla no tiene premisa.")

    entonces = tuple(_conclusion(c, regla_id) for c in cruda.get("entonces") or [])
    if not entonces:
        raise ErrorBaseConocimiento(f"{regla_id}: la regla no tiene conclusión.")

    certeza = float(cruda.get("certeza", 1.0))
    if not 0.0 < certeza <= 1.0:
        raise ErrorBaseConocimiento(f"{regla_id}: 'certeza' debe estar en (0, 1].")

    return Regla(
        id=regla_id,
        nombre=str(cruda.get("nombre", regla_id)),
        todas=todas,
        alguna=alguna,
        entonces=entonces,
        prioridad=int(cruda.get("prioridad", 50)),
        certeza=certeza,
        explicacion=str(cruda.get("explicacion", "")).strip(),
        orden=orden,
    )


def _condicion(cruda: Any, regla_id: str) -> Condicion:
    if not isinstance(cruda, dict) or "hecho" not in cruda:
        raise ErrorBaseConocimiento(f"{regla_id}: condición sin 'hecho'.")
    op = str(cruda.get("op", "=="))
    if op not in OPERADORES:
        raise ErrorBaseConocimiento(f"{regla_id}: operador desconocido '{op}'.")
    if op in OPERADORES_SIN_VALOR:
        if "valor" in cruda:
            raise ErrorBaseConocimiento(f"{regla_id}: '{op}' no lleva 'valor'.")
        return Condicion(
            hecho=str(cruda["hecho"]),
            op=op,
            valor=None,
            certeza_min=float(cruda.get("certeza_min", UMBRAL_CERTEZA)),
        )
    if "valor" not in cruda:
        raise ErrorBaseConocimiento(f"{regla_id}: condición sin 'valor'.")
    valor = cruda["valor"]
    if op in {"en", "no_en"} and not isinstance(valor, list):
        raise ErrorBaseConocimiento(f"{regla_id}: '{op}' requiere una lista.")
    if op in {">", ">=", "<", "<="} and (
        isinstance(valor, bool) or not isinstance(valor, (int, float))
    ):
        raise ErrorBaseConocimiento(f"{regla_id}: '{op}' requiere un número.")
    return Condicion(
        hecho=str(cruda["hecho"]),
        op=op,
        valor=valor,
        certeza_min=float(cruda.get("certeza_min", UMBRAL_CERTEZA)),
    )


def _conclusion(cruda: Any, regla_id: str) -> Conclusion:
    if not isinstance(cruda, dict) or "hecho" not in cruda or "valor" not in cruda:
        raise ErrorBaseConocimiento(f"{regla_id}: conclusión sin 'hecho' o 'valor'.")
    return Conclusion(hecho=str(cruda["hecho"]), valor=cruda["valor"])


def _validar_referencias(
    reglas: list[Regla], hechos_iniciales: dict[str, str], acciones: dict[str, str]
) -> None:
    concluidos = {c.hecho for regla in reglas for c in regla.entonces}
    for regla in reglas:
        for condicion in (*regla.todas, *regla.alguna):
            if (
                condicion.hecho not in hechos_iniciales
                and condicion.hecho not in concluidos
            ):
                raise ErrorBaseConocimiento(
                    f"{regla.id}: el hecho '{condicion.hecho}' no es un dato "
                    "inicial ni lo concluye ninguna regla."
                )
            if (
                condicion.op in OPERADORES_NEGATIVOS
                and condicion.hecho not in hechos_iniciales
            ):
                raise ErrorBaseConocimiento(
                    f"{regla.id}: '{condicion.op}' solo puede usarse sobre datos "
                    f"iniciales, y '{condicion.hecho}' es un hecho derivado."
                )
        for conclusion in regla.entonces:
            if conclusion.hecho in hechos_iniciales:
                raise ErrorBaseConocimiento(
                    f"{regla.id}: no se puede concluir el dato inicial "
                    f"'{conclusion.hecho}'."
                )
            if conclusion.hecho == "accion" and conclusion.valor not in acciones:
                raise ErrorBaseConocimiento(
                    f"{regla.id}: acción no declarada '{conclusion.valor}'."
                )


def _validar_estratos(reglas: list[Regla], univaluados: frozenset[str]) -> None:
    """
    Un atributo univaluado debe quedar decidido antes de usarse: toda regla que
    lo concluye necesita más prioridad que cualquier regla que lo consulta.
    """
    for atributo in univaluados:
        escritoras = [r for r in reglas if any(c.hecho == atributo for c in r.entonces)]
        lectoras = [
            r for r in reglas if any(c.hecho == atributo for c in (*r.todas, *r.alguna))
        ]
        if not escritoras:
            raise ErrorBaseConocimiento(
                f"Ninguna regla concluye el atributo univaluado '{atributo}'."
            )
        minimo = min(r.prioridad for r in escritoras)
        for regla in lectoras:
            if regla.prioridad >= minimo:
                raise ErrorBaseConocimiento(
                    f"{regla.id}: consulta '{atributo}' con prioridad "
                    f"{regla.prioridad}, pero debe ser menor que {minimo}."
                )
