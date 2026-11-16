// TransitGDL — arranque de la interfaz.
import { api } from "./api.js";
import { ArrivalsPane } from "./arrivals.js";
import { toward } from "./format.js";
import { TransitMap } from "./map.js";
import { ChatPane, ExpertPane, OpsPane, RoutesPane } from "./panes.js";
import { initSearch, initSheet, initTabs, toast } from "./ui.js";
import { VehicleLayer } from "./vehicles.js";

const REGION = "tonala_cucei";
const $ = (id) => document.getElementById(id);

const app = {
  catalog: [],
  routesById: new Map(),
  selectedRoute: null, // detalle de la ruta elegida
  patternId: null,
  stopQuery: null, // parada consultada (id o texto) de la tarjeta de llegada
  focus: null, // parada destacada en el mapa
  tm: null,
  vehicles: null,
  currentTab: "arrivals",
  ticket: 0,

  get selectedRouteId() {
    return this.selectedRoute?.route_id;
  },
  toast,
  colorOf(routeId) {
    return this.routesById.get(routeId)?.color || (this.selectedRoute?.route_id === routeId ? this.selectedRoute.color : "#8a96a3");
  },
  liveCount(routeId) {
    return this.vehicles ? this.vehicles.countForRoute(routeId) : 0;
  },

  // ── Navegación ──────────────────────────────────────────────────────────────

  async selectRoute(routeId, { patternId = null, fit = true, render = true } = {}) {
    const mine = ++this.ticket;
    let detail;
    try {
      detail = await api.routeDetail(routeId);
    } catch (error) {
      toast(error.status === 404 ? "No encontré esa ruta." : error.message, "error");
      return null;
    }
    if (mine !== this.ticket && render) return null;
    this.selectedRoute = { ...detail, label: detail.label };
    this.patternId = patternId && detail.paths.some((p) => p.pattern_id === patternId) ? patternId : detail.paths[0]?.pattern_id;
    this.tm.showRoute(detail, this.patternId, { fit });
    this._legend();
    if (render) {
      this.stopQuery = null;
      this.focus = null;
      this.tm.setFocus(null);
      this.vehicles.emphasis = null;
      showTab("arrivals");
      arrivals.route(detail, this.patternId);
      sheet.open(1);
    }
    this._refreshSidePanes();
    return detail;
  },

  setPattern(patternId) {
    this.patternId = patternId;
    this.tm.setActivePattern(patternId);
    this._legend();
    arrivals.route(this.selectedRoute, patternId);
  },

  showRouteStops() {
    if (!this.selectedRoute) return this.clear();
    this.stopQuery = null;
    this.focus = null;
    this.tm.setFocus(null);
    this.vehicles.emphasis = null;
    arrivals.route(this.selectedRoute, this.patternId);
  },

  async openStop(stopId) {
    if (!this.selectedRoute) return;
    await this._loadOptions(this.selectedRoute.route_id, stopId, this.patternId);
  },

  async openRouteAtStop(routeId, stopId, patternId) {
    const detail = await this.selectRoute(routeId, { patternId, fit: false, render: false });
    if (detail) await this._loadOptions(routeId, stopId, patternId);
  },

  /** "231 en CUCEI", "Línea 3 por Juárez": ruta y parada o lugar (igual que core/intents.py). */
  async openIntent(text) {
    const match = /^(?:(?:la|el|ruta|cami[oó]n)\s+)*((?:(?:l[ií]nea|tren|sitren)\s+)?[\p{L}\p{N}_-]+)\s+(?:en|por|hacia|a)\s+(.+?)\s*[?.!]*$/iu.exec(text.trim().replace(/^[¿¡\s]+/, ""));
    if (!match) return this.openPlace(text);
    return this.openRouteAt(match[1], match[2].trim(), text);
  },

  /** Llegadas de una ruta en una parada o lugar. */
  async openRouteAt(route, stop, text = `${route} en ${stop}`) {
    search.setText(text);
    showTab("arrivals");
    arrivals.loading();
    sheet.open(1);
    await this._loadOptions(route, stop, null, { fit: true });
  },

  async _loadOptions(route, stop, preferredPattern, { fit = false } = {}) {
    const mine = ++this.ticket;
    showTab("arrivals");
    try {
      const { options } = await api.options(route, stop);
      if (mine !== this.ticket) return;
      const first = options.find((o) => o.pattern_id === preferredPattern) || options[0];
      if (this.selectedRoute?.route_id !== first.route_id) {
        // Sin encuadrar la ruta completa: la cámara va a la parada.
        await this.selectRoute(first.route_id, { patternId: first.pattern_id, fit: fit && first.stop_lat == null, render: false });
      }
      this.stopQuery = { route, stop };
      arrivals.showOptions(this.selectedRoute, options, first.pattern_id);
      if (first.stop_lat != null) this.tm.flyTo(first.stop_lon, first.stop_lat, 14);
      sheet.open(1);
    } catch (error) {
      if (mine !== this.ticket) return;
      arrivals.error(error.status === 404 ? "Esa ruta no pasa por esa parada o lugar. Prueba con otra parada." : error.message);
    }
  },

  /** Llamado por la tarjeta cuando cambia la opción visible. */
  async onActiveEta(eta) {
    if (eta.route_id && eta.route_id !== this.selectedRoute?.route_id) {
      await this.selectRoute(eta.route_id, { patternId: eta.pattern_id, fit: false, render: false });
    }
    if (eta.pattern_id && eta.pattern_id !== this.patternId) {
      this.patternId = eta.pattern_id;
      this.tm.setActivePattern(eta.pattern_id);
      this._legend();
    }
    this.focus = eta.stop_lat != null ? { lon: eta.stop_lon, lat: eta.stop_lat, color: this.selectedRoute?.color, stop_id: eta.stop_id, name: eta.stop_name } : null;
    this.tm.setFocus(this.focus);
    this.vehicles.emphasis = eta.method === "vehiculo" ? eta.vehicle_id : null;
  },

  async refreshEta() {
    if (!this.stopQuery) return;
    const active = arrivals.options[arrivals.active];
    try {
      const { options } = await api.options(this.stopQuery.route, this.stopQuery.stop);
      if (!this.stopQuery) return;
      arrivals.showOptions(this.selectedRoute, options, active?.pattern_id);
    } catch {
      /* se conserva la última estimación */
    }
  },

  /** Llegadas en un lugar o parada. Con `stopId`, esa parada exacta (hay muchas homónimas). */
  async openPlace(query, { stopId = null } = {}) {
    const mine = ++this.ticket;
    search.setText(query);
    showTab("arrivals");
    arrivals.loading();
    sheet.open(1);
    try {
      const data = await api.arrivals(query, stopId);
      if (mine !== this.ticket) return;
      this.stopQuery = null;
      this.selectedRoute = null;
      this.tm.clearRoute();
      this._legend();
      this.vehicles.emphasis = null;
      this.focus = { lon: data.place.lon, lat: data.place.lat, color: "#f6bd60", name: data.place.name, stop_id: data.place.stop_ids?.[0] };
      this.tm.setFocus(this.focus);
      this.tm.flyTo(data.place.lon, data.place.lat, 15);
      arrivals.showPlace(data.place, data.arrivals);
      this._refreshSidePanes();
    } catch (error) {
      if (mine !== this.ticket) return;
      arrivals.error(error.status === 404 ? "No encontré ese lugar ni una parada con ese nombre." : error.message);
    }
  },

  clear() {
    this.ticket += 1;
    this.selectedRoute = null;
    this.patternId = null;
    this.stopQuery = null;
    this.focus = null;
    this.tm.clearRoute();
    this.tm.setFocus(null);
    this.vehicles.emphasis = null;
    this._legend();
    search.setText("");
    arrivals.home();
    this._refreshSidePanes();
  },

  async setWholeNetwork(on) {
    try {
      const data = on ? await api.network() : await api.bootstrap(REGION);
      this.catalog = data.routes;
      this.routesById = new Map(data.routes.map((r) => [r.route_id, r]));
      this.vehicles.setRoutes(data.routes);
      this.tm.setNetwork(data.shapes);
      toast(on ? `Red completa: ${data.routes.length} rutas.` : "Zona CUCEI · Tonalá.");
    } catch (error) {
      toast(error.message, "error");
    }
  },

  async refreshWeather() {
    try {
      const w = await api.weather();
      const extra = Math.round((w.bus_travel_factor - 1) * 100);
      const text = `${w.label[0].toUpperCase()}${w.label.slice(1)}${w.simulated ? " (simulado)" : ""}${extra > 2 ? ` · camiones +${extra} %` : " · sin retrasos por clima"}`;
      $("chip-weather").innerHTML = `<span class="chip-dot ${extra > 2 ? "chip-dot--warn" : "chip-dot--muted"}"></span><span></span>`;
      $("chip-weather").lastChild.textContent = text;
    } catch {
      $("chip-weather").lastChild.textContent = "Clima no disponible";
    }
  },

  _legend() {
    const legend = $("legend");
    const path = this.selectedRoute?.paths.find((p) => p.pattern_id === this.patternId);
    legend.hidden = !path;
    if (path) {
      legend.style.color = this.selectedRoute.color;
      $("legend-active").textContent = `${this.selectedRoute.short_name} hacia ${path.headsign}`;
    }
  },

  _refreshSidePanes() {
    if (this.currentTab === "expert") expert.render();
    if (this.currentTab === "ops") ops.render();
    if (this.currentTab === "routes") routes.render();
  },
};

// ── Interfaz ──────────────────────────────────────────────────────────────────

const arrivals = new ArrivalsPane($("pane-arrivals"), app);
const routes = new RoutesPane($("pane-routes"), app);
const expert = new ExpertPane($("pane-expert"), app);
const ops = new OpsPane($("pane-ops"), app);
new ChatPane($("pane-chat"), app);

const showTab = initTabs((name) => {
  app.currentTab = name;
  if (name === "routes") routes.render();
  if (name === "expert") expert.render();
  if (name === "ops") ops.render();
});
const sheet = initSheet();
const search = initSearch({
  search: (text) => api.search(text),
  onPick(item) {
    // El servidor ya separó la ruta y la parada: no se vuelve a interpretar el texto.
    if (item.kind === "intent") return app.openRouteAt(item.route, item.stop);
    if (item.kind === "route") {
      search.setText(item.label || item.short_name);
      return app.selectRoute(item.route_id);
    }
    if (item.kind === "place") return app.openPlace(item.name);
    return app.openPlace(item.stop_name, { stopId: item.stop_id });
  },
});

arrivals.home();

// ── Arranque ──────────────────────────────────────────────────────────────────

async function start() {
  let boot;
  try {
    boot = await api.bootstrap(REGION);
  } catch {
    toast("No hay conexión con el servidor de TransitGDL.", "error", 8000);
    return;
  }
  app.catalog = boot.routes;
  app.routesById = new Map(boot.routes.map((r) => [r.route_id, r]));

  const feed = boot.feed || {};
  const year = (feed.feed_end || "").slice(0, 4);
  $("chip-feed").textContent = year ? `Horarios oficiales del AMG · ${year}` : "Horarios oficiales del AMG";
  $("chip-feed").title = feed.expired ? "El último horario publicado ya venció; se usa como patrón semanal." : "";

  try {
    app.tm = await TransitMap.create("map", boot.config?.style_url);
  } catch (error) {
    $("map").innerHTML = `<div class="empty" style="margin:120px 24px"><strong>No se pudo mostrar el mapa</strong><span>Este navegador no admite WebGL2. El buscador y las llegadas siguen funcionando.</span></div>`;
    console.error(error);
    return;
  }
  if (!app.tm.hasBaseMap) toast("No cargó el mapa base; se muestran solo las rutas.", "error", 6000);
  app.tm.setNetwork(boot.shapes);

  app.vehicles = new VehicleLayer(app.tm, $("vehicle-layer"), {
    region: REGION,
    onCount(n) {
      $("chip-live").lastChild.textContent = `${n} unidades en vivo${boot.simulator ? " · simulación" : ""}`;
    },
  });
  app.vehicles.setRoutes(boot.routes);
  app.vehicles.ingest(Date.now(), boot.positions.map((p) => [
    p.vehicle_id, p.route_id, p.pattern_id, p.progress_m, p.lat, p.lon, p.bearing, p.speed_kmh, Date.parse(p.timestamp) || Date.now(),
  ]));
  app.vehicles.connect();

  app.tm.on("route", (routeId) => app.selectRoute(routeId, { fit: false }));
  app.tm.on("stop", (props) => app.openStop(props.stop_id));
  app.tm.on("vehicle", (point) => {
    const hit = app.vehicles.hitTest(point);
    if (!hit) return false;
    const sample = app.vehicles.latest(hit.id);
    const route = app.routesById.get(hit.route);
    app.selectRoute(hit.route, { patternId: sample?.pattern, fit: false }).then((detail) => {
      const path = detail?.paths.find((p) => p.pattern_id === sample?.pattern);
      const speed = sample?.speed != null ? ` · ${Math.round(sample.speed)} km/h` : "";
      toast(`Unidad de la ${route?.label || hit.route}${path ? ` hacia ${path.headsign}` : ""}${speed}`);
    });
    return true;
  });

  $("zoom-in").addEventListener("click", () => app.tm.zoomBy(1));
  $("zoom-out").addEventListener("click", () => app.tm.zoomBy(-1));
  $("locate").addEventListener("click", () => {
    if (!navigator.geolocation) return toast("Este navegador no comparte la ubicación.", "error");
    navigator.geolocation.getCurrentPosition(async (pos) => {
      try {
        const data = await api.nearby(pos.coords.latitude, pos.coords.longitude);
        if (!data.stops.length) return toast("No hay paradas a menos de 800 m.");
        app.openPlace(data.stops[0].stop_name);
      } catch (error) {
        toast(error.message, "error");
      }
    }, () => toast("No se pudo obtener tu ubicación.", "error"));
  });

  app.refreshWeather();
  setInterval(() => app.refreshWeather(), 5 * 60 * 1000);

  // Enlace directo: /?q=231 en CUCEI
  const query = new URLSearchParams(location.search).get("q");
  if (query) app.openIntent(query);
}

start();
window.__tgdlApp = app;
