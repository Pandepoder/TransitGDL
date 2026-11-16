"""
Sistema experto: une la base de conocimiento con el motor de inferencia y
traduce la memoria de trabajo final a un diagnóstico con explicación.
"""

from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .inference import (
    ORIGEN_INICIAL,
    Hecho,
    MotorInferencia,
    ResultadoInferencia,
    _clave,
)
from .knowledge_base import BaseConocimiento, cargar_base

NIVEL_POR_OMISION = "normal"
ESTADO_POR_OMISION = "normal"
# Acciones que dependen de la decisión de flota (y de la cifra recomendada).
ACCIONES_FLOTA = {"agregar": "agregar_unidades", "reducir": "reducir_unidades"}

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
    # "agregar", "reducir", "mantener" (la flota actual ya cumple) o None.
    decision_flota: str | None = None
    nota_flota: str | None = None
    acciones: list[AccionRecomendada] = Field(default_factory=list)
    # Respuesta a "¿por qué no se recomendó X?", si se pidió.
    por_que_no: list[str] = Field(default_factory=list)
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
        if self.nota_flota:
            lineas.append(self.nota_flota)
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
        por_que_no: str | None = None,
    ) -> Diagnostico:
        hechos = {k: v for k, v in hechos.items() if v is not None}
        resultado = self.inferir(hechos)
        memoria = resultado.memoria

        nivel = memoria.mejor("nivel_demanda")
        estado = memoria.mejor("estado_servicio")
        actuales = hechos.get("vehiculos_en_ruta")
        recomendados, decision, nota = _plan_flota(
            actuales if _entero(actuales) else None,
            hechos.get("unidades_necesarias"),
            memoria.mejor("decision_flota"),
            memoria.mejor("factor_flota"),
        )
        # Si la cifra muestra que la flota actual ya cumple, la acción de
        # flota que concluyó el motor se retira (queda explicada en la nota).
        descartadas = set(ACCIONES_FLOTA.values()) if decision == "mantener" else set()

        acciones = []
        for hecho in sorted(
            memoria.valores("accion"), key=lambda h: h.certeza, reverse=True
        ):
            if hecho.valor in descartadas:
                continue
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
            vehiculos_actuales=actuales if _entero(actuales) else None,
            vehiculos_recomendados=recomendados,
            decision_flota=decision,
            nota_flota=nota,
            acciones=acciones,
            hechos_iniciales=hechos,
            hechos_derivados=[
                h.to_dict() for h in memoria.todos() if ORIGEN_INICIAL not in h.origenes
            ],
            reglas_disparadas=[d.to_dict() for d in resultado.disparos],
            ciclos=resultado.ciclos,
            rutas_alternas=list(rutas_alternas or []),
        )
        if por_que_no:
            diagnostico.por_que_no = self.explicar_por_que_no(
                resultado, por_que_no, nota_flota=nota if descartadas else None
            )
        diagnostico.resumen = _resumen(diagnostico)
        return diagnostico

    def explicar_por_que_no(
        self,
        resultado: ResultadoInferencia,
        accion: str,
        *,
        nota_flota: str | None = None,
    ) -> list[str]:
        """
        Explicación del "¿por qué no?": para cada regla que podría recomendar
        `accion`, si se aplicó o qué condiciones no se cumplieron.
        """
        if accion not in self.base.acciones:
            return [f"«{accion}» no es una acción de la base de conocimiento."]
        disparadas = {d.regla.id for d in resultado.disparos}
        lineas = []
        for regla in self._reglas_que_concluyen("accion", [accion]):
            if regla.id in disparadas:
                lineas.append(f"{regla.id} · {regla.nombre}: sí se aplicó.")
                continue
            fallas = self._fallas(regla, resultado, disparadas, profundidad=2)
            lineas.append(
                f"{regla.id} · {regla.nombre}: no se cumple " + "; ".join(fallas) + "."
            )
        if nota_flota and accion in ACCIONES_FLOTA.values():
            lineas.append(nota_flota)
        return lineas

    def _reglas_que_concluyen(self, hecho: str, valores: list[Any]) -> list:
        claves = {_clave(v) for v in valores}
        return [
            regla
            for regla in self.base.reglas
            if any(
                c.hecho == hecho and _clave(c.valor) in claves for c in regla.entonces
            )
        ]

    def _fallas(
        self,
        regla,
        resultado: ResultadoInferencia,
        disparadas: set[str],
        profundidad: int,
    ) -> list[str]:
        """Condiciones de `regla` que no se cumplen, con su causa si es derivada."""
        memoria = resultado.memoria
        fallas = [
            self._causa(c, resultado, disparadas, profundidad)
            for c in regla.todas
            if self.motor.condicion_cumplida(c, memoria) is None
        ]
        if regla.alguna and not any(
            self.motor.condicion_cumplida(c, memoria) for c in regla.alguna
        ):
            fallas.append("ninguna de: " + " O ".join(c.texto() for c in regla.alguna))
        return fallas

    def _causa(
        self,
        condicion,
        resultado: ResultadoInferencia,
        disparadas: set[str],
        profundidad: int,
    ) -> str:
        """Texto de una condición fallida y, si es un hecho derivado, por qué faltó."""
        texto = condicion.texto()
        if (
            profundidad <= 1
            or condicion.hecho in self.base.hechos_iniciales
            or condicion.op not in ("==", "en")
        ):
            return texto
        valores = list(condicion.valor) if condicion.op == "en" else [condicion.valor]
        memoria = resultado.memoria
        mejor = memoria.mejor(condicion.hecho)
        if mejor is not None and condicion.hecho in self.base.univaluados:
            return f"{texto} (se concluyó {condicion.hecho} = {mejor.valor})"
        motivos = []
        for regla in self._reglas_que_concluyen(condicion.hecho, valores):
            if regla.id in disparadas:
                motivos.append(f"{regla.id} se aplicó, pero con certeza insuficiente")
                continue
            fallas = self._fallas(regla, resultado, disparadas, profundidad - 1)
            motivos.append(f"{regla.id} requiere " + " y ".join(fallas))
        return f"{texto} ({'; '.join(motivos)})" if motivos else texto


def _entero(valor: Any) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _plan_flota(
    actuales: int | None,
    necesarias: Any,
    decision: Hecho | None,
    factor: Hecho | None,
) -> tuple[int | None, str | None, str | None]:
    """
    Cifra de flota recomendada a partir de la decisión del motor.

    Devuelve (unidades recomendadas, decisión, nota). La base es lo que la
    ruta necesita según su horario; si no se conoce, lo que hay en servicio.
    Si la cifra contradice la decisión (decir "agregar" y recomendar menos de
    las que ya circulan), la decisión pasa a "mantener" y la nota lo explica.
    """
    valor = str(decision.valor) if decision else None
    if actuales is None:
        return None, valor, None
    if valor is None:
        return actuales, None, None
    base = necesarias if _entero(necesarias) and necesarias > 0 else actuales
    if base <= 0:
        return (
            None,
            valor,
            "No hay unidades en servicio ni flota de referencia para calcular cuántas.",
        )
    multiplicador = float(factor.valor) if factor else 1.0
    objetivo = max(1, ceil(base * multiplicador))
    if valor == "agregar" and objetivo <= actuales:
        return (
            actuales,
            "mantener",
            f"La flota actual ({actuales}) ya cubre lo recomendado ({objetivo}); "
            "no hace falta agregar unidades.",
        )
    if valor == "reducir" and objetivo >= actuales:
        return (
            actuales,
            "mantener",
            f"La flota actual ({actuales}) ya está por debajo de lo recomendado "
            f"({objetivo}); no conviene retirar unidades.",
        )
    return objetivo, valor, None


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
