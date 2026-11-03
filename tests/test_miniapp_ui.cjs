const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.attributes={};this.dataset={};this.hidden=false;this.value='';this.textContent='';this.style={}}
 append(...children){for(const c of children)c.parent=this;this.children.push(...children)}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this)}
 replaceChildren(...children){this.children=[...children]}
 setAttribute(k,v){this.attributes[k]=v}
 querySelector(q){for(const c of this.children){if(q.startsWith('.')&&(c.className||'').split(' ').includes(q.slice(1)))return c;const nested=c.querySelector?.(q);if(nested)return nested}return null}
}
const ids=['accounts','error','metrics','catalog','pricing','pricingRules','reviews','destination','topicSettings','publication','fullscreenBtn','reviewsBtn','pricingBtn','catalogBtn','reload',...['home','catalog','pricing','suppliers','reviews','preview'].map(s=>'tab-'+s)];
const elements=Object.fromEntries(ids.map(id=>[id,new Element('div')]));
const buttons=['home','catalog','pricing','suppliers','reviews','preview'].map(x=>{let e=new Element('button');e.dataset.tab=x;return e});
const shortcuts=['reviews','suppliers','catalog'].map(x=>{let e=new Element('button');e.dataset.go=x;return e});
const doc={getElementById:id=>elements[id],createElement:tag=>new Element(tag),querySelectorAll:q=>q==='[data-tab]'?buttons:shortcuts};
const p={id:1,model:'Example',category:'Example',approved:true,hidden:false,price:'70',status:'priced',sim:''};
const data={'/destination':{connected:false,publication_enabled:false},'/publication':{enabled:false,published:0,pending:0,items:[]},'/accounts':[{connected:true}],'/products':[p],'/markups':[],'/pricing/overview':{total:1,priced:1,review:0,items:[{id:1,price:'90',status:'priced'}]}};
const code=fs.readFileSync('app/static/miniapp.html','utf8').split('<script>').pop().split('</script>')[0];
const context={document:doc,window:{Telegram:{WebApp:{initData:'test',ready(){},expand(){}}}},fetch:async url=>{let key=url.replace('/miniapp/api','');if(!(key in data))throw Error('Unexpected '+key);return {ok:true,json:async()=>data[key]}},console,setTimeout,clearTimeout,confirm:()=>false,Date,Map,encodeURIComponent};
vm.createContext(context);vm.runInContext(code,context);
const all=e=>String(e.textContent)+' '+e.children.map(all).join(' ');
const firstTest=(async()=>{await new Promise(r=>setImmediate(r));assert.equal(elements['tab-home'].hidden,false);assert.equal(elements['tab-catalog'].hidden,true);assert.match(all(elements.metrics),/Товаров  *1/);await vm.runInContext('loadCatalog()',context);let txt=all(elements.catalog);assert.match(txt,/Расчётная цена: 90/);assert.doesNotMatch(txt,/Расчётная цена: 70/);vm.runInContext("showTab('preview')",context);assert.equal(elements['tab-preview'].hidden,false);assert.equal(elements['tab-home'].hidden,true);await new Promise(r=>setImmediate(r));assert.match(all(elements.destination),/Чат пока не привязан/);assert.equal(elements.error.textContent,'');console.log('UI navigation and authoritative price passed');})().catch(e=>{console.error(e);process.exit(1)});


(async()=>{
  await firstTest;
  const requests=[];
  const item={id:42,model:'iPhone 17 Pro',category:'Смартфоны',memory:'256 GB',color:'Black',sim:'eSIM',approved:true,hidden:false};
  data['/products']=[item];
  data['/pricing/overview']={mode:'day',moscow_time:'2026-10-29T12:00:00+03:00',total:1,priced:1,review:0,items:[]};
  data['/pricing/rules']={markups:[],reserves:[],promotions:[]};
  context.fetch=async(url,opts={})=>{const key=url.replace('/miniapp/api','');if(opts.method){requests.push({key,body:JSON.parse(opts.body)});return {ok:true,json:async()=>({ok:true})}};if(!(key in data))throw Error('Unexpected '+key);return {ok:true,json:async()=>data[key]}};
  await vm.runInContext('loadPricing()',context);
  const forms=elements.pricingRules.children.filter(x=>x.className==='form-stack');
  assert.equal(forms.length,2);
  const promo=forms[1],scope=promo.children[0],target=promo.children[1],amount=promo.children[3],save=promo.children[8];
  assert.equal(scope.value,'sku');
  assert.equal(target.children[1].value,'42');
  assert.match(target.children[1].textContent,/iPhone 17 Pro.*256 GB.*Black.*eSIM/);
  target.value='42';amount.value='250';await save.onclick();
  assert.equal(requests[0].key,'/pricing/promotion');
  assert.equal(requests[0].body.scope,'sku');assert.equal(requests[0].body.target,'42');assert.equal(requests[0].body.amount,'250');
  await vm.runInContext('loadCatalog()',context);
  const form=elements.catalog.children.find(x=>x.className==='form-stack');
  form.children[0].value='sku';form.children[0].onchange();
  assert.equal(form.children[1].children[1].value,'42');
  data['/products']=[];await vm.runInContext('loadPricing()',context);
  const empty=elements.pricingRules.children.filter(x=>x.className==='form-stack')[1];
  assert.equal(empty.children[1].disabled,true);assert.match(empty.children[2].textContent,/Сначала добавьте товары/);
  console.log('Product selection, promotion payload, markup selection and empty state passed');
  let fullscreenRequests=0;const events={};const tg={initData:'test',ready(){},expand(){},isVersionAtLeast(){return true},requestFullscreen(){fullscreenRequests++},exitFullscreen(){},onEvent(name,fn){events[name]=fn},isFullscreen:false};
  const fullscreenContext={...context,window:{Telegram:{WebApp:tg}}};vm.createContext(fullscreenContext);vm.runInContext(code,fullscreenContext);
  assert.equal(elements.fullscreenBtn.hidden,false);elements.fullscreenBtn.onclick();assert.equal(fullscreenRequests,1);
  tg.isFullscreen=true;events.fullscreenChanged();assert.equal(elements.fullscreenBtn.textContent,'Свернуть');
  events.fullscreenFailed({error:'UNSUPPORTED'});assert.match(elements.error.textContent,/не поддерживает/);
  console.log('Fullscreen controls and unsupported-client fallback passed');
  const items=Array.from({length:51},(_,i)=>({line:i+1,model:'AirPods '+(i+1),cost:'10000',sim:''}));
  const iphone={line:52,model:'iPhone 17 Pro 256 Silver',cost:'99000',sim:'SIM + eSIM'};
  data['/accounts/1/messages']={messages:[{id:10,chat_id:'1',seen_at:'2026-10-29T12:00:00Z',text:'source'}]};
  data['/accounts/1/messages/10/preview']={candidates:items,needs_review:[{possible_iphone:iphone}],ignored:[],message_hash:'abc',hi_android_excluded:true};
  const holder=new Element('div');context.holder=holder;
  await vm.runInContext('loadMessages(1,holder)',context);
  const details=holder.children[0];await details.children.find(x=>x.textContent==='Предварительно разобрать').onclick();
  const result=details.children.find(x=>x.className==='result');
  assert.doesNotMatch(all(result),/AirPods 51 —/);
  const candidateList=result.children.find(x=>x.children.some(y=>y.textContent.startsWith('Показать ещё')));
  await candidateList.children.find(x=>x.textContent.startsWith('Показать ещё')).onclick();
  assert.match(all(result),/AirPods 51 —/);
  const reviewList=result.children.find(x=>all(x).includes('Это iPhone — добавить'));
  const entry=reviewList.children[0];await entry.children.find(x=>x.textContent==='Это iPhone — добавить').onclick();
  assert(!entry.children[2].children.some(x=>x.tag==='input'));context.confirm=()=>true;
  const confirmSave=entry.children[2].children.find(x=>x.tag==='button');await confirmSave.onclick();
  const req=requests[requests.length-1];assert.equal(req.key,'/accounts/1/messages/10/import-line');assert.equal(req.body.confirmed_iphone,true);assert.equal(req.body.line,52);
  console.log('Price preview pagination and iPhone confirmation passed');
  data['/accounts/1/message-sources']={sources:[{chat_id:'1',label:'HI · @hi_catalog_bot',count:2},{chat_id:'2',label:'Tech · @tech_online_seller_bot',count:1}]};
  data['/accounts/1/messages?chat_id=1']={messages:[{id:11,chat_id:'1',seen_at:'2026-10-29T12:00:00Z',text:'HI price'}],next_before:11};
  data['/accounts/1/messages?before=11&chat_id=1']={messages:[{id:9,chat_id:'1',seen_at:'2026-10-29T12:00:00Z',text:'HI older'}]};
  data['/accounts/1/messages?chat_id=2']={messages:[{id:12,chat_id:'2',seen_at:'2026-10-29T12:00:00Z',text:'Tech price'}]};
  const sourcesHolder=new Element('div');context.sourcesHolder=sourcesHolder;
  await vm.runInContext('loadMessageSources(1,sourcesHolder)',context);
  const groups=sourcesHolder.children.filter(x=>x.className==='source-group');assert.equal(groups.length,2);
  groups[0].open=true;await groups[0].ontoggle();groups[1].open=true;await groups[1].ontoggle();
  assert.match(all(groups[0]),/HI price/);assert.doesNotMatch(all(groups[0]),/Tech price/);assert.match(all(groups[1]),/Tech price/);
  await groups[0].children[1].children.find(x=>x.textContent==='Ещё сообщения').onclick();
  assert.match(all(groups[0]),/HI older/);assert.doesNotMatch(all(groups[1]),/HI older/);
  console.log('Messages separated and paginated by actual source passed');
  data['/destination/topics']={topics:[{thread_id:41,title:'Техника',closed:false}],rules:[],publication_enabled:false};
  data['/products']=[{id:1,brand:'Apple',category:'Смартфоны'}];
  context.destinationForTest={connected:true,is_forum:true};await vm.runInContext('loadTopics(destinationForTest)',context);
  const topicForm=elements.topicSettings.children.find(x=>x.className==='form-stack');
  const selectors=topicForm.children.filter(x=>x.tag==='select');assert(selectors[1].children.some(x=>x.value==='Apple'&&x.textContent==='Apple'));assert(selectors[2].children.some(x=>x.value==='Смартфоны'));assert.equal(selectors[1].children[0].disabled,false);selectors[1].value='Apple';selectors[2].value='Смартфоны';selectors[3].value='41';
  await topicForm.children.find(x=>x.textContent==='Сохранить правило').onclick();
  const topicReq=requests[requests.length-1];assert.equal(topicReq.key,'/destination/routes');assert.equal(topicReq.body.brand,'Apple');assert.equal(topicReq.body.category,'Смартфоны');assert.equal(topicReq.body.thread_id,41);
  console.log('Topic routing selection passed');
  data['/products']=[{id:77,model:'AirPods 4',brand:'Apple',category:'Наушники',sim:'',approved:false}];
  data['/product-sources']=[{product_id:77,slot:1,chat_id:'22',message_id:10,line:1,message_hash:'abc',price:'8400',source_at:'2026-10-29T12:00:00Z'}];
  await vm.runInContext('loadReviews()',context);
  const review=elements.reviews.children.find(x=>x.tag==='details');assert.match(all(review),/цена в сообщении: 8400/);assert.match(all(review),/не подтверждена/);
  const sourceForm=review.children.find(x=>x.className==='form-stack');assert(!sourceForm.children.some(x=>x.tag==='input')); 
  await sourceForm.children.find(x=>x.textContent==='Подтвердить цену на сегодня').onclick();
  const priceReq=requests[requests.length-1];assert.equal(priceReq.key,'/accounts/1/messages/10/import-line');assert.equal(priceReq.body.line,1);assert.equal(priceReq.body.confirmed_today,true);
  console.log('Incoming product and price review passed');
  let failTopics=false;const topicCalls=[];
  context.fetch=async(url,opts={})=>{const key=url.replace('/miniapp/api','');topicCalls.push({key,method:opts.method||'GET'});if(key==='/destination/topics'&&failTopics)return {ok:false,json:async()=>({detail:'Не удалось получить список'})};return {ok:true,json:async()=>data[key]}};
  await vm.runInContext('loadTopics(destinationForTest)',context);
  assert.match(all(elements.topicSettings),/\/volttopic Название/);
  assert.doesNotMatch(all(elements.topicSettings),/Добавить аккаунт в чат|отслеживать поставщиков/);
  failTopics=true;const refreshButton=elements.topicSettings.children.find(x=>x.textContent==='Обновить список тем');await refreshButton.onclick();
  assert.equal(refreshButton.disabled,false);assert.match(all(elements.topicSettings),/Не удалось получить список/);
  console.log('Topic reload failure shown beside button passed');
  data['/accounts']=[{connected:true,slot:1,name:'Ilya'}];
  data['/destination/topics']={topics:[{thread_id:41,title:'Apple',closed:false}],rules:[],publication_enabled:false};
  data['/products']=[];
  failTopics=false;topicCalls.length=0;
  context.destinationForTest={connected:true,is_forum:true,title:'Quantumm'};
  await vm.runInContext('loadTopics(destinationForTest)',context);
  data['/destination/topics'].topics.push({thread_id:42,title:'Samsung',closed:false});
  await elements.topicSettings.children.find(x=>x.textContent==='Обновить список тем').onclick();
  assert.match(all(elements.topicSettings),/Список обновлён/);
  const routeForm=elements.topicSettings.children.find(x=>x.className==='form-stack');
  assert(routeForm.children.filter(x=>x.tag==='select')[3].children.some(x=>x.textContent==='Samsung'));
  assert(topicCalls.filter(x=>x.key==='/destination/topics').every(x=>x.method==='GET'));
  assert(!topicCalls.some(x=>x.key==='/accounts'||x.key.includes('/join/')||x.key.includes('/sync')));
  console.log('Bot topic list refresh uses saved topics without account or sync passed');

  const publicationCalls=[];let failPublish=false;
  data['/publication']={enabled:false,published:2,uncertain:0,errors:0,retry_after:0,price_mode:'verified_only',blockers:{total:3,needs_approval:1,hidden:0,missing_sim:1,no_offer:0,no_topic:1,no_forum:0},problem_products:[],items:[{name:'iPhone 17 256',topic:'Apple',price:'90 000 ₽',status:'готов'}]};
  context.fetch=async(url,opts={})=>{const key=url.replace('/miniapp/api','');publicationCalls.push({key,method:opts.method||'GET'});if(key==='/publication/enable'&&failPublish)return {ok:false,json:async()=>({detail:'Бот не имеет доступа'})};if(key==='/publication/enable')data['/publication'].enabled=true;if(key==='/publication/pause')data['/publication'].enabled=false;return {ok:true,json:async()=>data[key]||{ok:true}}};
  await vm.runInContext('loadPublication()',context);
  assert.match(all(elements.publication),/Публикация приостановлена/);
  assert.match(all(elements.publication),/ожидают одобрения: 1/);assert.match(all(elements.publication),/не указана SIM: 1/);assert.match(all(elements.publication),/не назначена тема: 1/);
  assert.match(all(elements.publication),/Без подтверждённой цены товар показывается «По запросу»/);
  assert(!publicationCalls.some(x=>x.method==='POST'));
  await elements.publication.children.find(x=>x.textContent==='Предпросмотр').onclick();
  assert.match(all(elements.publication),/iPhone 17 256.*Apple.*90 000/);
  assert(!elements.publication.children.some(x=>x.textContent==='Обновить публикацию'));
  failPublish=true;await elements.publication.children.find(x=>x.textContent==='Включить публикацию').onclick();
  assert.match(all(elements.publication),/Не удалось включить публикацию: Бот не имеет доступа/);
  failPublish=false;await elements.publication.children.find(x=>x.textContent==='Включить публикацию').onclick();
  assert.match(all(elements.publication),/Публикация включена/);
  assert(!publicationCalls.some(x=>x.key==='/publication/sync'));
  data['/publication'].errors=1;data['/publication'].uncertain=2;data['/publication'].retry_after=30;data['/publication'].problem_products=[{state:'send_failed',error:'TelegramForbiddenError'}];
  await elements.publication.children.find(x=>x.textContent==='Обновить публикацию').onclick();
  assert.match(all(elements.publication),/Ошибок публикации: 1/);assert.match(all(elements.publication),/Не удалось подтвердить результат отправки для 2/);assert.match(all(elements.publication),/30 сек/);assert.match(all(elements.publication),/TelegramForbiddenError/);
  await elements.publication.children.find(x=>x.textContent==='Приостановить').onclick();
  assert.match(all(elements.publication),/Публикация приостановлена/);
  assert.equal(publicationCalls.filter(x=>x.key==='/publication/sync'&&x.method==='POST').length,1);
  console.log('Publication preview and explicit sync passed');





})().catch(e=>{console.error(e);process.exitCode=1});


