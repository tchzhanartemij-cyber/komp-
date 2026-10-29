// Execute the actual chat UI against a small DOM and deterministic network fixtures.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/../index.html','utf8');
class Node {
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.attributes={};this.dataset={};this.style={setProperty(){}};this.hidden=false;this.disabled=false;this.value='';this._text='';this.scrollTop=0;this.scrollHeight=200;this.clientHeight=300;this.className='';this.classList={toggle(){},contains(){return false},remove(){},add(){}};}
 set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 get childNodes(){return this.children;}append(...kids){for(const k of kids){const child=typeof k==='object'?k:Object.assign(new Node('#text'),{textContent:k});child.parentElement=this;this.children.push(child);}}
 replaceChildren(...kids){this.children=[];this._text='';this.append(...kids);}setAttribute(k,v){this.attributes[k]=String(v);if(k==='value')this.value=String(v);if(k==='id')nodes.set(v,this);}getAttribute(k){return this.attributes[k];}removeAttribute(k){delete this.attributes[k];}
 addEventListener(k,f){(this.listeners[k]||(this.listeners[k]=[])).push(f);}async fire(k,extra={}){for(const f of this.listeners[k]||[])await f({target:this,preventDefault(){},...extra});}
 async click(){if(!this.disabled){if(this.onclick)await this.onclick();await this.fire('click');}}focus(){}closest(){return null;}querySelectorAll(){return [];}remove(){this.parentElement.children=this.parentElement.children.filter(c=>c!==this);}select(){}
}
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
for(const id of html.matchAll(/id="([^"]+)"/g))$(id[1]);
const storage=new Map(),sessions=new Map(),requests=[],timers=new Map(),uploads=[],revoked=[];let tid=0;
const document={hidden:false,documentElement:new Node(),body:new Node(),createElement:t=>new Node(t),createTextNode:t=>Object.assign(new Node('#text'),{textContent:t}),addEventListener(){},querySelectorAll(){return []},execCommand(){}};
class XHR {
 constructor(){this.upload={};this.headers={};uploads.push(this);}open(method,url){this.method=method;this.url=url;}setRequestHeader(k,v){this.headers[k]=v;}send(file){this.file=file;}abort(){this.aborted=true;this.onabort?.();}finish(status,data){this.status=status;this.responseText=JSON.stringify(data);this.onload();}
}
const model={id:'claude:claude-opus-5-5',title:'Claude Opus 5.5',route:'claude',available:true,caps:{image:true,pdf:true,text:true},probe:{status:'unknown'}};
const free={id:'free',title:'Бесплатная',route:'free',available:true,caps:{image:false,pdf:true,text:true},probe:{status:'unknown'}};
let responseError=false,sendFailure=false,canonical=true,chat={id:'a'.repeat(12),title:'Saved',project:'',messages:[]};
function reply(data,ok=true){return {ok,json:async()=>data};}
async function fetch(url,opts={}){
 const body=opts.body?JSON.parse(opts.body):null;requests.push({url,body});
 if(url==='/api/ai/models')return reply({models:[model,free],auto:{...model,label:'Авто → Claude Opus 5.5'},claude:{windows:{}},free_models:[]});
 if(url==='/api/ai/chats')return reply({chats:[{...chat,messages:undefined}],canonical:{available:canonical,reason:canonical?'':'Отключено'}});
 if(url==='/api/ai/projects')return reply({projects:[]});
 if(url.startsWith('/api/ai/chat?'))return reply(chat);
 if(url==='/api/workspace')return reply({status:{available:canonical,reason:''},session:{key:'main'},tasks:[]});
 if(url.startsWith('/api/workspace/history'))return reply({messages:[{id:'old',role:'assistant',text:'**Telegram**',ts:123,model:'session-model'}],next_offset:30});
 if(url==='/api/workspace/send'){if(responseError)throw Error('offline');return reply({id:1});}
 if(url==='/api/ai/probe'){model.probe={status:'ok',actual_model:'reported-model',at:1700000000};return reply({ok:true});}
 if(url==='/api/ai/file/delete'||url==='/api/ai/stop')return reply({ok:true});
 if(url==='/api/ai/send'){
  const start={type:'start',run:'run',chat:chat.id,title:'Claude Opus 5.5',transport:'Claude Code CLI · подписка'};
  let events=[start,{type:'meta',actual_model:'reported-model'}];
  if(sendFailure)events.push({type:'error',error:'Claude: требуется вход'});else{chat={...chat,messages:[...chat.messages,{role:'user',text:body.text,attachments:body.attachments.map(id=>({id,name:'photo.png',kind:'image'}))},{role:'assistant',text:'answer',model:'Claude Opus 5.5',actual_model:'reported-model',transport:'Claude Code CLI · подписка',mode:'chat',time:'2026-10-29 12:34'}]};events.push({type:'delta',text:'answer'},{type:'done',chat});}
  let sent=false;return {ok:true,body:{getReader:()=>({read:async()=>{if(sent)return {done:true};sent=true;return {done:false,value:new TextEncoder().encode(events.map(e=>JSON.stringify(e)).join('\n')+'\n')};}})}};
 }
 throw Error('Unexpected fetch '+url);
}
const ctx=vm.createContext({console,$,document,window:{},URL:{createObjectURL:()=> 'blob:preview',revokeObjectURL:u=>revoked.push(u)},XMLHttpRequest:XHR,fetch,localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,String(v))},sessionStorage:{getItem:k=>sessions.get(k)||null,setItem:(k,v)=>sessions.set(k,v)},crypto:require('crypto').webcrypto,btoa:s=>Buffer.from(s,'binary').toString('base64'),navigator:{clipboard:{writeText:async()=>{}}},location:{hash:'#ai'},current:'ai',matchMedia:()=>({matches:true}),setInterval(){},setTimeout:(f,ms)=>{timers.set(++tid,{f,ms});return tid;},clearTimeout:id=>timers.delete(id),requestAnimationFrame:f=>{queueMicrotask(f);return 1;},addEventListener(){},AbortController,TextDecoder,Uint8Array,Date,JSON,Map,Set,Promise,badge(){},summary:{},renderBanner(){},confirm:()=>true});
const run=s=>vm.runInContext(s,ctx),slice=(a,b)=>html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)));
run(slice('function el(', 'function setStatus('));run(slice('const AI =','// ----- theme:'));run(slice('// ----- workspace pure logic -----','\n</script>'));
const flush=()=>new Promise(r=>setImmediate(r));
const descendants=n=>[n,...n.children.flatMap(descendants)];
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Chat harness ended before all assertions');process.exitCode=1;}});
(async()=>{
 // First open selects canonical, polls, renders shared markdown, and never probes.
 await run('aiShown()');await flush();assert.equal(run('aiChatId'),'canonical');assert.equal($('h-ai').textContent,'Telegram · основной диалог');assert.equal($('ai-mode').textContent,'агент с инструментами');assert($('ai-attach').disabled);assert($('ai-session-model').textContent.includes('session-model'));assert($('ai-msgs').textContent.includes('Telegram'));assert(!requests.some(r=>r.url==='/api/ai/probe'));
 // Canonical retries preserve the original request ID; never use the selected local model.
 $('ai-text').value='canonical draft';await $('ai-text').fire('input');responseError=true;await run('chatSend()');const first=requests.filter(r=>r.url==='/api/workspace/send').at(-1).body;assert.equal($('ai-text').value,'canonical draft');responseError=false;await run('chatSend()');const second=requests.filter(r=>r.url==='/api/workspace/send').at(-1).body;assert.equal(first.request,second.request);assert(!Object.hasOwn(second,'model'));assert.equal(storage.get('svodka-ai-draft:canonical'),'');
 // Drafts and uploads belong to conversations, including an unsent new conversation.
 run('aiNew()');$('ai-text').value='new draft';await $('ai-text').fire('input');run("aiUpload({name:'photo.png',size:100,type:'image/png'})");assert($('ai-send').disabled);const upload=uploads.at(-1);assert.equal(upload.headers['X-File-Name'],'photo.png');upload.upload.onprogress({lengthComputable:true,loaded:50,total:100});assert.equal(run('aiFiles()[0].progress'),50);
 await run("aiOpen('aaaaaaaaaaaa')");assert.equal($('ai-text').value,'');run('aiNew()');assert.equal($('ai-text').value,'new draft');assert.equal(run('aiFiles().length'),1);upload.finish(201,{id:'b'.repeat(24),name:'photo.png',kind:'image',mime:'image/png',size:100,pages:null});assert(!$('ai-send').disabled);
 run("aiModel='free';aiChips()");assert($('ai-send').disabled);assert($('ai-chips').textContent.includes('не принимает изображения'));run("aiModel='claude:claude-opus-5-5';aiChips()");assert(!$('ai-send').disabled);
 // Explicit failure retains text and files; success stores actual-model metadata and releases previews.
 sendFailure=true;await $('ai-form').fire('submit');assert.equal($('ai-text').value,'new draft');assert.equal(run('aiFiles().length'),1);assert($('ai-err').textContent.includes('требуется вход'));sendFailure=false;await $('ai-form').fire('submit');assert.equal(run('aiChatId'),'aaaaaaaaaaaa');assert.equal(run('aiFiles().length'),0);assert(revoked.includes('blob:preview'));assert($('ai-msgs').textContent.includes('фактически reported-model'));assert($('ai-msgs').textContent.includes('чат без инструментов'));assert.equal(requests.filter(r=>r.url==='/api/ai/send').at(-1).body.attachments[0],'b'.repeat(24));
 // Edit never sends. Repeat creates a new request with the original attachment IDs.
 const before=requests.filter(r=>r.url==='/api/ai/send').length;await descendants($('ai-msgs')).find(n=>n.tagName==='BUTTON'&&n.textContent==='Изменить').click();assert.equal($('ai-text').value,'new draft');assert.equal(requests.filter(r=>r.url==='/api/ai/send').length,before);await descendants($('ai-msgs')).find(n=>n.tagName==='BUTTON'&&n.textContent==='Повторить · новый запрос').click();assert.equal(requests.filter(r=>r.url==='/api/ai/send').length,before+1);assert.equal(requests.filter(r=>r.url==='/api/ai/send').at(-1).body.attachments[0],'b'.repeat(24));
 // Removing active uploads aborts; removing stored uploads deletes only unattached files.
 run('aiNew()');run("aiUpload({name:'a.txt',size:10,type:'text/plain'})");const active=uploads.at(-1);run('aiRemoveFile(aiFiles()[0])');assert(active.aborted);run("aiUpload({name:'b.txt',size:10,type:'text/plain'})");uploads.at(-1).finish(201,{id:'c'.repeat(24),name:'b.txt',size:10,kind:'text'});run('aiRemoveFile(aiFiles()[0])');await flush();assert(requests.some(r=>r.url==='/api/ai/file/delete'&&r.body.id==='c'.repeat(24)));
 // Hash alias selects Telegram; polling does not run in a local conversation or hidden tab.
 ctx.location.hash='#chat';await run('aiShown()');await flush();assert.equal(run('aiChatId'),'canonical');run('aiNew()');let polls=requests.filter(r=>r.url==='/api/workspace').length;await run('chatPoll()');assert.equal(requests.filter(r=>r.url==='/api/workspace').length,polls);await run("aiOpen('canonical')");await flush();document.hidden=true;polls=requests.filter(r=>r.url==='/api/workspace').length;await run('chatPoll()');assert.equal(requests.filter(r=>r.url==='/api/workspace').length,polls);document.hidden=false;
 // Legacy model migration remains visible after conversation initialization.
 run("aiModel='chatgpt'");await run('aiLoadModels()');run('aiNew()');assert.equal(run('aiModel'),'claude:claude-opus-5-5');assert($('ai-err').textContent.includes('модель заменена на Opus 5.5'));
 // Probe is a deliberate click; links are http(s) only and safe text never becomes HTML.
 const probe=descendants($('ai-picker')).find(n=>n.tagName==='BUTTON'&&n.textContent==='Проверить');await probe.click();assert.equal(requests.filter(r=>r.url==='/api/ai/probe').length,1);assert($('ai-picker').textContent.includes('проверено: фактически reported-model'));
 const md=run('aiMarkdown("[ok](https://example.test) [bad](javascript:alert) <img onerror=x> `code`\\n```js\\nhello\\n```")');const links=descendants(md).filter(n=>n.tagName==='A');assert.equal(links.length,1);assert.equal(links[0].attributes.rel,'noopener noreferrer');assert.equal(links[0].attributes.target,'_blank');assert(!descendants(md).some(n=>n.tagName==='IMG'));
 ctx.aborted=false;await run("AI.controller={abort(){aborted=true}};AI.run='run';aiStop()");assert(ctx.aborted);assert(requests.some(r=>r.url==='/api/ai/stop'&&r.body.run==='run'));
 assert(/id="tab-chat" hidden/.test(html));assert(/class="agent-toolbar" hidden/.test(html));assert(html.includes('--kb'));
 console.log('Unified chat drafts, canonical routing, upload lifecycle, model probes and message actions passed');
})().then(()=>{completed=true},e=>{completed=true;console.error(e);process.exitCode=1});
