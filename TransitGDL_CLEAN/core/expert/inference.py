"""
Motor de inferencia del sistema experto de TransitGDL.

Encadenamiento hacia adelante con factores de certeza (estilo MYCIN):

    • Memoria de trabajo: hechos atributo–valor. Un atributo puede tener varios
      valores, cada uno con su propio factor de certeza (FC, entre 0 y 1).
    • Ciclo reconocer–actuar: en cada ciclo se arma la agenda con las reglas
      cuya premisa se cumple y que aún no se han disparado, se elige UNA
      (resolución de conflictos) y se dispara. Se repite hasta agenda vacía.
    • Resolución de conflictos: 1) mayor prioridad, 2) mayor especificidad
      (más condiciones), 3) orden de aparición en la base de conocimiento.
    • Refracción: cada regla se dispara como máximo una vez por consulta.
    • Certeza: FC(premisa) = mínimo de las condiciones "todas" (y el máximo de
      las "alguna"); FC(conclusión) = FC(premisa) × certeza de la regla.
      Si dos reglas concluyen el mismo hecho, se combinan:
      FC = FC1 + FC2 × (1 − FC1).
    • Datos ausentes: `desconocido` se cumple cuando un dato inicial no llegó
      (negación por ausencia) y `conocido` cuando sí llegó, con cualquier valor.

El motor no sabe nada de transporte: todo el conocimiento del dominio vive en
`rules.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Un hecho con certeza menor a este umbral se considera desconocido.
UMBRAL_CERTEZA = 0.2
MAX_CICLOS = 500

OPERADORES = {
    "==",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "en",
    "no_en",
    "conocido",
    "desconocido",
}
# Operadores que no comparan contra un valor.
OPERADORES_SIN_VALOR = {"conocido", "desconocido"}
# Operadores de negación: solo son seguros sobre datos iniciales, que no cambian
# durante la inferencia (un hecho derivado podría aparecer después).
OPERADORES_NEGATIVOS = {"!=", "no_en", "desconocido"}
ORIGEN_INICIAL = "dato"
VALOR_DESCONOCIDO = "desconocido"


@dataclass(frozen=True)
class Condicion:
    hecho: str
    op: str
    valor: Any
    certeza_min: float = UMBRAL_CERTEZA

    def texto(self) -> str:
        if self.op in OPERADORES_SIN_VALOR:
            base = f"{self.hecho} es {self.op}"
        else:
            base = f"{self.hecho} {self.op} {_fmt(self.valor)}"
        if self.certeza_min > UMBRAL_CERTEZA:
            base += f" (certeza ≥ {self.certeza_min:g})"
        return base


@dataclass(frozen=True)
class Conclusion:
    hecho: str
    valor: Any

    def texto(self) -> str:
        return f"{self.hecho} = {_fmt(self.valor)}"


@dataclass(frozen=True)
class Regla:
    id: str
    nombre: str
    todas: tuple[Condicion, ...]
    alguna: tuple[Condicion, ...]
    entonces: tuple[Conclusion, ...]
    prioridad: int = 50
    certeza: float = 1.0
    explicacion: str = ""
    orden: int = 0

    @property
    def especificidad(self) -> int:
        return len(self.todas) + len(self.alguna)

    def texto(self) -> str:
        partes = [c.texto() for c in self.todas]
        if self.alguna:
            partes.append("(" + " O ".join(c.texto() for c in self.alguna) + ")")
        return (
            "SI "
            + " Y ".join(partes)
            + " ENTONCES "
            + ", ".join(c.texto() for c in self.entonces)
        )


@dataclass
class Hecho:
    nombre: str
    valor: Any
    certeza: float = 1.0
    origenes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hecho": self.nombre,
            "valor": self.valor,
            "certeza": round(self.certeza, 3),
            "origenes": list(self.origenes),
        }


@dataclass
class Disparo:
    """Registro de una regla disparada: la base del módulo de explicación."""

    ciclo: int
    regla: Regla
    certeza_premisa: float
    premisas: list[Hecho]
    conclusiones: list[Hecho]

    def to_dict(self) -> dict:
        return {
            "ciclo": self.ciclo,
            "regla": self.regla.id,
            "nombre": self.regla.nombre,
            "prioridad": self.regla.prioridad,
            "texto": self.regla.texto(),
            "explicacion": self.regla.explicacion,
            "certeza_premisa": round(self.certeza_premisa, 3),
            "premisas": [
                {"hecho": h.nombre, "valor": h.valor, "certeza": round(h.certeza, 3)}
                for h in self.premisas
            ],
            "conclusiones": [
                {"hecho": h.nombre, "valor": h.valor, "certeza": round(h.certeza, 3)}
                for h in self.conclusiones
            ],
        }


class MemoriaTrabajo:
    """Base de hechos: atributo → {valor → Hecho}."""

    def __init__(self, hechos_iniciales: dict[str, Any] | None = None):
        self._hechos: dict[str, dict[Any, Hecho]] = {}
        for nombre, valor in (hechos_iniciales or {}).items():
            if valor is None:
                continue
            self.afirmar(nombre, valor, 1.0, ORIGEN_INICIAL)

    def afirmar(self, nombre: str, valor: Any, certeza: float, origen: str) -> Hecho:
        valores = self._hechos.setdefault(nombre, {})
        clave = _clave(valor)
        existente = valores.get(clave)
        if existente is None:
            hecho = Hecho(nombre, valor, _acotar(certeza), [origen])
            valores[clave] = hecho
            return hecho
        existente.certeza = _acotar(
            existente.certeza + certeza * (1.0 - existente.certeza)
        )
        if origen not in existente.origenes:
            existente.origenes.append(origen)
        return existente

    def valores(self, nombre: str) -> list[Hecho]:
        return list(self._hechos.get(nombre, {}).values())

    def obtener(self, nombre: str, valor: Any) -> Hecho | None:
        return self._hechos.get(nombre, {}).get(_clave(valor))

    def mejor(self, nombre: str) -> Hecho | None:
        """Valor con mayor certeza de un atributo (None si es desconocido)."""
        candidatos = [h for h in self.valores(nombre) if h.certeza >= UMBRAL_CERTEZA]
        if not candidatos:
            return None
        return max(candidatos, key=lambda h: h.certeza)

    def todos(self) -> list[Hecho]:
        return [h for valores in self._hechos.values() for h in valores.values()]


@dataclass
class ResultadoInferencia:
    memoria: MemoriaTrabajo
    disparos: list[Disparo]
    ciclos: int

    def cadena(self, nombre: str, valor: Any) -> list[Disparo]:
        """
        Explicación del "¿cómo?": todas las reglas que participaron, directa o
        indirectamente, en concluir (nombre, valor), en orden de disparo.
        """
        por_regla = {d.regla.id: d for d in self.disparos}
        visitadas: set[str] = set()
        pendientes: list[tuple[str, Any]] = [(nombre, valor)]
        while pendientes:
            n, v = pendientes.pop()
            hecho = self.memoria.obtener(n, v)
            if hecho is None:
                continue
            for origen in hecho.origenes:
                disparo = por_regla.get(origen)
                if disparo is None or origen in visitadas:
                    continue
                visitadas.add(origen)
                pendientes.extend((p.nombre, p.valor) for p in disparo.premisas)
        return [d for d in self.disparos if d.regla.id in visitadas]


class MotorInferencia:
    def __init__(self, reglas: list[Regla], univaluados: set[str] | None = None):
        self.reglas = list(reglas)
        # Atributos que solo admiten un valor: al evaluarlos cuenta únicamente
        # el de mayor certeza (así no se recomiendan acciones contradictorias).
        self.univaluados = set(univaluados or ())

    def inferir(self, hechos_iniciales: dict[str, Any]) -> ResultadoInferencia:
        memoria = MemoriaTrabajo(hechos_iniciales)
        disparadas: set[str] = set()
        disparos: list[Disparo] = []
        ciclo = 0

        while ciclo < MAX_CICLOS:
            agenda = []
            for regla in self.reglas:
                if regla.id in disparadas:
                    continue
                evaluacion = self._evaluar(regla, memoria)
                if evaluacion is not None:
                    agenda.append((regla, evaluacion))
            if not agenda:
                break

            regla, (certeza_premisa, premisas) = min(
                agenda,
                key=lambda item: (
                    -item[0].prioridad,
                    -item[0].especificidad,
                    item[0].orden,
                ),
            )
            ciclo += 1
            disparadas.add(regla.id)
            # Copia de las premisas tal como estaban al disparar.
            premisas_snapshot = [
                Hecho(h.nombre, h.valor, h.certeza, list(h.origenes)) for h in premisas
            ]
            certeza_conclusion = certeza_premisa * regla.certeza
            conclusiones = []
            for conclusion in regla.entonces:
                hecho = memoria.afirmar(
                    conclusion.hecho, conclusion.valor, certeza_conclusion, regla.id
                )
                conclusiones.append(
                    Hecho(
                        hecho.nombre, hecho.valor, hecho.certeza, list(hecho.origenes)
                    )
                )
            disparos.append(
                Disparo(ciclo, regla, certeza_premisa, premisas_snapshot, conclusiones)
            )

        return ResultadoInferencia(memoria=memoria, disparos=disparos, ciclos=ciclo)

    def _evaluar(
        self, regla: Regla, memoria: MemoriaTrabajo
    ) -> tuple[float, list[Hecho]] | None:
        """Devuelve (FC de la premisa, hechos que la sustentan) o None si no aplica."""
        certeza = 1.0
        usados: list[Hecho] = []

        for condicion in regla.todas:
            hecho = self._cumple(condicion, memoria)
            if hecho is None:
                return None
            certeza = min(certeza, hecho.certeza)
            usados.append(hecho)

        if regla.alguna:
            cumplidas = [
                h
                for h in (self._cumple(c, memoria) for c in regla.alguna)
                if h is not None
            ]
            if not cumplidas:
                return None
            mejor = max(cumplidas, key=lambda h: h.certeza)
            certeza = min(certeza, mejor.certeza)
            usados.append(mejor)

        return certeza, usados

    def condicion_cumplida(
        self, condicion: Condicion, memoria: MemoriaTrabajo
    ) -> Hecho | None:
        """Hecho que satisface la condición en `memoria`, o None (para explicar)."""
        return self._cumple(condicion, memoria)

    def _cumple(self, condicion: Condicion, memoria: MemoriaTrabajo) -> Hecho | None:
        """Hecho de mayor certeza que satisface la condición, o None."""
        if condicion.op == "desconocido":
            # Negación por ausencia: el dato no llegó. Es un hecho cierto (FC 1).
            if memoria.mejor(condicion.hecho) is not None:
                return None
            return Hecho(condicion.hecho, VALOR_DESCONOCIDO, 1.0, [ORIGEN_INICIAL])
        if condicion.op == "conocido":
            mejor = memoria.mejor(condicion.hecho)
            if mejor is None or mejor.certeza < condicion.certeza_min:
                return None
            return mejor
        if condicion.hecho in self.univaluados:
            mejor = memoria.mejor(condicion.hecho)
            valores = [mejor] if mejor is not None else []
        else:
            valores = memoria.valores(condicion.hecho)
        candidatos = [
            h
            for h in valores
            if h.certeza >= condicion.certeza_min
            and _comparar(h.valor, condicion.op, condicion.valor)
        ]
        if not candidatos:
            return None
        return max(candidatos, key=lambda h: h.certeza)


def _comparar(actual: Any, op: str, esperado: Any) -> bool:
    try:
        if op == "==":
            return _clave(actual) == _clave(esperado)
        if op == "!=":
            return _clave(actual) != _clave(esperado)
        if op == "en":
            return _clave(actual) in {_clave(v) for v in esperado}
        if op == "no_en":
            return _clave(actual) not in {_clave(v) for v in esperado}
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        if op == ">":
            return actual > esperado
        if op == ">=":
            return actual >= esperado
        if op == "<":
            return actual < esperado
        if op == "<=":
            return actual <= esperado
    except TypeError:
        return False
    return False


def _clave(valor: Any) -> Any:
    """Normaliza valores para comparar: True ≠ 1, y 3 == 3.0."""
    if isinstance(valor, bool):
        return ("bool", valor)
    if isinstance(valor, (int, float)):
        return ("num", float(valor))
    if isinstance(valor, str):
        return ("str", valor.strip().lower())
    return ("otro", repr(valor))


def _acotar(certeza: float) -> float:
    return max(0.0, min(1.0, float(certeza)))


def _fmt(valor: Any) -> str:
    if isinstance(valor, bool):
        return "sí" if valor else "no"
    if isinstance(valor, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in valor) + "]"
    if isinstance(valor, float):
        return f"{valor:g}"
    return str(valor)
