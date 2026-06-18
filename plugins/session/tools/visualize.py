#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Vendored from https://github.com/sniperunder123/okf-knowledge (MIT, @4d4a0f2)
# OKF v0.1 tooling. Do not edit locally; re-vendor from upstream to update.
# ---------------------------------------------------------------------------
"""Visualize an Open Knowledge Format (OKF) bundle as an interactive graph.

Reads every non-reserved .md concept, extracts its type/title/description from the
frontmatter and its outgoing intra-bundle links, then emits:
  <bundle>/viz.html   - SELF-CONTAINED interactive force-directed graph (no network)
  <bundle>/graph.mmd  - directory-level aggregated Mermaid diagram (renders on GitHub)

Parsing and link extraction are imported from okf_common so this matches the
validator EXACTLY: PyYAML frontmatter parsing and code-aware link extraction.

The graph shows the *semantic* links between concepts (index.md / log.md navigation
files are excluded), because those links are the knowledge graph.

The HTML is fully offline: no external <script>/<link>, no CDN. The interactive
graph is a compact vanilla-JS force-directed simulation rendered on a <canvas>.

Usage:
    python visualize.py <bundle> [--title "My Bundle"]

Requires PyYAML (via okf_common):  pip install pyyaml
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from okf_common import (
    extract_links,
    is_external,
    parse_frontmatter,
    path_part,
    resolve_link,
)

try:
    import yaml
except ImportError:
    yaml = None

RESERVED = {"index.md", "log.md"}

PALETTE = {
    "systems": "#4285F4", "nexus": "#EA4335", "modules": "#34A853",
    "data": "#F9AB00", "endpoints": "#FF6D00", "metrics": "#A142F4",
    "playbooks": "#00ACC1", "infrastructure": "#9E9E9E", "reference": "#8D6E63",
    "datasets": "#4285F4", "tables": "#34A853", "references": "#8D6E63",
    "": "#E91E63",  # root-level concepts (e.g. overview.md)
}
# Stable extra colors for groups not in PALETTE.
EXTRA_COLORS = [
    "#B0BEC5", "#F06292", "#7E57C2", "#26A69A", "#FFB300",
    "#5C6BC0", "#66BB6A", "#EF5350", "#29B6F6", "#AB47BC",
]
DEFAULT_COLOR = "#B0BEC5"


def group_of(rel):
    return rel.split("/", 1)[0] if "/" in rel else ""


def color_for(group, assigned):
    """Return a stable color for a group, assigning extras deterministically."""
    if group in PALETTE:
        return PALETTE[group]
    if group not in assigned:
        assigned[group] = EXTRA_COLORS[len(assigned) % len(EXTRA_COLORS)]
    return assigned[group]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Visualize an OKF bundle.")
    ap.add_argument("bundle")
    ap.add_argument("--title", default=None)
    args = ap.parse_args(argv)

    if yaml is None:
        print("error: PyYAML is required (via okf_common). "
              "Install it with: pip install pyyaml", file=sys.stderr)
        return 2

    root = Path(args.bundle)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    title = args.title or root.resolve().name + " — OKF Knowledge Graph"

    # 1. Collect concept nodes (non-reserved .md), parsed exactly like the validator.
    nodes = {}  # id -> {label, type, desc, group}
    files = {}  # id -> Path
    text_by_id = {}
    for md in sorted(root.rglob("*.md")):
        if not md.is_file() or md.name in RESERVED:
            continue
        rel = md.relative_to(root).as_posix()
        cid = rel[:-3]  # drop .md
        try:
            text = md.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = md.read_text(encoding="utf-8", errors="replace")
        data, err = parse_frontmatter(text)
        if err or not isinstance(data, dict):
            data = {}
        label = data.get("title")
        if not (isinstance(label, str) and label.strip()):
            label = md.stem.replace("-", " ")
        ctype = data.get("type")
        if not (isinstance(ctype, str) and ctype.strip()):
            ctype = "Concept"
        desc = data.get("description")
        if not isinstance(desc, str):
            desc = ""
        nodes[cid] = {
            "label": label.strip(),
            "type": ctype.strip(),
            "desc": desc.strip(),
            "group": group_of(rel),
        }
        files[cid] = md
        text_by_id[cid] = text

    # 2. Collect edges (concept -> concept) using the validator's link logic.
    #    Reserved files are not concepts; log.md links are excluded entirely.
    root_resolved = root.resolve()
    # Map resolved .md path -> concept id for matching link destinations.
    id_by_resolved = {
        files[cid].resolve(): cid for cid in files
    }
    edges = set()
    for cid, md in files.items():
        text = text_by_id[cid]
        for target in extract_links(text):
            if is_external(target) or target.startswith("#"):
                continue
            part = path_part(target)
            if not part.endswith(".md"):
                continue
            dest = resolve_link(part, md, root).resolve()
            tid = id_by_resolved.get(dest)
            if tid is not None and tid != cid:
                edges.add((cid, tid))

    # 3. Degrees (for node sizing).
    deg = Counter()
    for a, b in edges:
        deg[a] += 1
        deg[b] += 1

    # 4. Build graph payload for the self-contained renderer.
    group_color = {}
    out_nodes = []
    for cid, n in sorted(nodes.items()):
        color = color_for(n["group"], group_color)
        out_nodes.append({
            "id": cid,
            "label": n["label"],
            "type": n["type"],
            "desc": n["desc"],
            "group": n["group"] or "root",
            "color": color,
            "deg": deg.get(cid, 0),
        })
    out_edges = [{"from": a, "to": b} for a, b in sorted(edges)]

    groups_present = sorted({n["group"] for n in nodes.values()},
                            key=lambda g: (g == "", g))
    legend = [{"group": g or "root",
               "color": color_for(g, group_color),
               "count": sum(1 for n in nodes.values() if n["group"] == g)}
              for g in groups_present]
    stats = {"concepts": len(nodes), "links": len(edges),
             "groups": len(groups_present)}

    html_out = _HTML.replace("__TITLE__", html.escape(title)) \
        .replace("__NODES__", json.dumps(out_nodes, ensure_ascii=False)) \
        .replace("__EDGES__", json.dumps(out_edges, ensure_ascii=False)) \
        .replace("__LEGEND__", json.dumps(legend, ensure_ascii=False)) \
        .replace("__STATS__", json.dumps(stats, ensure_ascii=False))
    (root / "viz.html").write_text(html_out, encoding="utf-8")

    # 5. Aggregated directory-level Mermaid (clean enough to render on GitHub).
    inter = Counter()
    for a, b in edges:
        ga, gb = nodes[a]["group"] or "root", nodes[b]["group"] or "root"
        if ga != gb:
            inter[(ga, gb)] += 1
    counts = defaultdict(int)
    for n in nodes.values():
        counts[n["group"] or "root"] += 1
    lines = ["```mermaid", "graph LR"]
    for g in sorted(counts):
        lines.append(f'    {g}["{g} ({counts[g]})"]')
    for (ga, gb), c in sorted(inter.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {ga} -->|{c}| {gb}")
    lines.append("```")
    mmd = "\n".join(lines)
    (root / "graph.mmd").write_text(mmd + "\n", encoding="utf-8")

    print(f"viz.html : {len(out_nodes)} nodes, {len(out_edges)} edges")
    print(f"graph.mmd: {len(counts)} groups")
    print(mmd)
    return 0


# ---------------------------------------------------------------------------
# Self-contained HTML: dark theme, vanilla-JS force-directed graph on a canvas.
# NO external <script>/<link>; everything below is inlined.
# ---------------------------------------------------------------------------
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height:100%; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#0d1117; color:#e6edf3; overflow:hidden; }
  header { padding:12px 20px; border-bottom:1px solid #21262d; display:flex;
           align-items:center; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  .stat { font-size:12px; color:#8b949e; }
  .stat b { color:#e6edf3; }
  #search { margin-left:auto; background:#161b22; border:1px solid #30363d; color:#e6edf3;
            padding:6px 10px; border-radius:6px; font-size:13px; width:220px; outline:none; }
  #search:focus { border-color:#58a6ff; }
  #legend { display:flex; gap:14px; flex-wrap:wrap; padding:8px 20px; border-bottom:1px solid #21262d;
            font-size:12px; }
  .lg { display:flex; align-items:center; gap:6px; cursor:pointer; user-select:none; opacity:0.95; }
  .lg.off { opacity:0.35; text-decoration:line-through; }
  .dot { width:11px; height:11px; border-radius:50%; display:inline-block; }
  #wrap { position:relative; width:100vw; height:calc(100vh - 112px); }
  #net { display:block; width:100%; height:100%; cursor:grab; }
  #net.dragging { cursor:grabbing; }
  .hint { position:absolute; left:0; right:0; bottom:0; padding:6px 20px; font-size:11px;
          color:#6e7681; pointer-events:none; background:linear-gradient(transparent,#0d1117); }
  #tip { position:absolute; pointer-events:none; max-width:300px; background:#161b22;
         border:1px solid #30363d; border-radius:6px; padding:8px 10px; font-size:12px;
         color:#e6edf3; display:none; z-index:5; box-shadow:0 4px 16px rgba(0,0,0,0.5); }
  #tip b { color:#fff; } #tip i { color:#8b949e; }
  #btns { position:absolute; right:12px; bottom:30px; display:flex; flex-direction:column; gap:6px; }
  #btns button { width:30px; height:30px; background:#161b22; border:1px solid #30363d;
                 color:#e6edf3; border-radius:6px; font-size:16px; cursor:pointer; line-height:1; }
  #btns button:hover { border-color:#58a6ff; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="stat" id="stats"></span>
  <input id="search" placeholder="Search concepts…" autocomplete="off" spellcheck="false">
</header>
<div id="legend"></div>
<div id="wrap">
  <canvas id="net"></canvas>
  <div id="tip"></div>
  <div id="btns">
    <button id="zin" title="Zoom in">+</button>
    <button id="zout" title="Zoom out">−</button>
    <button id="zfit" title="Reset view">⤢</button>
  </div>
  <div class="hint">Click a node to highlight neighbours · click empty space to reset · drag nodes/background · scroll to zoom · click legend to toggle groups</div>
</div>
<script>
"use strict";
var NODES = __NODES__, EDGES = __EDGES__, LEGEND = __LEGEND__, STATS = __STATS__;

document.getElementById('stats').innerHTML =
  '<b>' + STATS.concepts + '</b> concepts · <b>' + STATS.links + '</b> links · <b>' +
  STATS.groups + '</b> groups';

// ---- Legend (clickable group toggles) ----
var hiddenGroups = {};
var legendEl = document.getElementById('legend');
LEGEND.forEach(function(l){
  var d = document.createElement('span');
  d.className = 'lg';
  d.innerHTML = '<span class="dot" style="background:' + l.color + '"></span>' +
                l.group + ' (' + l.count + ')';
  d.addEventListener('click', function(){
    hiddenGroups[l.group] = !hiddenGroups[l.group];
    d.classList.toggle('off', !!hiddenGroups[l.group]);
    needRedraw = true;
  });
  legendEl.appendChild(d);
});

// ---- Build node/edge model ----
var nodeById = {};
NODES.forEach(function(n){
  n.r = 6 + Math.min(18, Math.sqrt(n.deg) * 4);  // radius by degree
  n.x = 0; n.y = 0; n.vx = 0; n.vy = 0;
  n.fixed = false;
  nodeById[n.id] = n;
});
// Initialize positions on a circle (deterministic, avoids degenerate stacking).
(function(){
  var N = NODES.length || 1, R = 30 + N * 6;
  for (var i = 0; i < NODES.length; i++){
    var a = (i / N) * Math.PI * 2;
    NODES[i].x = Math.cos(a) * R * (0.6 + ((i * 13) % 7) / 10);
    NODES[i].y = Math.sin(a) * R * (0.6 + ((i * 7) % 5) / 10);
  }
})();
var links = EDGES.map(function(e){
  return { source: nodeById[e.from], target: nodeById[e.to] };
}).filter(function(l){ return l.source && l.target; });

// Adjacency for neighbour highlighting.
var adj = {};
NODES.forEach(function(n){ adj[n.id] = {}; });
links.forEach(function(l){
  adj[l.source.id][l.target.id] = true;
  adj[l.target.id][l.source.id] = true;
});
function isNeighbour(aId, bId){ return aId === bId || (adj[aId] && adj[aId][bId]); }

// ---- Canvas / view ----
var canvas = document.getElementById('net');
var ctx = canvas.getContext('2d');
var DPR = Math.max(1, window.devicePixelRatio || 1);
var W = 0, H = 0;
var view = { x: 0, y: 0, k: 1 };  // pan (x,y in screen px) + zoom k

function resize(){
  var wrap = document.getElementById('wrap');
  W = wrap.clientWidth; H = wrap.clientHeight;
  canvas.width = Math.round(W * DPR);
  canvas.height = Math.round(H * DPR);
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  needRedraw = true;
}
window.addEventListener('resize', resize);

function worldToScreen(wx, wy){
  return { x: wx * view.k + view.x, y: wy * view.k + view.y };
}
function screenToWorld(sx, sy){
  return { x: (sx - view.x) / view.k, y: (sy - view.y) / view.k };
}

// Fit the current node bounds into the viewport.
function fitView(){
  if (!NODES.length){ view = { x: W/2, y: H/2, k: 1 }; needRedraw = true; return; }
  var minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  NODES.forEach(function(n){
    if (n.x < minx) minx = n.x; if (n.y < miny) miny = n.y;
    if (n.x > maxx) maxx = n.x; if (n.y > maxy) maxy = n.y;
  });
  var bw = Math.max(1, maxx - minx), bh = Math.max(1, maxy - miny);
  var pad = 80;
  var k = Math.min((W - pad) / bw, (H - pad) / bh);
  k = Math.max(0.05, Math.min(2.5, k));
  view.k = k;
  view.x = W / 2 - ((minx + maxx) / 2) * k;
  view.y = H / 2 - ((miny + maxy) / 2) * k;
  needRedraw = true;
}

// ---- Force simulation (simple Barnes-Hut-free O(n^2) repulsion) ----
var alpha = 1.0;          // cooling factor
var REPULSION = 5500;
var SPRING = 0.02;
var SPRING_LEN = 70;
var CENTER = 0.012;
var DAMPING = 0.86;

function step(){
  if (alpha < 0.005) return false;
  var n = NODES.length, i, j;
  // Repulsion (pairwise).
  for (i = 0; i < n; i++){
    var a = NODES[i];
    for (j = i + 1; j < n; j++){
      var b = NODES[j];
      var dx = a.x - b.x, dy = a.y - b.y;
      var d2 = dx * dx + dy * dy;
      if (d2 < 0.01){ dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = 0.01; }
      var d = Math.sqrt(d2);
      var f = REPULSION / d2;
      var fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy;
      b.vx -= fx; b.vy -= fy;
    }
  }
  // Springs (edges).
  for (i = 0; i < links.length; i++){
    var s = links[i].source, t = links[i].target;
    var dx2 = t.x - s.x, dy2 = t.y - s.y;
    var dd = Math.sqrt(dx2 * dx2 + dy2 * dy2) || 0.01;
    var force = (dd - SPRING_LEN) * SPRING;
    var ux = (dx2 / dd) * force, uy = (dy2 / dd) * force;
    s.vx += ux; s.vy += uy;
    t.vx -= ux; t.vy -= uy;
  }
  // Centering + integrate.
  for (i = 0; i < n; i++){
    var p = NODES[i];
    if (p.fixed) { p.vx = 0; p.vy = 0; continue; }
    p.vx += -p.x * CENTER;
    p.vy += -p.y * CENTER;
    p.vx *= DAMPING; p.vy *= DAMPING;
    p.x += p.vx * alpha;
    p.y += p.vy * alpha;
  }
  alpha *= 0.985;
  return true;
}

// ---- Interaction state ----
var selected = null;       // selected node id (for neighbour highlight)
var searchHits = null;     // Set of ids matching search, or null
var hoverNode = null;
var needRedraw = true;

function nodeVisible(n){ return !hiddenGroups[n.group]; }

function nodeActive(n){
  // "active" = not dimmed by selection/search filters.
  if (!nodeVisible(n)) return false;
  if (searchHits) return searchHits.has(n.id);
  if (selected) return isNeighbour(selected, n.id);
  return true;
}

function draw(){
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);

  // Edges
  for (var i = 0; i < links.length; i++){
    var s = links[i].source, t = links[i].target;
    if (!nodeVisible(s) || !nodeVisible(t)) continue;
    var p1 = worldToScreen(s.x, s.y), p2 = worldToScreen(t.x, t.y);
    var hot = selected && (s.id === selected || t.id === selected);
    var dim = (selected || searchHits) && !hot &&
              !(searchHits && searchHits.has(s.id) && searchHits.has(t.id));
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.strokeStyle = hot ? '#58a6ff' : (dim ? 'rgba(48,54,61,0.35)' : 'rgba(80,90,100,0.7)');
    ctx.lineWidth = hot ? 1.6 : 0.7;
    ctx.stroke();
    if (hot){ drawArrow(p1, p2, t.r * view.k); }
  }

  // Nodes
  for (var n2 = 0; n2 < NODES.length; n2++){
    var nd = NODES[n2];
    if (!nodeVisible(nd)) continue;
    var p = worldToScreen(nd.x, nd.y);
    var r = Math.max(2, nd.r * view.k);
    var active = nodeActive(nd);
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = active ? nd.color : '#2a2f37';
    ctx.fill();
    if (nd.id === selected){ ctx.lineWidth = 2.5; ctx.strokeStyle = '#fff'; ctx.stroke(); }
    else if (nd === hoverNode){ ctx.lineWidth = 2; ctx.strokeStyle = '#cdd9e5'; ctx.stroke(); }
    else { ctx.lineWidth = 1.2; ctx.strokeStyle = '#0d1117'; ctx.stroke(); }

    // Label (only when zoomed in enough or active, to reduce clutter)
    if ((view.k > 0.55 || active || nd === hoverNode) && (active || !selected && !searchHits)){
      ctx.font = '12px -apple-system,Segoe UI,Roboto,sans-serif';
      ctx.fillStyle = active ? '#e6edf3' : '#586069';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(nd.label, p.x, p.y + r + 2);
    }
  }
  needRedraw = false;
}

function drawArrow(p1, p2, targetR){
  var dx = p2.x - p1.x, dy = p2.y - p1.y;
  var L = Math.sqrt(dx*dx + dy*dy) || 1;
  var ux = dx / L, uy = dy / L;
  var tipx = p2.x - ux * (targetR + 1), tipy = p2.y - uy * (targetR + 1);
  var a = 7;
  ctx.beginPath();
  ctx.moveTo(tipx, tipy);
  ctx.lineTo(tipx - ux*a + uy*a*0.5, tipy - uy*a - ux*a*0.5);
  ctx.lineTo(tipx - ux*a - uy*a*0.5, tipy - uy*a + ux*a*0.5);
  ctx.closePath();
  ctx.fillStyle = '#58a6ff';
  ctx.fill();
}

// ---- Render loop ----
function loop(){
  var moving = step();
  if (moving || needRedraw){ draw(); }
  requestAnimationFrame(loop);
}

// ---- Hit testing ----
function nodeAt(sx, sy){
  // iterate in reverse so topmost (later-drawn) wins
  for (var i = NODES.length - 1; i >= 0; i--){
    var nd = NODES[i];
    if (!nodeVisible(nd)) continue;
    var p = worldToScreen(nd.x, nd.y);
    var r = Math.max(4, nd.r * view.k) + 2;
    var dx = sx - p.x, dy = sy - p.y;
    if (dx*dx + dy*dy <= r*r) return nd;
  }
  return null;
}

// ---- Mouse / pointer handling ----
var drag = null;      // { node } or { pan:true }
var moved = false;
var last = { x: 0, y: 0 };

function localPos(ev){
  var rect = canvas.getBoundingClientRect();
  return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
}

canvas.addEventListener('mousedown', function(ev){
  var pos = localPos(ev);
  var nd = nodeAt(pos.x, pos.y);
  moved = false;
  last = pos;
  if (nd){
    drag = { node: nd };
    nd.fixed = true;
    canvas.classList.add('dragging');
  } else {
    drag = { pan: true };
    canvas.classList.add('dragging');
  }
});

window.addEventListener('mousemove', function(ev){
  var pos = localPos(ev);
  if (drag){
    var dx = pos.x - last.x, dy = pos.y - last.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
    if (drag.node){
      var w = screenToWorld(pos.x, pos.y);
      drag.node.x = w.x; drag.node.y = w.y;
      drag.node.vx = 0; drag.node.vy = 0;
      alpha = Math.max(alpha, 0.3);
    } else {
      view.x += dx; view.y += dy;
    }
    last = pos;
    needRedraw = true;
  } else {
    var nd = nodeAt(pos.x, pos.y);
    if (nd !== hoverNode){ hoverNode = nd; needRedraw = true; }
    showTip(nd, ev);
  }
});

window.addEventListener('mouseup', function(ev){
  if (drag){
    if (drag.node){
      drag.node.fixed = false;
      if (!moved){ toggleSelect(drag.node.id); }
    } else if (!moved){
      // click on empty space -> reset selection
      selected = null; needRedraw = true;
    }
    canvas.classList.remove('dragging');
    drag = null;
  }
});

function toggleSelect(id){
  selected = (selected === id) ? null : id;
  searchHits = null;
  document.getElementById('search').value = '';
  needRedraw = true;
}

// Wheel zoom centered on cursor.
canvas.addEventListener('wheel', function(ev){
  ev.preventDefault();
  var pos = localPos(ev);
  var w = screenToWorld(pos.x, pos.y);
  var factor = Math.exp(-ev.deltaY * 0.0012);
  var nk = Math.max(0.05, Math.min(6, view.k * factor));
  view.k = nk;
  // keep the world point under the cursor stationary
  view.x = pos.x - w.x * view.k;
  view.y = pos.y - w.y * view.k;
  needRedraw = true;
}, { passive: false });

// ---- Tooltip ----
var tip = document.getElementById('tip');
function showTip(nd, ev){
  if (!nd){ tip.style.display = 'none'; return; }
  var h = '<b>' + esc(nd.label) + '</b><br><i>' + esc(nd.type) + '</i>';
  if (nd.desc){ h += '<br>' + esc(nd.desc); }
  h += '<br><span style="color:#6e7681">' + esc(nd.group) + ' · ' + nd.deg + ' link' +
       (nd.deg === 1 ? '' : 's') + '</span>';
  tip.innerHTML = h;
  tip.style.display = 'block';
  var rect = canvas.getBoundingClientRect();
  var x = ev.clientX - rect.left + 14, y = ev.clientY - rect.top + 14;
  if (x + tip.offsetWidth > W) x = W - tip.offsetWidth - 6;
  if (y + tip.offsetHeight > H) y = y - tip.offsetHeight - 28;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}
function esc(s){
  return String(s).replace(/[&<>"]/g, function(c){
    return ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' })[c];
  });
}

// ---- Search ----
document.getElementById('search').addEventListener('input', function(ev){
  var q = ev.target.value.toLowerCase().trim();
  selected = null;
  if (!q){ searchHits = null; needRedraw = true; return; }
  searchHits = new Set();
  NODES.forEach(function(n){
    if ((n.label + ' ' + n.id + ' ' + n.type).toLowerCase().indexOf(q) >= 0){
      searchHits.add(n.id);
    }
  });
  needRedraw = true;
});

// ---- Zoom buttons ----
function zoomBy(f){
  var cx = W/2, cy = H/2;
  var w = screenToWorld(cx, cy);
  view.k = Math.max(0.05, Math.min(6, view.k * f));
  view.x = cx - w.x * view.k;
  view.y = cy - w.y * view.k;
  needRedraw = true;
}
document.getElementById('zin').addEventListener('click', function(){ zoomBy(1.3); });
document.getElementById('zout').addEventListener('click', function(){ zoomBy(1/1.3); });
document.getElementById('zfit').addEventListener('click', function(){ fitView(); });

// ---- Boot ----
resize();
fitView();
// Run a few warm-up steps so the initial frame isn't a tangled circle, then fit.
(function warmup(){
  for (var i = 0; i < 60; i++){ step(); }
  fitView();
})();
loop();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
