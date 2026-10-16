/* Authenticated UI adapter. Telemetry is the only source of operational state. */
(() => {
  const $ = id => document.getElementById(id);
  const base = (window.GRIDPULSE_API_BASE || (/^(8765|8766|5500|8080)$/.test(location.port) || location.protocol === 'file:' ? 'http://127.0.0.1:8000' : location.origin)).replace(/\/$/, '');
  let session, socket, reconnect, lastSeen = 0, healthy = false, busy = false, topology = null, audit = [], lastSnapshot = null;
  const roles = {GRID_OPERATOR:'operator',TOPOLOGY_ENGINEER:'engineer',NERC_AUDITOR:'auditor'};
  const say = (id, value) => { if ($(id)) $(id).textContent = value; };
  const pretty = x => typeof x === 'string' ? x : JSON.stringify(x, null, 2);
  // One readable line per agent step (graph briefing, tool calls, verdict, fallback), as streamed in telemetry.
  const agentSteps = steps => steps.map(x => {
    const t = (x.at || '').slice(11, 19);
    const head = {start:'▶ FAULT', context:`📚 GRAPH ${x.tool}`, tool:`🔧 ${x.tool}`, tool_error:`⚠ ${x.tool}`, thought:'💭 MODEL', submit:'📝 PLAN', verdict:`⚖ GUARD ${x.verdict}`, fallback:'↩ FALLBACK', wait:'⏳ WAIT', done:'✅ DONE'}[x.kind] || x.kind;
    const body = x.result || x.summary || x.text || x.error || (x.reasons || []).join('; ') || '';
    const extra = x.kind === 'submit' && x.plan ? `  ${JSON.stringify(x.plan)}` : x.server_ms ? `  (${x.server_ms} ms graph)` : '';
    return `${t}  ${head}  ${String(body).replace(/\s+/g, ' ').slice(0, 220)}${extra}`;
  }).join('\n');
  async function api(path, options = {}) {
    const response = await fetch(base + path, {...options, headers:{'Content-Type':'application/json', ...(session ? {Authorization:`Bearer ${session.access_token}`} : {}), ...options.headers}, signal:AbortSignal.timeout(options.timeoutMs || 20000)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && session) signOut();
      const d = data.detail;
      // FastAPI validation errors arrive as a list of {loc, msg}.
      const message = typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('; ') : d?.message || d?.code;
      const error = new Error(message || `Request failed (${response.status})`); error.status = response.status; throw error;
    }
    return data;
  }
  function controls() {
    document.querySelectorAll('[onclick^="triggerConsoleScenario"],[onclick="resetConsoleGrid()"],#agent-execute').forEach(el => el.disabled = !session || session.role !== 'operator' || !healthy || busy);
    $('console-status-dot').style.background = healthy ? '#34d399' : '#f59e0b';
  }
  function connection(text) { say('connection-status',text); say('backend-connection-pill',text); controls(); }
  // Background refreshes pass silent so they don't wipe a message the operator hasn't read yet.
  async function guarded(fn, silent = false) { try { if (!silent) say('action-message',''); return await fn(); } catch(e) { if (!silent || !$('action-message')?.textContent) say('action-message',e.message); } }
  let loginInFlight = false;
  async function login(body) {
    if (loginInFlight) return;
    loginInFlight = true;
    const card = $('gateway-auth-card');
    const actions = [...card.querySelectorAll('button[type="submit"], button[onclick^="loginWithRole"]')];
    actions.forEach(button => { button.disabled = true; });
    card.classList.remove('is-auth-error','is-auth-success');
    card.classList.add('is-authenticating');
    say('gateway-message','Authenticating operator…');
    try {
      session = await api('/api/auth/login',{method:'POST',body:JSON.stringify(body)});
      sessionStorage.setItem('gridpulse_live_session',JSON.stringify(session));
      card.classList.remove('is-authenticating');
      card.classList.add('is-auth-success');
      say('gateway-message','Access granted ✓');
      if (!matchMedia('(prefers-reduced-motion: reduce)').matches) await new Promise(resolve => setTimeout(resolve,220));
      await enter();
    } catch(e) {
      card.classList.remove('is-authenticating','is-auth-success');
      card.classList.add('is-auth-error');
      say('gateway-message',e.message);
    } finally {
      loginInFlight = false;
      actions.forEach(button => { button.disabled = false; });
      setTimeout(() => card.classList.remove('is-auth-error','is-auth-success'),500);
    }
  }
  window.handleGatewaySubmit = e => { e.preventDefault(); return login({username:$('login-email-input').value,password:$('login-pass-input').value,role:'operator',demo:false}); };
  window.loginWithRole = (username,role) => login({username,role:roles[role] || role,demo:true});
  window.signOut = () => {
    session=null; healthy=false; clearTimeout(reconnect); if(socket){socket.onclose=null;socket.close();} socket=null;
    sessionStorage.removeItem('gridpulse_live_session'); ['gp_lab_token','gp_lab_role','gp_lab_user'].forEach(k=>sessionStorage.removeItem(k)); topology=null; audit=[];lastSnapshot=null;
    ['trace-result','agent-result','audit-result','action-message'].forEach(id=>say(id,''));
    window.gridpulseFeatures?.reset();
    ['c-kpi-latency','c-kpi-hydro','c-kpi-load','c-kpi-bess','c-kpi-hospital'].forEach(id=>say(id,'—'));
    $('console-audit-tbody').replaceChildren(); say('console-mcp-log','Awaiting backend agent activity.');
    location.hash='gateway';setAppView('gateway');controls();
  };
  const switchPage = window.switchScadaPage;
  window.gridpulseClient={api,get session(){return session;},get base(){return base;}};
  window.switchScadaPage = page => { switchPage(page); location.hash=page; if(page==='audit') guarded(loadAudit); window.gridpulseFeatures?.activate(page); };
  function route() {
    const page=location.hash.slice(1);
    if(['topology','cypher','mcp','audit','askgrid','networks','learning'].includes(page)) {setAppView(session?'console':'gateway');if(session){switchPage(page);window.gridpulseFeatures?.activate(page);}}
    else if(page==='gateway'){if(session){location.hash='topology';return;}setAppView('gateway');}
    else setAppView('landing');
  }
  addEventListener('hashchange',route);
  window.transitionToGateway=()=>{location.hash=session?'topology':'gateway';route();};
  window.returnToCorridor=()=>{location.hash='';route();};
  async function loadTopology(){topology=await api('/api/graph/topology');}
  async function loadAudit(){
    audit=await api('/api/audit/logs?limit=100');
    const body=$('console-audit-tbody');body.replaceChildren();
    if(!audit.length){const row=body.insertRow();const cell=row.insertCell();cell.colSpan=5;cell.textContent='No audit records yet.';}
    for(const item of audit){const row=body.insertRow();for(const value of [item.timestamp,item.scenario_id??'—',item.target,item.action]){const cell=row.insertCell();cell.className='py-3 px-2';cell.textContent=value??'—';}const cell=row.insertCell();const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=item.outcome||'View evidence';const pre=document.createElement('pre');pre.className='integration-result';pre.textContent=pretty(item);details.append(summary,pre);cell.append(details);}
  }
  function snapshot(data){
    lastSnapshot=data;lastSeen=Date.now();const m=data.metrics||{};
    const number=(v,unit)=>Number.isFinite(v)?`${v} ${unit}`:'—';
    say('c-kpi-latency',number(m.graphblas_latency_ms,'ms'));say('c-kpi-load',number(m.substation_load_mw??m.city_load_mw,'MW'));say('c-kpi-bess',number(m.bess_injection_mw,'MW'));say('c-kpi-hospital',m.tier1_hospital_status||'UNKNOWN');
    const hydro=topology?.nodes.find(n=>n.id==='HYDRO_ALPINE_01')?.properties;
    say('c-kpi-hydro',number(hydro?.output_mw??hydro?.generation_mw,'MW'));
    say('load-note',`City demand · ${number(m.city_load_pct,'%')}`);say('bess-note',`BESS · ${number(data.bess?.soc_pct,'% SOC')}`);say('hospital-note','Backend telemetry');
    $('c-kpi-hospital').style.color=m.tier1_hospital_status==='PROTECTED'?'#10b981':m.tier1_hospital_status==='AT_RISK'?'#f59e0b':'#ef4444';
    const readable = value => String(value||'').replace(/[_.-]+/g,' ').replace(/\s+/g,' ').trim().toLowerCase().replace(/^./,letter=>letter.toUpperCase()).replace(/\btx (\d+)/g,'TX $1');
    const rawStatus=data.scenario?.status||'UNKNOWN';
    const plainStatus=readable(rawStatus);
    let scenarioName=readable(data.scenario?.name||'Live grid');
    if(scenarioName.toLowerCase().endsWith(` ${plainStatus.toLowerCase()}`))scenarioName=scenarioName.slice(0,-plainStatus.length).trim();
    const statusLabel={NOMINAL:'Normal operation',MITIGATED:'Risk contained',ACTIVE:'Incident active',TRIPPED:'Line tripped',UNKNOWN:'Awaiting status'}[rawStatus]||plainStatus;
    say('scada-topo-heading',`${scenarioName} — ${statusLabel}`);
    say('console-mcp-log',data.agent_steps?.length?agentSteps(data.agent_steps)+'\n\n'+(data.agent_log||''):pretty(data.agent_log||'No agent activity.'));
    consoleScenario=data.scenario?.id||0;consoleMitigated=data.scenario?.status==='MITIGATED';
    if(topology){for(const node of topology.nodes){const fresh=[...(data.towers||[]),...(data.consumers||[]),data.bess,data.substation].find(n=>n?.id===node.id);if(fresh)Object.assign(node.properties,fresh);}for(const edge of topology.edges){const span=(data.spans||[]).find(s=>edge.id.startsWith(s.id+':'));if(span)edge.properties.status=edge.id.includes('UPPER_PRIMARY')?span.upper_status:span.lower_status;}}
  }
  async function health(){try{const h=await api('/health');healthy=h.status==='HEALTHY';connection(healthy?'CONNECTED · authenticated simulation':'DEGRADED · FalkorDB unavailable');}catch(e){healthy=false;connection(`OFFLINE · ${e.message}`);}}
  function connect(){
    if(!session)return;
    // Never keep two sockets: each would carry its own reconnect loop.
    clearTimeout(reconnect);if(socket){socket.onclose=null;socket.close();socket=null;}
    const url=new URL(base+'/ws/telemetry');url.protocol=url.protocol==='https:'?'wss:':'ws:';url.searchParams.set('token',session.access_token);
    socket=new WebSocket(url);socket.onmessage=e=>{try{snapshot(JSON.parse(e.data));}catch{say('action-message','Invalid telemetry received.');}};
    socket.onclose=e=>{healthy=false;connection('DISCONNECTED · reconnecting');if(e.code===1008){signOut();say('gateway-message','Session expired. Sign in again.');}else if(session)reconnect=setTimeout(()=>{health();connect();},3000);};
    socket.onerror=()=>{healthy=false;connection('Telemetry connection unavailable');};
  }
  async function enter(){
    say('console-operator-pill',`${session.operator_id} · ${session.role}`);location.hash=['topology','cypher','mcp','audit','askgrid','networks','learning'].includes(location.hash.slice(1))?location.hash:'topology';route();
    await health();await Promise.allSettled([guarded(loadTopology),guarded(loadAudit),guarded(async()=>snapshot(await api('/api/sim/state')))]);connect();
  }
  window.triggerConsoleScenario=id=>guarded(async()=>{
    if(!healthy||session?.role!=='operator'||busy)throw Error('An operator session and healthy backend are required.');
    const mode=document.getElementById('console-scenario-mode')?.value==='agent'?'agent':'deterministic';
    busy=true;controls();try{await api(`/api/sim/trigger-scenario/${id}?mode=${mode}`,{method:'POST'});say('action-message',mode==='agent'?'AI operator investigating with graph tools: follow its steps in 03 / SCADA MCP Agent Loop.':'Scenario accepted. Following backend telemetry.');await loadAudit();}finally{busy=false;controls();}
  });
  window.resetConsoleGrid=()=>guarded(async()=>{if(!healthy||session?.role!=='operator')throw Error('An operator session and healthy backend are required.');await api('/api/sim/reset',{method:'POST'});snapshot(await api('/api/sim/state'));await loadTopology();await loadAudit();});
  // The public corridor is an explicitly illustrative animation, never an unauthenticated grid mutation.
  window.triggerScenario=id=>{activeScenarioId=id||1;isFaultActive=true;isRerouted=false;lightningFlashAlpha=1;scrollToStation(id===1?.24:id===2?.42:.88);say('hud-status-title','ILLUSTRATION · Sign in to run a backend scenario');say('metric-latency','—');say('metric-city-load','Illustrative');say('metric-hospital','Not live');};
  window.triggerFaultSimulation=()=>triggerScenario(activeScenarioId);
  window.resetGrid=()=>{isFaultActive=false;isRerouted=false;say('hud-status-title','ILLUSTRATIVE CORRIDOR · Open console for live data');};
  window.toggleAutoCycle=enabled=>{clearInterval(autoCycleTimer);if(enabled){let id=1;triggerScenario(id);autoCycleTimer=setInterval(()=>triggerScenario(id=id%3+1),5500);}};
  // Trace results: a readable summary first, the full API response one click away.
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const outcomeTone=o=>/WITHIN_LIMITS|NO_IMPACT/.test(o)?'ok':/ISLANDING/.test(o)?'warn':/OVERLOAD/.test(o)?'bad':'info';
  function renderTrace(r){
    const box=$('trace-result');box.replaceChildren();
    const s=r.summary||{},p=r.projection||{},o=r.origin||{};
    const head=el('div','trace-head');
    head.append(el('span',`trace-pill trace-${outcomeTone(s.projection_outcome)}`,(s.projection_outcome||r.outcome||'UNKNOWN').replace(/_/g,' ')));
    const where=o.kind==='SPAN'?`${o.id} (${o.from} → ${o.to})`:o.id||s.origin_id;
    const tripped=(p.assumed_tripped||[]).map(t=>Array.isArray(t)?t.join(':'):`${t.span_id}:${t.circuit}`);
    head.append(el('span','trace-sub',`${where}${tripped.length?` · assuming ${tripped.join(', ')} tripped`:' · current grid state'} · ${r.timing?.query_count??(r.evidence||[]).length} queries in ${r.timing?.graph_server_ms??'—'} ms`));
    box.append(head);
    const stats=el('div','trace-stats');
    for(const [label,value,tone] of [
      ['Relief needed',s.required_relief_mw?`${s.required_relief_mw} MW`:'None',s.required_relief_mw?'bad':'ok'],
      ['Downstream assets',`${s.downstream_node_count??0} nodes · ${s.furthest_hop??0} hops`],
      ['Substations',(s.downstream_substations||[]).join(', ')||'None reached'],
      ['Tier-1 at risk',(s.tier1_at_risk||[]).join(', ')||'None',(s.tier1_at_risk||[]).length?'bad':'ok']]){
      const card=el('div',`trace-stat${tone?` trace-${tone}`:''}`);card.append(el('small','',label),el('b','',value));stats.append(card);
    }
    box.append(stats);
    const nodes=r.reachability?.nodes||[],sub=nodes.filter(n=>n.label==='Substation').sort((a,b)=>a.hops-b.hops)[0];
    const path=sub?.path||nodes.map(n=>n.id);
    if(path.length){box.append(el('h4','trace-h','Cascade path'),el('div','trace-path',path.join('  →  ')));}
    if((p.overloads||[]).length){
      box.append(el('h4','trace-h','Projected overloads'));
      const list=el('ul','trace-list');
      for(const ov of p.overloads){const best=(ov.relief_options||[])[0];list.append(el('li','',`${ov.element} at ${ov.loading_pct}% of rating${best?` · relieve ${best.relief_mw} MW at ${best.substation_id}`:''}`));}
      box.append(list);
    }
    const consumers=r.impact?.consumers||[],bess=r.impact?.bess||[];
    if(consumers.length||bess.length){
      box.append(el('h4','trace-h','Downstream loads and batteries'));
      const table=el('table','trace-table'),hdr=table.insertRow();
      ['Asset','Type','MW','Status'].forEach(h=>hdr.append(el('th','',h)));
      for(const c of consumers){const row=table.insertRow();[c.name||c.id,`Tier ${c.priority_tier} ${String(c.category||'').toLowerCase()}`,c.mw_demand,c.risk==='NONE'?'OK':c.risk.replace(/_/g,' ')].forEach((v,k)=>{const td=row.insertCell();td.textContent=v??'—';if(k===3&&c.risk!=='NONE')td.className=c.priority_tier===1?'trace-bad':'trace-warn';});}
      for(const b of bess){const row=table.insertRow();[b.id,'Battery',`${b.active_injection_mw} / ${b.max_discharge_mw}`,`${b.status} · ${b.soc_pct}% SOC`].forEach(v=>row.insertCell().textContent=v??'—');}
      box.append(table);
    }
    if((r.evidence||[]).length){
      box.append(el('h4','trace-h','FalkorDB queries'));
      const list=el('ul','trace-list trace-mono');
      for(const ev of r.evidence)list.append(el('li','',`${ev.name} · ${ev.row_count} rows · ${ev.server_ms} ms · ${String(ev.evidence_hash||'').slice(0,19)}…`));
      box.append(list);
    }
    const raw=el('details','trace-raw'),sum=el('summary','','Raw API response (JSON)'),pre=el('pre','integration-result',pretty(r));
    raw.append(sum,pre);box.append(raw);
  }
  $('trace-form').onsubmit=e=>{e.preventDefault();guarded(async()=>{say('trace-result','Tracing…');
    const origin=$('trace-origin').value.trim().toUpperCase();
    // Only spans can trip; towers, plants and substations are traced from their current state.
    const trip=$('trace-trip').checked&&/^TX_\d+$/.test(origin)?[{span_id:origin,circuit:'UPPER_PRIMARY'}]:[];
    const result=await api('/api/graph/trace-cascade',{method:'POST',body:JSON.stringify({origin_id:origin,max_hops:Number($('trace-hops').value),assume_tripped:trip})});
    renderTrace(result);say('console-cypher-pre',(result.evidence||[]).map(x=>`// ${x.name} · ${x.row_count} rows · ${x.server_ms} ms\n${x.cypher}`).filter(Boolean).join('\n\n')||'No Cypher evidence returned.');}).then(()=>{if($('trace-result').textContent==='Tracing…')say('trace-result','Trace unavailable. See the error above.');});};
  $('agent-form').onsubmit=e=>{e.preventDefault();const submit=e.target.querySelector('button[type="submit"],button:not([type])');if(submit?.disabled)return;guarded(async()=>{say('agent-result','Requesting agent advice…');if(submit)submit.disabled=true;try{const result=await api('/api/mcp/agent/advise',{method:'POST',timeoutMs:180000,body:JSON.stringify({question:$('agent-question').value,execute:$('agent-execute').checked})});say('agent-result',pretty(result));await loadAudit();}catch(error){say('agent-result',error.message);throw error;}finally{if(submit)submit.disabled=false;}});};
  $('audit-refresh').onclick=()=>guarded(loadAudit);
  $('audit-verify').onclick=()=>guarded(async()=>say('audit-result',pretty(await api('/api/audit/verify'))));
  $('audit-export').onclick=()=>guarded(async()=>{await loadAudit();const url=URL.createObjectURL(new Blob([pretty(audit)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='gridpulse-audit.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  const topologyView=$('scada-topology-view'),fullscreenButton=$('topology-fullscreen-toggle');
  let topologyFallbackExpanded=false;
  function updateTopologyFullscreen(){
    const expanded=document.fullscreenElement===topologyView||topologyFallbackExpanded;
    fullscreenButton.textContent=expanded?'↙ Exit full screen':'⛶ Full screen';
    fullscreenButton.setAttribute('aria-label',expanded?'Exit topology full screen':'Enter topology full screen');
    fullscreenButton.setAttribute('aria-pressed',String(expanded));
    if(!expanded){topologyView.classList.remove('topology-returning');void topologyView.offsetWidth;topologyView.classList.add('topology-returning');setTimeout(()=>topologyView.classList.remove('topology-returning'),340);}
    requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')));
  }
  fullscreenButton.addEventListener('click',async()=>{
    if(document.fullscreenElement===topologyView){await document.exitFullscreen();return;}
    if(topologyFallbackExpanded){topologyFallbackExpanded=false;topologyView.classList.remove('topology-expanded');document.body.classList.remove('topology-expanded-active');updateTopologyFullscreen();return;}
    try{await topologyView.requestFullscreen();}
    catch{topologyFallbackExpanded=true;topologyView.classList.add('topology-expanded');document.body.classList.add('topology-expanded-active');updateTopologyFullscreen();}
  });
  document.addEventListener('fullscreenchange',updateTopologyFullscreen);
  document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;if(document.fullscreenElement===topologyView){document.exitFullscreen();return;}if(topologyFallbackExpanded){topologyFallbackExpanded=false;topologyView.classList.remove('topology-expanded');document.body.classList.remove('topology-expanded-active');updateTopologyFullscreen();}});
  const topoColors={line:'#0ea5e9',flow:'#72f7ff',down:'#ef4444',node:'#34d399'};
  function topologyLayout(nodes,w,h){
    const compact=w<900, left=compact?50:72, corridorEnd=w*.69;
    const towerY=h*.51, subX=w*.80, subY=towerY;
    const points={HYDRO_ALPINE_01:{x:left,y:towerY},SUB_METRO:{x:subX,y:subY},BESS_METRO_01:{x:subX,y:h*.84}};
    for(let i=1;i<=9;i++)points[`T${i}`]={x:left+52+(corridorEnd-left-52)*(i-1)/8,y:towerY};
    const consumers=nodes.filter(n=>n.label==='Consumer');
    const order={HOSPITAL:0,RESIDENTIAL:1,COMMERCIAL:2,INDUSTRIAL:3};
    consumers.sort((a,b)=>(order[a.properties?.category]??4)-(order[b.properties?.category]??4));
    consumers.forEach((n,i)=>{points[n.id]={x:w-60,y:h*(.14+i*.22)};});
    nodes.forEach((n,i)=>{if(!points[n.id])points[n.id]={x:left+36+(i%7)*(w-2*left)/7,y:h*.76};});
    return points;
  }
  function nodeIcon(ctx,node,x,y,compact,dark){
    const category=node.properties?.category;
    const kind=node.label==='Consumer'?category:node.label;
    const bad=/TRIPPED|OFFLINE|FAILED|FAULT|AT_RISK/.test(node.properties?.status||'') || node.properties?.energized===false;
    const color=bad?topoColors.down:kind==='Tower'?(dark?'#c5e9fa':'#0369a1'):kind==='BESS'?'#a855f7':kind==='HOSPITAL'?'#34d399':kind==='HydroPlant'?'#38bdf8':'#45caff';
    const tower=kind==='Tower';
    ctx.save();ctx.translate(x,y);
    ctx.fillStyle=dark?'#0d2238':'#e0f2fe';ctx.strokeStyle=bad?'#fb7185':color;ctx.lineWidth=1.25;
    ctx.beginPath();ctx.roundRect(-18,-18,36,36,8);ctx.fill();ctx.stroke();
    GridPulseIcons.draw(ctx,kind,color,tower?32:34);
    ctx.font=`600 ${compact?9:10}px "JetBrains Mono",monospace`;ctx.textAlign='center';ctx.textBaseline='top';ctx.fillStyle=dark?'#dbeafe':'#334155';
    const labels={HYDRO_ALPINE_01:'HYDRO',BESS_METRO_01:'BESS',SUB_METRO:'SUBSTATION',HOSP_ST_JUDE:'HOSPITAL',RES_VALLEY_SUBURBS:'HOMES',COM_METRO_CORE:'METRO',IND_METRO_12:'INDUSTRY'};
    ctx.fillText(labels[node.id]||node.id,0,22);ctx.restore();
  }
  window.gridpulseDrawTopology=(canvas,ctx)=>{
    if(!canvas||!ctx)return;const dpr=Math.min(devicePixelRatio||1,2),w=canvas.width/dpr,h=canvas.height/dpr,dark=isNightMode;
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle=dark?'#040814':'#f8fafc';ctx.fillRect(0,0,w,h);
    if(!topology){ctx.fillStyle=dark?'#94a3b8':'#475569';ctx.font='12px monospace';ctx.fillText('Waiting for FalkorDB topology…',20,40);return;}
    const nodes=topology.nodes,points=topologyLayout(nodes,w,h),now=performance.now()/1000,compact=w<900;
    const invalid=/TRIPPED|OPEN|OFFLINE|FAULT|FAILED/;
    const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    for(const e of topology.edges){
      const a=points[e.source],b=points[e.target];if(!a||!b)continue;
      const source=nodes.find(n=>n.id===e.source),target=nodes.find(n=>n.id===e.target);
      const inactive=invalid.test(e.properties?.status||'') || source?.properties?.energized===false || target?.properties?.energized===false || (e.type==='CONNECTED' && (lastSnapshot?.bess?.active_injection_mw??0)<=0);
      const dx=b.x-a.x,dy=b.y-a.y,length=Math.hypot(dx,dy);if(length<1)continue;
      const parallel=e.type==='SPAN'||e.type==='TERMINATES_AT';
      const lane=parallel?(e.id.includes('UPPER_PRIMARY')?-4:4):0;
      const ox=-dy/length*lane,oy=dx/length*lane;
      const start={x:a.x+ox,y:a.y+oy},end={x:b.x+ox,y:b.y+oy};
      const route=e.type==='DISTRIBUTES_TO'?[start,{x:start.x+48,y:start.y},{x:start.x+48,y:end.y},end]:[start,end];
      const segments=[];let routeLength=0;
      for(let j=1;j<route.length;j++){const from=route[j-1],to=route[j],size=Math.hypot(to.x-from.x,to.y-from.y);if(size){segments.push({from,to,size,at:routeLength});routeLength+=size;}}
      if(!routeLength)continue;
      ctx.strokeStyle=inactive?(dark?'#754254':'#fda4af'):(dark?'#147fa9':'#0284c7');ctx.lineWidth=parallel?1.7:2;ctx.lineCap='round';ctx.lineJoin='round';ctx.beginPath();ctx.moveTo(start.x,start.y);route.slice(1).forEach(p=>ctx.lineTo(p.x,p.y));ctx.stroke();
      if(inactive || reducedMotion)continue;
      const spacing=compact?82:100,count=Math.max(1,Math.floor(routeLength/spacing));
      const seed=[...e.id].reduce((sum,ch)=>sum+ch.charCodeAt(0),0);
      const speed=43;const phase=(now*speed+seed*3)%routeLength;
      const along=distance=>{const d=((distance%routeLength)+routeLength)%routeLength;const s=segments.find(s=>d<=s.at+s.size)||segments.at(-1);const t=(d-s.at)/s.size;return{x:s.from.x+(s.to.x-s.from.x)*t,y:s.from.y+(s.to.y-s.from.y)*t};};
      ctx.save();ctx.shadowColor=topoColors.flow;ctx.shadowBlur=dark?5:2;ctx.strokeStyle=dark?'#a5f3fc':'#0369a1';ctx.lineWidth=3;ctx.lineCap='round';
      for(let i=0;i<count;i++){
        const head=phase+i*routeLength/count;
        if(head%routeLength<10)continue;
        const tail=along(head-10),tip=along(head);
        ctx.beginPath();ctx.moveTo(tail.x,tail.y);ctx.lineTo(tip.x,tip.y);ctx.stroke();
      }
      ctx.restore();
    }
    for(const n of nodes){const p=points[n.id];nodeIcon(ctx,n,p.x,p.y,compact,dark);}
    ctx.font='10px "JetBrains Mono",monospace';ctx.textAlign='left';ctx.fillStyle=dark?'#94a3b8':'#475569';
    ctx.fillText(`${nodes.length} assets · ${topology.edges.length} connections`,16,h-15);
    ctx.fillStyle=topoColors.flow;ctx.beginPath();ctx.arc(w-117,h-19,2.5,0,Math.PI*2);ctx.fill();ctx.fillStyle=dark?'#cbd5e1':'#475569';ctx.fillText('Current flow',w-108,h-15);
  };
  setInterval(()=>{if(session){health();if(lastSeen&&Date.now()-lastSeen>10000){healthy=false;connection('STALE · waiting for telemetry');}if(healthy)guarded(loadTopology,true);}},10000);
  localStorage.removeItem('gridpulse_session');resetGrid();controls();route();
  try{session=JSON.parse(sessionStorage.getItem('gridpulse_live_session'));}catch{session=null;}
  // Only a rejected session signs out; a network blip on reload keeps it and lets the reconnect loop recover.
  if(session)api('/api/auth/session').then(enter).catch(e=>{if(e.status)signOut();else enter();});
})();
