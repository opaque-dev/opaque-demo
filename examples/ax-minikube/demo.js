const el=id=>document.getElementById(id);
const labels={awaiting_delegation:'Awaiting native human review',awaiting_scope_review:'Awaiting scope approval',ready:'Approved scope ready',running:'AX workload running',inspected:'Broker responses retained',held:'Held for inspection'};
let pending=false;
function text(tag,value){const n=document.createElement(tag);n.textContent=value;return n;}
function render(s){
  el('phase').textContent=labels[s.phase]||'Runner starting';
  el('run').disabled=pending||s.phase!=='ready'||!s.policy_verified;
  el('message').textContent=s.message||s.action_error||(s.phase==='ready'?'The broker activated the reviewed scope. Run the checked-in proposals once.':s.phase==='inspected'?'Responses retained. Unknown or missing outcomes do not authorize a retry.':'Complete native review using the operator review command. This page cannot grant approval.');
  const p=s.compiled_policy?.policy?.spec;
  el('statuses').textContent=p?.authority?.allowedStatuses?.join(', ')||'—';
  el('resources').textContent=p?.authority?.maxResources??'—';
  el('attempts').textContent=p?.authority?.maxAttempts??'—';
  el('duration').textContent=p?.authority?.maxDuration||'—';
  el('approval').textContent=p?.approval?.scope||'—';
  el('policy').textContent=s.policy_source||'Unavailable';
  el('scope').textContent=JSON.stringify(s.scope_request,null,2);
  el('policy-state').textContent=s.policy_verified?'MATCHED TO BROKER SNAPSHOT':'AWAITING AUTHENTICATED INSPECTION';
  el('observed').textContent=s.policy_observation?.digest||'No authenticated observation yet.';
  if(s.actions){el('actions').replaceChildren(...s.actions.map(a=>{const row=document.createElement('tr');row.append(text('td',a.action.action_key),text('td',a.action.status),text('td',a.response.error?'Denied: '+a.response.error.code:JSON.stringify(a.response.result)),text('td','No'));return row;}));}
  el('responses').textContent=s.actions?JSON.stringify(s.actions,null,2):'No broker action responses yet.';
  const rows=[['Expected policy digest',s.compiled_policy?.digest],['Broker setup stage',s.broker_stage],['Scope',s.scope_id],['Issuance review',s.issuance_round_id],['AX pod',s.pod_uid],['Namespace',s.namespace],['Opaque revision',s.core_revision],['AX revision',s.ax_revision]];
  el('bindings').replaceChildren(...rows.map(([k,v])=>{const row=document.createElement('div');row.append(text('dt',k),text('dd',v||'Not observed'));return row;}));
}
async function refresh(){try{const r=await fetch('/api/state');const s=await r.json();if(!r.ok)throw Error(s.error);render(s);}catch(e){el('phase').textContent='State unavailable';el('message').textContent=e.message;el('run').disabled=true;}}
el('run').addEventListener('click',async()=>{pending=true;el('run').disabled=true;try{const r=await fetch('/api/run',{method:'POST'});const s=await r.json();if(!r.ok)throw Error(s.error);await refresh();}catch(e){el('message').textContent=e.message;}finally{pending=false;}});
(async function poll(){await refresh();setTimeout(poll,3000);})();
