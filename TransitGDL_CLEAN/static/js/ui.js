// Piezas de interfaz: pestañas, hoja inferior (móvil), avisos y buscador.
import { esc, routeChip, routeSubtitle } from "./format.js";

const $ = (selector, root = document) => root.querySelector(selector);

// ── Avisos ───────────────────────────────────────────────────────────────────

export function toast(message, kind = "info", ms = 3200) {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast${kind === "error" ? " toast--error" : ""}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ── Pestañas ─────────────────────────────────────────────────────────────────

export function initTabs(onChange) {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (tab, focus = false) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
    onChange(tab.id.replace("tab-", ""));
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      const delta = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!delta) return;
      event.preventDefault();
      select(tabs[(index + delta + tabs.length) % tabs.length], true);
    });
  });
  return (name) => select(document.getElementById(`tab-${name}`));
}

// ── Hoja inferior en móvil: tres alturas, se arrastra o se toca ─────────────

export function initSheet() {
  const panel = $("#panel");
  const handle = $("#sheet-handle");
  const narrow = window.matchMedia("(max-width: 767px)");
  const snaps = () => [190, Math.round(window.innerHeight * 0.46), window.innerHeight - 72];
  let level = 1;

  const apply = (px) => {
    panel.style.setProperty("--sheet-h", `${px}px`);
    document.documentElement.style.setProperty("--sheet-h", `${px}px`);
  };
  const snapTo = (index) => {
    level = Math.max(0, Math.min(2, index));
    apply(snaps()[level]);
  };

  let start = null;
  handle.addEventListener("pointerdown", (event) => {
    if (!narrow.matches) return;
    start = { y: event.clientY, h: panel.getBoundingClientRect().height, moved: false };
    handle.setPointerCapture(event.pointerId);
    panel.classList.add("is-dragging");
  });
  handle.addEventListener("pointermove", (event) => {
    if (!start) return;
    const dy = start.y - event.clientY;
    if (Math.abs(dy) > 4) start.moved = true;
    const [min, , max] = snaps();
    apply(Math.max(min, Math.min(max, start.h + dy)));
  });
  const end = () => {
    if (!start) return;
    panel.classList.remove("is-dragging");
    if (start.moved) {
      const h = panel.getBoundingClientRect().height;
      const s = snaps();
      snapTo(s.reduce((best, value, i) => (Math.abs(value - h) < Math.abs(s[best] - h) ? i : best), 0));
    } else {
      snapTo(level === 2 ? 0 : level + 1);
    }
    start = null;
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
  handle.addEventListener("keydown", (event) => {
    if (event.key === "ArrowUp") snapTo(level + 1);
    if (event.key === "ArrowDown") snapTo(level - 1);
  });
  window.addEventListener("resize", () => narrow.matches && snapTo(level));
  if (narrow.matches) snapTo(1);

  return {
    open: (index = 1) => narrow.matches && level < index && snapTo(index),
  };
}

// ── Buscador ─────────────────────────────────────────────────────────────────

export function initSearch({ search, onPick }) {
  const input = $("#search-input");
  const list = $("#search-results");
  const clear = $("#search-clear");
  let items = [];
  let active = -1;
  let timer = null;
  let ticket = 0;

  const close = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };

  const highlight = (index) => {
    active = index;
    list.querySelectorAll(".search-option").forEach((el, i) => el.setAttribute("aria-selected", String(i === index)));
    if (index >= 0) {
      input.setAttribute("aria-activedescendant", `opt-${index}`);
      list.querySelector(`#opt-${index}`)?.scrollIntoView({ block: "nearest" });
    }
  };

  const render = (result) => {
    items = [];
    const groups = [];
    if (result.intent) {
      items.push({ kind: "intent", ...result.intent });
      groups.push(["", [items.length - 1]]);
    }
    const add = (title, kind, entries) => {
      if (!entries.length) return;
      const idx = entries.map((entry) => items.push({ kind, ...entry }) - 1);
      groups.push([title, idx]);
    };
    add("Rutas", "route", result.routes);
    add("Lugares", "place", result.places);
    add("Paradas", "stop", result.stops);

    if (!items.length) {
      list.innerHTML = `<li class="search-empty">Sin resultados para «${esc(result.query)}».</li>`;
    } else {
      list.innerHTML = groups.map(([title, idx]) => `
        ${title ? `<li class="search-group" role="presentation">${title}</li>` : ""}
        ${idx.map((i) => option(items[i], i)).join("")}`).join("");
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    highlight(items.length ? 0 : -1);
  };

  const option = (item, i) => {
    let lead = "";
    let title = "";
    let sub = "";
    if (item.kind === "intent") {
      lead = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#f6bd60" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>`;
      title = `Llegadas de la ${esc(item.route)} en ${esc(item.stop)}`;
      sub = "Tiempo estimado en ambos sentidos";
    } else if (item.kind === "route") {
      lead = routeChip(item);
      title = esc(item.branch_name || item.long_name || item.short_name);
      sub = esc(routeSubtitle(item));
    } else if (item.kind === "place") {
      lead = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#c9d5e3" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 21s-7-6.2-7-11a7 7 0 1 1 14 0c0 4.8-7 11-7 11z"></path><circle cx="12" cy="10" r="2.5"></circle></svg>`;
      title = esc(item.name);
      sub = "Lugar · llegadas cercanas";
    } else {
      lead = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#c9d5e3" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="5"></circle></svg>`;
      title = esc(item.stop_name);
      sub = esc(item.routes.slice(0, 6).join(", ") + (item.routes.length > 6 ? "…" : ""));
    }
    return `<li class="search-option" role="option" id="opt-${i}" data-index="${i}" aria-selected="false">${lead}
      <span class="opt-main"><span class="opt-title">${title}</span><span class="opt-sub">${sub}</span></span></li>`;
  };

  const pick = (index) => {
    const item = items[index];
    if (!item) return;
    close();
    input.blur();
    onPick(item);
  };

  input.addEventListener("input", () => {
    clear.hidden = !input.value;
    clearTimeout(timer);
    const text = input.value.trim();
    if (!text) return close();
    timer = setTimeout(async () => {
      const mine = ++ticket;
      try {
        const result = await search(text);
        if (mine === ticket && input.value.trim()) render(result);
      } catch {
        /* sin resultados */
      }
    }, 180);
  });
  input.addEventListener("keydown", (event) => {
    if (list.hidden) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(Math.min(items.length - 1, active + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(Math.max(0, active - 1));
    } else if (event.key === "Escape") {
      close();
    }
  });
  $("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!list.hidden && active >= 0) pick(active);
  });
  list.addEventListener("pointerdown", (event) => {
    const el = event.target.closest(".search-option");
    if (!el) return;
    event.preventDefault();
    pick(Number(el.dataset.index));
  });
  clear.addEventListener("click", () => {
    input.value = "";
    clear.hidden = true;
    close();
    input.focus();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("#search-form")) close();
  });

  return {
    setText(text) {
      input.value = text;
      clear.hidden = !text;
    },
  };
}
