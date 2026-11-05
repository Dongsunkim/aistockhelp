'use strict';
(() => {
  const get = id => document.getElementById(id);
  const html = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const display = value => value == null || value === '' ? '—' : String(value);
  // Financial amounts remain strings: converting to Number loses large-account precision.
  const amount = value => {
    const raw = display(value);
    if (!/^-?\d+(?:\.\d+)?$/.test(raw)) return html(raw);
    const [integer, fraction] = raw.split('.');
    return html(integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction === undefined ? '' : '.' + fraction));
  };
  const receipt = value => /^\d{14}$/.test(String(value ?? '')) ? `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${value}` : null;
  const safeLink = value => {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && url.hostname === 'dart.fss.or.kr' ? url.href : null;
    } catch { return null; }
  };
  const now = new Date();
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en', {timeZone:'Asia/Seoul', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(now).map(p => [p.type, p.value]));
  const today = `${parts.year}-${parts.month}-${parts.day}`;
  const past = new Date(today + 'T00:00:00Z');
  past.setUTCDate(past.getUTCDate() - 150);
  get('dart-year').value = String(Number(parts.year) - 1);
  get('dart-year').max = parts.year;
  get('dart-begin').value = past.toISOString().slice(0, 10);
  get('dart-end').value = today;
  get('dart-begin').max = get('dart-end').max = today;

  const state = {generation:0, controller:null, signature:null, query:null, financial:null, pages:[], items:[], paging:false};
  const signature = () => ['market','symbol','dart-year','dart-report','dart-basis','dart-begin','dart-end'].map(id => get(id).value.trim()).join('|');
  const active = generation => generation === state.generation && state.signature === signature();
  function reset(message) {
    state.generation += 1;
    state.controller?.abort();
    state.signature = null;
    state.query = null;
    state.financial = null;
    state.pages = [];
    state.items = [];
    state.paging = false;
    for (const id of ['dart-financial-results','dart-disclosure-results','dart-financial-status','dart-disclosure-status']) get(id).textContent = '';
    get('dart-status').textContent = message;
    get('dart-submit').disabled = false;
    get('dart-submit').textContent = 'DART 공시·재무 조회';
    get('dart-more').hidden = true;
    get('dart-all').hidden = true;
    get('dart-more').disabled = false;
    get('dart-all').disabled = false;
    get('dart-more').textContent = '다음 공시 100건 불러오기';
    get('dart-all').textContent = '공시 전체 불러오기';
    get('dart-financial-download').disabled = get('dart-disclosure-download').disabled = true;
  }
  function invalidate() {
    if (state.signature !== null && state.signature !== signature()) reset('조회 조건이 바뀌었습니다. DART 공시·재무 조회를 다시 실행하세요.');
  }
  async function post(path, body, signal) {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal});
    let data;
    try { data = await response.json(); } catch { throw new Error('서버 응답을 읽지 못했습니다.'); }
    if (!response.ok) throw new Error(response.status === 429 ? '요청 횟수 제한입니다. 잠시 후 다음 페이지 또는 전체 불러오기로 이어서 조회하세요.' : typeof data.detail === 'string' ? data.detail : `조회 조건을 확인하세요. (HTTP ${response.status})`);
    return data;
  }
  const companyLabel = company => `${company?.corp_name || '기업명 미제공'} (${company?.stock_code || state.query.symbol})`;
  function financialTable(accounts, label) {
    return `<div class="dart-table-wrap" tabindex="0" role="region" aria-label="${html(label)} 재무계정 표"><table class="dart-table"><caption>${html(label)} · API가 제공한 원래 기간명과 통화 기준</caption><thead><tr><th scope="col">계정</th><th scope="col">당기 금액</th><th scope="col">당기 누적</th><th scope="col">전기 금액</th><th scope="col">전기 분기 금액</th><th scope="col">전기 누적</th><th scope="col">전전기 금액</th><th scope="col">통화</th><th scope="col">원문</th></tr></thead><tbody>${accounts.map(row => {
      const url = receipt(row.rcept_no);
      const cell = (value, period) => `<td><span class="dart-amount">${amount(value)}</span>${period ? `<small>${html(period)}</small>` : ''}</td>`;
      return `<tr><th scope="row">${html(row.account_nm)}${row.account_detail ? `<small>${html(row.account_detail)}</small>` : ''}${row.account_id ? `<small>${html(row.account_id)}</small>` : ''}</th>${cell(row.thstrm_amount,row.thstrm_nm)}${cell(row.thstrm_add_amount,'누적금액 (제공 시)')}${cell(row.frmtrm_amount,row.frmtrm_nm)}${cell(row.frmtrm_q_amount,row.frmtrm_q_nm)}${cell(row.frmtrm_add_amount,'누적금액 (제공 시)')}${cell(row.bfefrmtrm_amount,row.bfefrmtrm_nm)}<td>${html(display(row.currency))}</td><td>${url ? `<a href="${html(url)}" target="_blank" rel="noopener noreferrer">공시 보기 ↗</a>` : '—'}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }
  function renderFinancial(data) {
    const statements = data.statements || [];
    const count = statements.reduce((n, statement) => n + (statement.accounts || []).length, 0);
    get('dart-financial-status').textContent = `${companyLabel(data.company)} · ${data.year}년 ${data.report_label || data.report_code} · ${count}개 계정 · ${data.complete ? '요청한 회계기준 조회 완료' : '일부 회계기준 조회 실패'}`;
    get('dart-financial-results').innerHTML = statements.map(statement => {
      const label = `${statement.fs_name || statement.fs_div} (${statement.fs_div})`;
      if (statement.status !== 'available') return `<div class="dart-source-note"><strong>${html(label)}</strong><p>${html(statement.message || (statement.status === 'no_data' ? '이 기간에 제공되는 재무계정이 없습니다.' : '조회하지 못했습니다.'))}</p></div>`;
      const groups = new Map();
      for (const row of statement.accounts || []) {
        const key = row.sj_div || 'OTHER';
        if (!groups.has(key)) groups.set(key, {name:row.sj_nm || key, rows:[]});
        groups.get(key).rows.push(row);
      }
      return `<details class="dart-basis" open><summary>${html(label)} · ${(statement.accounts || []).length}개 계정</summary>${[...groups.entries()].map(([key, group]) => `<details class="dart-sheet"><summary>${html(group.name)} (${html(key)}) · ${group.rows.length}개 계정</summary>${financialTable(group.rows, `${label} / ${group.name}`)}</details>`).join('')}</details>`;
    }).join('') + `<p class="small-note">${html(data.note || '공시 원문의 단위·기간·정정 여부를 확인하세요.')} 조회시각: ${html(data.fetched_at || '미제공')}</p>`;
    get('dart-financial-download').disabled = false;
  }
  function renderDisclosures() {
    const latest = state.pages.at(-1);
    if (!latest) return;
    const total = latest.total_count;
    const progress = disclosureProgress();
    get('dart-disclosure-status').textContent = `${companyLabel(latest.company)} · ${latest.begin} ~ ${latest.end} · 전체 ${total}건 중 ${state.items.length}건 조회 · ${state.pages.length}페이지 · ${progress.complete ? '전체 목록 조회 완료' : latest.has_more ? '다음 페이지 있음' : '마지막 페이지 도달 · 전체 목록 수집 미완료'}${progress.consistent ? '' : ' · 조회 중 공시 목록이 변했거나 페이지가 겹칩니다. 처음부터 다시 조회하세요.'}`;
    get('dart-disclosure-results').innerHTML = state.items.length ? state.items.map(item => {
      const url = receipt(item.rcept_no) || safeLink(item.link);
      const title = html(item.title || item.report_nm || '제목 미제공');
      return `<article class="dart-disclosure"><h4>${url ? `<a href="${html(url)}" target="_blank" rel="noopener noreferrer">${title} ↗</a>` : title}</h4><p>${html(item.date || item.rcept_dt || '날짜 미제공')} · ${html(item.flr_nm || item.corp_name || item.source || '금융감독원 DART')}${item.corp_cls ? ` · 법인구분 ${html(item.corp_cls)}` : ''}</p><p>접수번호 ${html(item.rcept_no || '미제공')}${item.rm ? ` · ${html(item.rm)}` : ''} · 공시 원문 별도 확인</p></article>`;
    }).join('') : '<p class="source-empty">선택한 기간에 제공되는 공시가 없습니다.</p>';
    get('dart-more').hidden = !latest.has_more;
    get('dart-all').hidden = !latest.has_more;
    get('dart-more').textContent = '다음 공시 100건 불러오기';
    get('dart-disclosure-download').disabled = false;
  }
  function disclosureProgress() {
    const latest = state.pages.at(-1);
    if (!latest) return {complete:false, consistent:false};
    const stableTotal = state.pages.every(page => page.total_count === latest.total_count);
    const sequential = state.pages.every((page, index) => page.page === index + 1);
    const rawCount = state.pages.reduce((count, page) => count + page.items.length, 0);
    const consistent = stableTotal && sequential && rawCount === state.items.length;
    return {consistent, complete:consistent && !latest.has_more && state.items.length === latest.total_count};
  }
  async function loadFinancial(generation) {
    try {
      const data = await post('/api/dart/financials', {symbol:state.query.symbol, year:state.query.year, report_code:state.query.report_code, fs_div:state.query.fs_div}, state.controller.signal);
      if (!active(generation)) return;
      state.financial = data;
      renderFinancial(data);
    } catch (error) {
      if (active(generation) && error.name !== 'AbortError') get('dart-financial-status').textContent = '재무계정 조회 실패: ' + error.message;
    }
  }
  async function loadDisclosures(generation, page) {
    try {
      const data = await post('/api/dart/disclosures', {symbol:state.query.symbol, begin:state.query.begin, end:state.query.end, page}, state.controller.signal);
      if (!active(generation)) return;
      if (data.page !== page || !Array.isArray(data.items)) throw new Error('공시 페이지 응답을 확인할 수 없습니다.');
      state.pages.push(data);
      const seen = new Set(state.items.map(item => item.rcept_no).filter(Boolean));
      for (const item of data.items) {
        if (item.rcept_no && seen.has(item.rcept_no)) continue;
        state.items.push(item);
        if (item.rcept_no) seen.add(item.rcept_no);
      }
      renderDisclosures();
      return true;
    } catch (error) {
      if (active(generation) && error.name !== 'AbortError') get('dart-disclosure-status').textContent = `공시 ${page}페이지 조회 실패: ${error.message}${state.items.length ? ` (이전 ${state.items.length}건은 유지됨)` : ''}`;
      return false;
    }
  }
  get('dart-form').addEventListener('submit', async event => {
    event.preventDefault();
    reset('DART 재무계정과 공시를 각각 조회하고 있습니다…');
    const symbol = get('symbol').value.trim();
    const begin = get('dart-begin').value, end = get('dart-end').value;
    if (get('market').value === 'US' || !/^\d{6}$/.test(symbol)) {
      get('dart-status').textContent = '위 시장과 종목코드에서 국내 종목의 6자리 코드를 선택하세요.';
      return;
    }
    const days = (Date.parse(end + 'T00:00:00Z') - Date.parse(begin + 'T00:00:00Z')) / 86400000;
    if (!Number.isFinite(days) || days < 0 || days > 366) {
      get('dart-status').textContent = '공시 종료일은 시작일 이후, 조회기간은 최대 366일로 선택하세요.';
      return;
    }
    state.signature = signature();
    state.query = {symbol, year:Number(get('dart-year').value), report_code:get('dart-report').value, fs_div:get('dart-basis').value, begin, end};
    state.controller = new AbortController();
    const generation = state.generation;
    get('dart-submit').disabled = true;
    get('dart-submit').textContent = 'DART 조회 중…';
    get('dart-financial-status').textContent = '재무계정 조회 중…';
    get('dart-disclosure-status').textContent = '공시 첫 페이지 조회 중…';
    await Promise.allSettled([loadFinancial(generation), loadDisclosures(generation, 1)]);
    if (!active(generation)) return;
    get('dart-status').textContent = '조회가 끝났습니다. 재무계정과 공시의 조회 상태를 각각 확인하세요.';
    get('dart-submit').disabled = false;
    get('dart-submit').textContent = 'DART 공시·재무 조회';
  });
  async function loadRemaining(all) {
    invalidate();
    const latest = state.pages.at(-1);
    if (!latest?.has_more || state.paging) return;
    const generation = state.generation;
    state.paging = true;
    get('dart-more').disabled = true;
    get('dart-all').disabled = true;
    get('dart-more').textContent = '다음 페이지 조회 중…';
    get('dart-all').textContent = all ? '전체 공시 순서대로 조회 중…' : '공시 전체 불러오기';
    do {
      const next = state.pages.at(-1).page + 1;
      if (!await loadDisclosures(generation, next)) break;
    } while (all && active(generation) && state.pages.at(-1).has_more);
    if (active(generation)) {
      state.paging = false;
      get('dart-more').disabled = false;
      get('dart-all').disabled = false;
      get('dart-more').textContent = '다음 공시 100건 불러오기';
      get('dart-all').textContent = '공시 전체 불러오기';
    }
  }
  get('dart-more').addEventListener('click', () => loadRemaining(false));
  get('dart-all').addEventListener('click', () => loadRemaining(true));
  function download(payload, filename) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)], {type:'application/json;charset=utf-8'}));
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = filename;
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  get('dart-financial-download').addEventListener('click', () => {
    invalidate();
    if (state.financial) download(state.financial, `dart-financials-${state.query.symbol}-${state.query.year}-${state.query.report_code}.json`);
  });
  get('dart-disclosure-download').addEventListener('click', () => {
    invalidate();
    if (state.pages.length) {
      const progress = disclosureProgress();
      download({query:state.query,loaded_count:state.items.length,loaded_pages:state.pages.map(page => page.page),total_count:state.pages.at(-1).total_count,complete:progress.complete,snapshot_consistent:progress.consistent,items:state.items,pages:state.pages}, `dart-disclosures-${state.query.symbol}-${state.query.begin}-${state.query.end}.json`);
    }
  });
  for (const id of ['market','symbol','dart-year','dart-report','dart-basis','dart-begin','dart-end']) {
    get(id).addEventListener('input', invalidate);
    get(id).addEventListener('change', invalidate);
  }
  // Existing quick picks and watchlist actions change the stock inputs programmatically.
  document.addEventListener('click', () => queueMicrotask(invalidate));
})();
