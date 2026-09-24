const el=id=>document.getElementById(id);
const labels={awaiting_start:'Ready to run',running:'Experiment running',inspecting:'Verifying retained evidence',inspected:'Evidence inspected',recovered:'Pod replacement verified',held:'Held for inspection',starting:'Runner starting'};
let pending=false;
function text(tag,value,cls){const n=document.createElement(tag);n.textContent=value;if(cls)n.className=cls;return n;}
function render(s){
  el('phase').textContent=s.busy?'Cluster action in progress':labels[s.phase]||'State unavailable';
  el('run').disabled=pending||s.busy||s.phase!=='awaiting_start';
  el('restart').disabled=pending||s.busy||!['inspected','recovered'].includes(s.phase);
  el('message').textContent=s.action_error||s.error||(s.busy?'Waiting for the cluster. No work is retried automatically.':s.phase==='awaiting_start'?'Starting this demo requests a synthetic experiment. It is not a native human approval.':s.phase==='recovered'?'A fresh AX runner read the existing result. No additional attempt was authorized.':s.phase==='inspected'?'Evidence verified. Replace the pod to inspect persistence across a new AX process.':'Waiting for observed results…');
  for(const [id,key] of Object.entries({charged:'charged_attempts',unknown:'unknown',accepted:'api_accepted',denied:'budget_denials'}))el(id).textContent=s[key]??'—';
  el('recovery').hidden=!(s.pod_replaced&&s.recovered_existing_run&&s.phase==='recovered');
  el('observation').textContent=s.actions?'OBSERVED IN THIS RUN':'NO OBSERVATION YET';
  if(s.actions){el('actions').replaceChildren(...s.actions.map((a,i)=>{const row=document.createElement('tr');row.append(text('td',`WORKER ${String(i+1).padStart(2,'0')}`),text('td',a.execution.replaceAll('_',' '),'state '+a.execution),text('td',a.execution==='api_accepted'?'Inspect provider completion':'Hold for reconciliation'),text('td',a.retry_authorized?'Yes':'No'));return row;}));}
  const rows=[['Logical run',s.run_id],['Checkpoint SHA-256',s.checkpoint_sha256],['Current pod',s.pod_uid],['First pod',s.first_pod_uid],['Namespace',s.namespace],['Opaque revision',s.core_revision],['AX revision',s.ax_revision]];
  el('bindings').replaceChildren(...rows.map(([k,v])=>{const row=document.createElement('div');row.append(text('dt',k),text('dd',v||'Not observed'));return row;}));
  const keys=['crash','scope_revoked','changed_effect_rejected','altered_export_rejected','review_signatures_verified','pod_replaced','recovered_existing_run','approval','native_human_review','live_broker_rpc','agent_substrate','kubernetes_pod','provider_effects','independent_evaluation'];
  el('checks').textContent=JSON.stringify(Object.fromEntries(keys.filter(k=>k in s).map(k=>[k,s[k]])),null,2);
}
async function refresh(){try{const r=await fetch('/api/state');const s=await r.json();if(!r.ok)throw Error(s.error);render(s);}catch(e){el('phase').textContent='State unavailable';el('message').textContent=e.message;el('run').disabled=true;el('restart').disabled=true;}}
async function action(kind){pending=true;el('run').disabled=true;el('restart').disabled=true;try{const r=await fetch('/api/'+kind,{method:'POST'});const s=await r.json();if(!r.ok)throw Error(s.error);await refresh();}catch(e){el('message').textContent=e.message;}finally{pending=false;}}
el('run').addEventListener('click',()=>action('run'));el('restart').addEventListener('click',()=>action('restart'));
(async function poll(){await refresh();setTimeout(poll,2500);})();
