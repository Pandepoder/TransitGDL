# TransitGDL

Plataforma de información y operación para el transporte público del Área Metropolitana de Guadalajara. Calcula cuándo llega tu camión aunque no todas las unidades reporten GPS, diagnostica la operación de cada ruta con un sistema experto y lo muestra en un mapa en vivo.

Combina:

- el **horario oficial** (GTFS) de 247 rutas: Mi Transporte, Mi Macro, Mi Tren y SiTren;
- la **posición de las unidades** que sí reportan GPS (en la demostración, un simulador);
- el **clima** (Open-Meteo), las **incidencias** reportadas y las **calificaciones** de quienes viajan.

---

## Arrancar

> Requiere Python 3.11 o superior. Usa un entorno virtual: `requirements.txt` fija versiones exactas.

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows (PowerShell)
source .venv/bin/activate           # Linux / macOS
pip install -r requirements.txt
cp .env.example .env                # USE_MEMORY_CACHE=true: no hace falta Redis
```

```bash
.\start.ps1                                          # Windows
uvicorn api.main:app --host 127.0.0.1 --port 8000    # Linux / macOS
```

- Interfaz: <http://127.0.0.1:8000/> (también acepta `/?q=231 en CUCEI`)
- Documentación de la API: <http://127.0.0.1:8000/docs>

El primer arranque convierte el GTFS en `data/transit.db` (unos 3 s); los siguientes tardan menos de medio segundo.

Con Docker: `docker-compose up --build`.

---

## Datos

| Archivo | Qué es | Cómo se actualiza |
|---|---|---|
| `data/gtfs_amg.zip` | GTFS oficial del Gobierno de Jalisco (datos de marzo de 2024, el más reciente publicado) | `python scripts/download_gtfs.py` (verifica la huella SHA-256; `--accept-new` para adoptar un feed nuevo) |
| `data/transit.db` | Base derivada del GTFS (no se versiona) | Se reconstruye sola si falta o si el zip cambió; a mano: `python scripts/build_transit_db.py`, también con la aplicación abierta (la app la copia a memoria al arrancar) |
| `data/landmarks.json` | 44 puntos de interés (CUCEI, Minerva, Central Nueva…) con coordenadas de OpenStreetMap | `python scripts/build_landmarks.py` |

La construcción de la base:

- separa cada ruta en **patrones** (ruta + sentido + secuencia de paradas) y guarda la distancia de cada parada sobre el trazo. Si el trazo pasa dos veces cerca de una parada (vueltas en "gota", ida y regreso por la misma calle), elige la pasada que respeta el orden del recorrido;
- **corrige defectos del feed**:
  - trazos invertidos o intercambiados entre sentidos, y trazos que no pasan por sus paradas (se arma un trazo híbrido);
  - andenes duplicados: el Tren Ligero listaba cada estación dos veces, así que la Línea 1 mostraba 40 estaciones en vez de 20 y la Línea 2, 20 en vez de 10;
  - recorridos duplicados: SiTren trae cada línea dos veces con otros identificadores de parada; se fusionan;
  - tiempos imposibles: tramos demasiado rápidos o esperas de más de 10 min a menos de 2 km/h (C129, C38-V2), y recorridos completos de 0.1 a 45 km/h. Se reconstruyen con una velocidad creíble;
  - tipos de día sin frecuencia;
  - destinos escritos en mayúsculas ("AUDITORIO");
- **marca lo que no puede corregir** y la interfaz lo avisa: recorridos con solo sus dos terminales (22), tiempos reconstruidos (13), trazo aproximado (7) y paradas repetidas en exceso (C130);
- crea los **alias**: los números de siempre llevan a las claves oficiales (231 → T15, 55 → T11B, 615 → C38/C39, 646 → T04A-1 y T04A-2), y los nombres de sistema también ("Línea 3" y "tren ligero" → Tren L3; "macrobús" → Mi Macro Calzada; "sitren 2"). Las 29 rutas del catálogo oficial sin viajes (como la 231C, T15-C06) llevan a su troncal, con un aviso;
- indexa paradas por texto (sin acentos) y por cercanía.

El reporte de validación aparece al correr `build_transit_db.py`. Como el calendario del feed venció, se usa como patrón semanal, y los días festivos operan con horario de domingo.

**Verificación externa.** 189 recorridos de 93 rutas están también en OpenStreetMap. Casi todos (178) se importaron de los mismos datos del gobierno, así que coinciden parada por parada: eso confirma que la construcción no altera los datos, no que la calle real coincida. Las estaciones de Mi Macro Calzada y Periférico, mapeadas a mano en OpenStreetMap, quedan a menos de 120 m de las del feed (mediana de 4 a 22 m).

---

## Cómo se calcula la llegada

Para una ruta, un sentido y una parada (`core/eta_engine.py`):

1. **Unidad en vivo.** Se toma la unidad del mismo sentido que aún no pasa la parada y llega primero. El tiempo sale del perfil del horario entre la unidad y la parada, no de una línea recta. Las posiciones sin sentido declarado se ubican sobre el trazo y se descartan si van al revés.
2. **Frecuencia.** Si la ruta opera "cada H minutos" y no hay unidad a la vista, la espera media es H/2 (rango de 0 a H). También se usa cuando la unidad con GPS más próxima tarda más de 1.5 frecuencias: con rastreo parcial, lo probable es que antes pase otra que no reporta.
3. **Horario.** Si el servicio tiene hora exacta o aún no empieza, se usa la próxima pasada.

Sobre eso:

- **Clima:** un factor que alarga el recorrido según la intensidad de la lluvia; al tren le afecta una fracción.
- **Incidencias:** suman minutos según su gravedad, solo si están entre la unidad y la parada, y caducan solas.
- **Aprendizaje:** las calificaciones corrigen el sesgo por ruta, sentido, parada y franja del día. Una parada con pocas calificaciones se apoya en el promedio de su ruta. Las de estimaciones por frecuencia se guardan pero no se aprenden: con "pasa cada H min" la espera real es aleatoria entre 0 y H.
- **Rango y confianza:** el rango de un horario crece con la anticipación (±2 min más un 5 % de la espera). La confianza no supera la calidad de los datos oficiales: es baja con tiempos reconstruidos y a lo más media si el recorrido solo trae sus terminales o tiene un trazo aproximado.

Todos los parámetros están en `core/params.yaml`, para ajustarlos sin tocar código.

### Evaluación

`python scripts/evaluate_eta.py --salida docs/evaluacion_eta.md` compara el método anterior (línea recta, sin sentido) con el nuevo usando el simulador:

| Escenario | Método | Error medio | Llegada dentro del rango |
|---|---|---|---|
| Despejado | Anterior | 4.6 min | 61 % |
| Despejado | Nuevo | 1.1 min | 80 % |
| Lluvia fuerte | Anterior | 5.9 min | 47 % |
| Lluvia fuerte | Nuevo | 1.1 min | 82 % |

El simulador sigue el perfil del horario, así que favorece al método nuevo; la comparación mide sobre todo los errores de geometría y de sentido del método anterior. Detalle en [docs/evaluacion_eta.md](docs/evaluacion_eta.md).

---

## Flota y sistema experto

**Flota necesaria** (`core/fleet.py`): tiempo de ciclo (ida, vuelta y descansos, con clima) ÷ frecuencia programada. `/fleet/recommend` usa la misma decisión que el sistema experto; con `vehicles` (el conteo del operador) compara esa cifra con la flota en servicio.

**Unidades con GPS ≠ flota en servicio.** Las posiciones en vivo suelen ser una muestra (rastreo parcial, o el simulador en la demostración), así que no se comparan con la flota necesaria. Solo cuentan como flota si `GPS_COBERTURA_COMPLETA=true`. Con `GPS_FEED_ACTIVO=true`, una ruta sin reportes en hora pico pide revisar sus equipos; sin fuente GPS no se alerta.

**Sistema experto** (`core/expert/`): diagnostica la operación de una ruta y explica cada recomendación con la cadena de reglas que la produjo.

| Componente | Archivo | Qué hace |
|---|---|---|
| Base de conocimiento | `core/expert/rules.yaml` | 64 reglas SI–ENTONCES en seis estratos, con prioridad y factor de certeza; se editan sin tocar Python y se validan al cargar |
| Base de hechos | `core/expert/facts.py` | Clima, calendario, frecuencia, flota necesaria, flota en servicio (si se conoce), GPS, incidencias, error aprendido, si el horario ya está reducido y si la ruta tiene servicio a esa hora |
| Motor de inferencia | `core/expert/inference.py` | Encadenamiento hacia adelante, resolución de conflictos por prioridad y especificidad, factores de certeza, datos ausentes (`desconocido`) |
| Explicación | `core/expert/system.py` | Cómo: la cadena de reglas detrás de cada acción y su motivo de fondo. ¿Por qué no?: qué condición faltó para recomendar otra acción |

Criterios del diagnóstico:

- **Una sola hora pico.** Si la frecuencia de la ruta cambia durante el día, manda la de la ruta; la hora pico de la ciudad solo se usa para rutas de frecuencia fija.
- **La demanda se mide frente a lo programado.** En la hora pico de una ruta que ya sube su frecuencia, la demanda está cubierta; solo la lluvia, la saturación o un pico que el horario no atiende piden más unidades. Al revés igual: si el horario ya baja el servicio en fin de semana, festivo o de noche, no se recortan más unidades.
- **Una sola decisión de flota** (agregar o retirar). Sin conteo real recomienda lo que pide el horario; con conteo, la cifra coincide con la decisión: si la flota actual ya cumple, pasa a "mantener" y se explica.
- **Sin servicio programado** solo se informa; no se recomiendan unidades, GPS ni ajustes al ETA.
- **Rutas alternas:** las que cubren al menos el 25 % de las paradas de la ruta, de la que más comparte a la que menos.

En un barrido de 20,000 escenarios aleatorios se usan las 64 reglas y no aparece ninguna recomendación contradictoria.

```bash
curl "http://127.0.0.1:8000/expert/diagnose?route_id=231"
curl "http://127.0.0.1:8000/expert/diagnose?route_id=231&clima=thunderstorm&hora=8&dia=domingo"   # escenario
curl "http://127.0.0.1:8000/expert/diagnose?route_id=231&vehicles=30"   # con la flota en servicio
curl "http://127.0.0.1:8000/expert/diagnose?route_id=231&por_que_no=agregar_unidades"
curl "http://127.0.0.1:8000/expert/rules"
```

---

## Interfaz

`static/` contiene el marcado (`index.html`), los estilos (`css/app.css`) y módulos ES sin paso de compilación (`js/`).

- **Mapa:** MapLibre GL JS 6.16 con el estilo oscuro de OpenFreeMap (sin clave). `MAP_STYLE_URL` permite otro proveedor. Si el estilo no carga, las rutas se dibujan sobre fondo liso.
- **Animación:** las unidades se deslizan sobre la calle entre actualizaciones (canal en vivo `/map/ws`) y apuntan a su rumbo. La ruta elegida muestra flechas y un trazo que fluye en su sentido, y la parada elegida late. Con "reducir movimiento" activado en el sistema, todo queda quieto.
- **Paneles:** buscador único (clave, número de siempre, nombre de sistema, parada o lugar) y pestañas Llegadas, Rutas, Experto, Operación y Asistente. En móvil el panel es una hoja inferior con tres alturas. Con varias paradas del mismo nombre, cada resultado abre la suya. Las esperas de más de una hora muestran la hora de llegada.
- **Peso:** 34 KB de código propio y 27 KB de datos iniciales, comprimidos. MapLibre (unos 440 KB) llega aparte desde el CDN.
- **Asistente:** el mismo bot de WhatsApp (`bot/whatsapp_handler.py`), también en `/chat/message`. Entiende las mismas consultas que el buscador ("231 en CUCEI", "Línea 3 por Juárez"; `core/intents.py`).

---

## API principal

| Ruta | Para qué |
|---|---|
| `GET /eta/?route=231&stop=CUCEI` | Mejor llegada (las otras opciones van en `other_options`) |
| `GET /eta/options?route=231&stop=CUCEI` | Una llegada por sentido o ramal |
| `GET /eta/arrivals?place=CUCEI` | Próximas llegadas de todas las rutas en un lugar (`stop_id=` para una parada exacta) |
| `GET /search?q=231 en CUCEI` | Rutas, lugares y paradas en una búsqueda |
| `GET /map/bootstrap` | Rutas, trazos (polilíneas codificadas) y unidades para el mapa |
| `WS /map/ws?compact=true` | Posiciones en vivo |
| `POST /feedback/` | Calificar una predicción |
| `GET /fleet/recommend?route_id=231&vehicles=20` | Flota necesaria; con `vehicles`, contra la flota en servicio |
| `GET /expert/diagnose?route_id=231` | Diagnóstico del sistema experto |

---

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest -q
black --check api core tests scripts
ruff check api core tests scripts bot
```

Las pruebas usan `tests/fixtures/gtfs_mini.zip`, un recorte del GTFS oficial con siete rutas reales (`python scripts/make_test_fixture.py` lo regenera). No hacen llamadas de red. La integración continua (`.github/workflows/python-app.yml`, en la raíz del repositorio, que es donde GitHub la busca) corre todo lo anterior dentro de `TransitGDL_CLEAN/` en cada push y pull request a `main`, y además construye la base desde el feed oficial.

---

## Estructura

```text
api/        API (FastAPI) y rutas HTTP/WebSocket
bot/        Asistente conversacional (WhatsApp y web)
core/       Lógica: base de transporte, ETA, clima, incidencias, aprendizaje, flota, sistema experto, simulador
data/       GTFS oficial, puntos de interés y bases locales
docs/       Evaluación del ETA
scripts/    Descarga de datos, construcción de la base, evaluación
static/     Interfaz web
tests/      Pruebas
```

---

## Limitaciones conocidas

- **Datos de 2024:** el feed oficial más reciente no incluye los cambios de 2025 y 2026 (por ejemplo, las rutas 8 y 9 de Mi Transporte). La interfaz muestra el año de los datos; actualizarlo es correr `download_gtfs.py` cuando se publique otro.
- **Defectos del feed sin arreglo posible:** 22 recorridos traen solo sus dos terminales y C130 repite muchas paradas. La aplicación los marca, pero no puede inventar las paradas que faltan.
- **Simulador de demostración:** las unidades en vivo son simuladas. Hay hasta tres por sentido en cada ruta de la zona CUCEI/Tonalá, y circulan las 24 horas aunque no haya servicio programado. Son una muestra: el sistema experto no las cuenta como flota.
- **Hora pico de los camiones:** el GTFS da a cada ruta de camión una sola frecuencia para todo el día, así que su hora pico se toma del horario general. En tren y Macrobús sale de sus propias frecuencias.
- **Paradas homónimas en el asistente:** muchas calles se repiten ("Morelos" tiene 6 paradas distintas). El asistente lista las rutas de cada una para que el usuario escriba la ruta junto con la parada.
- **Mapa base:** OpenFreeMap es un servicio comunitario sin garantía de disponibilidad. MapLibre 6 requiere WebGL2.

---

## Fuentes y licencias

- Horarios: GTFS del Gobierno de Jalisco, vía el portal de datos abiertos del estado (espejo en Mobility Database, registro mdb-2366).
- Mapa y puntos de interés: © colaboradores de OpenStreetMap (ODbL); teselas de OpenFreeMap y OpenMapTiles.
- Clima: Open-Meteo.
- Código: licencia MIT.
