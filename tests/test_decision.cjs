const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/decision.js'),'utf8'),context);
const decide=result=>context.buildDecision(result);

function result() {
  return {market:'KOSPI',financials:[],analysis:{close:100,data_age_days:1,
    levels:{entry_low:98,entry_high:102,rr:2.5},technical_ok:true,confirmed:false,
    conditions:['trend','pullback','rsi','volatility','volume','rr'].map(id=>({id,pass:true})),
    evidence_checks:[{name:'종가 데이터 최근 5일 이내',pass:true}],red_flags:[]},
    data_quality:{price_levels_eligible:true,verified_for_trade:false,price_level_blockers:[],
      financials_eligible:false,financial_blockers:['재무제표 핵심 3개 계정 조회 미완료'],
      warnings:['재무제표 핵심 3개 계정 조회 미완료']}};
}

function favorable() {
  const r=result();
  r.macro=benignMacro();
  r.financials=['매출액','영업이익','순이익'].map((name,i)=>({name,value:[7e13,8e12,6e12][i],
    unit:'KRW',period:'2026년 반기',basis:'연결 재무제표 · 분기/반기는 누적값일 수 있음',
    source:'OpenDART 주요 재무계정'}));
  r.data_quality.financials_eligible=true;r.data_quality.financial_blockers=[];
  r.data_quality.warnings=['최신 가치평가 및 증권사 원문 교차검증 미완료'];
  r.news=[{title:'삼성전자 실적 관련 보도',age_hours:3}];
  r.filings=[{title:'반기보고서',age_days:40,source:'금융감독원 DART'}];
  r.analysis.evidence_checks.push({name:'최근 72시간 내 뉴스',pass:true},
    {name:'최근 120일 내 공식 공시',pass:true},{name:'위험 키워드 기사 없음',pass:true});
  return r;
}

function benignMacro() {
  return {eligible:true,summary:'필수 자료를 확인했습니다.',risk_flags:[],warnings:[],
    checked_at:'2026-11-06T01:00:00+00:00',metrics:Object.fromEntries(
      ['rate','usdkrw','inflation','sp500','nasdaq','sox','nvda','aapl','msft'].map(id=>[id,{
        value:100,unit:id==='rate' || id==='inflation'?'%':'지수/가격',
        change_pct:0.2,change_pp:0,asof:'2026-11-05',source:'오프라인 테스트 제공원',
        url:'https://example.org/metric/'+id}]))};
}

test('valid technicals without financials produce a clear hold with reason',()=>{
  const r=result();r.macro=benignMacro();const decision=decide(r);
  assert.match(decision.headline,/매수 판단은 보류/);
  assert.ok(decision.reasons.some(r=>r.includes('재무자료가 부족')));
  assert.match(decision.nextStep,/98원 ~ 102원 구간 안/);
  assert.equal(decision.reasons.length,3);
});

test('failed trend and price explain waiting and the actual price range',()=>{
  const r=result();r.analysis.technical_ok=false;r.analysis.close=110;
  r.analysis.conditions[0].pass=false;r.analysis.conditions[1].pass=false;
  const decision=decide(r);
  assert.equal(decision.headline,'지금은 매수를 서두르기 어렵습니다');
  assert.match(decision.reasons[0],/상승 흐름/);
  assert.match(decision.reasons[1],/98원 ~ 102원.*높습니다/);
  assert.match(decision.nextStep,/내려오는지/);
});

test('low price does not become a buy signal',()=>{
  const r=result();r.analysis.close=90;r.analysis.conditions[1].pass=false;
  const decision=decide(r);
  assert.match(decision.reasons[0],/추가 하락/);
  assert.match(decision.nextStep,/회복하는지/);
  assert.notEqual(decision.tone,'review');
});

test('stale data takes priority over otherwise passing price conditions',()=>{
  const r=result();r.analysis.data_age_days=6;
  const decision=decide(r);
  assert.match(decision.headline,/판단하기 어렵습니다/);
  assert.match(decision.reasons[0],/최신 확정 종가/);
  assert.doesNotMatch(decision.nextStep,/98원/);
});

test('unverified price source explains why a judgment cannot be made',()=>{
  const r=result();r.data_quality.price_levels_eligible=false;
  r.data_quality.price_level_blockers=['국내 데이터 출처가 네이버로 검증되지 않았습니다.'];
  const decision=decide(r);
  assert.match(decision.reasons[0],/출처/);
  assert.match(decision.headline,/판단하기 어렵습니다/);
});

test('detected risk is stated as a title-based finding requiring the original',()=>{
  const r=result();r.analysis.red_flags=['회계부정 수사 보도'];
  assert.ok(decide(r).reasons.some(reason=>reason.includes('제목에서 위험 문구')));
});

test('each approval flag alone is insufficient for a buy review judgment',()=>{
  for(const flag of ['quality','analysis']) {
    const r=favorable();
    if(flag==='quality')r.data_quality.verified_for_trade=true;
    else r.analysis.confirmed=true;
    assert.notEqual(decide(r).tone,'review');
  }
});

test('fully verified result may describe review eligibility without promising a trade',()=>{
  const r=favorable();r.data_quality.verified_for_trade=true;r.analysis.confirmed=true;
  const decision=decide(r);
  assert.equal(decision.tone,'review');
  assert.equal(decision.headline,'매수 검토 조건을 충족했습니다');
  r.financials_error='조회 실패';
  assert.notEqual(decide(r).tone,'review');
});

test('US range is formatted in dollars and missing payload stays inconclusive',()=>{
  const r=result();r.market='US';r.macro=benignMacro();
  assert.match(decide(r).nextStep,/98.00달러 ~ 102.00달러/);
  assert.match(decide({}).headline,/판단하기 어렵습니다/);
});

test('all observed inputs can yield a watch candidate while trade verification stays false',()=>{
  const r=favorable();const decision=decide(r);
  assert.equal(r.data_quality.verified_for_trade,false);
  assert.equal(r.analysis.confirmed,false);
  assert.equal(decision.headline,'매수 관찰 후보입니다');
  assert.equal(decision.tone,'watch');
  assert.ok(decision.reasons.some(reason=>reason.includes('2026년 반기 매출 70조원, 영업이익 8조원')));
  assert.ok(decision.reasons.some(reason=>reason.includes('제목과 발행 시점만')));
  assert.match(decision.nextStep,/실시간 가격과 공시·기사 원문/);
  assert.equal(decision.sources.map(source=>source.state).join(','),'확인,확인,제목 확인,자료 없음,확인');
});

test('account labels and unvalidated facts cannot create a favorable candidate',()=>{
  for(const mutate of [
    r=>{r.financials=[];},
    r=>{r.data_quality.financials_eligible=false;},
    r=>{delete r.data_quality.financials_eligible;},
    r=>{r.financials[0].value='70000000000000';},
    r=>{r.financials[0].value=NaN;},
    r=>{r.financials[0].unit='USD';},
    r=>{r.financials[0].source='블로그';},
    r=>{r.financials[0].period='2025년 반기';},
    r=>{r.financials[0].basis='별도';},
    r=>{r.financials.push({...r.financials[0]});}
  ]) {
    const r=favorable();mutate(r);const decision=decide(r);
    assert.equal(decision.tone,'hold');
    assert.equal(decision.sources[1].state,'미확인');
  }
});

test('stale or missing article and official filing metadata prevent a candidate',()=>{
  for(const mutate of [
    r=>{r.news=[];},r=>{r.filings=[];},
    r=>{r.news[0].age_hours=80;},r=>{r.filings[0].age_days=121;},
    r=>{r.filings[0].source='뉴스';},
    r=>{r.analysis.evidence_checks.find(check=>check.name==='최근 72시간 내 뉴스').pass=false;},
    r=>{r.analysis.evidence_checks.find(check=>check.name==='최근 120일 내 공식 공시').pass=false;}
  ]) {
    const r=favorable();mutate(r);const decision=decide(r);
    assert.equal(decision.tone,'hold');
    assert.equal(decision.sources[2].state,'자료 부족');
  }
});

test('provider errors and title risk override otherwise favorable observations',()=>{
  for(const mutate of [r=>{r.financials_error='실패';},r=>{r.news_error='실패';},
    r=>{r.filings_error='실패';},r=>{r.analysis.red_flags=['회계부정 의혹 보도'];},
    r=>{r.analysis.evidence_checks.find(check=>check.name==='위험 키워드 기사 없음').pass=false;}]) {
    const r=favorable();mutate(r);assert.equal(decide(r).tone,'hold');
  }
});

test('operating and net losses produce concrete caution even with passing chart conditions',()=>{
  const r=favorable();r.financials[1].value=-3e12;r.financials[2].value=-1e12;
  const decision=decide(r);
  assert.equal(decision.tone,'wait');
  assert.match(decision.headline,/적자/);
  assert.match(decision.reasons[0],/2026년 반기 영업손실 3조원, 순손실 1조원/);
  assert.match(decision.nextStep,/적자 원인/);
});

test('missing chart conditions prevent a favorable observation and every output has source states',()=>{
  const r=favorable();r.analysis.conditions=r.analysis.conditions.filter(check=>check.id!=='rr');
  const decision=decide(r);assert.equal(decision.tone,'hold');
  assert.equal(decision.sources[0].state,'미확인');
  for(const item of [decide({}),decide(result()),decide(favorable()),decision]) {
    assert.equal(item.sources.map(source=>source.label).join(','),'차트,재무,기사·공시,리서치,미국 경제');
    assert.ok(item.reasons.length<=3);
  }
});

test('Naver actual financials are distinct from estimates and unverified accounting basis',()=>{
  const r=favorable();
  r.financials.forEach(f=>Object.assign(f,{source:'네이버증권 기업실적분석',basis_verified:true,is_estimate:false}));
  assert.equal(decide(r).tone,'watch');
  assert.ok(decide(r).reasons.some(reason=>reason.includes('네이버증권')));
  r.financials[0].basis_verified=false;
  assert.notEqual(decide(r).tone,'watch');
  r.financials[0].basis_verified=true;r.financials[0].is_estimate=true;
  assert.notEqual(decide(r).tone,'watch');
});

test('missing or stale macro eligibility blocks otherwise favorable observation',()=>{
  for(const mutate of [r=>{delete r.macro;},r=>{r.macro.eligible=false;r.macro.warnings=['오래된 자료'];}]) {
    const r=favorable();mutate(r);const decision=decide(r);
    assert.equal(decision.tone,'hold');
    assert.ok(decision.reasons.some(reason=>reason.includes('미국 금리·환율·물가·주요 주식 자료가 모두 확인되지 않아')));
    assert.equal(decision.sources.find(source=>source.label==='미국 경제').state,'자료 부족');
  }
});

test('each of all nine macro measures is mandatory even when trade flags are true',()=>{
  for(const id of ['rate','usdkrw','inflation','sp500','nasdaq','sox','nvda','aapl','msft']) {
    for(const verified of [false,true]) {
      const r=favorable();delete r.macro.metrics[id];
      r.analysis.confirmed=verified;r.data_quality.verified_for_trade=verified;
      const decision=decide(r);
      assert.equal(decision.tone,'hold',id);
      assert.ok(decision.reasons.some(reason=>reason.includes('미국 금리')));
    }
  }
});

test('macro values, changes, and provenance must be actual usable fields',()=>{
  for(const mutate of [r=>{r.macro.metrics.rate.value=NaN;},r=>{r.macro.metrics.rate.change_pp=null;},
    r=>{r.macro.metrics.inflation.change_pp=null;},r=>{r.macro.metrics.usdkrw.change_pct=null;},
    r=>{r.macro.metrics.nvda.asof='';},r=>{r.macro.metrics.aapl.source='';},
    r=>{r.macro.metrics.msft.url='javascript:alert(1)';}]) {
    const r=favorable();mutate(r);assert.equal(decide(r).tone,'hold');
  }
});

test('supplied macro risk stops positive observation and explains the actual flag',()=>{
  for(const flag of ['나스닥이 전일 대비 3.2% 하락했습니다.',{text:'원·달러 환율 변동 확대'}]) {
    const r=favorable();r.macro.risk_flags=[flag];const decision=decide(r);
    assert.equal(decision.tone,'wait');
    assert.match(decision.headline,/미국 경제·시장 주의 신호/);
    assert.ok(decision.reasons.some(reason=>reason.includes(typeof flag==='string'?flag:flag.text)));
    assert.equal(decision.sources.find(source=>source.label==='미국 경제').state,'주의');
  }
});

test('failed chart summary includes macro risk while retaining severe company risk priority',()=>{
  const r=favorable();r.analysis.conditions[0].pass=false;r.analysis.conditions[1].pass=false;
  r.macro.risk_flags=['미국 주요 주식의 하락폭 확대'];
  assert.equal(decide(r).tone,'wait');
  assert.match(decide(r).reasons[2],/미국 주요 주식의 하락폭 확대/);
  r.analysis.red_flags=['회계부정 수사 보도'];
  assert.match(decide(r).reasons[2],/뉴스·공시 제목에서 위험 문구/);
});

test('a failed chart with missing macro states the missing mandatory check',()=>{
  const r=favorable();r.analysis.conditions[0].pass=false;delete r.macro;
  const decision=decide(r);
  assert.equal(decision.tone,'wait');
  assert.ok(decision.reasons.some(reason=>reason.includes('미국 금리·환율·물가')));
});
