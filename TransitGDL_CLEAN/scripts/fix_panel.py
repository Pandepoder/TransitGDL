from pathlib import Path

PANEL = """<aside class="panel" id="panel">
  <div class="panel-tabs" id="panel-tabs">
    <button type="button" class="on" data-ptab="routes">Rutas</button>
    <button type="button" data-ptab="eta">ETA</button>
    <button type="button" data-ptab="tools">Mas</button>
  </div>
  <div class="tab-pane on" id="ptab-routes">
    <div class="panel-hd"><span>Lineas CUCEI / Tonala</span><button class="btn sec sm" type="button" id="btn-inject">Bus demo</button></div>
    <div class="panel-bd scroll" id="route-list"></div>
  </div>
  <div class="tab-pane" id="ptab-eta">
    <div class="panel-hd">ETA y parada</div>
    <div class="panel-bd" style="flex:1;overflow-y:auto">
      <input class="inp" id="stop-q" placeholder="Parada: CUCEI, Tonaltecas, Loma Dorada" autocomplete="off"/>
      <div id="stop-suggest" class="meta"></div>
      <div id="eta-box"><p class="meta">Elige ruta y parada o usa el asistente.</p></div>
      <div id="insights" class="insight" style="display:none"></div>
    </div>
    <div class="panel-hd">Calificar prediccion</div>
    <div class="panel-bd" id="fb-section" style="opacity:.45;pointer-events:none">
      <p class="meta">Tras calcular un ETA, indica si fue preciso.</p>
      <div class="fb-row">
        <button class="btn sm sec" type="button" data-rating="1">Antes</button>
        <button class="btn sm sec" type="button" data-rating="2">Puntual</button>
        <button class="btn sm sec" type="button" data-rating="3">+5 min</button>
        <button class="btn sm sec" type="button" data-rating="4">+12 min</button>
        <button class="btn sm sec" type="button" data-rating="5">+20 min</button>
      </div>
      <p id="fb-msg" class="meta" style="margin-top:6px"></p>
    </div>
  </div>
  <div class="tab-pane" id="ptab-tools">
    <div class="panel-hd">Demo y clima</div>
    <div class="panel-bd">
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">
        <button class="btn sm sec" type="button" data-scenario="eta">ETA CUCEI</button>
        <button class="btn sm sec" type="button" data-scenario="incident">Incidencia</button>
        <button class="btn sm sec" type="button" data-scenario="feedback">Aprendizaje</button>
        <button class="btn sm warn" type="button" id="btn-rain">Lluvia</button>
        <button class="btn sm sec" type="button" id="btn-clear-weather">Clima real</button>
      </div>
      <label class="pill" style="display:flex;align-items:center;gap:6px;margin-bottom:6px;cursor:pointer"><input type="checkbox" id="chk-all-lines"/> Todas las lineas</label>
      <label class="pill" style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" id="chk-all-network"/> Toda la red GDL</label>
    </div>
    <div class="panel-hd">Incidencia</div>
    <div class="panel-bd">
      <textarea id="inc-txt" placeholder="Que esta pasando" rows="2"></textarea>
      <select id="inc-route"></select>
      <button class="btn sm" type="button" id="inc-send" style="width:100%">Reportar</button>
      <p id="inc-msg" class="meta" style="margin-top:6px"></p>
    </div>
  </div>
</aside>"""

PANEL = PANEL.replace("<motion.div", "<div").replace("</motion.div>", "</div>")

p = Path(__file__).resolve().parent.parent / "static" / "index.html"
t = p.read_text(encoding="utf-8")
start = t.index('<aside class="panel"')
end = t.index("</aside>", start) + len("</aside>")
p.write_text(t[:start] + PANEL + t[end:], encoding="utf-8")
print("ok")
