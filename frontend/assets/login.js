(() => {
  const API = location.port==='8000' ? location.origin : 'http://localhost:8000';
  const roles = { operator:'Chief Grid Operator', engineer:'GraphBLAS Topology Engineer', auditor:'Compliance Auditor' };
  const message = document.getElementById('auth-message');
  let busy = false;
  function setBusy(value) { busy = value; document.querySelectorAll('form button,.role-btn').forEach(b => b.disabled = value); }
  function saveSession(data, role, mode, username) {
    const token = data.access_token || data.token || null;
    const serverRole = data.role || data.user?.role;
    const accepted = Object.hasOwn(roles, serverRole) ? serverRole : role;
    const session = { role:accepted, name:data.user?.name || username || roles[accepted], mode, token, createdAt:Date.now(), expiresAt:Date.now() + Math.min(Number(data.expires_in) || 28800,86400) * 1000 };
    try {
      localStorage.setItem('gridpulse_session', JSON.stringify(session));
      localStorage.setItem('gridpulse_operator', JSON.stringify({ name:session.name, role:accepted }));
    } catch { throw new Error('Browser storage is unavailable. Enable storage for this page before signing in.'); }
    location.href = 'dashboard.html';
  }
  async function login(role, demo, credentials = {}) {
    if (busy) return;
    setBusy(true); message.textContent = demo ? 'Checking demo authentication service…' : 'Authenticating with the backend…';
    let response;
    try {
      response = await fetch(`${API}/api/auth/login`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ ...credentials, role, demo }), signal:AbortSignal.timeout(3500) });
    } catch {
      if (demo) {
        try { saveSession({},role,'demo',roles[role]); } catch (error) { message.textContent=error.message;setBusy(false); }
      } else { message.textContent='Authentication service unavailable. Try again or choose a demo role below.';setBusy(false); }
      return;
    }
    try {
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `Authentication rejected (${response.status}). Check the backend login contract.`);
      const data = result.data || result;
      if (!data.access_token && !data.token) throw new Error('The authentication response did not include a session token. Check the backend login response.');
      saveSession(data,role,'live',credentials.username);
    } catch (error) { message.textContent=error.message;setBusy(false); }
  }
  document.getElementById('login-form').addEventListener('submit',event => {
    event.preventDefault();const form = new FormData(event.target);
    login('operator',false,{username:form.get('username').trim(),password:form.get('password')});
  });
  document.querySelectorAll('[data-role]').forEach(button => button.addEventListener('click',() => login(button.dataset.role,true)));

  const canvas=document.getElementById('auth-network'),ctx=canvas.getContext('2d');
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  let width=0,height=0,frame=0;
  function size(){const dpr=Math.min(devicePixelRatio||1,2);width=innerWidth;height=innerHeight;canvas.width=width*dpr;canvas.height=height*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);draw(performance.now());}
  function draw(time){ctx.clearRect(0,0,width,height);const dark=document.documentElement.classList.contains('dark');const gap=100;
    for(let y=70,row=0;y<height;y+=gap,row++)for(let x=-60;x<width;x+=gap){const px=x+(row%2)*45,py=y+Math.sin(x*.005+row)*20;
      ctx.strokeStyle=dark?'rgba(56,189,248,.10)':'rgba(2,132,199,.12)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(px,py);ctx.lineTo(px+gap,py+Math.sin(row)*20);ctx.moveTo(px,py);ctx.lineTo(px+45,py+gap);ctx.stroke();
      const glow=.3+.7*(.5+.5*Math.sin(time*.0007+x*.005+row));ctx.fillStyle=dark?`rgba(56,189,248,${glow*.5})`:`rgba(2,132,199,${glow*.4})`;ctx.beginPath();ctx.arc(px,py,2,0,Math.PI*2);ctx.fill();}
  }
  function animate(t){if(!document.hidden)draw(t);frame=requestAnimationFrame(animate);}
  size();if(!reduced)frame=requestAnimationFrame(animate);addEventListener('resize',size);addEventListener('gridpulse-theme',()=>draw(performance.now()));addEventListener('pagehide',()=>cancelAnimationFrame(frame));
})();
