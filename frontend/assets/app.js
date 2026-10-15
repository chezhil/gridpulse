/* Authenticated UI adapter. Telemetry is the only source of operational state. */
(() => {
  const $ = id => document.getElementById(id);
  const base = (window.GRIDPULSE_API_BASE || (/^(8765|8766|5500|8080)$/.test(location.port) || location.protocol === 'file:' ? 'http://127.0.0.1:8000' : location.origin)).replace(/\/$/, '');
  let session, socket, reconnect, lastSeen = 0, healthy = false, busy = false, topology = null, audit = [], lastSnapshot = null;
  const roles = {GRID_OPERATOR:'operator',TOPOLOGY_ENGINEER:'engineer',NERC_AUDITOR:'auditor'};
  const say = (id, value) => { if ($(id)) $(id).textContent = value; };
  const pretty = x => typeof x === 'string' ? x : JSON.stringify(x, null, 2);
  async function api(path, options = {}) {
    const response = await fetch(base + path, {...options, headers:{'Content-Type':'application/json', ...(session ? {Authorization:`Bearer ${session.access_token}`} : {}), ...options.headers}, signal:AbortSignal.timeout(20000)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && session) signOut();
      throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || data.detail?.code || `Request failed (${response.status})`);
    }
    return data;
  }
  function controls() {
    document.querySelectorAll('[onclick^="triggerConsoleScenario"],[onclick="resetConsoleGrid()"],#agent-execute').forEach(el => el.disabled = !session || session.role !== 'operator' || !healthy || busy);
    $('console-status-dot').style.background = healthy ? '#34d399' : '#f59e0b';
  }
  function connection(text) { say('connection-status',text); say('backend-connection-pill',text); controls(); }
  async function guarded(fn) { try { say('action-message',''); return await fn(); } catch(e) { say('action-message',e.message); } }
  async function login(body) {
    say('gateway-message','Connecting…');
    try {
      session = await api('/api/auth/login',{method:'POST',body:JSON.stringify(body)});
      sessionStorage.setItem('gridpulse_live_session',JSON.stringify(session));
      say('gateway-message',''); await enter();
    } catch(e) { say('gateway-message',e.message); }
  }
  window.handleGatewaySubmit = e => { e.preventDefault(); return login({username:$('login-email-input').value,password:$('login-pass-input').value,role:'operator',demo:false}); };
  window.loginWithRole = (username,role) => login({username,role:roles[role] || role,demo:true});
  window.signOut = () => {
    session=null; healthy=false; clearTimeout(reconnect); if(socket){socket.onclose=null;socket.close();} socket=null;
    sessionStorage.removeItem('gridpulse_live_session'); topology=null; audit=[];lastSnapshot=null;
    ['c-kpi-latency','c-kpi-hydro','c-kpi-load','c-kpi-bess','c-kpi-hospital'].forEach(id=>say(id,'—'));
    $('console-audit-tbody').replaceChildren(); say('console-mcp-log','Awaiting backend agent activity.');
    location.hash='gateway';setAppView('gateway');controls();
  };
  const switchPage = window.switchScadaPage;
  window.switchScadaPage = page => { switchPage(page); location.hash=page; if(page==='audit') guarded(loadAudit); };
  function route() {
    const page=location.hash.slice(1);
    if(['topology','cypher','mcp','audit'].includes(page)) {setAppView(session?'console':'gateway');if(session)switchPage(page);}
    else if(page==='gateway')setAppView('gateway');
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
    say('load-note',`SUB_METRO · ${number(m.city_load_pct,'%')}`);say('bess-note',`BESS · ${number(data.bess?.soc_pct,'% SOC')}`);say('hospital-note','Backend telemetry');
    say('scada-topo-heading',`${data.scenario?.name||'Grid'} · ${data.scenario?.status||'UNKNOWN'}`);
    say('console-mcp-log',data.agent_steps?.length?pretty(data.agent_steps):pretty(data.agent_log||'No agent activity.'));
    consoleScenario=data.scenario?.id||0;consoleMitigated=data.scenario?.status==='MITIGATED';
    if(topology){for(const node of topology.nodes){const fresh=[...(data.towers||[]),...(data.consumers||[]),data.bess,data.substation].find(n=>n?.id===node.id);if(fresh)Object.assign(node.properties,fresh);}for(const edge of topology.edges){const span=(data.spans||[]).find(s=>edge.id.startsWith(s.id+':'));if(span)edge.properties.status=edge.id.includes('UPPER_PRIMARY')?span.upper_status:span.lower_status;}}
  }
  async function health(){try{const h=await api('/health');healthy=h.status==='HEALTHY';connection(healthy?'CONNECTED · authenticated simulation':'DEGRADED · FalkorDB unavailable');}catch(e){healthy=false;connection(`OFFLINE · ${e.message}`);}}
  function connect(){
    if(!session)return;
    const url=new URL(base+'/ws/telemetry');url.protocol=url.protocol==='https:'?'wss:':'ws:';url.searchParams.set('token',session.access_token);
    socket=new WebSocket(url);socket.onmessage=e=>{try{snapshot(JSON.parse(e.data));}catch{say('action-message','Invalid telemetry received.');}};
    socket.onclose=e=>{healthy=false;connection('DISCONNECTED · reconnecting');if(e.code===1008){signOut();say('gateway-message','Session expired. Sign in again.');}else if(session)reconnect=setTimeout(()=>{health();connect();},3000);};
    socket.onerror=()=>{healthy=false;connection('Telemetry connection unavailable');};
  }
  async function enter(){
    say('console-operator-pill',`${session.operator_id} · ${session.role}`);location.hash=['topology','cypher','mcp','audit'].includes(location.hash.slice(1))?location.hash:'topology';route();
    await health();await Promise.allSettled([guarded(loadTopology),guarded(loadAudit),guarded(async()=>snapshot(await api('/api/sim/state')))]);connect();
  }
  window.triggerConsoleScenario=id=>guarded(async()=>{
    if(!healthy||session?.role!=='operator'||busy)throw Error('An operator session and healthy backend are required.');
    busy=true;controls();try{await api(`/api/sim/trigger-scenario/${id}`,{method:'POST'});say('action-message','Scenario accepted. Following backend telemetry.');await loadAudit();}finally{busy=false;controls();}
  });
  window.resetConsoleGrid=()=>guarded(async()=>{if(!healthy||session?.role!=='operator')throw Error('An operator session and healthy backend are required.');await api('/api/sim/reset',{method:'POST'});snapshot(await api('/api/sim/state'));await loadTopology();await loadAudit();});
  // The public corridor is an explicitly illustrative animation, never an unauthenticated grid mutation.
  window.triggerScenario=id=>{activeScenarioId=id||1;isFaultActive=true;isRerouted=false;lightningFlashAlpha=1;scrollToStation(id===1?.24:id===2?.42:.88);say('hud-status-title','ILLUSTRATION · Sign in to run a backend scenario');say('metric-latency','—');say('metric-city-load','Illustrative');say('metric-hospital','Not live');};
  window.triggerFaultSimulation=()=>triggerScenario(activeScenarioId);
  window.resetGrid=()=>{isFaultActive=false;isRerouted=false;say('hud-status-title','ILLUSTRATIVE CORRIDOR · Open console for live data');};
  window.toggleAutoCycle=enabled=>{clearInterval(autoCycleTimer);if(enabled){let id=1;triggerScenario(id);autoCycleTimer=setInterval(()=>triggerScenario(id=id%3+1),5500);}};
  $('trace-form').onsubmit=e=>{e.preventDefault();guarded(async()=>{say('trace-result','Tracing…');const result=await api('/api/graph/trace-cascade',{method:'POST',body:JSON.stringify({origin_id:$('trace-origin').value,max_hops:Number($('trace-hops').value)})});say('trace-result',pretty(result));say('console-cypher-pre',(result.evidence||[]).map(x=>x.cypher).filter(Boolean).join('\n\n')||'No Cypher evidence returned.');}).then(()=>{if($('trace-result').textContent==='Tracing…')say('trace-result','Trace unavailable. See the error above.');});};
  $('agent-form').onsubmit=e=>{e.preventDefault();guarded(async()=>{say('agent-result','Requesting agent advice…');try{const result=await api('/api/mcp/agent/advise',{method:'POST',body:JSON.stringify({question:$('agent-question').value,execute:$('agent-execute').checked})});say('agent-result',pretty(result));await loadAudit();}catch(error){say('agent-result',error.message);throw error;}});};
  $('audit-refresh').onclick=()=>guarded(loadAudit);
  $('audit-verify').onclick=()=>guarded(async()=>say('audit-result',pretty(await api('/api/audit/verify'))));
  $('audit-export').onclick=()=>guarded(async()=>{await loadAudit();const url=URL.createObjectURL(new Blob([pretty(audit)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='gridpulse-audit.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  window.gridpulseDrawTopology=(canvas,ctx)=>{
    if(!canvas||!ctx)return;const dpr=Math.min(devicePixelRatio||1,2),w=canvas.width/dpr,h=canvas.height/dpr;
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle=isNightMode?'#040814':'#f8fafc';ctx.fillRect(0,0,w,h);ctx.font='12px monospace';ctx.fillStyle='#94a3b8';
    if(!topology){ctx.fillText('Waiting for FalkorDB topology…',20,40);return;}
    const points={};const nodes=topology.nodes;nodes.forEach((n,i)=>{const tower=/^T(\d+)$/.exec(n.id);points[n.id]=tower?{x:w*(.12+Number(tower[1])*.075),y:h*.4}:{x:40+(i%5)*(w-80)/4,y:h*(i%2?.78:.16)};});
    for(const e of topology.edges){const a=points[e.source],b=points[e.target];if(!a||!b)continue;ctx.strokeStyle=/TRIPPED|OPEN|OFFLINE|FAULT/.test(e.properties?.status||'')?'#f43f5e':'#0ea5e9';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
    for(const n of nodes){const p=points[n.id];ctx.fillStyle=['TRIPPED','OFFLINE','FAILED'].includes(n.properties?.status)?'#f43f5e':'#34d399';ctx.beginPath();ctx.arc(p.x,p.y,7,0,Math.PI*2);ctx.fill();ctx.fillStyle=isNightMode?'#cbd5e1':'#334155';ctx.font=`${w<500?8:10}px monospace`;ctx.textAlign='center';ctx.fillText(n.id,p.x,p.y+22);}
    ctx.textAlign='left';ctx.fillStyle='#94a3b8';ctx.fillText(`${nodes.length} assets · ${topology.edges.length} connections`,16,h-16);
  };
  setInterval(()=>{if(session){health();if(lastSeen&&Date.now()-lastSeen>10000){healthy=false;connection('STALE · waiting for telemetry');}if(healthy)guarded(loadTopology);}},10000);
  localStorage.removeItem('gridpulse_session');resetGrid();controls();route();
  try{session=JSON.parse(sessionStorage.getItem('gridpulse_live_session'));}catch{session=null;}
  if(session)api('/api/auth/session').then(enter).catch(()=>signOut());
})();
