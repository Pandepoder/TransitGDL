"""Pruebas de los arreglos de saneamiento: festivos, feed vencido y caché GPS."""

from __future__ import annotations

import asyncio
import zipfile
from datetime import date, datetime

from core.calendar_utils import es_festivo
from core.config import CONFIG
from core.demand_analyzer import DemandAnalyzer
from core.gps_cache import GPSCache
from core.gtfs_loader import GTFSLoader
from core.timezone_utils import GDL_TZ

# ── Festivos ─────────────────────────────────────────────────────────────────


def test_festivo_nacional_se_detecta():
    # Día de la Revolución 2026 (se recorre al lunes 16 de noviembre).
    assert es_festivo(date(2026, 11, 16)) is True
    assert es_festivo(date(2026, 9, 16)) is True
    assert es_festivo(date(2026, 11, 17)) is False


def test_festivo_acepta_datetime_y_alimenta_la_demanda():
    momento = datetime(2026, 12, 25, 8, 0, tzinfo=GDL_TZ)
    assert es_festivo(momento) is True
    assert DemandAnalyzer._is_holiday(momento) is True


# ── Feed vencido ─────────────────────────────────────────────────────────────


def _zip_gtfs_vencido(path) -> str:
    """GTFS mínimo cuyo calendario terminó en 2022."""
    archivos = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
        "R1,1,Linea vencida,3\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        "A,Terminal A,20.60,-103.30\nB,Terminal B,20.61,-103.31\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,"
        "saturday,sunday,start_date,end_date\n"
        "TODOS,1,1,1,1,1,1,1,20200101,20221231\n",
        "trips.txt": "route_id,service_id,trip_id\n"
        + "".join(f"R1,TODOS,T{h:02d}\n" for h in range(24)),
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        + "".join(
            f"T{h:02d},{h:02d}:00:00,{h:02d}:00:00,A,1\n"
            f"T{h:02d},{h:02d}:10:00,{h:02d}:10:00,B,2\n"
            for h in range(24)
        ),
    }
    with zipfile.ZipFile(path, "w") as archivo:
        for nombre, contenido in archivos.items():
            archivo.writestr(nombre, contenido)
    return str(path)


def test_feed_vencido_sigue_dando_salidas(tmp_path):
    loader = GTFSLoader(_zip_gtfs_vencido(tmp_path / "vencido.zip")).load()

    assert loader._feed_last_date == date(2022, 12, 31)
    assert loader.get_next_departures("R1", "A", 0, limit=3)


def test_un_servicio_vigente_no_apaga_a_los_vencidos(tmp_path):
    """Regresión: añadir un servicio con fechas vigentes dejaba sin horario al resto."""
    loader = GTFSLoader(_zip_gtfs_vencido(tmp_path / "vencido.zip")).load()
    loader._service_calendars["NUEVO"] = {
        "service_id": "NUEVO",
        "monday": "1",
        "tuesday": "1",
        "wednesday": "1",
        "thursday": "1",
        "friday": "1",
        "saturday": "1",
        "sunday": "1",
        "start_date": "20250101",
        "end_date": "20301231",
    }

    hoy = datetime.now(GDL_TZ).date()
    activos = loader._active_service_ids_for_date(hoy)

    assert {"TODOS", "NUEVO"} <= activos
    assert loader.get_next_departures("R1", "A", 0, limit=3)


def test_servicio_que_aun_no_empieza_no_se_activa(tmp_path):
    loader = GTFSLoader(_zip_gtfs_vencido(tmp_path / "vencido.zip")).load()
    loader._service_calendars["FUTURO"] = {
        "service_id": "FUTURO",
        "monday": "1",
        "tuesday": "1",
        "wednesday": "1",
        "thursday": "1",
        "friday": "1",
        "saturday": "1",
        "sunday": "1",
        "start_date": "20990101",
        "end_date": "20991231",
    }

    activos = loader._active_service_ids_for_date(datetime.now(GDL_TZ).date())

    assert "FUTURO" not in activos


# ── Caché GPS ────────────────────────────────────────────────────────────────


def test_modo_memoria_por_configuracion_no_reintenta_redis(monkeypatch):
    monkeypatch.setattr(CONFIG, "use_memory_cache", True)
    cache = GPSCache("redis://127.0.0.1:1/0")
    cache._use_memory = True

    assert asyncio.run(cache._try_reconnect_redis()) is False
    # No hubo intento de conexión: la marca de último fallo sigue intacta.
    assert cache._last_redis_failure == 0.0
