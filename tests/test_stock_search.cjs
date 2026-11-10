const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function page(handler = () => ({items: []})) {
  let now = 0, next = 0, invalidations = 0;
  const timers = new Map(), requests = [];
  const ids = ['company-name','symbol','market','stock-suggestions','stock-search-status'];
  const elements = Object.fromEntries(ids.map(id => [id, {
    value: '', hidden: true, innerHTML: '', textContent: '', dataset: {}, attrs: {}, listeners: {},
    addEventListener(type,callback) { (this.listeners[type] ||= []).push(callback); },
    setAttribute(key,value) {this.attrs[key] = String(value);},
    removeAttribute(key) {delete this.attrs[key];},
    dispatchEvent(event) {for (const listener of this.listeners[event.type] || []) listener(event);},
    querySelectorAll() {return [];}, focus() {this.focused = true;}
  }]));
  elements['company-name'].value = '삼성전자'; elements.symbol.value = '005930'; elements.market.value = 'KOSPI';
  const context = vm.createContext({
    document: {getElementById: id => elements[id]}, AbortController,
    Event: class Event {constructor(type,options) {this.type=type;Object.assign(this,options);}},
    invalidateCurrentSelection() {invalidations++;},
    setTimeout(callback, delay) {const id=++next;timers.set(id,{at:now+delay,callback});return id;},
    clearTimeout(id) {timers.delete(id);},
    fetch(url,options) {
      if (url === '/api/stocks/catalog') return Promise.resolve({ok:true,json:async()=>({items:[]})});
      requests.push({url,options});
      return Promise.resolve().then(() => handler(url, options)).then(result => {
        if (result && typeof result.ok === 'boolean') return result;
        return {ok:true,json:async()=>result};
      });
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/stock-search.js'),'utf8'),context);
  return {
    elements, requests, context,
    invalidations: () => invalidations,
    edit(id,value,event={}) {elements[id].value=value;elements[id].dispatchEvent({type:'input',...event});},
    event(id,type,event={}) {elements[id].dispatchEvent({type,...event});},
    resolve: () => context.resolveCurrentStockSelection(),
    advance(milliseconds) {
      const end=now+milliseconds;
      while (true) {
        const timer=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];
        if (!timer || timer[1].at>end) break;
        now=timer[1].at;timers.delete(timer[0]);timer[1].callback();
      }
      now=end;
    }
  };
}
async function flush() {for(let i=0;i<15;i++)await Promise.resolve();}
function deferred() {let resolve;return {promise:new Promise(yes=>resolve=yes),resolve:value=>resolve(value)};}

test('company edit immediately replaces prior ticker and market without lookup',async()=>{
  const p=page();
  p.context.stockSearchSelectionChanged('US','AAPL','Apple');
  p.edit('company-name','삼성전자');
  assert.equal(p.elements.symbol.value,'005930');
  assert.equal(p.elements.market.value,'KOSPI');
  assert.equal((await p.resolve()).name,'삼성전자');
  assert.equal(p.requests.length,0);
  assert.ok(p.invalidations()>0,'editing discards old analysis and QA');
});

test('preferred shares, aliases and typed ticker resolve distinct canonical identities',async()=>{
  const p=page();
  p.edit('company-name','삼성전자우');
  assert.equal(p.elements.symbol.value,'005935');
  p.edit('company-name','엔비디아');
  assert.equal(p.elements.symbol.value,'NVDA');
  assert.equal(p.elements.market.value,'US');
  p.edit('symbol','005930');
  assert.equal(p.elements['company-name'].value,'삼성전자');
  assert.equal((await p.resolve()).symbol,'005930');
});

test('ambiguous prefix clears stale ticker and waits for explicit selection',async()=>{
  const p=page();
  p.edit('company-name','삼성');
  assert.equal(p.elements.symbol.value,'');
  assert.equal(p.elements['stock-suggestions'].hidden,false);
  assert.match(p.elements['stock-suggestions'].innerHTML,/005930/);
  assert.match(p.elements['stock-suggestions'].innerHTML,/005935/);
  assert.equal(await p.resolve(),null);
  assert.equal(p.elements.symbol.value,'');
});

test('IME composition discards stale ticker and refuses premature analysis',async()=>{
  const p=page();
  p.event('company-name','compositionstart');
  assert.equal(p.elements.symbol.value,'');
  p.edit('company-name','삼성전자',{isComposing:true});
  assert.equal(await p.resolve(),null);
  assert.equal(p.requests.length,0);
  p.event('company-name','compositionend');
  assert.equal((await p.resolve()).symbol,'005930');
});

test('stale remote lookup never overwrites the newly selected stock',async()=>{
  const remote=deferred();
  const p=page(()=>remote.promise);
  p.edit('company-name','한화에어로스페이스');
  p.advance(200);await flush();
  p.edit('company-name','삼성전자');
  remote.resolve({items:[{symbol:'012450',name:'한화에어로스페이스',market:'KOSPI'}]});
  await flush();
  assert.equal(p.elements.symbol.value,'005930');
  assert.equal(p.elements['company-name'].value,'삼성전자');
});

test('lookup body timeout is visible even when fetch ignores abort and can retry',async()=>{
  const p=page(()=>({ok:true,json:()=>new Promise(()=>{})}));
  p.edit('company-name','없는회사');
  const pending=p.resolve();
  await flush();p.advance(4500);await flush();
  assert.equal(await pending,null);
  assert.match(p.elements['stock-search-status'].textContent,/늦어지고.*다시 검색/);
  p.edit('company-name','삼성전자');
  assert.equal((await p.resolve()).symbol,'005930');
});

test('saved unknown ticker is authoritative over an outdated saved company label',async()=>{
  const p=page(()=>({items:[{symbol:'012450',name:'한화에어로스페이스',market:'KOSPI'}]}));
  p.context.stockSearchSelectionChanged('KOSPI','012450','삼성전자');
  const result=await p.resolve();
  assert.equal(result.symbol,'012450');
  assert.equal(result.name,'한화에어로스페이스');
  assert.match(p.requests[0].url,/q=012450/);
});
