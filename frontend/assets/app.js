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
    say('load-note',`City demand · ${number(m.city_load_pct,'%')}`);say('bess-note',`BESS · ${number(data.bess?.soc_pct,'% SOC')}`);say('hospital-note','Backend telemetry');
    $('c-kpi-hospital').style.color=m.tier1_hospital_status==='PROTECTED'?'#10b981':m.tier1_hospital_status==='AT_RISK'?'#d97706':'#f43f5e';
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
  const topologyView=$('scada-topology-view'),fullscreenButton=$('topology-fullscreen-toggle');
  let topologyFallbackExpanded=false;
  function updateTopologyFullscreen(){
    const expanded=document.fullscreenElement===topologyView||topologyFallbackExpanded;
    fullscreenButton.textContent=expanded?'↙ Exit full screen':'⛶ Full screen';
    fullscreenButton.setAttribute('aria-label',expanded?'Exit topology full screen':'Enter topology full screen');
    fullscreenButton.setAttribute('aria-pressed',String(expanded));
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
  const topoColors={line:'#0ea5e9',flow:'#72f7ff',down:'#f43f5e',node:'#34d399'};
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
    const color=bad?topoColors.down:kind==='BESS'?'#a78bfa':kind==='HOSPITAL'?'#34d399':kind==='HydroPlant'?'#38bdf8':'#45caff';
    ctx.save();ctx.translate(x,y);ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1.7;ctx.lineJoin='round';ctx.lineCap='round';
    const path=(...coords)=>{ctx.beginPath();ctx.moveTo(coords[0],coords[1]);for(let i=2;i<coords.length;i+=2)ctx.lineTo(coords[i],coords[i+1]);ctx.stroke();};
    if(kind==='Tower'){
      path(0,-12,-8,10,8,10,0,-12);path(-5,2,5,2);path(-7,8,7,8);path(-9,-5,9,-5);path(-7,-1,7,-1);ctx.fillRect(-1,-15,2,3);
    }else if(kind==='HydroPlant'){
      ctx.strokeRect(-11,-8,22,18);path(-11,-3,11,-3);ctx.beginPath();ctx.arc(0,3,5,0,Math.PI*2);ctx.stroke();path(0,-2,0,8);path(-5,3,5,3);
    }else if(kind==='Substation'){
      ctx.strokeRect(-11,-9,22,19);ctx.strokeRect(-4,-5,8,11);path(-8,-13,-8,-9);path(8,-13,8,-9);path(-11,5,-15,5);path(11,5,15,5);
    }else if(kind==='BESS'){
      ctx.strokeRect(-11,-9,21,18);ctx.fillRect(10,-3,3,6);const bars=Math.max(0,Math.min(3,Math.ceil((node.properties?.soc_pct??0)/34)));for(let i=0;i<bars;i++)ctx.fillRect(-7+i*5,-5,3,10);
    }else if(kind==='HOSPITAL'){
      ctx.strokeRect(-10,-10,20,20);ctx.fillRect(-2,-7,4,14);ctx.fillRect(-7,-2,14,4);
    }else if(kind==='RESIDENTIAL'){
      path(-11,0,0,-10,11,0);path(-8,-2,-8,10,8,10,8,-2);ctx.strokeRect(-2,3,4,7);
    }else if(kind==='COMMERCIAL'){
      ctx.strokeRect(-10,-5,8,15);ctx.strokeRect(-1,-10,11,20);ctx.fillRect(3,-5,2,2);ctx.fillRect(3,0,2,2);ctx.fillRect(-7,0,2,2);
    }else if(kind==='INDUSTRIAL'){
      path(-11,9,-11,-3,-4,1,1,-3,6,1,10,-2,10,9,-11,9);path(6,-2,6,-11,9,-11,9,-2);
    }else{ctx.beginPath();ctx.arc(0,0,7,0,Math.PI*2);ctx.stroke();}
    ctx.font=`${compact?8:10}px "JetBrains Mono",monospace`;ctx.textAlign='center';ctx.textBaseline='top';ctx.fillStyle=dark?'#cbd5e1':'#334155';
    const labels={HYDRO_ALPINE_01:'HYDRO',BESS_METRO_01:'BESS',SUB_METRO:'SUBSTATION',HOSP_ST_JUDE:'HOSPITAL',RES_VALLEY_SUBURBS:'HOMES',COM_METRO_CORE:'METRO',IND_METRO_12:'INDUSTRY'};
    ctx.fillText(labels[node.id]||node.id,0,15);ctx.restore();
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
      const lane=parallel?(e.id.includes('UPPER_PRIMARY')?-3:3):0;
      const ox=-dy/length*lane,oy=dx/length*lane;
      const start={x:a.x+ox,y:a.y+oy},end={x:b.x+ox,y:b.y+oy};
      ctx.strokeStyle=inactive?(dark?'#6b3040':'#fda4af'):(dark?'#0ea5e9':'#0284c7');ctx.lineWidth=parallel?1.5:2;ctx.beginPath();ctx.moveTo(start.x,start.y);ctx.lineTo(end.x,end.y);ctx.stroke();
      if(inactive || reducedMotion)continue;
      const spacing=compact?54:70,count=Math.max(1,Math.floor(length/spacing));
      const speed=45;const phase=(now*speed+e.id.length*11)%length;
      ctx.save();ctx.shadowColor=topoColors.flow;ctx.shadowBlur=dark?9:3;
      for(let i=0;i<count;i++){
        const t=((phase+i*length/count)%length)/length;
        const x=start.x+dx*t,y=start.y+dy*t;
        ctx.fillStyle=dark?'#a5f3fc':'#0369a1';ctx.beginPath();ctx.arc(x,y,compact?2.2:2.7,0,Math.PI*2);ctx.fill();
      }
      ctx.restore();
    }
    for(const n of nodes){const p=points[n.id];nodeIcon(ctx,n,p.x,p.y,compact,dark);}
    ctx.font='10px "JetBrains Mono",monospace';ctx.textAlign='left';ctx.fillStyle=dark?'#94a3b8':'#475569';
    ctx.fillText(`${nodes.length} assets · ${topology.edges.length} connections`,16,h-15);
    ctx.fillStyle=topoColors.flow;ctx.beginPath();ctx.arc(w-117,h-19,2.5,0,Math.PI*2);ctx.fill();ctx.fillStyle=dark?'#cbd5e1':'#475569';ctx.fillText('Current flow',w-108,h-15);
  };
  setInterval(()=>{if(session){health();if(lastSeen&&Date.now()-lastSeen>10000){healthy=false;connection('STALE · waiting for telemetry');}if(healthy)guarded(loadTopology);}},10000);
  localStorage.removeItem('gridpulse_session');resetGrid();controls();route();
  try{session=JSON.parse(sessionStorage.getItem('gridpulse_live_session'));}catch{session=null;}
  if(session)api('/api/auth/session').then(enter).catch(()=>signOut());
})();
