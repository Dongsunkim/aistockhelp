const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function deferred() {
  let resolve,reject;
  const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};
}

function clock() {
  let now=0,next=0;
  const timers=new Map();
  return {
    timers,
    setTimeout(callback,delay) {
      const id=++next;
      timers.set(id,{at:now+delay,callback});
      return id;
    },
    clearTimeout(id) {timers.delete(id);},
    advance(milliseconds) {
      const end=now+milliseconds;
      while(true) {
        const nextTimer=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];
        if(!nextTimer || nextTimer[1].at>end)break;
        now=nextTimer[1].at;
        timers.delete(nextTimer[0]);
        nextTimer[1].callback();
      }
      now=end;
    }
  };
}

class Element {
  constructor(tag='div') {
    this.tagName=tag;this.textContent='';this.value='';this.hidden=false;
    this.disabled=false;this.dataset={};this.children=[];this.attributes={};this.listeners={};
  }
  set innerHTML(value) {throw new Error('Untrusted HTML must not be inserted');}
  addEventListener(type,callback) {this.listeners[type]=callback;}
  setAttribute(name,value) {this.attributes[name]=value;}
  replaceChildren(...children) {this.children=[...children];}
  append(...children) {this.children.push(...children);}
}

function page() {
  const ids=['qa-form','qa-input','qa-submit','qa-answer','qa-notice','qa-sources','qa-result','qa-heading'];
  const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const requests=[],timer=clock();
  const context=vm.createContext({
    document:{getElementById:id=>elements[id],createElement:tag=>new Element(tag)},
    URL,AbortController,
    setTimeout:timer.setTimeout,clearTimeout:timer.clearTimeout,
    fetch(url,options) {
      // Deliberately ignore AbortSignal to exercise a stalled mobile transport.
      const pending=deferred();
      requests.push({url,options,...pending,bodyReads:0});
      return pending.promise;
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/qa.js'),'utf8'),context);
  return {elements,requests,timer,reset:context.resetStockQA,
    submit(question='지금 기다려야 하나요?') {
      elements['qa-input'].value=question;
      return elements['qa-form'].listeners.submit({preventDefault(){}});
    },
    respond(index,payload) {
      const request=requests[index];
      request.resolve({ok:true,status:200,json(){request.bodyReads++;return Promise.resolve(payload);}});
    },
    respondWithBody(index,body) {
      const request=requests[index];
      request.resolve({ok:true,status:200,json(){request.bodyReads++;return body.promise;}});
    }
  };
}

async function flush() {
  for(let step=0;step<12;step++)await Promise.resolve();
}

const samsung={symbol:'005930',market:'KOSPI',company_name:'삼성전자'};
const sk={symbol:'000660',market:'KOSPI',company_name:'SK하이닉스'};
const answer={mode:'ai',answer:'수집 자료에 따른 답변입니다. [P1]',sources:[
  {id:'P1',title:'확정 일봉',url:'https://finance.naver.com/item/main.naver?code=005930'}
],asof:'2026-11-06',notice:'원문 확인 필요'};

function assertIdle(p) {
  assert.equal(p.elements['qa-submit'].disabled,false);
  assert.equal(p.elements['qa-submit'].textContent,'질문');
  assert.equal(p.elements['qa-form'].attributes['aria-busy'],'false');
  assert.equal(p.timer.timers.size,0);
}

test('40-second watchdog settles a stalled fetch, clears previous content and permits retry',async()=>{
  const p=page();p.reset(samsung);
  const first=p.submit();p.respond(0,answer);await first;
  assert.equal(p.elements['qa-sources'].children.length,1);
  const pending=p.submit('새 질문을 알려줘');
  let settled=false;pending.then(()=>{settled=true;});
  assert.equal(p.elements['qa-answer'].textContent,'');
  assert.equal(p.elements['qa-sources'].children.length,0);
  p.timer.advance(39999);await flush();
  assert.equal(settled,false);
  assert.equal(p.elements['qa-submit'].disabled,true);
  p.timer.advance(1);await flush();
  assert.equal(settled,true,'UI must settle even when fetch ignores abort');
  await pending;
  assert.equal(p.requests.length,2,'timeout must not issue a duplicate question');
  assert.equal(p.requests[1].options.signal.aborted,true);
  assert.match(p.elements['qa-notice'].textContent,/답변 시간이 초과.*다시 질문/);
  assert.equal(p.elements['qa-result'].dataset.mode,'unavailable');
  assert.equal(p.elements['qa-answer'].textContent,'');
  assertIdle(p);
});

test('the watchdog includes a stalled JSON body and discards its late answer',async()=>{
  const p=page(),body=deferred();p.reset(samsung);
  const pending=p.submit();p.respondWithBody(0,body);await flush();
  assert.equal(p.requests[0].bodyReads,1);
  p.timer.advance(40000);await flush();await pending;
  const notice=p.elements['qa-notice'].textContent;
  assert.match(notice,/시간이 초과/);
  assertIdle(p);
  body.resolve(answer);await flush();
  assert.equal(p.elements['qa-notice'].textContent,notice);
  assert.equal(p.elements['qa-answer'].textContent,'');
  assert.equal(p.elements['qa-sources'].children.length,0);
  assert.equal(p.requests.length,1);
});

test('stock reset settles an uncancellable request and clears its deadline immediately',async()=>{
  const p=page();p.reset(samsung);
  const old=p.submit();let settled=false;old.then(()=>{settled=true;});
  p.timer.advance(10000);
  p.reset(sk);
  assert.equal(p.requests[0].options.signal.aborted,true);
  assert.equal(p.timer.timers.size,0);
  await flush();assert.equal(settled,true);await old;
  assert.equal(p.elements['qa-heading'].textContent,'SK하이닉스(000660)에 질문');
  assert.equal(p.elements['qa-result'].hidden,true);
  assertIdle(p);
  const current=p.submit('새 종목 실적은?');
  p.timer.advance(30000);await flush();
  assert.equal(p.elements['qa-submit'].disabled,true);
  assert.doesNotMatch(p.elements['qa-notice'].textContent,/시간이 초과/);
  p.respond(0,answer);await flush();
  assert.equal(p.elements['qa-answer'].textContent,'');
  p.respond(1,{...answer,answer:'SK하이닉스의 확인된 자료입니다.'});await current;
  assert.equal(p.elements['qa-answer'].textContent,'SK하이닉스의 확인된 자료입니다.');
  assertIdle(p);
});

test('a newer question owns its deadline and an older deadline cannot alter its result',async()=>{
  const p=page();p.reset(samsung);
  const old=p.submit('첫 번째 질문');
  p.timer.advance(15000);
  const current=p.submit('두 번째 질문');
  await flush();await old;
  assert.equal(p.timer.timers.size,1);
  p.timer.advance(25000);await flush();
  assert.equal(p.elements['qa-submit'].disabled,true);
  assert.equal(p.requests[1].options.signal.aborted,false);
  assert.doesNotMatch(p.elements['qa-notice'].textContent,/시간이 초과/);
  p.respond(1,{...answer,answer:'두 번째 질문의 답변'});await current;
  p.respond(0,{...answer,answer:'첫 번째 질문의 늦은 답변'});await flush();
  p.timer.advance(40000);await flush();
  assert.equal(p.elements['qa-answer'].textContent,'두 번째 질문의 답변');
  assertIdle(p);
});

test('late headers after timeout cannot render or replace a later successful question',async()=>{
  const p=page();p.reset(samsung);
  const old=p.submit();p.timer.advance(40000);await flush();await old;
  const current=p.submit('다시 질문합니다');
  p.respond(1,{...answer,answer:'재시도 답변'});await current;
  p.respond(0,{...answer,answer:'시간 초과 후 도착한 답변'});await flush();
  assert.equal(p.requests[0].bodyReads,0,'do not consume an expired response body');
  assert.equal(p.elements['qa-answer'].textContent,'재시도 답변');
  assertIdle(p);
});

for(const stage of ['fetch','json']) {
  test(`an unexpected active AbortError from ${stage} shows a retry message`,async()=>{
    const p=page();p.reset(samsung);
    const pending=p.submit();
    const error=new Error('unexpected abort');error.name='AbortError';
    if(stage==='fetch')p.requests[0].reject(error);
    else {
      const body=deferred();p.respondWithBody(0,body);await flush();body.reject(error);
    }
    await pending;
    assert.equal(p.requests[0].options.signal.aborted,false);
    assert.equal(p.elements['qa-result'].dataset.mode,'unavailable');
    assert.match(p.elements['qa-notice'].textContent,/다시 질문/);
    assert.equal(p.elements['qa-answer'].textContent,'');
    assertIdle(p);
  });
}

test('clearing the selected stock aborts and settles without enabling the empty question box',async()=>{
  const p=page();p.reset(samsung);
  const pending=p.submit();p.reset(null);
  assert.equal(p.timer.timers.size,0);
  await flush();await pending;
  p.timer.advance(40000);await flush();
  assert.equal(p.elements['qa-submit'].disabled,true);
  assert.equal(p.elements['qa-input'].disabled,true);
  assert.equal(p.elements['qa-result'].hidden,true);
  assert.equal(p.elements['qa-form'].attributes['aria-busy'],'false');
});
