// Formato de textos y utilidades de geometría.

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Texto legible sobre un color de fondo #RRGGBB. */
export function inkFor(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "#ffffff";
  const n = parseInt(m[1], 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return lum > 0.32 ? "#14110a" : "#ffffff";
}

export function routeChip(route, size = "") {
  const color = route.color || "#8a96a3";
  const name = route.short_name || route.route_number || route.route_id;
  return `<span class="route-chip ${size}" style="background:${esc(color)};color:${inkFor(color)}">${esc(name)}</span>`;
}

export function minutesText(minutes) {
  const m = Math.max(0, Math.round(minutes));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

export function distanceText(meters) {
  if (meters == null) return "";
  if (meters < 1000) return `${Math.round(meters / 10) * 10} m`;
  return `${(meters / 1000).toFixed(1)} km`;
}

export function rangeText(lo, hi) {
  if (lo == null || hi == null) return "";
  const a = Math.round(lo);
  const b = Math.round(hi);
  if (b <= a) return `Cerca de ${minutesText(a)}`;
  if (a <= 0) return `En menos de ${minutesText(b)}`;
  return `Llega entre ${a} y ${minutesText(b)}`;
}

export function methodBadge(eta) {
  if (eta.method === "vehiculo") return `<span class="badge badge--live">En vivo</span>`;
  if (eta.method === "frecuencia") return `<span class="badge">Por frecuencia</span>`;
  return `<span class="badge">Horario</span>`;
}

export const CONFIDENCE_TEXT = { alta: "Confianza alta", media: "Confianza media", baja: "Confianza baja" };

/** Tipo de servicio, para subtítulos. */
export const LAYER_LABEL = {
  metro: "Tren ligero",
  macro: "Mi Macro",
  sitren: "SiTren",
  troncal: "Troncal Mi Transporte",
  complementaria: "Complementaria Mi Transporte",
  feeder: "Alimentadora Mi Macro",
  bus: "Autobús",
};

/** Subtítulo de una ruta: número anterior y tipo de servicio. */
export const routeSubtitle = (route) =>
  [route.legacy_number ? `Antes ${route.legacy_number}` : "", LAYER_LABEL[route.layer] || ""].filter(Boolean).join(" · ");

/** Etiqueta del sentido: "Hacia La Coronilla". */
export const toward = (headsign) => (headsign ? `Hacia ${headsign}` : "");

// ── Geometría ──────────────────────────────────────────────────────────────

/** Decodifica una polilínea (precisión 5) en [[lon, lat], …]. */
export function decodePolyline(text) {
  const points = [];
  let index = 0;
  let lat = 0;
  let lon = 0;
  while (index < text.length) {
    for (let k = 0; k < 2; k += 1) {
      let shift = 0;
      let result = 0;
      let byte;
      do {
        byte = text.charCodeAt(index++) - 63;
        result |= (byte & 0x1f) << shift;
        shift += 5;
      } while (byte >= 0x20);
      const delta = result & 1 ? ~(result >> 1) : result >> 1;
      if (k === 0) lat += delta;
      else lon += delta;
    }
    points.push([lon / 1e5, lat / 1e5]);
  }
  return points;
}

const M_PER_DEG = 111195;

/** Trazo con distancias acumuladas (m) para ubicar puntos por avance. */
export function makePath(coords, lengthM) {
  const cum = [0];
  const k = Math.cos(((coords[0]?.[1] ?? 20.6) * Math.PI) / 180);
  for (let i = 1; i < coords.length; i += 1) {
    const dx = (coords[i][0] - coords[i - 1][0]) * k * M_PER_DEG;
    const dy = (coords[i][1] - coords[i - 1][1]) * M_PER_DEG;
    cum.push(cum[i - 1] + Math.hypot(dx, dy));
  }
  const drawn = cum[cum.length - 1] || 1;
  // El avance viene medido sobre el trazo completo; el dibujado está simplificado.
  return { coords, cum, scale: lengthM ? drawn / lengthM : 1, k };
}

/** Punto y rumbo (grados, 0 = norte) a `progress` metros del inicio. */
export function pointAt(path, progress) {
  const { coords, cum } = path;
  const n = coords.length;
  if (n === 0) return null;
  if (n === 1) return { lon: coords[0][0], lat: coords[0][1], bearing: 0 };
  const d = Math.min(Math.max(progress * path.scale, 0), cum[n - 1]);
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= d) lo = mid;
    else hi = mid;
  }
  const span = cum[hi] - cum[lo] || 1;
  const t = (d - cum[lo]) / span;
  const [x0, y0] = coords[lo];
  const [x1, y1] = coords[hi];
  const bearing = (Math.atan2((x1 - x0) * path.k, y1 - y0) * 180) / Math.PI;
  return { lon: x0 + (x1 - x0) * t, lat: y0 + (y1 - y0) * t, bearing: (bearing + 360) % 360 };
}

export function boundsOf(coordLists) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const list of coordLists) {
    for (const [x, y] of list) {
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }
  }
  return Number.isFinite(minX) ? [[minX, minY], [maxX, maxY]] : null;
}
