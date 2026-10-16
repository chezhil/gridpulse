/* Themed workspaces for the graph, network, and learning APIs. */
(() => {
  const $ = id => document.getElementById(id);
  const client = window.gridpulseClient;
  let templates = [], networks = [], sampleContent = '', importedSpec = null, loadedAt = 0;
  const say = (id, value) => { if ($(id)) $(id).textContent = value; };
  const showError = (id, error) => say(id, error.message || String(error));
  const pretty = value => JSON.stringify(value, null, 2);
  function options(id, rows, label, placeholder) {
    const select = $(id), value = select.value;
    select.replaceChildren();
    if (placeholder) select.add(new Option(placeholder, ''));
    rows.forEach(row => select.add(new Option(label(row), row.id)));
    if ([...select.options].some(o => o.value === value)) select.value = value;
  }
  function metrics(id, values) {
    const host=$(id);host.replaceChildren();
    Object.entries(values||{}).forEach(([key,value])=>{const item=document.createElement('span');item.textContent=`${key.replaceAll('_',' ')}: ${value??'—'}`;host.append(item);});
  }
  function list(id, rows, format, empty) {
    const host=$(id);host.replaceChildren();
    if(!rows?.length){host.textContent=empty;return;}
    rows.slice(-30).reverse().forEach(row=>{const item=document.createElement('article');item.textContent=format(row);host.append(item);});
  }
  async function loadSaved(force=false) {
    if(!client.session)return;
    if(!force && Date.now()-loadedAt<10000)return;
    const data=await client.api('/api/networks');
    networks=data.networks||[];loadedAt=Date.now();
    options('network-select',networks,n=>n.name||n.id,'Select a saved network');
    options('learning-network-select',networks,n=>n.name||n.id,'Select a saved network');
    options('ask-grid-network',networks,n=>n.name||n.id,'Live grid');
    const selected=networks.find(n=>n.id===$('network-select').value);
    say('network-summary',selected?`${selected.id} · ${selected.name} · ${selected.counts?.nodes??'—'} nodes`:`${networks.length} saved network${networks.length===1?'':'s'}`);
  }
  async function loadTemplates() {
    const data=await client.api('/api/networks/templates');templates=data.templates||[];
    options('network-template-select',templates,t=>t.name||t.id);
    describeTemplate();
  }
  function describeTemplate(){const t=templates.find(x=>x.id===$('network-template-select').value);say('network-template-info',t?`${t.description||''} · ${Object.entries(t.counts||{}).map(([k,v])=>`${v} ${k}`).join(' · ')}`:'No templates returned.');}
  async function loadLearning(){
    const [curve,playbooks]=await Promise.all([client.api('/api/graph/memory/learning'),client.api('/api/graph/memory/playbooks')]);
    metrics('learning-live-summary',curve.summary);
    list('learning-incidents',curve.incidents,r=>`${r.at||''} · ${r.signature||r.id} · ${r.status||''} · ${r.source||''}`,'No incidents recorded yet.');
    list('learning-playbooks',playbooks.playbooks,r=>`${r.signature||r.id} · ${r.success_count??0} successes · ${r.failure_count??0} failures${r.retired?' · retired':''}`,'No playbooks learned yet.');
  }
  async function loadNetworkMemory(){
    const id=$('learning-network-select').value;
    if(!id){$('learning-network-summary').replaceChildren();say('learning-network-incidents','Select a saved network.');return;}
    const data=await client.api(`/api/networks/${encodeURIComponent(id)}/memory`);
    metrics('learning-network-summary',data.summary);
    list('learning-network-incidents',data.incidents,r=>`${r.at||''} · ${r.signature||r.id} · ${r.status||''}`,'No rehearsals recorded for this network.');
  }
  async function activate(page){
    if(!client.session)return;
    try{
      if(page==='askgrid')await loadSaved();
      if(page==='networks')await Promise.all([loadSaved(true),loadTemplates()]);
      if(page==='learning')await Promise.all([loadSaved(),loadLearning()]).then(loadNetworkMemory);
    }catch(error){showError(page==='askgrid'?'ask-grid-status':page==='learning'?'learning-status':'network-status',error);}
  }
  // Query rows as a table: graph paths read as A → B, nested values stay compact.
  function renderRows(box,rows){
    box.replaceChildren();if(!rows.length){box.textContent='No rows.';return;}
    const cell=v=>v===null||v===undefined?'—':Array.isArray(v)?v.map(x=>typeof x==='object'&&x?x.id??JSON.stringify(x):x).join(' → '):typeof v==='object'?(v.id??Object.entries(v).map(([k,x])=>`${k}: ${typeof x==='object'?JSON.stringify(x):x}`).join(', ')):String(v);
    const cols=[...new Set(rows.flatMap(r=>Object.keys(r)))];
    const table=document.createElement('table');table.className='trace-table';const head=table.insertRow();
    cols.forEach(c=>{const th=document.createElement('th');th.textContent=c;head.append(th);});
    rows.slice(0,200).forEach(r=>{const tr=table.insertRow();cols.forEach(c=>tr.insertCell().textContent=cell(r[c]));});
    const wrap=document.createElement('div');wrap.style.overflowX='auto';wrap.append(table);box.append(wrap);
    if(rows.length>200)box.append(Object.assign(document.createElement('p'),{className:'feature-meta',textContent:`Showing 200 of ${rows.length} rows.`}));
  }
  $('ask-grid-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=event.submitter||$('ask-grid-form').querySelector('button');button.disabled=true;say('ask-grid-status','Querying the graph…');
    try{const data=await client.api('/api/mcp/ask',{method:'POST',body:JSON.stringify({question:$('ask-grid-question').value,network_id:$('ask-grid-network').value||null}),timeoutMs:60000});
      say('ask-grid-answer',data.answer||data.explanation||'No answer returned.');say('ask-grid-cypher',data.cypher||'No query generated.');say('ask-grid-meta',data.answerable?`${data.row_count??0} rows · ${data.server_ms??'—'} ms · evidence ${data.evidence_hash||'—'}`:'The question could not be answered.');renderRows($('ask-grid-rows'),data.rows||[]);say('ask-grid-status','Read-only query complete.');
    }catch(error){showError('ask-grid-status',error);}finally{button.disabled=false;}
  });
  $('network-template-select').addEventListener('change',describeTemplate);
  $('network-template-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=event.submitter||$('network-template-form').querySelector('button');button.disabled=true;say('network-status','Creating network…');
    try{const name=$('network-name').value.trim();const data=await client.api('/api/networks',{method:'POST',body:JSON.stringify({template:$('network-template-select').value,...(name?{name}:{})})});loadedAt=0;await loadSaved(true);$('network-select').value=data.id;say('network-status',`Created ${data.name||data.id}. Open the visual editor to adjust it.`);}
    catch(error){showError('network-status',error);}finally{button.disabled=false;}
  });
  $('network-refresh').addEventListener('click',()=>loadSaved(true).catch(error=>showError('network-status',error)));
  $('network-select').addEventListener('change',()=>{const n=networks.find(x=>x.id===$('network-select').value);say('network-summary',n?`${n.name} · ${n.id}`:'Select a network to inspect it in the advanced Lab.');});
  $('network-load-sample').addEventListener('click',async()=>{
    try{say('network-import-info','Loading sample…');const id=$('network-sample').value;const response=await fetch(`${client.base}/api/networks/samples/${id}`);if(!response.ok)throw Error(`Sample unavailable (${response.status})`);sampleContent=await response.text();$('network-file').value='';importedSpec=null;$('network-save-import').disabled=true;say('network-import-info',`${id.toUpperCase()} loaded. Convert and validate to preview the network.`);}catch(error){showError('network-import-info',error);}
  });
  $('network-file').addEventListener('change',()=>{sampleContent='';importedSpec=null;$('network-save-import').disabled=true;say('network-import-info',$('network-file').files[0]?.name||'Choose a file or sample.');});
  $('network-convert').addEventListener('click',async()=>{
    const button=$('network-convert');button.disabled=true;say('network-import-info','Converting and validating…');
    try{const file=$('network-file').files[0];const content=file?await file.text():sampleContent;if(!content)throw Error('Choose a file or load a sample first.');
      const format=file?.name.toLowerCase().endsWith('.json')?'gridpulse':'matpower';const data=await client.api('/api/networks/convert',{method:'POST',body:JSON.stringify({format,content,name:file?.name||$('network-sample').value}),timeoutMs:60000});
      importedSpec=data.spec;$('network-save-import').disabled=false;
      say('network-import-info',`Valid ${importedSpec.name||'network'} · ${(importedSpec.buses?.length??0)+(importedSpec.substations?.length??0)} grid nodes · ${importedSpec.lines?.length??0} lines · ${data.warnings?.length??0} warnings. Save when ready.`);
    }catch(error){importedSpec=null;$('network-save-import').disabled=true;showError('network-import-info',error);}finally{button.disabled=false;}
  });
  $('network-save-import').addEventListener('click',async()=>{
    const button=$('network-save-import');button.disabled=true;
    try{if(!importedSpec)throw Error('Convert a file first.');const data=await client.api('/api/networks',{method:'POST',body:JSON.stringify({spec:importedSpec}),timeoutMs:60000});loadedAt=0;await loadSaved(true);$('network-select').value=data.id;say('network-status',`Saved imported network ${data.name||data.id}.`);}
    catch(error){showError('network-status',error);button.disabled=false;}
  });
  $('network-editor-link').addEventListener('click',()=>{const s=client.session;if(!s)return;sessionStorage.setItem('gp_lab_token',s.access_token);sessionStorage.setItem('gp_lab_role',s.role);sessionStorage.setItem('gp_lab_user',s.operator_id);});
  $('learning-network-select').addEventListener('change',()=>loadNetworkMemory().catch(error=>showError('learning-status',error)));
  $('learning-refresh').addEventListener('click',()=>activate('learning'));
  $('learning-rehearse-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=event.submitter||$('learning-rehearse-form').querySelector('button');button.disabled=true;say('learning-status','Rehearsing critical outages and demand changes…');
    try{const id=$('learning-network-select').value;if(!id)throw Error('Select a saved network.');const data=await client.api(`/api/networks/${encodeURIComponent(id)}/rehearse`,{method:'POST',body:JSON.stringify({max_cases:Number($('learning-cases').value)}),timeoutMs:120000});say('learning-status',`Completed ${data.cases??0} cases · saved ${data.saved??0} · new playbooks ${data.new_playbooks??0} · reused ${data.solved_from_memory??0}.`);await loadNetworkMemory();}
    catch(error){showError('learning-status',error);}finally{button.disabled=false;}
  });
  window.gridpulseFeatures={activate,reset(){templates=[];networks=[];sampleContent='';importedSpec=null;loadedAt=0;$('network-save-import').disabled=true;
    // The next operator must not see the previous one's answers.
    ['ask-grid-answer','ask-grid-cypher','ask-grid-meta','ask-grid-status','learning-status','network-status'].forEach(id=>{if($(id))$(id).textContent='';});$('ask-grid-rows')?.replaceChildren();}};
  if(client.session)activate(location.hash.slice(1));
})();
