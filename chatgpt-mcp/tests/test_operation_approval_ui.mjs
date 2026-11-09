import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const file = new URL("../operation-approval.html", import.meta.url);
const html = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
const source = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
const OP="1".repeat(32), CARD="2".repeat(32), TOKEN="3".repeat(64);
const XSS='<img src=x onerror="globalThis.pwned=1">';
class Element {
  constructor(id) { this.id=id; this.children=[]; this.listeners={}; this.dataset={};
    this.style={}; this.disabled=false; this.hidden=false; }
  get textContent() { return this.children.join(""); }
  set textContent(value) { this.children=[String(value)]; }
  get innerHTML() { throw Error("innerHTML forbidden"); }
  set innerHTML(_) { throw Error("innerHTML forbidden"); }
  addEventListener(type,fn) { (this.listeners[type] ||= []).push(fn); }
  async dispatch(type) { await Promise.all((this.listeners[type]||[]).map(fn=>fn({type}))); }
  getBoundingClientRect() { return {height:520}; }
}
function fixture(extra={}) {
 return {component:"operation",family:"registered",project:"repo",target:"test-target",
  operation_id:OP,card_id:CARD,state:"pending",plan_digest:"4".repeat(64),
  source_digest:"5".repeat(64),expires_at:"2099-10-16T05:30:00+00:00",
  executes_on_approval:false,supports_direct_execution:true,approval_required:true,
  receipt_verified:false,authorization_verified:false,
  plan:{project:"repo",target:"test-target",operation_id:OP,action:"compose.restart",
    parameters:{services:["api"]},impact:{description:XSS}},...extra};
}
const approved=()=>fixture({state:"approved",approved:true,approval_required:false,
  receipt_verified:true,authorization_verified:true,supports_direct_execution:false,
  next_action:{tool:"execute_host_operation",arguments:{project:"repo",target:"test-target",
    operation_id:OP}}});
const executing=()=>({...approved(),execution_requested:true,execution_started:true,
  plan:{state:"queued",phase:"QUEUED",started_at:"2026-10-28T04:00:00Z"},
  next_action:{tool:"read_host_operation_events",arguments:{project:"repo",
    target:"test-target",operation_id:OP}}});
const result=(value,token)=>({structuredContent:value,
  ...(token?{_meta:{operation_confirmation:token}}:{})});
async function load({output=fixture(),token=TOKEN,callTool,metadata={}}={}) {
 assert.ok(source, "An executable inline approval UI is required");
 const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)]
  .map(m=>[m[1],new Element(m[1])]));
 const root=new Element("root"), body=new Element("body"), posted=[],calls=[],states=[];
 const listeners={}, parent={postMessage:v=>posted.push(v)};
 const openai={toolOutput:output,toolResponseMetadata:{...metadata,
   ...(token?{operation_confirmation:token}:{})},
   callTool:async(name,args)=>{calls.push({name,args:structuredClone(args)});
     return callTool?callTool(name,args):result(approved());},
   setWidgetState:v=>states.push(structuredClone(v))};
 const context={document:{documentElement:root,body,getElementById:id=>elements.get(id)},
   parent,openai,Date,Map,Object,console,structuredClone,innerWidth:390,
   setTimeout:(fn,delay)=>{if(delay<=15000)fn();return 1},clearTimeout(){},
   requestAnimationFrame:fn=>{fn();return null},cancelAnimationFrame(){},
   ResizeObserver:class{observe(){}disconnect(){}},
   addEventListener:(type,fn)=>{(listeners[type]||=[]).push(fn)},removeEventListener(){}};
 context.window=context;vm.createContext(context);vm.runInContext(source,context);
 for(let i=0;i<6;i++)await Promise.resolve();
 return {elements,posted,calls,states,context,listeners,parent};
}
test("shows the exact plan safely and keeps hidden token out of visible output",async()=>{
 const app=await load();
 assert.equal(app.elements.get("project").textContent,"repo");
 assert.equal(app.elements.get("operation").textContent,OP);
 assert.match(app.elements.get("details").textContent,/compose.restart/);
 assert.match(app.elements.get("details").textContent,/<img src=x/);
 assert.equal(app.context.pwned,undefined);
 assert.equal(app.elements.get("approve").disabled,false);
 assert.equal(app.elements.get("execute").disabled,false);
 assert.equal(app.elements.get("cancel").disabled,false);
 assert.equal([...app.elements.values()].some(e=>e.textContent.includes(TOKEN)),false);
 assert.equal(app.context.openai.toolResponseMetadata,undefined);
});
test("approval-only sends the exact app callback and publishes no capability",async()=>{
 const app=await load();await app.elements.get("approve").dispatch("click");
 assert.deepEqual(app.calls,[{name:"confirm_operation_approval",args:{
  project:"repo",family:"registered",target:"test-target",operation_id:OP,
  card_id:CARD,confirmation_token:TOKEN}}]);
 assert.equal(app.elements.get("approve").disabled,true);
 assert.match(app.elements.get("status").textContent,/승인/);
 assert.equal(JSON.stringify(app.states).includes(TOKEN),false);
 assert.equal(JSON.stringify(app.states).includes("next_action"),false);
});
test("approve-and-execute is one callback and a double click never submits twice",async()=>{
 let release;const wait=new Promise(resolve=>{release=resolve});
 const app=await load({callTool:()=>wait});
 const a=app.elements.get("execute").dispatch("click");
 const b=app.elements.get("execute").dispatch("click");
 assert.equal(app.calls.length,1);
 assert.equal(app.calls[0].name,"confirm_and_execute_operation");
 release(result(executing()));await Promise.all([a,b]);
 assert.match(app.elements.get("status").textContent,/실행/);
 assert.equal(app.elements.get("execute").disabled,true);
 assert.equal(app.calls.some(x=>x.name==="execute_host_operation"),false);
 assert.equal(JSON.stringify(app.states).includes(TOKEN),false);
});
test("cancel closes the pending plan and disables every further action",async()=>{
 const app=await load({callTool:()=>result(fixture({state:"cancelled",
  approval_required:false,approved:false,supports_direct_execution:false}))});
 await app.elements.get("cancel").dispatch("click");
 assert.equal(app.calls[0].name,"cancel_operation_approval");
 assert.match(app.elements.get("status").textContent,/취소/);
 for(const key of ["approve","execute","cancel"])assert.equal(app.elements.get(key).disabled,true);
});
test("loss or cross-plan reply locks buttons and never retries",async()=>{
 for(const callTool of [()=>{throw Error("lost")},()=>result({...approved(),operation_id:"8".repeat(32)})]){
  const app=await load({callTool});await app.elements.get("execute").dispatch("click");
  await app.elements.get("execute").dispatch("click");
  assert.equal(app.calls.length,1);
  assert.match(app.elements.get("status").textContent,/확정할 수 없/);
  for(const key of ["approve","execute","cancel"])assert.equal(app.elements.get(key).disabled,true);
 }
});
test("active grants and terminal prepare outputs never request a new approval",async()=>{
 for(const output of [
  {project:"repo",task_id:OP,state:"prepared",approval_required:false,
   authorization:{kind:"host_access"},model:"synthetic"},
  {project:"repo",operation_id:OP,state:"failed",code:"ALREADY_FINISHED"}
 ]){
  const app=await load({output,token:null});
  assert.equal(app.calls.length,0);
  for(const key of ["approve","execute","cancel"])assert.equal(app.elements.get(key).disabled,true);
  assert.doesNotMatch(app.elements.get("status").textContent,/다시.*승인/);
  assert.match(app.elements.get("details").textContent,new RegExp(output.state));
 }
});
test("agent objective stays in private review and callback omits target",async()=>{
 const output=fixture({family:"agent",project:"repo",target:null,
  plan:{project:"repo",task_id:OP,profile_id:"codex-test",model:"synthetic",
   objective_sha256:"6".repeat(64),allowed_paths:["src"]}});
 const response=approved();response.family="agent";response.target=null;
 response.plan={task_id:OP,project:"repo"};
 response.next_action={tool:"start_agent_task",arguments:{project:"repo",task_id:OP}};
 const app=await load({output,metadata:{operation_review:{objective:"Private objective "+XSS,
   apply_policy:"review_only",timeout_minutes:30}},callTool:()=>result(response)});
 assert.match(app.elements.get("review").textContent,/Private objective/);
 await app.elements.get("approve").dispatch("click");
 assert.equal(Object.hasOwn(app.calls[0].args,"target"),false);
 assert.equal(app.calls[0].args.family,"agent");
 assert.equal(JSON.stringify(app.states).includes("Private objective"),false);
});
test("wrong or missing confirmation metadata is closed",async()=>{
 for(const token of [null,"wrong"]){
  const app=await load({token});
  for(const key of ["approve","execute","cancel"])assert.equal(app.elements.get(key).disabled,true);
  await app.elements.get("execute").dispatch("click");assert.equal(app.calls.length,0);
 }
});

test("a still-pending callback reply is ambiguous and cannot enable a retry",async()=>{
 const app=await load({callTool:()=>result(fixture(),TOKEN)});
 await app.elements.get("approve").dispatch("click");
 await app.elements.get("approve").dispatch("click");
 assert.equal(app.calls.length,1);
 assert.match(app.elements.get("status").textContent,/확정할 수 없/);
 for(const key of ["approve","execute","cancel"])assert.equal(app.elements.get(key).disabled,true);
});
