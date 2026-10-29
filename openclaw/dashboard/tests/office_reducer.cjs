// Run the actual inline office core and UI with a minimal DOM, no browser or network.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/../index.html','utf8');
const section=(a,b)=>{const start=html.indexOf(a),end=html.indexOf(b,start);assert(start>=0&&end>start);return html.slice(start+a.length,end);};
const ctx=vm.createContext({console});
vm.runInContext(section('// OFFICE_CORE_BEGIN','// OFFICE_CORE_END').replace(/^.*\n/,''),ctx);
const run=s=>vm.runInContext(s,ctx);
const api=run('({officeReduce,officeInitial,officePath,officeStep,officeTargets,OFFICE_MAP})');
const {officeReduce:reduce,officeInitial:initial,officePath:path,officeStep:step,officeTargets:targets,OFFICE_MAP:map}=api;
const row=(status='running',extra={})=>({key:'coder:8',kind:'coder',label:'Кодер #8',title:'Задача',provider:'Claude Code',model:null,status,activity:'code',activity_at:1000,started:990,updated:1000,parent:null,...extra});
const snap=(time,...sessions)=>({server_time:time,sessions,agent:{available:true,reason:'test'},queue:{available:true,reason:'test'}});
const json=x=>JSON.parse(JSON.stringify(x));
function frozen(obj){Object.freeze(obj);for(const v of Object.values(obj))if(v&&typeof v==='object'&&!Object.isFrozen(v))frozen(v);return obj;}
let s=initial();
s=reduce(frozen(s),frozen(snap(1000,row('queued'))));assert.equal(s.chars['coder:8'].place,'queue');assert(!s.chars['coder:8'].working);
let desk;
for(const [i,status,activity,place,text] of [[1,'running','code','desk','пишет код'],[2,'running','search_code','desk','ищет в коде'],[3,'question',null,'desk','ждёт решения'],[4,'running','thinking','desk','думает'],[5,'done',null,'lounge','отдыхает']]){
 const event=snap(1000+i,row(status,{activity,updated:1000+i}));s=reduce(frozen(s),frozen(event));
 const c=s.chars['coder:8'];assert.equal(c.place,place);assert.equal(c.text,text);if(desk===undefined)desk=c.desk;assert.equal(s.slots['coder:8'],desk);assert.equal(c.desk,place==='desk'?desk:null);
 assert.strictEqual(reduce(s,event),s,'duplicate snapshots are idempotent');
}
assert.equal(s.chars['coder:8'].badge,'✓');
assert.strictEqual(reduce(s,snap(999,row('running'))),s,'ignore older snapshots');
{let r=reduce(initial(),snap(2000,row('running',{updated:2045,activity_at:2045})));r=reduce(r,snap(2060,row('done',{updated:2040,activity:null})));
 assert.equal(r.chars['coder:8'].place,'lounge','minute-precision terminal status after fresher log mtime is applied');
 r=reduce(r,snap(2120,row('queued',{updated:2040,activity:null})));assert.equal(r.chars['coder:8'].place,'queue','redo/answer re-queues a finished task');}
let active=reduce(initial(),snap(1000,row('running'),row('running',{key:'agent:12',kind:'agent',activity:'answering'})));
assert.equal(new Set(Object.values(active.chars).map(c=>c.desk)).size,2);
for(const [kind,age,stale] of [['coder',120,false],['coder',121,true],['codex',121,true],['agent',90,false],['agent',91,true],['chat',91,true]]){
 const c=reduce(initial(),snap(1000+age,row('running',{kind}))).chars['coder:8'];
 assert.equal(c.stale,stale);assert.equal(c.working,!stale);assert.equal(c.place,'desk');if(stale)assert.equal(c.text,'нет свежих данных');
}
for(const activity of [null,undefined,'invented']){const c=reduce(initial(),snap(1001,row('running',{activity}))).chars['coder:8'];assert.equal(c.text,'нет данных о действии');assert(!c.working);assert.equal(c.place,'desk');}
let decision=reduce(initial(),snap(1200,row('question',{updated:1200})));
decision=reduce(decision,snap(1300,row('running',{updated:1300,activity_at:1000})));
assert.equal(decision.chars['coder:8'].source.status,'running');assert.equal(decision.chars['coder:8'].text,'нет свежих данных');assert(!decision.chars['coder:8'].working);
assert.equal(reduce(initial(),snap(1001,row('question',{kind:'agent'}))).chars['coder:8'].text,'неизвестно','only coder question waits for a decision');
for(const [status,place,text,badge] of [['waiting_limit','desk','ждёт лимит',''],['cancelled','entrance','отменён',''],['failed','desk','ошибка','✗'],['interrupted','desk','прерван','✗'],['incomplete','desk','ответ неполный','✗'],['timeout','desk','время вышло','✗'],['strange','entrance','неизвестно',''],['unknown','entrance','неизвестно','']]){
 const c=reduce(initial(),snap(1000,row(status))).chars['coder:8'];assert.equal(c.place,place);assert.equal(c.text,text);assert.equal(c.badge,badge);assert(!c.working);
}
for(const status of ['completed','done','nochange','merged'])assert.equal(reduce(initial(),snap(1000,row(status))).chars['coder:8'].place,'lounge');
const before=json(active),offline=reduce(frozen(active),{type:'offline',at:1020});
assert.deepEqual(json(active),before);assert(!offline.online);
for(const [key,c] of Object.entries(offline.chars)){assert.equal(c.text,'неизвестно');assert(!c.working);assert.equal(c.place,active.chars[key].place);assert.equal(c.desk,active.chars[key].desk);}
const resumed=reduce(offline,snap(1000,row('running'),row('running',{key:'agent:12',kind:'agent',activity:'answering'})));
assert(resumed.online);assert.deepEqual(json(resumed),before);assert.equal(Object.keys(resumed.chars).length,2);
const oldOffline=reduce(offline,snap(999,row('done')));assert.strictEqual(oldOffline,offline);
const doneOffline=reduce(s,{type:'offline',at:1021});assert.equal(doneOffline.chars['coder:8'].text,'неизвестно');assert(!doneOffline.chars['coder:8'].working);
let missing=reduce(active,snap(1001));assert.equal(missing.chars['coder:8'].text,'неизвестно');assert.equal(missing.chars['coder:8'].place,'entrance');assert.equal(missing.chars['coder:8'].missing,1);
assert.strictEqual(reduce(missing,snap(1001)),missing);
let retryMissing=reduce(reduce(missing,{type:'offline',at:1002}),snap(1001));assert.deepEqual(json(retryMissing),json(missing));
missing=reduce(missing,snap(1002));assert.equal(Object.keys(missing.chars).length,0);
const returned=reduce(missing,snap(1003,row('running',{updated:1003})));
assert.equal(returned.chars['coder:8'].source.status,'running');assert.equal(returned.chars['coder:8'].desk,active.chars['coder:8'].desk);
let terminal=reduce(initial(),snap(100000,...Array.from({length:9},(_,i)=>row('done',{key:'coder:'+i,updated:100000-i})),row('done',{key:'coder:old',updated:1})));
assert.equal(Object.keys(terminal.chars).length,6);assert(!terminal.chars['coder:old']);assert(terminal.chars['coder:0']);
terminal=reduce(terminal,snap(100010));assert.equal(Object.keys(terminal.chars).length,6);
terminal=reduce(terminal,snap(200000));assert.equal(Object.keys(terminal.chars).length,0);
assert.equal(Object.keys(reduce(initial(),snap(1000)).chars).length,0);
const crowded=reduce(initial(),snap(1000,...Array.from({length:14},(_,i)=>row('running',{key:'coder:'+i}))));
assert.equal(Object.values(crowded.chars).filter(c=>c.place==='desk').length,9);assert.equal(Object.values(crowded.chars).filter(c=>c.place==='aisle').length,5);
assert.equal(new Set(Object.values(crowded.chars).filter(c=>c.desk!==null).map(c=>c.desk)).size,9);
const resting=reduce(initial(),snap(1000,...Array.from({length:6},(_,i)=>row('running',{key:'agent:'+i,kind:'agent'}))));
const shared=reduce(resting,snap(1001,...Array.from({length:6},(_,i)=>row('done',{key:'agent:'+i,kind:'agent',updated:1001})),...Array.from({length:9},(_,i)=>row('running',{key:'coder:'+i,updated:1001}))));
assert.equal(Object.values(shared.chars).filter(c=>c.place==='desk').length,9,'resting workers release their desks');
for(const state of [crowded,s,active])for(const p of Object.values(targets(state.chars)))assert(!map.blocked[p.y][p.x]);
// Every chair approach connects to every lounge seat and entrance via cardinal aisles.
for(const d of map.desks)for(const to of [...map.lounge,{x:12,y:14}]){
 const route=path(map,d.seat,to);assert(route.length>1);
 assert.deepEqual(json(route[0]),json(d.seat));assert.deepEqual(json(route.at(-1)),json(to));
 for(let i=0;i<route.length;i++){const p=route[i];assert(!map.blocked[p.y][p.x]);if(i)assert.equal(Math.abs(p.x-route[i-1].x)+Math.abs(p.y-route[i-1].y),1);}
 let a=step(null,d.seat,0,false);const paused=a;
 assert.strictEqual(step(a,to,1,false,false),paused,'offline cannot start a lounge transition');
 a=step(a,to,.01,true);assert.equal(a.x,to.x);assert.equal(a.y,to.y);assert.equal(a.path.length,0,'reduced motion is immediate');
 a=step(null,d.seat,0,false);
 for(let i=0;i<1000;i++){a=step(a,to,.05,false);assert(!map.blocked[Math.round(a.y)][Math.round(a.x)]);if(!a.path.length)break;}
 assert.equal(a.x,to.x);assert.equal(a.y,to.y);assert.equal(a.path.length,0);
}
assert.equal(path(map,{x:2,y:2},{x:12,y:14}).length,0);
// Replanning halfway through a tile keeps the route on an aisle.
let actor=step(step(null,map.desks[0].seat,0,false),map.lounge[0],.05,false);
for(let i=0;i<500;i++){actor=step(actor,{x:12,y:14},.05,false);assert(!map.blocked[Math.round(actor.y)][Math.round(actor.x)]);}
assert.equal(actor.x,12);assert.equal(actor.y,14);

// Execute the UI and actual polling handler with fakes; no canvas renderer dependency.
const listeners={},frames=new Map(),nodes=new Map();let frameId=0,requests=[];
const drawing=new Proxy({measureText:t=>({width:String(t).length*6})},{get:(o,k)=>o[k] || (()=>{})});
function node(){return {hidden:false,textContent:'',children:[],isConnected:true,attrs:{},classList:{remove(){},add(){}},getContext:()=>drawing,getBoundingClientRect:()=>({left:0,top:0,width:600,height:450}),append(...v){this.children.push(...v)},replaceChildren(...v){this.children=v},setAttribute(k,v){this.attrs[k]=v},addEventListener(k,f){(this.events ||= {})[k]=f},remove(){this.isConnected=false},focus(){document.activeElement=this},setPointerCapture(){},click(){this.onclick?.()}};}
const $=id=>nodes.get(id)||(nodes.set(id,node()),nodes.get(id));
const document={hidden:false,activeElement:null,createElement:node,addEventListener(k,f){listeners[k]=f}};
const RM={matches:false,addEventListener(k,f){this.change=f}};
Object.assign(ctx,{$,document,RM,window:{devicePixelRatio:2},current:'agents',el:(tag,props={},...kids)=>{const n=node();Object.assign(n,props);n.append(...kids);return n},requestAnimationFrame:f=>{frames.set(++frameId,f);return frameId},cancelAnimationFrame:id=>frames.delete(id),addEventListener(){},ResizeObserver:class{observe(){}},officeTime:t=>String(t),agentReason:node(),agentSend:node(),renderAgentRuns(){},Date,setTimeout:()=>0,clearTimeout(){}});
vm.runInContext(section('// OFFICE_UI_BEGIN','// OFFICE_UI_END'),ctx);
$('office-drawer').hidden=true;
run('officeVisibility()');assert.equal(frames.size,1);
function frame(t){const next=frames.entries().next().value;if(next){frames.delete(next[0]);next[1](t);}}
ctx.snapshot=snap(1000,row());run('officeAccept(snapshot)');frame(100);assert(frames.size>0,'working frame animates');
ctx.current='ai';run('officeVisibility()');assert.equal(frames.size,0,'no office frame on another page');
ctx.current='agents';document.hidden=true;run('officeVisibility()');assert.equal(frames.size,0,'no background office frames');
document.hidden=false;run('officeVisibility()');frame(200);
run("officeAccept({type:'offline',at:1001})");frame(300);assert.equal(frames.size,0,'offline freezes all animation');assert($('office-notice').textContent.startsWith('Нет связи с сервером · данные от'));
RM.matches=true;run('officeAccept(snapshot)');frame(400);assert.equal(frames.size,0,'reduced motion does not animate');
const focus=node();focus.focus();run("officeSelect('coder:8')");assert.strictEqual(document.activeElement,$('office-close'));assert(!$('office-drawer').hidden);
listeners.keydown({key:'Escape',preventDefault(){}});assert($('office-drawer').hidden);assert.strictEqual(document.activeElement,focus);
{const pend=run('officeView')(row('running',{tool_pending:true,updated:1000,activity_at:1000,activity:'command'}),1600,0);assert.equal(pend.stale,false);assert.equal(pend.text,'запускает команду · ждёт результата');
 assert.equal(run('officeView')(row('running',{updated:1000,activity_at:1000}),1600,0).stale,true,'no pending tool -> stale after 120 s');
 assert.equal(run('officeView')(row('running',{tool_pending:true,updated:1000,activity_at:1000}),1000+7201,0).stale,true,'pending tool beyond coder hard limit is stale');}
const refresh=html.slice(html.indexOf('async function refreshAgent(){'),html.indexOf('\nshowPage();refreshAgent();'));
vm.runInContext('let agentRefreshing=false;'+refresh,ctx);
vm.runInContext(html.match(/async function aiGet\(url\) [^\n]+/)[0],ctx);
ctx.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:url!=='/api/office',json:async()=>url==='/api/office'?{error:'HTTP 503'}:{reason:'test',available:true,runs:[]}}};
async function integration(){
 await run('refreshAgent()');assert.equal(run('officeState.online'),false);assert.equal(run("officeState.chars['coder:8'].text"),'неизвестно');
 ctx.fetch=async(url,opts)=>{requests.push({url,opts});throw Error('connection lost')};
 await run('refreshAgent()');assert.equal(run('officeState.online'),false);
 ctx.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>url==='/api/office'?ctx.snapshot:{reason:'test',available:true,runs:[]}}};
 await run('refreshAgent()');assert.equal(run('officeState.online'),true);assert.equal(run('Object.keys(officeState.chars).length'),1);assert.equal(run("officeState.chars['coder:8'].desk"),0);
 assert(requests.every(({url,opts})=>(url==='/api/office'||url==='/api/agent')&&!opts.method),'opening the office only uses GET');
 document.hidden=true;const n=requests.length;await run('refreshAgent()');assert.equal(requests.length,n);
 ctx.current='life';document.hidden=false;await run('refreshAgent()');assert.equal(requests.length,n);
 console.log('Office reducer, paths, reduced motion, UI lifecycle and polling regressions passed');
}
integration().catch(e=>{console.error(e);process.exitCode=1});
