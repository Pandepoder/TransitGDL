from pathlib import Path

p = Path(__file__).resolve().parent.parent / "static" / "index.html"
t = p.read_text(encoding="utf-8")

dock = """
<nav class="dock" id="dock">
  <button type="button" class="on" data-dock="map"><span class="ico">🗺</span>Mapa</button>
  <button type="button" data-dock="routes"><span class="ico">🚌</span>Rutas</button>
  <button type="button" data-dock="chat"><span class="ico">💬</span>Asistente</button>
  <button type="button" data-dock="tools"><span class="ico">⚙</span>Más</button>
</nav>
"""

if 'id="dock"' not in t:
    t = t.replace("</aside>\n<div class=\"sheet\"", "</aside>\n" + dock + "<motion.div class=\"sheet\"", 1)
    t = t.replace('<motion.div class="sheet"', '<motion.div class="sheet"'.replace("motion.", ""), 1)

marker = '<aside class="panel" id="panel">\n  <div class="panel-hd"><span>Rutas</span>'
if 'id="panel-tabs"' not in t and marker in t:
    t = t.replace(
        marker,
        '<aside class="panel" id="panel">\n'
        '  <div class="panel-tabs" id="panel-tabs">'
        '<button type="button" class="on" data-ptab="routes">Rutas</button>'
        '<button type="button" data-ptab="eta">ETA</button>'
        '<button type="button" data-ptab="tools">Más</button></div>\n'
        '  <div class="tab-pane on" id="ptab-routes"><div class="panel-hd"><span>Rutas</span>',
        1,
    )
    t = t.replace(
        '  <div class="panel-bd scroll" id="route-list"></div>\n  <div class="panel-hd">ETA y parada</motion.div>',
        '  <div class="panel-bd scroll" id="route-list"></div></div>\n'
        '  <div class="tab-pane" id="ptab-eta"><div class="panel-hd">ETA y parada</div>',
        1,
    )
    t = t.replace(
        '  <div class="panel-hd">Incidencia',
        '  </div></div>\n  <motion.div class="tab-pane" id="ptab-tools"><div class="panel-hd">Incidencia'.replace(
            "motion.", ""
        ),
        1,
    )

js = (
    "function setPanelTab(name){const t=document.getElementById('panel-tabs');"
    "if(!t)return;t.querySelectorAll('[data-ptab]').forEach(b=>b.classList.toggle('on',b.dataset.ptab===name));"
    "document.querySelectorAll('.tab-pane').forEach(p=>p.classList.toggle('on',p.id==='ptab-'+name));}"
    "function setDockTab(name){const d=document.getElementById('dock');if(!d)return;"
    "d.querySelectorAll('[data-dock]').forEach(b=>b.classList.toggle('on',b.dataset.dock===name));"
    "if(name==='map'){setPanelOpen(false);toggleChat(false);}"
    "else if(name==='routes'){setPanelOpen(true);setPanelTab('routes');toggleChat(false);}"
    "else if(name==='chat'){setPanelOpen(false);toggleChat(true);}"
    "else if(name==='tools'){setPanelOpen(true);setPanelTab('tools');toggleChat(false);}"
    "syncChromeHeight();}"
)

if "function setDockTab" not in t:
    anchor = '$("btn-menu").onclick=()=>setPanelOpen(!$("panel").classList.contains("open"));'
    t = t.replace(
        anchor,
        anchor
        + js
        + "document.querySelectorAll('#dock [data-dock]').forEach(b=>b.onclick=()=>setDockTab(b.dataset.dock));"
        + "document.querySelectorAll('#panel-tabs [data-ptab]').forEach(b=>b.onclick=()=>setPanelTab(b.dataset.ptab));",
    )

p.write_text(t, encoding="utf-8")
print("patched ok")
