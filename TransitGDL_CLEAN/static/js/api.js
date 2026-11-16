// Acceso a la API de TransitGDL.

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && typeof payload.detail === "string" ? payload.detail : `Error ${response.status}`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return payload;
}

const q = (params) => new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
const get = (path, params = {}) => request(Object.keys(params).length ? `${path}?${q(params)}` : path);
const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body ?? {}) });

export const api = {
  bootstrap: (region = "tonala_cucei") => get("/map/bootstrap", { region }),
  network: () => get("/map/network"),
  patterns: (ids) => get("/map/patterns", { ids: ids.join(",") }),
  positions: (region) => get("/map/positions", { region }),
  nearby: (lat, lon) => get("/map/nearby", { lat, lon, radius_km: 0.8, limit: 8 }),
  routeDetail: (routeId) => get(`/map/routes/${encodeURIComponent(routeId)}/detail`),
  search: (text) => get("/search", { q: text, limit: 6 }),
  arrivals: (place) => get("/eta/arrivals", { place, limit: 14 }),
  options: (route, stop) => get("/eta/options", { route, stop }),
  feedback: (predictionId, rating) => post("/feedback/", { prediction_id: predictionId, rating }),
  diagnose: (routeId, clima, hora) => get("/expert/diagnose", { route_id: routeId, clima, hora }),
  rules: () => get("/expert/rules"),
  weather: () => get("/demo/weather"),
  setWeather: (condition) => request(`/demo/weather?${q({ condition })}`, { method: "POST" }),
  clearWeather: () => request("/demo/weather", { method: "DELETE" }),
  fleet: (routeId, vehicles) => get("/fleet/recommend", { route_id: routeId, vehicles }),
  incidents: (routeId) => get("/incidents/", { route_id: routeId, limit: 20 }),
  reportIncident: (body) => post("/incidents/", body),
  resolveIncident: (id) => post(`/incidents/${encodeURIComponent(id)}/resolve`),
  chat: (message, sessionId) => post("/chat/message", { message, session_id: sessionId }),
};

export function liveSocketUrl(region) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/map/ws?${q({ compact: "true", region })}`;
}
