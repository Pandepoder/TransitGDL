"""Parámetros de cálculo, leídos de `params.yaml` (ver ese archivo para su significado)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).with_name("params.yaml")


def load_params(path: str | Path | None = None) -> dict[str, Any]:
    with Path(path or PARAMS_PATH).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("params.yaml debe ser un mapa de secciones.")
    return data


PARAMS: dict[str, Any] = load_params()

CLIMA: dict[str, Any] = PARAMS["clima"]
INCIDENCIAS: dict[str, Any] = PARAMS["incidencias"]
ETA: dict[str, Any] = PARAMS["eta"]
APRENDIZAJE: dict[str, Any] = PARAMS["aprendizaje"]
FLOTA: dict[str, Any] = PARAMS["flota"]
