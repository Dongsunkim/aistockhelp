'use strict';
(() => {
  const get = id => document.getElementById(id);
  const tokenInput = get('alerts-access-token'), save = get('alerts-save');
  if (!tokenInput || !save) return;
  const TOKEN_KEY = 'signaldesk.alert-access';
  let token = '', available = false, busy = false, revision = 0, timer = null;
  let selectionRevision = 0, derivedPlan = null;
  try { token = sessionStorage.getItem(TOKEN_KEY) || ''; } catch {}
  tokenInput.value = token;
  const node = (tag,text,className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  };
  const formatPrice = (price,market) => typeof price === 'number' && Number.isFinite(price)
    ? `${price.toLocaleString('ko-KR',{maximumFractionDigits:market === 'US' ? 2 : 0})}${market === 'US' ? '달러' : '원'}` : '확인 전';
  const formatTime = value => {
    const stamp = new Date(value);
    return value && Number.isFinite(stamp.getTime()) ? stamp.toLocaleString('ko-KR') : '확인 전';
  };
  const items = payload => Array.isArray(payload) ? payload : Array.isArray(payload?.items) ? payload.items : [];
  const updateButton = () => { save.disabled = !available || busy || token.length < 24; };
  function message(text,error=false) {
    const target = get('alerts-message');
    target.textContent = text;
    target.hidden = !text;
    target.dataset.state = error ? 'error' : 'ready';
  }
  async function request(path,{method='GET',body,access=token}={}) {
    const controller = new AbortController();
    let timeout;
    const deadline = new Promise((resolve,reject) => {
      timeout = setTimeout(() => {
        controller.abort();
        reject(new Error('알림 서버의 응답이 늦어지고 있습니다. 새로고침해 주세요.'));
      },5000);
    });
    const operation = (async () => {
      const headers = {'Content-Type':'application/json'};
      if (access) headers.Authorization = `Bearer ${access}`;
      const response = await fetch(`/api/alerts${path}`,{
        method,headers,signal:controller.signal,body:body ? JSON.stringify(body) : undefined,
      });
      const payload = await response.json();
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) throw new Error('개인 알림 접근키를 확인해 주세요.');
        throw new Error(typeof payload.detail === 'string' ? payload.detail : '알림 요청을 처리하지 못했습니다. 다시 시도해 주세요.');
      }
      return payload;
    })();
    try { return await Promise.race([operation,deadline]); }
    finally { clearTimeout(timeout); }
  }
  function renderStatus(status) {
    const badge = get('alerts-status');
    available = status?.configured === true;
    const running = available && status.worker_running === true;
    const quoteReady = status.quote_configured === true;
    badge.textContent = running ? (quoteReady ? '감시 서버 작동 중' : '시세 설정 필요')
      : available ? '감시 서버 응답 대기' : '감시 서버 미연결';
    badge.dataset.state = running && quoteReady ? 'running' : available ? 'blocked' : 'offline';
    const details = [];
    if (status?.note) details.push(status.note);
    if (available) {
      details.push(`마지막 감시 확인 ${formatTime(status.last_checked_at)}`);
      details.push(status.delivery_configured === true ? '텔레그램 설정됨 · 전송 여부는 알림 기록에서 확인' : '휴대폰 알림 미연결 · 도달 내역은 앱에서 확인');
    } else if (!status?.note) details.push('감시 서버를 연결하면 앱을 닫아도 등록한 조건을 계속 점검합니다.');
    get('alerts-status-detail').textContent = details.join(' · ');
    updateButton();
  }
  function clearPrivate() {
    get('alerts-rules').replaceChildren(node('p','내 알림을 연결하면 등록한 감시 조건이 표시됩니다.','subtle'));
    get('alerts-events').replaceChildren(node('p','내 알림 연결 후 확인할 수 있습니다.','subtle'));
  }
  function condition(rule) {
    if (rule.action === 'buy_zone') return `매수 관찰 ${formatPrice(rule.lower,rule.market)} ~ ${formatPrice(rule.upper,rule.market)}`;
    if (rule.action === 'sell_above') return `매도 관찰 ${formatPrice(rule.threshold,rule.market)} 이상`;
    return `손절 관찰 ${formatPrice(rule.threshold,rule.market)} 이하`;
  }
  function ruleDetail(rule) {
    const observation = rule.last_evaluation || rule.evaluation || {};
    const state = rule.last_status || rule.status || observation.status;
    const reasons = rule.blocked_reasons || observation.blocked_reasons || observation.reasons;
    const reason = rule.last_reason || rule.reason || observation.reason || (Array.isArray(reasons) ? reasons.join(' · ') : '');
    if (rule.enabled === false || rule.enabled === 0) return {state:'paused',text:'일시정지됨'};
    if (reason) return {state:'blocked',text:reason};
    if (state === 'blocked') return {state:'blocked',text:'시세 또는 판단 근거를 확인하고 있습니다.'};
    if (state === 'inside') return {state:'ready',text:'가격과 조건 도달 · 최근 도달 알림에서 전송 상태를 확인하세요.'};
    if (state === 'outside') return {state:'ready',text:'설정한 관찰 가격을 기다리고 있습니다.'};
    return {state:'ready',text:rule.checked_at || rule.last_checked_at ? '가격 조건을 감시하고 있습니다.' : '첫 감시 결과를 기다리고 있습니다.'};
  }
  function renderRules(rules) {
    const target = get('alerts-rules');
    target.replaceChildren();
    if (!rules.length) { target.append(node('p','아직 등록한 감시 조건이 없습니다.','subtle')); return; }
    for (const rule of rules) {
      const card = node('article',undefined,'alert-rule');
      const detail = ruleDetail(rule);
      card.dataset.state = detail.state;
      card.append(node('strong',`${rule.name || rule.symbol} · ${rule.symbol}`),node('p',condition(rule)),node('p',detail.text));
      const observation = rule.last_evaluation || rule.evaluation || {};
      const price = rule.last_price ?? observation.price;
      const checkedAt = rule.checked_at || rule.last_checked_at || observation.checked_at;
      if (typeof price === 'number') card.append(node('p',`최근 확인 ${formatPrice(price,rule.market)} · ${formatTime(checkedAt)}`));
      const actions = node('div',undefined,'alert-rule-actions');
      const pause = node('button',detail.state === 'paused' ? '감시 재개' : '일시정지');
      pause.type = 'button';
      pause.addEventListener('click',() => mutateRule(rule.id,'PATCH',{enabled:detail.state === 'paused'},pause));
      const remove = node('button','삭제');
      remove.type = 'button';
      remove.addEventListener('click',() => mutateRule(rule.id,'DELETE',undefined,remove));
      actions.append(pause,remove);card.append(actions);target.append(card);
    }
  }
  function renderEvents(events) {
    const target = get('alerts-events');
    target.replaceChildren();
    if (!events.length) { target.append(node('p','아직 가격과 조건을 충족한 도달 알림이 없습니다.','subtle')); return; }
    const delivery = {sent:'휴대폰 전송 완료',pending:'전송 대기',failed:'전송 재시도 대기',cancelled:'전송 취소',expired:'전송 기한 만료 · 기록만 보관',disabled:'앱 내 기록',not_configured:'앱 내 기록'};
    for (const event of events) {
      const card = node('article',undefined,'alert-event');
      const state = event.delivery_status || event.delivery_state || (event.delivered_at || event.sent_at ? 'sent' : 'pending');
      card.append(node('strong',event.message || '가격 조건 도달'),node('p',`${formatTime(event.created_at || event.triggered_at)} · ${delivery[state] || '전송 상태 확인 중'}`));
      if (event.provider) card.append(node('p',`시세 출처 ${event.provider}`));
      target.append(card);
    }
  }
  async function refresh() {
    const version = ++revision, access = token;
    let statusReceived = false;
    try {
      const status = await request('/status',{access:''});
      if (version !== revision) return;
      statusReceived = true;
      renderStatus(status);
      if (!available || access.length < 24) { clearPrivate(); return; }
      const responses = await Promise.all([request('/rules',{access}),request('/events',{access})]);
      if (version !== revision || access !== token) return;
      renderRules(items(responses[0]));renderEvents(items(responses[1]));
    } catch (error) {
      if (version !== revision) return;
      // A private access error does not turn a live server into a connected account.
      clearPrivate();
      message(error.message,true);
      if (!statusReceived) renderStatus({configured:false,note:'감시 서버에 연결하지 못했습니다. 새로고침해 주세요.'});
    }
  }
  async function mutateRule(id,method,body,button) {
    const access = token;
    button.disabled = true;
    try {
      await request(`/rules/${encodeURIComponent(id)}`,{method,body,access});
      if (access === token) { message(method === 'DELETE' ? '감시 조건을 삭제했습니다.' : '감시 상태를 변경했습니다.'); await refresh(); }
    } catch (error) { if (access === token) message(error.message,true); }
    finally { button.disabled = false; }
  }
  function numeric(id) {
    const value = get(id).value.trim();
    if (!value) return null;
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) throw new Error('관찰 가격은 0보다 큰 숫자로 입력해 주세요.');
    return number;
  }
  get('alerts-form').addEventListener('submit',async event => {
    event.preventDefault();
    if (busy) return;
    if (!available) { message('감시 서버를 먼저 연결해 주세요.',true); return; }
    if (token.length < 24) { message('내 알림 연결에서 개인 접근키를 입력해 주세요.',true); return; }
    const selectionVersion = selectionRevision;
    const access = token;
    busy = true;updateButton();message('');
    let saved = 0;
    try {
      const lower = numeric('alert-buy-lower'), upper = numeric('alert-buy-upper');
      const sell = numeric('alert-sell'), stop = numeric('alert-stop');
      if ((lower === null) !== (upper === null) || (lower !== null && lower > upper)) throw new Error('매수 구간의 하한과 상한을 함께 입력하고, 하한을 상한 이하로 정해 주세요.');
      const plans = [];
      if (lower !== null) plans.push({action:'buy_zone',lower,upper});
      if (sell !== null) plans.push({action:'sell_above',threshold:sell});
      if (stop !== null) plans.push({action:'stop_below',threshold:stop});
      if (!plans.length) throw new Error('감시할 가격 조건을 하나 이상 입력해 주세요.');
      if (typeof resolveCurrentStockSelection !== 'function') throw new Error('종목 검색이 준비되지 않았습니다. 새로고침해 주세요.');
      const selected = await resolveCurrentStockSelection();
      if (!selected || selectionVersion !== selectionRevision || access !== token) return;
      const identity = {symbol:selected.symbol,name:selected.name,market:selected.market};
      get('alerts-selected').textContent = `${selected.name} · ${selected.market} ${selected.symbol}`;
      for (const plan of plans) {
        if (selectionVersion !== selectionRevision || access !== token) throw new Error('선택한 종목이 변경되었습니다. 등록된 조건을 확인해 주세요.');
        await request('/rules',{method:'POST',body:{...identity,...plan,enabled:true},access});
        saved++;
      }
      message(`${saved}개 조건을 감시에 등록했습니다. 실제 감시 상태는 아래 목록에서 확인하세요.`);
      await refresh();
    } catch (error) {
      if (access === token) message(`${saved ? `${saved}개 조건은 등록되었습니다. ` : ''}${error.message}`,true);
      if (saved) await refresh();
    } finally { busy = false;updateButton(); }
  });
  get('alerts-access-form').addEventListener('submit',event => {
    event.preventDefault();
    const value = tokenInput.value.trim();
    if (value.length < 24) { message('감시 서버에 설정한 24자 이상의 개인 접근키를 입력해 주세요.',true); return; }
    token = value;revision++;clearPrivate();
    try { sessionStorage.setItem(TOKEN_KEY,token); } catch {}
    message('');updateButton();refresh();
  });
  get('alerts-disconnect').addEventListener('click',() => {
    token = '';tokenInput.value = '';revision++;
    try { sessionStorage.removeItem(TOKEN_KEY); } catch {}
    clearPrivate();updateButton();message('이 탭의 알림 연결을 해제했습니다. 서버에 등록한 감시는 계속됩니다.');
  });
  get('alerts-refresh').addEventListener('click',() => { message('');refresh(); });
  globalThis.resetAlertSelection = () => {
    selectionRevision++;
    derivedPlan = null;
    get('alerts-selected').textContent = '위에서 종목을 선택하고 관찰할 가격을 입력하세요.';
    for (const id of ['alert-buy-lower','alert-buy-upper','alert-sell','alert-stop']) get(id).value = '';
    get('alerts-plan-source').textContent = '';
    get('alerts-plan-source').hidden = true;
    get('alerts-use-analysis').hidden = true;
    message('');
  };
  globalThis.setAlertAnalysis = result => {
    derivedPlan = null;
    get('alerts-use-analysis').hidden = true;
    const levels = result?.analysis?.levels;
    if (result?.data_quality?.price_levels_eligible !== true || !levels) return;
    const {entry_low:lower,entry_high:upper,stop,target_resistance:target} = levels;
    if (![lower,upper,stop,target].every(value => typeof value === 'number' && Number.isFinite(value) && value > 0)
        || !(stop < lower && lower <= upper && upper < target)) return;
    if (result.symbol !== get('symbol').value.trim().toUpperCase() || result.market !== get('market').value) return;
    derivedPlan = {symbol:result.symbol,name:result.company_name,market:result.market,lower,upper};
    get('alerts-use-analysis').hidden = false;
  };
  get('alerts-use-analysis').addEventListener('click',() => {
    if (!derivedPlan || derivedPlan.symbol !== get('symbol').value.trim().toUpperCase() || derivedPlan.market !== get('market').value) return;
    get('alert-buy-lower').value = String(derivedPlan.lower);
    get('alert-buy-upper').value = String(derivedPlan.upper);
    get('alert-sell').value = '';
    get('alert-stop').value = '';
    get('alerts-selected').textContent = `${derivedPlan.name || derivedPlan.symbol} · ${derivedPlan.market} ${derivedPlan.symbol}`;
    get('alerts-plan-source').textContent = '기술적 관찰 가격대 · 근거 조건 미충족이면 알림 보류';
    get('alerts-plan-source').hidden = false;
    get('alerts-settings').open = true;
    get('alerts-settings').scrollIntoView?.({behavior:'smooth',block:'start'});
  });
  for (const id of ['alert-buy-lower','alert-buy-upper','alert-sell','alert-stop']) {
    get(id).addEventListener('input',() => {
      get('alerts-plan-source').textContent = '';
      get('alerts-plan-source').hidden = true;
    });
  }
  function schedule() {
    clearTimeout(timer);
    if (document.hidden) return;
    timer = setTimeout(async () => { await refresh();schedule(); },30000);
  }
  document.addEventListener('visibilitychange',() => {
    clearTimeout(timer);
    if (!document.hidden) { refresh();schedule(); }
  });
  updateButton();refresh();schedule();
})();
