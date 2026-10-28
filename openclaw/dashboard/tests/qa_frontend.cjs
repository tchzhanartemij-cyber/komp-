// Synthetic regression harness: execute the actual inline handlers without a browser.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(__dirname + '/../index.html', 'utf8');
const between = (a,b) => html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)));
const nodes = new Map();
function node() { return {dataset:{}, elements:[], hidden:false, children:[], classList:{toggle(){}}, append(x){this.children.push(x)},setAttribute(){},focus(){document.activeElement=this},scrollIntoView(){this.scrolled=true}, contains(x){return this===x},parentElement:{classList:{toggle(k,v){nodes.set(k,v)}}}}; }
const $ = id => nodes.get(id) || (nodes.set(id,node()),nodes.get(id));
const listeners={}; const document={activeElement:null,addEventListener:(k,f)=>listeners[k]=f,querySelector:()=>null};
let mobile=false, resized=0, redrawn=0;
const ctx=vm.createContext({console,assert,$,document,matchMedia:()=>({matches:mobile}),setTimeout,clearTimeout,AbortController,Date,JSON,Promise,Map,
 el:(tag,p)=>({...node(),...p}),setStatus:(t,e)=>{$('status').textContent=t;$('status').bad=!!e},
 render(){},resizeCanvas(){resized++},redraw(){redrawn++},current:'graph',selected:'',G:{ents:new Map(),nodes:new Map(),history:[],hidden:new Set()},setView(){},renderKinds(){},entKey:s=>s.toLowerCase()});
vm.runInContext('let data={},lastJson="";'+between('let mutationError =','// ----- pages:'),ctx);
const run = s=>vm.runInContext(s,ctx);
let requests=[], pending=[];
ctx.fetch=(url,opts)=>{requests.push({url,opts});return new Promise((resolve,reject)=>pending.push({resolve,reject,opts}));};
const reply=(ok=true,body='{}')=>({ok,text:async()=>body,json:async()=>JSON.parse(body)});
const flush=()=>new Promise(r=>setImmediate(r));
async function main(){
 // R2: rejection persists across successful sync; delete/toggle callers get a result/retry.
 let p=run("post('/api/tasks/toggle',{id:1})");pending.shift().resolve(reply(false,'{"error":"Rejected"}'));
 assert.equal((await p).ok,false);assert.equal($('status').bad,true);
 p=run('load()');pending.shift().resolve(reply());await p;assert.equal($('status').textContent,'Rejected');assert.equal(run('typeof mutationRetry'),'function');
 // R6: coalesce polling and force a fresh response after a mutation invalidates a pending read.
 run('dataNext=0');let first=run('load()');assert.strictEqual(run('load()'),first);
 let second=run('load(true)');assert.strictEqual(first,second);assert.equal(pending.length,1);
 pending.shift().resolve(reply(true,'{"old":true}'));await flush();assert.equal(run('data.old'),undefined);assert.equal(pending.length,1);
 pending.shift().resolve(reply(true,'{"latest":true}'));await first;assert.equal(run('data.latest'),true);
 p=run('load()');pending.shift().reject(Error('offline'));await p;let count=requests.length;await run('load()');assert.equal(requests.length,count);assert(run('dataNext>Date.now()'));
 // Abort on timeout, with no unhandled rejection. Use a virtual timer in this context.
 let timeout;ctx.setTimeout=fn=>(timeout=fn,1);ctx.clearTimeout=()=>{};run('dataNext=0');p=run('load()');let req=pending.shift();req.opts.signal.addEventListener('abort',()=>req.reject(Error('aborted')));timeout();await p;assert(req.opts.signal.aborted);
 // R3/R4: actual selection and close handlers; no mutation path in close.
 vm.runInContext(between('let panelOpen =','function renderKinds()'),ctx);
 ctx.renderPanel=()=>{$('g-panel').hidden=!run('panelOpen');$('g-panel').parentElement.classList.toggle('panel-closed',!run('panelOpen'));};
 const entity={key:'qa',name:'QA',kind:'person'};ctx.G.ents.set('qa',entity);
 mobile=true;run("selectNode('qa',false)");assert.equal($('g-panel').scrolled,true);assert.strictEqual(document.activeElement,$('g-panel'));
 count=requests.length;listeners.keydown({key:'Escape',preventDefault(){}});assert.equal($('g-panel').hidden,true);assert.equal(nodes.get('panel-closed'),true);assert.equal(requests.length,count);assert(resized>0);assert($('g-box').scrolled);
 mobile=false;$('g-panel').scrolled=false;run("selectNode('qa',false)");assert.equal($('g-panel').hidden,false);assert.equal($('g-panel').scrolled,false);
 // R5: map persists all values even after blur; caret is restored only for an actual focused field.
 const field=(name,value,type='text')=>({name,value,type,focus(){this.focused=true},setSelectionRange(a,b){this.caret=[a,b]}});
 function form(type,values,key){const f=node();f.dataset={form:type,...(key?{draftKey:key}:{})};f.id=key?'f-fact':'form';f.elements=Object.entries(values).map(([k,v])=>field(k,v));f.elements.push({type:'submit',disabled:false});f.elements.namedItem=n=>f.elements.find(x=>x.name===n);f.reset=()=>f.elements.forEach(x=>{if(x.name)x.value=''});f.closest=()=>({close(){f.closed=true}});f.requestSubmit=()=>listeners.submit({target:f,preventDefault(){}});return f;}
 let f=form('memory',{name:'QA',text:'blur draft',kind:'person'},'qa');nodes.set('f-fact',f);document.activeElement=$('g-search');run('captureFactDraft()');assert.equal(run("factDrafts.get('qa')[1][1]"),'blur draft');
 f=form('memory',{name:'New',text:'new draft',kind:'topic'},'__new__');nodes.set('f-fact',f);run('captureFactDraft()');assert.equal(run("factDrafts.get('qa')[1][1]"),'blur draft');assert.equal(run("factDrafts.get('__new__')[2][1]"),'topic');
 vm.runInContext(between('function restoreTyping(', 'function renderGraph()'),ctx);
 run("restoreTyping(factDrafts.get('__new__'),'',null)");assert(!f.elements.namedItem('text').focused);
 run("restoreTyping(factDrafts.get('__new__'),'text',[2,4])");assert.deepEqual(f.elements.namedItem('text').caret,[2,4]);
 // Execute renderPanel itself through repeated blur refreshes and entity switches.
 const simplePanel = ctx.renderPanel;
 $('g-panel').replaceChildren=()=>{};
 ctx.kindBadge=()=>node(); ctx.rich=()=>node(); ctx.short=x=>x;
 ctx.factForm=e=>{const next=form('memory',{name:e?e.name:'',text:'',kind:''},e?e.key:'__new__');nodes.set('f-fact',next);return next;};
 ctx.G.history=[];
 Object.assign(entity,{page:null,backlinks:[],links:new Map(),mentions:[]});
 vm.runInContext(between('function renderPanel()', 'function restoreTyping('),ctx);
 run("selected='qa';panelOpen=true");
 f=form('memory',{name:'QA',text:'persistent',kind:'person'},'qa');nodes.set('f-fact',f);document.activeElement=$('g-search');
 for(let i=0;i<12;i++){run('renderPanel()');assert.equal($('f-fact').elements.namedItem('text').value,'persistent');assert(!$('f-fact').elements.namedItem('text').focused);}
 run("selected='';renderPanel()");assert.equal($('f-fact').elements.namedItem('text').value,'new draft');
 run("selected='qa';renderPanel()");assert.equal($('f-fact').elements.namedItem('text').value,'persistent');
 ctx.renderPanel=simplePanel;
 // R1: every common form retains complete draft on rejection and suppresses duplicates.
 const handler=between("document.addEventListener('submit', async ev =>", "document.querySelectorAll('[data-toggle]')");vm.runInContext(handler,ctx);
 const cases={expense:{amount:'12',category:'food',note:'note'},tasks:{title:'task',due:'2026-11-01'},plans:{title:'plan',due:''},work:{title:'work',due:''},notes:{text:'note'},worknotes:{text:'work note'},food:{item:'meal',kcal:'120'},memory:{name:'QA',text:'fact',kind:'person'}};
 for(const [type,values] of Object.entries(cases)){
   f=form(type,values,type==='memory'?'qa':null);if(type==='memory')nodes.set('f-fact',f);
   const event={target:f,preventDefault(){}};let save=listeners.submit(event);assert(f.elements.at(-1).disabled);count=requests.length;await listeners.submit(event);assert.equal(requests.length,count);
   pending.shift().resolve(reply(false,'{"error":"save failed"}'));await save;assert(!f.closed);assert(!f.elements.at(-1).disabled);for(const [k,v] of Object.entries(values))assert.equal(f.elements.namedItem(k).value,v);
 }
 // Confirmed success clears unchanged values, but preserves edits made while pending.
 for(const edit of [false,true]){
   f=form('notes',{text:'submitted'});const save=listeners.submit({target:f,preventDefault(){}});if(edit)f.elements.namedItem('text').value='later edit';
   pending.shift().resolve(reply());await flush();pending.shift().resolve(reply());await save;
   assert.equal(f.elements.namedItem('text').value,edit?'later edit':'');assert.equal(!!f.closed,!edit);
 }
 // R5: saving an entity while switching away clears only its submitted draft.
 vm.runInContext(between('function renderPanel()', 'function restoreTyping('),ctx);
 run("selected='qa';panelOpen=true");
 f=form('memory',{name:'QA',text:'saved fact',kind:'person'},'qa');nodes.set('f-fact',f);
 let save=listeners.submit({target:f,preventDefault(){}});
 run("captureFactDraft();selected='';renderPanel()");
 $('f-fact').elements.namedItem('text').value='independent new page';
 pending.shift().resolve(reply());await flush();pending.shift().resolve(reply());await save;
 assert.equal(run("factDrafts.has('qa')"),false);
 assert.equal($('f-fact').elements.namedItem('text').value,'independent new page');
 // Reopen a pending entity and edit after submission: success must retain later text.
 run("selected='qa';renderPanel()");f=$('f-fact');f.elements.namedItem('text').value='submitted fact';
 save=listeners.submit({target:f,preventDefault(){}});f.elements.namedItem('text').value='later fact';
 pending.shift().resolve(reply());await flush();pending.shift().resolve(reply());await save;
 assert.equal($('f-fact').elements.namedItem('text').value,'later fact');
 console.log('R1–R6 synthetic frontend regressions passed');
}
main().catch(e=>{console.error(e);process.exitCode=1});
