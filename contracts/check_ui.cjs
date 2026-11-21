const fs = require('fs'), vm = require('vm'), assert = require('assert');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const all=[]; let document;
class Element {
 constructor(tag,attrs={}) { this.tagName=tag.toUpperCase(); this.attrs={...attrs};this.value=attrs.value||'';this.checked='checked'in attrs;this.disabled=false;this.className=attrs.class||'';this.dataset={};this.children=[];this.handlers={};this.textContent='';this.innerHTML='';this.scrollTop=0;this.scrollHeight=200;this.firstElementChild={nextSibling:null};for(const[k,v]of Object.entries(attrs))if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;this.classList={contains:c=>this.className.split(/\s+/).includes(c),add:c=>{if(!this.classList.contains(c))this.className+=' '+c},remove:c=>{this.className=this.className.split(/\s+/).filter(x=>x!==c).join(' ')},toggle:(c,on)=>{on=on===undefined?!this.classList.contains(c):on;on?this.classList.add(c):this.classList.remove(c);return on;}};}
 setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k]??null;}removeAttribute(k){delete this.attrs[k];}addEventListener(k,f){this.handlers[k]=f;}focus(){document.activeElement=this;}appendChild(el){this.children.push(el);}insertBefore(el){this.children.push(el);}replaceChildren(){this.children=[];}querySelector(sel){return this.children.find(x=>matches(x,sel))||null;}querySelectorAll(sel){return query(sel);}
}
function matches(el,sel){sel=sel.trim().split(/\s+/).at(-1);if(sel.startsWith('#'))return el.attrs.id===sel.slice(1);if(sel.startsWith('['))return sel.slice(1,-1)in el.attrs;let [tag,...cs]=sel.split('.');return (!tag||el.tagName===tag.toUpperCase())&&cs.every(c=>el.classList.contains(c));}
function query(sel){return all.filter(x=>sel.split(',').some(part=>matches(x,part)));}
for(const [tag,attrs]of data.nodes)all.push(new Element(tag,attrs));
document={getElementById:id=>all.find(x=>x.attrs.id===id)||null,querySelectorAll:query,querySelector:s=>query(s)[0],createElement:t=>new Element(t),addEventListener(){},activeElement:{tagName:'BODY'}};
const el=id=>document.getElementById(id);
for(const [id,value]of Object.entries({securitySelect:'300308',tradeChannel:'auction',demoConnection:'online',demoReceipt:'pending',demoInbox:'normal'}))el(id).value=value;
const context={document,window:{clearTimeout(){},setTimeout(){},setInterval(){}},history:{replaceState(){}},location:{hash:''},console};vm.createContext(context);
let code=fs.readFileSync(process.argv[3],'utf8').replace('    })();','globalThis.api={showScreen,handleAction,sendChat,updateChannel,updateConnection,queue:demoQueue};\n    })();');
vm.runInContext(code,context);const api=context.api;let checks=0;function test(name,f){f();checks++;console.log('PASS '+name);}
const action=(name,id)=>api.handleAction(name,el(id));
test('13 screens retain navigable active state',()=>{for(let n=1;n<=13;n++){api.showScreen(n);assert(el('screen-'+n).classList.contains('active'));}});
test('rich chat belongs to chat tab',()=>{api.showScreen(12);assert(query('[data-tab]').find(x=>x.dataset.tab==='11').attrs['aria-current']==='page');});
test('zero trade quantity blocks submission and focuses field',()=>{api.showScreen(3);el('buyQuantity').value='0';action('confirm-buy','confirmBuy');assert.equal(api.queue.length,0);assert.equal(document.activeElement,el('buyQuantity'));});
test('valid 25-share fill defaults to pending and no success receipt',()=>{el('buyQuantity').value='25';action('confirm-buy','confirmBuy');assert.equal(api.queue[0].stage,'pending');assert(el('buyReceipt').classList.contains('hidden'));});
test('repeated pending click reuses command id',()=>{const id=api.queue[0].id;action('confirm-buy','confirmBuy');assert.equal(api.queue.length,1);assert.equal(api.queue[0].id,id);});
test('transport receipt remains business pending',()=>{el('demoReceipt').value='delivered';el('buyQuantity').value='26';action('confirm-buy','confirmBuy');assert.equal(api.queue[1].stage,'delivered');assert(el('buyReceipt').classList.contains('hidden'));});
test('offline mode never grants selected success scenario',()=>{el('demoReceipt').value='ack';el('demoConnection').value='local-offline';el('buyQuantity').value='27';action('confirm-buy','confirmBuy');assert.equal(api.queue[2].stage,'pending');assert(el('buyReceipt').classList.contains('hidden'));});
test('explicit online business ack reveals sample receipt',()=>{el('demoConnection').value='online';el('buyQuantity').value='28';action('confirm-buy','confirmBuy');assert.equal(api.queue[3].stage,'ack');assert(!el('buyReceipt').classList.contains('hidden'));});
test('after-hours channel supplies machine-readable datetime',()=>{el('tradeChannel').value='afterHours';api.updateChannel();assert(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(el('buyTime').value));});
test('recovery isolates old queue and blocks new writes',()=>{el('demoConnection').value='recovering';el('demoConnection').handlers.change();const size=api.queue.length;action('pause','pauseButton');assert.equal(api.queue.length,size);assert.equal(api.queue[0].stage,'isolated');});
test('chat creates outgoing message only',()=>{api.showScreen(11);el('demoConnection').value='offline';el('chatInput').value='测试离线';const count=el('chatMessages').children.length;api.sendChat();assert.equal(el('chatMessages').children.length,count+1);assert.equal(api.queue.at(-1).stage,'pending');});
test('file deletion offline remains pending, original retained',()=>{api.showScreen(13);action('delete-file','applyImport');assert(!el('fileDeleteConfirm').classList.contains('hidden'));action('confirm-delete-file','applyImport');assert(el('fileFeedback').textContent.includes('原图状态保持已保留'));});
test('invalid import quantity focuses error and does not queue',()=>{el('externalConsent').checked=true;const n=query('.number-input')[0];n.value='0.5';const size=api.queue.length;action('apply-import','applyImport');assert.equal(api.queue.length,size);assert.equal(document.activeElement,n);});
console.log('TOTAL '+checks+' logic scenarios; DOM stub only, no browser or layout verification.');
