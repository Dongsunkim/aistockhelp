'use strict';

// One question about the selected stock; responses never follow a stock change.
(function () {
  const ids=['qa-form','qa-input','qa-submit','qa-answer','qa-notice','qa-sources','qa-result','qa-heading'];
  const nodes=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));
  const QA_TIMEOUT_MS=40000;
  let selected=null;
  let version=0;
  let pending=null;

  function clearAnswer() {
    nodes['qa-answer'].textContent='';
    nodes['qa-notice'].textContent='';
    nodes['qa-sources'].replaceChildren();
    nodes['qa-result'].dataset.mode='';
  }

  function resetStockQA(result) {
    ++version;
    pending?.abort();
    pending=null;
    selected=result && typeof result.symbol==='string' && result.symbol && typeof result.market==='string'
      ? {symbol:result.symbol,market:result.market,name:result.company_name||result.symbol}:null;
    clearAnswer();
    nodes['qa-result'].hidden=true;
    nodes['qa-input'].value='';
    nodes['qa-input'].disabled=!selected;
    nodes['qa-input'].maxLength=600;
    nodes['qa-submit'].disabled=!selected;
    nodes['qa-submit'].textContent='질문';
    nodes['qa-form'].setAttribute('aria-busy','false');
    const name=selected && selected.name!==selected.symbol?selected.name+'('+selected.symbol+')':selected?.symbol;
    nodes['qa-heading'].textContent=selected?name+'에 질문':'이 종목에 질문하기';
  }

  function showUnavailable(message) {
    clearAnswer();
    nodes['qa-result'].hidden=false;
    nodes['qa-result'].dataset.mode='unavailable';
    nodes['qa-notice'].textContent=message;
  }

  function renderSources(sources) {
    for(const source of (Array.isArray(sources)?sources:[]).slice(0,8)) {
      if(!source || typeof source.url!=='string')continue;
      let url;
      try {url=new URL(source.url);} catch {continue;}
      if(!['https:','http:'].includes(url.protocol) || url.username || url.password)continue;
      const anchor=document.createElement('a');
      anchor.href=url.href;
      anchor.target='_blank';
      anchor.rel='noopener noreferrer';
      anchor.textContent=(source.id?'['+String(source.id)+'] ':'')+String(source.title||'자료 원문');
      nodes['qa-sources'].append(anchor);
    }
  }

  async function submitQuestion(event) {
    event.preventDefault();
    if(!selected)return;
    const question=nodes['qa-input'].value.trim();
    if(!question || question.length>600) {
      showUnavailable(question?'질문은 600자 이내로 입력해 주세요.':'궁금한 내용을 입력해 주세요.');
      return;
    }
    const request=++version,stock=selected;
    pending?.abort();
    const controller=new AbortController();
    pending=controller;
    const isCurrent=()=>request===version && selected===stock;
    clearAnswer();
    nodes['qa-result'].hidden=false;
    nodes['qa-notice'].textContent='선택한 종목의 자료에서 답변을 확인하고 있습니다.';
    nodes['qa-submit'].disabled=true;
    nodes['qa-submit'].textContent='답변 중…';
    nodes['qa-form'].setAttribute('aria-busy','true');
    let timeoutId,abortHandler,timedOut=false;
    const interrupted=new Promise((resolve,reject)=>{
      abortHandler=()=>{
        clearTimeout(timeoutId);
        const error=new Error('질문 요청이 변경되었습니다.');
        error.name='AbortError';
        reject(error);
      };
      controller.signal.addEventListener('abort',abortHandler,{once:true});
      timeoutId=setTimeout(()=>{
        timedOut=true;
        const error=new Error('답변 시간이 초과되었습니다. 잠시 뒤 다시 질문해 주세요.');
        error.name='QATimeoutError';
        reject(error);
        controller.abort();
      },QA_TIMEOUT_MS);
    });
    try {
      const task=(async()=>{
        const response=await fetch('/api/ask',{
          method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,
          body:JSON.stringify({symbol:stock.symbol,market:stock.market,name:stock.name,question})
        });
        if(!isCurrent() || controller.signal.aborted)return;
        if(!response.ok)throw new Error(response.status===429?'질문이 많습니다. 잠시 뒤 다시 시도해 주세요.':'답변을 불러오지 못했습니다. 잠시 뒤 다시 질문해 주세요.');
        return response.json();
      })();
      // Race the whole response body too: a transport may ignore cancellation.
      const answer=await Promise.race([task,interrupted]);
      if(!isCurrent() || controller.signal.aborted)return;
      if(!answer || !['ai','data_only','unavailable'].includes(answer.mode))
        throw new Error('답변 형식을 확인하지 못했습니다. 다시 질문해 주세요.');
      if(answer.mode==='unavailable') {
        showUnavailable(typeof answer.notice==='string' && answer.notice.trim()?answer.notice:
          typeof answer.answer==='string' && answer.answer.trim()?answer.answer:'지금은 답변을 제공할 수 없습니다. 잠시 뒤 다시 질문해 주세요.');
        return;
      }
      if(typeof answer.answer!=='string' || !answer.answer.trim())
        throw new Error('답변을 확인하지 못했습니다. 다시 질문해 주세요.');
      nodes['qa-result'].dataset.mode=answer.mode;
      nodes['qa-answer'].textContent=answer.answer;
      const label=answer.mode==='ai'?'AI 답변':'자료 기반 안내 · AI 미연결';
      const notice=typeof answer.notice==='string' && answer.notice.trim()?' · '+answer.notice.trim():'';
      const asof=typeof answer.asof==='string' && answer.asof.trim()?' · 기준 '+answer.asof.trim():'';
      nodes['qa-notice'].textContent=label+notice+asof;
      renderSources(answer.sources);
    } catch(error) {
      if(!isCurrent() || (controller.signal.aborted && !timedOut))return;
      showUnavailable(timedOut?'답변 시간이 초과되었습니다. 잠시 뒤 다시 질문해 주세요.':
        error instanceof Error && error.message.startsWith('질문이 많습니다.')?
        error.message:'답변을 불러오지 못했습니다. 잠시 뒤 다시 질문해 주세요.');
    } finally {
      clearTimeout(timeoutId);
      controller.signal.removeEventListener('abort',abortHandler);
      if(isCurrent()) {
        pending=null;
        nodes['qa-submit'].disabled=false;
        nodes['qa-submit'].textContent='질문';
        nodes['qa-form'].setAttribute('aria-busy','false');
      }
    }
  }

  nodes['qa-form'].addEventListener('submit',submitQuestion);
  globalThis.resetStockQA=resetStockQA;
  resetStockQA(null);
})();
