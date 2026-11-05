const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '..', 'static', 'dart.js'), 'utf8');
function context(fetch) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'',_text:'',innerHTML:'',get textContent(){return this._text;},set textContent(value){this._text=value;this.innerHTML='';},disabled:false,hidden:false,handlers:{},addEventListener(name, fn){this.handlers[name] = fn;}});
    return nodes.get(id);
  };
  node('market').value='KOSPI';node('symbol').value='005930';
  node('dart-report').value='11011';node('dart-basis').value='BOTH';
  const document = {getElementById:node,addEventListener(){},createElement(){return {click(){},remove(){}};},body:{append(){}}};
  vm.runInNewContext(code,{document,fetch,Intl,Date,Number,Map,Set,String,Array,Object,JSON,AbortController,Error,URL,Blob,setTimeout,queueMicrotask});
  return node;
}
const response = data => ({ok:true,json:async()=>data});
const financial = {company:{corp_name:'삼성전자',stock_code:'005930'},year:2025,report_label:'사업보고서',complete:true,statements:[{fs_div:'CFS',fs_name:'연결',status:'available',accounts:[{account_nm:'<img src=x onerror=alert(1)>',account_detail:'지배기업',sj_div:'BS',sj_nm:'재무상태표',thstrm_amount:'9007199254740993123',thstrm_nm:'제57기',thstrm_add_amount:'-12000000000000000001',currency:'KRW',rcept_no:'20260310000001'}]}]};
function disclosures(page){return {company:financial.company,begin:'2026-01-01',end:'2026-11-01',page,total_count:3,total_pages:3,has_more:page<3,complete:page===3,items:[{rcept_no:'2026110500000'+page,title:'공시 '+page,date:'2026-11-05'}]};}
(async()=>{
  let attempts=0;
  const calls=[];
  const n=context(async(path,options)=>{
    const body=JSON.parse(options.body);calls.push([path,body]);
    if(path.endsWith('financials'))return response(financial);
    if(body.page===2 && attempts++===0)return {ok:false,status:429,json:async()=>({detail:'Rate limit'})};
    return response(disclosures(body.page));
  });
  await n('dart-form').handlers.submit({preventDefault(){}});
  assert(n('dart-financial-results').innerHTML.includes('9,007,199,254,740,993,123'));
  assert(n('dart-financial-results').innerHTML.includes('-12,000,000,000,000,000,001'));
  assert(n('dart-financial-results').innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!n('dart-financial-results').innerHTML.includes('<img src=x'));
  assert.equal(n('dart-more').hidden,false);
  await n('dart-all').handlers.click();
  assert(n('dart-disclosure-status').textContent.includes('요청 횟수 제한'));
  assert(n('dart-disclosure-status').textContent.includes('이전 1건'));
  assert.equal(n('dart-all').disabled,false);
  await n('dart-all').handlers.click();
  assert(n('dart-disclosure-status').textContent.includes('3건 중 3건'));
  assert(n('dart-disclosure-status').textContent.includes('전체 목록 조회 완료'));
  assert.equal(n('dart-all').hidden,true);
  assert.deepEqual(calls.filter(c=>c[0].endsWith('disclosures')).map(c=>c[1].page),[1,2,2,3]);
  n('symbol').value='000660';n('symbol').handlers.input();
  assert.equal(n('dart-financial-results').innerHTML,'');
  assert.equal(n('dart-financial-download').disabled,true);
  assert.equal(n('dart-disclosure-download').disabled,true);
  console.log('PASS exact financial integer strings, escaped account labels, 429 retry/resume, all-pages retrieval, stale-query download reset');
  const pending=[];
  const stale=context((path)=>new Promise(resolve=>pending.push(()=>resolve(response(path.endsWith('financials')?financial:disclosures(1))))));
  const running=stale('dart-form').handlers.submit({preventDefault(){}});
  stale('symbol').value='000660';stale('symbol').handlers.input();
  pending.forEach(resolve=>resolve());await running;
  assert.equal(stale('dart-financial-download').disabled,true);
  assert.equal(stale('dart-disclosure-download').disabled,true);
  assert(stale('dart-status').textContent.includes('조회 조건이 바뀌었습니다'));
  console.log('PASS late stale response cannot replace results after symbol changes');
  for (const mode of ['overlap','changed-total']) {
    const shifted=context(async(path,options)=>{
      if(path.endsWith('financials'))return response(financial);
      const page=JSON.parse(options.body).page;
      const data=disclosures(page);
      data.total_count=mode==='changed-total'&&page===2?1:2;
      data.has_more=page<2;
      if(mode==='overlap'&&page===2)data.items[0].rcept_no='20261105000001';
      return response(data);
    });
    await shifted('dart-form').handlers.submit({preventDefault(){}});
    await shifted('dart-all').handlers.click();
    assert(shifted('dart-disclosure-status').textContent.includes('수집 미완료'));
    assert(shifted('dart-disclosure-status').textContent.includes('처음부터 다시 조회'));
    assert(!shifted('dart-disclosure-status').textContent.includes('전체 목록 조회 완료'));
  }
  console.log('PASS overlapping pages and changing total counts never claim whole-list completeness');
})().catch(error=>{console.error(error);process.exitCode=1;});
