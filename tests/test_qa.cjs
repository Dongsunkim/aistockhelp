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
  const requests=[];
  const context=vm.createContext({
    document:{getElementById:id=>elements[id],createElement:tag=>new Element(tag)},
    URL,AbortController,
    fetch(url,options) {
      const pending=deferred();requests.push({url,options,...pending});return pending.promise;
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/qa.js'),'utf8'),context);
  return {elements,requests,reset:context.resetStockQA,
    submit(question) {
      elements['qa-input'].value=question;
      return elements['qa-form'].listeners.submit({preventDefault(){}});
    },
    respond(index,payload,status=200) {
      requests[index].resolve({ok:status>=200 && status<300,status,json:async()=>payload});
    }
  };
}

const samsung={symbol:'005930',market:'KOSPI',company_name:'삼성전자'};
const sk={symbol:'000660',market:'KOSPI',company_name:'SK하이닉스'};
const answer={mode:'ai',answer:'수집된 자료에 따른 답변입니다. [P1]',sources:[],asof:'2026-11-06',notice:'공시 원문 확인 필요'};

test('question is bound to the selected stock and displays loading immediately',async()=>{
  const p=page();p.reset(samsung);
  const pending=p.submit('  지금 기다려야 하나요?  ');
  assert.equal(p.elements['qa-heading'].textContent,'삼성전자(005930)에 질문');
  assert.equal(p.elements['qa-submit'].disabled,true);
  assert.equal(p.elements['qa-submit'].textContent,'답변 중…');
  assert.equal(p.elements['qa-form'].attributes['aria-busy'],'true');
  assert.equal(p.requests[0].url,'/api/ask');
  assert.deepEqual(JSON.parse(p.requests[0].options.body),{
    symbol:'005930',market:'KOSPI',name:'삼성전자',question:'지금 기다려야 하나요?'
  });
  p.respond(0,answer);await pending;
  assert.equal(p.elements['qa-answer'].textContent,answer.answer);
  assert.match(p.elements['qa-notice'].textContent,/^AI 답변/);
  assert.match(p.elements['qa-notice'].textContent,/2026-11-06/);
  assert.equal(p.elements['qa-submit'].disabled,false);
  assert.equal(p.elements['qa-form'].attributes['aria-busy'],'false');
});

test('reset aborts and ignores an answer from the previously selected stock',async()=>{
  const p=page();p.reset(samsung);
  const earlier=p.submit('기다릴 이유가 뭔가요?');
  p.reset(sk);
  assert.equal(p.requests[0].options.signal.aborted,true);
  assert.equal(p.elements['qa-result'].hidden,true);
  assert.equal(p.elements['qa-input'].value,'');
  p.respond(0,answer);await earlier;
  assert.equal(p.elements['qa-answer'].textContent,'');
  assert.equal(p.elements['qa-result'].hidden,true);
  assert.equal(p.elements['qa-heading'].textContent,'SK하이닉스(000660)에 질문');
});

test('newer question wins even when an earlier transport ignores cancellation',async()=>{
  const p=page();p.reset(samsung);
  const earlier=p.submit('실적은 어떤가요?');
  const later=p.submit('미국 금리가 어떤 영향을 주나요?');
  assert.equal(p.requests[0].options.signal.aborted,true);
  p.respond(1,{...answer,answer:'최근 금리 자료 확인이 필요합니다.'});await later;
  p.respond(0,{...answer,answer:'이전 질문 답변'});await earlier;
  assert.equal(p.elements['qa-answer'].textContent,'최근 금리 자료 확인이 필요합니다.');
  assert.equal(p.elements['qa-submit'].textContent,'질문');
});

test('data-only answer is labeled honestly and sources are safe text links',async()=>{
  const p=page();p.reset(samsung);
  const pending=p.submit('매수 구간은?');
  p.respond(0,{...answer,mode:'data_only',answer:'<script>alert(1)</script>',sources:[
    {id:'P1',title:'<img src=x onerror=alert(1)>',url:'https://finance.naver.com/item/main.naver?code=005930'},
    {id:'P2',title:'위험',url:'javascript:alert(1)'},
    {id:'P3',title:'데이터',url:'data:text/html,unsafe'},
    {id:'P4',title:'인증정보',url:'https://user:password@example.com/'},
    {id:'P5',title:'잘못된 주소',url:'not a URL'}
  ]});await pending;
  assert.match(p.elements['qa-notice'].textContent,/자료 기반 안내 · AI 미연결/);
  assert.equal(p.elements['qa-answer'].textContent,'<script>alert(1)</script>');
  const sources=p.elements['qa-sources'].children;
  assert.equal(sources.length,1);
  assert.equal(sources[0].tagName,'a');
  assert.equal(sources[0].textContent,'[P1] <img src=x onerror=alert(1)>');
  assert.equal(sources[0].rel,'noopener noreferrer');
  assert.equal(sources[0].target,'_blank');
});

test('network errors show a retry message without AI answer or internal details',async()=>{
  const p=page();p.reset(samsung);
  const pending=p.submit('실적이 궁금해요');
  p.requests[0].reject(new Error('internal credentials must not appear'));await pending;
  assert.equal(p.elements['qa-answer'].textContent,'');
  assert.match(p.elements['qa-notice'].textContent,/다시 질문/);
  assert.doesNotMatch(p.elements['qa-notice'].textContent,/credentials|AI 답변/);
  assert.equal(p.elements['qa-submit'].disabled,false);
  assert.equal(p.elements['qa-result'].dataset.mode,'unavailable');
});

test('unavailable server mode clears previous content and is not labeled as an AI answer',async()=>{
  const p=page();p.reset(samsung);
  const first=p.submit('기사 요약?');p.respond(0,answer);await first;
  const next=p.submit('다시 알려줘');
  p.respond(1,{mode:'unavailable',answer:'연결을 확인해 주세요.',notice:'AI 연결을 확인할 수 없습니다.'});await next;
  assert.equal(p.elements['qa-answer'].textContent,'');
  assert.equal(p.elements['qa-notice'].textContent,'AI 연결을 확인할 수 없습니다.');
  assert.equal(p.elements['qa-sources'].children.length,0);
  assert.equal(p.elements['qa-result'].dataset.mode,'unavailable');
});

test('no stock, blank and oversized questions never trigger a paid API request',async()=>{
  const p=page();
  await p.submit('아무 종목이나 알려줘');
  assert.equal(p.requests.length,0);
  assert.equal(p.elements['qa-submit'].disabled,true);
  p.reset(samsung);
  await p.submit('  ');await p.submit('가'.repeat(601));
  assert.equal(p.requests.length,0);
  assert.equal(p.elements['qa-input'].maxLength,600);
  assert.match(p.elements['qa-notice'].textContent,/600자/);
});

test('unrecognized or empty server answer cannot be presented as generated content',async()=>{
  for(const response of [{mode:'ai',answer:''},{mode:'unknown',answer:'unsupported'}]) {
    const p=page();p.reset(samsung);const pending=p.submit('질문');
    p.respond(0,response);await pending;
    assert.equal(p.elements['qa-answer'].textContent,'');
    assert.equal(p.elements['qa-result'].dataset.mode,'unavailable');
  }
});

test('an API rate limit has a clear retry message',async()=>{
  const p=page();p.reset(samsung);const pending=p.submit('지금?');
  p.respond(0,{},429);await pending;
  assert.match(p.elements['qa-notice'].textContent,/질문이 많습니다/);
  assert.equal(p.elements['qa-answer'].textContent,'');
});
