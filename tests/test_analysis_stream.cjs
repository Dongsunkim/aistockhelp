const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const encoder = new TextEncoder();
const pending = ['news', 'research', 'filings', 'financials', 'quote', 'macro'];

function result(symbol = '005930', company = '삼성전자') {
  return {
    market: 'KOSPI', symbol, company_name: company,
    meta: {fetched_at: '2026-11-06T00:00:00Z', provider: '네이버 증권'},
    analysis: {
      date: '2026-11-05', close: 70000, status: '관망', confirmed: false,
      technical_ok: false, data_age_days: 1,
      levels: {entry_low: 68000, entry_high: 69000, stop: 65000, target_resistance: 80000, rr: 2.75},
      conditions: [{id: 'pullback', name: '매수 구간', pass: false, detail: '구간 위'}],
      evidence_checks: [{name: '종가 데이터 최근 5일 이내', pass: true}],
      method: {entry: '확정 일봉 계산', caveat: '원문 확인 필요'}, red_flags: []
    },
    data_quality: {
      price_levels_eligible: true, price_level_blockers: [], verified_for_trade: false,
      status: '데이터 검증 미완료 · 관망', warnings: ['재무제표 핵심 3개 계정 조회 미완료']
    },
    news: [], filings: [], financials: [], research: [],
    chart: [{date: '2026-11-04', close: 69500}, {date: '2026-11-05', close: 70000}]
  };
}

function channel() {
  let controller;
  const body = new ReadableStream({start(value) { controller = value; }});
  return {
    response: new Response(body, {headers: {'Content-Type': 'application/x-ndjson'}}),
    bytes(bytes) { controller.enqueue(bytes); },
    send(event, newline = '\n') { controller.enqueue(encoder.encode(JSON.stringify(event) + newline)); },
    close() { controller.close(); },
    interrupt() { controller.error(new TypeError('network interrupted')); }
  };
}

function page(handleFetch) {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    textContent: '', innerHTML: '', value: '', hidden: false, disabled: false,
    open: false, dataset: {}, style: {}, attributes: {}, scrolls: 0,
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener() {}, querySelectorAll() { return []; },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key] ?? null; },
    removeAttribute(key) { delete this.attributes[key]; },
    scrollIntoView() { this.scrolls++; }
  }]));
  const calls = [], qaResets = [];
  const context = vm.createContext({
    document: {
      getElementById(id) {
        assert.ok(elements.has(id), `Element ${id} exists in the actual HTML`);
        return elements.get(id);
      },
      querySelectorAll() { return []; }
    },
    fetch(url, options) {
      // Startup health/watchlist are unrelated to analysis and intentionally pending.
      if (!String(url).startsWith('/api/analyze')) return new Promise(() => {});
      calls.push({url, options});
      return Promise.resolve().then(() => handleFetch(url, options));
    },
    resetStockQA(bundle) {
      qaResets.push(bundle);
      elements.get('qa-input').disabled = !bundle;
      elements.get('qa-submit').disabled = !bundle;
      elements.get('qa-input').value = '';
    },
    URL, console, AbortController, TextDecoder, TextEncoder, setTimeout, clearTimeout
  });
  for (const script of ['decision.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(root, 'static', script), 'utf8'), context);
  }
  return {
    elements, calls, qaResets, context,
    start(symbol = '005930', company = '삼성전자') {
      elements.get('market').value = 'KOSPI';
      elements.get('symbol').value = symbol;
      elements.get('company-name').value = company;
      return vm.runInContext('runAnalyze()', context);
    },
    current() { return vm.runInContext('current', context); },
    decision(bundle) {
      context.bundle = bundle;
      return vm.runInContext('buildDecision(bundle)', context);
    }
  };
}

async function until(check, message) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (check()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.fail(message);
}

test('actual chart appears before final evidence without resetting details or question input', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response);
  const done = p.start();
  stream.send({stage: 'partial', result: result(), pending_sources: pending});
  await until(() => !p.elements.get('result-area').hidden, 'first chart must render while sources are pending');

  assert.equal(p.elements.get('entry-zone').textContent, '68,000 ~ 69,000 KRW');
  assert.match(p.elements.get('price-chart').innerHTML, /<path /);
  assert.match(p.elements.get('decision-headline').textContent, /차트.*완료.*확인 중/);
  assert.match(p.elements.get('decision-label').textContent, /확인 중/);
  assert.match(p.elements.get('decision-sources').innerHTML, /확인 중/);
  assert.match(p.elements.get('analysis-progress').textContent, /확인/);
  assert.match(p.elements.get('analysis-progress').textContent, /0\/6/);
  assert.match(p.elements.get('financials-list').innerHTML, /확인 중/);
  assert.equal(p.elements.get('macro-status').textContent, '확인 중');
  assert.match(p.elements.get('quote-value').textContent, /확인 중/);
  assert.equal(p.current(), null, 'partial data must not become final QA/watchlist context');
  assert.equal(p.elements.get('qa-input').disabled, true);
  assert.equal(p.elements.get('analyze-btn').disabled, true);
  assert.equal(p.qaResets.length, 1);
  assert.equal(p.qaResets[0], null);

  p.elements.get('analysis-details').open = true;
  p.elements.get('qa-input').value = '다음 질문 초안';
  const scrolls = p.elements.get('result-area').scrolls;
  const next = result();
  next.news = [{title: '새 기사', source: '네이버', link: 'https://finance.naver.com/', date: '2026-11-06'}];
  stream.send({stage: 'partial', result: next, pending_sources: pending.filter(source => source !== 'news')});
  await until(() => p.elements.get('news-list').innerHTML.includes('새 기사'), 'newly finished source must update');
  assert.match(p.elements.get('analysis-progress').textContent, /1\/6/);
  assert.equal(p.elements.get('analysis-details').open, true);
  assert.equal(p.elements.get('result-area').scrolls, scrolls);
  assert.equal(p.elements.get('qa-input').value, '다음 질문 초안');
  assert.equal(p.qaResets.length, 1, 'partials must not repeatedly reset the QA form');

  const final = result();
  final.research = [{title: '최종 리서치', source: '네이버 증권', link: 'https://finance.naver.com/', type: 'research_report'}];
  stream.send({stage: 'complete', result: final, pending_sources: []});
  stream.close();
  await done;
  assert.equal(p.elements.get('decision-headline').textContent, p.decision(final).headline);
  assert.match(p.elements.get('research-list').innerHTML, /최종 리서치/);
  assert.equal(p.current().symbol, '005930');
  assert.equal(p.elements.get('qa-input').disabled, false);
  assert.equal(p.elements.get('analyze-btn').disabled, false);
  assert.equal(p.elements.get('analysis-details').open, true);
  assert.equal(p.elements.get('result-area').scrolls, scrolls);
  assert.equal(p.qaResets.length, 2);
  assert.equal(p.qaResets[1].symbol, '005930');
});

test('core evidence can show its decision while optional research and quote are still pending', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response), bundle = result();
  const done = p.start();
  stream.send({stage: 'partial', result: bundle, pending_sources: ['research', 'quote']});
  await until(() => !p.elements.get('result-area').hidden, 'core result must display before optional sources');
  assert.equal(p.elements.get('decision-headline').textContent, p.decision(bundle).headline);
  assert.match(p.elements.get('decision-sources').innerHTML, /리서치.*확인 중/);
  assert.match(p.elements.get('analysis-progress').textContent, /리서치|시세/);
  assert.equal(p.current(), null);
  assert.equal(p.elements.get('qa-submit').disabled, true);
  stream.send({stage: 'complete', result: bundle, pending_sources: []});
  stream.close();
  await done;
});

test('NDJSON decoding preserves Korean split across UTF-8 chunks and accepts a final line without newline', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response), bundle = result();
  const done = p.start();
  const message = JSON.stringify({stage: 'partial', result: bundle, pending_sources: pending});
  const bytes = encoder.encode('\n\r\n' + message + '\r\n');
  const nameOffset = Buffer.from(bytes).indexOf(Buffer.from('삼성전자'));
  assert.ok(nameOffset > 0);
  stream.bytes(bytes.slice(0, nameOffset + 1));
  stream.bytes(bytes.slice(nameOffset + 1, nameOffset + 2));
  stream.bytes(bytes.slice(nameOffset + 2));
  await until(() => p.elements.get('result-name').textContent === '삼성전자', 'multibyte company name must survive streaming');
  assert.doesNotMatch(p.elements.get('result-name').textContent, /�/);
  const finalBytes = encoder.encode(JSON.stringify({stage: 'complete', result: bundle, pending_sources: []}));
  stream.bytes(finalBytes.slice(0, 11));
  stream.bytes(finalBytes.slice(11));
  stream.close();
  await done;
  assert.equal(p.current().company_name, '삼성전자');
  assert.equal(p.elements.get('decision-headline').textContent, p.decision(bundle).headline);
});

test('a cached completed response labels its age and preserves source observation dates', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response), bundle = result();
  bundle.cache = {hit: true, age_seconds: 37.9, stored_at: '2026-11-06T00:00:00Z'};
  bundle.macro = {
    eligible: false, risk_flags: [], metrics: {
      rate: {value: 4, change_pp: -0.25, unit: '%', asof: '2026-10-30', source: 'FRED', url: 'https://fred.stlouisfed.org/series/DFF'}
    }
  };
  const done = p.start();
  stream.send({stage: 'complete', result: bundle, pending_sources: []});
  stream.close();
  await done;
  assert.match(p.elements.get('decision-asof').textContent, /37초 전 조회 자료 · 기준시각/);
  assert.match(p.elements.get('result-date').textContent, /37초 전 조회 자료 · 기준시각/);
  assert.match(p.elements.get('decision-asof').textContent, /2026-11-05 확정 종가/);
  assert.match(p.elements.get('macro-metrics').innerHTML, /2026-10-30/);
  assert.equal(p.current().meta.fetched_at, '2026-11-06T00:00:00Z');
});

test('a late response for the previous stock cannot replace the selected stock or re-enable its loading button', {timeout: 3000}, async () => {
  let resolveOld;
  const oldResponse = new Promise(resolve => { resolveOld = resolve; });
  const active = channel(), old = channel();
  const p = page((_url, options) => JSON.parse(options.body).symbol === '005930' ? oldResponse : active.response);
  const oldDone = p.start();
  await until(() => p.calls.length === 1, 'first request must begin');
  const activeDone = p.start('000660', 'SK하이닉스');
  active.send({stage: 'partial', result: result('000660', 'SK하이닉스'), pending_sources: pending});
  await until(() => p.elements.get('result-name').textContent === 'SK하이닉스', 'new stock must render');
  resolveOld(old.response);
  old.send({stage: 'complete', result: result(), pending_sources: []});
  old.close();
  await oldDone;
  assert.equal(p.elements.get('result-name').textContent, 'SK하이닉스');
  assert.equal(p.elements.get('analyze-btn').disabled, true);
  assert.equal(p.current(), null);
  active.send({stage: 'complete', result: result('000660', 'SK하이닉스'), pending_sources: []});
  active.close();
  await activeDone;
  assert.equal(p.current().symbol, '000660');
  assert.equal(p.elements.get('result-name').textContent, 'SK하이닉스');
  assert.equal(p.elements.get('analyze-btn').disabled, false);
  assert.equal(p.calls[0].options.signal.aborted, true);
});

for (const status of [404, 405]) {
  test(`stream HTTP ${status} falls back once to the supported JSON endpoint`, {timeout: 3000}, async () => {
    const bundle = result();
    const p = page(url => url.endsWith('/stream')
      ? new Response('unsupported', {status})
      : new Response(JSON.stringify(bundle), {headers: {'Content-Type': 'application/json'}}));
    await p.start();
    assert.deepEqual(p.calls.map(call => call.url), ['/api/analyze/stream', '/api/analyze']);
    assert.equal(p.current().symbol, '005930');
    assert.equal(p.elements.get('decision-headline').textContent, p.decision(bundle).headline);
    assert.equal(p.qaResets.length, 2);
  });
}

test('a stream error never triggers a duplicate provider request or enables QA', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response);
  const done = p.start();
  stream.send({stage: 'error', detail: '일봉 데이터 조회에 실패했습니다.'});
  stream.close();
  await done;
  assert.deepEqual(p.calls.map(call => call.url), ['/api/analyze/stream']);
  assert.equal(p.current(), null);
  assert.equal(p.elements.get('qa-input').disabled, true);
  assert.equal(p.elements.get('analyze-btn').disabled, false);
  assert.equal(p.elements.get('app-message').hidden, false);
  assert.match(p.elements.get('app-message').textContent, /실패/);
});

test('HTTP failure is reported without falling back to another expensive analysis', {timeout: 3000}, async () => {
  const p = page(() => new Response(JSON.stringify({detail: '자료 제공 서버가 응답하지 않습니다.'}), {
    status: 503, headers: {'Content-Type': 'application/json'}
  }));
  await p.start();
  assert.equal(p.calls.length, 1);
  assert.equal(p.current(), null);
  assert.match(p.elements.get('app-message').textContent, /응답하지 않습니다/);
  assert.equal(p.elements.get('analyze-btn').disabled, false);
});

for (const ending of ['interrupted', 'closed', 'malformed', 'error stage']) {
  test(`a ${ending} stream retains a visibly incomplete chart and offers retry without final QA context`, {timeout: 3000}, async () => {
    const stream = channel(), p = page(() => stream.response);
    const done = p.start();
    stream.send({stage: 'partial', result: result(), pending_sources: pending});
    await until(() => !p.elements.get('result-area').hidden, 'partial chart must render before failure');
    if (ending === 'interrupted') stream.interrupt();
    else {
      if (ending === 'malformed') stream.bytes(encoder.encode('{this is not JSON}\n'));
      if (ending === 'error stage') stream.send({stage: 'error', detail: '추가 자료 확인에 실패했습니다.'});
      stream.close();
    }
    await done;
    assert.equal(p.calls.length, 1, 'failed stream must not silently run the same providers again');
    assert.equal(p.elements.get('result-area').hidden, false);
    assert.equal(p.elements.get('entry-zone').textContent, '68,000 ~ 69,000 KRW');
    assert.equal(p.current(), null);
    assert.equal(p.elements.get('qa-input').disabled, true);
    assert.equal(p.qaResets.length, 1);
    assert.equal(p.elements.get('analyze-btn').disabled, false, 'the user must be able to retry');
    assert.match(p.elements.get('decision-headline').textContent + ' ' + p.elements.get('analysis-progress').textContent, /확인 중|미완료|완료하지 못|중단|끝내지 못/);
    assert.equal(p.elements.get('app-message').hidden, false);
  });
}
