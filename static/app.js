"use strict";
let selected = null, token = "", lastTask = "", lastTasks = "", lastInventory = "";
const $ = id => document.getElementById(id);
const money = n => new Intl.NumberFormat("en-US", {style:"currency",currency:"USD"}).format(n/100);
function el(tag, text, className) {const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(className)n.className=className; return n;}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Relay-Token":token},body:JSON.stringify(body)});
  const data = await response.json(); if(!response.ok)throw new Error(data.error || "Request failed"); return data;
}
function renderTasks(tasks) {
  const key=JSON.stringify(tasks)+selected;if(key===lastTasks)return;lastTasks=key;
  $("task-count").textContent=tasks.length;$("task-list").replaceChildren();
  if(!tasks.length)$("task-list").append(el("p","Your first request starts here.","muted"));
  tasks.forEach(t=>{const b=el("button",undefined,"task-button"+(t.id===selected?" active":""));b.append(el("span",t.goal),el("small",t.status.replaceAll("_"," ")));b.onclick=()=>{selected=t.id;lastTask="";refresh();};$("task-list").append(b);});
}
function renderCompany(company) {
  const key=JSON.stringify(company.inventory)+JSON.stringify(company.policy)+JSON.stringify(company.surplus);if(key===lastInventory)return;lastInventory=key;
  $("inventory").replaceChildren(); company.inventory.forEach(item=>{const row=el("div",undefined,"inventory-row");const spare=(company.surplus||[]).find(s=>s.sku===item.sku);row.append(el("span",item.sku.toLowerCase()),el("strong",`${item.quantity} ready / ${spare?spare.quantity:0} surplus`));$("inventory").append(row);});
  $("policy").textContent=`Policy v${company.policy.version}: human approval above ${money(company.policy.approval_cents)}. Approved suppliers only. Order cap ${money(company.policy.max_order_cents)}.`;
}
function renderTask(task) {
  const key=JSON.stringify(task);if(key===lastTask)return;lastTask=key;
  const expanded=new Set([...$("events").querySelectorAll("details[open]")].map(n=>n.dataset.seq));
  $("empty").hidden=true;$("task-detail").hidden=false;$("task-id").textContent="TASK / "+task.id.slice(0,8);
  $("task-goal").textContent=task.goal;$("task-status").textContent=task.status.replaceAll("_"," ");$("task-status").className="badge "+task.status;
  $("contract").textContent=`${task.intent.quantity??'?'} × ${task.intent.sku.toLowerCase()} · budget ${task.intent.budget_cents?money(task.intent.budget_cents):'needs clarification'} · ${task.memory.allow_transfer?'internal transfer or purchase':'purchase requested'}`;
  renderClarification(task);
  $("progress").replaceChildren();for(let i=0;i<9;i++)$("progress").append(el("span",undefined,task.status==='completed'||i<task.memory.facts.length?"done":""));
  $("export").href=`/api/tasks/${task.id}/evidence`;
  const attention=$("attention");attention.hidden=!['needs_attention','blocked','rejected','failed'].includes(task.status);attention.replaceChildren();
  if(!attention.hidden){const last=task.events[task.events.length-1];attention.append(el("h3",task.status==='needs_attention'?"Outcome needs investigation":"Task stopped"),el("p",last.message,"muted"));if(task.status==='needs_attention')attention.append(el("p","A purchase may already exist. Review the exported evidence before creating another request.","helper"));}
  const approval=task.approvals.find(a=>a.status==="pending");const panel=$("approval");panel.hidden=task.status!=="awaiting_approval"||!approval;panel.replaceChildren();
  if(!panel.hidden){
    const q=approval.payload;panel.append(el("h3","Your approval is needed"),el("p","Review this exact purchase. A changed quote requires a new decision.","helper"));
    const grid=el("div",undefined,"quote-grid");[["SUPPLIER",q.vendor_name],["TOTAL",money(q.total_cents)],["ITEMS",`${q.quantity} × ${q.sku.toLowerCase()}`],["QUOTE EXPIRES",new Date(q.expires_at*1000).toLocaleTimeString()]].forEach(([label,value])=>{const cell=el("div");cell.append(el("span",label),el("strong",value));grid.append(cell);});panel.append(grid,el("p","Fingerprint: "+approval.fingerprint,"fingerprint"));
    const actions=el("div",undefined,"approval-actions");[true,false].forEach(approved=>{const b=el("button",approved?"Approve purchase":"Reject",approved?"approve":"reject");b.onclick=async()=>{actions.querySelectorAll("button").forEach(x=>x.disabled=true);try{await api(`/api/tasks/${task.id}/decision`,{approval_id:approval.id,approved});lastTask="";await refresh();}catch(e){$("error").textContent=e.message;lastTask="";await refresh();}};actions.append(b);});panel.append(actions);
  }
  const evidence=$("evidence");evidence.hidden=task.status!=="completed";evidence.replaceChildren();
  if(!evidence.hidden){const proof=task.memory.outputs.verify_outcome||task.memory.outputs.verify_transfer;evidence.append(el("h3","✓ Outcome verified"),el("p",`${proof.route==='internal_transfer'?'Internal transfer / no purchase':proof.order_id} · ${money(proof.total_cents)} · stock ${proof.stock_before} → ${proof.stock_after}`,"helper"));Object.entries(proof.checks).forEach(([name,passed])=>evidence.append(el("div",(passed?"✓ ":"✕ ")+name.replaceAll("_"," "),"check")));const a=el("a","Download receipt ↓","text-link");a.href=`/api/tasks/${task.id}/receipt`;evidence.append(a);}
  $("events").replaceChildren();task.events.forEach(event=>{const d=el("details",undefined,"event "+event.kind);d.dataset.seq=String(event.seq);d.open=expanded.has(String(event.seq));const s=el("summary");s.append(el("span",`${String(event.seq).padStart(3,"0")} / ${event.kind.replaceAll("_"," ")}`,"event-kind"),el("span",event.message));d.append(s,el("pre",JSON.stringify(event.detail,null,2)));$("events").append(d);});
}
async function refresh(){try{const state=await api("/api/state");token=state.token;$("interpreter").textContent=state.interpreter;$("connector").textContent=state.connector;if(!selected&&state.tasks.length)selected=state.tasks[0].id;renderTasks(state.tasks);renderCompany(state.company);if(selected)renderTask(await api("/api/tasks/"+selected));}catch(error){$("error").textContent=error.message;}}
function renderClarification(task) {
  const panel=$("clarification");panel.hidden=task.status!=="needs_input";panel.replaceChildren();
  if(panel.hidden)return;
  panel.append(el("h3","A little context before I act"),el("p","I need explicit limits. Nothing has been purchased or moved.","helper"));
  const form=el("form");form.id="clarify-form";
  for(const field of task.memory.missing){
    const label=el("label",field==='quantity'?'How many additional units?':'Maximum budget (USD)');
    const input=el("input");input.type="number";input.name=field;input.id="clarify-"+field;input.required=true;
    input.min=field==='quantity'?'1':'0.01';input.max=field==='quantity'?'100':'100000';input.step=field==='quantity'?'1':'0.01';
    label.htmlFor=input.id;form.append(label,input);
  }
  const submit=el("button","Continue this task →","primary");submit.type="submit";form.append(submit);
  form.addEventListener("submit",async event=>{event.preventDefault();const answers={};for(const field of task.memory.missing){const value=Number(form.elements.namedItem(field).value);answers[field]=field==='budget_cents'?Math.round(value*100):value;}submit.disabled=true;try{await api(`/api/tasks/${task.id}/clarify`,answers);lastTask="";await refresh();}catch(error){$("error").textContent=error.message;submit.disabled=false;}});
  panel.append(form);
}
$("goal-form").addEventListener("submit",async event=>{event.preventDefault();$("error").textContent="";$("launch").disabled=true;try{const result=await api("/api/tasks",{goal:$("goal").value,scenario:$("scenario").value});selected=result.id;lastTask="";await refresh();}catch(error){$("error").textContent=error.message;}finally{$("launch").disabled=false;}});
async function poll(){await refresh();setTimeout(poll,900);}poll();
