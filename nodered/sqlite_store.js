'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

const MAX_QUEUE = 100;
const MAX_DEAD_LETTERS = 100;
const MAX_DEDUP_KEYS = 500;
const RETRY_DELAYS = [1000, 2000, 4000];
const WATCHDOG_MS = 15000;
const MAX_UA_WRITE_ATTEMPTS = 3;
const UA_WRITE_PENDING = 'PENDING';
const UA_WRITE_SENDING = 'SENDING';
const LEGACY_MIGRATION_KEY = 'legacy_context_imported_v1';
const METRIC_TYPES = new Set(['ng', 'complete']);
const METRIC_ERROR_CODES = new Set([
  'MES_UNAVAILABLE',
  'MES_BUSY',
  'MES_REJECTED',
  'MES_HTTP_4XX',
  'TRANSPORT_ERROR',
  'UNKNOWN_ERROR',
  'WATCHDOG_TIMEOUT',
  'QUEUE_OVERFLOW',
  'INVALID_RESPONSE_SCHEMA',
  'CORRELATION_MISMATCH',
  'MISSING_MES_ORDER_NO',
  'MISSING_RECEIPT_NO',
  'UA_WRITE_FAILED',
  'UA_WRITE_TIMEOUT',
  'UA_WRITE_VALUE_MISMATCH',
]);

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

function metricType(value) {
  const text = asString(value);
  if (!text) return '';
  return METRIC_TYPES.has(text) ? text : 'other';
}

function metricErrorCode(value) {
  const text = asString(value);
  if (!text) return '';
  return METRIC_ERROR_CODES.has(text) ? text : 'OTHER_ERROR';
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

      CREATE TABLE IF NOT EXISTS ua_writebacks (
        queue_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENDING')),
        receipt TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at INTEGER NOT NULL DEFAULT 0,
        started_at INTEGER,
        last_error_code TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_ua_writebacks_status_next
        ON ua_writebacks(status, next_at);

      CREATE TABLE IF NOT EXISTS ua_write_targets (
        queue_id TEXT NOT NULL,
        target TEXT NOT NULL,
        expected_value TEXT NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (queue_id, target)
      );

      CREATE TABLE IF NOT EXISTS meta (
        meta_key TEXT PRIMARY KEY,
        meta_value TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS metrics (
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        value INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (name, type, outcome, error_code)
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
    this._metricIncrementStatement = this.db.prepare(`
      INSERT INTO metrics(name, type, outcome, error_code, value, updated_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(name, type, outcome, error_code) DO UPDATE SET
        value = metrics.value + excluded.value,
        updated_at = excluded.updated_at
    `);
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

  _fileSize(suffix = '') {
    try {
      return Number(fs.statSync(this.dbPath + suffix).size || 0);
    } catch (_) {
      return 0;
    }
  }

  storage() {
    return {
      mainBytes: this._fileSize(''),
      walBytes: this._fileSize('-wal'),
      shmBytes: this._fileSize('-shm'),
    };
  }

  _queueRow(id) {
    return this.db.prepare('SELECT * FROM queue WHERE id = ?').get(id);
  }

  _uaWritebackRow(queueId) {
    return this.db.prepare('SELECT * FROM ua_writebacks WHERE queue_id = ?').get(queueId);
  }

  _uaWriteTargets(queueId, unconfirmedOnly = false) {
    const sql = unconfirmedOnly
      ? 'SELECT * FROM ua_write_targets WHERE queue_id = ? AND confirmed = 0 ORDER BY target'
      : 'SELECT * FROM ua_write_targets WHERE queue_id = ? ORDER BY target';
    return this.db.prepare(sql).all(queueId).map(row => ({
      target: row.target,
      expectedValue: row.expected_value,
      confirmed: Boolean(row.confirmed),
    }));
  }

  _deleteUaWriteback(queueId) {
    this.db.prepare('DELETE FROM ua_write_targets WHERE queue_id = ?').run(queueId);
    this.db.prepare('DELETE FROM ua_writebacks WHERE queue_id = ?').run(queueId);
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

  _metricIncrement(name, type, outcome, errorCode, now, amount = 1) {
    this._metricIncrementStatement.run(
      name,
      metricType(type),
      outcome || '',
      metricErrorCode(errorCode),
      asInteger(amount, 1),
      asInteger(now, Date.now())
    );
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
    this._deleteUaWriteback(row.id);
    this.db.prepare('DELETE FROM queue WHERE id = ?').run(row.id);
    this._pruneDeadLetters();
    this._metricIncrement('delivery_total', row.type, 'dead', '', now);
    this._metricIncrement(
      'delivery_errors_total', row.type, '', errorCode || 'UNKNOWN_ERROR', now
    );
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

      let overflowImported = 0;
      while (this._queueCount() > MAX_QUEUE) {
        const victim = this.db.prepare(
          "SELECT * FROM queue WHERE status = 'PENDING' ORDER BY seq LIMIT 1"
        ).get();
        if (!victim) break;
        this._moveToDead(
          victim,
          'QUEUE_OVERFLOW',
          'legacy-migration-overflow',
          victim.attempts || 0,
          Date.now(),
        );
        overflowImported += 1;
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
      return {
        migrated: true,
        queue: queueImported,
        dead: deadImported,
        dedup: dedupImported,
        overflow: overflowImported,
      };
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
        this._metricIncrement('enqueue_total', type, 'duplicate', '', now);
        return { enqueued: false, duplicate: true, reason: 'queue-id', dropped: [], queueLength: this._queueCount() };
      }
      for (const entry of dedupKeys) {
        if (this.hasDedup(entry.kind, entry.key)) {
          this._metricIncrement('enqueue_total', type, 'duplicate', '', now);
          return { enqueued: false, duplicate: true, reason: 'dedup', dropped: [], queueLength: this._queueCount() };
        }
      }

      const queueLength = this._queueCount();
      if (queueLength >= MAX_QUEUE) {
        // Capacity is a hard backpressure boundary: never evict a durable task
        // to make room for a newer one. The caller must leave the UA event
        // unacknowledged so it can be replayed after the queue drains.
        this._metricIncrement('enqueue_total', type, 'rejected', '', now);
        return {
          enqueued: false,
          duplicate: false,
          rejected: true,
          reason: 'capacity',
          dropped: [],
          queueLength,
          capacity: MAX_QUEUE,
        };
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
      this._metricIncrement('enqueue_total', type, 'accepted', '', now);
      return {
        enqueued: true,
        duplicate: false,
        rejected: false,
        reason: '',
        dropped: [],
        queueLength: this._queueCount(),
        capacity: MAX_QUEUE,
      };
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
        const writeback = this._uaWritebackRow(inflight.id);
        if (writeback) {
          const remainingTargets = this._uaWriteTargets(inflight.id, true);
          if (!remainingTargets.length) {
            this._deleteUaWriteback(inflight.id);
            this.db.prepare('DELETE FROM queue WHERE id = ?').run(inflight.id);
            this._metricIncrement('ua_write_total', inflight.type, 'success', '', now);
            return {
              state: 'ua-write-complete',
              item: rowToQueueItem(inflight),
              queueLength: this._queueCount(),
              queueTotal: this._queueCount(),
            };
          }

          const startedAt = asInteger(writeback.started_at);
          const writeExpired = !startedAt || now - startedAt > this.watchdogMs;
          if (writeback.status === UA_WRITE_SENDING && writeExpired) {
            const attempts = asInteger(writeback.attempts);
            if (attempts >= MAX_UA_WRITE_ATTEMPTS) {
              const dead = this._moveToDead(
                inflight, 'UA_WRITE_TIMEOUT', 'ua-write-timeout', attempts, now
              );
              this._metricIncrement('ua_write_total', inflight.type, 'dead', '', now);
              this._metricIncrement(
                'ua_write_errors_total', inflight.type, '', 'UA_WRITE_TIMEOUT', now
              );
              return {
                state: 'ua-write-dead',
                item: dead,
                queueLength: this._queueCount(),
                queueTotal: this._queueCount(),
              };
            }
            const delay = RETRY_DELAYS[Math.min(attempts - 1, RETRY_DELAYS.length - 1)];
            this.db.prepare(`
              UPDATE ua_writebacks
              SET status = ?, next_at = ?, started_at = NULL,
                  last_error_code = 'UA_WRITE_TIMEOUT', updated_at = ?
              WHERE queue_id = ? AND status = 'SENDING'
            `).run(UA_WRITE_PENDING, now + delay, now, inflight.id);
            this._metricIncrement('ua_write_total', inflight.type, 'retry', '', now);
            this._metricIncrement(
              'ua_write_errors_total', inflight.type, '', 'UA_WRITE_TIMEOUT', now
            );
            return {
              state: 'ua-write-retry',
              item: rowToQueueItem(inflight),
              delay,
              queueLength: this._queueCount(),
              queueTotal: this._queueCount(),
            };
          }

          if (writeback.status === UA_WRITE_PENDING && asInteger(writeback.next_at) <= now) {
            const attempts = asInteger(writeback.attempts) + 1;
            this.db.prepare(`
              UPDATE ua_writebacks
              SET status = ?, attempts = ?, started_at = ?, updated_at = ?
              WHERE queue_id = ? AND status = 'PENDING'
            `).run(UA_WRITE_SENDING, attempts, now, now, inflight.id);
            return {
              state: 'ua-write',
              item: rowToQueueItem(inflight),
              targets: this._uaWriteTargets(inflight.id, true),
              queueLength: this._queueCount(),
              queueTotal: this._queueCount(),
            };
          }

          return {
            state: 'ua-write-inflight',
            item: rowToQueueItem(inflight),
            queueLength: this._queueCount(),
            queueTotal: this._queueCount(),
          };
        }

        const sentAt = asInteger(inflight.sent_at);
        if (!sentAt || now - sentAt > this.watchdogMs) {
          const attempts = asInteger(inflight.attempts) + 1;
          if (attempts > RETRY_DELAYS.length) {
            const dead = this._moveToDead(
              inflight, 'WATCHDOG_TIMEOUT', 'watchdog-timeout', attempts, now
            );
            this._metricIncrement('watchdog_total', '', 'dead', '', now);
            return { state: 'dead', item: dead, queueLength: this._queueCount(), queueTotal: this._queueCount() };
          }
          const delay = RETRY_DELAYS[attempts - 1];
          this.db.prepare(`
            UPDATE queue
            SET attempts = ?, status = 'PENDING', next_at = ?, sent_at = NULL,
                last_status = 'watchdog-timeout', last_error_code = 'WATCHDOG_TIMEOUT'
            WHERE id = ? AND status = 'SENDING'
          `).run(attempts, now + delay, inflight.id);
          this._metricIncrement('watchdog_total', '', 'recovered', '', now);
          this._metricIncrement('delivery_total', inflight.type, 'retry', '', now);
          this._metricIncrement('delivery_errors_total', inflight.type, '', 'WATCHDOG_TIMEOUT', now);
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

  complete({ id, correlationId = '', receipt, targets = [], now = Date.now() }) {
    return this._transaction(() => {
      const row = this._findInflight(id);
      if (!row || (correlationId && row.correlation_id !== correlationId)) return null;
      if (this._uaWritebackRow(row.id)) return null;
      const writebackReceipt = asString(receipt);
      if (!writebackReceipt) throw new Error('UA writeback receipt is required');
      if (!Array.isArray(targets) || targets.length === 0) {
        throw new Error('UA writeback targets are required');
      }

      this.db.prepare(`
        INSERT INTO ua_writebacks(
          queue_id, status, receipt, attempts, next_at, started_at,
          last_error_code, created_at, updated_at
        ) VALUES (?, ?, ?, 1, ?, ?, '', ?, ?)
      `).run(row.id, UA_WRITE_SENDING, writebackReceipt, now, now, now, now);
      const insertTarget = this.db.prepare(`
        INSERT INTO ua_write_targets(queue_id, target, expected_value, confirmed, updated_at)
        VALUES (?, ?, ?, 0, ?)
      `);
      for (const target of targets) {
        const topic = asString(target && target.topic);
        const expectedValue = asString(target && target.value);
        if (!topic || !expectedValue) throw new Error('UA writeback target topic/value is required');
        insertTarget.run(row.id, topic, expectedValue, now);
      }
      this._metricIncrement('delivery_total', row.type, 'success', '', now);
      return rowToQueueItem(row);
    });
  }

  confirmUaWriteTarget({ id, correlationId = '', target, value, now = Date.now() }) {
    return this._transaction(() => {
      const row = this._findInflight(id);
      if (!row || (correlationId && row.correlation_id !== correlationId)) return null;
      const writeback = this._uaWritebackRow(row.id);
      if (!writeback || writeback.status !== UA_WRITE_SENDING) return null;
      const topic = asString(target);
      const expected = this.db.prepare(
        'SELECT * FROM ua_write_targets WHERE queue_id = ? AND target = ?'
      ).get(row.id, topic);
      if (!expected) return null;
      if (value !== undefined && value !== null && asString(value) !== expected.expected_value) {
        this._metricIncrement(
          'ua_write_errors_total', row.type, '', 'UA_WRITE_VALUE_MISMATCH', now
        );
        return {
          outcome: 'mismatch',
          id: row.id,
          target: topic,
          expected: expected.expected_value,
          actual: asString(value),
          confirmed: 0,
        };
      }
      this.db.prepare(`
        UPDATE ua_write_targets SET confirmed = 1, updated_at = ?
        WHERE queue_id = ? AND target = ?
      `).run(now, row.id, topic);
      this.db.prepare(
        'UPDATE ua_writebacks SET updated_at = ? WHERE queue_id = ?'
      ).run(now, row.id);
      const remaining = Number(this.db.prepare(`
        SELECT COUNT(*) AS count FROM ua_write_targets
        WHERE queue_id = ? AND confirmed = 0
      `).get(row.id).count || 0);
      if (remaining === 0) {
        this._deleteUaWriteback(row.id);
        this.db.prepare('DELETE FROM queue WHERE id = ?').run(row.id);
        this._metricIncrement('ua_write_total', row.type, 'success', '', now);
        return { outcome: 'complete', id: row.id, target: topic, confirmed: 0 };
      }
      return { outcome: 'confirmed', id: row.id, target: topic, confirmed: remaining };
    });
  }

  failUaWrite({ id, correlationId = '', errorCode = 'UA_WRITE_FAILED', reason = '', now = Date.now() }) {
    return this._transaction(() => {
      const row = this._findInflight(id);
      if (!row || (correlationId && row.correlation_id !== correlationId)) return null;
      const writeback = this._uaWritebackRow(row.id);
      if (!writeback || writeback.status !== UA_WRITE_SENDING) return null;
      const attempts = asInteger(writeback.attempts);
      if (attempts >= MAX_UA_WRITE_ATTEMPTS) {
        const dead = this._moveToDead(row, errorCode, reason, attempts, now);
        this._metricIncrement('ua_write_total', row.type, 'dead', '', now);
        this._metricIncrement('ua_write_errors_total', row.type, '', errorCode, now);
        return { outcome: 'dead', item: dead, queueLength: this._queueCount() };
      }
      const delay = RETRY_DELAYS[Math.min(attempts - 1, RETRY_DELAYS.length - 1)];
      this.db.prepare(`
        UPDATE ua_writebacks
        SET status = ?, next_at = ?, started_at = NULL, last_error_code = ?, updated_at = ?
        WHERE queue_id = ? AND status = 'SENDING'
      `).run(UA_WRITE_PENDING, now + delay, errorCode, now, row.id);
      this._metricIncrement('ua_write_total', row.type, 'retry', '', now);
      this._metricIncrement('ua_write_errors_total', row.type, '', errorCode, now);
      return {
        outcome: 'retry',
        item: rowToQueueItem(row),
        delay,
        queueLength: this._queueCount(),
      };
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
      this._metricIncrement('delivery_total', row.type, 'retry', '', now);
      this._metricIncrement('delivery_errors_total', row.type, '', errorCode || 'UNKNOWN_ERROR', now);
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

  replayDeadLetters({ id = '', limit = MAX_DEAD_LETTERS, now = Date.now() } = {}) {
    const queueId = String(id || '').trim();
    const replayLimit = Math.min(
      Math.max(1, asInteger(limit, MAX_DEAD_LETTERS)),
      MAX_DEAD_LETTERS
    );
    return this._transaction(() => {
      const rows = queueId
        ? this.db.prepare(
          'SELECT * FROM dead_letters WHERE queue_id = ? ORDER BY seq LIMIT ?'
        ).all(queueId, replayLimit)
        : this.db.prepare(
          'SELECT * FROM dead_letters ORDER BY seq LIMIT ?'
        ).all(replayLimit);
      let replayed = 0;
      let skipped = 0;
      let capacitySkipped = 0;
      for (const row of rows) {
        if (this._queueRow(row.queue_id)) {
          skipped += 1;
          continue;
        }
        if (this._queueCount() >= MAX_QUEUE) {
          capacitySkipped += 1;
          continue;
        }
        this.db.prepare(`
          INSERT INTO queue(
            id, type, correlation_id, url, body_json, created_at, attempts,
            next_at, status, sent_at, last_status, last_error_code, enqueued_at
          ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'PENDING', NULL, 'replayed', '', ?)
        `).run(
          row.queue_id,
          row.type,
          row.correlation_id || '',
          row.url,
          row.body_json,
          row.created_at || '',
          now,
          now
        );
        this.db.prepare('DELETE FROM dead_letters WHERE seq = ?').run(row.seq);
        replayed += 1;
      }
      return {
        replayed,
        skipped,
        capacitySkipped,
        remainingDeadLetters: this._deadCount(),
        queueLength: this._queueCount(),
      };
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
    const uaCounts = { PENDING: 0, SENDING: 0 };
    const uaRows = this.db.prepare(
      'SELECT status, COUNT(*) AS count FROM ua_writebacks GROUP BY status'
    ).all();
    for (const row of uaRows) {
      if (uaCounts[row.status] !== undefined) uaCounts[row.status] = Number(row.count || 0);
    }
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
      uaWriteback: {
        pending: uaCounts.PENDING,
        sending: uaCounts.SENDING,
      },
      storage: this.storage(),
    };
  }

  metricSnapshot() {
    return this.db.prepare(`
      SELECT name, type, outcome, error_code, value, updated_at
      FROM metrics
      ORDER BY name, type, outcome, error_code
    `).all().map(row => ({
      name: row.name,
      type: row.type || '',
      outcome: row.outcome || '',
      errorCode: row.error_code || '',
      value: asInteger(row.value),
      updatedAt: asInteger(row.updated_at),
    }));
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
  MAX_UA_WRITE_ATTEMPTS,
};
