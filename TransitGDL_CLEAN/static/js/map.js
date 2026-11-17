// Mapa: MapLibre GL JS 6 (módulo ES) con el estilo oscuro de OpenFreeMap.
import * as maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@6.16.0/dist/maplibre-gl.mjs";
import { boundsOf, decodePolyline, makePath, reducedMotion } from "./format.js";

const CENTER = [-103.3, 20.645];
const STYLE_TIMEOUT_MS = 6000;

// Mapa base de respaldo: si las teselas no cargan, las rutas se dibujan sobre fondo liso.
const FALLBACK_STYLE = {
  version: 8,
  sources: {},
  layers: [{ id: "fondo", type: "background", paint: { "background-color": "#0e141b" } }],
};

// Secuencia de guiones que hace "fluir" el trazo en su sentido de circulación.
const FLOW_STEPS = [
  [0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1], [2.5, 4, 0.5], [3, 4, 0],
  [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5], [0, 2, 3, 2], [0, 2.5, 3, 1.5], [0, 3, 3, 1], [0, 3.5, 3, 0.5],
];

const EMPTY = { type: "FeatureCollection", features: [] };

async function loadStyle(url) {
  if (!url) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), STYLE_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function arrowImage() {
  const size = 32;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#0e141b";
  ctx.beginPath();
  ctx.moveTo(23, 16);
  ctx.lineTo(11, 8.5);
  ctx.lineTo(14, 16);
  ctx.lineTo(11, 23.5);
  ctx.closePath();
  ctx.fill();
  return ctx.getImageData(0, 0, size, size);
}

export class TransitMap {
  /** Crea el mapa; si el estilo no responde, usa el fondo liso. */
  static async create(container, styleUrl) {
    const style = await loadStyle(styleUrl);
    const map = new maplibregl.Map({
      container,
      style: style || FALLBACK_STYLE,
      center: CENTER,
      zoom: 12.2,
      minZoom: 9,
      maxZoom: 18,
      maxPitch: 0,
      dragRotate: false,
      pitchWithRotate: false,
      attributionControl: { compact: true },
    });
    map.touchZoomRotate.disableRotation();
    map.keyboard.disableRotation();
    // Basta con el estilo cargado para agregar capas; "load" espera además el
    // primer cuadro dibujado, que no llega mientras la pestaña está oculta.
    if (!map.isStyleLoaded()) {
      await new Promise((resolve) => {
        const done = () => {
          map.off("style.load", done);
          map.off("load", done);
          resolve();
        };
        map.on("style.load", done);
        map.on("load", done);
      });
    }
    return new TransitMap(map, Boolean(style));
  }

  constructor(map, hasBaseMap) {
    this.map = map;
    this.hasBaseMap = hasBaseMap;
    this.patterns = new Map(); // pattern_id → { path, route_id, color, headsign }
    this.selectedRoute = null;
    this.activePattern = null;
    this.focus = null; // parada destacada { lon, lat, color }
    this.userMoved = false;
    this.handlers = { route: () => {}, stop: () => {}, background: () => {}, vehicle: null };
    this._flowStep = 0;
    this._flowTimer = null;
    this._addLayers();
    this._bindEvents();
  }

  // ── Capas ─────────────────────────────────────────────────────────────────

  _addLayers() {
    const map = this.map;
    // Las rutas van encima de las calles del mapa base y debajo de sus nombres:
    // se insertan antes del primer texto que aparece después de la última calle.
    const baseLayers = map.getStyle().layers;
    let lastShape = -1;
    baseLayers.forEach((layer, i) => {
      if (["line", "fill", "fill-extrusion"].includes(layer.type)) lastShape = i;
    });
    const firstLabel = baseLayers.slice(lastShape + 1).find((layer) => layer.type === "symbol")?.id;
    map.addImage("flecha", arrowImage(), { pixelRatio: 2 });

    map.addSource("red", { type: "geojson", data: EMPTY });
    map.addSource("ruta", { type: "geojson", data: EMPTY });
    map.addSource("paradas", { type: "geojson", data: EMPTY });

    const width = (low, high) => ["interpolate", ["linear"], ["zoom"], 10, low, 15, high];
    map.addLayer({
      id: "red-linea",
      type: "line",
      source: "red",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": width(1.3, 3.4), "line-opacity": 0.72 },
    }, firstLabel);
    map.addLayer({
      id: "red-toque",
      type: "line",
      source: "red",
      paint: { "line-color": "#000", "line-width": 14, "line-opacity": 0 },
    }, firstLabel);
    map.addLayer({
      id: "ruta-contraria",
      type: "line",
      source: "ruta",
      filter: ["==", ["get", "role"], "otro"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": width(1.5, 3.5), "line-opacity": 0.6, "line-dasharray": [1, 2.5] },
    }, firstLabel);
    map.addLayer({
      id: "ruta-borde",
      type: "line",
      source: "ruta",
      filter: ["==", ["get", "role"], "activo"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#0e141b", "line-width": width(5, 11) },
    }, firstLabel);
    map.addLayer({
      id: "ruta-linea",
      type: "line",
      source: "ruta",
      filter: ["==", ["get", "role"], "activo"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": width(3, 6.5) },
    }, firstLabel);
    map.addLayer({
      id: "ruta-flujo",
      type: "line",
      source: "ruta",
      filter: ["==", ["get", "role"], "activo"],
      paint: { "line-color": "#fff4dc", "line-opacity": 0.85, "line-width": width(1, 2.2), "line-dasharray": FLOW_STEPS[0] },
    }, firstLabel);
    map.addLayer({
      id: "paradas-punto",
      type: "circle",
      source: "paradas",
      minzoom: 11.5,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 11.5, 2.5, 16, 6],
        "circle-color": "#0e141b",
        "circle-stroke-color": ["get", "color"],
        "circle-stroke-width": 2,
      },
    });
    map.addLayer({
      id: "ruta-flechas",
      type: "symbol",
      source: "ruta",
      filter: ["==", ["get", "role"], "activo"],
      layout: {
        "symbol-placement": "line",
        "symbol-spacing": 110,
        "icon-image": "flecha",
        "icon-size": ["interpolate", ["linear"], ["zoom"], 11, 0.55, 16, 0.9],
        "icon-allow-overlap": true,
        "icon-ignore-placement": true,
        "icon-rotation-alignment": "map",
      },
    });
    map.addLayer({
      id: "paradas-toque",
      type: "circle",
      source: "paradas",
      minzoom: 11.5,
      paint: { "circle-radius": 14, "circle-opacity": 0 },
    });
  }

  _bindEvents() {
    const map = this.map;
    map.on("dragstart", () => { this.userMoved = true; });
    map.on("wheel", () => { this.userMoved = true; });
    map.on("click", (event) => {
      if (this.handlers.vehicle && this.handlers.vehicle(event.point)) return;
      const stop = map.queryRenderedFeatures(event.point, { layers: ["paradas-toque"] })[0];
      if (stop) return this.handlers.stop(stop.properties);
      const route = map.queryRenderedFeatures(event.point, { layers: ["red-toque"] })[0];
      if (route) return this.handlers.route(route.properties.route_id);
      this.handlers.background();
    });
    for (const layer of ["paradas-toque", "red-toque"]) {
      map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
    }
  }

  on(kind, handler) {
    this.handlers[kind] = handler;
  }

  // ── Datos ─────────────────────────────────────────────────────────────────

  /** Registra trazos (polilínea codificada o coordenadas [lon, lat]) para dibujarlos y animar unidades. */
  registerPaths(paths, color) {
    for (const p of paths) {
      if (this.patterns.has(p.pattern_id)) continue;
      const coords = p.polyline ? decodePolyline(p.polyline) : p.coordinates || [];
      if (coords.length < 2) continue;
      this.patterns.set(p.pattern_id, {
        route_id: p.route_id,
        color: color || p.color,
        headsign: p.headsign,
        approximate: p.approximate,
        coords,
        path: makePath(coords, p.length_m),
      });
    }
  }

  setNetwork(shapes) {
    const features = [];
    for (const shape of shapes) {
      this.registerPaths(shape.paths.map((p) => ({ ...p, route_id: shape.route_id })), shape.color);
      for (const p of shape.paths) {
        const known = this.patterns.get(p.pattern_id);
        if (!known) continue;
        features.push({
          type: "Feature",
          properties: { route_id: shape.route_id, color: shape.color },
          geometry: { type: "LineString", coordinates: known.coords },
        });
      }
    }
    this.map.getSource("red").setData({ type: "FeatureCollection", features });
  }

  /** Destaca una ruta: sus dos sentidos, flechas y paradas del sentido activo. */
  showRoute(detail, activePatternId, { fit = true } = {}) {
    this.registerPaths(detail.paths.map((p) => ({ ...p, route_id: detail.route_id })), detail.color);
    this.selectedRoute = detail;
    this.activePattern = activePatternId || detail.paths[0]?.pattern_id;
    this._renderRoute();
    this.map.setPaintProperty("red-linea", "line-opacity", 0.2);
    if (fit) {
      const bounds = boundsOf(detail.paths.map((p) => this.patterns.get(p.pattern_id)?.coords || []));
      if (bounds) this.fit(bounds);
    }
    this._startFlow();
  }

  setActivePattern(patternId) {
    if (!this.selectedRoute) return;
    this.activePattern = patternId;
    this._renderRoute();
  }

  _renderRoute() {
    const detail = this.selectedRoute;
    const features = detail.paths.map((p) => ({
      type: "Feature",
      properties: { role: p.pattern_id === this.activePattern ? "activo" : "otro", color: detail.color },
      geometry: { type: "LineString", coordinates: this.patterns.get(p.pattern_id)?.coords || [] },
    }));
    // El sentido activo se dibuja al final, encima del contrario.
    features.sort((a, b) => (a.properties.role === "activo") - (b.properties.role === "activo"));
    this.map.getSource("ruta").setData({ type: "FeatureCollection", features });
    const stops = (detail.stops || [])
      .filter((s) => s.pattern_id === this.activePattern)
      .map((s) => ({
        type: "Feature",
        properties: { stop_id: s.stop_id, name: s.stop_name, color: detail.color, sequence: s.sequence },
        geometry: { type: "Point", coordinates: [s.lon, s.lat] },
      }));
    this.map.getSource("paradas").setData({ type: "FeatureCollection", features: stops });
  }

  clearRoute() {
    this.selectedRoute = null;
    this.activePattern = null;
    this.map.getSource("ruta").setData(EMPTY);
    this.map.getSource("paradas").setData(EMPTY);
    this.map.setPaintProperty("red-linea", "line-opacity", 0.72);
    this._stopFlow();
  }

  setFocus(focus) {
    this.focus = focus;
  }

  fit(bounds, maxZoom = 14.5) {
    const narrow = this.map.getContainer().clientWidth < 768;
    const padding = narrow
      ? { top: 130, bottom: Math.round(window.innerHeight * 0.48), left: 30, right: 30 }
      : { top: 80, bottom: 60, left: 60, right: 60 };
    this.map.fitBounds(bounds, { padding, maxZoom, duration: reducedMotion() ? 0 : 900 });
  }

  flyTo(lon, lat, zoom = 15) {
    const narrow = this.map.getContainer().clientWidth < 768;
    this.map.flyTo({
      center: [lon, lat],
      zoom: Math.max(this.map.getZoom(), zoom),
      offset: narrow ? [0, -Math.round(window.innerHeight * 0.2)] : [0, 0],
      duration: reducedMotion() ? 0 : 1000,
      essential: false,
    });
  }

  zoomBy(delta) {
    this.map.easeTo({ zoom: this.map.getZoom() + delta, duration: reducedMotion() ? 0 : 250 });
  }

  // ── Animación del trazo ───────────────────────────────────────────────────

  _startFlow() {
    if (this._flowTimer || reducedMotion()) return;
    this._flowTimer = setInterval(() => {
      if (document.hidden) return;
      this._flowStep = (this._flowStep + 1) % FLOW_STEPS.length;
      this.map.setPaintProperty("ruta-flujo", "line-dasharray", FLOW_STEPS[this._flowStep]);
    }, 70);
  }

  _stopFlow() {
    clearInterval(this._flowTimer);
    this._flowTimer = null;
  }
}
