// Pestañas Rutas, Experto, Operación y Asistente.
import { api } from "./api.js";
import { esc, routeChip } from "./format.js";

const GROUPS = [
  ["destacadas", "Las de siempre: 231 · 55 · 615 · 646"],
  ["metro", "Tren ligero"],
  ["macro", "Macrobús"],
  ["sitren", "SiTren"],
  ["troncal", "Troncales"],
  ["complementaria", "Complementarias"],
  ["feeder", "Alimentadoras"],
  ["bus", "Otras"],
];
const FEATURED = ["231", "55", "615", "646"];

// ── Rutas ────────────────────────────────────────────────────────────────────

export class RoutesPane {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.filter = "";
    this.showAll = false;
    root.addEventListener("input", (event) => {
      if (event.target.id === "route-filter") {
        this.filter = event.target.value;
        this._renderList();
      }
    });
    root.addEventListener("change", async (event) => {
      if (event.target.id === "route-all") {
        this.showAll = event.target.checked;
        await this.app.setWholeNetwork(this.showAll);
        this.render();
      }
    });
    root.addEventListener("click", (event) => {
      const row = event.target.closest("[data-route]");
      if (row) this.app.selectRoute(row.dataset.route);
    });
  }

  render() {
    this.root.innerHTML = `
      <label class="field">Filtrar rutas
        <input id="route-filter" type="search" value="${esc(this.filter)}" placeholder="T15, 231, Macro…" autocomplete="off">
      </label>
      <label class="btn-row small" style="color:var(--text-2)"><input id="route-all" type="checkbox" ${this.showAll ? "checked" : ""}> Mostrar toda la red del área metropolitana</label>
      <div id="route-list"></div>`;
    this._renderList();
  }

  _renderList() {
    const box = this.root.querySelector("#route-list");
    if (!box) return;
    const f = this.filter.trim().toLowerCase();
    const routes = this.app.catalog.filter((r) => !f
      || r.short_name.toLowerCase().includes(f)
      || (r.legacy_number || "").startsWith(f)
      || (r.long_name || "").toLowerCase().includes(f));
    const groups = new Map(GROUPS.map(([key]) => [key, []]));
    for (const r of routes) {
      const key = FEATURED.includes(r.legacy_number) ? "destacadas" : groups.has(r.layer) ? r.layer : "bus";
      groups.get(key).push(r);
    }
    groups.get("destacadas").sort((a, b) => FEATURED.indexOf(a.legacy_number) - FEATURED.indexOf(b.legacy_number));
    const html = GROUPS.map(([key, title]) => {
      const list = groups.get(key);
      if (!list.length) return "";
      return `<h2 class="section-title" style="margin:12px 0 4px">${title}</h2>
        <div class="list">${list.map((r) => `
          <button type="button" class="row" data-route="${esc(r.route_id)}" ${this.app.selectedRouteId === r.route_id ? 'aria-current="true"' : ""}>
            ${routeChip(r)}
            <span class="row-main">
              <span class="row-title">${esc(r.branch_name || r.long_name)}</span>
              <span class="row-sub">${esc([r.legacy_number ? `Antes ${r.legacy_number}` : "", r.directions.map((d) => d.headsign).join(" ⇄ ")].filter(Boolean).join(" · "))}</span>
              ${r.directions.some((d) => d.notes?.length) ? `<span class="row-note" title="${esc([...new Set(r.directions.flatMap((d) => d.notes || []))].join(" "))}">Datos oficiales incompletos</span>` : ""}
            </span>
          </button>`).join("")}</div>`;
    }).join("");
    box.innerHTML = html || `<p class="muted">Ninguna ruta coincide.</p>`;
  }
}

// ── Experto ──────────────────────────────────────────────────────────────────

const LEVEL = { critica: "crítica", alta: "alta", normal: "normal", baja: "baja", critico: "crítico", degradado: "degradado" };
const VALUE_TEXT = {
  true: "sí", false: "no", clear: "despejado", cloudy: "nublado", rain: "lluvia", thunderstorm: "tormenta", fog: "niebla", snow: "nieve",
  low: "baja", medium: "media", high: "alta", critical: "crítica", pico_matutino: "pico matutino", pico_mediodia: "pico de mediodía",
  pico_vespertino: "pico vespertino", valle: "valle", nocturno: "nocturno", agregar: "agregar", reducir: "retirar",
};

/** Valor de un hecho en palabras ("sí", "tormenta", "12"). */
function valueText(value) {
  if (value === null || value === undefined) return "—";
  const key = String(value);
  if (key in VALUE_TEXT) return VALUE_TEXT[key];
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  return key.replace(/_/g, " ");
}

const pctText = (c) => (c == null ? "" : `${Math.round(c * 100)} %`);
// Una regla con `si` y `si_alguna` sobre el mismo hecho lo usa dos veces: se muestra una.
const uniqueFacts = (facts) => facts.filter((f, i) => facts.findIndex((g) => g.hecho === f.hecho && g.valor === f.valor) === i);
const factText = (f) => `${esc(f.hecho)} = ${esc(valueText(f.valor))}${f.certeza != null && f.certeza < 1 ? ` <span class="muted">(${pctText(f.certeza)})</span>` : ""}`;

/** Flota del diagnóstico: solo compara contra la flota en servicio si hay conteo real. */
function fleetText(d) {
  const rec = d.vehiculos_recomendados;
  if (d.vehiculos_actuales != null) {
    return rec != null && rec !== d.vehiculos_actuales ? `Flota: ${d.vehiculos_actuales} en servicio → ${rec} recomendadas` : "";
  }
  if (d.unidades_necesarias) {
    const extra = rec != null && rec !== d.unidades_necesarias ? `; con la demanda actual, ${rec}` : "";
    return `Según el horario hacen falta ${d.unidades_necesarias} unidades${extra}`;
  }
  return "";
}

export class ExpertPane {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.result = null;
    this.scenario = null; // clima, hora y día del último diagnóstico
    this.kb = null; // base de conocimiento (descripciones de hechos y acciones)
    root.addEventListener("click", (event) => this._onClick(event));
  }

  async _knowledge() {
    if (!this.kb) {
      try {
        this.kb = await api.rules();
      } catch {
        this.kb = { hechos_iniciales: {}, acciones: {} };
      }
    }
    return this.kb;
  }

  render() {
    const route = this.app.selectedRoute;
    const hours = Array.from({ length: 24 }, (_, h) => `<option value="${h}">${String(h).padStart(2, "0")}:00</option>`).join("");
    this.root.innerHTML = `
      <p class="small muted" style="margin:0">El sistema experto revisa clima, calendario, frecuencia, flota, incidencias y calificaciones, y explica cada recomendación con sus reglas.</p>
      ${route
        ? `<div class="route-head">${routeChip(route)}<span class="row-main"><span class="row-title">${esc(route.label || route.short_name)}</span><span class="row-sub">${esc(route.long_name)}</span></span></div>`
        : `<div class="empty"><span>Elige una ruta (en el mapa, el buscador o la pestaña Rutas) para diagnosticarla.</span></div>`}
      <div class="grid-2" style="grid-template-columns:repeat(auto-fit,minmax(104px,1fr))">
        <label class="field">Clima
          <select id="ex-clima"><option value="">El actual</option><option value="clear">Despejado</option><option value="cloudy">Nublado</option><option value="rain">Lluvia</option><option value="thunderstorm">Tormenta</option><option value="fog">Niebla</option></select>
        </label>
        <label class="field">Hora
          <select id="ex-hora"><option value="">La actual</option>${hours}</select>
        </label>
        <label class="field">Día
          <select id="ex-dia"><option value="">Hoy</option><option value="laboral">Entre semana</option><option value="sabado">Sábado</option><option value="domingo">Domingo</option><option value="festivo">Festivo</option></select>
        </label>
      </div>
      <div class="btn-row">
        <button type="button" class="btn btn--primary" data-run ${route ? "" : "disabled"}>Diagnosticar</button>
        <button type="button" class="btn" data-rules>Ver las reglas</button>
      </div>
      <div id="ex-result"></div>`;
  }

  async _onClick(event) {
    const t = event.target.closest("button");
    if (!t) return;
    const box = this.root.querySelector("#ex-result");
    if (t.hasAttribute("data-run")) {
      box.innerHTML = `<div class="card"><div class="skeleton"></div><div class="skeleton" style="width:60%"></div></div>`;
      this.scenario = {
        route: this.app.selectedRoute.route_id,
        clima: this.root.querySelector("#ex-clima").value,
        hora: this.root.querySelector("#ex-hora").value,
        dia: this.root.querySelector("#ex-dia").value,
      };
      try {
        const [d, kb] = await Promise.all([
          api.diagnose(this.scenario.route, this.scenario.clima, this.scenario.hora, this.scenario.dia),
          this._knowledge(),
        ]);
        this.result = d;
        box.innerHTML = this._diagnosis(d, kb);
      } catch (error) {
        box.innerHTML = `<p class="alert">${esc(error.message)}</p>`;
      }
    } else if (t.hasAttribute("data-whynot")) {
      const out = this.root.querySelector("#ex-whynot-out");
      const action = this.root.querySelector("#ex-whynot").value;
      if (!this.scenario || !action) return;
      out.innerHTML = `<div class="skeleton"></div>`;
      try {
        const s = this.scenario;
        const d = await api.diagnose(s.route, s.clima, s.hora, s.dia, action);
        out.innerHTML = `<ul class="why-not-list">${d.por_que_no.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>`;
      } catch (error) {
        out.innerHTML = `<p class="alert">${esc(error.message)}</p>`;
      }
    } else if (t.hasAttribute("data-rules")) {
      try {
        const kb = await api.rules();
        box.innerHTML = `<div class="rules"><h2 class="section-title">Base de conocimiento · ${kb.total_reglas} reglas</h2><ol>${kb.reglas.map((r) => `<li><b>${esc(r.id)} · ${esc(r.nombre)}</b><br>${esc(r.texto)}<br><span class="muted">${esc(r.explicacion)}</span></li>`).join("")}</ol></div>`;
      } catch (error) {
        box.innerHTML = `<p class="alert">${esc(error.message)}</p>`;
      }
    }
  }

  _diagnosis(d, kb) {
    const pct = (c) => (c == null ? "" : ` · ${pctText(c)}`);
    const scenario = Object.keys(d.escenario || {}).length ? `<span class="badge">Escenario simulado</span>` : "";
    const fleet = [fleetText(d), d.nota_flota]
      .filter(Boolean)
      .map((text) => `<p class="small" style="margin:0;color:var(--text-2)">${esc(text)}</p>`)
      .join("");
    return `
      <section class="card">
        <div class="level-tags">
          <span class="level level--${esc(d.nivel_demanda)}">Demanda ${esc(LEVEL[d.nivel_demanda] || d.nivel_demanda)}${pct(d.certeza_demanda)}</span>
          <span class="level level--${esc(d.estado_servicio)}">Servicio ${esc(LEVEL[d.estado_servicio] || d.estado_servicio)}${pct(d.certeza_servicio)}</span>
          ${scenario}
        </div>
        ${fleet}
        ${d.acciones.length ? d.acciones.map((a) => `
          <div class="action">
            <b>${esc(a.descripcion)}</b><span class="small muted">Certeza ${pctText(a.certeza)}</span>
            ${a.motivo ? `<span class="small" style="color:var(--text-2)">${esc(a.motivo)}</span>` : ""}
            <details><summary>¿Por qué?</summary><ol>${a.porque.map((p) => `<li>${esc(p)}</li>`).join("")}</ol></details>
          </div>`).join("") : `<p style="margin:0">Sin acciones: la ruta opera con normalidad.</p>`}
        ${this._whyNot(d, kb)}
        ${this._facts(d, kb)}
        ${this._trace(d)}
      </section>`;
  }

  /** "¿Por qué no?": las acciones de la base que este diagnóstico no recomendó. */
  _whyNot(d, kb) {
    const recommended = new Set(d.acciones.map((a) => a.id));
    const others = Object.entries(kb.acciones || {}).filter(([id]) => !recommended.has(id));
    if (!others.length) return "";
    return `
      <div class="why-not">
        <label class="field">¿Por qué no se recomendó…?
          <select id="ex-whynot">${others.map(([id, text]) => `<option value="${esc(id)}">${esc(text)}</option>`).join("")}</select>
        </label>
        <button type="button" class="btn btn--sm" data-whynot>Explicar</button>
        <div id="ex-whynot-out" class="small"></div>
      </div>`;
  }

  /** Base de hechos: los datos que entraron al motor y lo que concluyó. */
  _facts(d, kb) {
    const descriptions = kb.hechos_iniciales || {};
    const inputs = Object.entries(d.hechos_iniciales || {});
    const derived = d.hechos_derivados || [];
    return `
      <details class="trace small">
        <summary>Base de hechos · ${inputs.length} datos de entrada, ${derived.length} concluidos</summary>
        <h3 class="trace-title">Datos de entrada</h3>
        <dl class="facts-list">${inputs.map(([name, value]) => `
          <div><dt>${esc((descriptions[name] || name).split(":")[0])}</dt><dd>${esc(valueText(value))}</dd></div>`).join("")}
        </dl>
        <h3 class="trace-title">Hechos concluidos</h3>
        <ul class="derived-list">${derived.map((f) => `
          <li>${factText(f)} <span class="muted">· ${esc((f.origenes || []).join(", "))}</span></li>`).join("")}
        </ul>
      </details>`;
  }

  /** Traza de inferencia: una regla disparada por ciclo, con sus premisas y conclusiones. */
  _trace(d) {
    const chain = new Set(d.acciones[0]?.reglas || []);
    const firings = d.reglas_disparadas || [];
    return `
      <details class="trace small">
        <summary>Traza de inferencia · ${firings.length} reglas en ${d.ciclos ?? firings.length} ciclos${chain.size ? " · resaltada la cadena de la primera acción" : ""}</summary>
        <ol class="trace-list">${firings.map((r) => `
          <li class="${chain.has(r.regla) ? "is-chain" : ""}">
            <span class="trace-head">Ciclo ${esc(r.ciclo)} · <b>${esc(r.regla)}</b> ${esc(r.nombre)} <span class="muted">· prioridad ${esc(r.prioridad)}</span></span>
            <span class="trace-if">SI ${r.premisas.length ? uniqueFacts(r.premisas).map(factText).join(" Y ") : "—"}</span>
            <span class="trace-then">→ ${r.conclusiones.map(factText).join(" · ")}</span>
          </li>`).join("")}
        </ol>
      </details>`;
  }
}

// ── Operación ────────────────────────────────────────────────────────────────

const ACTION_TEXT = {
  add_vehicles: "Conviene reforzar la ruta",
  reduce_vehicles: "Se pueden retirar unidades y reasignarlas",
  keep: "La flota alcanza para la frecuencia programada",
  alert: "Alerta operativa: la demanda es crítica",
  monitor: "Sin datos suficientes para evaluar la flota",
};
const SEVERITY = [["low", "Baja"], ["medium", "Media"], ["high", "Alta"], ["critical", "Crítica"]];

export class OpsPane {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    root.addEventListener("click", (event) => this._onClick(event));
    root.addEventListener("submit", (event) => this._onSubmit(event));
  }

  async render() {
    const route = this.app.selectedRoute;
    this.root.innerHTML = `
      <section class="card">
        <h2 class="section-title">Clima</h2>
        <p id="ops-weather" style="margin:0">…</p>
        <div class="btn-row">
          <button type="button" class="btn btn--sm" data-weather="rain">Simular lluvia</button>
          <button type="button" class="btn btn--sm" data-weather="thunderstorm">Simular tormenta</button>
          <button type="button" class="btn btn--sm" data-weather="">Usar el clima real</button>
        </div>
      </section>
      <section class="card" id="ops-fleet">
        <h2 class="section-title">Flota de la ruta</h2>
        ${route ? `<div class="skeleton"></div>` : `<p class="muted" style="margin:0">Elige una ruta para ver cuántas unidades necesita.</p>`}
      </section>
      <section class="card">
        <h2 class="section-title">Reportar incidencia</h2>
        <form id="ops-incident" style="display:flex;flex-direction:column;gap:10px">
          <label class="field">Ruta
            <select name="route" required>${this.app.catalog.map((r) => `<option value="${esc(r.route_id)}" ${route?.route_id === r.route_id ? "selected" : ""}>${esc(r.label || r.short_name)}</option>`).join("")}</select>
          </label>
          <label class="field">Qué pasa
            <textarea name="description" minlength="5" maxlength="500" required placeholder="Choque en Av. Revolución, un carril cerrado"></textarea>
          </label>
          <label class="field">Gravedad
            <select name="severity">${SEVERITY.map(([v, l]) => `<option value="${v}" ${v === "medium" ? "selected" : ""}>${l}</option>`).join("")}</select>
          </label>
          <label class="btn-row small" style="color:var(--text-2)"><input type="checkbox" name="here" ${this.app.focus ? "" : "disabled"}> Ubicarla en la parada elegida${this.app.focus ? ` (${esc(this.app.focus.name || "")})` : ""}</label>
          <button type="submit" class="btn btn--primary">Reportar</button>
        </form>
      </section>
      <section class="card" id="ops-incidents"><h2 class="section-title">Incidencias activas</h2><div class="skeleton"></div></section>`;
    this._weather();
    this._fleet();
    this._incidents();
  }

  async _weather() {
    try {
      const w = await api.weather();
      const extra = Math.round((w.bus_travel_factor - 1) * 100);
      this.root.querySelector("#ops-weather").innerHTML = `${esc(w.label[0].toUpperCase() + w.label.slice(1))}${w.simulated ? " (simulado)" : ""}. ${extra > 0 ? `Los camiones tardan alrededor de ${extra} % más; el tren, ${Math.round((w.train_travel_factor - 1) * 100)} %.` : "Sin efecto en los tiempos."}`;
    } catch {
      /* sin datos de clima */
    }
  }

  /**
   * Flota de la ruta. Las unidades del mapa son una muestra (las que reportan
   * GPS), así que solo se compara contra la flota si el operador la escribe.
   */
  async _fleet(inService = null) {
    const route = this.app.selectedRoute;
    const box = this.root.querySelector("#ops-fleet");
    if (!route || !box) return;
    const live = this.app.liveCount(route.route_id);
    try {
      const f = await api.fleet(route.route_id, inService);
      box.innerHTML = `
        <h2 class="section-title">Flota de ${esc(route.label || route.short_name)}</h2>
        <div class="facts">
          <span class="fact"><span class="k">${inService == null ? "Recomendadas" : "En servicio → recomendadas"}</span><span class="v">${inService == null ? f.recommended_vehicles : `${inService} → ${f.recommended_vehicles}`}</span></span>
          <span class="fact"><span class="k">Con GPS en el mapa</span><span class="v">${live}</span></span>
          <span class="fact"><span class="k">Pasa cada</span><span class="v">${f.headway_minutes ? `${f.headway_minutes} min` : "—"}</span></span>
        </div>
        <p style="margin:0">${esc(ACTION_TEXT[f.action] || "")}</p>
        <p class="small muted" style="margin:0">${esc(f.rationale)}.</p>
        <form id="ops-fleet-count" class="btn-row">
          <label class="field" style="flex:1">Unidades en servicio (opcional)
            <input name="count" type="number" min="0" max="500" inputmode="numeric" value="${inService ?? ""}" placeholder="Conteo del operador">
          </label>
          <button type="submit" class="btn btn--sm">Comparar</button>
        </form>`;
    } catch (error) {
      box.innerHTML = `<p class="alert">${esc(error.message)}</p>`;
    }
  }

  async _incidents() {
    const box = this.root.querySelector("#ops-incidents");
    try {
      const data = await api.incidents(this.app.selectedRoute?.route_id);
      box.innerHTML = `<h2 class="section-title">Incidencias activas${this.app.selectedRoute ? " en la ruta" : ""}</h2>
        ${data.items.length ? `<div class="list">${data.items.map((i) => `
          <div class="row">
            <span class="row-main"><span class="row-title">${esc(i.description)}</span>
            <span class="row-sub">${esc(SEVERITY.find(([v]) => v === i.severity)?.[1] || i.severity)} · ${esc(i.affected_routes.join(", "))}</span></span>
            <button type="button" class="btn btn--sm" data-resolve="${esc(i.incident_id)}">Resolver</button>
          </div>`).join("")}</div>` : `<p class="muted" style="margin:0">No hay incidencias activas.</p>`}`;
    } catch {
      box.innerHTML = `<p class="muted">No se pudieron cargar las incidencias.</p>`;
    }
  }

  async _onClick(event) {
    const t = event.target.closest("button");
    if (!t) return;
    if (t.dataset.weather !== undefined) {
      try {
        if (t.dataset.weather) await api.setWeather(t.dataset.weather);
        else await api.clearWeather();
        this.app.toast(t.dataset.weather ? "Clima simulado: los tiempos ya lo consideran." : "Se usa el clima real.");
        this.app.refreshWeather();
        this.app.refreshEta();
        this._weather();
        this._fleet();
      } catch (error) {
        this.app.toast(error.message, "error");
      }
    }
    if (t.dataset.resolve) {
      await api.resolveIncident(t.dataset.resolve);
      this._incidents();
      this.app.refreshEta();
    }
  }

  async _onSubmit(event) {
    event.preventDefault();
    const form = event.target;
    const data = new FormData(form);
    if (form.id === "ops-fleet-count") {
      const raw = String(data.get("count") || "").trim();
      return this._fleet(raw === "" ? null : Math.max(0, Math.round(Number(raw))));
    }
    const body = {
      description: String(data.get("description") || "").trim(),
      affected_routes: [String(data.get("route"))],
      severity: String(data.get("severity")),
      stop_ids: data.get("here") && this.app.focus?.stop_id ? [this.app.focus.stop_id] : [],
    };
    try {
      const result = await api.reportIncident(body);
      form.reset();
      const alt = result.alternate_routes?.[0]?.alternate_description;
      this.app.toast(alt ? `Incidencia registrada. ${alt}` : "Incidencia registrada.", "info", 5000);
      this._incidents();
      this.app.refreshEta();
    } catch (error) {
      this.app.toast(error.message, "error");
    }
  }
}

// ── Asistente ────────────────────────────────────────────────────────────────

const SUGGESTIONS = ["¿Cuánto falta la 231 en CUCEI?", "¿Qué rutas pasan por Minerva?", "diagnostico 646", "¿Hay retrasos por lluvia?"];

export class ChatPane {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.session = null;
    try {
      this.session = localStorage.getItem("tgdl_chat_session");
    } catch {
      this.session = null;
    }
    root.innerHTML = `
      <div class="chat">
        <div class="chat-log" id="chat-log" aria-live="polite"></div>
        <div class="suggestions">${SUGGESTIONS.map((s) => `<button type="button" class="btn btn--sm" data-say="${esc(s)}">${esc(s)}</button>`).join("")}</div>
        <form class="chat-form" id="chat-form">
          <label class="sr-only" for="chat-input">Mensaje para el asistente</label>
          <input id="chat-input" autocomplete="off" placeholder="Escribe tu pregunta…" maxlength="500">
          <button type="submit" class="btn btn--primary">Enviar</button>
        </form>
      </div>`;
    this.log = root.querySelector("#chat-log");
    this._add("bot", "Hola. Soy el mismo asistente que responde por WhatsApp. Pregúntame cuánto falta tu ruta, qué rutas pasan por un lugar o pide un diagnóstico.");
    root.querySelector("#chat-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = root.querySelector("#chat-input");
      this.send(input.value);
      input.value = "";
    });
    root.addEventListener("click", (event) => {
      const t = event.target.closest("[data-say], [data-show]");
      if (!t) return;
      if (t.dataset.say) this.send(t.dataset.say);
      if (t.dataset.show) this.app.openIntent(t.dataset.show);
    });
  }

  _add(who, text, extra = "") {
    const el = document.createElement("div");
    el.className = `msg msg--${who}`;
    // Formato del bot: *negritas* y saltos de línea.
    el.innerHTML = esc(text).replace(/\*([^*\n]+)\*/g, "<b>$1</b>").replace(/\n/g, "<br>") + extra;
    this.log.appendChild(el);
    this.log.scrollTop = this.log.scrollHeight;
  }

  async send(text) {
    const message = text.trim();
    if (!message) return;
    this._add("user", message);
    try {
      const d = await api.chat(message, this.session);
      this.session = d.session_id;
      try {
        localStorage.setItem("tgdl_chat_session", d.session_id);
      } catch {
        /* sin almacenamiento local */
      }
      // Si la respuesta es un tiempo de llegada, se ofrece verlo en el mapa.
      const route = /^\*Ruta ([\w-]+)/.exec(d.reply);
      const stop = / en \*([^*\n]+)\*/.exec(d.reply);
      const link = route && stop ? `<br><button type="button" class="btn btn--sm" style="margin-top:8px" data-show="${esc(`${route[1]} en ${stop[1]}`)}">Ver en el mapa</button>` : "";
      this._add("bot", d.reply, link);
    } catch {
      this._add("bot", "No pude conectar con el asistente. Intenta de nuevo en un momento.");
    }
  }
}

