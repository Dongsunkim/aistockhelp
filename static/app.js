'use strict';
const el=id=>document.getElementById(id);
const escapeHtml=(t)=>String(t??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl=(s)=>{try{const u=new URL(s);return ['https:','http:'].includes(u.protocol)?u.href:'#'}catch{return '#'}};
const nf=(x,market)=>x==null?'—':Number(x).toLocaleString('ko-KR',{minimumFractionDigits:market==='US'?2:0,maximumFractionDigits:market==='US'?2:0});
let current=null;
let analysisRequest=0;
let analysisController=null;
let cloudMode=false;
const LOCAL_WATCH_KEY='signaldesk.v03.watchlist';
function localWatch(path, method='GET', body) {
  const entries=JSON.parse(localStorage.getItem(LOCAL_WATCH_KEY)||'[]');
  if (path==='/api/watchlist' && method==='GET') return entries;
  if (path==='/api/watchlist' && method==='POST') {
    const id=Date.now()+Math.floor(Math.random()*1000);
    const rest=entries.filter(x=>!(x.symbol===body.symbol && x.market===body.market));
    rest.unshift({...body,id,created_at:new Date().toISOString()});
    localStorage.setItem(LOCAL_WATCH_KEY,JSON.stringify(rest));return {saved:true};
  }
  const m=path.match(/^\/api\/watchlist\/(\d+)$/);
  if(m && method==='DELETE') {
    localStorage.setItem(LOCAL_WATCH_KEY,JSON.stringify(entries.filter(x=>x.id!==Number(m[1]))));
    return {deleted:true};
  }
  throw Error('지원하지 않는 로컬 저장 요청입니다.');
}
const marketName=x=>({'KOSPI':'KOSPI','KOSDAQ':'KOSDAQ','US':'US EQUITIES'}[x]||x);
const currency=x=>x==='US'?'USD':'KRW';
function say(message,error=false){const m=el('app-message');m.textContent=message;m.hidden=false;m.style.background=error?'#fff4ef':'#eef5fd';m.style.color=error?'#a04d3d':'#376080';m.scrollIntoView({behavior:'smooth',block:'nearest'});}
function clearMessage(){el('app-message').hidden=true;}
async function api(path,method='GET',body,signal){if(cloudMode && path.startsWith('/api/watchlist'))return localWatch(path,method,body);const headers={'Content-Type':'application/json'};const rsp=await fetch(path,{method,headers,signal,body:body?JSON.stringify(body):undefined});let payload;try{payload=await rsp.json()}catch{throw new Error('서버 응답을 읽지 못했습니다.')}if(!rsp.ok)throw new Error(payload.detail||`서버 오류 ${rsp.status}`);return payload;}
async function health(){try{const r=await api('/api/health');const c=r.configured;cloudMode=!!r.serverless;el('watch-help').textContent=cloudMode?'관심 종목은 현재 브라우저에만 저장됩니다. 자동 알림은 아직 연결되지 않았습니다.':'관심 종목과 관찰 가격을 저장할 수 있습니다. 자동 알림은 아직 연결되지 않았습니다.';el('setup-state').textContent=`연동 상태 · DART ${c.dart?'설정됨':'키 필요'} / KIS 시세 ${c.kis_quote?'설정됨':'미연결'} / SEC ${c.sec?'설정됨':'식별 정보 필요'} / AI 요약 ${c.ai?'로컬 전용':'공개 앱 비활성'} / 텔레그램 ${c.telegram?'설정됨':'설정 필요'} · 기술분석은 확정 일봉이며 추가 REST 시세는 최신성 검증이 필요합니다.`;}catch(e){el('setup-state').textContent='연동 상태를 확인할 수 없습니다.';}}
function setSelection(market,symbol,name){el('market').value=market;el('symbol').value=symbol;el('company-name').value=name;}
const ANALYSIS_SOURCES={news:'기사',research:'리서치',filings:'공시',financials:'재무',quote:'현재가',macro:'미국 경제'};
const DECISION_SOURCES=['news','filings','financials','macro'];
const ANALYSIS_TIMEOUT_MS=22000;
async function streamAnalysis(body,signal,onUpdate){
  const response=await fetch('/api/analyze/stream',{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/x-ndjson'},body:JSON.stringify(body),signal});
  if(signal.aborted){const error=new Error('분석 요청이 변경되었습니다.');error.name='AbortError';throw error;}
  if(response.status===404 || response.status===405){
    const result=await api('/api/analyze','POST',body,signal);
    onUpdate({stage:'complete',result,pending_sources:[]});
    return;
  }
  if(!response.ok){
    let detail;try{detail=(await response.json()).detail;}catch{}
    throw new Error(typeof detail==='string'?detail:`분석 서버에 연결하지 못했습니다. (${response.status})`);
  }
  if(!response.body?.getReader)throw new Error('분석 연결을 읽지 못했습니다. 다시 분석해 주세요.');
  const reader=response.body.getReader(),decoder=new TextDecoder('utf-8',{fatal:true});
  const cancelReader=()=>{try{Promise.resolve(reader.cancel()).catch(()=>{});}catch{}};
  signal.addEventListener('abort',cancelReader,{once:true});
  let buffer='',complete=false;
  const consume=line=>{
    if(!line.trim())return;
    let update;try{update=JSON.parse(line);}catch{throw new Error('분석 응답을 읽지 못했습니다. 다시 분석해 주세요.');}
    if(update?.stage==='error')throw new Error(typeof update.detail==='string'?update.detail:'분석을 완료하지 못했습니다. 다시 분석해 주세요.');
    if(!update || !['partial','complete'].includes(update.stage) || !update.result || !Array.isArray(update.pending_sources) || update.pending_sources.some(source=>!Object.hasOwn(ANALYSIS_SOURCES,source)) || (update.stage==='complete' && update.pending_sources.length))throw new Error('분석 응답 형식이 올바르지 않습니다. 다시 분석해 주세요.');
    onUpdate(update);
    complete=update.stage==='complete';
  };
  try{
    while(!complete){
      const chunk=await reader.read();
      if(signal.aborted){const error=new Error('분석 요청이 변경되었습니다.');error.name='AbortError';throw error;}
      buffer+=decoder.decode(chunk.value,{stream:!chunk.done});
      let newline;
      while(!complete && (newline=buffer.indexOf('\n'))!==-1){const line=buffer.slice(0,newline);buffer=buffer.slice(newline+1);consume(line);}
      if(chunk.done){if(!complete && buffer.trim())consume(buffer);break;}
    }
    if(!complete)throw new Error('분석 연결이 끊겼습니다. 다시 분석해 주세요.');
  }finally{
    signal.removeEventListener('abort',cancelReader);
    cancelReader();
    try{reader.releaseLock();}catch{}
  }
}
async function runAnalyze(){
  clearMessage();
  const market=el('market').value,symbol=el('symbol').value.trim().toUpperCase(),name=el('company-name').value.trim();
  if(!symbol){say('종목코드를 입력하세요.',true);return;}
  const request=++analysisRequest;
  analysisController?.abort();
  const controller=analysisController=new AbortController();
  current=null;
  if(typeof resetStockQA==='function')resetStockQA(null);
  el('backtest-result').textContent='검증을 실행하면 과거 거래 건수·승률·수익률과 가정을 확인할 수 있습니다.';
  el('backtest-button').disabled=true;
  el('backtest-button').textContent='최근 3년 기술적 전략 검증';
  el('result-area').hidden=true;
  el('analysis-failure').hidden=true;
  el('analysis-completion').hidden=true;
  el('empty-state').hidden=true;
  el('loading-state').hidden=false;
  el('loading-name').textContent=(name||symbol)+' 분석 중';
  el('loading-detail').textContent='확정 일봉을 먼저 계산하고, 재무·기사·공시·미국 경제를 함께 확인합니다.';
  el('analysis-progress').hidden=true;
  el('analysis-details').open=false;
  el('result-area').setAttribute('aria-busy','true');
  const btn=el('analyze-btn');btn.disabled=true;btn.textContent='분석 중…';
  let displayed=false,completed=false,lastResult=null,pendingSources=[],timeoutId,abortHandler;
  const interrupted=new Promise((resolve,reject)=>{
    abortHandler=()=>{const error=new Error('분석 요청이 변경되었습니다.');error.name='AbortError';reject(error);};
    controller.signal.addEventListener('abort',abortHandler,{once:true});
    timeoutId=setTimeout(()=>{
      const error=new Error('서버 응답 시간이 초과되었습니다. 잠시 뒤 다시 분석해 주세요.');
      error.name='AnalysisTimeoutError';
      reject(error);
      controller.abort();
    },ANALYSIS_TIMEOUT_MS);
  });
  try{
    const task=streamAnalysis({market,symbol,name,with_ai:false},controller.signal,update=>{
      if(request!==analysisRequest || controller.signal.aborted)return;
      const r=update.result;
      if(r.symbol!==symbol || r.market!==market)throw new Error('요청한 종목과 분석 응답이 다릅니다. 다시 분석해 주세요.');
      const partial=update.stage==='partial';
      drawResult(r,{initializeQA:false,pendingSources:update.pending_sources,partial});
      lastResult=r;pendingSources=update.pending_sources;
      if(!partial){
        current=r;completed=true;
        if(typeof resetStockQA==='function')resetStockQA(r);
        el('backtest-button').disabled=false;
      }
      el('loading-state').hidden=true;
      el('result-area').hidden=false;
      if(!displayed){el('result-area').scrollIntoView({behavior:'smooth',block:'start'});displayed=true;}
    });
    await Promise.race([task,interrupted]);
  }catch(e){
    if(request!==analysisRequest)return;
    const detail=['TypeError','AbortError'].includes(e.name)?'분석 연결이 중단되었습니다. 다시 분석해 주세요.':e.message;
    if(displayed && !completed){
      drawDecision(lastResult);
      drawPendingSources(pendingSources,true);
      el('decision-headline').textContent='필수 자료 확인 불가로 매수 판단 보류';
      el('decision-label').textContent='판단 보류';
      el('decision-card').dataset.tone='hold';
      el('decision-reasons').innerHTML='<li>확인된 확정 일봉의 차트와 참고 가격대는 아래에서 볼 수 있습니다.</li><li>분석 연결이 중단되어 종합 판단에 필요한 자료 확인을 마치지 못했습니다.</li>';
      el('decision-next').textContent='자료가 부족해 지금 매수를 권할 근거가 충분하지 않습니다. 다시 분석해 주세요.';
      el('signal-status').textContent='필수 자료 확인 불가 · 매수 판단 보류';
      el('signal-status').className='status-pill hold';
      el('quality-status').textContent='필수 자료 확인 불가 · 매수 판단 보류';
      el('analysis-progress').hidden=true;
      el('analysis-completion').hidden=false;
      el('analysis-completion').dataset.state='error';
      el('analysis-completion').textContent=detail+(pendingSources.length?' 확인하지 못한 자료: '+pendingSources.map(source=>ANALYSIS_SOURCES[source]).join('·')+'.':'')+' 연결이 중단되어 AI 질문은 열리지 않았습니다.';
    }else if(!completed){
      el('empty-state').hidden=true;
      el('analysis-failure-detail').textContent=(name||symbol)+' · '+detail;
      el('analysis-failure').hidden=false;
      el('analysis-failure').scrollIntoView({behavior:'smooth',block:'nearest'});
    }
    if(!completed)say(detail,true);
  }finally{
    clearTimeout(timeoutId);
    controller.signal.removeEventListener('abort',abortHandler);
    if(request===analysisRequest){
      el('loading-state').hidden=true;
      el('result-area').setAttribute('aria-busy','false');
      btn.disabled=false;btn.innerHTML='매수 시점 확인 <span>↗</span>';
      if(analysisController===controller)analysisController=null;
    }
  }
}
function showChecks(id,list){el(id).innerHTML=list.map(c=>`<div class="check-item"><div class="check-icon ${c.pass?'yes':''}">${c.pass?'✓':'!'}</div><div><div class="check-name">${escapeHtml(c.name)}</div><div class="check-detail">${escapeHtml(c.detail)}</div></div></div>`).join('');}
function sourcesList(id,items,error){el(id).innerHTML=error?`<div class="source-empty">${escapeHtml(error)}</div>`:items.length?items.map((n,i)=>`<a class="source-item" href="${escapeHtml(safeUrl(n.link))}" target="_blank" rel="noopener noreferrer"><div class="source-title">${escapeHtml(n.title)} ↗</div><div class="source-meta">[${id==='news-list'?'N':id==='research-list'?'R':'F'}${i+1}] ${escapeHtml(n.source)}${n.broker?' · '+escapeHtml(n.broker):''} · ${escapeHtml(n.date||n.published_at||'발행일 미상')}</div><div class="source-meta">${escapeHtml(n.fallback_reason||n.verification||n.basis||'')}</div></a>`).join(''):'<div class="source-empty">현재 표시할 근거가 없습니다. 자료가 없다는 사실은 안전하다는 뜻이 아닙니다.</div>';}
function drawChart(points,levels,market){const svg=el('price-chart');const W=750,H=255,L=14,R=63,T=19,B=27;const values=points.map(p=>p.close).filter(Number.isFinite);if(levels)values.push(levels.entry_low,levels.entry_high,levels.stop,levels.target_resistance);let lo=Math.min(...values),hi=Math.max(...values);const pad=Math.max((hi-lo)*.09,1);lo-=pad;hi+=pad;const x=i=>L+i/(Math.max(points.length-1,1))*(W-L-R);const y=v=>T+(hi-v)/(hi-lo)*(H-T-B);const grid=[0,.25,.5,.75,1].map(t=>{const val=lo+(hi-lo)*t;return `<line x1="${L}" x2="${W-R+4}" y1="${y(val)}" y2="${y(val)}" stroke="#e5edf3" stroke-dasharray="4,5"/><text x="${W-R+10}" y="${y(val)+4}" font-size="11" fill="#90a0ac">${escapeHtml(nf(val,market))}</text>`}).join('');const line=points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join(' ');const zoneTop=levels?y(levels.entry_high):0,zoneBottom=levels?y(levels.entry_low):0;const start=points[0]?.date||'',end=points.at(-1)?.date||'';svg.innerHTML=`${grid}${levels?`<rect x="${L}" y="${zoneTop}" width="${W-L-R}" height="${Math.max(1,zoneBottom-zoneTop)}" fill="#d8eafa" opacity=".8"/><line x1="${L}" x2="${W-R}" y1="${y(levels.stop)}" y2="${y(levels.stop)}" stroke="#d57e7e" stroke-width="1.5" stroke-dasharray="5,5"/>`:''}<path d="${line}" fill="none" stroke="#173e5b" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/><circle cx="${x(points.length-1)}" cy="${y(points.at(-1).close)}" r="4.5" fill="#266db5" stroke="white" stroke-width="2"/><text x="${L}" y="${H-5}" font-size="11" fill="#95a2ae">${escapeHtml(start)}</text><text x="${W-R}" text-anchor="end" y="${H-5}" font-size="11" fill="#95a2ae">${escapeHtml(end)}</text>`;}
function drawDecision(r,pendingSources=[],partial=false){
  const decision=buildDecision(r);
  const decisionReady=!DECISION_SOURCES.some(source=>pendingSources.includes(source));
  el('decision-company').textContent=(r.company_name||r.symbol)+(r.company_name && r.company_name!==r.symbol?' ('+r.symbol+')':'')+' · 지금 매수해도 될까요?';
  el('decision-headline').textContent=decision.headline;
  el('decision-card').dataset.tone=decision.tone;
  el('decision-label').textContent=({wait:'관망',hold:'판단 보류',watch:'매수 관찰',review:'매수 검토'}[decision.tone]||'판단 보류');
  el('decision-reasons').innerHTML=decision.reasons.map(reason=>'<li>'+escapeHtml(reason)+'</li>').join('');
  el('decision-next').textContent=decision.nextStep;
  el('decision-sources').innerHTML=(decision.sources||[]).map(source=>`<span>${escapeHtml(source.label)} <strong>${escapeHtml(source.state)}</strong></span>`).join('');
  if(partial){
    const sourceKeys={'재무':['financials'],'기사·공시':['news','filings'],'리서치':['research'],'미국 경제':['macro']};
    el('decision-sources').innerHTML=(decision.sources||[]).map(source=>`<span>${escapeHtml(source.label)} <strong>${escapeHtml((sourceKeys[source.label]||[]).some(key=>pendingSources.includes(key))?'확인 중':source.state)}</strong></span>`).join('');
    if(!decisionReady){
      el('decision-headline').textContent='차트 계산 완료 · 종합 판단 확인 중';
      el('decision-label').textContent='확인 중';
      el('decision-card').dataset.tone='hold';
      el('decision-reasons').innerHTML='<li>확정 일봉으로 차트와 참고 가격대를 계산했습니다.</li><li>재무·기사·공시·미국 경제 확인이 끝나면 종합 판단을 표시합니다.</li>';
      el('decision-next').textContent='확인 중인 자료가 있습니다. 차트 계산만으로 매수 시점을 판단하지 않습니다.';
      el('signal-status').textContent='종합 판단 확인 중';
      el('signal-status').className='status-pill hold';
    }
  }
  drawCompletion(r,partial);
  const progress=el('analysis-progress');
  progress.hidden=!partial;
  progress.dataset.state='loading';
  if(partial){
    const pending=[...new Set(pendingSources)],done=Object.keys(ANALYSIS_SOURCES).length-pending.length;
    progress.textContent=(decisionReady?'핵심 자료 점검 완료 · ':'차트 계산 완료 · ')+`자료 점검 ${done}/6 완료`+(pending.length?' · '+(decisionReady?'추가 ':'')+pending.map(source=>ANALYSIS_SOURCES[source]).join('·')+' 확인 중':' · 결과 정리 중')+' · AI 질문은 결과 정리 후 열립니다.';
  }
  el('decision-source-link').textContent=r.market==='US'?'Yahoo 시세 출처 확인 ↗':'네이버 증권에서 확인 ↗';
  el('decision-source-link').href=r.market==='US'?'https://finance.yahoo.com/quote/'+encodeURIComponent(r.symbol):'https://finance.naver.com/item/main.naver?code='+encodeURIComponent(r.symbol);
  el('decision-asof').textContent=`${r.analysis.date} 확정 종가 ${nf(r.analysis.close,r.market)} ${currency(r.market)} 기준 · 실시간 판단이 아닙니다.`;
  if(r.cache?.hit===true)el('decision-asof').textContent=`${Math.max(0,Math.floor(Number(r.cache.age_seconds)||0))}초 전 조회 자료 · 기준시각 ${new Date(r.meta.fetched_at).toLocaleString('ko-KR')} · `+el('decision-asof').textContent;
}
function drawCompletion(r,partial=false){
  const completion=r.completion||{},notice=el('analysis-completion');
  const missing=[...new Set([...(completion.timed_out_sources||[]),...(completion.unavailable_sources||[])])].filter(source=>Object.hasOwn(ANALYSIS_SOURCES,source));
  notice.hidden=partial || (completion.status!=='partial' && !missing.length);
  notice.dataset.state='partial';
  if(!notice.hidden)notice.textContent='확인된 자료로 분석을 마쳤습니다. '+(completion.message||'일부 자료를 확인하지 못해 판단에 제한이 있습니다.')+(missing.length?' 확인하지 못한 자료: '+missing.map(source=>ANALYSIS_SOURCES[source]).join('·')+'.':'')+' 누락된 자료와 판단 근거를 AI에 질문할 수 있습니다.';
}
function drawPendingSources(pendingSources,interrupted=false){
  for(const [source,id] of Object.entries({news:'news-list',filings:'filings-list',research:'research-list',financials:'financials-list'})){
    if(pendingSources.includes(source))el(id).innerHTML=`<div class="source-empty">${escapeHtml(ANALYSIS_SOURCES[source])} 자료 ${interrupted?'확인 불가 · 연결이 중단되었습니다.':'확인 중…'}</div>`;
  }
  if(pendingSources.includes('macro')){
    el('macro-status').textContent=interrupted?'확인 불가':'확인 중';
    el('macro-summary').textContent=interrupted?'연결이 중단되어 미국 금리·환율·물가·주요 주식 자료를 모두 확인하지 못했습니다.':'미국 금리·환율·물가·주요 주식 자료를 확인하고 있습니다.';
    if(interrupted)el('macro-warnings').innerHTML='<li>미국 경제·시장 필수 자료 조회가 중단되었습니다.</li>';
  }
  if(pendingSources.includes('quote')){
    el('quote-value').textContent=interrupted?'현재가 확인 불가':'현재가 확인 중';
    el('quote-info').textContent=interrupted?'연결이 중단되어 현재가를 확인하지 못했습니다.':'현재가는 추가 참고 자료입니다. 차트는 확정 일봉으로 계산합니다.';
  }
  if(DECISION_SOURCES.some(source=>pendingSources.includes(source)))el('quality-status').textContent='종합 자료 확인 중';
}
function drawMacro(macro){
  const m=macro||{},metrics=m.metrics||{};
  const labels={rate:'미국 실효금리',usdkrw:'원/달러 환율',inflation:'미국 소비자물가 상승률',sp500:'S&P500',nasdaq:'나스닥',sox:'필라델피아 반도체',nvda:'엔비디아',aapl:'애플',msft:'마이크로소프트'};
  el('macro-status').textContent=m.eligible===true?(m.risk_flags?.length?'주의 요인 있음':'자료 확인'):'자료 부족';
  el('macro-summary').textContent=m.summary||'미국 경제 자료를 확인하지 못했습니다. 이 상태에서는 매수 관찰 후보로 표시하지 않습니다.';
  el('macro-metrics').innerHTML=Object.entries(labels).map(([id,label])=>{
    const metric=metrics[id];
    if(!metric || !Number.isFinite(metric.value))return `<div class="macro-metric"><strong>${escapeHtml(label)}</strong><p>미확인</p></div>`;
    const delta=id==='rate'||id==='inflation'?metric.change_pp:metric.change_pct;
    const change=Number.isFinite(delta)?`${delta>0?'+':''}${nf(delta,'US')}${id==='rate'||id==='inflation'?'%p':'%'} (직전 관측 대비)`:'이전 관측 미확인';
    return `<div class="macro-metric"><a href="${escapeHtml(safeUrl(metric.url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)} ↗</a><p>${escapeHtml(nf(metric.value,'US'))} ${escapeHtml(metric.unit)}</p><small>${escapeHtml(change)}<br/>${escapeHtml(metric.asof)}${id==='inflation'?' 관측월':' 기준'} · ${escapeHtml(metric.source)}</small></div>`;
  }).join('');
  el('macro-warnings').innerHTML=[...(m.risk_flags||[]),...(m.warnings||[])].map(w=>'<li>'+escapeHtml(w)+'</li>').join('');
}
function drawPriceLevels(levels,market,quality){
  const eligible=quality.price_levels_eligible===true;
  el('entry-zone').textContent=eligible?nf(levels.entry_low,market)+' ~ '+nf(levels.entry_high,market)+' '+currency(market):'유효한 가격대 없음';
  el('price-level-status').textContent=eligible?'참고용 기술적 계산값':'가격 데이터 확인 필요';
  el('entry-caption').textContent=eligible
    ?'확정 일봉으로 계산한 참고 구간입니다. 재무·원문 검증은 별도로 필요하며, 구간 도달만으로 매수하지 않습니다.'
    :(quality.price_level_blockers?.length?quality.price_level_blockers.join(' '):'가격 데이터와 산식을 확인할 수 없어 구간을 표시하지 않습니다.');
  el('stop-price').textContent=eligible?nf(levels.stop,market):'산출 불가';
  el('target-price').textContent=eligible?nf(levels.target_resistance,market):'산출 불가';
  el('rr-value').textContent=eligible?'1 : '+nf(levels.rr,'US'):'산출 불가';
  el('zone-legend').hidden=!eligible;
  el('stop-legend').hidden=!eligible;
  return eligible?levels:null;
}
function drawResult(r,{initializeQA=true,pendingSources=[],partial=false}={}){const a=r.analysis,levels=a.levels,m=r.market;el('result-market').textContent=marketName(m)+' · '+r.symbol;el('result-name').textContent=r.company_name||r.symbol;el('result-date').textContent=`최종 확정 일봉 ${a.date} · ${r.cache?.hit===true?Math.max(0,Math.floor(Number(r.cache.age_seconds)||0))+'초 전 조회 자료 · 기준시각':'조회'} ${new Date(r.meta.fetched_at).toLocaleString('ko-KR')} · ${r.meta.provider}`;el('close-price').textContent=nf(a.close,m)+' '+currency(m);el('signal-status').textContent=a.status;el('signal-status').className='status-pill '+(a.confirmed?'ok':a.status.includes('보류')||a.status.includes('확인')?'hold':'no');const q=r.data_quality||{};el('quality-status').textContent=q.status||'검증 상태 미확인';el('quality-warnings').innerHTML=(q.warnings||['품질 상태 미확인']).map(v=>'<li>'+escapeHtml(v)+'</li>').join('');const chartLevels=drawPriceLevels(levels,m,q);drawDecision(r,pendingSources,partial);drawMacro(r.macro);if(initializeQA && typeof resetStockQA==='function')resetStockQA(r);el('pass-count').textContent=a.conditions.filter(x=>x.pass).length+' / '+a.conditions.length+'개 충족';showChecks('condition-list',a.conditions);showChecks('evidence-check-list',a.evidence_checks);el('formula-list').innerHTML=Object.entries(a.method).filter(([k])=>k!=='caveat').map(([k,v])=>`<div class="method-item"><strong>${escapeHtml({'entry':'매수가 범위','stop':'손절 기준','target':'목표/저항','risk_reward':'손익비 산식'}[k]||k)}</strong><div>${escapeHtml(v)}</div></div>`).join('');el('formula-caveat').textContent=a.method.caveat;const red=el('red-flags');red.hidden=!a.red_flags.length;red.textContent=a.red_flags.length?`주의: 최근 기사 제목에서 위험 키워드 ${a.red_flags.length}건 감지 · ${a.red_flags.slice(0,2).join(' / ')} · 문맥 확인 전 판단 보류`:' ';sourcesList('news-list',r.news,r.news_error);sourcesList('filings-list',r.filings,r.filings_error);sourcesList('research-list',r.research||[],r.research_error);const fitems=r.financials||[];el('financials-list').innerHTML=fitems.length?fitems.map(f=>`<a class="source-item" href="${escapeHtml(safeUrl(f.link))}" target="_blank" rel="noopener noreferrer"><div class="source-title">${escapeHtml(f.name)} · ${escapeHtml(Number(f.value).toLocaleString('ko-KR'))} ${escapeHtml(f.unit)}</div><div class="source-meta">${escapeHtml(f.period)} · ${escapeHtml(f.source)} · ${escapeHtml(f.basis)}</div></a>`).join(''):`<div class="source-empty">${escapeHtml(r.financials_error||'적합한 재무계정이 조회되지 않았습니다.')}</div>`;el('quote-value').textContent=r.quote?nf(r.quote.price,m)+' '+currency(m):'연결된 현재가 없음';
  el('quote-info').textContent=r.quote?`${r.quote.provider} · 조회 ${new Date(r.quote.fetched_at).toLocaleString('ko-KR')} · ${r.quote.exchange_timestamp?'체결시각 '+r.quote.exchange_timestamp:'체결시각 검증 불가'} · ${r.quote.verified_live?'5분 이내 체결시각 확인':'실시간 매매 기준으로 검증되지 않음'} · ${r.quote.note}`:(r.quote_error||'KIS 시세 API 미연결.');
  el('ai-content').textContent=r.ai_summary||r.ai_error||'규칙 기반 근거를 검토하세요.';drawEvidence(r.evidence_pack);drawChart(r.chart,chartLevels,m);if(partial)drawPendingSources(pendingSources);}
function drawEvidence(pack){
  const summary=el('evidence-reasons'), documents=el('document-index');
  if(!pack){summary.textContent='근거 목록을 불러오지 못했습니다.';documents.textContent='근거 목록 없음';return;}
  summary.innerHTML=(pack.price_reason||[]).map((n,i)=>`<div class="reason-line"><span>${i+1}</span><p>${escapeHtml(n)}</p></div>`).join('');
  const docs=pack.documents||[];
  el('document-count').textContent=`${docs.length}건의 연결 근거 · 원문 검증 상태 표시`;
  documents.innerHTML=docs.length?docs.map(d=>`<a class="evidence-document" href="${escapeHtml(safeUrl(d.url))}" target="_blank" rel="noopener noreferrer"><div class="document-main"><span class="document-tier">${escapeHtml(d.tier)}</span> <strong>[${escapeHtml(d.id)}] ${escapeHtml(d.title)}</strong></div><div class="document-meta">${escapeHtml(d.source)} · ${escapeHtml(d.published_at||'발행일 미상')} · ${escapeHtml(d.level)}</div><p>용도: ${escapeHtml(d.why)}</p><small>한계: ${escapeHtml(d.warning)}</small></a>`).join(''):'<p class="source-empty">조회 가능한 문서가 없습니다. 자료 부재는 매수 근거가 아닙니다.</p>';
}
async function loadWatchlist(){try{const entries=await api('/api/watchlist');el('watchlist-items').innerHTML=entries.length?entries.map(w=>`<div class="watch-item"><div><strong>${escapeHtml(w.name)} <span class="subtle">${escapeHtml(w.symbol)}</span></strong><small>${escapeHtml(marketName(w.market))} · 매수 관찰 ≤ ${escapeHtml(w.buy_below??'미설정')} · 매도 관찰 ≥ ${escapeHtml(w.sell_above??'미설정')}</small></div><div class="watch-actions"><button class="analyze-watch" data-id="${w.id}" title="분석">분석</button><button class="delete-watch" data-id="${w.id}" title="삭제">×</button></div></div>`).join(''):'<div class="subtle">관심 종목이 아직 없습니다. 위에서 종목을 분석한 뒤 등록할 수 있습니다.</div>';el('watchlist-items').querySelectorAll('.analyze-watch').forEach(btn=>btn.addEventListener('click',()=>{const w=entries.find(x=>x.id===Number(btn.dataset.id));setSelection(w.market,w.symbol,w.name);runAnalyze();}));el('watchlist-items').querySelectorAll('.delete-watch').forEach(btn=>btn.addEventListener('click',async()=>{if(!confirm('관심 종목을 삭제할까요?'))return;try{await api(`/api/watchlist/${btn.dataset.id}`,'DELETE');loadWatchlist()}catch(e){say(e.message,true)}}));}catch(e){el('watchlist-items').textContent='관심종목 조회 오류: '+e.message;}}
el('analysis-retry').addEventListener('click',()=>runAnalyze());el('analyze-form').addEventListener('submit',e=>{e.preventDefault();runAnalyze()});el('watch-form').addEventListener('submit',async e=>{e.preventDefault();if(!current){say('먼저 종목을 분석하세요.',true);return;}const buy=el('watch-buy').value,sell=el('watch-sell').value;try{await api('/api/watchlist','POST',{symbol:current.symbol,name:current.company_name,market:current.market,buy_below:buy?Number(buy):null,sell_above:sell?Number(sell):null});say(cloudMode?'관심 종목을 이 브라우저에 저장했습니다. 자동 알림은 아직 연결되지 않았습니다.':'관심 종목을 저장했습니다. 알림을 받으려면 별도 감시 프로세스가 필요합니다.');loadWatchlist();}catch(e){say(e.message,true)}});el('refresh-watch').addEventListener('click',loadWatchlist);document.querySelectorAll('.quick-picks button').forEach(btn=>btn.addEventListener('click',()=>{setSelection(btn.dataset.market,btn.dataset.symbol,btn.dataset.name);runAnalyze()}));health().then(loadWatchlist);


async function runBacktest(){
  if(!current){say('먼저 종목을 분석하세요.',true);return;}
  const request=analysisRequest,stock=current;
  const button=el('backtest-button');button.disabled=true;button.textContent='과거 검증 계산 중…';
  el('backtest-result').textContent='확정 일봉을 조회해 과거 조건을 계산하고 있습니다.';
  try{
    const response=await api('/api/backtest','POST',{symbol:stock.symbol,market:stock.market,horizon:Number(el('backtest-horizon').value)});
    if(request!==analysisRequest || current!==stock)return;
    const b=response.backtest;
    const fmt=x=>x==null?'표본 없음':Number(x).toLocaleString('ko-KR',{maximumFractionDigits:2})+'%';
    el('backtest-result').innerHTML=`<div class="bt-grid"><div><small>가상 진입 건수</small><strong>${b.total_trades}건</strong></div><div><small>승률</small><strong>${fmt(b.win_rate_pct)}</strong></div><div><small>평균 거래 수익률</small><strong>${fmt(b.avg_return_pct)}</strong></div><div><small>누적 수익률 (재투자 가정)</small><strong>${fmt(b.cumulative_return_pct)}</strong></div><div><small>최대 낙폭</small><strong>${fmt(b.max_drawdown_pct)}</strong></div></div><p class="small-note">검증기간: ${escapeHtml(b.start_date)}~${escapeHtml(b.end_date)} · 최대 ${b.horizon_sessions}거래일 보유 · 왕복 수수료 ${b.round_trip_fees_bps}bp · 매수/매도 슬리피지 각 ${b.slippage_per_side_bps}bp</p>${b.notes.map(n=>`<p class="small-note">• ${escapeHtml(n)}</p>`).join('')}${b.trades.length?`<details><summary>최근 가상 거래 내역 ${b.trades.length}건 보기</summary><div class="bt-trades">${b.trades.map(t=>`<div>${escapeHtml(t.entry_date)} → ${escapeHtml(t.exit_date)} : ${escapeHtml(t.net_return_pct)}% · ${escapeHtml(t.reason)}</div>`).join('')}</div></details>`:''}`;
  }catch(e){if(request===analysisRequest && current===stock)el('backtest-result').textContent='백테스트 오류: '+e.message;}
  finally{if(request===analysisRequest && current===stock){button.disabled=false;button.textContent='최근 3년 기술적 전략 검증';}}
}
el('backtest-button').addEventListener('click',runBacktest);

el('symbol').addEventListener('input',()=>{el('company-name').value='';});
el('market').addEventListener('change',()=>{el('company-name').value='';});
