# TransitGDL

**Intelligent transit operations platform for schedule-based public transportation systems**

TransitGDL is a modular platform designed to improve operational decision-making for public transportation agencies, especially in regions with limited real-time infrastructure.

Instead of relying entirely on live GPS feeds, the system combines:

- **GTFS static schedules**
- **Weather conditions**
- **Incident reports**
- **Passenger demand heuristics**
- **Feedback-driven prediction adjustments**

The result is a resilient ETA and demand estimation engine that can operate in infrastructure-constrained environments.

---

## Key Features

- Schedule-first ETA prediction engine
- Demand analysis with contextual weighting
- Weather-aware operational adjustments
- Feedback loop for continuous model correction
- REST API for external integrations
- WhatsApp bot interface for user interaction
- Dockerized deployment support
- Automated tests for core flows

---

## Project Structure

```bash
TransitGDL/
├── api/                # FastAPI application and routes
├── bot/                # Messaging integrations (e.g. WhatsApp)
├── core/               # Business logic engines
├── data/               # GTFS and demo datasets
├── scripts/            # Utility scripts
├── tests/              # Test suite
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## Installation

> **Requiere Python 3.11 o 3.12.** Usa siempre un virtualenv — el `requirements.txt` tiene versiones pin exactas que pueden colisionar con paquetes del sistema.

### 1. Clonar el repositorio

```bash
git clone https://github.com/your-username/TransitGDL.git
cd TransitGDL
```

### 2. Crear y activar virtualenv

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (CMD)
.venv\Scripts\activate.bat
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. (Opcional) Herramientas de desarrollo

```bash
pip install -r requirements-dev.txt
```

### 5. (Opcional) Lint y formato

```bash
black api core tests
ruff check api core tests
```

---

## Arrancar la aplicación

### 1. Configurar entorno

```bash
cp .env.example .env
# Para desarrollo local deja USE_MEMORY_CACHE=true (no requiere Redis).
# Rellena TWILIO_* solo si usas el bot de WhatsApp.
```

### 2. Iniciar el servidor

```bash
# Windows (PowerShell)
.\start.ps1

# Windows (CMD — doble clic o terminal)
start.bat

# Linux / macOS
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Dashboard: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
Docs API: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Prueba rápida:

```bash
curl "http://127.0.0.1:8000/eta/?route=231&stop=CUCEI"
curl "http://127.0.0.1:8000/eta/?route=MC-L1&stop=Artes%20Pl%C3%A1sticas"
```

Las rutas aceptan la clave oficial (`T15`) o el número anterior (`231`); la parada puede ser un nombre de parada o un lugar conocido (`CUCEI`, `Minerva`, `Tonalá Centro`).

---

## Datos

La app usa el GTFS oficial del Área Metropolitana de Guadalajara publicado por el Gobierno de Jalisco (247 rutas con servicio: Mi Transporte, Mi Macro, Mi Tren y SiTren). El archivo `data/gtfs_amg.zip` viene en el repositorio.

```bash
# Volver a descargarlo (verifica la huella SHA-256)
python scripts/download_gtfs.py

# Reconstruir la base de transporte y ver el reporte de validación
python scripts/build_transit_db.py
```

Al arrancar, la app convierte el zip en `data/transit.db` (SQLite) si esa base falta o si el zip cambió. Ahí quedan las rutas con sus dos sentidos, la distancia de cada parada sobre el trazo, las frecuencias por tipo de día y los alias de números antiguos. El feed trae trazos invertidos o que no pasan por sus paradas; el constructor los corrige y lo reporta.

`data/landmarks.json` contiene 44 puntos de interés con coordenadas de OpenStreetMap (se regenera con `python scripts/build_landmarks.py`).

El feed vigente más reciente es de 2024. Su calendario ya venció, así que se usa como patrón semanal y los días festivos operan con horario de domingo.

---

## Running with Docker

```bash
docker-compose up --build
```

---

## Running Tests

```bash
pytest
```

---

## Example Workflow

1. Ensure `data/gtfs_amg.zip` exists (included in the repo, or run `python scripts/download_gtfs.py`).
2. Start the API service (`uvicorn api.main:app --reload` or `.\run.ps1`).
3. Open the dashboard and pick a demo scenario, or query `/eta/?route=231&stop=CUCEI`.
4. Submit feedback via `/feedback/` using the `prediction_id` from an ETA response.
5. Observe adaptive prediction updates in later ETA calls.

---

## Sistema experto

`core/expert/` contiene un sistema experto basado en reglas que diagnostica la operación de una ruta y recomienda acciones.

| Componente | Archivo | Qué hace |
|---|---|---|
| Base de conocimiento | `core/expert/rules.yaml` | 48 reglas SI–ENTONCES con prioridad y factor de certeza. Se editan sin tocar Python. |
| Base de hechos | `core/expert/facts.py` | Reúne clima, calendario, flota, frecuencia, incidencias y error aprendido de la ruta. |
| Motor de inferencia | `core/expert/inference.py` | Encadenamiento hacia adelante, resolución de conflictos por prioridad y especificidad, factores de certeza estilo MYCIN. |
| Explicación | `core/expert/system.py` | Cada acción incluye la cadena de reglas que la justifica. |

Uso:

```bash
# Diagnóstico de una ruta con los hechos actuales
curl "http://127.0.0.1:8000/expert/diagnose?route_id=231"

# Escenario "¿qué pasaría si…?"
curl "http://127.0.0.1:8000/expert/diagnose?route_id=231&clima=thunderstorm&hora=8"

# Ver la base de conocimiento
curl "http://127.0.0.1:8000/expert/rules"
```

`POST /expert/infer` recibe hechos arbitrarios (`{"hechos": {"clima": "rain", "hora": 8}}`). En el dashboard está la pestaña **Experto**, y en el asistente el comando `diagnostico 231`.

Para agregar una regla, edita `rules.yaml`; al arrancar se valida que los hechos y acciones que usa estén declarados.

---

## Development Notes

- Keep business logic isolated inside `core/`
- Avoid placing executable scripts in project root
- Use `scripts/` for utilities and one-off tasks
- Maintain test coverage for every new engine feature

---

## Roadmap

- Historical demand model integration
- Advanced anomaly detection
- Multi-city deployment support
- Fleet optimization recommendations

---

## Continuous Integration

This repository includes a GitHub Actions workflow to verify the project automatically on push and pull request to `main`.

The workflow installs dependencies, runs the test suite, and validates Python syntax.

Workflow path:

```text
.github/workflows/python-app.yml
```

---

## License

MIT License


