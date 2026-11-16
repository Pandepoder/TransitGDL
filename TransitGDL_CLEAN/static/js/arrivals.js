// Pestaña "Llegadas": ruta elegida, sentido, tarjeta de llegada y llegadas por lugar.
import { api } from "./api.js";
import {
  CONFIDENCE_TEXT,
  distanceText,
  esc,
  methodBadge,
  minutesText,
  rangeText,
  routeChip,
  routeSubtitle,
  toward,
} from "./format.js";

const FEATURED = [
  { query: "231 en CUCEI", label: "231 en CUCEI" },
  { query: "646 en Tonalá Centro", label: "646 en Tonalá" },
  { query: "L3 en Cucei", label: "Tren L3 en Cucei" },
];
const PLACES = ["CUCEI", "Tonalá Centro", "Glorieta Minerva", "Central Nueva"];
const RATINGS = [
  { rating: 1, label: "Antes" },
  { rating: 2, label: "A tiempo" },
  { rating: 4, label: "Tarde" },
];

export class ArrivalsPane {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.options = [];
    this.active = 0;
    this.loadedAt = 0;
    this.rated = new Set();
    root.addEventListener("click", (event) => this._onClick(event));
    setInterval(() => this._tick(), 5000);
  }

  // ── Vistas ────────────────────────────────────────────────────────────────

  home() {
    this.options = [];
    this.root.innerHTML = `
      <div class="empty">
        <strong>¿Cuándo pasa tu camión?</strong>
        <span>Busca una ruta por su clave nueva o su número de siempre (T15 o 231), una parada o un lugar como CUCEI. También puedes tocar una línea en el mapa.</span>
      </div>
      <div>
        <h2 class="section-title">Pruébalo</h2>
        <div class="btn-row" style="margin-top:8px">
          ${FEATURED.map((f) => `<button type="button" class="btn btn--sm" data-intent="${esc(f.query)}">${esc(f.label)}</button>`).join("")}
        </div>
      </div>
      <div>
        <h2 class="section-title">Llegadas cerca de</h2>
        <div class="btn-row" style="margin-top:8px">
          ${PLACES.map((p) => `<button type="button" class="btn btn--sm" data-place="${esc(p)}">${esc(p)}</button>`).join("")}
        </div>
      </div>`;
  }

  loading() {
    this.root.innerHTML = `<div class="card"><div class="skeleton" style="width:40%"></div><div class="skeleton" style="height:56px;width:30%"></div><div class="skeleton"></div><div class="skeleton" style="width:70%"></div></div>`;
  }

  error(message) {
    this.root.innerHTML = `<div class="empty"><strong>No se pudo calcular</strong><span>${esc(message)}</span><button type="button" class="btn" data-home>Volver al inicio</button></div>`;
  }

  /** Ruta elegida sin parada: sentidos y paradas del sentido activo. */
  route(detail, patternId) {
    this.options = [];
    const path = detail.paths.find((p) => p.pattern_id === patternId) || detail.paths[0];
    const stops = (detail.stops || []).filter((s) => s.pattern_id === path.pattern_id);
    this.root.innerHTML = `
      ${this._routeHead(detail)}
      ${this._directionToggle(detail.paths.map((p) => ({ id: p.pattern_id, label: toward(p.headsign) })), path.pattern_id)}
      ${path.approximate ? `<p class="small muted">El trazo oficial de este sentido no es confiable; se dibuja uniendo sus paradas.</p>` : ""}
      <div>
        <h2 class="section-title">Elige tu parada <span class="muted">· ${stops.length} paradas, ${path.length_km} km</span></h2>
        <div class="list" style="margin-top:6px">
          ${stops.map((s) => `
            <button type="button" class="row" data-stop="${esc(s.stop_id)}">
              <span class="row-main"><span class="row-title">${esc(s.stop_name)}</span></span>
              <span class="small muted">${s.sequence + 1}</span>
            </button>`).join("")}
        </div>
      </div>`;
  }

  /** Opciones de una ruta en una parada (una por sentido o ramal). */
  showOptions(detail, options, preferredPattern) {
    this.detail = detail;
    this.options = options;
    const preferred = options.findIndex((o) => o.pattern_id === preferredPattern);
    this.active = preferred >= 0 ? preferred : 0;
    this.loadedAt = Date.now();
    this._renderOptions();
  }

  _renderOptions() {
    const eta = this.options[this.active];
    if (!eta) return;
    const detail = this.detail;
    const toggle = this.options.length > 1
      ? this._directionToggle(this.options.map((o, i) => ({ id: String(i), label: `${toward(o.headsign)}${o.route_id !== detail?.route_id ? ` · ${o.route_number}` : ""}` })), String(this.active), "data-option")
      : "";
    const others = this.options.filter((_, i) => i !== this.active);
    this.root.innerHTML = `
      ${detail ? this._routeHead(detail, eta) : ""}
      ${toggle}
      ${this._card(eta)}
      ${others.length ? `
        <div>
          <h2 class="section-title">También en esta parada</h2>
          <div class="list" style="margin-top:6px">
            ${others.map((o) => `
              <button type="button" class="row" data-option="${this.options.indexOf(o)}">
                ${routeChip({ color: detail?.color, short_name: o.route_number })}
                <span class="row-main"><span class="row-title">${esc(toward(o.headsign))}</span><span class="row-sub">${esc(o.stop_name)}</span></span>
                <span class="row-end">${o.service_day_offset ? "mañana" : minutesText(o.estimated_minutes)}</span>
              </button>`).join("")}
          </div>
        </div>` : ""}
      <button type="button" class="btn" data-back-route>Ver todas las paradas de la ruta</button>
      <p class="small muted">Calculado sobre la ruta con el horario oficial y la posición de la unidad. El rango se ajusta con las calificaciones de quienes viajan.</p>`;
    this.app.onActiveEta(eta);
  }

  /** Llegadas de todas las rutas en un lugar o parada. */
  showPlace(place, arrivals) {
    this.options = [];
    this.place = place;
    this.root.innerHTML = `
      <div class="route-head">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#f6bd60" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 21s-7-6.2-7-11a7 7 0 1 1 14 0c0 4.8-7 11-7 11z"></path><circle cx="12" cy="10" r="2.5"></circle></svg>
        <span class="row-main"><span class="row-title">${esc(place.name)}</span><span class="row-sub">${place.kind === "lugar" ? "Paradas a menos de 600 m" : "Parada"} · ${arrivals.length} próximas llegadas</span></span>
      </div>
      ${arrivals.length ? `
        <div class="list">
          ${arrivals.map((a, i) => `
            <button type="button" class="row" data-arrival="${i}">
              ${routeChip({ color: this.app.colorOf(a.route_id), short_name: a.route_number })}
              <span class="row-main">
                <span class="row-title">${esc(toward(a.headsign))}</span>
                <span class="row-sub">${esc(a.stop_name)}${a.walk_meters != null ? ` · ${distanceText(a.walk_meters)} a pie` : ""}${a.route_label && a.route_label !== a.route_number ? ` · ${esc(a.route_label.replace(a.route_number, "").trim())}` : ""}</span>
              </span>
              <span class="row-end">${a.service_day_offset ? "mañana" : minutesText(a.estimated_minutes)}${a.method === "vehiculo" ? ' <span class="badge badge--live" style="height:20px;padding:0 6px"><span class="sr-only">en vivo</span></span>' : ""}</span>
            </button>`).join("")}
        </div>` : `<div class="empty"><span>No hay salidas programadas cerca de aquí en las próximas horas.</span></div>`}`;
    this.arrivals = arrivals;
  }

  // ── Piezas ────────────────────────────────────────────────────────────────

  _routeHead(detail) {
    const sub = routeSubtitle(detail);
    return `
      <div class="route-head">
        ${routeChip(detail, "route-chip--lg")}
        <span class="row-main">
          <span class="row-title">${esc(detail.branch_name || detail.long_name || detail.short_name)}</span>
          <span class="row-sub">${esc(sub)}</span>
        </span>
        <button type="button" class="icon-btn" data-clear aria-label="Cerrar ruta"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"></path></svg></button>
      </div>`;
  }

  _directionToggle(items, activeId, attr = "data-pattern") {
    if (items.length < 2) return "";
    return `
      <div class="dir-toggle" role="group" aria-label="Sentido">
        ${items.map((it) => `<button type="button" class="btn" ${attr}="${esc(it.id)}" aria-pressed="${it.id === activeId}">${esc(it.label)}</button>`).join("")}
      </div>`;
  }

  _card(eta) {
    const minutes = this._liveMinutes(eta);
    const lo = eta.window_start_minutes;
    const hi = eta.window_end_minutes;
    const scale = Math.max(15, Math.ceil((hi ?? minutes) / 5) * 5 + 5);
    const pct = (v) => `${Math.min(100, Math.max(0, (v / scale) * 100)).toFixed(1)}%`;
    const tomorrow = eta.service_day_offset > 0;
    const facts = [];
    if (eta.method === "vehiculo" && eta.vehicle_distance_m != null) facts.push(["Unidad a", distanceText(eta.vehicle_distance_m)]);
    if (eta.average_headway_minutes) facts.push(["Pasa cada", `${Math.round(eta.average_headway_minutes)} min`]);
    if (eta.tracked_vehicle_minutes != null) facts.push(["Unidad con GPS", `en ${Math.round(eta.tracked_vehicle_minutes)} min`]);
    if (eta.next_arrivals_minutes?.length) facts.push(["Después", eta.next_arrivals_minutes.slice(0, 2).map((m) => Math.round(m)).join(" · ") + " min"]);
    else if (eta.next_departures?.length > 1) facts.push(["Después", eta.next_departures.slice(1, 3).map((d) => d.departure_time.slice(0, 5)).join(" · ")]);
    facts.push(["Clima", eta.weather_label || "—"]);
    const rated = this.rated.has(eta.prediction_id);

    return `
      <section class="card" aria-label="Próxima llegada">
        <div class="arrival-top">
          <span class="arrival-stop">
            <span class="label">Parada</span>
            <span class="name">${esc(eta.stop_name)}</span>
            ${eta.place_name ? `<span class="small muted">A ${distanceText(eta.walk_meters)} de ${esc(eta.place_name)}</span>` : ""}
          </span>
          ${methodBadge(eta)}
        </div>
        <div class="countdown" aria-live="polite">
          ${tomorrow
            ? `<span class="num is-now">Mañana</span><span class="unit">${esc(eta.next_departures?.[0]?.departure_time?.slice(0, 5) || "")}</span>`
            : minutes < 1
              ? `<span class="num is-now" data-num>Llegando</span>`
              : `<span class="num" data-num>${Math.round(minutes)}</span><span class="unit">min</span>`}
        </div>
        ${!tomorrow && lo != null && hi != null ? `
          <div class="range">
            <div class="range-track" aria-hidden="true">
              <div class="range-band" style="left:${pct(lo)};width:calc(${pct(hi)} - ${pct(lo)})"></div>
              <div class="range-mark" style="left:${pct(eta.estimated_minutes)}"></div>
            </div>
            <div class="range-labels"><span>${esc(eta.method === "frecuencia" ? `Espera media · ${rangeText(lo, hi).toLowerCase()}` : rangeText(lo, hi))}</span><span>${esc(CONFIDENCE_TEXT[eta.reliability_label] || "")}</span></div>
          </div>` : ""}
        <div class="facts">${facts.slice(0, 3).map(([k, v]) => `<span class="fact"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></span>`).join("")}</div>
        ${eta.tracked_vehicle_minutes != null ? `<p class="small muted" style="margin:0">La unidad con GPS más próxima está lejos; por la frecuencia, es probable que antes pase otra que no reporta su posición.</p>` : ""}
        ${(eta.alerts || []).map((a) => `<div class="alert">Aviso: ${esc(a)}</div>`).join("")}
        ${Math.abs(eta.learned_correction_minutes) >= 1 ? `<p class="small muted">Incluye ${eta.learned_correction_minutes > 0 ? "+" : ""}${Math.round(eta.learned_correction_minutes)} min aprendidos de calificaciones anteriores.</p>` : ""}
        ${rated
          ? `<span class="feedback-done">Gracias, tu calificación ajusta las siguientes estimaciones.</span>`
          : `<div class="btn-row"><span class="small" style="color:var(--text-2)">¿Llegó a tiempo?</span>${RATINGS.map((r) => `<button type="button" class="btn btn--sm" data-rate="${r.rating}">${r.label}</button>`).join("")}</div>`}
      </section>`;
  }

  _liveMinutes(eta) {
    const elapsed = (Date.now() - this.loadedAt) / 60000;
    return Math.max(0, eta.estimated_minutes - elapsed);
  }

  /** Cuenta regresiva local entre recálculos del servidor. */
  _tick() {
    const eta = this.options[this.active];
    if (!eta || eta.service_day_offset) return;
    if (Date.now() - this.loadedAt > 30000) {
      this.app.refreshEta();
      return;
    }
    const el = this.root.querySelector("[data-num]");
    const minutes = this._liveMinutes(eta);
    if (el && minutes >= 1) {
      const text = String(Math.round(minutes));
      if (el.textContent !== text) {
        el.textContent = text;
        el.animate?.([{ transform: "translateY(-6px)", opacity: 0.4 }, { transform: "none", opacity: 1 }], { duration: 300, easing: "ease-out" });
      }
    }
  }

  // ── Eventos ───────────────────────────────────────────────────────────────

  async _onClick(event) {
    const t = event.target.closest("button");
    if (!t) return;
    if (t.dataset.intent) return this.app.openIntent(t.dataset.intent);
    if (t.dataset.place) return this.app.openPlace(t.dataset.place);
    if (t.hasAttribute("data-home")) return this.app.clear();
    if (t.hasAttribute("data-clear")) return this.app.clear();
    if (t.hasAttribute("data-back-route")) return this.app.showRouteStops();
    if (t.dataset.pattern) return this.app.setPattern(t.dataset.pattern);
    if (t.dataset.stop) return this.app.openStop(t.dataset.stop);
    if (t.dataset.arrival) {
      const a = this.arrivals[Number(t.dataset.arrival)];
      return this.app.openRouteAtStop(a.route_id, a.stop_id, a.pattern_id);
    }
    if (t.dataset.option) {
      this.active = Number(t.dataset.option);
      this.loadedAt = this.loadedAt || Date.now();
      return this._renderOptions();
    }
    if (t.dataset.rate) {
      const eta = this.options[this.active];
      try {
        await api.feedback(eta.prediction_id, Number(t.dataset.rate));
        this.rated.add(eta.prediction_id);
        this._renderOptions();
      } catch (error) {
        this.app.toast(error.status === 404 ? "Esta estimación ya no se puede calificar." : error.message, "error");
      }
    }
  }
}
