"""
Sistema experto: une la base de conocimiento con el motor de inferencia y
traduce la memoria de trabajo final a un diagnóstico con explicación.
"""

from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .inference import ORIGEN_INICIAL, MotorInferencia, ResultadoInferencia
from .knowledge_base import BaseConocimiento, cargar_base

NIVEL_POR_OMISION = "normal"
ESTADO_POR_OMISION = "normal"

_ETIQUETA_NIVEL = {
    "critica": "crítica",
    "alta": "alta",
    "normal": "normal",
    "baja": "baja",
}
_ETIQUETA_ESTADO = {
    "critico": "crítico",
    "degradado": "degradado",
    "normal": "normal",
}


class AccionRecomendada(BaseModel):
    id: str
    descripcion: str
    certeza: float
    reglas: list[str] = Field(default_factory=list)
    porque: list[str] = Field(default_factory=list)


class Diagnostico(BaseModel):
    route_id: str | None = None
    route_name: str | None = None
    nivel_demanda: str = NIVEL_POR_OMISION
    certeza_demanda: float | None = None
    estado_servicio: str = ESTADO_POR_OMISION
    certeza_servicio: float | None = None
    vehiculos_actuales: int | None = None
    vehiculos_recomendados: int | None = None
    acciones: list[AccionRecomendada] = Field(default_factory=list)
    hechos_iniciales: dict[str, Any] = Field(default_factory=dict)
    hechos_derivados: list[dict] = Field(default_factory=list)
    reglas_disparadas: list[dict] = Field(default_factory=list)
    ciclos: int = 0
    rutas_alternas: list[str] = Field(default_factory=list)
    resumen: str = ""

    def to_chat_message(self) -> str:
        titulo = f"*Diagnóstico experto · ruta {self.route_name or self.route_id}*"
        lineas = [
            titulo,
            f"Demanda: *{_ETIQUETA_NIVEL.get(self.nivel_demanda, self.nivel_demanda)}*"
            + _pct(self.certeza_demanda),
            f"Servicio: *{_ETIQUETA_ESTADO.get(self.estado_servicio, self.estado_servicio)}*"
            + _pct(self.certeza_servicio),
        ]
        if (
            self.vehiculos_recomendados is not None
            and self.vehiculos_actuales is not None
            and self.vehiculos_recomendados != self.vehiculos_actuales
        ):
            lineas.append(
                f"Flota: {self.vehiculos_actuales} en ruta → "
                f"{self.vehiculos_recomendados} recomendadas"
            )
        if self.acciones:
            lineas.append("")
            lineas.append("*Recomendaciones*")
            for accion in self.acciones[:4]:
                lineas.append(f"· {accion.descripcion}{_pct(accion.certeza)}")
                if accion.porque:
                    lineas.append(f"  Porque: {accion.porque[-1]}")
            if self.rutas_alternas:
                lineas.append("Rutas alternas: " + ", ".join(self.rutas_alternas[:4]))
        else:
            lineas.append("")
            lineas.append("Sin acciones: la ruta opera con normalidad.")
        lineas.append("")
        lineas.append(f"Reglas aplicadas: {len(self.reglas_disparadas)}")
        return "\n".join(lineas)


class SistemaExperto:
    def __init__(self, ruta_reglas: str | Path | None = None):
        self.base: BaseConocimiento = cargar_base(ruta_reglas)
        self.motor = MotorInferencia(
            list(self.base.reglas), univaluados=set(self.base.univaluados)
        )

    def validar_hechos(self, hechos: dict[str, Any]) -> list[str]:
        """Nombres de hechos que no están declarados como datos iniciales."""
        return sorted(set(hechos) - set(self.base.hechos_iniciales))

    def inferir(self, hechos: dict[str, Any]) -> ResultadoInferencia:
        return self.motor.inferir(hechos)

    def diagnosticar(
        self,
        hechos: dict[str, Any],
        *,
        route_id: str | None = None,
        route_name: str | None = None,
        rutas_alternas: list[str] | None = None,
    ) -> Diagnostico:
        hechos = {k: v for k, v in hechos.items() if v is not None}
        resultado = self.inferir(hechos)
        memoria = resultado.memoria

        nivel = memoria.mejor("nivel_demanda")
        estado = memoria.mejor("estado_servicio")
        factor = memoria.mejor("factor_flota")

        actuales = hechos.get("vehiculos_en_ruta")
        necesarias = hechos.get("unidades_necesarias")
        recomendados = None
        if isinstance(actuales, int) and not isinstance(actuales, bool):
            multiplicador = float(factor.valor) if factor else 1.0
            # La base es lo que la ruta necesita según su horario; si no se
            # conoce, se parte de lo que hay en servicio.
            base = (
                necesarias
                if isinstance(necesarias, int) and necesarias > 0
                else actuales
            )
            if multiplicador == 1.0 and base == actuales:
                recomendados = actuales
            else:
                recomendados = max(1, ceil(max(1, base) * multiplicador))

        acciones = []
        for hecho in sorted(
            memoria.valores("accion"), key=lambda h: h.certeza, reverse=True
        ):
            cadena = resultado.cadena("accion", hecho.valor)
            acciones.append(
                AccionRecomendada(
                    id=str(hecho.valor),
                    descripcion=self.base.acciones.get(hecho.valor, str(hecho.valor)),
                    certeza=round(hecho.certeza, 2),
                    reglas=[d.regla.id for d in cadena],
                    porque=[
                        f"{d.regla.id} · {d.regla.explicacion or d.regla.nombre}"
                        for d in cadena
                    ],
                )
            )

        diagnostico = Diagnostico(
            route_id=route_id,
            route_name=route_name,
            nivel_demanda=str(nivel.valor) if nivel else NIVEL_POR_OMISION,
            certeza_demanda=round(nivel.certeza, 2) if nivel else None,
            estado_servicio=str(estado.valor) if estado else ESTADO_POR_OMISION,
            certeza_servicio=round(estado.certeza, 2) if estado else None,
            vehiculos_actuales=actuales if isinstance(actuales, int) else None,
            vehiculos_recomendados=recomendados,
            acciones=acciones,
            hechos_iniciales=hechos,
            hechos_derivados=[
                h.to_dict() for h in memoria.todos() if ORIGEN_INICIAL not in h.origenes
            ],
            reglas_disparadas=[d.to_dict() for d in resultado.disparos],
            ciclos=resultado.ciclos,
            rutas_alternas=list(rutas_alternas or []),
        )
        diagnostico.resumen = _resumen(diagnostico)
        return diagnostico


def _resumen(d: Diagnostico) -> str:
    partes = [
        f"Demanda {_ETIQUETA_NIVEL.get(d.nivel_demanda, d.nivel_demanda)}",
        f"servicio {_ETIQUETA_ESTADO.get(d.estado_servicio, d.estado_servicio)}",
    ]
    if d.acciones:
        partes.append(f"{len(d.acciones)} acciones recomendadas")
    else:
        partes.append("sin acciones")
    return " · ".join(partes)


def _pct(certeza: float | None) -> str:
    if certeza is None:
        return ""
    return f" ({round(certeza * 100)} %)"
