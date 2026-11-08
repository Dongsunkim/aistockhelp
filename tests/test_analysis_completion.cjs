const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const encoder = new TextEncoder();
const pendingSources = ['news', 'research', 'filings', 'financials', 'quote', 'macro'];

function result(symbol = '005930', company = '삼성전자') {
  return {
    market: 'KOSPI', symbol, company_name: company,
    meta: {fetched_at: '2026-11-06T00:00:00Z', provider: '네이버 증권'},
    analysis: {
      date: '2026-11-05', close: 68500, status: '추가 검증 필요', confirmed: false,
      technical_ok: true, data_age_days: 1,
      levels: {entry_low: 68000, entry_high: 69000, stop: 65000, target_resistance: 80000, rr: 2.75},
      conditions: ['trend', 'pullback', 'rsi', 'volatility', 'volume', 'rr'].map(id => ({id, name: id, pass: true, detail: '조건 충족'})),
      evidence_checks: [{name: '종가 데이터 최근 5일 이내', pass: true}],
      method: {entry: '확정 일봉 계산', caveat: '원문 확인 필요'}, red_flags: []
    },
    data_quality: {
      price_levels_eligible: true, price_level_blockers: [], verified_for_trade: false,
      status: '데이터 검증 미완료 · 관망', warnings: ['필수 미국 경제 자료 미확인']
    },
    news: [], filings: [], financials: [], research: [],
    macro: {eligible: false, metrics: {}, risk_flags: [], warnings: ['미국 경제 자료 미확인']},
    chart: [{date: '2026-11-04', close: 68000}, {date: '2026-11-05', close: 68500}]
  };
}

function fakeClock() {
  let now = 0, serial = 0;
  const timers = new Map();
  return {
    setTimeout(callback, delay = 0, ...args) {
      const id = ++serial;
      timers.set(id, {at: now + Number(delay), run: () => callback(...args)});
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    now() { return now; },
    callbacks() { return [...timers.values()].map(timer => timer.run); },
    async advance(milliseconds) {
      now += milliseconds;
      for (let iteration = 0; iteration < 10; iteration++) {
        const due = [...timers].filter(([, timer]) => timer.at <= now);
        for (const [id, timer] of due) {
          timers.delete(id);
          timer.run();
        }
        await flush();
        if (![...timers.values()].some(timer => timer.at <= now)) return;
      }
      assert.fail('timer callbacks must settle without a repeated immediate timer');
    }
  };
}

function channel({stuckCancel = false} = {}) {
  let controller;
  const body = new ReadableStream({
    start(value) { controller = value; },
    cancel() { return stuckCancel ? new Promise(() => {}) : undefined; }
  });
  return {
    response: new Response(body, {headers: {'Content-Type': 'application/x-ndjson'}}),
    send(event) { controller.enqueue(encoder.encode(JSON.stringify(event) + '\n')); },
    close() { controller.close(); },
    interrupt(error = new TypeError('connection lost')) { controller.error(error); }
  };
}

function completedResponse(bundle) {
  return new Response(JSON.stringify({stage: 'complete', result: bundle, pending_sources: []}) + '\n', {
    headers: {'Content-Type': 'application/x-ndjson'}
  });
}

function page(handleFetch) {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map(([tag, id]) => [id, {
    textContent: '', innerHTML: '', value: '', hidden: /\bhidden\b/.test(tag), disabled: false,
    open: false, dataset: {}, style: {}, attributes: {}, scrolls: 0, listeners: {},
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); },
    querySelectorAll() { return []; },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key] ?? null; },
    removeAttribute(key) { delete this.attributes[key]; },
    scrollIntoView() { this.scrolls++; },
    click() {
      if (this.disabled) return;
      const event = {preventDefault() {}, currentTarget: this, target: this};
      for (const listener of this.listeners.click || []) listener(event);
      this.onclick?.(event);
    }
  }]));
  const clock = fakeClock(), calls = [], qaResets = [];
  const context = vm.createContext({
    document: {
      getElementById(id) {
        assert.ok(elements.has(id), `Element ${id} exists in the actual HTML`);
        return elements.get(id);
      },
      querySelectorAll() { return []; }
    },
    fetch(url, options) {
      if (!String(url).startsWith('/api/analyze')) return new Promise(() => {});
      calls.push({url, options});
      return Promise.resolve().then(() => handleFetch(url, options));
    },
    resetStockQA(bundle) {
      qaResets.push(bundle);
      elements.get('qa-input').disabled = !bundle;
      elements.get('qa-submit').disabled = !bundle;
    },
    URL, console, AbortController, TextDecoder, TextEncoder,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    performance: {now: clock.now}
  });
  for (const script of ['decision.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(root, 'static', script), 'utf8'), context);
  }
  return {
    html, elements, context, calls, clock, qaResets,
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

function flush() { return new Promise(resolve => setImmediate(resolve)); }

async function until(check, message) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (check()) return;
    await flush();
  }
  assert.fail(message);
}

function assertStopped(p) {
  assert.equal(p.elements.get('analyze-btn').disabled, false, 'analysis must be retryable');
  assert.equal(p.elements.get('loading-state').hidden, true, 'spinner must stop');
  assert.equal(p.elements.get('result-area').getAttribute('aria-busy'), 'false');
  assert.equal(p.elements.get('analysis-progress').hidden, true, 'pending progress must end');
}

test('a terminal partial completion gives a conservative decision, missing-source notice, and usable QA', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response), bundle = result();
  const done = p.start();
  stream.send({stage: 'partial', result: bundle, pending_sources: pendingSources});
  await until(() => !p.elements.get('result-area').hidden, 'initial chart must render');
  assert.equal(p.elements.get('qa-submit').disabled, true);
  bundle.completion = {
    status: 'partial', timed_out_sources: ['macro'], unavailable_sources: ['macro', 'financials'],
    message: '일부 자료를 제한 시간 안에 확인하지 못해, 확인된 자료를 기준으로 판단했습니다.'
  };
  stream.send({stage: 'complete', result: bundle, pending_sources: []});
  stream.close();
  await done;

  assertStopped(p);
  assert.equal(p.elements.get('decision-headline').textContent, p.decision(bundle).headline);
  assert.match(p.elements.get('decision-headline').textContent, /판단.*보류/);
  assert.doesNotMatch(p.elements.get('decision-headline').textContent + p.elements.get('decision-label').textContent + p.elements.get('decision-sources').innerHTML, /확인 중/);
  assert.equal(p.elements.get('analysis-completion').hidden, false);
  assert.match(p.elements.get('analysis-completion').textContent, /일부 자료|제한 시간|확인하지 못/);
  assert.match(p.elements.get('analysis-completion').textContent, /미국 경제|재무/);
  assert.equal(p.current().completion.status, 'partial');
  assert.equal(p.elements.get('qa-submit').disabled, false);
  assert.equal(p.elements.get('backtest-button').disabled, false);
  assert.equal(p.qaResets.length, 2, 'terminal partial receives its final, usable QA context once');
  assert.equal(p.qaResets[1].symbol, '005930');
});

test('terminal completion waits for no further sources and clears the prior stock completion notice', {timeout: 3000}, async () => {
  const first = result();
  first.completion = {status: 'partial', timed_out_sources: ['macro'], unavailable_sources: ['macro'], message: '미국 경제 자료를 확인하지 못했습니다.'};
  const second = result('000660', 'SK하이닉스');
  second.completion = {status: 'complete', timed_out_sources: [], unavailable_sources: [], message: '자료 점검을 완료했습니다.'};
  const p = page((_url, options) => completedResponse(JSON.parse(options.body).symbol === '005930' ? first : second));
  await p.start();
  assert.equal(p.elements.get('analysis-completion').hidden, false);
  await p.start('000660', 'SK하이닉스');
  assertStopped(p);
  assert.equal(p.current().symbol, '000660');
  assert.equal(p.elements.get('decision-headline').textContent, p.decision(second).headline);
  assert.ok(p.elements.get('analysis-completion').hidden || !p.elements.get('analysis-completion').textContent.includes('확인하지 못했습니다'), 'old missing-source notice must not follow a different stock');
});

test('a fetch that ignores cancellation reaches the total deadline before response headers and shows a specific failure card', {timeout: 3000}, async () => {
  const p = page(() => new Promise(() => {}));
  let settled = false;
  const done = p.start().then(() => { settled = true; });
  await until(() => p.calls.length === 1, 'request must begin');
  assert.equal(p.elements.get('analyze-btn').disabled, true);
  await p.clock.advance(22000);
  await until(() => settled, 'client deadline must settle even if fetch ignores AbortSignal');
  await done;

  assertStopped(p);
  assert.equal(p.calls.length, 1, 'deadline must not trigger duplicate provider fetching');
  assert.equal(p.calls[0].options.signal.aborted, true);
  assert.equal(p.current(), null);
  assert.equal(p.elements.get('qa-submit').disabled, true);
  assert.equal(p.elements.get('analysis-failure').hidden, false);
  assert.equal(p.elements.get('empty-state').hidden, true, 'failure must not look like the initial stock picker');
  assert.match(p.html, /매수 시점을 판단할 자료가 부족합니다/);
  assert.match(p.elements.get('analysis-failure-detail').textContent, /시간|연결|응답/);
  assert.equal(p.elements.get('analysis-retry').disabled, false);
});

test('an active fetch rejecting AbortError shows a retryable failure even when its signal was never aborted', {timeout: 3000}, async () => {
  const error = Object.assign(new Error('upstream transport aborted unexpectedly'), {name: 'AbortError'});
  const p = page(() => Promise.reject(error));
  await p.start();

  assertStopped(p);
  assert.equal(p.calls.length, 1, 'transport abort must not trigger another provider request');
  assert.equal(p.calls[0].options.signal.aborted, false, 'this is a transport failure, not a superseded request');
  assert.equal(p.current(), null);
  assert.equal(p.elements.get('qa-submit').disabled, true);
  assert.equal(p.elements.get('analysis-failure').hidden, false);
  assert.equal(p.elements.get('empty-state').hidden, true);
  assert.match(p.elements.get('analysis-failure-detail').textContent, /연결.*중단.*다시 분석/);
  assert.doesNotMatch(p.elements.get('analysis-failure-detail').textContent, /upstream transport/);
  assert.equal(p.elements.get('app-message').hidden, false);
  assert.equal(p.elements.get('analysis-retry').disabled, false);
});

test('an active reader AbortError after a chart remains a visible hold rather than silently abandoning the result', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response);
  const done = p.start();
  stream.send({stage: 'partial', result: result(), pending_sources: pendingSources});
  await until(() => !p.elements.get('result-area').hidden, 'initial chart must render');
  stream.interrupt(Object.assign(new Error('upstream reader aborted unexpectedly'), {name: 'AbortError'}));
  await done;

  assertStopped(p);
  assert.equal(p.calls[0].options.signal.aborted, false);
  assert.equal(p.calls.length, 1);
  assert.equal(p.current(), null);
  assert.equal(p.elements.get('qa-submit').disabled, true);
  assert.equal(p.elements.get('decision-headline').textContent, '필수 자료 확인 불가로 매수 판단 보류');
  assert.equal(p.elements.get('analysis-completion').hidden, false);
  assert.match(p.elements.get('analysis-completion').textContent, /연결.*중단.*다시 분석/);
  assert.doesNotMatch(p.elements.get('analysis-completion').textContent, /upstream reader|확인 중/);
});

test('the same total deadline ends a stalled reader after chart rendering without waiting for cancellation cleanup', {timeout: 3000}, async () => {
  const stream = channel({stuckCancel: true}), p = page(() => stream.response);
  let settled = false;
  const done = p.start().then(() => { settled = true; });
  await until(() => p.calls.length === 1, 'request must begin');
  await p.clock.advance(10000);
  stream.send({stage: 'partial', result: result(), pending_sources: pendingSources});
  await until(() => !p.elements.get('result-area').hidden, 'chart must render before the deadline');
  await p.clock.advance(11999);
  assert.equal(settled, false, 'the request is within the total client budget');
  await p.clock.advance(1);
  await until(() => settled, 'a partial chunk must not restart the full request timeout');
  await done;

  assertStopped(p);
  assert.equal(p.calls.length, 1);
  assert.equal(p.elements.get('result-area').hidden, false);
  assert.equal(p.elements.get('decision-headline').textContent, '필수 자료 확인 불가로 매수 판단 보류');
  assert.equal(p.current(), null);
  assert.equal(p.elements.get('qa-submit').disabled, true);
  assert.equal(p.elements.get('analysis-completion').hidden, false);
  assert.doesNotMatch(p.elements.get('decision-sources').innerHTML, /확인 중/);
});

test('a transport failure after a chart becomes an explicit hold without presenting a completed server context', {timeout: 3000}, async () => {
  const stream = channel(), p = page(() => stream.response);
  const done = p.start();
  stream.send({stage: 'partial', result: result(), pending_sources: pendingSources});
  await until(() => !p.elements.get('result-area').hidden, 'chart must render');
  stream.interrupt();
  await done;

  assertStopped(p);
  assert.equal(p.calls.length, 1);
  assert.equal(p.elements.get('decision-headline').textContent, '필수 자료 확인 불가로 매수 판단 보류');
  assert.match(p.elements.get('decision-label').textContent, /보류/);
  assert.doesNotMatch(p.elements.get('decision-sources').innerHTML, /확인 중/);
  assert.equal(p.elements.get('analysis-completion').hidden, false);
  assert.match(p.elements.get('analysis-completion').textContent, /실패|불가|중단|완료하지 못|연결/);
  assert.equal(p.current(), null);
  assert.equal(p.elements.get('qa-submit').disabled, true);
  assert.equal(p.qaResets.length, 1);
});

test('no-price errors have a visible retry action that starts a new analysis', {timeout: 3000}, async () => {
  let attempts = 0;
  const p = page(() => {
    if (++attempts > 1) return completedResponse(result());
    return new Response(JSON.stringify({stage: 'error', detail: '최신 확정 일봉 가격을 확인하지 못했습니다.'}) + '\n', {
      headers: {'Content-Type': 'application/x-ndjson'}
    });
  });
  await p.start();
  assertStopped(p);
  assert.equal(p.elements.get('analysis-failure').hidden, false);
  assert.equal(p.elements.get('empty-state').hidden, true);
  assert.match(p.elements.get('analysis-failure-detail').textContent, /최신 확정 일봉 가격/);
  assert.equal(p.calls.length, 1);
  p.elements.get('analysis-retry').click();
  await until(() => p.current()?.symbol === '005930', 'failure retry must run a new analysis with the selected stock');
  assert.equal(p.calls.length, 2);
  assert.equal(p.elements.get('analysis-failure').hidden, true);
  assert.equal(p.elements.get('qa-submit').disabled, false);
});

test('an old request deadline cannot stop a newly selected stock or overwrite its completed result', {timeout: 3000}, async () => {
  const active = channel();
  const p = page((_url, options) => JSON.parse(options.body).symbol === '005930' ? new Promise(() => {}) : active.response);
  let oldSettled = false;
  const oldDone = p.start().then(() => { oldSettled = true; });
  await until(() => p.calls.length === 1, 'first request must begin');
  const oldTimers = p.clock.callbacks();
  assert.ok(oldTimers.length, 'the old request must have a finite watchdog');
  await p.clock.advance(1000);
  const activeDone = p.start('000660', 'SK하이닉스');
  active.send({stage: 'partial', result: result('000660', 'SK하이닉스'), pending_sources: pendingSources});
  await until(() => p.elements.get('result-name').textContent === 'SK하이닉스', 'new stock chart must display');
  // A timeout already queued by the browser may run even after clearing its timer.
  for (const run of oldTimers) run();
  await until(() => oldSettled, 'old request cleanup must finish');
  await oldDone;
  assert.equal(p.elements.get('result-name').textContent, 'SK하이닉스');
  assert.equal(p.elements.get('analysis-failure').hidden, true);
  assert.equal(p.elements.get('analyze-btn').disabled, true);
  assert.equal(p.calls[1].options.signal.aborted, false);

  const bundle = result('000660', 'SK하이닉스');
  bundle.completion = {status: 'complete', timed_out_sources: [], unavailable_sources: [], message: '자료 점검을 완료했습니다.'};
  active.send({stage: 'complete', result: bundle, pending_sources: []});
  active.close();
  await activeDone;
  await p.clock.advance(30000);
  assertStopped(p);
  assert.equal(p.current().symbol, '000660');
  assert.equal(p.elements.get('analysis-failure').hidden, true);
  assert.equal(p.elements.get('decision-headline').textContent, p.decision(bundle).headline);
});
