"""Durable single-owner monitor rules, crossing state, and notification outbox.

Use a persistent disk for SIGNAL_ALERT_DB on the always-running monitor service.
The Vercel application must proxy this service rather than open a local database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import uuid


def utcnow():
    return datetime.now(timezone.utc)


def stamp(now=None):
    return (now or utcnow()).astimezone(timezone.utc).isoformat()


class AlertStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('SIGNAL_ALERT_DB') or
                         Path(__file__).parent / 'alerts.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS monitor_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, name TEXT NOT NULL, market TEXT NOT NULL,
                    action TEXT NOT NULL, lower REAL, upper REAL, threshold REAL,
                    enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
                    inside INTEGER, event_seq INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT NOT NULL DEFAULT 'waiting', last_reason TEXT,
                    last_price REAL, last_quote_at TEXT, checked_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                    sequence INTEGER NOT NULL, symbol TEXT NOT NULL,
                    name TEXT NOT NULL, market TEXT NOT NULL, action TEXT NOT NULL,
                    price REAL NOT NULL, message TEXT NOT NULL, quote_timestamp TEXT, risk_reward REAL,
                    created_at TEXT NOT NULL, delivery_status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                    lease_token TEXT, lease_until TEXT, delivered_at TEXT, last_error TEXT,
                    UNIQUE(rule_id, revision, sequence)
                );
                CREATE TABLE IF NOT EXISTS monitor_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), last_cycle_at TEXT,
                    last_error TEXT, interval_seconds REAL NOT NULL DEFAULT 15
                );
                INSERT OR IGNORE INTO monitor_state(id) VALUES(1);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _rule(row):
        if row is None:
            return None
        result = dict(row)
        result['enabled'] = bool(result['enabled'])
        if result['inside'] is not None:
            result['inside'] = bool(result['inside'])
        return result

    def create(self, payload):
        from alert_engine import validate_rule
        from stock_lookup import resolve_stock
        clean = validate_rule(payload)
        identity = resolve_stock(payload.get('symbol', ''), payload.get('market', ''),
                                 payload.get('name', ''))
        enabled = payload.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('감시 활성 상태를 확인해 주세요.')
        at = stamp()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # A mobile retry must not create a second watcher or reset a paused
            # rule. The transaction also serializes concurrent identical POSTs.
            existing = db.execute('''SELECT * FROM monitor_rules WHERE symbol=? AND market=?
                AND action=? AND lower IS ? AND upper IS ? AND threshold IS ? ORDER BY id LIMIT 1''',
                (identity['symbol'], identity['market'], clean['action'], clean.get('lower'),
                 clean.get('upper'), clean.get('threshold'))).fetchone()
            if existing is not None:
                return self._rule(existing)
            if db.execute('SELECT COUNT(*) FROM monitor_rules').fetchone()[0] >= 100:
                raise ValueError('감시 조건은 최대 100개까지 저장할 수 있습니다.')
            cursor = db.execute('''INSERT INTO monitor_rules
                (symbol,name,market,action,lower,upper,threshold,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (identity['symbol'], identity['name'], identity['market'], clean['action'],
                 clean.get('lower'), clean.get('upper'), clean.get('threshold'),
                 int(enabled), at, at))
            row = db.execute('SELECT * FROM monitor_rules WHERE id=?',
                             (cursor.lastrowid,)).fetchone()
        return self._rule(row)

    def list(self):
        with self._connect() as db:
            return [self._rule(row) for row in db.execute(
                'SELECT * FROM monitor_rules ORDER BY id DESC')]

    def delete(self, rule_id):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            removed = db.execute('DELETE FROM monitor_rules WHERE id=?', (rule_id,)).rowcount
            db.execute("UPDATE monitor_events SET delivery_status='cancelled' "
                       "WHERE rule_id=? AND delivery_status='pending'", (rule_id,))
        return bool(removed)

    def update_enabled(self, rule_id, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('감시 활성 상태를 확인해 주세요.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM monitor_rules WHERE id=?', (rule_id,)).fetchone()
            if row is None:
                return None
            if bool(row['enabled']) != enabled:
                db.execute('''UPDATE monitor_rules SET enabled=?,revision=revision+1,
                           inside=NULL,last_status=?,last_reason=NULL,updated_at=? WHERE id=?''',
                           (int(enabled), 'waiting' if enabled else 'paused', stamp(), rule_id))
                db.execute("UPDATE monitor_events SET delivery_status='cancelled' "
                           "WHERE rule_id=? AND delivery_status='pending'", (rule_id,))
            result = db.execute('SELECT * FROM monitor_rules WHERE id=?', (rule_id,)).fetchone()
        return self._rule(result)

    def record_observation(self, rule_id, revision, evaluation, now=None):
        """Atomically persist state and enqueue one event per valid entry.

        Stale/blocked quotes keep the previous inside state. A stale worker
        snapshot cannot update a paused, deleted, or revised rule.
        """
        at = stamp(now)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM monitor_rules WHERE id=? AND revision=? AND enabled=1',
                             (rule_id, revision)).fetchone()
            if row is None:
                return None
            inside = evaluation.get('inside')
            next_inside = row['inside'] if inside is None else int(inside)
            fire = inside is True and row['inside'] != 1
            sequence = row['event_seq'] + int(fire)
            db.execute('''UPDATE monitor_rules SET inside=?,event_seq=?,last_status=?,
                last_reason=?,last_price=?,last_quote_at=?,checked_at=?,updated_at=? WHERE id=?''',
                (next_inside, sequence, evaluation['status'], evaluation.get('reason'),
                 evaluation.get('price'), evaluation.get('quote_timestamp'), at, at, rule_id))
            if not fire:
                return None
            cursor = db.execute('''INSERT INTO monitor_events
                (rule_id,revision,sequence,symbol,name,market,action,price,message,quote_timestamp,risk_reward,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (rule_id, revision, sequence, row['symbol'], row['name'], row['market'],
                 row['action'], evaluation['price'], evaluation['message'],
                 evaluation.get('quote_timestamp'), evaluation.get('risk_reward'), at))
            event = db.execute('SELECT * FROM monitor_events WHERE id=?',
                               (cursor.lastrowid,)).fetchone()
        return dict(event)

    def events(self, limit=50):
        limit = min(100, max(1, int(limit)))
        with self._connect() as db:
            # Never return internal lease credentials to the browser.
            return [{k: v for k, v in dict(row).items() if k not in {'lease_token', 'lease_until'}}
                    for row in db.execute('SELECT * FROM monitor_events ORDER BY id DESC LIMIT ?',
                                          (limit,))]

    def claim_delivery(self, now=None):
        """Lease an outbox entry; concurrent service workers cannot both claim it."""
        now = now or utcnow()
        at, until, token = stamp(now), stamp(now + timedelta(seconds=90)), uuid.uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire_pending(db, now)
            row = db.execute('''SELECT e.* FROM monitor_events e JOIN monitor_rules r
                ON r.id=e.rule_id AND r.revision=e.revision AND r.enabled=1
                WHERE e.delivery_status='pending' AND (e.next_attempt_at IS NULL OR e.next_attempt_at<=?)
                AND (e.lease_until IS NULL OR e.lease_until<=?) ORDER BY e.id LIMIT 1''',
                (at, at)).fetchone()
            if row is None:
                return None
            db.execute('UPDATE monitor_events SET lease_token=?,lease_until=? WHERE id=?',
                       (token, until, row['id']))
        result = dict(row)
        result['lease_token'] = token
        return result

    @staticmethod
    def _expire_pending(db, now):
        """A delayed message must never pose as a current trading opportunity."""
        expired = []
        for row in db.execute("SELECT id,quote_timestamp FROM monitor_events WHERE delivery_status='pending'"):
            try:
                quoted = datetime.fromisoformat(str(row['quote_timestamp']).replace('Z', '+00:00'))
                valid = quoted.utcoffset() is not None and 0 <= (now - quoted).total_seconds() <= 120
            except (ValueError, TypeError, OverflowError):
                valid = False
            if not valid:
                expired.append((row['id'],))
        if expired:
            db.executemany("UPDATE monitor_events SET delivery_status='expired',lease_token=NULL,"
                           "lease_until=NULL,last_error='시세 관측 시각 미검증 또는 120초 경과 · 전송하지 않음' WHERE id=?",
                           expired)

    def expire_pending(self, now=None):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire_pending(db, now or utcnow())

    def complete_delivery(self, event_id, token, *, success, now=None):
        now = now or utcnow()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM monitor_events WHERE id=? AND lease_token=? "
                             "AND delivery_status='pending'", (event_id, token)).fetchone()
            if row is None:
                return False
            attempts = row['attempts'] + 1
            retry = stamp(now + timedelta(seconds=min(3600, 15 * 2 ** min(attempts, 8))))
            db.execute('''UPDATE monitor_events SET delivery_status=?,attempts=?,
                next_attempt_at=?,lease_token=NULL,lease_until=NULL,delivered_at=?,last_error=? WHERE id=?''',
                ('sent' if success else 'pending', attempts, None if success else retry,
                 stamp(now) if success else None, None if success else '알림 전송 실패 · 재시도 대기', event_id))
        return True

    def heartbeat(self, *, now=None, error=None, interval_seconds=15):
        with self._connect() as db:
            db.execute('UPDATE monitor_state SET last_cycle_at=?,last_error=?,interval_seconds=? WHERE id=1',
                       (stamp(now), error, interval_seconds))

    def status(self, now=None):
        now = now or utcnow()
        with self._connect() as db:
            row = dict(db.execute('SELECT * FROM monitor_state WHERE id=1').fetchone())
            counts = db.execute('SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM monitor_rules').fetchone()
            pending = db.execute("SELECT COUNT(*) FROM monitor_events WHERE delivery_status='pending'").fetchone()[0]
        age = None
        if row['last_cycle_at']:
            age = (now - datetime.fromisoformat(row['last_cycle_at'])).total_seconds()
        return {'worker_running': age is not None and 0 <= age <= max(60, 3 * row['interval_seconds']),
                'last_cycle_at': row['last_cycle_at'], 'last_error': row['last_error'],
                'interval_seconds': row['interval_seconds'], 'rule_count': counts[0],
                'enabled_count': counts[1], 'pending_delivery': pending,
                'delivery_configured': bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'))}
