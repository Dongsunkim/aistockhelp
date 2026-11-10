const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/alerts.js'),'utf8');

function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject};}
class Element{
  constructor(){this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.dataset={};this.children=[];this.listeners={};}
  set innerHTML(value){throw new Error('User data must be inserted as text');}
  addEventListener(name,listener){this.listeners[name]=listener;}
  replaceChildren(...children){this.children=[...children];}
  append(...children){this.children.push(...children);}
}
function page(token=''){
  const ids=['alerts-access-token','alerts-save','alerts-status','alerts-status-detail','alerts-selected','alerts-form','alerts-message','alerts-rules','alerts-events','alerts-access-form','alerts-disconnect','alerts-refresh','alert-buy-lower','alert-buy-upper','alert-sell','alert-stop','alerts-plan-source','alerts-use-analysis','alerts-settings','symbol','market'];
  const el=Object.fromEntries(ids.map(id=>[id,new Element()]));
  el.symbol.value='005930';el.market.value='KOSPI';
  const requests=[],storage=new Map(),timers=new Map(),listeners={};
  if(token)storage.set('signaldesk.alert-access',token);
  let now=0,nextId=0;
  const document={hidden:false,getElementById:id=>el[id],createElement:()=>new Element(),addEventListener:(type,fn)=>{listeners[type]=fn;}};
  const context=vm.createContext({document,AbortController,Date,Number,Promise,encodeURIComponent,
    sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    setTimeout(fn,delay){const id=++nextId;timers.set(id,{fn,at:now+delay});return id;},clearTimeout:id=>timers.delete(id),
    fetch(url,options){const pending=deferred();requests.push({url,options,...pending});return pending.promise;},
    resolveCurrentStockSelection:async()=>({symbol:'005930',name:'삼성전자',market:'KOSPI'}),
  });
  vm.runInContext(source,context);
  return {el,requests,storage,document,listeners,context,
    respond(index,payload,status=200){requests[index].resolve({ok:status<400,status,json:()=>Promise.resolve(payload)});},
    advance(ms){const end=now+ms;while(true){const pair=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];if(!pair||pair[1].at>end)break;now=pair[1].at;timers.delete(pair[0]);pair[1].fn();}now=end;},
    submit(){return el['alerts-form'].listeners.submit({preventDefault(){}});},
  };
}
const flush=async()=>{for(let i=0;i<24;i++)await Promise.resolve();};
const access='private-alert-token-at-least-24-characters';
const online={configured:true,worker_running:true,last_checked_at:'2026-11-10T02:01:00Z',quote_configured:true,delivery_configured:true,note:'15초 간격 점검'};
async function connect(p){p.respond(0,online);await flush();p.respond(1,{items:[]});p.respond(2,{items:[]});await flush();}
function allText(element){return [element.textContent,...element.children.map(allText)].join(' ');}

test('unconnected monitor is explicit and cannot register, status does not carry private token',async()=>{
  const p=page(access);
  assert.equal(p.requests[0].url,'/api/alerts/status');
  assert.equal(p.requests[0].options.headers.Authorization,undefined);
  p.respond(0,{configured:false,worker_running:false,note:'감시 서버 미연결'});await flush();
  assert.equal(p.el['alerts-save'].disabled,true);
  assert.equal(p.el['alerts-status'].textContent,'감시 서버 미연결');
  await p.submit();
  assert.equal(p.requests.length,1);
  assert.match(p.el['alerts-message'].textContent,/서버를 먼저 연결/);
});

test('private conditions use bearer token and render actual block reasons as text',async()=>{
  const p=page(access);p.respond(0,online);await flush();
  assert.equal(p.requests[1].options.headers.Authorization,`Bearer ${access}`);
  assert.equal(p.requests[2].options.headers.Authorization,`Bearer ${access}`);
  assert(!p.requests.some(r=>r.url.includes(access)));
  p.respond(1,{items:[{id:1,symbol:'005930',name:'<img src=x>',market:'KOSPI',action:'buy_zone',lower:70000,upper:72000,enabled:true,last_status:'blocked',last_reason:'미국 경제 자료 미확인',last_price:71000,checked_at:'2026-11-10T02:01:00Z'}]});
  p.respond(2,{items:[{message:'<script>alert(1)</script>',delivery_status:'sent',created_at:'2026-11-10T02:01:00Z'}]});await flush();
  assert.equal(p.el['alerts-save'].disabled,false);
  assert.match(allText(p.el['alerts-rules']),/<img src=x>/);
  assert.match(allText(p.el['alerts-rules']),/미국 경제 자료 미확인/);
  assert.match(allText(p.el['alerts-rules']),/71,000원/);
  assert.match(allText(p.el['alerts-events']),/휴대폰 전송 완료/);
  assert.match(allText(p.el['alerts-events']),/<script>/);
});

test('registration awaits canonical stock selection and sends only price rule',async()=>{
  const p=page(access);await connect(p);
  p.el['alert-buy-lower'].value='70000';p.el['alert-buy-upper'].value='72000';
  const resolved=deferred();p.context.resolveCurrentStockSelection=()=>resolved.promise;
  const submitted=p.submit();await flush();
  assert.equal(p.requests.length,3,'no request before stock identity settles');
  resolved.resolve({symbol:'005930',name:'삼성전자',market:'KOSPI'});await flush();
  const sent=JSON.parse(p.requests[3].options.body);
  assert.deepEqual(sent,{symbol:'005930',name:'삼성전자',market:'KOSPI',action:'buy_zone',lower:70000,upper:72000,enabled:true});
  assert.equal('evidence' in sent,false);
  p.respond(3,{id:1},201);await flush();p.respond(4,online);await flush();p.respond(5,{items:[]});p.respond(6,{items:[]});await submitted;
  assert.match(p.el['alerts-message'].textContent,/1개 조건을 감시에 등록/);
});

test('incomplete or reversed buy zone and absent price refuse before network mutation',async()=>{
  const p=page(access);await connect(p);
  p.el['alert-buy-lower'].value='72000';
  await p.submit();assert.match(p.el['alerts-message'].textContent,/하한과 상한/);
  p.el['alert-buy-upper'].value='70000';
  await p.submit();assert.match(p.el['alerts-message'].textContent,/하한과 상한/);
  p.el['alert-buy-lower'].value='';p.el['alert-buy-upper'].value='';
  await p.submit();assert.match(p.el['alerts-message'].textContent,/하나 이상/);
  assert.equal(p.requests.length,3);
});

test('disconnect removes session key and ignores in-flight private response',async()=>{
  const p=page(access);p.respond(0,online);await flush();
  p.el['alerts-disconnect'].listeners.click();
  assert.equal(p.storage.has('signaldesk.alert-access'),false);
  p.respond(1,{items:[{name:'SECRET',id:9,symbol:'005930'}]});p.respond(2,{items:[{message:'SECRET'}]});await flush();
  assert(!allText(p.el['alerts-rules']).includes('SECRET'));
  assert(!allText(p.el['alerts-events']).includes('SECRET'));
  assert.equal(p.el['alerts-save'].disabled,true);
});

test('five-second watchdog covers stalled body and clears previously running status',async()=>{
  const p=page(access);await connect(p);
  p.el['alerts-refresh'].listeners.click();await flush();
  const body=deferred();p.requests[3].resolve({ok:true,status:200,json:()=>body.promise});await flush();
  p.advance(5000);await flush();
  assert.equal(p.el['alerts-status'].textContent,'감시 서버 미연결');
  assert.equal(p.el['alerts-save'].disabled,true);
  assert.match(p.el['alerts-message'].textContent,/응답이 늦어/);
  body.resolve(online);await flush();
  assert.equal(p.el['alerts-status'].textContent,'감시 서버 미연결');
});

test('hidden page stops periodic refresh and returns to immediate status check',async()=>{
  const p=page();p.respond(0,{configured:false});await flush();
  p.document.hidden=true;p.listeners.visibilitychange();p.advance(60000);await flush();
  assert.equal(p.requests.length,1);
  p.document.hidden=false;p.listeners.visibilitychange();
  assert.equal(p.requests.length,2);
});

test('analysis price plan requires valid quality, ordered finite levels and current symbol',()=>{
  const p=page();
  const result={symbol:'005930',company_name:'삼성전자',market:'KOSPI',data_quality:{price_levels_eligible:true},analysis:{levels:{entry_low:70000,entry_high:72000,stop:66000,target_resistance:85000}}};
  p.context.setAlertAnalysis(result);
  assert.equal(p.el['alerts-use-analysis'].hidden,false);
  p.el['alerts-use-analysis'].listeners.click();
  assert.equal(p.el['alert-buy-lower'].value,'70000');
  assert.equal(p.el['alert-buy-upper'].value,'72000');
  assert.equal(p.el['alert-sell'].value,'');
  assert.equal(p.el['alert-stop'].value,'');
  assert.equal(p.el['alerts-settings'].open,true);
  assert.match(p.el['alerts-plan-source'].textContent,/근거 조건 미충족이면 알림 보류/);
  for (const bad of [
    {...result,symbol:'000660'},
    {...result,data_quality:{price_levels_eligible:false}},
    {...result,analysis:{levels:{...result.analysis.levels,entry_low:NaN}}},
    {...result,analysis:{levels:{...result.analysis.levels,stop:75000}}},
  ]) {p.context.setAlertAnalysis(bad);assert.equal(p.el['alerts-use-analysis'].hidden,true);}
});

test('identity change clears all previous stock prices and the derived plan',()=>{
  const p=page();
  for (const id of ['alert-buy-lower','alert-buy-upper','alert-sell','alert-stop'])p.el[id].value='70000';
  p.context.setAlertAnalysis({symbol:'005930',market:'KOSPI',data_quality:{price_levels_eligible:true},analysis:{levels:{entry_low:70000,entry_high:72000,stop:66000,target_resistance:85000}}});
  p.context.resetAlertSelection();
  for (const id of ['alert-buy-lower','alert-buy-upper','alert-sell','alert-stop'])assert.equal(p.el[id].value,'');
  assert.equal(p.el['alerts-use-analysis'].hidden,true);
  assert.equal(p.el['alerts-plan-source'].hidden,true);
  p.el['alerts-use-analysis'].listeners.click();
  assert.equal(p.el['alert-buy-lower'].value,'');
});
