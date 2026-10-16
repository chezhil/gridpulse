/* GridPulse click-to-draw network editor (Cytoscape). Edits a GridPulse network spec in place.
 *
 * Modes: select (edit properties, drag to arrange), bus / substation (click empty canvas),
 * generator / load / battery (click the bus or substation to attach to), line (click two
 * buses/substations; clicking a generator then a bus re-connects the generator), delete.
 * Positions are stored in spec.layout so a drawn network keeps its shape after saving.
 */
(() => {
  const KINDS = {
    buses: { label: 'Bus', cls: 'Tower', prefix: 'B', defaults: {} },
    substations: { label: 'Substation', cls: 'Substation', prefix: 'SUB_', defaults: { capacity_mw: 300 } },
    generators: { label: 'Generator', cls: 'HydroPlant', prefix: 'GEN_', defaults: { output_mw: 0, capacity_mw: 500 } },
    loads: { label: 'Load', cls: 'Consumer', prefix: 'LOAD_', defaults: { mw_demand: 50, priority_tier: 2 } },
    batteries: { label: 'Battery', cls: 'BESS', prefix: 'BESS_', defaults: { max_discharge_mw: 40, soc_pct: 80 } },
    lines: { label: 'Line', prefix: 'L', defaults: { impedance_pu: 0.01, limit_mw: 300 } },
  };
  const FIELDS = {
    buses: [['id', 'text'], ['name', 'text']],
    substations: [['id', 'text'], ['name', 'text'], ['capacity_mw', 'number']],
    generators: [['id', 'text'], ['name', 'text'], ['connects_to', 'text'], ['output_mw', 'number'], ['capacity_mw', 'number']],
    loads: [['id', 'text'], ['name', 'text'], ['substation', 'text'], ['mw_demand', 'number'], ['priority_tier', 'tier'], ['breaker_id', 'text']],
    batteries: [['id', 'text'], ['substation', 'text'], ['max_discharge_mw', 'number'], ['soc_pct', 'number']],
    lines: [['id', 'text'], ['from', 'text'], ['to', 'text'], ['impedance_pu', 'number'], ['limit_mw', 'number'], ['backup_limit_mw', 'number'], ['backup_impedance_pu', 'number']],
  };
  const ID_RE = /^[A-Z0-9_]{1,40}$/;
  const icon = window.GridPulseIcons.cytoscapeStyle;
  const STYLE = [
    { selector: 'node', style: { label: 'data(label)', color: '#dbeafe', 'font-size': 10, 'font-weight': 600, 'text-valign': 'bottom', 'text-margin-y': 5, ...icon('Consumer','#94a3b8',31,31) } },
    { selector: 'node.HydroPlant', style: icon('HydroPlant','#38bdf8',34,34) },
    { selector: 'node.Tower', style: icon('Tower','#45caff',38,46) },
    { selector: 'node.Substation', style: icon('Substation','#38bdf8',34,34) },
    { selector: 'node.BESS', style: icon('BESS','#a78bfa',34,34) },
    { selector: 'node.Consumer', style: icon('Consumer','#94a3b8',31,31) },
    { selector: 'node.category-HOSPITAL', style: icon('HOSPITAL','#34d399',34,34) },
    { selector: 'node.category-RESIDENTIAL', style: icon('RESIDENTIAL','#45caff',34,34) },
    { selector: 'node.category-COMMERCIAL', style: icon('COMMERCIAL','#45caff',34,34) },
    { selector: 'node.category-INDUSTRIAL', style: icon('INDUSTRIAL','#f59e0b',34,34) },
    { selector: 'node.tier1', style: { 'border-width': 2, 'border-color': '#a855f7' } }, { selector: 'node.tier3', style: { 'border-width': 2, 'border-color': '#f59e0b' } },
    { selector: 'node.pending', style: { 'border-width': 3, 'border-color': '#fde047' } },
    { selector: ':selected', style: { 'border-width': 3, 'border-color': '#fde047', 'line-color': '#fde047' } },
    { selector: 'edge', style: { width: 2.5, 'line-color': '#38bdf8', 'curve-style': 'bezier', label: '' } },
    { selector: 'edge:selected', style: { label: 'data(label)', 'font-size': 10, color: '#e0f2fe', 'text-background-color': '#082036', 'text-background-opacity': 0.9, 'text-background-padding': '3px', 'text-rotation': 'autorotate' } },
    { selector: 'edge.backup', style: { 'line-style': 'double', width: 5 } },
    { selector: 'edge.attach', style: { 'line-color': '#334155', width: 1, label: '' } },
    { selector: 'edge.feed', style: { 'line-color': '#22d3ee', width: 3, label: '' } },
  ];

  function create(containerId, opts = {}) {
    const ed = { spec: blank(), mode: 'select', pending: null, undo: [], selected: null, onChange: opts.onChange || (() => {}), onSelect: opts.onSelect || (() => {}), onStatus: opts.onStatus || (() => {}) };
    const cy = cytoscape({ container: document.getElementById(containerId), style: STYLE, wheelSensitivity: 0.25, boxSelectionEnabled: false });
    ed.cy = cy;

    function blank() { return { name: 'My grid', description: '', generators: [], buses: [], substations: [], lines: [], loads: [], batteries: [], layout: {} }; }
    function normalize(spec) {
      const s = { ...blank(), ...JSON.parse(JSON.stringify(spec || {})) };
      for (const k of Object.keys(KINDS)) s[k] = Array.isArray(s[k]) ? s[k] : [];
      s.layout = s.layout && typeof s.layout === 'object' ? s.layout : {};
      return s;
    }
    const kindOf = (id) => Object.keys(KINDS).find((k) => k !== 'lines' && ed.spec[k].some((e) => e.id === id));
    const find = (kind, id) => ed.spec[kind].find((e) => e.id === id);
    const allIds = () => new Set(Object.keys(KINDS).filter((k) => k !== 'lines').flatMap((k) => ed.spec[k].map((e) => e.id)));
    function nextId(kind) {
      const used = kind === 'lines' ? new Set(ed.spec.lines.map((l) => l.id)) : allIds();
      let i = 1; while (used.has(KINDS[kind].prefix + i)) i++;
      return KINDS[kind].prefix + i;
    }
    function snapshot() { ed.undo.push(JSON.stringify(ed.spec)); if (ed.undo.length > 60) ed.undo.shift(); }
    function changed(selectId) { capturePositions(); render(); ed.onChange(ed.getSpec()); if (selectId) select(selectId); }

    function capturePositions() {
      cy.nodes().forEach((n) => { ed.spec.layout[n.id()] = { x: Math.round(n.position('x')), y: Math.round(n.position('y')) }; });
    }
    function render() {
      const s = ed.spec; const els = [];
      const node = (kind, e, extra = '') => els.push({ group: 'nodes', data: { id: e.id, label: e.id, kind }, classes: `${KINDS[kind].cls} ${extra}`, position: s.layout[e.id] ? { ...s.layout[e.id] } : undefined });
      s.buses.forEach((e) => node('buses', e)); s.substations.forEach((e) => node('substations', e));
      s.generators.forEach((e) => node('generators', e)); s.batteries.forEach((e) => node('batteries', e));
      s.loads.forEach((e) => node('loads', e, `${e.priority_tier === 1 ? 'tier1' : e.priority_tier === 3 ? 'tier3' : ''} category-${e.category || ({1:'HOSPITAL',2:'RESIDENTIAL',3:'INDUSTRIAL'}[e.priority_tier]) || 'RESIDENTIAL'}`));
      const ids = allIds();
      s.lines.forEach((l) => { if (ids.has(l.from) && ids.has(l.to)) els.push({ group: 'edges', data: { id: 'line:' + l.id, source: l.from, target: l.to, label: `${l.id} ${l.limit_mw ?? ''}`, kind: 'lines', ref: l.id }, classes: l.backup ? 'backup' : '' }); });
      s.generators.forEach((g) => { if (ids.has(g.connects_to)) els.push({ group: 'edges', data: { id: 'feed:' + g.id, source: g.id, target: g.connects_to }, classes: 'feed' }); });
      s.loads.forEach((l) => { if (ids.has(l.substation)) els.push({ group: 'edges', data: { id: 'att:' + l.id, source: l.substation, target: l.id }, classes: 'attach' }); });
      s.batteries.forEach((b) => { if (ids.has(b.substation)) els.push({ group: 'edges', data: { id: 'att:' + b.id, source: b.id, target: b.substation }, classes: 'attach' }); });
      const missing = els.filter((e) => e.group === 'nodes' && !e.position);
      cy.elements().remove(); cy.add(els);
      if (missing.length === els.filter((e) => e.group === 'nodes').length && missing.length) {
        cy.layout({ name: 'cose', animate: false, nodeRepulsion: missing.length > 100 ? 6000 : 9000, idealEdgeLength: missing.length > 100 ? 30 : 60, numIter: 1200 }).run();
        capturePositions(); cy.fit(undefined, 30);
      } else if (missing.length) {
        // Place newly added or unpositioned nodes next to what they attach to.
        missing.forEach((m) => {
          const e = cy.getElementById(m.data.id);
          const nb = e.neighborhood('node').filter((n) => s.layout[n.id()]);
          const base = nb.length ? nb[0].position() : { x: cy.width() / 2, y: cy.height() / 2 };
          e.position({ x: base.x + 40 + Math.random() * 30, y: base.y + 40 + Math.random() * 30 });
        });
        capturePositions();
      }
      if (ed.pending) cy.getElementById(ed.pending).addClass('pending');
    }

    function select(id) {
      ed.selected = id;
      cy.elements().unselect();
      const kind = id && (id.startsWith('line:') ? 'lines' : kindOf(id));
      if (!kind) { ed.onSelect(null); return; }
      const ref = kind === 'lines' ? id.slice(5) : id;
      cy.getElementById(id).select();
      const e = find(kind, ref);
      const view = kind === 'lines' ? { ...e, backup_limit_mw: e.backup?.limit_mw ?? '', backup_impedance_pu: e.backup?.impedance_pu ?? '' } : e;
      ed.onSelect({ kind, label: KINDS[kind].label, id: ref, fields: FIELDS[kind].map(([k, t]) => ({ key: k, type: t, value: view[k] ?? '' })) });
    }

    function renameRefs(oldId, newId) {
      const s = ed.spec;
      s.lines.forEach((l) => { if (l.from === oldId) l.from = newId; if (l.to === oldId) l.to = newId; });
      s.generators.forEach((g) => { if (g.connects_to === oldId) g.connects_to = newId; });
      s.loads.forEach((l) => { if (l.substation === oldId) l.substation = newId; });
      s.batteries.forEach((b) => { if (b.substation === oldId) b.substation = newId; });
      if (s.layout[oldId]) { s.layout[newId] = s.layout[oldId]; delete s.layout[oldId]; }
    }
    ed.applyProps = (kind, id, values) => {
      const e = find(kind, id); if (!e) return;
      const newId = String(values.id || id).trim().toUpperCase();
      if (!ID_RE.test(newId)) throw new Error('IDs use uppercase letters, digits and underscores');
      if (newId !== id && (kind === 'lines' ? ed.spec.lines.some((l) => l.id === newId) : allIds().has(newId))) throw new Error(`'${newId}' is already used`);
      snapshot();
      for (const [k, type] of FIELDS[kind]) {
        if (k === 'id' || k.startsWith('backup_')) continue;
        const raw = values[k];
        if (raw === undefined) continue;
        if (raw === '' || raw === null) { delete e[k]; continue; }
        e[k] = type === 'number' || type === 'tier' ? Number(raw) : ['connects_to', 'substation', 'from', 'to', 'breaker_id'].includes(k) ? String(raw).toUpperCase() : raw;
      }
      if (kind === 'loads' && e.priority_tier) e.category = { 1: 'HOSPITAL', 2: 'RESIDENTIAL', 3: 'INDUSTRIAL' }[e.priority_tier];
      if (kind === 'lines') {
        const bl = Number(values.backup_limit_mw);
        if (values.backup_limit_mw !== '' && bl > 0) e.backup = { limit_mw: bl, impedance_pu: Number(values.backup_impedance_pu) || (e.impedance_pu || 0.01) * 1.5 };
        else delete e.backup;
      }
      if (newId !== id) { if (kind !== 'lines') renameRefs(id, newId); e.id = newId; }
      changed(kind === 'lines' ? 'line:' + newId : newId);
    };

    ed.remove = (kind, id) => {
      const s = ed.spec; snapshot();
      if (kind === 'lines') s.lines = s.lines.filter((l) => l.id !== id);
      else {
        s[kind] = s[kind].filter((e) => e.id !== id);
        delete s.layout[id];
        if (kind === 'buses' || kind === 'substations') {
          s.lines = s.lines.filter((l) => l.from !== id && l.to !== id);
          s.generators = s.generators.filter((g) => g.connects_to !== id);
          s.loads = s.loads.filter((l) => l.substation !== id);
          s.batteries = s.batteries.filter((b) => b.substation !== id);
        }
      }
      ed.selected = null; ed.onSelect(null); changed();
    };

    function addAt(kind, pos, attachTo) {
      snapshot();
      const id = nextId(kind);
      const e = { id, ...KINDS[kind].defaults };
      if (kind === 'generators') e.connects_to = attachTo;
      if (kind === 'loads' || kind === 'batteries') e.substation = attachTo;
      ed.spec[kind].push(e);
      ed.spec.layout[id] = { x: Math.round(pos.x), y: Math.round(pos.y) };
      changed(id);
    }

    ed.addFromPalette = (kind, clientX, clientY) => {
      if (!['buses', 'substations', 'generators', 'loads', 'batteries'].includes(kind)) return false;
      const rect = cy.container().getBoundingClientRect();
      const pan = cy.pan(), zoom = cy.zoom();
      const pos = { x: (clientX - rect.left - pan.x) / zoom, y: (clientY - rect.top - pan.y) / zoom };
      let attachTo;
      if (['generators', 'loads', 'batteries'].includes(kind)) {
        const eligible = kind === 'generators' ? 'node.Tower, node.Substation' : 'node.Substation';
        const targets = cy.nodes(eligible).toArray().sort((a, b) =>
          Math.hypot(a.position('x') - pos.x, a.position('y') - pos.y) - Math.hypot(b.position('x') - pos.x, b.position('y') - pos.y));
        if (!targets.length) {
          ed.onStatus(kind === 'generators' ? 'Add a bus or substation first.' : 'Add a substation first, then drop this nearby.', 'warn');
          return false;
        }
        attachTo = targets[0].id();
      }
      addAt(kind, pos, attachTo);
      ed.onStatus(`Added ${KINDS[kind].label.toLowerCase()}${attachTo ? ` to ${attachTo}` : ''}. Drag it to adjust its position.`, '');
      return true;
    };

    cy.on('tap', (evt) => {
      if (evt.target !== cy) return;
      const pos = evt.position;
      if (ed.mode === 'buses' || ed.mode === 'substations') addAt(ed.mode, pos);
      else if (['generators', 'loads', 'batteries'].includes(ed.mode)) ed.onStatus(`Click the ${ed.mode === 'generators' ? 'bus or substation' : 'substation'} to attach the ${KINDS[ed.mode].label.toLowerCase()} to.`, 'warn');
      else { ed.pending = null; select(null); render(); }
    });
    cy.on('tap', 'node', (evt) => {
      const id = evt.target.id(); const kind = kindOf(id);
      const gridNode = kind === 'buses' || kind === 'substations';
      if (ed.mode === 'delete') return ed.remove(kind, id);
      if (ed.mode === 'lines') {
        if (!ed.pending) { if (gridNode || kind === 'generators') { ed.pending = id; render(); ed.onStatus(`From ${id}: now click the other end.`, 'warn'); } return; }
        const a = ed.pending; ed.pending = null;
        if (a === id) { render(); return; }
        const ka = kindOf(a);
        if (ka === 'generators' && gridNode) { snapshot(); find('generators', a).connects_to = id; changed(a); return; }
        if (!(ka === 'buses' || ka === 'substations') || !gridNode) { render(); ed.onStatus('Lines connect buses and substations.', 'bad'); return; }
        snapshot(); const lid = nextId('lines');
        ed.spec.lines.push({ id: lid, from: a, to: id, ...KINDS.lines.defaults });
        changed('line:' + lid); return;
      }
      if (ed.mode === 'generators' && gridNode) return addAt('generators', { x: evt.target.position('x') - 50, y: evt.target.position('y') - 50 }, id);
      if ((ed.mode === 'loads' || ed.mode === 'batteries') && kind === 'substations') {
        const n = ed.spec[ed.mode].filter((e) => e.substation === id).length;
        return addAt(ed.mode, { x: evt.target.position('x') + (ed.mode === 'loads' ? 45 : -45), y: evt.target.position('y') + 35 + n * 28 }, id);
      }
      if (['generators', 'loads', 'batteries'].includes(ed.mode)) { ed.onStatus(`Pick a ${ed.mode === 'generators' ? 'bus or substation' : 'substation'}.`, 'bad'); return; }
      select(id);
    });
    cy.on('tap', 'edge', (evt) => {
      const id = evt.target.id();
      if (!id.startsWith('line:')) return;
      if (ed.mode === 'delete') return ed.remove('lines', id.slice(5));
      select(id);
    });
    cy.on('dragfree', 'node', (evt) => { ed.spec.layout[evt.target.id()] = { x: Math.round(evt.target.position('x')), y: Math.round(evt.target.position('y')) }; ed.onChange(ed.getSpec()); });

    ed.setMode = (m) => {
      ed.mode = m; ed.pending = null; render();
      const help = { select: 'Click to edit, drag to arrange.', buses: 'Click empty canvas to place a bus.', substations: 'Click empty canvas to place a substation.',
        generators: 'Click a bus or substation to attach a generator. The first generator by id balances the grid (slack).', lines: 'Click two buses/substations to connect them. Generator then bus re-connects a generator.',
        loads: 'Click a substation to attach a load.', batteries: 'Click a substation to attach a battery.', delete: 'Click an element to delete it (and what hangs off it).' }[m];
      ed.onStatus(help, '');
    };
    ed.load = (spec) => { ed.spec = normalize(spec); ed.undo = []; ed.pending = null; ed.selected = null; ed.onSelect(null); render(); cy.fit(undefined, 30); };
    ed.getSpec = () => { capturePositions(); return JSON.parse(JSON.stringify(ed.spec)); };
    ed.undoLast = () => { const prev = ed.undo.pop(); if (!prev) return false; ed.spec = JSON.parse(prev); ed.selected = null; ed.onSelect(null); render(); ed.onChange(ed.getSpec()); return true; };
    ed.autoLayout = () => { snapshot(); ed.spec.layout = {}; render(); ed.onChange(ed.getSpec()); };
    ed.resize = () => cy.resize();
    ed.fit = () => cy.fit(undefined, 55);
    ed.zoomBy = (factor) => cy.zoom({ level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor)), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
    ed.counts = () => Object.fromEntries(Object.keys(KINDS).map((k) => [k, ed.spec[k].length]));
    return ed;
  }

  window.GridEditor = { create, KINDS };
})();
