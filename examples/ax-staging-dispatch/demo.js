const el=id=>document.getElementById(id);
const labels={awaiting_delegation:'Awaiting native human review',awaiting_scope_review:'Awaiting scope approval',ready:'Approved scope ready',running:'AX workload running',inspected:'Broker responses retained',held:'Held for inspection'};
let pending=false;
function text(tag,value){const n=document.createElement(tag);n.textContent=value;return n;}
function response(a){if(!a.response)return a.state==='not_attempted'?'Not attempted':'Answer lost (worker killed)';if(a.response.error)return'Denied: '+a.response.error.code;const r=a.response.result||{};return'state '+(r.state||'?')+(r.action&&r.action.resource_version?' @ '+r.action.resource_version.slice(0,12):'');}
function render(s){
  el('phase').textContent=labels[s.phase]||'Runner starting';
  el('run').disabled=pending||s.phase!=='ready'||!s.policy_verified;
  el('message').textContent=s.message||s.action_error||(s.phase==='ready'?'The broker activated the reviewed scope. Run the checked-in proposals once.':s.phase==='inspected'?'Responses retained. Unknown or missing outcomes do not authorize a retry.':'Complete native review with the numbered demo.py commands. This page cannot grant approval.');
  const p=s.compiled_policy?.policy?.spec;const w=p?.authority?.workflows?.[0];
  el('repository').textContent=w?.repository||'pending';
  el('workflow').textContent=w?.path||'pending';
  el('ref').textContent=w?.ref||'pending';
  el('attempts').textContent=p?.authority?.maxAttempts??'pending';
  el('duration').textContent=p?.authority?.maxDuration||'pending';
  el('approval').textContent=p?.approval?.scope||'pending';
  el('action-approval').textContent=p?.approval?.action||'pending';
  el('policy').textContent=s.policy_source||'Unavailable';
  el('scope').textContent=JSON.stringify(s.scope_request,null,2);
  el('policy-state').textContent=s.policy_verified?'MATCHED TO BROKER SNAPSHOT':'AWAITING AUTHENTICATED INSPECTION';
  el('observed').textContent=s.policy_observation?.digest||'No authenticated observation yet.';
  el('token').textContent=s.token_custody?'/var/lib/opaque/github.token in the broker pod: mode/uid/gid/links '+s.token_custody:'Not observed.';
  if(s.actions){el('actions').replaceChildren(...s.actions.map(a=>{const row=document.createElement('tr');row.append(text('td',a.action.action_key),text('td',a.action.resource.split(':')[1]+' @ '+a.action.resource.split(':')[2]),text('td',response(a)),text('td',(a.state||'pending')+(a.reconciled?' (read back with scope outcome)':'')),text('td','No'));return row;}));}
  el('responses').textContent=s.actions?JSON.stringify(s.actions,null,2):'No broker action responses yet.';
  const rows=[['Expected policy digest',s.compiled_policy?.digest],['Broker setup stage',s.broker_stage],['Scope',s.scope_id],['Issuance review',s.issuance_round_id],['Run variant',s.variant],['AX pod',s.pod_uid],['Namespace',s.namespace],['Opaque revision',s.core_revision],['AX revision',s.ax_revision]];
  el('bindings').replaceChildren(...rows.map(([k,v])=>{const row=document.createElement('div');row.append(text('dt',k),text('dd',v||'Not observed'));return row;}));
}
async function refresh(){try{const r=await fetch('/api/state');const s=await r.json();if(!r.ok)throw Error(s.error);render(s);}catch(e){el('phase').textContent='State unavailable';el('message').textContent=e.message;el('run').disabled=true;}}
el('run').addEventListener('click',async()=>{pending=true;el('run').disabled=true;try{const r=await fetch('/api/run',{method:'POST'});const s=await r.json();if(!r.ok)throw Error(s.error);await refresh();}catch(e){el('message').textContent=e.message;}finally{pending=false;}});
(async function poll(){await refresh();setTimeout(poll,3000);})();
