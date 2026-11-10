'use strict';
(() => {
  const get = id => document.getElementById(id);
  const nameInput = get('company-name'), codeInput = get('symbol'), marketInput = get('market');
  const list = get('stock-suggestions'), status = get('stock-search-status');
  const normalize = value => String(value || '').normalize('NFKC').replace(/\s+/gu, '').toLocaleLowerCase('en-US');
  const html = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // These common identities work immediately, including before the catalog loads.
  const seeds = [
    ['KOSPI','005930','삼성전자',['Samsung Electronics']],
    ['KOSPI','005935','삼성전자우',['삼성전자우선주']],
    ['KOSPI','000660','SK하이닉스',['하이닉스','SK Hynix']],
    ['KOSPI','035420','NAVER',['네이버']],
    ['KOSPI','035720','카카오',['Kakao']],
    ['KOSPI','005380','현대차',['현대자동차','Hyundai Motor']],
    ['KOSPI','000270','기아',['Kia']],
    ['KOSPI','373220','LG에너지솔루션',['LG Energy Solution']],
    ['KOSPI','068270','셀트리온',['Celltrion']],
    ['KOSDAQ','086520','에코프로',['EcoPro']],
    ['KOSDAQ','247540','에코프로비엠',['EcoPro BM']],
    ['KOSDAQ','196170','알테오젠',['Alteogen']],
    ['US','AAPL','Apple',['애플','Apple Inc','Apple Inc.']],
    ['US','NVDA','NVIDIA',['엔비디아','Nvidia Corporation']],
    ['US','MSFT','Microsoft',['마이크로소프트']],
    ['US','TSLA','Tesla',['테슬라']],
    ['US','AMZN','Amazon',['아마존']],
    ['US','GOOGL','Alphabet',['알파벳','Google','구글']]
  ].map(([market,symbol,name,aliases]) => ({market,symbol,name,aliases,source:'종목 목록'}));
  const catalog = new Map(seeds.map(item => [`${item.market}:${item.symbol}`, item]));
  const cache = new Map();
  let selected = seeds.find(item => item.symbol === codeInput.value && item.market === marketInput.value && normalize(item.name) === normalize(nameInput.value)) || null;
  let mode = nameInput.value.trim() ? 'name' : 'symbol';
  let revision = 0, controller = null, scheduled = null, pending = null, composing = false;
  let programmaticSymbol = false;
  let options = [], activeIndex = -1, blurTimer = null;
  const input = () => mode === 'name' ? nameInput : codeInput;
  const query = () => input().value.trim();
  const valid = item => item && ['KOSPI','KOSDAQ','US'].includes(item.market)
    && typeof item.name === 'string' && item.name.trim() && item.name.length <= 120
    && typeof item.symbol === 'string' && (item.market === 'US' ? /^[A-Z][A-Z0-9.-]{0,17}$/ : /^\d{6}$/).test(item.symbol);
  const aliases = item => [item.name,item.symbol,...(Array.isArray(item.aliases) ? item.aliases : [])].map(normalize);
  const matches = (item, value, kind) => kind === 'symbol' ? normalize(item.symbol) === normalize(value) : aliases(item).includes(normalize(value));
  function exact(items, value, kind) {
    const found = items.filter(item => matches(item, value, kind));
    return found.length === 1 ? found[0] : null;
  }
  function merge(...groups) {
    const unique = new Map();
    for (const item of groups.flat()) if (valid(item)) unique.set(`${item.market}:${item.symbol}`, item);
    return [...unique.values()];
  }
  function local(value, kind) {
    const key = normalize(value);
    if (!key) return [];
    return [...catalog.values()].filter(item => kind === 'symbol' ? normalize(item.symbol).startsWith(key) : aliases(item).some(alias => alias.includes(key))).slice(0, 8);
  }
  function feedback(message, error = false) {
    status.textContent = message;
    status.dataset.state = error ? 'error' : 'ready';
  }
  function close() {
    list.hidden = true;
    activeIndex = -1;
    for (const field of [nameInput,codeInput]) {
      field.setAttribute('aria-expanded','false');
      field.removeAttribute('aria-activedescendant');
    }
  }
  function render(items) {
    options = items;
    activeIndex = -1;
    if (!items.length) { close(); return; }
    list.innerHTML = items.map((item,index) => `<li id="stock-option-${index}" role="option" aria-selected="false" data-stock-index="${index}"><strong>${html(item.name)}</strong><small>${html(item.market)} · ${html(item.symbol)}</small></li>`).join('');
    list.hidden = false;
    input().setAttribute('aria-expanded','true');
  }
  function cancel() {
    ++revision;
    clearTimeout(scheduled);
    scheduled = null;
    controller?.abort();
    controller = null;
    pending = null;
  }
  function invalidate() {
    if (typeof invalidateCurrentSelection === 'function') invalidateCurrentSelection();
  }
  function notifyCodeChange() {
    // DART uses the same inputs and must discard the previous company's results.
    if (typeof Event === 'function') marketInput.dispatchEvent(new Event('input',{bubbles:true}));
  }
  function choose(item) {
    cancel();
    selected = item;
    marketInput.value = item.market;
    codeInput.value = item.symbol;
    nameInput.value = item.name;
    close();
    feedback(`${item.name} · ${item.market} ${item.symbol}`);
    nameInput.removeAttribute('aria-invalid');
    codeInput.removeAttribute('aria-invalid');
    notifyCodeChange();
    return item;
  }
  function unresolved(message, error = false) {
    selected = null;
    if (mode === 'name') codeInput.value = '';
    feedback(message,error);
    notifyCodeChange();
  }
  async function search(value, kind, version) {
    const key = `${kind}:${normalize(value)}`;
    if (pending?.key === key && pending.version === version) return pending.promise;
    if (cache.has(key)) return cache.get(key);
    controller?.abort();
    const requestController = controller = new AbortController();
    let timeout, timedOut = false;
    // Covers both the HTTP response and its JSON body, even if a host ignores abort.
    const deadline = new Promise((resolve,reject) => {
      timeout = setTimeout(() => { timedOut = true; requestController.abort(); },4500);
      requestController.signal.addEventListener('abort',() => {
        const error = new Error(timedOut ? '검색 응답이 늦어지고 있습니다. 다시 검색해 주세요.' : '검색이 변경되었습니다.');
        error.name = timedOut ? 'TimeoutError' : 'AbortError'; reject(error);
      },{once:true});
    });
    const request = (async () => {
      const response = await fetch(`/api/stocks/search?q=${encodeURIComponent(value)}`,{signal:requestController.signal});
      if (!response.ok) throw new Error('종목을 확인하지 못했습니다. 잠시 후 다시 검색해 주세요.');
      const data = await response.json();
      if (!Array.isArray(data.items)) throw new Error('종목을 확인하지 못했습니다. 다시 검색해 주세요.');
      return merge(local(value,kind),data.items).slice(0,8);
    })();
    const promise = Promise.race([request,deadline]).then(items => {
      if (version === revision && !requestController.signal.aborted) {
        cache.set(key,items);
        if (cache.size > 128) cache.delete(cache.keys().next().value);
      }
      return items;
    }).finally(() => {
      clearTimeout(timeout);
      if (controller === requestController) controller = null;
      if (pending?.promise === promise) pending = null;
    });
    pending = {key,version,promise};
    return promise;
  }
  async function updateRemote(value, kind, version) {
    try {
      const items = await search(value,kind,version);
      if (version !== revision || composing || mode !== kind || query() !== value) return null;
      const match = exact(items,value,kind);
      if (match) return choose(match);
      render(items);
      unresolved(items.length ? '아래 목록에서 정확한 종목을 선택하세요.' : '일치하는 종목이 없습니다. 기업명이나 종목코드를 확인하세요.',true);
    } catch (error) {
      if (version !== revision || error.name === 'AbortError') return null;
      feedback(error.message,true);
    }
    return null;
  }
  function edited(kind) {
    cancel();
    programmaticSymbol = false;
    mode = kind;
    selected = null;
    invalidate();
    if (kind === 'symbol') nameInput.value = '';
    if (composing) { close(); feedback('기업명 입력 중…'); return; }
    const value = query();
    if (!value) { options = []; close(); unresolved('기업명이나 종목코드를 입력하세요.'); return; }
    const items = local(value,kind), match = exact(items,value,kind);
    if (match) { choose(match); return; }
    render(items);
    unresolved(items.length ? '아래 목록에서 정확한 종목을 선택하세요.' : '종목 확인 중…');
    const version = revision;
    scheduled = setTimeout(() => { scheduled = null; updateRemote(value,kind,version); },200);
  }
  nameInput.addEventListener('compositionstart',() => { composing = true; programmaticSymbol = false; cancel(); selected = null; codeInput.value = ''; invalidate(); close(); });
  nameInput.addEventListener('compositionend',() => { composing = false; edited('name'); });
  nameInput.addEventListener('input',event => { if (event.isComposing) composing = true; edited('name'); });
  codeInput.addEventListener('input',() => edited('symbol'));
  codeInput.addEventListener('blur',() => {
    if (!selected && mode === 'symbol' && query() && !composing) {
      clearTimeout(scheduled);
      scheduled = null;
      updateRemote(query(),mode,revision);
    }
  });
  marketInput.addEventListener('change',() => {
    cancel(); invalidate(); programmaticSymbol = false; selected = null; nameInput.value = ''; codeInput.value = ''; mode = 'name';
    options = []; close(); feedback('선택한 시장의 기업명 또는 종목코드를 입력하세요.');
  });
  for (const field of [nameInput,codeInput]) {
    field.addEventListener('focus',() => { clearTimeout(blurTimer); if (!selected && options.length) render(options); });
    field.addEventListener('blur',() => { blurTimer = setTimeout(close,150); });
    field.addEventListener('keydown',event => {
      if (event.isComposing || composing || event.keyCode === 229) { if (event.key === 'Enter') event.preventDefault(); return; }
      if (event.key === 'Escape') { close(); return; }
      if (event.key === 'Enter' && !list.hidden && activeIndex >= 0) { event.preventDefault(); choose(options[activeIndex]); return; }
      if (!['ArrowDown','ArrowUp'].includes(event.key) || !options.length) return;
      event.preventDefault();
      if (list.hidden) render(options);
      activeIndex = (activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      list.querySelectorAll('[role="option"]').forEach((option,index) => option.setAttribute('aria-selected',String(index === activeIndex)));
      field.setAttribute('aria-activedescendant',`stock-option-${activeIndex}`);
    });
  }
  list.addEventListener('pointerdown',event => event.preventDefault());
  list.addEventListener('click',event => {
    const option = event.target.closest('[data-stock-index]');
    if (option && options[Number(option.dataset.stockIndex)]) choose(options[Number(option.dataset.stockIndex)]);
  });
  globalThis.stockSearchSelectionChanged = (market,symbol,name) => {
    cancel(); invalidate();
    const known = catalog.get(`${market}:${String(symbol).toUpperCase()}`);
    mode = 'name';
    if (known) choose(known);
    else {
      marketInput.value = market; codeInput.value = String(symbol).toUpperCase(); nameInput.value = name || '';
      selected = null; close(); feedback('종목을 확인한 뒤 분석합니다.');
      mode = 'symbol'; programmaticSymbol = true;
    }
  };
  globalThis.resolveCurrentStockSelection = async () => {
    if (composing) { feedback('기업명 입력을 마친 뒤 분석해 주세요.',true); return null; }
    if (selected && selected.market === marketInput.value && selected.symbol === codeInput.value.trim().toUpperCase() && selected.name === nameInput.value.trim()) return selected;
    cancel();
    // Programmatic form edits are also validated; a stale name/code pair cannot pass.
    mode = programmaticSymbol ? 'symbol' : nameInput.value.trim() ? 'name' : 'symbol';
    const value = query();
    if (!value) { unresolved('기업명이나 종목코드를 입력하세요.',true); input().focus(); return null; }
    const items = local(value,mode), match = exact(items,value,mode);
    if (match) return choose(match);
    unresolved('종목 확인 중…');
    const version = revision, kind = mode;
    const matchRemote = await updateRemote(value,kind,version);
    if (matchRemote) return matchRemote;
    if (version === revision) { input().setAttribute('aria-invalid','true'); input().focus(); }
    return null;
  };
  // Catalog completion only enriches later lookups; it never overwrites typed input.
  const catalogController = new AbortController();
  const catalogTimeout = setTimeout(() => catalogController.abort(),4500);
  fetch('/api/stocks/catalog',{signal:catalogController.signal}).then(response => response.ok ? response.json() : null).then(data => {
    for (const item of data?.items || []) if (valid(item)) {
      const key = `${item.market}:${item.symbol}`, old = catalog.get(key);
      catalog.set(key,{...item,aliases:[...(old?.aliases || []),...(Array.isArray(item.aliases) ? item.aliases : [])]});
    }
  }).catch(() => {}).finally(() => clearTimeout(catalogTimeout));
})();
