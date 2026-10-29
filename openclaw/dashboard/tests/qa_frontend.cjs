// Synthetic regression harness: execute the actual inline handlers without a browser.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(__dirname + '/../index.html', 'utf8');
const between = (a,b) => html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)));
const nodes = new Map();
function node() { return {dataset:{}, elements:[], hidden:false, children:[], classList:{toggle(){}}, append(x){this.children.push(x)},replaceChildren(...xs){this.children=xs},setAttribute(k,v){this[k]=v},focus(){document.activeElement=this},scrollIntoView(){this.scrolled=true}, contains(x){return this===x},parentElement:{classList:{toggle(k,v){nodes.set(k,v)}}}}; }
const $ = id => nodes.get(id) || (nodes.set(id,node()),nodes.get(id));
const listeners={}; const document={activeElement:null,addEventListener:(k,f)=>listeners[k]=f,querySelector:()=>null};
const storage=new Map();
let mobile=false, resized=0, redrawn=0;
const ctx=vm.createContext({console,assert,$,document,crypto:require('crypto').webcrypto,btoa:s=>Buffer.from(s,'binary').toString('base64'),localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},matchMedia:()=>({matches:mobile}),setTimeout,clearTimeout,AbortController,Date,JSON,Promise,Map,
 el:(tag,p)=>({...node(),...p}),setStatus:(t,e)=>{$('status').textContent=t;$('status').bad=!!e},
 applyPendingMemory(){},render(){},resizeCanvas(){resized++},redraw(){redrawn++},current:'graph',selected:'',G:{ents:new Map(),nodes:new Map(),history:[],hidden:new Set()},setView(){},renderKinds(){},entKey:s=>s.toLowerCase()});
vm.runInContext('let data={},lastJson="";'+between('let mutationError =','// ----- pages:'),ctx);
const run = s=>vm.runInContext(s,ctx);
let requests=[], pending=[];
ctx.fetch=(url,opts)=>{requests.push({url,opts});return new Promise((resolve,reject)=>pending.push({resolve,reject,opts}));};
const reply=(ok=true,body='{}')=>({ok,text:async()=>body,json:async()=>JSON.parse(body)});
const flush=()=>new Promise(r=>setImmediate(r));
async function main(){
 // R2: rejection persists across successful sync; delete/toggle callers get a result/retry.
 let p=run("post('/api/tasks/toggle',{id:1})");pending.shift().resolve(reply(false,'{"error":"Rejected"}'));
 assert.equal((await p).ok,false);assert.equal(run('mutationError'),'Rejected');
 p=run('load()');pending.shift().resolve(reply());await p;assert($('status').textContent.startsWith('Обновлено'));assert.equal(run('mutationError'),'Rejected');assert.equal(run('typeof mutationRetry'),'function');
 // Separate transport status, exact action identity, dismiss, and ambiguous outcomes.
 p=run("post('/api/tasks/toggle',{id:2})");pending.shift().resolve(reply());await p;
 assert.equal(run('mutationError'),'Rejected');pending.shift().reject(Error('offline'));await run('dataFlight');
 assert.equal($('status').textContent,'Нет связи с сервером');assert.equal(run('mutationError'),'Rejected');
 p=run("post('/api/tasks/toggle',{id:3})");pending.shift().reject(Error('connection lost'));assert((await p).ambiguous);
 assert.equal(run('mutationRetry'),null);assert(run('mutationError').includes('неизвестен'));
 assert.equal(run('mutationFailures.size'),2);
 const dismiss=$('mutation-status').children.at(-1).children.at(-1);assert.equal(dismiss.text,'Скрыть ошибку');dismiss.onclick();
 assert.equal(run('mutationError'),'Rejected');
 p=run("post('/api/tasks/toggle',{id:1})");pending.shift().resolve(reply());await p;
 assert.equal(run('mutationFailures.size'),0);pending.shift().resolve(reply());await run('dataFlight');
 for(const status of [400,401,409,500]) {
   p=run("post('/api/rejected',{id:1})");pending.shift().resolve({...reply(false),status});assert.equal((await p).ok,false);assert.equal(run('typeof mutationRetry'),'function');
 }
 run("mutationStatus('',null,failedMutationKey)");
 // A proxy gateway error cannot prove that an add/toggle was rejected.
 for (const status of [502,503,504]) {
   p=run("post('/api/tasks/toggle',{id:status})".replace('status',String(status)));
   pending.shift().resolve({ok:false,status});const result=await p;
   assert(result.ambiguous);assert.equal(run('mutationRetry'),null);
 }
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
 // Native Escape in dialog/search/select leaves memory open; graph Escape closes.
 document.querySelector=()=>({open:true});listeners.keydown({key:'Escape',preventDefault(){throw Error('native dialog escape prevented')}});assert(run('panelOpen'));
 document.querySelector=()=>null;document.activeElement={tagName:'SELECT'};
 listeners.keydown({key:'Escape',preventDefault(){throw Error('native select escape prevented')}});assert(run('panelOpen'));
 document.activeElement=$('g-search');listeners.keydown({key:'Escape',preventDefault(){throw Error('search escape prevented')}});assert(run('panelOpen'));
 document.activeElement=$('g-panel');listeners.keydown({key:'Escape',preventDefault(){}});assert.equal(storage.get('svodka-memory-closed'),'true');
 assert(html.includes('onclick="selectNode(\'\', false)">Новая страница'));
 run("selectNode('',false)");assert(run('panelOpen'));assert.equal(storage.get('svodka-memory-closed'),'false');
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
 ctx.factForm=e=>{const next=form('memory',{name:e?e.name:'',text:'',kind:''},e?e.key:'__new__');nodes.set('f-fact',next);ctx.renderFormFeedback(next);return next;};
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
 for(const [type,values] of Object.entries(cases)) for(const fault of [400,500,502,503,504,'network']){
   f=form(type,values,type==='memory'?'qa':null);if(type==='memory')nodes.set('f-fact',f);
   const event={target:f,preventDefault(){}};let save=listeners.submit(event);assert(f.elements.at(-1).disabled);count=requests.length;await listeners.submit(event);assert.equal(requests.length,count);
   const request=pending.shift();
   if(type==='memory'){const body=JSON.parse(request.opts.body);assert(/^[A-Za-z0-9_-]{22}$/.test(body.request));assert.equal(body.source,'dashboard');}
   if(fault==='network')request.reject(Error('lost'));else request.resolve({...reply(false,'{"error":"save failed"}'),status:fault});
   await save;assert(!f.closed);assert(!f.elements.at(-1).disabled);for(const [k,v] of Object.entries(values))assert.equal(f.elements.namedItem(k).value,v);
   const message=f._feedbackNode;
   assert(f.children.includes(message));assert.equal(message.role,'alert');assert.equal(message['aria-live'],'assertive');assert(!message.hidden);assert.equal(message.children.length,0);
   assert(message.textContent.includes(fault===400||fault===500?'кнопкой формы':'проверь результат'));
   assert.equal(run('mutationRetry'),null);
 }
 // Rebuilt memory DOM retains its own error, without exposing it on another draft.
 vm.runInContext(between('function renderPanel()', 'function restoreTyping('),ctx);
 run("selected='qa';panelOpen=true");
 for(let i=0;i<12;i++){run('renderPanel()');assert($('f-fact')._feedbackNode.textContent.includes('неизвестен'));}
 run("selected='';renderPanel()");assert($('f-fact')._feedbackNode.hidden);
 run("selected='qa';renderPanel()");assert.equal($('f-fact')._feedbackNode.role,'alert');
 ctx.renderPanel=simplePanel;
 // Static forms keep independent errors; success clears only the submitted form.
 const staticForms=[form('notes',{text:'first'}),form('notes',{text:'second'})];
 for(const target of staticForms){const save=listeners.submit({target,preventDefault(){}});pending.shift().resolve({...reply(false,'{"error":"own error"}'),status:400});await save;}
 const retry=listeners.submit({target:staticForms[0],preventDefault(){}});
 pending.shift().resolve(reply());await retry;
 assert(staticForms[0]._feedbackNode.hidden);assert.equal(staticForms[1]._feedbackNode.textContent,'own error · Исправь данные и повтори сохранение кнопкой формы.');
 pending.shift().resolve(reply());await run('dataFlight');
 // A POST resolves while an older GET remains hung; stale data is discarded.
 run('dataNext=0');let hung=run('load()');let hungRequest=pending.shift();
 f=form('notes',{text:'clear immediately'});let immediate=listeners.submit({target:f,preventDefault(){}});
 pending.shift().resolve(reply());await immediate;assert(f.closed);assert.equal(f.elements.namedItem('text').value,'');assert.strictEqual(run('dataFlight'),hung);
 hungRequest.resolve(reply(true,'{"staleAfterPost":true}'));await flush();assert.equal(run('data.staleAfterPost'),undefined);
 pending.shift().resolve(reply());await hung;
 // Confirmed success clears unchanged values, but preserves edits made while pending.
 for(const edit of [false,true]){
   f=form('notes',{text:'submitted'});const save=listeners.submit({target:f,preventDefault(){}});if(edit)f.elements.namedItem('text').value='later edit';
   pending.shift().resolve(reply());await save;assert.equal(pending.length,1);pending.shift().resolve(reply());await run('dataFlight');
   assert.equal(f.elements.namedItem('text').value,edit?'later edit':'');assert.equal(!!f.closed,!edit);assert.equal(f._feedbackNode.hidden,!edit);if(edit){assert.equal(f._feedbackNode.role,'status');assert.equal(f._feedbackNode.textContent,'Сохранено; новые изменения еще не отправлены');}
 }
 // Missing named controls after reset must not turn confirmed success into rejection.
 f=form('notes',{text:'submitted'});const reset=f.reset;f.reset=()=>{reset();f.elements.namedItem=()=>null};
 let missingSave=listeners.submit({target:f,preventDefault(){}});pending.shift().resolve(reply());await missingSave;assert(f.closed);
 pending.shift().resolve(reply());await run('dataFlight');
 f=form('memory',{name:'QA',text:'existing fact',kind:'person'},'qa');nodes.set('f-fact',f);
 const dedupSave=listeners.submit({target:f,preventDefault(){}});
 pending.shift().resolve(reply(true,'{"ok":true,"duplicate":true}'));await dedupSave;
 assert.equal(run("factFeedback.get('qa').text"),'Такой факт уже есть — не дублирую');
 pending.shift().resolve(reply());await run('dataFlight');
 // R5: saving an entity while switching away clears only its submitted draft.
 vm.runInContext(between('function renderPanel()', 'function restoreTyping('),ctx);
 run("selected='qa';panelOpen=true");
 f=form('memory',{name:'QA',text:'saved fact',kind:'person'},'qa');nodes.set('f-fact',f);
 let save=listeners.submit({target:f,preventDefault(){}});
 run("captureFactDraft();selected='';renderPanel()");
 $('f-fact').elements.namedItem('text').value='independent new page';
 pending.shift().resolve(reply());await save;assert.equal(pending.length,1);pending.shift().resolve(reply());await run('dataFlight');
 assert.equal(run("factDrafts.has('qa')"),false);assert.equal(run("factFeedback.has('qa')"),false);
 assert.equal($('f-fact').elements.namedItem('text').value,'independent new page');
 // Reopen a pending entity and edit after submission: success must retain later text.
 run("selected='qa';renderPanel()");f=$('f-fact');f.elements.namedItem('text').value='submitted fact';
 save=listeners.submit({target:f,preventDefault(){}});f.elements.namedItem('text').value='later fact';
 pending.shift().resolve(reply());await save;assert.equal(pending.length,1);pending.shift().resolve(reply());await run('dataFlight');
 assert.equal($('f-fact').elements.namedItem('text').value,'later fact');
 // New memory intent is fulfilled only when fresh graph data contains the page.
 run("selected='';panelOpen=true");f=form('memory',{name:'Created',text:'fact',kind:'topic'},'__new__');nodes.set('f-fact',f);
 save=listeners.submit({target:f,preventDefault(){}});pending.shift().resolve(reply());await save;
 assert.equal(run('pendingMemory'),'created');assert.equal($('f-fact').elements.namedItem('text').value,'');
 ctx.G.ents.set('created',{key:'created',name:'Created',backlinks:[],links:new Map(),mentions:[]});
 pending.shift().resolve(reply());await run('dataFlight');assert.equal(run('selected'),'created');assert(run('panelOpen'));
 run("selected='';renderPanel()");f=$('f-fact');f.elements.namedItem('name').value='Next';f.elements.namedItem('text').value='submitted';
 save=listeners.submit({target:f,preventDefault(){}});f.elements.namedItem('text').value='later new page edit';
 pending.shift().resolve(reply());await save;
 ctx.G.ents.set('next',{key:'next',name:'Next',backlinks:[],links:new Map(),mentions:[]});
 pending.shift().resolve(reply());await run('dataFlight');assert.equal(run('selected'),'next');
 run("selectNode('',false)");assert.equal($('f-fact').elements.namedItem('text').value,'later new page edit');
 // Revisiting the graph must neither reopen a closed panel nor scroll a selected page.
 ctx.readColors=()=>{};ctx.fit=()=>{};const resize=ctx.resizeCanvas;ctx.resizeCanvas=()=>true;
 vm.runInContext(between('let memoryStatusFlight =', '// Zoom/pan'),ctx);
 run('closeMemoryPanel()');$('g-panel').scrolled=false;run('graphShown()');assert.equal(run('panelOpen'),false);assert.equal($('g-panel').scrolled,false);
 ctx.resizeCanvas=resize;
 const statusRequest=pending.shift();assert.equal(requests.at(-1).url,'/api/memory/status');
 statusRequest.resolve(reply(true,JSON.stringify({store:{pages:2,facts:3},audit:{last:null},search:{mode:'keyword',reason:'diagnostic'},contract:[],tools:{probe:false}})));await flush();
 assert($('mem-status-body').children[0].text.includes('keyword'));assert($('mem-status-body').children[1].text.includes('фактов: 3'));
 const probe=run('loadMemoryStatus(true)');assert.equal(requests.at(-1).url,'/api/memory/status?probe=1');
 pending.shift().resolve(reply(true,JSON.stringify({store:{pages:2,facts:3},audit:{last:null},search:{mode:'keyword',reason:'diagnostic'},contract:[],tools:{probe:true,memory_get:{ok:true,error:null,ms:2},sessions_history:{ok:false,error:'недоступен',ms:3}}})));await probe;
 assert($('mem-tools').textContent.includes('memory_get: ok · 2 мс'));assert($('mem-tools').textContent.includes('sessions_history: ошибка'));
 // An invalidation queued at response completion must cause a new request.
 let completion=run('load()');const last=pending.shift();last.resolve(reply());
 completion.then(()=>run('load(true)'));await completion;await flush();assert.equal(pending.length,1);
 pending.shift().resolve(reply());await run('dataFlight');
 await flush();assert.equal(pending.length,0);assert.equal(run('dataFlight'),null);
 console.log('R1–R6 synthetic frontend regressions passed');
}
let completed=false;
process.on('beforeExit',()=>{if(!completed){console.error('Harness ended with unfinished async assertions');process.exitCode=1;}});
main().then(()=>{completed=true},e=>{completed=true;console.error(e);process.exitCode=1});

// Voice entry stays in the existing capsule; the panel never takes the modal top layer.
const nav = html.match(/<nav class="nav"[\s\S]*?<\/nav>/)[0];
assert(nav.includes('id="voice-btn"'));
assert(nav.indexOf('id="voice-btn"') < nav.indexOf('class="sep"'));
assert(/<aside id="voice" role="complementary"/.test(html));
assert(!/<dialog id="voice"/.test(html));
assert(/id="voice-status" role="status" aria-live="polite"/.test(html));
const mobileItem=html.match(/width: clamp\((\d+)px, (\d+)vw, 46px\)/);
const itemWidth=Math.max(+mobileItem[1],Math.min(320*(+mobileItem[2])/100,46));
assert(9*itemWidth+9*3.2+16+5 < 320-24);
for(const script of html.matchAll(/<script>([\s\S]*?)<\/script>/g))new vm.Script(script[1]);
assert(!/\?\?=|\|\|=|&&=|structuredClone|\.at\(|\(\?<([=!])/.test(html));

require('./workspace_ui.cjs');
