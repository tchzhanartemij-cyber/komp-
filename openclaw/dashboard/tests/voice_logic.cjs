// Execute production voice helpers and lifecycle with deterministic browser facilities.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/../index.html','utf8');
const section=(a,b)=>html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)));
const core=vm.createContext({});vm.runInContext(section('// VOICE_CORE_BEGIN','// VOICE_CORE_END'),core);
const pure=s=>vm.runInContext(s,core),plain=x=>JSON.parse(JSON.stringify(x));
function vad(samples,mode='listening'){core.state={mode};let events=[];for(const [now,rms] of samples){core.now=now;core.rms=rms;const result=pure('vadStep(state,rms,now)');core.state=result.state;if(result.event)events.push(result.event);}return events;}
const speech=Array.from({length:9},(_,i)=>[i*50,.05]);
assert.deepEqual(vad([...speech,[1250,0],[1300,0]]),['start','end']);
assert.deepEqual(vad([[0,.05],[50,.05],[100,0],[949,0],[950,0]]),['start','discard']);
assert.deepEqual(vad([[0,.06],[300,.06],[600,.06]],'speaking'),[]);
assert.deepEqual(vad([[0,.1],[200,.1],[249,.1],[250,.1]],'speaking'),['barge']);
assert.deepEqual(vad([[0,.1],[200,.1],[240,.01],[300,.1],[500,.1]],'speaking'),[]);
assert.deepEqual(vad([...speech,[30000,.05]]),['start','end']);
const before={mode:'listening'};core.before=before;pure('vadStep(before,.05,0)');assert.deepEqual(before,{mode:'listening'});
assert.deepEqual(plain(pure('splitSentences("Первое. Втор")')),{ready:['Первое.'],rest:'Втор'});
assert.deepEqual(plain(pure('splitSentences("Второе! Третье? Хвост")')),{ready:['Второе!','Третье?'],rest:'Хвост'});
assert.equal(pure('isEcho("Я сохранил вашу заметку",["Первый ответ", "Да. Я сохранил вашу заметку в памяти."])'),true);
assert.equal(pure('isEcho("да",["Да, я сохранил вашу заметку"])'),false);
assert.equal(pure('isEcho("Создай другую задачу",["Я сохранил вашу заметку"])'),false);
core.markdown='# Заголовок\n**Ответ.** [Ссылка](https://example.com?k=hidden)\n```python\nsecret()\n```\nХвост.';
const spoken=pure('stripSpeechMarkdown(markdown)');assert(!spoken.includes('secret'));assert(!spoken.includes('hidden'));assert(spoken.includes('код показан на экране.'));assert(!/[#*`]/.test(spoken));
core.markdown='Ответ.\n```js\nneverSpeak()';assert(!pure('stripSpeechMarkdown(markdown)').includes('neverSpeak'));
core.markdown='Ответ.\n~~~js\nneverSpeak()\n~~~';assert(!pure('stripSpeechMarkdown(markdown)').includes('neverSpeak'));
assert.equal(pure('voiceInput("[Голосовой запрос из Сводки. Контекст: вкладка Память]\\nПривет")'),'Привет');
pure('var attempt=voiceAttempt(null,"A","Память","0123456789abcdef")');assert.strictEqual(pure('voiceAttempt(attempt,"B","ИИ","new")'),pure('attempt'));

function browser(storage=new Map()){
 let now=0,seq=0,cancelled=0,mics=0,stops=0;const nodes=new Map(),timers=new Map(),events={},spoken=[],requests=[];
 function node(){return {hidden:false,value:'',dataset:{state:'idle',collapsed:'false'},children:[],scrollTop:0,scrollHeight:0,clientHeight:100,textContent:'',setAttribute(k,v){this[k]=v;},focus(){},addEventListener(k,f){this[k]=f;},replaceChildren(...kids){this.children=kids;},append(...kids){this.children.push(...kids);}};}
 const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};$('voice').hidden=true;
 const track={readyState:'live',enabled:true,stop(){stops++;this.readyState='ended';}};
 const stream={getTracks:()=>[track],getAudioTracks:()=>[track]};
 class AudioContext{constructor(){this.state='suspended';}resume(){this.state='running';return Promise.resolve();}close(){this.state='closed';return Promise.resolve();}createMediaStreamSource(){return {connect(){},disconnect(){}};}createAnalyser(){return {fftSize:2048,getByteTimeDomainData(a){a.fill(128);}};}}
 class Recorder{constructor(s,o){this.state='inactive';this.mimeType=o.mimeType;}static isTypeSupported(t){return t==='audio/mp4';}start(){this.state='recording';}stop(){this.state='inactive';if(this.ondataavailable)this.ondataavailable({data:new Blob(['a'.repeat(2048)])});if(this.onstop)this.onstop();}}
 const listen=(k,f)=>{(events[k]||(events[k]=[])).push(f);};
 const context=vm.createContext({console,AbortController,Blob,Uint8Array,URL,Math,Date,Set,Map,JSON,Array,Promise,performance:{now:()=>now},current:'graph',selected:'dima',G:{ents:new Map([['dima',{name:'Дима'}]])},AI:{chat:null},$,el:(tag,p,...kids)=>({...node(),...p,children:kids}),statusLabel:s=>s,load:async()=>{},refreshAgent(){},
  document:{body:{dataset:{}},hidden:false,activeElement:null,addEventListener:listen},navigator:{onLine:true,mediaDevices:{getUserMedia:async()=>{mics++;return stream;}}},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},addEventListener:listen,
  setTimeout:(f,ms)=>{timers.set(++seq,{f,ms});return seq;},clearTimeout:id=>timers.delete(id),setInterval:(f,ms)=>{timers.set(++seq,{f,ms,interval:true});return seq;},clearInterval:id=>timers.delete(id),
  MediaRecorder:Recorder,SpeechSynthesisUtterance:class{constructor(text){this.text=text;}}
 });
 context.window={isSecureContext:true,MediaRecorder:Recorder,AudioContext,crypto:{},speechSynthesis:{cancel(){cancelled++;},speak(u){spoken.push(u);},getVoices:()=>[{lang:'ru-RU'}],resume(){}}};
 let route=async url=>url==='/api/voice'?{stt:true,reason:'ok'}:url==='/api/agent'?{available:true,runs:[]}:{tasks:{tasks:[]}};
 context.fetch=async(url,opts)=>{requests.push({url,opts});const body=await route(url,opts);if(body&&body.http)return {ok:false,status:body.http,json:async()=>({error:body.error})};return {ok:true,json:async()=>body};};
 vm.runInContext(section('// ----- voice:','</script>'),context);
 const run=s=>vm.runInContext(s,context),flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
 return {context,run,$,timers,spoken,requests,storage,track,stream,flush,events,route:f=>{route=f;},time:n=>{now=n;},mics:()=>mics,stops:()=>stops,cancelled:()=>cancelled,
  fire:async ms=>{const entry=[...timers].find(([id,t])=>t.ms===ms);assert(entry,'timer '+ms);timers.delete(entry[0]);entry[1].f();await flush();}};
}
async function main(){
 const b=browser();assert.equal(b.mics(),0);await b.run('voiceOpen()');await b.flush();assert.equal(b.mics(),0);assert(b.run('voiceContext()').includes('Дима'));
 const start=b.run('voiceStart()');await b.flush();await b.fire(180);await start;assert.equal(b.mics(),1);assert(b.run('V.session'));assert.equal(b.$('voice-mic-live').hidden,false);assert.equal(b.run('V.ac.state'),'running');
 b.run("current='work';voiceCollapse(true)");assert(b.run('V.session'));assert.equal(b.$('voice-mic-live').hidden,false);assert.equal(b.$('voice-body').hidden,true);
 b.run('voicePause()');assert.equal(b.track.enabled,false);assert.equal(b.$('voice').dataset.state,'paused');assert.equal(b.$('voice-mic-live').hidden,false);b.run('voicePause()');await b.flush();assert.equal(b.mics(),1);
 // A second tab cannot acquire the microphone; stop releases the same lock.
 const second=browser(b.storage);await second.run('voiceOpen()');await second.run('voiceStart()');assert.equal(second.mics(),0);assert(second.$('voice-notice').textContent.includes('другой вкладке'));
 b.run('voiceStop()');assert.equal(b.run('V.stream'),null);assert.equal(b.storage.has('svodka-voice-lock'),false);assert(b.cancelled()>0);assert.equal(b.$('voice-mic-live').hidden,true);
 // A permission prompt resolving after Stop cannot revive microphone tracks.
 const late=browser();let grant;late.context.navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{grant=resolve;});await late.run('voiceOpen()');const waiting=late.run('voiceStart()');await late.flush();await late.fire(180);late.run('voiceStop()');grant(late.stream);await waiting;assert.equal(late.track.readyState,'ended');assert.equal(late.run('V.session'),false);
 // Voice transcription immediately enqueues the agent turn. No review/manual submit.
 const v=browser();let failSend=true,serverOutput='',completed=false;
 v.route(async(url,opts)=>{
  if(url==='/api/voice/transcribe')return {text:'Запомни встречу завтра'};
  if(url==='/api/agent/send'){if(failSend){failSend=false;throw Error('connection lost after acceptance');}return {id:7,duplicate:true};}
  if(url==='/api/agent')return {available:true,runs:[{id:7,input:'[Голосовой запрос из Сводки. Контекст: Память]\nЗапомни встречу завтра',output:serverOutput,status:completed?'completed':'running',model:'actual-model'}]};
  if(url==='/api/data')return {tasks:{tasks:[{raw:'- [ ] встреча'}]}};
  return {ok:true};
 });
 v.run('V.session=true;V.capture={blob:new Blob(["a".repeat(2048)],{type:"audio/mp4"}),end:0,epoch:V.epoch}');v.time(1000);await v.run('voiceTranscribe()');assert.equal(v.run('V.turns.length'),1);assert.equal(v.run('V.turns[0].text'),'Запомни встречу завтра');assert.equal(v.$('voice-text').value,'');
 // Fetch failed after the server accepted: use the identical ID and exact payload.
 await v.run('voiceSubmit(V.turns[0])');assert.equal(v.$('voice').dataset.state,'offline');v.run('V.turns[0].next=0');await v.run('voicePoll()');
 const sends=v.requests.filter(r=>r.url==='/api/agent/send');assert.equal(sends.length,2);assert.equal(sends[0].opts.body,sends[1].opts.body);assert.equal(v.run('V.turns[0].id'),7);
 assert.equal(v.run('V.turns[0].metrics.t_transcript'),1000);
 // Polling speaks complete sentences while the server run is still running.
 serverOutput='Первое предложение. Незаверш';v.time(1700);await v.run('voicePoll()');assert.equal(v.spoken.length,1);assert.equal(v.spoken[0].text,'Первое предложение.');v.spoken[0].onstart();assert.equal(v.run('V.turns[0].metrics.t_first_audio'),1700);assert.equal(v.run('V.turns[0].finished'),false);
 await v.run('voicePoll()');assert.equal(v.spoken.length,1);
 serverOutput='Первое предложение. Незавершённое второе!\n```js\nprivateCode()\n```';await v.run('voicePoll()');assert.equal(v.run('V.queue.length'),2);assert.equal(v.run('V.recorder'),null);
 v.run('voiceInterrupt()');assert.equal(v.run('V.queue.length'),0);assert(v.cancelled()>0);assert(v.$('voice-notice').textContent.includes('ответ дописывается в истории'));assert(v.run('V.recorder!==null'));
 v.run('voiceDropRecording()');completed=true;await v.run('voicePoll()');assert.equal(v.spoken.length,1);assert.equal(v.run('V.turns[0].finished'),true);assert(v.$('voice-history').children[0].children[0].text.includes('Запомни'));assert(!v.$('voice-history').children[0].children[0].text.includes('[Голосовой'));assert(v.$('voice-history').children[0].children[3].text.includes('actual-model'));
 const metrics=v.requests.find(r=>r.url==='/api/voice/metrics');assert(metrics);const payload=JSON.parse(metrics.opts.body);assert.equal(payload.mode,'voice');assert(!('text' in payload));assert.equal(payload.t_done,1700);
 // Same-count edits are visible in the data diff.
 v.run('V.turns[0].before=voiceCounts({tasks:{tasks:[{raw:"- [x] встреча"}]}})');await v.run('voiceDiff(V.turns[0])');assert(v.$('voice-diff').textContent.includes('Дела: изменено'));
 // Echo is discarded; the next fresh turn remains possible.
 v.run('V.recent=[{text:"Запомни встречу завтра"}];V.capture={blob:new Blob(["a".repeat(2048)],{type:"audio/mp4"}),end:0,epoch:V.epoch}');await v.run('voiceTranscribe()');assert.equal(v.run('V.turns.length'),1);assert(v.$('voice-notice').textContent.includes('Эхо'));
 const hidden=browser();hidden.run('V.session=true;V.stream={getTracks:()=>[{readyState:"live",stop(){}}]}');hidden.context.document.hidden=true;hidden.events.visibilitychange.forEach(f=>f());assert.equal(hidden.run('V.session'),false);assert.equal(hidden.run('V.stream'),null);assert.equal(hidden.$('voice-status').textContent,'Разговор остановлен: страница свёрнута');
 const caps=browser();caps.run('V.session=true;V.started=0;V.idleSince=0;voiceState("listening")');caps.time(120000);caps.run('voiceTick()');assert(!caps.run('V.session'));assert(caps.$('voice-status').textContent.includes('120 секунд'));
 caps.run('V.session=true;V.started=0;V.paused=true');caps.time(900000);caps.run('voiceTick()');assert(!caps.run('V.session'));assert(caps.$('voice-status').textContent.includes('15 минут'));
 // A failed poll must not leave an existing recording running beyond its cap.
 const net=browser();net.run('V.session=true;V.offline=true;V.analyser={getByteTimeDomainData(a){a.fill(140)}};V.samples=new Uint8Array(2048);V.recorder={state:"recording",stop(){this.state="inactive";}};V.vad={mode:"hearing",started:0,last:29950,lastVoice:29950,speechMs:400}');net.time(30000);net.run('voiceTick()');assert.equal(net.run('V.recorder.state'),'inactive');
 // An interrupted iOS AudioContext exposes a gesture-driven resume, not a dead loop.
 const ios=browser();await ios.run('voiceOpen()');const iosStart=ios.run('voiceStart()');await ios.flush();await ios.fire(180);await iosStart;ios.run('V.ac.state="interrupted";V.ac.onstatechange()');assert.equal(ios.$('voice-resume').hidden,false);await ios.run('voiceResumeAudio()');assert.equal(ios.$('voice-resume').hidden,true);assert.equal(ios.mics(),1);ios.run('voiceStop()');
 bootTests();console.log('Voice logic, streaming, lifecycle, dedupe and boot diagnostics passed');
}
function bootTests(){
 const nodes=new Map(),timeouts=[],requests=[],listeners={};
 const document={body:null,getElementById:id=>nodes.get(id),createElement:tag=>({children:[],setAttribute(){},appendChild(c){this.children.push(c);}}),documentElement:{appendChild(n){nodes.set(n.id,n);}}};
 // Minimal style support is enough to prove early errors remain visible without the main script.
 const create=document.createElement;document.createElement=tag=>({...create(tag),style:{}});
 const window={addEventListener:(k,f)=>listeners[k]=f,fetch:(url,opts)=>{requests.push({url,opts});return Promise.reject(Error('401'));}};
 const ctx=vm.createContext({window,document,navigator:{userAgent:'WebView',sendBeacon:()=>false},Blob,Date,location:{reload(){}},setTimeout:(f,ms)=>timeouts.push({f,ms})});
 vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);assert.equal(timeouts[0].ms,12000);window.__svodkaBootStage='data-requested';timeouts[0].f();assert(nodes.get('boot-error').textContent.includes('этап data-requested'));
 window.onerror('broken at https://panel.test/?k=private-token');assert(window.__svodkaBootFailed);assert(!nodes.get('boot-error').textContent.includes('private-token'));assert(!requests[requests.length-1].opts.body.includes('private-token'));assert.equal(requests[0].url,'/api/client-log');assert.equal(requests[0].opts.keepalive,true);
 listeners.unhandledrejection({reason:Error('bad startup')});assert(nodes.get('boot-error').textContent.includes('bad startup'));assert.equal(nodes.get('boot-error').children[0].textContent,'Повторить');
 window.__svodkaBooted=true;const count=requests.length;window.onerror('after boot');assert.equal(requests.length,count);
}
let completed=false;process.on('beforeExit',()=>{if(!completed){console.error('Voice harness ended before async checks completed');process.exitCode=1;}});
main().then(()=>{completed=true;},e=>{completed=true;console.error(e);process.exitCode=1;});
