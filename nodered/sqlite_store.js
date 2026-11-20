'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

const MAX_QUEUE = 100;
const MAX_DEAD_LETTERS = 100;
const MAX_DEDUP_KEYS = 500;
const RETRY_DELAYS = [1000, 2000, 4000];
const WATCHDOG_MS = 15000;
const LEGACY_MIGRATION_KEY = 'legacy_context_imported_v1';

function resolveDbPath() {
  if (process.env.QC_DB_PATH) return path.resolve(process.env.QC_DB_PATH);
  const dataDir = process.env.QC_DATA_DIR
    ? path.resolve(process.env.QC_DATA_DIR)
    : path.join(process.cwd(), 'data');
  return path.join(dataDir, 'qc-queue.sqlite');
}

function nowIso(value) {
  return new Date(value).toISOString();
}

function asString(value) {
  return value == null ? '' : String(value);
}

function asInteger(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : fallback;
}

function rowToQueueItem(row) {
  if (!row) return null;
  return {
    id: row.id,
    type: row.type,
    correlationId: row.correlation_id || '',
    url: row.url,
    body: JSON.parse(row.body_json),
    createdAt: row.created_at || '',
    attempts: row.attempts || 0,
    nextAt: row.next_at || 0,
    status: row.status,
    sentAt: row.sent_at == null ? undefined : row.sent_at,
    lastStatus: row.last_status || '',
    lastErrorCode: row.last_error_code || '',
  };
}

function rowToDeadLetter(row) {
  if (!row) return null;
  return {
    id: row.queue_id,
    type: row.type,
    correlationId: row.correlation_id || '',
    url: row.url,
    body: JSON.parse(row.body_json),
    createdAt: row.created_at || '',
    attempts: row.attempts || 0,
    nextAt: row.next_at || 0,
    status: row.status || '',
    sentAt: row.sent_at == null ? undefined : row.sent_at,
    lastStatus: row.last_status || '',
    lastErrorCode: row.last_error_code || '',
    errorCode: row.error_code || '',
    failedAt: row.failed_at || '',
  };
}

class SqliteQueueStore {
  constructor(options = {}) {
    this.dbPath = path.resolve(options.dbPath || resolveDbPath());
    this.watchdogMs = asInteger(options.watchdogMs, WATCHDOG_MS);
    this._bootstrapped = false;
    fs.mkdirSync(path.dirname(this.dbPath), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(this.dbPath, { timeout: 5000 });
    this.db.exec(`
      PRAGMA journal_mode = WAL;
      PRAGMA synchronous = FULL;
      PRAGMA busy_timeout = 5000;
      PRAGMA foreign_keys = ON;

      CREATE TABLE IF NOT EXISTS queue (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL,
        correlation_id TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        body_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENDING')),
        sent_at INTEGER,
        last_status TEXT NOT NULL DEFAULT '',
        last_error_code TEXT NOT NULL DEFAULT '',
        enqueued_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_queue_status_next
        ON queue(status, next_at, seq);

      CREATE TABLE IF NOT EXISTS dead_letters (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        queue_id TEXT NOT NULL,
        type TEXT NOT NULL,
        correlation_id TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        body_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '',
        sent_at INTEGER,
        last_status TEXT NOT NULL DEFAULT '',
        last_error_code TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        failed_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS dedup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        dedup_key TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE (kind, dedup_key)
      );
      CREATE INDEX IF NOT EXISTS idx_dedup_kind_id ON dedup(kind, id);

      CREATE TABLE IF NOT EXISTS meta (
        meta_key TEXT PRIMARY KEY,
        meta_value TEXT NOT NULL
      );
    `);
    try {
      fs.chmodSync(this.dbPath, 0o600);
    } catch (_) {
      // Windows and some mounted filesystems do not expose POSIX permissions.
    }

    this._queueCountStatement = this.db.prepare('SELECT COUNT(*) AS count FROM queue');
    this._deadCountStatement = this.db.prepare('SELECT COUNT(*) AS count FROM dead_letters');
    this._metaGetStatement = this.db.prepare('SELECT meta_value FROM meta WHERE meta_key = ?');
    this._metaSetStatement = this.db.prepare(
      'INSERT INTO meta(meta_key, meta_value) VALUES (?, ?) ' +
      'ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value'
    );
  }

  close() {
    if (this.db) {
      this.db.close();
      this.db = null;
    }
  }

  _transaction(work) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = work();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      try {
        this.db.exec('ROLLBACK');
      } catch (_) {
        // Preserve the original error if SQLite already rolled back.
      }
      throw error;
    }
  }

  _queueCount() {
    return Number(this._queueCountStatement.get().count || 0);
  }

  _deadCount() {
    return Number(this._deadCountStatement.get().count || 0);
  }

  _queueRow(id) {
    return this.db.prepare('SELECT * FROM queue WHERE id = ?').get(id);
  }

  _findInflight(id) {
    if (id) {
      return this.db.prepare(
        "SELECT * FROM queue WHERE id = ? AND status = 'SENDING' ORDER BY seq LIMIT 1"
      ).get(id) || null;
    }
    return this.db.prepare(
      "SELECT * FROM queue WHERE status = 'SENDING' ORDER BY seq LIMIT 1"
    ).get() || null;
  }

  _pruneDedup(kind) {
    this.db.prepare(
      'DELETE FROM dedup WHERE kind = ? AND id NOT IN (' +
      'SELECT id FROM dedup WHERE kind = ? ORDER BY id DESC LIMIT ?)'
    ).run(kind, kind, MAX_DEDUP_KEYS);
  }

  _pruneDeadLetters() {
    this.db.prepare(
      'DELETE FROM dead_letters WHERE seq NOT IN (' +
      'SELECT seq FROM dead_letters ORDER BY seq DESC LIMIT ?)'
    ).run(MAX_DEAD_LETTERS);
  }

  _moveToDead(row, errorCode, reason, attempts, now) {
    const failedAt = nowIso(now);
    this.db.prepare(`
      INSERT INTO dead_letters(
        queue_id, type, correlation_id, url, body_json, created_at, attempts,
        next_at, status, sent_at, last_status, last_error_code, error_code, failed_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      row.id,
      row.type,
      row.correlation_id || '',
      row.url,
      row.body_json,
      row.created_at || '',
      asInteger(attempts, row.attempts || 0),
      row.next_at || 0,
      row.status || '',
      row.sent_at == null ? null : row.sent_at,
      reason,
      errorCode,
      errorCode,
      failedAt
    );
    this.db.prepare('DELETE FROM queue WHERE id = ?').run(row.id);
    this._pruneDeadLetters();
    return {
      id: row.id,
      type: row.type,
      correlationId: row.correlation_id || '',
      url: row.url,
      body: JSON.parse(row.body_json),
      createdAt: row.created_at || '',
      attempts: asInteger(attempts, row.attempts || 0),
      nextAt: row.next_at || 0,
      status: row.status || '',
      sentAt: row.sent_at == null ? undefined : row.sent_at,
      lastStatus: reason,
      lastErrorCode: errorCode,
      errorCode,
      failedAt,
    };
  }

  bootstrap(flow) {
    if (this._bootstrapped) return this;
    const legacy = {
      buf: flow.get('buf') || [],
      dead: flow.get('dead') || [],
      ngSeen: flow.get('ngSeen') || {},
      ngSnSeen: flow.get('ngSnSeen') || {},
      doneSeen: flow.get('doneSeen') || {},
    };
    const migration = this.migrateLegacy(legacy);
    if (migration.migrated) {
      flow.set('buf', []);
      flow.set('dead', []);
      flow.set('ngSeen', {});
      flow.set('ngSnSeen', {});
      flow.set('doneSeen', {});
    }
    this._bootstrapped = true;
    return this;
  }

  migrateLegacy(legacy = {}) {
    return this._transaction(() => {
      const marker = this._metaGetStatement.get(LEGACY_MIGRATION_KEY);
      if (marker) return { migrated: false, queue: 0, dead: 0, dedup: 0 };

      let queueImported = 0;
      let deadImported = 0;
      let dedupImported = 0;
      let sequence = 0;
      const insertQueue = this.db.prepare(`
        INSERT OR IGNORE INTO queue(
          id, type, correlation_id, url, body_json, created_at, attempts,
          next_at, status, sent_at, last_status, last_error_code, enqueued_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `);
      const legacyItems = Array.isArray(legacy.buf) ? legacy.buf : [];
      for (const item of legacyItems) {
        if (!item || !item.id || !item.type || !item.url) continue;
        let bodyJson;
        try {
          bodyJson = JSON.stringify(item.body);
        } catch (_) {
          continue;
        }
        if (!bodyJson) continue;
        const oldStatus = asString(item.status).toUpperCase();
        if (oldStatus === 'SUCCESS') continue;
        if (oldStatus === 'FAILED') {
          this.db.prepare(`
            INSERT INTO dead_letters(
              queue_id, type, correlation_id, url, body_json, created_at, attempts,
              next_at, status, sent_at, last_status, last_error_code, error_code, failed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          `).run(
            item.id,
            item.type,
            asString(item.correlationId),
            item.url,
            bodyJson,
            asString(item.createdAt),
            asInteger(item.attempts),
            asInteger(item.nextAt),
            oldStatus,
            item.sentAt == null ? null : asInteger(item.sentAt),
            asString(item.lastStatus),
            asString(item.lastErrorCode || item.errorCode),
            asString(item.errorCode),
            asString(item.failedAt) || nowIso(Date.now())
          );
          deadImported += 1;
          continue;
        }
        const status = oldStatus === 'SENDING' ? 'SENDING' : 'PENDING';
        const result = insertQueue.run(
          item.id,
          item.type,
          asString(item.correlationId),
          item.url,
          bodyJson,
          asString(item.createdAt),
          asInteger(item.attempts),
          asInteger(item.nextAt),
          status,
          item.sentAt == null ? null : asInteger(item.sentAt),
          asString(item.lastStatus),
          asString(item.lastErrorCode),
          Date.now() + sequence
        );
        sequence += 1;
        if (result.changes) queueImported += 1;
      }

      while (this._queueCount() > MAX_QUEUE) {
        const victim = this.db.prepare(
          "SELECT id FROM queue WHERE status = 'PENDING' ORDER BY seq LIMIT 1"
        ).get() || this.db.prepare('SELECT id FROM queue ORDER BY seq LIMIT 1').get();
        if (!victim) break;
        this.db.prepare('DELETE FROM queue WHERE id = ?').run(victim.id);
      }

      const insertDedup = this.db.prepare(
        'INSERT OR IGNORE INTO dedup(kind, dedup_key, created_at) VALUES (?, ?, ?)'
      );
      const dedupGroups = [
        ['ng_fp', legacy.ngSeen],
        ['ng_sn', legacy.ngSnSeen],
        ['done', legacy.doneSeen],
      ];
      let dedupOrder = 0;
      for (const [kind, values] of dedupGroups) {
        if (!values || typeof values !== 'object' || Array.isArray(values)) continue;
        for (const key of Object.keys(values)) {
          const result = insertDedup.run(kind, key, Date.now() + dedupOrder);
          dedupOrder += 1;
          if (result.changes) dedupImported += 1;
        }
        this._pruneDedup(kind);
      }

      if (Array.isArray(legacy.dead)) {
        const insertDead = this.db.prepare(`
          INSERT INTO dead_letters(
            queue_id, type, correlation_id, url, body_json, created_at, attempts,
            next_at, status, sent_at, last_status, last_error_code, error_code, failed_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        `);
        for (const item of legacy.dead) {
          if (!item || !item.id || !item.type) continue;
          let bodyJson;
          try {
            bodyJson = JSON.stringify(item.body || {});
          } catch (_) {
            continue;
          }
          insertDead.run(
            item.id,
            item.type,
            asString(item.correlationId),
            asString(item.url),
            bodyJson,
            asString(item.createdAt),
            asInteger(item.attempts),
            asInteger(item.nextAt),
            asString(item.status),
            item.sentAt == null ? null : asInteger(item.sentAt),
            asString(item.lastStatus),
            asString(item.lastErrorCode || item.errorCode),
            asString(item.errorCode),
            asString(item.failedAt) || nowIso(Date.now())
          );
          deadImported += 1;
        }
        this._pruneDeadLetters();
      }

      this._metaSetStatement.run(LEGACY_MIGRATION_KEY, nowIso(Date.now()));
      return { migrated: true, queue: queueImported, dead: deadImported, dedup: dedupImported };
    });
  }

  hasDedup(kind, key) {
    return Boolean(this.db.prepare(
      'SELECT 1 AS found FROM dedup WHERE kind = ? AND dedup_key = ?'
    ).get(kind, key));
  }

  enqueue({ id, type, correlationId, url, body, createdAt, dedupKeys = [], now = Date.now() }) {
    let bodyJson;
    try {
      bodyJson = JSON.stringify(body);
    } catch (error) {
      throw new Error(`cannot serialize queue body: ${error.message}`);
    }
    if (!bodyJson) throw new Error('cannot serialize queue body');
    return this._transaction(() => {
      if (this._queueRow(id)) {
        return { enqueued: false, duplicate: true, reason: 'queue-id', dropped: [], queueLength: this._queueCount() };
      }
      for (const entry of dedupKeys) {
        if (this.hasDedup(entry.kind, entry.key)) {
          return { enqueued: false, duplicate: true, reason: 'dedup', dropped: [], queueLength: this._queueCount() };
        }
      }

      const dropped = [];
      while (this._queueCount() >= MAX_QUEUE) {
        const victim = this.db.prepare(
          "SELECT id FROM queue WHERE status = 'PENDING' ORDER BY seq LIMIT 1"
        ).get() || this.db.prepare('SELECT id FROM queue ORDER BY seq LIMIT 1').get();
        if (!victim) break;
        this.db.prepare('DELETE FROM queue WHERE id = ?').run(victim.id);
        dropped.push(victim.id);
      }

      const insertDedup = this.db.prepare(
        'INSERT INTO dedup(kind, dedup_key, created_at) VALUES (?, ?, ?)'
      );
      for (const entry of dedupKeys) {
        insertDedup.run(entry.kind, entry.key, now);
      }
      this.db.prepare(`
        INSERT INTO queue(
          id, type, correlation_id, url, body_json, created_at, attempts,
          next_at, status, enqueued_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'PENDING', ?)
      `).run(id, type, correlationId || '', url, bodyJson, createdAt || '', now);
      for (const entry of dedupKeys) this._pruneDedup(entry.kind);
      return { enqueued: true, duplicate: false, reason: '', dropped, queueLength: this._queueCount() };
    });
  }

  getInflight(id = '', correlationId = '') {
    const row = this._findInflight(id);
    if (!row) return null;
    if (correlationId && row.correlation_id && row.correlation_id !== correlationId) return null;
    return rowToQueueItem(row);
  }

  claimNext(now = Date.now()) {
    return this._transaction(() => {
      const inflight = this._findInflight('');
      if (inflight) {
        const sentAt = asInteger(inflight.sent_at);
        if (!sentAt || now - sentAt > this.watchdogMs) {
          const attempts = asInteger(inflight.attempts) + 1;
          if (attempts > RETRY_DELAYS.length) {
            const dead = this._moveToDead(
              inflight, 'WATCHDOG_TIMEOUT', 'watchdog-timeout', attempts, now
            );
            return { state: 'dead', item: dead, queueLength: this._queueCount(), queueTotal: this._queueCount() };
          }
          const delay = RETRY_DELAYS[attempts - 1];
          this.db.prepare(`
            UPDATE queue
            SET attempts = ?, status = 'PENDING', next_at = ?, sent_at = NULL,
                last_status = 'watchdog-timeout', last_error_code = 'WATCHDOG_TIMEOUT'
            WHERE id = ? AND status = 'SENDING'
          `).run(attempts, now + delay, inflight.id);
          const updated = this._queueRow(inflight.id);
          return {
            state: 'watchdog-retry',
            item: rowToQueueItem(updated),
            delay,
            queueLength: this._queueCount(),
            queueTotal: this._queueCount(),
          };
        }
        return { state: 'inflight', item: rowToQueueItem(inflight), queueLength: this._queueCount(), queueTotal: this._queueCount() };
      }

      const pending = this.db.prepare(
        "SELECT * FROM queue WHERE status = 'PENDING' AND next_at <= ? ORDER BY seq LIMIT 1"
      ).get(now);
      if (!pending) {
        return { state: 'idle', item: null, queueLength: this._queueCount(), queueTotal: this._queueCount() };
      }
      this.db.prepare(
        "UPDATE queue SET status = 'SENDING', sent_at = ? WHERE id = ? AND status = 'PENDING'"
      ).run(now, pending.id);
      return {
        state: 'send',
        item: rowToQueueItem(this._queueRow(pending.id)),
        queueLength: this._queueCount(),
        queueTotal: this._queueCount(),
      };
    });
  }

  complete({ id, correlationId = '' }) {
    return this._transaction(() => {
      const row = this._findInflight(id);
      if (!row || (correlationId && row.correlation_id !== correlationId)) return null;
      const result = this.db.prepare(
        "DELETE FROM queue WHERE id = ? AND status = 'SENDING' AND correlation_id = ?"
      ).run(id, correlationId || row.correlation_id || '');
      if (!result.changes) return null;
      return rowToQueueItem(row);
    });
  }

  retry({ id, correlationId = '', delayMs, errorCode, reason, now = Date.now() }) {
    return this._transaction(() => {
      const row = this._findInflight(id);
      if (!row || (correlationId && row.correlation_id !== correlationId)) return null;
      const attempts = asInteger(row.attempts) + 1;
      if (attempts > RETRY_DELAYS.length) {
        const dead = this._moveToDead(row, errorCode, reason, attempts, now);
        return { outcome: 'dead', item: dead, queueLength: this._queueCount() };
      }
      const delay = Math.max(0, asInteger(delayMs, RETRY_DELAYS[attempts - 1]));
      this.db.prepare(`
        UPDATE queue
        SET attempts = ?, status = 'PENDING', next_at = ?, sent_at = NULL,
            last_status = ?, last_error_code = ?
        WHERE id = ? AND status = 'SENDING' AND correlation_id = ?
      `).run(attempts, now + delay, reason || '', errorCode || '', id, row.correlation_id || '');
      return {
        outcome: 'retry',
        item: rowToQueueItem(this._queueRow(id)),
        delay,
        queueLength: this._queueCount(),
      };
    });
  }

  deadLetter({ id, correlationId = '', errorCode, reason, now = Date.now() }) {
    return this._transaction(() => {
      const row = this._findInflight(id);
      if (!row || (correlationId && row.correlation_id !== correlationId)) return null;
      return this._moveToDead(row, errorCode, reason, row.attempts || 0, now);
    });
  }

  status() {
    const counts = { PENDING: 0, SENDING: 0, FAILED: 0 };
    const grouped = this.db.prepare(
      'SELECT status, COUNT(*) AS count FROM queue GROUP BY status'
    ).all();
    for (const row of grouped) {
      if (counts[row.status] !== undefined) counts[row.status] = Number(row.count || 0);
    }
    let oldest = 0;
    const timestamps = this.db.prepare('SELECT created_at, sent_at FROM queue').all();
    for (const row of timestamps) {
      const timestamp = row.created_at ? Date.parse(row.created_at) : asInteger(row.sent_at);
      if (timestamp && (!oldest || timestamp < oldest)) oldest = timestamp;
    }

    const dedupCounts = { ngFingerprints: 0, ngSerialNumbers: 0, completedWorkOrders: 0 };
    const dedupRows = this.db.prepare(
      'SELECT kind, COUNT(*) AS count FROM dedup GROUP BY kind'
    ).all();
    for (const row of dedupRows) {
      if (row.kind === 'ng_fp') dedupCounts.ngFingerprints = Number(row.count || 0);
      else if (row.kind === 'ng_sn') dedupCounts.ngSerialNumbers = Number(row.count || 0);
      else if (row.kind === 'done') dedupCounts.completedWorkOrders = Number(row.count || 0);
    }

    const latestDead = this.db.prepare(
      'SELECT * FROM dead_letters ORDER BY seq DESC LIMIT 1'
    ).get();
    return {
      queue: {
        total: this._queueCount(),
        pending: counts.PENDING,
        sending: counts.SENDING,
        oldestAgeSec: oldest ? Math.max(0, Math.round((Date.now() - oldest) / 1000)) : 0,
      },
      deadLetters: {
        total: this._deadCount(),
        last: rowToDeadLetter(latestDead),
      },
      dedup: dedupCounts,
    };
  }
}

function createStore(options = {}) {
  return new SqliteQueueStore(options);
}

module.exports = {
  createStore,
  resolveDbPath,
  MAX_QUEUE,
  MAX_DEAD_LETTERS,
  MAX_DEDUP_KEYS,
  RETRY_DELAYS,
  WATCHDOG_MS,
};
