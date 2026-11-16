// Unidades en vivo sobre el mapa.
//
// Las posiciones llegan cada ~2 s por WebSocket. Para que el movimiento sea
// continuo, cada unidad se dibuja unos segundos "en el pasado", interpolando
// entre las dos muestras que rodean ese instante. Si la muestra trae el avance
// sobre el trazo (metros), la unidad se desliza por la calle y gira con ella;
// si no, se interpola en línea recta.
import { api, liveSocketUrl } from "./api.js";
import { pointAt, reducedMotion } from "./format.js";

const RENDER_DELAY_MS = 3200;
const MAX_SAMPLES = 4;
const STALE_MS = 120000;
const HIT_RADIUS = 16;

export class VehicleLayer {
  constructor(transitMap, canvas, { region, onCount } = {}) {
    this.tm = transitMap;
    this.map = transitMap.map;
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.region = region;
    this.onCount = onCount || (() => {});
    this.vehicles = new Map(); // id → { route, samples: [] }
    this.drawn = []; // posiciones en pantalla del último cuadro, para tocar unidades
    this.routeInfo = new Map(); // route_id → { color, short_name }
    this.emphasis = null; // vehicle_id resaltado (el del ETA)
    this.clockOffset = 0;
    this.pendingPatterns = new Set();
    this.missingTimer = null;
    this.stats = { fps: 0, frames: 0, last: performance.now(), drawn: 0 };
    window.__tgdl = this.stats;
    this._resize = this._resize.bind(this);
    this._frame = this._frame.bind(this);
    new ResizeObserver(this._resize).observe(canvas.parentElement);
    this._resize();
    requestAnimationFrame(this._frame);
  }

  setRoutes(routes) {
    for (const r of routes) this.routeInfo.set(r.route_id, { color: r.color, short: r.short_name });
  }

  setRegion(region) {
    this.region = region;
    this.vehicles.clear();
    this.connect();
  }

  // ── Datos ─────────────────────────────────────────────────────────────────

  connect() {
    clearTimeout(this._retry);
    clearInterval(this._poll);
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
    }
    let opened = false;
    try {
      this.socket = new WebSocket(liveSocketUrl(this.region));
    } catch {
      this._startPolling();
      return;
    }
    this.socket.onopen = () => { opened = true; };
    this.socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "positions") this.ingest(message.t, message.v);
    };
    this.socket.onclose = () => {
      // Sin WebSocket (proxy, red): se consulta cada pocos segundos.
      if (!opened) this._startPolling();
      else this._retry = setTimeout(() => this.connect(), 3000);
    };
  }

  _startPolling() {
    clearInterval(this._poll);
    const tick = async () => {
      try {
        const data = await api.positions(this.region);
        const now = Date.now();
        this.ingest(now, data.positions.map((p) => [
          p.vehicle_id, p.route_id, p.pattern_id, p.progress_m, p.lat, p.lon, p.bearing, p.speed_kmh,
          Date.parse(p.timestamp) || now,
        ]));
      } catch {
        /* se reintenta en el siguiente ciclo */
      }
    };
    tick();
    this._poll = setInterval(tick, 3000);
  }

  ingest(serverTime, rows) {
    if (serverTime) this.clockOffset = serverTime - Date.now();
    const seen = new Set();
    for (const [id, route, pattern, progress, lat, lon, bearing, speed, t] of rows) {
      seen.add(id);
      let v = this.vehicles.get(id);
      if (!v) {
        v = { route, samples: [] };
        this.vehicles.set(id, v);
      }
      const last = v.samples[v.samples.length - 1];
      if (last && last.t >= t) continue;
      v.route = route;
      v.samples.push({ t, pattern, progress, lat, lon, bearing, speed });
      if (v.samples.length > MAX_SAMPLES) v.samples.shift();
      if (pattern && !this.tm.patterns.has(pattern)) this.pendingPatterns.add(pattern);
    }
    const cutoff = Date.now() + this.clockOffset - STALE_MS;
    for (const [id, v] of this.vehicles) {
      if (!seen.has(id) && v.samples[v.samples.length - 1].t < cutoff) this.vehicles.delete(id);
    }
    this.onCount(this.vehicles.size);
    if (this.pendingPatterns.size && !this.missingTimer) {
      this.missingTimer = setTimeout(() => this._fetchPatterns(), 400);
    }
  }

  async _fetchPatterns() {
    const ids = [...this.pendingPatterns].slice(0, 150);
    ids.forEach((id) => this.pendingPatterns.delete(id));
    this.missingTimer = null;
    try {
      const data = await api.patterns(ids);
      this.tm.registerPaths(data.patterns);
    } catch {
      /* sin trazo, la unidad se interpola en línea recta */
    }
  }

  // ── Dibujo ────────────────────────────────────────────────────────────────

  _resize() {
    const box = this.canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(box.width * dpr);
    this.canvas.height = Math.round(box.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.width = box.width;
    this.height = box.height;
  }

  _position(v, renderTime, smooth) {
    const s = v.samples;
    const last = s[s.length - 1];
    // Dos muestras que rodean el instante de dibujo; fuera de rango, la más cercana.
    let a = last;
    let b = last;
    if (smooth && renderTime < last.t) {
      if (renderTime <= s[0].t) {
        a = s[0];
        b = s[0];
      } else {
        for (let i = 1; i < s.length; i += 1) {
          if (s[i].t >= renderTime) {
            a = s[i - 1];
            b = s[i];
            break;
          }
        }
      }
    }
    const span = b.t - a.t;
    const f = span > 0 ? Math.min(Math.max((renderTime - a.t) / span, 0), 1) : 1;
    const known = a.pattern && this.tm.patterns.get(a.pattern);
    if (known && a.pattern === b.pattern && a.progress != null && b.progress != null && b.progress >= a.progress - 5) {
      const point = pointAt(known.path, a.progress + (b.progress - a.progress) * f);
      if (point) return point;
    }
    const pick = f < 1 ? a : b;
    return {
      lon: a.lon + (b.lon - a.lon) * f,
      lat: a.lat + (b.lat - a.lat) * f,
      bearing: pick.bearing ?? 0,
    };
  }

  _frame(now) {
    requestAnimationFrame(this._frame);
    if (document.hidden) return;
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.width, this.height);
    const smooth = !reducedMotion();
    const renderTime = Date.now() + this.clockOffset - RENDER_DELAY_MS;
    const zoom = this.map.getZoom();
    const selected = this.tm.selectedRoute?.route_id;
    const mapBearing = this.map.getBearing();
    const drawn = [];
    const late = [];

    this._drawFocus(now);

    for (const [id, v] of this.vehicles) {
      const pos = this._position(v, renderTime, smooth);
      const p = this.map.project([pos.lon, pos.lat]);
      if (p.x < -20 || p.y < -20 || p.x > this.width + 20 || p.y > this.height + 20) continue;
      const isSelected = selected && v.route === selected;
      const item = { id, route: v.route, x: p.x, y: p.y, angle: ((pos.bearing - mapBearing) * Math.PI) / 180, isSelected };
      drawn.push(item);
      // Las unidades de la ruta elegida se dibujan al final, encima del resto.
      if (isSelected) late.push(item);
      else this._drawVehicle(item, zoom, Boolean(selected));
    }
    for (const item of late) this._drawVehicle(item, zoom, true, now);
    this.drawn = drawn;

    this.stats.frames += 1;
    this.stats.drawn = drawn.length;
    if (now - this.stats.last >= 1000) {
      this.stats.fps = Math.round((this.stats.frames * 1000) / (now - this.stats.last));
      this.stats.frames = 0;
      this.stats.last = now;
    }
  }

  _drawVehicle(item, zoom, someSelected, now = 0) {
    const ctx = this.ctx;
    const info = this.routeInfo.get(item.route) || {};
    const color = info.color || "#8a96a3";
    const big = item.isSelected;
    const r = big ? (zoom >= 13 ? 9 : 7.5) : zoom >= 14 ? 7 : zoom >= 12 ? 5.5 : 4;
    ctx.save();
    ctx.globalAlpha = someSelected && !big ? 0.35 : 1;
    ctx.translate(item.x, item.y);

    if (item.id === this.emphasis) {
      const phase = reducedMotion() ? 0.4 : (now % 1600) / 1600;
      ctx.beginPath();
      ctx.arc(0, 0, r + 4 + phase * 12, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(255,255,255,${0.7 * (1 - phase)})`;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    ctx.beginPath();
    ctx.arc(0, 0, r, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = big ? 2.5 : 1.5;
    ctx.strokeStyle = big ? "#ffffff" : "#0e141b";
    ctx.stroke();

    // Flecha hacia el rumbo de la unidad.
    ctx.rotate(item.angle);
    const a = r * 0.62;
    ctx.beginPath();
    ctx.moveTo(0, -a);
    ctx.lineTo(a * 0.78, a * 0.62);
    ctx.lineTo(0, a * 0.22);
    ctx.lineTo(-a * 0.78, a * 0.62);
    ctx.closePath();
    ctx.fillStyle = "#0e141b";
    ctx.fill();
    ctx.restore();

    // Etiqueta: la ruta elegida desde zoom medio; las demás solo de cerca.
    if ((big && zoom >= 12.5) || zoom >= 15) {
      const text = info.short || item.route;
      ctx.save();
      ctx.font = "600 11px Inter, system-ui, sans-serif";
      const w = ctx.measureText(text).width + 10;
      const x = item.x - w / 2;
      const y = item.y - r - 20;
      ctx.globalAlpha = someSelected && !big ? 0.35 : 0.95;
      ctx.fillStyle = "rgba(14,20,27,.88)";
      ctx.beginPath();
      ctx.roundRect(x, y, w, 16, 5);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.fillText(text, x + 5, y + 12);
      ctx.restore();
    }
  }

  _drawFocus(now) {
    const focus = this.tm.focus;
    if (!focus) return;
    const ctx = this.ctx;
    const p = this.map.project([focus.lon, focus.lat]);
    ctx.save();
    if (!reducedMotion()) {
      const phase = (now % 2000) / 2000;
      ctx.beginPath();
      ctx.arc(p.x, p.y, 9 + phase * 20, 0, Math.PI * 2);
      ctx.strokeStyle = focus.color || "#f6bd60";
      ctx.globalAlpha = 0.85 * (1 - phase);
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
    ctx.beginPath();
    ctx.arc(p.x, p.y, 8, 0, Math.PI * 2);
    ctx.fillStyle = focus.color || "#f6bd60";
    ctx.fill();
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
    ctx.restore();
  }

  /** Unidad bajo un punto de la pantalla, o null. */
  hitTest(point) {
    let best = null;
    let bestD = HIT_RADIUS;
    for (const item of this.drawn) {
      const d = Math.hypot(item.x - point.x, item.y - point.y);
      if (d < bestD) {
        best = item;
        bestD = d;
      }
    }
    return best;
  }

  latest(id) {
    const v = this.vehicles.get(id);
    return v ? { route: v.route, ...v.samples[v.samples.length - 1] } : null;
  }

  countForRoute(routeId) {
    let n = 0;
    for (const v of this.vehicles.values()) if (v.route === routeId) n += 1;
    return n;
  }
}
