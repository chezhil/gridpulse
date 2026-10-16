/* GridPulse Lab: temporary UI over every backend feature. Plain JS, no build step. */
(() => {
  const API = (location.protocol.startsWith('http') && !['5500', '8080', '8765', '8766'].includes(location.port)) ? location.origin : 'http://localhost:8000';
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(n)) ? '—' : Number(n).toFixed(d);
  let token = sessionStorage.getItem('gp_lab_token');
  let role = sessionStorage.getItem('gp_lab_role');
  let currentNet = null;   // {id, spec}
  let cases = [];
  const cys = {};

  // ---------------- basics ----------------
  function toast(msg, info = false) {
    const t = $('toast'); t.textContent = msg; t.className = info ? 'info' : ''; t.style.display = 'block';
    clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = 'none'), info ? 2500 : 7000);
  }
  async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) };
    const res = await fetch(API + path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = data.detail;
      let msg = typeof d === 'string' ? d : d?.message || `HTTP ${res.status}`;
      if (d?.details?.problems) msg += ':\n• ' + d.details.problems.map((p) => typeof p === 'string' ? p : JSON.stringify(p)).join('\n• ');
      if (Array.isArray(d)) msg = d.map((e) => `${(e.loc || []).join('.')}: ${e.msg}`).join('; ');
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return data;
  }
  const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); } catch (e) { toast(e.message); } finally { btn.disabled = false; } };
  const pill = (txt, cls) => `<span class="pill ${cls}">${esc(txt)}</span>`;
  const outcomePill = (o) => pill(o, o === 'WITHIN_LIMITS' || o === 'MITIGATED' || o === 'SAVED' || o === 'APPROVED' || o === 'NOT_NEEDED' ? 'ok' : o === 'ISLANDING_PREDICTED' || o === 'NOT_SAVEABLE' || o === 'FAILED' || o === 'REJECTED' ? 'bad' : 'warn');

  // ---------------- tabs ----------------
  $('tabs').addEventListener('click', (e) => {
    const t = e.target.dataset.t; if (!t) return;
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === t));
    document.querySelectorAll('main section').forEach((s) => s.classList.toggle('on', s.id === t));
    Object.values(cys).forEach((c) => c && c.resize());
    if (t === 'builder') { editor.resize(); editor.cy.fit(undefined, 30); }
    if (t === 'memory') loadMemory();
    if (t === 'askgrid' && cys.cyAsk) cys.cyAsk.resize();
    if (t === 'audit') loadAudit();
    if (t === 'sim') renderCases();
  });

  // ---------------- auth ----------------
  function showWho() { $('who').textContent = token ? `signed in as ${sessionStorage.getItem('gp_lab_user')} (${role})` : 'not signed in'; }
  $('login').onclick = () => busy($('login'), async () => {
    const r = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username: $('user').value || 'LAB_OPERATOR', password: 'gridpulse2026', role: $('role').value }) });
    token = r.access_token; role = r.role;
    sessionStorage.setItem('gp_lab_token', token); sessionStorage.setItem('gp_lab_role', role); sessionStorage.setItem('gp_lab_user', r.operator_id);
    showWho(); connectWs(); loadNetworks(); toast('Signed in', true);
  });

  // ---------------- graph rendering ----------------
  const STYLE = [
    { selector: 'node', style: { label: 'data(label)', color: '#cbd5e1', 'font-size': 9, 'text-valign': 'bottom', 'text-margin-y': 3, 'background-color': '#38bdf8', width: 14, height: 14 } },
    { selector: 'node.HydroPlant', style: { shape: 'round-triangle', 'background-color': '#22d3ee', width: 22, height: 22 } },
    { selector: 'node.Tower', style: { shape: 'diamond', 'background-color': '#94a3b8', width: 12, height: 12 } },
    { selector: 'node.Substation', style: { shape: 'round-rectangle', 'background-color': '#0ea5e9', width: 20, height: 20 } },
    { selector: 'node.BESS', style: { shape: 'barrel', 'background-color': '#10b981', width: 16, height: 16 } },
    { selector: 'node.Consumer', style: { shape: 'ellipse', 'background-color': '#64748b', width: 11, height: 11, 'font-size': 8 } },
    { selector: 'node.tier1', style: { 'background-color': '#a855f7', width: 15, height: 15 } },
    { selector: 'node.tier3', style: { 'background-color': '#f59e0b' } },
    { selector: 'node.off', style: { 'background-color': '#334155', 'border-width': 2, 'border-color': '#f59e0b' } },
    { selector: 'node.islanded', style: { 'border-width': 3, 'border-color': '#f59e0b' } },
    { selector: 'node.hl', style: { 'border-width': 3, 'border-color': '#fde047' } },
    { selector: 'edge', style: { width: 2, 'line-color': '#38bdf8', 'curve-style': 'bezier', 'target-arrow-shape': 'none', opacity: 0.85 } },
    { selector: 'edge.LOWER_BACKUP', style: { 'line-color': '#1d4ed8', width: 1.5 } },
    { selector: 'edge.dist', style: { 'line-color': '#334155', width: 1 } },
    { selector: 'edge.feeds', style: { 'line-color': '#22d3ee', width: 3 } },
    { selector: 'edge.tripped', style: { 'line-color': '#64748b', 'line-style': 'dashed', width: 2 } },
    { selector: 'edge.fault', style: { 'line-color': '#ef4444', 'line-style': 'dashed', width: 3 } },
    { selector: 'edge.rerouted', style: { 'line-color': '#10b981', width: 3.5 } },
    { selector: 'edge.over', style: { 'line-color': '#ef4444', width: 4, label: 'data(load)', 'font-size': 8, color: '#fecaca' } },
    { selector: 'edge.path', style: { 'line-color': '#fde047', width: 4 } },
  ];
  function toElements(topo) {
    const els = [];
    for (const n of topo.nodes) {
      const p = n.properties || {};
      const cls = [n.label, p.priority_tier === 1 ? 'tier1' : '', p.priority_tier === 3 ? 'tier3' : '', p.energized === false ? 'off' : ''].join(' ');
      els.push({ data: { id: n.id, label: n.id, props: p, kind: n.label }, classes: cls });
    }
    for (const e of topo.edges) {
      const p = e.properties || {};
      const cls = e.type === 'DISTRIBUTES_TO' || e.type === 'CONNECTED' ? 'dist' : e.type === 'FEEDS' ? 'feeds' : (p.circuit || '') + (p.status === 'TRIPPED' ? ' tripped' : '');
      els.push({ data: { id: e.id, source: e.source, target: e.target, props: p, kind: e.type }, classes: cls });
    }
    return els;
  }
  function draw(containerId, topo, onTap) {
    if (cys[containerId]) cys[containerId].destroy();
    const n = topo.nodes.length;
    const corridor = topo.nodes.some((x) => x.id === 'HYDRO_ALPINE_01') && topo.nodes.every((x) => x.label !== 'Tower' || x.properties?.world_x !== undefined);
    const cy = cytoscape({ container: $(containerId), elements: toElements(topo), style: STYLE, wheelSensitivity: 0.25,
      layout: corridor ? { name: 'preset', positions: corridorPositions(topo) } : { name: 'cose', animate: false, nodeRepulsion: n > 100 ? 6000 : 9000, idealEdgeLength: n > 100 ? 30 : 60, numIter: 1200 } });
    cy.fit(undefined, 20);
    cy.on('tap', 'node, edge', (ev) => onTap && onTap(ev.target));
    cys[containerId] = cy;
    return cy;
  }
  // The demo corridor has real world_x coordinates: lay it out left-to-right like the landing page.
  function corridorPositions(topo) {
    const pos = {}; const byId = Object.fromEntries(topo.nodes.map((n) => [n.id, n]));
    const sub = topo.nodes.find((n) => n.label === 'Substation');
    const subX = (sub?.properties?.world_x ?? 5545) / 6;
    topo.nodes.forEach((n) => {
      const x = n.properties?.world_x;
      if (n.label === 'HydroPlant' || n.label === 'Tower' || n.label === 'Substation') pos[n.id] = { x: (x ?? 0) / 6, y: 0 };
    });
    const consumers = topo.nodes.filter((n) => n.label === 'Consumer');
    consumers.forEach((n, i) => (pos[n.id] = { x: subX - 120 + i * 80, y: 130 }));
    topo.nodes.filter((n) => n.label === 'BESS').forEach((n, i) => (pos[n.id] = { x: subX + 60 + i * 40, y: -110 }));
    topo.nodes.forEach((n) => { if (!pos[n.id]) pos[n.id] = { x: subX, y: 200 }; });
    return (node) => pos[node.id()] || { x: 0, y: 0 };
  }
  const describe = (el) => toast(`${el.data('kind')} ${el.id()}\n${JSON.stringify(el.data('props'), null, 1).slice(0, 600)}`, true);

  // ---------------- live ops ----------------
  let ws = null, liveTopo = null, wsDelay = 1000;
  async function loadLiveTopology() {
    try { liveTopo = await api('/api/graph/topology'); draw('cyLive', liveTopo, describe); } catch (e) { toast('Topology: ' + e.message); }
  }
  function connectWs() {
    if (!token) return;
    if (ws) { ws.onclose = null; ws.close(); }
    ws = new WebSocket(API.replace(/^http/, 'ws') + '/ws/telemetry?token=' + encodeURIComponent(token));
    ws.onopen = () => { wsDelay = 1000; $('ws').textContent = 'telemetry: live'; $('ws').className = 'pill ok'; };
    ws.onclose = (ev) => {
      if (ev.code === 1008) {  // server rejected the session (e.g. backend restarted): stop and ask to sign in
        token = null; sessionStorage.removeItem('gp_lab_token'); showWho();
        $('ws').textContent = 'telemetry: sign in'; $('ws').className = 'pill bad'; toast('Session expired: sign in again', true); return;
      }
      $('ws').textContent = 'telemetry: reconnecting'; $('ws').className = 'pill warn';
      setTimeout(connectWs, wsDelay); wsDelay = Math.min(wsDelay * 2, 15000);
    };
    ws.onmessage = (m) => { try { renderLive(JSON.parse(m.data)); } catch (e) { /* ignore malformed frames */ } };
  }
  let lastSteps = '';
  function renderLive(s) {
    const m = s.metrics || {};
    $('kpi').innerHTML = [
      ['Scenario', `${s.scenario?.name || '—'}`], ['Status', s.scenario?.status], ['Mode', s.mode || 'deterministic'],
      ['City load', `${fmt(m.city_load_mw, 0)} MW`], ['Substation', `${fmt(m.substation_load_mw, 0)} MW`],
      ['Battery', `${fmt(m.bess_injection_mw, 1)} MW`], ['Hospital', m.tier1_hospital_status], ['Graph ms', fmt(m.graphblas_latency_ms, 3)],
    ].map(([k, v]) => `<div><small>${k}</small><b>${esc(v)}</b></div>`).join('');
    $('agentLog').textContent = s.agent_log || '';
    $('cypher').textContent = s.active_cypher || '—';
    const steps = s.agent_steps || [];
    const sig = JSON.stringify(steps.map((x) => x.at));
    if (sig !== lastSteps) { lastSteps = sig; $('steps').innerHTML = steps.length ? steps.map(stepHtml).join('') : '<span class="muted small">No agent steps for this run (deterministic mode shows its Cypher below).</span>'; $('steps').scrollTop = 1e6; }
    const mem = s.memory;
    $('liveMemory').innerHTML = mem ? memorySummary(mem) : '—';
    const cy = cys.cyLive; if (!cy) return;
    for (const sp of s.spans || []) {
      const up = cy.getElementById(`${sp.id}:UPPER_PRIMARY`), lo = cy.getElementById(`${sp.id}:LOWER_BACKUP`);
      up.removeClass('fault rerouted'); lo.removeClass('fault rerouted');
      if (['FAULT_TRIPPED', 'DE_ENERGIZED', 'TRIPPED'].includes(sp.upper_status)) up.addClass('fault');
      if (sp.lower_status === 'REROUTED_BESS') lo.addClass('rerouted');
      if (['FAULT_TRIPPED', 'DE_ENERGIZED', 'TRIPPED'].includes(sp.lower_status)) lo.addClass('fault');
    }
    for (const c of s.consumers || []) cy.getElementById(c.id).toggleClass('off', c.energized === false);
  }
  function stepHtml(x) {
    const head = { start: '▶ Fault', context: `📚 Graph briefing: ${x.tool}`, wait: '⏳ Waiting', thought: '💭 Model', tool: `🔧 ${x.tool}`, tool_error: `⚠ ${x.tool} error`, submit: '📝 Plan submitted', verdict: `⚖ Graph guard: ${x.verdict}`, fallback: '↩ Fallback', done: '✅ Done' }[x.kind] || x.kind;
    let body = '';
    if (x.kind === 'tool' || x.kind === 'context') body = `<div class="mono">${esc(JSON.stringify(x.input))}</div><div>${esc(x.result)} <span class="muted">(${fmt(x.server_ms, 3)} ms graph)</span></div>` +
      (x.cypher?.length ? `<details><summary>Cypher & evidence</summary><pre>${esc(x.cypher.join('\n\n'))}\n\n${esc((x.evidence_hashes || []).join('\n'))}</pre></details>` : '');
    else if (x.kind === 'submit') body = `<div>${esc(x.summary)}</div><div class="mono">${esc(JSON.stringify(x.plan))}</div>`;
    else if (x.kind === 'verdict') body = `<div>${esc(x.summary || '')}</div>${x.reasons?.length ? `<div class="bad mono">${esc(x.reasons.join('; '))}</div>` : ''}`;
    else body = `<div>${esc(x.text || x.error || '')}</div>`;
    return `<div class="step ${x.kind}"><b>${head}</b> <span class="muted small">${esc((x.at || '').slice(11, 19))}</span>${body}</div>`;
  }
  function memorySummary(mem) {
    const parts = [];
    if (mem.suggestions?.length) parts.push('<b>Learned plans for this fault:</b> ' + mem.suggestions.map((sg) => `${esc(sg.signature)} (score ${sg.score}; ${esc(sg.why)})`).join('; '));
    if (mem.reused_playbook) parts.push(`<b class="ok">↺ Reused playbook ${esc(mem.reused_playbook)}</b>${mem.reused_credit ? ` → now ${mem.reused_credit.success_count}✓/${mem.reused_credit.failure_count}✗` : ''}`);
    if (mem.playbooks?.length) parts.push('<b>Playbooks nearby:</b> ' + mem.playbooks.map((p) => `${esc(p.signature)} (${p.success_count}✓/${p.failure_count}✗)`).join(', '));
    if (mem.incidents?.length) parts.push(`<b>${mem.incidents.length} past incident(s) near this fault</b>: ` + mem.incidents.slice(0, 3).map((i) => `${esc(i.id)} ${esc(i.status)} via ${esc(i.source)}`).join('; '));
    if (mem.recorded_incident) parts.push(`<b>Recorded:</b> ${esc(mem.recorded_incident)} → playbook ${esc(mem.signature)} now ${mem.playbook?.success_count ?? 0}✓/${mem.playbook?.failure_count ?? 0}✗`);
    return parts.join('<br>') || 'No related memory yet.';
  }
  document.querySelectorAll('[data-scn]').forEach((b) => (b.onclick = () => busy(b, async () => {
    await api(`/api/sim/trigger-scenario/${b.dataset.scn}?mode=${$('mode').value}`, { method: 'POST', body: '{}' });
    toast('Scenario started', true);
  })));
  $('reset').onclick = () => busy($('reset'), async () => { await api('/api/sim/reset', { method: 'POST', body: '{}' }); await loadLiveTopology(); toast('Grid reset', true); });

  // ---------------- network builder (click-to-draw editor) ----------------
  let validateTimer = null;
  const editor = GridEditor.create('cyNet', {
    onChange: (spec) => {
      $('spec').value = JSON.stringify(spec, null, 1);
      const c = editor.counts();
      $('edCounts').textContent = `${c.generators} generators · ${c.buses} buses · ${c.substations} substations · ${c.lines} lines · ${c.loads} loads · ${c.batteries} batteries`;
      $('specMsg').textContent = currentNet ? 'unsaved changes' : 'not saved yet'; $('specMsg').className = 'small warn';
      clearTimeout(validateTimer); validateTimer = setTimeout(liveValidate, 600);
    },
    onSelect: renderProps,
    onStatus: (msg, cls) => { $('edHelp').textContent = msg; $('edHelp').className = cls ? cls : 'muted'; },
  });
  async function liveValidate() {
    if (!token) { $('edValid').textContent = 'sign in to validate'; $('edValid').className = 'pill'; return; }
    try {
      const r = await api('/api/networks/validate', { method: 'POST', body: JSON.stringify({ spec: editor.getSpec() }) });
      $('edValid').textContent = r.warnings.length ? `valid · ${r.warnings.length} warning(s)` : 'valid ✓';
      $('edValid').className = 'pill ' + (r.warnings.length ? 'warn' : 'ok'); $('edValid').title = r.warnings.join('\n');
    } catch (e) {
      const n = (e.message.match(/•/g) || []).length;
      $('edValid').textContent = `${n || 1} problem(s)`; $('edValid').className = 'pill bad'; $('edValid').title = e.message;
    }
  }
  $('edValid').onclick = () => $('edValid').title && toast($('edValid').title, $('edValid').classList.contains('ok'));
  function renderProps(sel) {
    if (!sel) { $('edProps').innerHTML = '<span class="muted">Select a part or wire on the canvas to edit it.</span>'; return; }
    const labels = {id:'ID',name:'Name',capacity_mw:'Capacity (MW)',output_mw:'Output (MW)',mw_demand:'Demand (MW)',priority_tier:'Priority',max_discharge_mw:'Max output (MW)',soc_pct:'Charge (%)',from:'From',to:'To',limit_mw:'Limit (MW)',connects_to:'Connected to',substation:'Substation',impedance_pu:'Impedance',backup_limit_mw:'Backup limit (MW)',backup_impedance_pu:'Backup impedance',breaker_id:'Breaker ID'};
    const primary = {buses:['id','name'],substations:['id','name','capacity_mw'],generators:['id','name','output_mw','capacity_mw'],loads:['id','name','mw_demand','priority_tier'],batteries:['id','max_discharge_mw','soc_pct'],lines:['id','from','to','limit_mw']}[sel.kind] || [];
    const field = (f) => f.type === 'tier'
      ? `<label>${labels[f.key]||f.key}<select data-k="${f.key}">${[1, 2, 3].map((t) => `<option value="${t}" ${Number(f.value) === t ? 'selected' : ''}>${t} ${['', 'critical', 'protected', 'flexible'][t]}</option>`).join('')}</select></label>`
      : `<label>${labels[f.key]||f.key}<input data-k="${f.key}" type="${f.type === 'number' ? 'number' : 'text'}" step="any" value="${esc(f.value)}"></label>`;
    const basic = sel.fields.filter(f=>primary.includes(f.key));
    const advanced = sel.fields.filter(f=>!primary.includes(f.key));
    $('edProps').innerHTML = `<b>${esc(sel.label)} ${esc(sel.id)}</b><div class="property-grid">${basic.map(field).join('')}</div>` +
      (advanced.length ? `<details><summary>More settings</summary><div class="property-grid">${advanced.map(field).join('')}</div></details>` : '') +
      `<div class="row"><button class="b p" id="propApply">Apply</button><button class="b d" id="propDelete">Delete</button></div>`;
    $('propApply').onclick = () => {
      const values = {}; $('edProps').querySelectorAll('[data-k]').forEach((i) => (values[i.dataset.k] = i.value));
      try { editor.applyProps(sel.kind, sel.id, values); } catch (e) { toast(e.message); }
    };
    $('propDelete').onclick = () => editor.remove(sel.kind, sel.id);
  }
  document.querySelectorAll('#edModes [data-mode]').forEach((b) => (b.onclick = () => {
    document.querySelectorAll('#edModes [data-mode]').forEach((x) => x.classList.toggle('on', x === b)); editor.setMode(b.dataset.mode);
  }));
  const builderCanvas = $('cyNet');
  document.querySelectorAll('#edModes [draggable=true]').forEach(button => {
    button.addEventListener('dragstart', event => {
      event.dataTransfer.setData('application/x-gridpulse-part', button.dataset.mode);
      event.dataTransfer.effectAllowed = 'copy';
    });
  });
  builderCanvas.addEventListener('dragover', event => {
    if (!event.dataTransfer.types.includes('application/x-gridpulse-part')) return;
    event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; builderCanvas.classList.add('builder-drop-ready');
  });
  builderCanvas.addEventListener('dragleave', event => { if (!builderCanvas.contains(event.relatedTarget)) builderCanvas.classList.remove('builder-drop-ready'); });
  builderCanvas.addEventListener('drop', event => {
    const kind = event.dataTransfer.getData('application/x-gridpulse-part');
    builderCanvas.classList.remove('builder-drop-ready');
    if (!kind) return;
    event.preventDefault();
    document.querySelector('#edModes [data-mode="select"]').click();
    editor.addFromPalette(kind, event.clientX, event.clientY);
  });
  $('edFit').onclick = () => editor.fit();
  $('edZoomIn').onclick = () => editor.zoomBy(1.25);
  $('edZoomOut').onclick = () => editor.zoomBy(0.8);
  const builderWorkspace = $('builder');
  let builderFallbackFullscreen = false;
  function syncBuilderFullscreen() {
    const expanded = document.fullscreenElement === builderWorkspace || builderFallbackFullscreen;
    $('edFullscreen').textContent = expanded ? '↙ Exit full screen' : '⛶ Full screen';
    $('edFullscreen').setAttribute('aria-pressed', String(expanded));
    requestAnimationFrame(() => { editor.resize(); requestAnimationFrame(() => editor.resize()); });
  }
  $('edFullscreen').onclick = async () => {
    if (document.fullscreenElement === builderWorkspace) { await document.exitFullscreen(); return; }
    if (builderFallbackFullscreen) { builderFallbackFullscreen = false; builderWorkspace.classList.remove('builder-fullscreen-fallback'); document.body.classList.remove('builder-fullscreen-active'); syncBuilderFullscreen(); return; }
    try { await builderWorkspace.requestFullscreen(); }
    catch { builderFallbackFullscreen = true; builderWorkspace.classList.add('builder-fullscreen-fallback'); document.body.classList.add('builder-fullscreen-active'); syncBuilderFullscreen(); }
  };
  document.addEventListener('fullscreenchange', syncBuilderFullscreen);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && builderFallbackFullscreen) {
      builderFallbackFullscreen = false; builderWorkspace.classList.remove('builder-fullscreen-fallback'); document.body.classList.remove('builder-fullscreen-active'); syncBuilderFullscreen();
    }
  });
  $('edUndo').onclick = () => editor.undoLast() || toast('Nothing to undo', true);
  $('edLayout').onclick = () => editor.autoLayout();
  document.addEventListener('keydown', (e) => {
    if (!$('builder').classList.contains('on') || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
    if ((e.metaKey || e.ctrlKey) && e.key === 'z') { e.preventDefault(); editor.undoLast(); }
  });
  function loadIntoEditor(spec, label) {
    editor.load(spec); $('edName').textContent = label || spec.name || '';
    $('spec').value = JSON.stringify(editor.getSpec(), null, 1);
    const c = editor.counts();
    $('edCounts').textContent = `${c.generators} generators · ${c.buses} buses · ${c.substations} substations · ${c.lines} lines · ${c.loads} loads · ${c.batteries} batteries`;
    liveValidate();
  }
  $('specApply').onclick = () => { try { loadIntoEditor(JSON.parse($('spec').value)); } catch (e) { toast('Spec is not valid JSON: ' + e.message); } };

  async function loadNetworks(selectId) {
    if (!token) return;
    const [list, tpl] = await Promise.all([api('/api/networks'), api('/api/networks/templates')]);
    $('tplSel').innerHTML = tpl.templates.map((t) => `<option value="${t.id}" title="${esc(t.description)}">${esc(t.name)} (${t.counts.buses + t.counts.substations} buses)</option>`).join('');
    $('netSel').innerHTML = '<option value="">— your saved networks —</option>' + list.networks.map((n) => `<option value="${n.id}">${esc(n.name)} · ${n.id}</option>`).join('');
    const opts = '<option value="live">Live demo grid</option>' + list.networks.map((n) => `<option value="${n.id}">${esc(n.name)} · ${n.id}</option>`).join('');
    for (const sel of ['anTarget', 'askTarget', 'memTarget']) { const keep = $(sel).value; $(sel).innerHTML = opts; if ([...$(sel).options].some((o) => o.value === keep)) $(sel).value = keep; }
    if (selectId) { $('netSel').value = selectId; await openNetwork(selectId); }
  }
  async function openNetwork(id) {
    if (!id) return;
    const [net, topo] = await Promise.all([api(`/api/networks/${id}`), api(`/api/networks/${id}/topology`)]);
    currentNet = { id, spec: net.spec, topo };
    loadIntoEditor(net.spec, `${net.name} · ${id}`);
    $('specMsg').textContent = 'saved ✓'; $('specMsg').className = 'small ok'; $('simNet').textContent = `${net.name} · ${id}`;
    $('netInfo').innerHTML = `Graph <span class="mono">gridpulse_net_${id.toLowerCase()}</span> · ${topo.counts.nodes} nodes, ${topo.counts.edges} edges` + (net.warnings.length ? ` · <span class="warn">${esc(net.warnings.join('; '))}</span>` : '');
    draw('cySim', topo, describe);
    cases = []; presetCases();
  }
  function startUnsaved(spec, label) { currentNet = null; $('netSel').value = ''; loadIntoEditor(spec, label); $('specMsg').textContent = 'not saved yet'; $('specMsg').className = 'small warn'; }
  $('netSel').onchange = () => busy($('netSel'), () => openNetwork($('netSel').value));
  $('netRefresh').onclick = () => busy($('netRefresh'), () => loadNetworks(currentNet?.id));
  $('netTemplate').onclick = () => busy($('netTemplate'), async () => { const t = await api(`/api/networks/templates/${$('tplSel').value}`); startUnsaved(t.spec, `template: ${t.spec.name}`); });
  $('netBlank').onclick = () => startUnsaved({ name: 'My grid' }, 'New unsaved network');
  $('netDelete').onclick = () => busy($('netDelete'), async () => {
    if (!currentNet || !confirm(`Delete ${currentNet.id}?`)) return;
    await api(`/api/networks/${currentNet.id}`, { method: 'DELETE' }); currentNet = null; cases = []; editor.load({}); await loadNetworks(); toast('Deleted', true);
  });
  async function save(asNew) {
    const spec = editor.getSpec();
    const n = (currentNet && !asNew) ? await api(`/api/networks/${currentNet.id}`, { method: 'PUT', body: JSON.stringify({ spec }) })
      : await api('/api/networks', { method: 'POST', body: JSON.stringify({ spec }) });
    await loadNetworks(n.id); toast(`Saved ${n.id}`, true);
  }
  $('specSave').onclick = () => busy($('specSave'), () => save(false));
  $('specSaveNew').onclick = () => busy($('specSaveNew'), () => save(true));

  async function importContent(content, format, name) {
    const tier1 = $('impTier1').value.trim();
    const body = { format, content, name, battery_mw: Number($('impBattery').value) || 0 };
    if (tier1) body.tier1_buses = tier1.split(/[,\s]+/).map(Number).filter((x) => Number.isInteger(x));
    const r = await api('/api/networks/convert', { method: 'POST', body: JSON.stringify(body) });
    startUnsaved(r.spec, `imported: ${r.spec.name}`);
    $('impOut').innerHTML = (r.summary ? `<b>${r.summary.buses} buses · ${r.summary.branches} branches · ${r.summary.generators} generators · ${r.summary.loads} loads</b><br>` : '') +
      (r.notes || []).map((n) => `• ${esc(n)}`).join('<br>') + (r.warnings.length ? `<br><span class="warn">${esc(r.warnings.join('; '))}</span>` : '') +
      '<br><span class="ok">Loaded into the editor. Click “Save as new” to store it in FalkorDB.</span>';
  }
  $('impFile').onchange = () => busy($('impFile'), async () => {
    const f = $('impFile').files[0]; if (!f) return;
    if (f.size > 5e6) throw new Error('File too large (max 5 MB)');
    const fmt = f.name.endsWith('.json') ? 'gridpulse' : $('impFormat').value;
    $('impFormat').value = fmt;
    await importContent(await f.text(), fmt, f.name.replace(/\.[^.]+$/, ''));
  });
  $('impSampleBtn').onclick = () => busy($('impSampleBtn'), async () => {
    const name = $('impSample').value;
    const res = await fetch(`${API}/api/networks/samples/${name}`); if (!res.ok) throw new Error('sample not found');
    await importContent(await res.text(), 'matpower', `${name.toUpperCase()} (imported)`);
  });

  // ---------------- test cases ----------------
  function lineIds() { return (currentNet?.spec?.lines || []).map((l) => l.id); }
  function presetCases() {
    const lines = currentNet?.spec?.lines || [];
    const busiest = lines.slice(0, 1).map((l) => l.id);
    cases = [{ name: 'Base case' }, { name: `Lose ${busiest[0] || 'a line'}`, trip: busiest.map((id) => ({ span_id: id, circuit: 'UPPER_PRIMARY' })) },
      { name: 'Heatwave +20%', demand_scale: 1.2 }, { name: 'N-1 scan', type: 'n1' }];
    renderCases();
  }
  $('presetCases').onclick = presetCases;
  $('addCase').onclick = () => { cases.push({ name: `Case ${cases.length + 1}`, trip: [] }); renderCases(); };
  function renderCases() {
    const opts = lineIds();
    $('cases').innerHTML = cases.map((c, i) => `
      <div class="case" data-i="${i}">
        <div class="row small"><input data-k="name" value="${esc(c.name)}" size="18">
          <select data-k="type"><option value="scenario" ${c.type !== 'n1' ? 'selected' : ''}>scenario</option><option value="n1" ${c.type === 'n1' ? 'selected' : ''}>N-1 scan</option></select>
          <label>demand × <input data-k="demand_scale" type="number" step="0.05" value="${c.demand_scale ?? ''}" placeholder="1.0"></label>
          <button class="b d" data-del="${i}">✕</button></div>
        <div class="row small">trip: <select data-k="trip" multiple size="3" style="min-width:160px">${opts.map((id) => `<option ${(c.trip || []).some((t) => t.span_id === id) ? 'selected' : ''}>${id}</option>`).join('')}</select>
          <span>battery MW <input data-k="bess" placeholder="BESS_1=40" value="${esc(Object.entries(c.bess_injection_mw || {}).map(([k, v]) => `${k}=${v}`).join(','))}" size="14"></span>
          <span>shed <input data-k="deenergize" placeholder="LOAD_A,LOAD_B" value="${esc((c.deenergize || []).join(','))}" size="14"></span></div>
      </div>`).join('') || '<p class="muted small">Pick a network, then add cases or load presets.</p>';
    $('cases').querySelectorAll('[data-del]').forEach((b) => (b.onclick = () => { cases.splice(+b.dataset.del, 1); renderCases(); }));
    $('cases').querySelectorAll('.case').forEach((div) => div.addEventListener('change', () => {
      const c = cases[+div.dataset.i];
      c.name = div.querySelector('[data-k=name]').value; c.type = div.querySelector('[data-k=type]').value;
      const ds = div.querySelector('[data-k=demand_scale]').value; c.demand_scale = ds ? Number(ds) : undefined;
      c.trip = [...div.querySelector('[data-k=trip]').selectedOptions].map((o) => ({ span_id: o.value, circuit: 'UPPER_PRIMARY' }));
      c.bess_injection_mw = Object.fromEntries(div.querySelector('[data-k=bess]').value.split(',').map((x) => x.split('=')).filter((x) => x[0] && x[1]).map(([k, v]) => [k.trim().toUpperCase(), Number(v)]));
      c.deenergize = div.querySelector('[data-k=deenergize]').value.split(',').map((x) => x.trim().toUpperCase()).filter(Boolean);
    }));
  }
  $('runCases').onclick = () => busy($('runCases'), async () => {
    if (!currentNet) throw new Error('Pick or create a network in Network Builder first');
    const body = { threshold_pct: Number($('thr').value) || 95, cases: cases.map((c) => Object.fromEntries(Object.entries(c).filter(([, v]) => v !== undefined && v !== '' && !(Array.isArray(v) && !v.length) && !(typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length)))) };
    const r = await api(`/api/networks/${currentNet.id}/simulate`, { method: 'POST', body: JSON.stringify(body) });
    renderSim(r);
  });
  $('rehearse').onclick = () => busy($('rehearse'), async () => {
    if (!currentNet) throw new Error('Pick or create a network in Network Builder first');
    $('rehearseOut').innerHTML = '<p class="muted">Rehearsing critical outages and demand surges…</p>';
    const r = await api(`/api/networks/${currentNet.id}/rehearse`, { method: 'POST', body: JSON.stringify({ max_cases: 40 }) });
    $('rehearseOut').innerHTML = `<p>🎓 Rehearsed ${r.cases} cases in ${fmt(r.timing.engine_total_ms / 1000, 1)}s: ${r.saved} saved (${r.solved_from_memory} straight from memory), ${r.not_saveable} not saveable, ${r.skipped_within_limits} fine as is. Memory now holds ${r.memory.playbooks} playbooks (${r.memory.generalized_reuses} generalised reuses). Run the simulation again: cases now start from memory.</p>`;
  });
  function renderSim(r) {
    const rows = r.cases.map((c, i) => {
      if (c.type === 'n1') return `<tr><td>${esc(c.name)}</td><td>${outcomePill(c.n1_secure ? 'N-1 SECURE' : 'N-1 INSECURE')}</td><td colspan="3">${c.critical_outages.length} of ${c.contingency_count} single outages break limits: <span class="mono">${esc(c.critical_outages.slice(0, 12).join(', '))}${c.critical_outages.length > 12 ? '…' : ''}</span></td><td></td></tr>`;
      const m = c.mitigation;
      const plan = m.plan ? [...Object.entries(m.plan.bess_dispatch || {}).map(([k, v]) => `${k} → ${fmt(v)} MW`), ...(m.plan.trip_breakers || []).map((b) => `open ${b}`)].join('<br>') : '';
      const memo = m.from_memory ? `<div class="ok small">↺ from memory: ${esc(m.playbook)} (${esc(m.why)})</div>` : '';
      return `<tr data-case="${i}" style="cursor:pointer"><td>${esc(c.name)}</td><td>${outcomePill(c.outcome)}</td>
        <td class="mono">${c.overloads.map((o) => `${esc(o.element)} ${fmt(o.loading_pct)}%`).join('<br>') || '—'}${c.islanded_substations.length ? `<br><span class="warn">islanded: ${esc(c.islanded_substations.join(', '))}</span>` : ''}</td>
        <td>${c.required_relief_mw ?? '—'} MW</td><td>${c.tier1.map((t) => `<span class="${t.supplied ? 'ok' : 'bad'}">${esc(t.id)}</span>`).join(', ') || '—'}</td>
        <td>${outcomePill(m.verdict)}${memo}<div class="mono small">${plan || ''}</div>${m.reason ? `<div class="warn small">${esc(m.reason)}</div>` : ''}</td></tr>`;
    }).join('');
    $('simOut').innerHTML = `<p>${r.summary.cases} cases · ${r.summary.within_limits} within limits · ${r.summary.saved_by_mitigation} saved by mitigation (${r.summary.solved_from_memory || 0} from memory) · ${r.summary.not_saveable} not saveable · ${fmt(r.timing.engine_total_ms, 0)} ms · model ${esc(r.model)} · click a row to highlight it</p>
      <table><tr><th>Case</th><th>Outcome</th><th>Overloads</th><th>Relief</th><th>Tier-1</th><th>Suggested fix</th></tr>${rows}</table>`;
    $('simOut').querySelectorAll('[data-case]').forEach((tr) => (tr.onclick = () => highlightCase(r.cases[+tr.dataset.case], cases[+tr.dataset.case])));
  }
  function highlightCase(res, input) {
    const cy = cys.cySim; if (!cy) return;
    cy.elements().removeClass('over tripped islanded off hl');
    for (const o of res.overloads) { const e = cy.getElementById(o.element); e.addClass('over'); e.data('load', `${fmt(o.loading_pct, 0)}%`); cy.getElementById(o.element).addClass('hl'); }
    for (const t of input?.trip || []) cy.getElementById(`${t.span_id}:${t.circuit}`).addClass('tripped');
    for (const s of res.islanded_substations) cy.getElementById(s).addClass('islanded');
    for (const l of res.mitigation?.plan?.shed_loads || []) cy.getElementById(l).addClass('off');
  }

  // ---------------- analysis ----------------
  const target = () => $('anTarget').value;
  const base = () => target() === 'live' ? '/api/graph' : `/api/networks/${target()}`;
  $('anN1').onclick = () => busy($('anN1'), async () => {
    const r = await api(`${base()}/contingency`, { method: 'POST', body: '{}' });
    $('anN1Out').innerHTML = `<p>${outcomePill(r.n1_secure ? 'N-1 SECURE' : 'N-1 INSECURE')} ${r.critical_outages.length}/${r.contingency_count} critical · ${fmt(r.timing.engine_total_ms, 0)} ms (tags written to the graph)</p>
      <table><tr><th>Outage</th><th>Outcome</th><th>Worst element</th><th>%</th></tr>${r.contingencies.slice(0, 25).map((c) => `<tr><td class="mono">${esc(c.outage)}</td><td>${outcomePill(c.outcome)}</td><td class="mono">${esc(c.worst_element)}</td><td>${fmt(c.worst_loading_pct)}</td></tr>`).join('')}</table>`;
  });
  $('anCrit').onclick = () => busy($('anCrit'), async () => {
    const r = await api(`${base()}/criticality?limit=12`);
    const max = Math.max(...r.ranking.map((x) => x.betweenness), 1);
    $('anCritOut').innerHTML = r.ranking.map((x) => `<div class="row"><span class="mono" style="width:90px">${esc(x.id)}</span><span style="background:#0e7490;height:8px;width:${Math.round(160 * x.betweenness / max)}px;border-radius:4px"></span><span>${fmt(x.betweenness, 0)}</span>${x.n1_critical_circuits.length ? `<span class="warn small">N-1: ${esc(x.n1_critical_circuits.slice(0, 2).join(', '))}</span>` : ''}</div>`).join('');
  });
  $('anIsl').onclick = () => busy($('anIsl'), async () => {
    const [islands, zones] = await Promise.all([api(`${base()}/islands`), api(`${base()}/zones`)]);
    $('anIslOut').innerHTML = `<p>${islands.connected ? pill('connected', 'ok') : pill(`${islands.components.length} parts`, 'warn')}</p>` +
      islands.components.map((c) => `<div>${c.has_generator ? '⚡' : '⛔ no generator'} ${c.size} buses: <span class="mono">${esc(c.members.slice(0, 12).join(', '))}${c.size > 12 ? '…' : ''}</span></div>`).join('') +
      '<h3>Zones</h3>' + zones.zones.slice(0, 12).map((z) => `<div>zone ${z.zone} (${z.size}): <span class="mono">${esc(z.members.slice(0, 14).join(', '))}${z.size > 14 ? '…' : ''}</span></div>`).join('');
  });
  $('anRr').onclick = () => busy($('anRr'), async () => {
    const q = new URLSearchParams({ source_id: $('rrFrom').value.trim().toUpperCase(), target_id: $('rrTo').value.trim().toUpperCase(), k: 3 });
    const r = await api(`${base()}/reroute?${q}`);
    $('anRrOut').innerHTML = `<p>${outcomePill(r.outcome)} examined ${r.candidates_examined} candidates</p>` + r.paths.map((p, i) => `<div class="case"><b>#${i + 1}</b> impedance ${fmt(p.impedance_pu, 4)} pu<div class="mono">${esc(p.nodes.join(' → '))}</div><div class="mono muted">${esc(p.circuits.join(', '))}</div></div>`).join('');
  });
  $('anTrace').onclick = () => busy($('anTrace'), async () => {
    const origin = $('trOrigin').value.trim().toUpperCase();
    const body = { origin_id: origin, max_hops: 8, assume_tripped: $('trTrip').checked && !/^T\d|^SUB|^B\d/.test(origin) ? [{ span_id: origin, circuit: 'UPPER_PRIMARY' }] : [] };
    const r = await api(target() === 'live' ? '/api/graph/trace-cascade' : `${base()}/trace`, { method: 'POST', body: JSON.stringify(body) });
    const s = r.summary;
    $('anTraceOut').innerHTML = `<p>${outcomePill(s.projection_outcome)} relief ${s.required_relief_mw ?? '—'} MW · downstream ${esc(s.downstream_substations.join(', ') || 'none')} · Tier-1 at risk ${esc(s.tier1_at_risk.join(', ') || 'none')} · ${fmt(r.timing.graph_server_ms, 3)} ms graph</p>
      <div class="mono">${r.reachability.nodes.map((n) => `${n.id}@${n.hops}`).join(' · ')}</div>
      <details><summary>Cypher evidence (${r.evidence.length} queries)</summary>${r.evidence.map((e) => `<pre>${esc(e.cypher)}\nparams ${esc(JSON.stringify(e.params))}\n${e.row_count} rows · ${e.server_ms} ms · ${e.evidence_hash}</pre>`).join('')}</details>`;
  });

  // ---------------- agent ----------------
  $('ask').onclick = () => busy($('ask'), async () => {
    $('agentOut').innerHTML = '<p class="muted">Agent investigating… (Groq)</p>'; $('agentCalls').innerHTML = '';
    try {
      const r = await api('/api/mcp/agent/advise', { method: 'POST', body: JSON.stringify({ question: $('q').value, execute: $('exec').checked }) });
      $('agentOut').innerHTML = `<p>${outcomePill(r.validation.verdict)} ${esc(r.summary)}</p><p class="muted">${esc(r.rationale)}</p>
        <div class="mono">${esc(JSON.stringify(r.plan))}</div>${r.validation.reasons.length ? `<p class="bad">${esc(r.validation.reasons.join('; '))}</p>` : ''}
        <p class="muted small">${esc(r.model)} · ${r.usage.input_tokens}+${r.usage.output_tokens} tokens · ${r.elapsed_s}s · audit ${esc(r.audit_id)}${r.executed?.length ? ` · executed ${r.executed.length} action(s)` : ''}${r.execution_blocked ? ` · execution blocked: ${esc(r.execution_blocked.join('; '))}` : ''}</p>`;
      $('agentCalls').innerHTML = r.tool_calls.map((c) => `<div class="step ${c.error ? 'tool_error' : ''}"><b>🔧 ${esc(c.tool)}</b><div class="mono">${esc(JSON.stringify(c.input))}</div>${c.error ? `<div class="bad">${esc(c.error)}</div>` : `<div class="muted mono">${esc((c.evidence_hashes || []).join(' '))}</div>`}</div>`).join('');
    } catch (e) {
      $('agentOut').innerHTML = `<p class="bad">${esc(e.message)}</p>${e.status === 503 ? '<p class="muted">Set GROQ_API_KEY in .env and restart the backend to enable the agent.</p>' : ''}`;
    }
  });

  // ---------------- memory & learning ----------------
  const SOURCE_COLOR = { INTERCEPTOR: '#64748b', PLAYBOOK: '#10b981', AGENT: '#a855f7', REHEARSAL: '#f59e0b' };
  async function loadMemory() {
    try {
      const t = $('memTarget').value;
      const data = t === 'live'
        ? await Promise.all([api('/api/graph/memory/learning'), api('/api/graph/memory/playbooks')]).then(([c, p]) => ({ ...c, playbooks: p.playbooks }))
        : await api(`/api/networks/${t}/memory`);
      const sm = data.summary;
      $('memSummary').innerHTML = [['incidents', sm.incidents], ['mitigated', sm.mitigated], ['solved from memory', sm.solved_from_memory],
        ['generalised reuses', sm.generalized_reuses], ['playbooks', sm.playbooks], ['retired', sm.retired_playbooks]]
        .map(([k, v]) => `<span class="pill">${k}: <b>${v}</b></span>`).join(' ');
      const inc = data.incidents;
      const maxSteps = Math.max(1, ...inc.map((i) => i.steps || 1));
      $('memCurve').innerHTML = inc.length ? `<div style="display:flex;align-items:flex-end;gap:3px;height:120px;border-bottom:1px solid var(--line);padding-top:6px;overflow-x:auto">` +
        inc.map((i, n) => {
          const color = i.status === 'FAILED' ? '#ef4444' : i.learned_from && i.source !== 'AGENT' ? (i.learned_from === i.signature ? '#10b981' : '#22d3ee') : (SOURCE_COLOR[i.source] || '#64748b');
          const h = Math.max(6, Math.round(110 * (i.steps || 1) / maxSteps));
          return `<div title="#${n + 1} ${esc(i.signature)}\n${esc(i.source)} ${esc(i.status)}\nsteps ${i.steps ?? '?'} · ${i.mitigation_ms ?? '?'} ms · shed ${i.shed_mw ?? 0} MW${i.learned_from ? '\nfrom playbook ' + esc(i.learned_from) : ''}" style="flex:0 0 16px;height:${h}px;background:${color};border-radius:3px 3px 0 0"></div>`;
        }).join('') + '</div><p class="small muted">Bar height = decision steps per incident (lower is faster). Hover a bar for details.</p>' : '<p class="muted">No incidents yet. Run scenarios in Live Ops, or Rehearse a network in Test Cases.</p>';
      $('memInc').innerHTML = inc.length ? `<table><tr><th>#</th><th>Trigger</th><th>How</th><th>Outcome</th><th>Steps</th><th>ms</th><th>Shed MW</th></tr>${inc.slice().reverse().slice(0, 40).map((i, k) => `<tr><td>${inc.length - k}</td><td class="mono">${esc(i.signature)}</td><td>${esc(i.source)}${i.learned_from ? `<div class="small ok">↺ ${esc(i.learned_from)}</div>` : ''}</td><td>${outcomePill(i.status)}</td><td>${i.steps ?? '—'}</td><td>${i.mitigation_ms ?? '—'}</td><td>${i.shed_mw ?? 0}</td></tr>`).join('')}</table>` : '';
      $('memPb').innerHTML = data.playbooks.length ? `<table><tr><th>Trigger</th><th>W/L</th><th>Generalised</th><th>Plan</th><th></th></tr>${data.playbooks.map((p) => `<tr><td class="mono">${esc(p.signature)}</td><td><span class="ok">${p.success_count}</span>/<span class="bad">${p.failure_count}</span></td><td>${p.generalized || 0}</td><td class="mono">${p.plan ? esc(JSON.stringify(p.plan)) : '<span class="muted">no working plan</span>'}</td><td>${p.retired ? pill('retired', 'bad') : ''}</td></tr>`).join('')}</table>` : '<p class="muted">No playbooks yet.</p>';
    } catch (e) { toast('Memory: ' + e.message); }
  }
  $('memLoad').onclick = () => busy($('memLoad'), loadMemory);
  $('memTarget').onchange = () => loadMemory();
  $('recall').onclick = () => busy($('recall'), async () => {
    if ($('memTarget').value !== 'live') throw new Error('Suggestions lookup runs on the live grid; for networks use Test Cases (memory is used automatically)');
    const r = await api(`/api/graph/memory/suggest?origin_id=${encodeURIComponent($('recallId').value.trim().toUpperCase())}`);
    $('recallOut').innerHTML = `<p>anchors ${esc(r.anchors.join(', '))} · ${r.considered} playbooks considered · ${fmt(r.timing.graph_server_ms, 3)} ms</p>` +
      (r.suggestions.length ? r.suggestions.map((sg) => `<div class="case">${pill(`score ${sg.score}`, sg.exact ? 'ok' : 'warn')} <span class="mono">${esc(sg.signature)}</span><div>${esc(sg.why)}</div><div class="mono small">${esc(JSON.stringify(sg.plan))}</div></div>`).join('') : '<p class="muted">Nothing similar in memory yet.</p>');
  });

  // ---------------- ask the grid ----------------
  $('askExamples').addEventListener('click', (e) => { if (e.target.tagName === 'BUTTON') { $('askQ').value = e.target.textContent; $('askGo').click(); } });
  $('askGo').onclick = () => busy($('askGo'), async () => {
    $('askOut').innerHTML = '<p class="muted">Thinking…</p>';
    const target = $('askTarget').value;
    try {
      const r = await api('/api/mcp/ask', { method: 'POST', body: JSON.stringify({ question: $('askQ').value, network_id: target === 'live' ? null : target }) });
      if (!r.answerable) { $('askOut').innerHTML = `<p class="warn">${esc(r.explanation)}</p>`; return; }
      const cols = r.columns;
      const cell = (v) => typeof v === 'object' && v !== null ? esc(v.path ? v.path.join(' → ') : JSON.stringify(v).slice(0, 120)) : esc(v);
      $('askOut').innerHTML = `<p><b>${esc(r.answer || '')}</b></p><pre>${esc(r.cypher)}</pre>
        <p class="muted">${esc(r.explanation)} · ${r.row_count} row(s) · ${fmt(r.server_ms, 3)} ms in FalkorDB · ${r.elapsed_s}s total · ${esc(r.evidence_hash.slice(0, 23))}…${r.attempts > 1 ? ' · corrected after 1 failed attempt' : ''}</p>` +
        (r.rows.length ? `<table><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr>${r.rows.slice(0, 50).map((row) => `<tr>${cols.map((c) => `<td class="mono">${cell(row[c])}</td>`).join('')}</tr>`).join('')}</table>` : '');
      const topo = target === 'live' ? await api('/api/graph/topology') : await api(`/api/networks/${target}/topology`);
      const cy = draw('cyAsk', topo, describe);
      const ids = new Set(r.highlight_ids);
      cy.nodes().forEach((n) => { if (ids.has(n.id())) n.addClass('hl'); });
      cy.edges().forEach((e) => { const p = e.data('props') || {}; if (ids.has(p.id)) e.addClass('path'); });
    } catch (e) {
      $('askOut').innerHTML = `<p class="bad">${esc(e.message)}</p>${e.status === 503 ? '<p class="muted">Set GROQ_API_KEY in .env and restart the backend.</p>' : ''}`;
    }
  });

  // ---------------- benchmark ----------------
  $('bRun').onclick = () => busy($('bRun'), async () => {
    $('bOut').innerHTML = '<p class="muted">Running…</p>';
    const r = await api('/api/graph/benchmark', { method: 'POST', body: JSON.stringify({ size: Number($('bSize').value), max_hops: Number($('bHops').value), runs: Number($('bRuns').value) }) });
    const rows = Object.entries(r.engines).map(([k, v]) => `<tr><td class="mono">${k}</td><td>${v.engine_time.median_ms}</td><td>${v.engine_time.p95_ms}</td><td>${v.client_roundtrip.median_ms}</td></tr>`).join('');
    $('bOut').innerHTML = `<p>${r.grid.buses} buses · ${r.grid.lines} lines · ${r.max_hops} hops · median reach ${r.median_reach_size} buses · ${outcomePill(r.results_verified_equal ? 'RESULTS EQUAL' : 'MISMATCH')} ${r.load.reused ? '(graph reused)' : `(loaded in ${r.load.load_s}s)`}</p>
      <table><tr><th>Engine</th><th>median ms</th><th>p95 ms</th><th>round trip median ms</th></tr>${rows}</table>
      ${r.n1_lodf ? `<p>LODF N-1 on this mesh: ${r.n1_lodf.branches} outages in ${r.n1_lodf.n1_scan_ms} ms (factorisation ${r.n1_lodf.factorization_ms} ms), ${r.n1_lodf.insecure_outages} insecure</p>` : ''}
      <details><summary>Queries</summary><pre>${esc(Object.values(r.queries).join('\n\n'))}</pre></details><p class="muted">${esc(r.notes.join(' '))}</p>`;
  });

  // ---------------- audit ----------------
  async function loadAudit() {
    try {
      const logs = await api('/api/audit/logs?limit=60');
      $('auOut').innerHTML = `<table><tr><th>seq</th><th>time</th><th>action</th><th>target</th><th>outcome</th><th>operator</th><th>state v</th><th>record hash</th></tr>${logs.map((l) => `<tr><td>${l.seq ?? '—'}</td><td>${esc((l.timestamp || '').slice(11, 19))}</td><td class="mono">${esc(l.action)}</td><td class="mono">${esc(l.target)}</td><td>${outcomePill(l.outcome)}</td><td>${esc(l.operator_id)}</td><td>${l.state_version ?? '—'}</td><td class="mono muted">${esc((l.record_hash || '').slice(0, 16))}</td></tr>`).join('')}</table>`;
    } catch (e) { toast('Audit: ' + e.message); }
  }
  $('auLoad').onclick = () => busy($('auLoad'), loadAudit);
  $('auVerify').onclick = () => busy($('auVerify'), async () => {
    const r = await api('/api/audit/verify');
    $('auVerifyOut').innerHTML = r.valid ? `${pill('CHAIN VALID', 'ok')} ${r.checked} records${r.legacy_unchained ? `, ${r.legacy_unchained} legacy` : ''}` : `${pill('BROKEN', 'bad')} at seq ${r.first_break.seq}: ${esc(r.first_break.problem)}`;
  });

  // ---------------- boot ----------------
  showWho();
  loadLiveTopology();
  const requestedTab=location.hash.slice(1);
  if(document.querySelector(`#tabs button[data-t="${requestedTab}"]`))document.querySelector(`#tabs button[data-t="${requestedTab}"]`).click();
  if (token) { api('/api/auth/session').then(() => { connectWs(); loadNetworks(); }).catch(() => { token = null; sessionStorage.removeItem('gp_lab_token'); showWho(); toast('Session expired: sign in again', true); }); }
  else toast('Sign in (top right) to use live controls, networks and the agent.', true);
})();
