'use strict';

// A short reading of the supplied result; this does not fetch or invent market data.
function buildDecision(result) {
  const r=result||{}, a=r.analysis||{}, q=r.data_quality||{}, levels=a.levels||{};
  const conditions=Array.isArray(a.conditions)?a.conditions:[];
  const evidence=Array.isArray(a.evidence_checks)?a.evidence_checks:[];
  const blockers=Array.isArray(q.price_level_blockers)?q.price_level_blockers:[];
  const money=value=>Number(value).toLocaleString('ko-KR',{
    maximumFractionDigits:r.market==='US'?2:0,
    minimumFractionDigits:r.market==='US'?2:0
  })+(r.market==='US'?'달러':'원');
  const rangeValid=q.price_levels_eligible===true &&
    [levels.entry_low,levels.entry_high].every(Number.isFinite) &&
    levels.entry_low>0 && levels.entry_high>=levels.entry_low;
  const range=rangeValid?money(levels.entry_low)+' ~ '+money(levels.entry_high):'';
  const recentEnough=Number.isFinite(a.data_age_days) && a.data_age_days>=0 && a.data_age_days<=5 &&
    evidence.some(c=>c.name==='종가 데이터 최근 5일 이내' && c.pass===true);
  const financials=Array.isArray(r.financials)?r.financials:[];
  const facts=['매출액','영업이익','순이익'].map(name=>financials.filter(f=>f.name===name));
  const missingFinancials=Boolean(r.financials_error) || facts.some(matches=>matches.length!==1);
  const [revenue,operating,net]=facts.map(matches=>matches[0]);
  const expectedUnit=r.market==='US'?'USD':'KRW';
  const expectedSource=r.market==='US'?'SEC XBRL companyfacts':'OpenDART 주요 재무계정';
  // The server checks period recency and accounting basis. Check the values used
  // in this summary too, rather than treating an account name as a financial fact.
  const financialReady=q.financials_eligible===true && !missingFinancials &&
    [revenue,operating,net].every(f=>Number.isFinite(f.value) && f.unit===expectedUnit &&
      (f.source===expectedSource || (r.market!=='US' && f.source==='네이버증권 기업실적분석' && f.basis_verified===true && f.is_estimate===false)) && typeof f.period==='string' && f.period.trim() &&
      typeof f.basis==='string' && f.basis.trim()) &&
    new Set([revenue,operating,net].map(f=>f.period)).size===1 &&
    new Set([revenue,operating,net].map(f=>f.basis)).size===1;
  const losses=financialReady?[operating,net].filter(f=>f.value<0):[];
  const riskFlag=Array.isArray(a.red_flags) && a.red_flags.length>0;
  const evidencePass=name=>evidence.some(c=>c.name===name && c.pass===true);
  const newsReady=!r.news_error && evidencePass('최근 72시간 내 뉴스') &&
    Array.isArray(r.news) && r.news.some(n=>n.title && Number.isFinite(n.age_hours) && n.age_hours>=0 && n.age_hours<=72);
  const filingSource=r.market==='US'?'미국 SEC EDGAR':'금융감독원 DART';
  const filingsReady=!r.filings_error && evidencePass('최근 120일 내 공식 공시') &&
    Array.isArray(r.filings) && r.filings.some(f=>f.title && f.source===filingSource &&
      Number.isFinite(f.age_days) && f.age_days>=0 && f.age_days<=120);
  const externalReady=newsReady && filingsReady;
  const failed=conditions.filter(c=>c.pass!==true);
  const technicalReady=a.technical_ok===true && !failed.length &&
    ['trend','pullback','rsi','volatility','volume','rr'].every(id=>conditions.some(c=>c.id===id && c.pass===true));
  const macro=r.macro||{};
  const macroIds=['rate','usdkrw','inflation','sp500','nasdaq','sox','nvda','aapl','msft'];
  const macroMetrics=macro.metrics && typeof macro.metrics==='object' && !Array.isArray(macro.metrics)?macro.metrics:{};
  const macroRisk=Array.isArray(macro.risk_flags) && macro.risk_flags.length>0;
  // Recency and risk thresholds belong to the source audit. The summary still
  // requires every requested measure and its provenance before a favorable view.
  const macroReady=macro.eligible===true && Array.isArray(macro.risk_flags) && macroIds.every(id=>{
    const metric=macroMetrics[id];
    const change=id==='rate' || id==='inflation'?'change_pp':'change_pct';
    return metric && Number.isFinite(metric.value) && Number.isFinite(metric[change]) &&
      ['unit','asof','source','url'].every(key=>typeof metric[key]==='string' && metric[key].trim()) &&
      /^https?:\/\//.test(metric.url);
  });
  function macroReason() {
    if(macroRisk) {
      const flag=macro.risk_flags[0];
      const detail=typeof flag==='string'?flag:flag?.text||flag?.detail||flag?.reason||flag?.message||flag?.label||'';
      return detail?'미국 경제·시장에 주의 신호가 있습니다: '+detail:'미국 경제·시장에 주의 신호가 있습니다.';
    }
    return !macroReady?'미국 금리·환율·물가·주요 주식 자료가 모두 확인되지 않아 매수 판단을 보류합니다.':null;
  }
  const sources=[
    {label:'차트',state:!rangeValid || !recentEnough?'미확인':failed.length?'조건 미충족':technicalReady?'확인':'미확인'},
    {label:'재무',state:financialReady?'확인':'미확인'},
    {label:'기사·공시',state:externalReady?'제목 확인':'자료 부족'},
    {label:'리서치',state:r.research_error?'자료 부족':(r.research||[]).some(item=>item.type==='research_report')?'목록 확인':(r.research||[]).length?'관련 보도':'자료 없음'},
    {label:'미국 경제',state:macroRisk?'주의':macroReady?'확인':'자료 부족'}
  ];
  const compactMoney=value=>{
    const absolute=Math.abs(value);
    const unit=r.market==='US'?(absolute>=1e9?[1e9,'십억 달러']:absolute>=1e6?[1e6,'백만 달러']:null):
      absolute>=1e12?[1e12,'조원']:absolute>=1e8?[1e8,'억원']:null;
    return unit?(value/unit[0]).toLocaleString('ko-KR',{maximumFractionDigits:2})+unit[1]:money(value);
  };
  const lossReason=()=>losses.length?
    revenue.period+' '+losses.map(f=>(f.name==='영업이익'?'영업손실 ':'순손실 ')+compactMoney(-f.value)).join(', ')+'이 확인됩니다.':'';

  function evidenceReason() {
    if(riskFlag)return '뉴스·공시 제목에서 위험 문구가 발견돼 원문 확인이 필요합니다.';
    if(losses.length)return lossReason();
    if(missingFinancials)return '기업 실적을 확인할 재무자료가 부족합니다.';
    if(!financialReady)return '실적 자료의 수치·출처·기간·통화·작성 기준을 확인해야 합니다.';
    if(!newsReady)
      return '최근 뉴스를 충분히 확인하지 못했습니다.';
    if(!filingsReady)
      return '최근 공식 공시를 충분히 확인하지 못했습니다.';
    return '기업가치와 뉴스·공시 원문 확인이 끝나지 않았습니다.';
  }

  function contextReason() {
    if(riskFlag || losses.length)return evidenceReason();
    return macroReason()||evidenceReason();
  }

  function nextStep() {
    if(!rangeValid || !Number.isFinite(a.close))
      return '최신 종가와 가격 출처를 확인한 뒤 다시 분석해 주세요.';
    if(a.close>levels.entry_high)
      return range+' 구간으로 내려오는지 살펴보고, 주가 흐름과 실적·공시를 다시 확인하세요.';
    if(a.close<levels.entry_low)
      return range+' 구간으로 회복하는지 살펴보고, 하락 이유와 실적·공시를 다시 확인하세요.';
    return '최근 종가는 '+range+' 구간 안에 있습니다. 주가 흐름과 실적·공시 확인이 먼저입니다.';
  }

  function blockerReason(value) {
    const text=String(value);
    if(/종가|기준일/.test(text))return '최신 확정 종가를 확인하지 못했습니다.';
    if(/출처|네이버|일봉|시장 구분/.test(text))return '가격 데이터의 출처를 확인하지 못했습니다.';
    if(/관측/.test(text))return '주가 흐름을 판단할 과거 가격자료가 부족합니다.';
    return '가격대와 손실·이익 계산을 검증하지 못했습니다.';
  }

  if(!rangeValid || !recentEnough) {
    const reasons=recentEnough?blockers.slice(0,2).map(blockerReason):['최신 확정 종가를 확인하지 못했습니다.'];
    if(!reasons.length)reasons.push('신뢰할 수 있는 가격자료가 아직 준비되지 않았습니다.');
    reasons.push('자료가 부족하므로 지금이 매수 시점이라고 판단할 수 없습니다.');
    return {headline:'현재 자료로는 매수 시점을 판단하기 어렵습니다',
      reasons:[...new Set(reasons)].slice(0,3),
      nextStep:'최신 종가와 가격 출처를 확인한 뒤 다시 분석해 주세요.',tone:'hold',sources};
  }

  function failedReason(condition) {
    const indicators=a.indicators||{};
    switch(condition.id) {
      case 'trend':return '주가가 안정적인 상승 흐름이라는 조건을 충족하지 못했습니다.';
      case 'pullback':return a.close>levels.entry_high?
        '최근 종가가 관찰 가격대('+range+')보다 높습니다.':
        '최근 종가가 관찰 가격대('+range+')보다 낮아 추가 하락 여부를 확인해야 합니다.';
      case 'rr':return Number.isFinite(levels.rr)?
        '예상 이익이 감수할 손실의 '+levels.rr.toFixed(2)+'배로, 기준인 2배에 못 미칩니다.':
        '예상 이익이 감수할 손실에 비해 충분하지 않습니다.';
      case 'rsi':return Number.isFinite(indicators.rsi14) && indicators.rsi14>62?
        '최근 주가 상승이 가팔라 가격 부담을 확인해야 합니다.':
        '최근 주가 흐름이 약해 회복을 확인해야 합니다.';
      case 'volatility':return '하루 가격 변동이 커 손실 위험이 높습니다.';
      case 'volume':return '거래량이 평소 범위를 벗어나 추가 확인이 필요합니다.';
      default:return '매수 검토 조건 중 일부를 충족하지 못했습니다.';
    }
  }

  if(failed.length) {
    return {headline:'지금은 매수를 서두르기 어렵습니다',
      reasons:[...failed.slice(0,2).map(failedReason),contextReason()],
      nextStep:nextStep(),tone:'wait',sources};
  }
  if(!technicalReady) {
    return {headline:'현재 자료로는 매수 시점을 판단하기 어렵습니다',
      reasons:['주가 흐름과 가격 조건의 확인이 끝나지 않았습니다.',contextReason()],
      nextStep:nextStep(),tone:'hold',sources};
  }
  if(losses.length) {
    return {headline:'실적 적자로 매수는 신중히 볼 시점입니다',
      reasons:[lossReason(),'주가 조건을 충족해도 실적 회복과 적자 원인 확인이 필요합니다.',
        riskFlag?'뉴스·공시 제목에서 발견된 위험 문구도 원문으로 확인해야 합니다.':macroReason()||'흑자 전환 가능성이나 기업가치는 이 수치만으로 판단할 수 없습니다.'],
      nextStep:'해당 실적의 공시 원문에서 적자 원인과 회복 가능성을 확인한 뒤 다시 검토하세요.',tone:'wait',sources};
  }
  if(macroRisk) {
    return {headline:'미국 경제·시장 주의 신호로 매수는 신중히 볼 시점입니다',
      reasons:[macroReason(),'주가 조건만으로 미국 경제·시장의 주의 신호를 상쇄할 수는 없습니다.',
        !macroReady?'일부 미국 경제·시장 자료도 확인되지 않았습니다.':evidenceReason()],
      nextStep:'미국 금리·환율·물가·주요 주식의 변화를 확인한 뒤 다시 검토하세요.',tone:'wait',sources};
  }
  if(!macroReady) {
    return {headline:'기술적 조건은 맞지만, 매수 판단은 보류입니다',
      reasons:['주가 흐름과 가격은 앱의 관찰 조건을 충족했습니다.',macroReason(),evidenceReason()],
      nextStep:'미국 금리·환율·물가·주요 주식의 최신 자료를 확인한 뒤 다시 검토하세요.',tone:'hold',sources};
  }
  if(q.verified_for_trade===true && a.confirmed===true && externalReady && macroReady &&
      evidence.every(c=>c.pass===true) && !riskFlag && financialReady) {
    return {headline:'매수 검토 조건을 충족했습니다',
      reasons:['주가 흐름과 가격 조건을 충족했습니다.','재무자료와 외부 근거의 검증 조건을 충족했습니다.'],
      nextStep:'실시간 가격과 본인의 손실 감수 범위를 확인한 뒤 매수를 검토하세요.',tone:'review',sources};
  }
  if(financialReady && externalReady && !riskFlag && macroReady &&
      evidencePass('위험 키워드 기사 없음') && evidence.every(c=>c.pass===true)) {
    return {headline:'매수 관찰 후보입니다',
      reasons:['주가 조건을 충족했고 미국 금리·환율·물가·주요 주식 자료를 확인했습니다.',
        revenue.source+' · '+revenue.period+' 매출 '+compactMoney(revenue.value)+', 영업이익 '+compactMoney(operating.value)+'입니다.',
        '최근 기사·공시는 제목과 발행 시점만 확인했으며, 원문 확인이 필요합니다.'],
      nextStep:range+' 구간을 관찰하면서 실시간 가격과 공시·기사 원문을 확인한 뒤 검토하세요.',tone:'watch',sources};
  }
  return {headline:'기술적 조건은 맞지만, 매수 판단은 보류입니다',
    reasons:['주가 흐름과 가격은 앱의 관찰 조건을 충족했습니다.',evidenceReason(),
      '가격 조건만으로 지금 매수를 권할 근거는 충분하지 않습니다.'],
    nextStep:nextStep(),tone:'hold',sources};
}
