const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

function page() {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    textContent: '', innerHTML: '', hidden: false, dataset: {},
    addEventListener() {}, querySelectorAll() { return []; }
  }]));
  const context = vm.createContext({
    document: {
      getElementById(id) {
        assert.ok(elements.has(id), `Element ${id} exists in the actual HTML`);
        return elements.get(id);
      },
      querySelectorAll() { return []; }
    },
    fetch: () => new Promise(() => {}), URL, console
  });
  for (const script of ['decision.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(root, 'static', script), 'utf8'), context);
  }
  return {elements, render(result) { context.result = result; vm.runInContext('drawResult(result)', context); }};
}

function result() {
  return {
    market: 'KOSPI', symbol: '005930', company_name: '삼성전자',
    meta: {fetched_at: '2026-11-06T00:00:00Z', provider: 'test'},
    analysis: {
      date: '2026-11-05', close: 70000, status: '관망', confirmed: false,
      technical_ok: false, data_age_days: 1,
      levels: {entry_low: 68000, entry_high: 69000, stop: 65000, target_resistance: 80000, rr: 2.75},
      conditions: [{id: 'pullback', name: '매수 구간', pass: false, detail: '구간 위'}],
      evidence_checks: [{name: '종가 데이터 최근 5일 이내', pass: true}], method: {entry: 'test', caveat: 'test'}, red_flags: []
    },
    data_quality: {
      price_levels_eligible: true, price_level_blockers: [], verified_for_trade: false,
      status: '데이터 검증 미완료 · 관망', warnings: ['재무제표 핵심 3개 계정 조회 미완료']
    },
    news: [], filings: [], financials: [],
    chart: [{date: '2026-11-04', close: 69500}, {date: '2026-11-05', close: 70000}]
  };
}

test('valid technical prices render despite missing financial verification', () => {
  const p = page();
  p.render(result());
  assert.equal(p.elements.get('entry-zone').textContent, '68,000 ~ 69,000 KRW');
  assert.equal(p.elements.get('stop-price').textContent, '65,000');
  assert.equal(p.elements.get('target-price').textContent, '80,000');
  assert.equal(p.elements.get('rr-value').textContent, '1 : 2.75');
  assert.match(p.elements.get('price-level-status').textContent, /기술적 계산값/);
  assert.match(p.elements.get('decision-headline').textContent, /서두르기 어렵/);
  assert.match(p.elements.get('quality-warnings').innerHTML, /재무/);
  assert.match(p.elements.get('price-chart').innerHTML, /<rect /);
  assert.equal(p.elements.get('zone-legend').hidden, false);
});

test('switching to invalid data removes previous prices and chart overlays and explains why', () => {
  const p = page();
  p.render(result());
  const blocked = result();
  blocked.data_quality.price_levels_eligible = false;
  blocked.data_quality.price_level_blockers = ['최신 확정 종가가 아닙니다.'];
  p.render(blocked);
  assert.equal(p.elements.get('entry-zone').textContent, '유효한 가격대 없음');
  assert.equal(p.elements.get('entry-caption').textContent, '최신 확정 종가가 아닙니다.');
  for (const id of ['stop-price', 'target-price', 'rr-value']) {
    assert.equal(p.elements.get(id).textContent, '산출 불가');
  }
  assert.doesNotMatch(p.elements.get('price-chart').innerHTML, /<rect /);
  assert.equal(p.elements.get('zone-legend').hidden, true);
  assert.equal(p.elements.get('stop-legend').hidden, true);
  assert.match(p.elements.get('decision-headline').textContent, /판단하기 어렵/);
});

test('missing quality metadata cannot expose prices', () => {
  const p = page();
  const r = result();
  delete r.data_quality;
  p.render(r);
  assert.equal(p.elements.get('entry-zone').textContent, '유효한 가격대 없음');
  assert.match(p.elements.get('entry-caption').textContent, /확인할 수 없어/);
  assert.doesNotMatch(p.elements.get('price-chart').innerHTML, /<rect /);
});
