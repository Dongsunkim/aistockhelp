"""Polling monitor and durable notification delivery; never started on import.

The worker must run on an always-on host with persistent SIGNAL_ALERT_DB. Polls
are snapshots and cannot guarantee that a brief price crossing is observed.
Telegram delivery is at least once: a crash after remote acceptance but before
the local commit can duplicate a message. Stored rule crossings are deduplicated.
"""
from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import math
import os
import threading

import requests

from alert_engine import evaluate
from alert_store import AlertStore


def fetch_official_quote(symbol, market):
    from quotes import fetch_monitor_quote
    quote = dict(fetch_monitor_quote(symbol, market))
    # The official REST call is bound to the requested symbol. Preserve any
    # explicit provider identity so an unexpected mismatch still fails closed.
    quote.setdefault('symbol', symbol)
    quote.setdefault('market', market)
    return quote


def send_telegram(event):
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise ValueError('텔레그램 수신 설정이 필요합니다.')
    message = (f"{event['name']} ({event['symbol']})\n{event['message']}\n"
               f"현재 관측가: {event['price']:,.2f}\n시세 관측 시각: {event['quote_timestamp']}\n"
               f"알림 번호: {event['id']} · 자동 주문 없음")
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat, 'text': message}, timeout=10)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('ok') is not True:
        raise ValueError('텔레그램 전송을 확인하지 못했습니다.')


class MonitorWorker:
    def __init__(self, store=None, quote_provider=None, evidence_provider=None,
                 send_provider=None, interval_seconds=15):
        self.store = store or AlertStore()
        self.quote_provider = quote_provider or fetch_official_quote
        self.evidence_provider = evidence_provider
        self.send_provider = send_provider
        interval = float(interval_seconds)
        if not math.isfinite(interval):
            raise ValueError('감시 간격은 유한한 숫자여야 합니다.')
        self.interval_seconds = max(5, interval)
        self._evidence = {}
        self._pending_evidence = {}
        self._evidence_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='monitor-evidence')
        self._cycle_lock = threading.Lock()

    def _research(self, rule, now):
        if self.evidence_provider is None:
            return None
        key = (rule['symbol'], rule['market'], rule['name'])
        pending = self._pending_evidence.get(key)
        if pending is not None and pending.done():
            self._pending_evidence.pop(key, None)
            try:
                evidence = pending.result()
                if not isinstance(evidence, dict):
                    raise ValueError('판단 근거 응답을 확인할 수 없습니다.')
                # checked_at remains the original server verification time.
                self._evidence[key] = (now, evidence)
            except Exception:
                self._evidence[key] = (now, {'eligible': False})
        cached = self._evidence.get(key)
        if cached:
            age = (now - cached[0]).total_seconds()
            if 0 <= age < (300 if cached[1].get('eligible') is True else 30):
                return cached[1]
        # One pending job per stock and no unbounded task queue. Slow research
        # cannot hold up fresh price checks or notifications for other stocks.
        if key not in self._pending_evidence and len(self._pending_evidence) < 4:
            self._pending_evidence[key] = self._evidence_pool.submit(
                self.evidence_provider, rule['symbol'], rule['market'], rule['name'])
        # Bounded by the maximum number of persisted rules.
        if len(self._evidence) > 100:
            self._evidence.pop(next(iter(self._evidence)))
        return cached[1] if cached else None

    def close(self):
        # Active provider calls are bounded by their own HTTP deadlines; Python
        # cannot forcibly cancel a request already running in a thread.
        self._evidence_pool.shutdown(wait=False, cancel_futures=True)

    def _deliver(self, now=None):
        clock = lambda: now or datetime.now(timezone.utc)
        self.store.expire_pending(clock())
        sender = self.send_provider
        if sender is None:
            if not (os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')):
                return
            sender = send_telegram
        # At most two bounded sends per cycle keep quote checks responsive.
        for _ in range(2):
            event = self.store.claim_delivery(clock())
            if event is None:
                return
            try:
                outcome = sender(event)
                if outcome is False:
                    raise ValueError('전송 미확인')
            except Exception:
                # Do not expose exception URLs, which could contain bot tokens.
                self.store.complete_delivery(event['id'], event['lease_token'], success=False, now=clock())
            else:
                self.store.complete_delivery(event['id'], event['lease_token'], success=True, now=clock())

    def run_once(self, now=None):
        """Collect independently, then atomically evaluate current rule revisions."""
        if not self._cycle_lock.acquire(blocking=False):
            return {'events': 0, 'skipped': True}
        try:
            fixed_now = now
            clock = lambda: fixed_now or datetime.now(timezone.utc)
            generated, failures = 0, 0
            quotes = {}
            rules = self.store.list()
            active_research = {(r['symbol'], r['market'], r['name']) for r in rules
                               if r['enabled'] and r['action'] == 'buy_zone'}
            # Deleted/paused stocks must not occupy refresh slots permanently.
            # Running requests remain tracked until done, keeping the queue bounded.
            for key, future in list(self._pending_evidence.items()):
                if key not in active_research and (future.done() or future.cancel()):
                    self._pending_evidence.pop(key, None)
            for key in list(self._evidence):
                if key not in active_research:
                    self._evidence.pop(key, None)
            for rule in rules:
                if not rule['enabled']:
                    continue
                try:
                    key = (rule['symbol'], rule['market'])
                    if key not in quotes:
                        try:
                            quotes[key] = self.quote_provider(*key)
                        except Exception:
                            quotes[key] = None
                    quote = quotes[key]
                    if quote is None:
                        raise ValueError('시세 응답 없음')
                    evidence = self._research(rule, clock()) if rule['action'] == 'buy_zone' else None
                    result = evaluate(rule, quote, evidence, now=clock())
                except Exception:
                    failures += 1
                    result = {'status': 'blocked', 'inside': None, 'price': None,
                              'quote_timestamp': None,
                              'reason': '자료 연결 실패 · 다음 주기에 다시 확인합니다.', 'message': ''}
                if self.store.record_observation(rule['id'], rule['revision'], result, clock()):
                    generated += 1
            self._deliver(fixed_now)
            # A heartbeat means a completed polling cycle, not module startup.
            self.store.heartbeat(now=clock(), interval_seconds=self.interval_seconds,
                                 error='일부 종목 자료 연결 실패' if failures else None)
            return {'events': generated, 'failures': failures, 'skipped': False}
        finally:
            self._cycle_lock.release()

    def run(self, stop_event):
        try:
            while not stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    # A failed cycle does not refresh the heartbeat. It will become
                    # visibly stale if storage/provider setup stays broken.
                    pass
                stop_event.wait(self.interval_seconds)
        finally:
            self.close()
