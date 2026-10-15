(() => {
  let active=0,mitigated=false,timer=null,cycle=null;
  try{const s=JSON.parse(localStorage.getItem('gridpulse_session'));if(s?.expiresAt>Date.now())document.querySelectorAll('[data-console-link]').forEach(a=>a.href='dashboard.html');}catch{}
  const originalReset=window.resetGridSimulation;
  function resetState(){clearTimeout(timer);clearTimeout(cycle);active=0;mitigated=false;originalReset();document.querySelectorAll('[data-landing-scenario]').forEach(b=>b.setAttribute('aria-pressed','false'));}
  window.resetGridSimulation=()=>{resetState();document.getElementById('landing-auto').checked=false;};
  function run(id){resetState();active=id;document.querySelectorAll('[data-landing-scenario]').forEach(b=>b.setAttribute('aria-pressed',String(Number(b.dataset.landingScenario)===id)));isFaultActive=id===1;lightningFlashAlpha=id===1?1:0;window.scrollToStation(id===1?.24:id===2?.54:.97);window.selectCypherTab(1);timer=setTimeout(()=>{mitigated=true;isRerouted=id===1;window.selectCypherTab(2);if(document.getElementById('landing-auto').checked)cycle=setTimeout(()=>run(id%3+1),8500);},2200);}
  window.triggerFaultSimulation=()=>run(1);
  document.querySelectorAll('[data-landing-scenario]').forEach(b=>b.addEventListener('click',()=>run(Number(b.dataset.landingScenario))));
  document.getElementById('landing-auto').addEventListener('change',event=>{clearTimeout(cycle);if(event.target.checked)run(active||1);});
  const originalTransmission=drawTransmissionLinesAndFlow;
  drawTransmissionLinesAndFlow=function(){originalTransmission();if(active!==2)return;const a=towers[4],b=towers[5];ctx.save();ctx.strokeStyle=mitigated?'#34d399':'#fbbf24';ctx.lineWidth=3;ctx.setLineDash([12,9]);ctx.lineDashOffset=-animTime*30;ctx.beginPath();ctx.moveTo(a.x,a.baseY-a.h*.75);ctx.quadraticCurveTo((a.x+b.x)/2,a.baseY-a.h*.6,b.x,b.baseY-b.h*.75);ctx.stroke();ctx.setLineDash([]);if(!mitigated)for(let i=0;i<9;i++){const t=(animTime*.13+i*.11)%1,x=a.x+(b.x-a.x)*(i/9)+Math.sin(animTime+i)*22,y=840-t*260,r=25+t*55;const g=ctx.createRadialGradient(x,y,0,x,y,r);g.addColorStop(0,`rgba(251,146,60,${(1-t)*.18})`);g.addColorStop(1,'rgba(251,146,60,0)');ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();}ctx.restore();};
  const originalCity=drawMetropolitanSmartCity;
  drawMetropolitanSmartCity=function(camX){originalCity(camX);if(active!==3||mitigated)return;ctx.save();for(let i=0;i<7;i++){const x=5650+i*200,y=800+Math.sin(animTime*1.5+i)*28;ctx.strokeStyle=`rgba(251,191,36,${.15+.1*Math.sin(animTime*2+i)})`;ctx.lineWidth=3;ctx.beginPath();ctx.ellipse(x,y,90,155,0,Math.PI,Math.PI*2);ctx.stroke();}ctx.restore();};
  const originalUI=updateUIReadouts;
  updateUIReadouts=function(...args){originalUI(...args);if(!active)return;const name=['','LIGHTNING · TX_882','WILDFIRE · T5–T6','HEATWAVE · 120% LOAD'][active];document.getElementById('hud-status-title').textContent=`SIMULATION: ${name} · ${mitigated?'MITIGATED':'DETECTED'}`;document.getElementById('agent-log-line').textContent=mitigated?'Visual demo: Tier-3 load shed; BESS support; hospital protected.':'Visual demo: evaluating cascade and available mitigation assets.';if(active===3)document.getElementById('metric-city-load').textContent=mitigated?'104% (Simulated)':'120% (Simulated)';};
  addEventListener('pagehide',()=>{clearTimeout(timer);clearTimeout(cycle);});
})();
