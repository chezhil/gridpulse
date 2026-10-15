/* Live browser acceptance check. Requires Playwright + a running Compose app. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const base=process.env.GRIDPULSE_URL||'http://localhost:8000';
const out=process.env.GRIDPULSE_BROWSER_OUTPUT||'work/browser-results';
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const b=await chromium.launch({headless:true});
 const p=await b.newPage({viewport:{width:1440,height:1000}});
 const errors=[],events=[],mutations=[];
 p.on('pageerror',e=>errors.push(e.message));
 p.on('request',r=>{if(r.method()==='POST'&&r.url().includes('/api/'))mutations.push(r.url());});
 p.on('websocket',ws=>ws.on('framereceived',f=>{try{events.push(JSON.parse(f.payload))}catch{}}));
 await p.addInitScript(()=>{const WS=window.WebSocket;window.__testSockets=[];window.WebSocket=class extends WS{constructor(...args){super(...args);window.__testSockets.push(this)}}});
 const screenshot=async name=>p.screenshot({path:`${out}/${name}.png`,fullPage:!name.startsWith('landing')});
 const live=()=>p.waitForFunction(()=>document.querySelector('#connection-text')?.textContent.includes('LIVE BACKEND'));
 const nominal=()=>p.waitForFunction(()=>document.querySelector('#system-status').textContent.includes('NOMINAL'));
 const reset=async()=>{await p.locator('#reset-grid').click();await nominal();};
 const fits=async()=>assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`Horizontal overflow: ${p.url()}`);
 await p.goto(base);await p.waitForSelector('#realistic-world-canvas');
 await p.evaluate(()=>window.gridpulseTheme.apply('dark'));await p.waitForTimeout(1200);await screenshot('landing-dark');
 await p.locator('#theme-toggle-btn').click();assert.equal(await p.locator('html').evaluate(n=>n.classList.contains('dark')),false);await p.waitForTimeout(1800);await screenshot('landing-light');
 await p.setViewportSize({width:390,height:844});await fits();await screenshot('landing-narrow');
 await p.locator('[data-console-link]').click();await fits();await screenshot('login-narrow');
 await p.locator('[data-role="operator"]').click();await p.waitForURL('**/dashboard.html');await live();await fits();
 await p.setViewportSize({width:1440,height:1000});
 for(const id of [1,2,3]){
  await reset();const start=events.length;
  await p.locator(`[data-scenario="${id}"]`).click();
  await p.waitForFunction(()=>document.querySelector('#system-status').textContent.includes('MITIGATED'));
  await p.waitForFunction(()=>document.querySelector('#audit-list').textContent.includes('MITIGATION_VERIFIED'));
  const run=events.slice(start).find(e=>e.scenario?.status==='MITIGATED');assert.ok(run);assert.equal(run.metrics.bess_injection_mw,45);
  assert.equal(await p.locator('#hospital-state').textContent(),'Protected');
  assert.ok((await p.locator('#query-code').textContent()).includes('MATCH'));
  const paths=await p.locator('#topology [data-edge-id]').evaluateAll(ns=>ns.map(n=>({id:n.dataset.edgeId,from:n.dataset.from,to:n.dataset.to,cls:n.getAttribute('class')})));
  assert.deepEqual(paths.filter(n=>n.id==='TX_882').map(n=>[n.from,n.to]),[['T3','T4']]);
  assert.deepEqual(paths.filter(n=>n.id==='TX_888').map(n=>[n.from,n.to]),[['T9','SUB_METRO']]);
  assert.ok(paths.find(n=>n.from==='BESS_METRO_01').cls.includes('reroute'));
  assert.ok(paths.find(n=>n.to==='IND_METRO_12').cls.includes('fault'));
  if(id<3){const span=id===1?'TX_882':'TX_884';assert.ok(paths.find(n=>n.id===span).cls.includes('fault'));assert.ok((await p.locator(`[data-backup-span="${span}"]`).getAttribute('class')).includes('reroute'));}
  else assert.ok(events.slice(start).some(e=>e.metrics?.city_load_pct===120&&e.metrics.city_load_mw===1008),'No exactly 120% heatwave telemetry');
  await screenshot(`scenario-${id}`);
  console.log(JSON.stringify({scenario:id,metrics:run.metrics,run_id:run.run_id}));
 }
 await p.locator('[data-theme-toggle]').click();await screenshot('dashboard-dark');
 await p.setViewportSize({width:390,height:844});await fits();await screenshot('dashboard-narrow-dark');
 await p.locator('[data-theme-toggle]').click();await fits();await screenshot('dashboard-narrow-light');
 await reset();await p.locator('[data-scenario="1"]').click();await p.locator('#reset-grid').click();await nominal();
 await p.waitForTimeout(800);assert.equal(await p.locator('#bess-mw').textContent(),'0');
 await p.evaluate(()=>window.__testSockets.forEach(ws=>ws.close()));
 await p.waitForFunction(()=>document.querySelector('#connection-text').textContent.includes('CONNECTION LOST'));await live();
 console.log('Reset during run and WebSocket reconnect passed');
 await p.getByRole('button',{name:'Offline simulation',exact:true}).click();
 const count=mutations.length;await p.locator('[data-scenario="3"]').click();await p.waitForTimeout(3500);
 assert.equal(mutations.length,count);assert.equal(await p.locator('#graph-latency').textContent(),'—');
 assert.ok((await p.locator('#connection-text').textContent()).includes('OFFLINE SIMULATION'));
 for(const role of ['engineer','auditor']){
  await p.locator('#mobile-sign-out').click();await p.waitForURL('**/login.html');await p.locator(`[data-role="${role}"]`).click();await live();
  assert.equal(await p.locator('[data-scenario="1"]').isDisabled(),true);assert.equal(await p.locator('#reset-grid').isDisabled(),true);
 }
 await p.route('**/api/graph/topology',r=>r.abort());await p.locator('#reconnect').click();
 await p.waitForFunction(()=>document.querySelector('#connection-text').textContent.includes('BACKEND UNAVAILABLE'));
 await p.unroute('**/api/graph/topology');await live();
 await p.evaluate(()=>{const s=JSON.parse(localStorage.getItem('gridpulse_session'));s.token='expired-acceptance-token';localStorage.setItem('gridpulse_session',JSON.stringify(s));});
 await p.reload();await p.waitForFunction(()=>document.querySelector('#connection-text').textContent.includes('SESSION EXPIRED'));
 assert.deepEqual(errors,[]);console.log('Browser acceptance passed: themes, 390px/1440px, three scenarios, graph/audit, canonical mapping, reset, reconnect, offline and read-only roles.');
 await b.close();
})().catch(e=>{console.error(e);process.exit(1)});
