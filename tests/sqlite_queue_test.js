'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

const {
  createStore,
  MAX_QUEUE,
  WATCHDOG_MS,
} = require('../nodered/sqlite_store.js');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qc-sqlite-queue-'));
let sequence = 0;
let passed = 0;
const failures = [];

function freshDbPath(name) {
  sequence += 1;
  return path.join(root, `${String(sequence).padStart(2, '0')}-${name}.sqlite`);
}

function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`PASS | ${name}`);
  } catch (error) {
    failures.push({ name, error });
    console.log(`FAIL | ${name} ${error.message}`);
  }
}

function accessors() {
  const values = new Map();
  return {
    values,
    flow: {
      get(key) {
        return values.get(key);
      },
      set(key, value) {
        values.set(key, value);
      },
    },
  };
}

function enqueueNg(store, serialNumber, occurredAt, now = 1000) {
  const fingerprint = `${serialNumber}|${occurredAt}`;
  return store.enqueue({
    id: `ng|${serialNumber}|${occurredAt}`,
    type: 'ng',
    correlationId: `corr-${serialNumber}-${occurredAt}`,
    url: 'http://127.0.0.1/mes/ng-order',
    body: { schemaVersion: '1.0', serialNumber, occurredAt },
    createdAt: occurredAt,
    dedupKeys: [
      { kind: 'ng_fp', key: fingerprint },
      { kind: 'ng_sn', key: serialNumber },
    ],
    now,
  });
}

function metricValue(store, name, type = '', outcome = '', errorCode = '') {
  const row = store.metricSnapshot().find(item =>
    item.name === name &&
    item.type === type &&
    item.outcome === outcome &&
    item.errorCode === errorCode
  );
  return row ? row.value : 0;
}

try {
  check('SQLite schema opens in WAL mode', () => {
    const dbPath = freshDbPath('wal');
    const store = createStore({ dbPath });
    try {
      const db = new DatabaseSync(dbPath);
      try {
        assert.equal(db.prepare('PRAGMA journal_mode').get().journal_mode, 'wal');
      } finally {
        db.close();
      }
    } finally {
      store.close();
    }
  });

  check('NG dedup and enqueue are idempotent', () => {
    const store = createStore({ dbPath: freshDbPath('dedup-ng') });
    try {
      const first = enqueueNg(store, 'SN-1', '2026-11-20T12:00:00.000Z');
      const second = enqueueNg(store, 'SN-1', '2026-11-20T12:00:00.000Z', 1001);
      const third = enqueueNg(store, 'SN-1', '2026-11-20T12:00:01.000Z', 1002);
      assert.equal(first.enqueued, true);
      assert.equal(second.enqueued, false);
      assert.equal(second.reason, 'queue-id');
      assert.equal(third.enqueued, false);
      assert.equal(third.reason, 'dedup');
      const status = store.status();
      assert.equal(status.queue.total, 1);
      assert.equal(status.dedup.ngFingerprints, 1);
      assert.equal(status.dedup.ngSerialNumbers, 1);
    } finally {
      store.close();
    }
  });

  check('completion dedup uses workOrderId', () => {
    const store = createStore({ dbPath: freshDbPath('dedup-done') });
    try {
      const enqueue = () => store.enqueue({
        id: 'done|WO-1',
        type: 'complete',
        correlationId: 'corr-done',
        url: 'http://127.0.0.1/mes/complete',
        body: { schemaVersion: '1.0', workOrderId: 'WO-1' },
        createdAt: '2026-11-20T12:00:00.000Z',
        dedupKeys: [{ kind: 'done', key: 'WO-1' }],
        now: 1000,
      });
      assert.equal(enqueue().enqueued, true);
      assert.equal(enqueue().enqueued, false);
      assert.equal(store.status().dedup.completedWorkOrders, 1);
    } finally {
      store.close();
    }
  });

  check('dedup and queue insert roll back together', () => {
    const dbPath = freshDbPath('atomic');
    const store = createStore({ dbPath });
    const db = new DatabaseSync(dbPath);
    try {
      db.exec(`
        CREATE TRIGGER force_queue_failure
        BEFORE INSERT ON queue
        WHEN NEW.id = 'ng|ATOMIC|2026-11-20T12:00:00.000Z'
        BEGIN
          SELECT RAISE(ABORT, 'forced queue failure');
        END;
      `);
      assert.throws(
        () => enqueueNg(store, 'ATOMIC', '2026-11-20T12:00:00.000Z'),
        /forced queue failure/
      );
      assert.equal(Number(db.prepare('SELECT COUNT(*) AS count FROM dedup').get().count), 0);
      assert.equal(Number(db.prepare('SELECT COUNT(*) AS count FROM queue').get().count), 0);
      assert.equal(Number(db.prepare('SELECT COUNT(*) AS count FROM metrics').get().count), 0);
    } finally {
      db.close();
      store.close();
    }
  });

  check('queue and dedup uniqueness constraints reject duplicates', () => {
    const dbPath = freshDbPath('constraints');
    const store = createStore({ dbPath });
    const db = new DatabaseSync(dbPath);
    try {
      db.prepare(
        "INSERT INTO dedup(kind, dedup_key, created_at) VALUES ('done', 'WO-X', 1)"
      ).run();
      assert.throws(
        () => db.prepare(
          "INSERT INTO dedup(kind, dedup_key, created_at) VALUES ('done', 'WO-X', 2)"
        ).run(),
        /UNIQUE constraint failed/
      );
      assert.throws(
        () => db.prepare(
          "INSERT INTO queue(id, type, url, body_json, status, enqueued_at) " +
          "VALUES ('bad-status', 'ng', 'http://x', '{}', 'BAD', 1)"
        ).run(),
        /CHECK constraint failed/
      );
    } finally {
      db.close();
      store.close();
    }
  });

  check('single-flight claim only dispatches one item', () => {
    const store = createStore({ dbPath: freshDbPath('single-flight') });
    try {
      enqueueNg(store, 'SN-A', '2026-11-20T12:00:00.000Z');
      enqueueNg(store, 'SN-B', '2026-11-20T12:00:01.000Z');
      const first = store.claimNext(2000);
      const second = store.claimNext(2001);
      assert.equal(first.state, 'send');
      assert.equal(first.item.status, 'SENDING');
      assert.equal(second.state, 'inflight');
      assert.equal(store.status().queue.sending, 1);
      assert.equal(store.complete({
        id: first.item.id,
        correlationId: first.item.correlationId,
      }).id, first.item.id);
      const third = store.claimNext(2002);
      assert.equal(third.state, 'send');
      assert.notEqual(third.item.id, first.item.id);
    } finally {
      store.close();
    }
  });

  check('correlation guard prevents stale response finalization', () => {
    const store = createStore({ dbPath: freshDbPath('correlation') });
    try {
      enqueueNg(store, 'SN-CORR', '2026-11-20T12:00:00.000Z');
      const sent = store.claimNext(2000);
      assert.equal(store.complete({ id: sent.item.id, correlationId: 'wrong' }), null);
      assert.equal(store.status().queue.sending, 1);
      assert.equal(store.complete({
        id: sent.item.id,
        correlationId: sent.item.correlationId,
      }).id, sent.item.id);
      assert.equal(store.status().queue.total, 0);
    } finally {
      store.close();
    }
  });

  check('retry advances attempts and moves exhaustion to dead letters', () => {
    const store = createStore({ dbPath: freshDbPath('retry') });
    try {
      enqueueNg(store, 'SN-RETRY', '2026-11-20T12:00:00.000Z');
      const delays = [1000, 2000, 4000];
      let now = 2000;
      let sent = store.claimNext(now);
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const retried = store.retry({
          id: sent.item.id,
          correlationId: sent.item.correlationId,
          delayMs: delays[attempt - 1],
          errorCode: 'MES_UNAVAILABLE',
          reason: 'HTTP 500',
          now,
        });
        assert.equal(retried.outcome, 'retry');
        assert.equal(retried.item.attempts, attempt);
        assert.equal(retried.item.status, 'PENDING');
        now = retried.item.nextAt;
        sent = store.claimNext(now);
        assert.equal(sent.state, 'send');
      }
      const dead = store.retry({
        id: sent.item.id,
        correlationId: sent.item.correlationId,
        delayMs: 4000,
        errorCode: 'MES_UNAVAILABLE',
        reason: 'HTTP 500',
        now,
      });
      assert.equal(dead.outcome, 'dead');
      assert.equal(dead.item.attempts, 4);
      assert.equal(dead.item.errorCode, 'MES_UNAVAILABLE');
      const status = store.status();
      assert.equal(status.queue.total, 0);
      assert.equal(status.deadLetters.total, 1);
      assert.equal(status.deadLetters.last.errorCode, 'MES_UNAVAILABLE');
    } finally {
      store.close();
    }
  });

  check('metrics commit with queue transitions and survive restart', () => {
    const dbPath = freshDbPath('metrics');
    const first = createStore({ dbPath });
    try {
      enqueueNg(first, 'SN-METRIC', '2026-11-20T12:00:00.000Z');
      enqueueNg(first, 'SN-METRIC', '2026-11-20T12:00:00.000Z', 1001);
      enqueueNg(first, 'SN-METRIC', '2026-11-20T12:00:01.000Z', 1002);
      const sent = first.claimNext(2000);
      first.complete({ id: sent.item.id, correlationId: sent.item.correlationId });

      first.enqueue({
        id: 'done|WO-METRIC',
        type: 'complete',
        correlationId: 'corr-done-metric',
        url: 'http://127.0.0.1/mes/complete',
        body: { schemaVersion: '1.0', workOrderId: 'WO-METRIC' },
        createdAt: '2026-11-20T12:00:02.000Z',
        dedupKeys: [{ kind: 'done', key: 'WO-METRIC' }],
        now: 3000,
      });
      const done = first.claimNext(4000);
      const retried = first.retry({
        id: done.item.id,
        correlationId: done.item.correlationId,
        delayMs: 1000,
        errorCode: 'MES_UNAVAILABLE',
        reason: 'HTTP 500',
        now: 4000,
      });
      const retriedDone = first.claimNext(retried.item.nextAt);
      first.deadLetter({
        id: retriedDone.item.id,
        correlationId: retriedDone.item.correlationId,
        errorCode: 'CUSTOM_MES_CODE',
        reason: 'HTTP 400 custom',
        now: retried.item.nextAt + 1,
      });

      assert.equal(metricValue(first, 'enqueue_total', 'ng', 'accepted'), 1);
      assert.equal(metricValue(first, 'enqueue_total', 'ng', 'duplicate'), 2);
      assert.equal(metricValue(first, 'enqueue_total', 'complete', 'accepted'), 1);
      assert.equal(metricValue(first, 'delivery_total', 'ng', 'success'), 1);
      assert.equal(metricValue(first, 'delivery_total', 'complete', 'retry'), 1);
      assert.equal(
        metricValue(first, 'delivery_errors_total', 'complete', '', 'MES_UNAVAILABLE'), 1
      );
      assert.equal(
        metricValue(first, 'delivery_errors_total', 'complete', '', 'OTHER_ERROR'), 1
      );
      assert.equal(
        metricValue(first, 'delivery_errors_total', 'complete', '', 'CUSTOM_MES_CODE'), 0
      );
    } finally {
      first.close();
    }

    const second = createStore({ dbPath });
    try {
      assert.equal(metricValue(second, 'enqueue_total', 'ng', 'accepted'), 1);
      assert.equal(metricValue(second, 'enqueue_total', 'ng', 'duplicate'), 2);
      assert.equal(metricValue(second, 'delivery_total', 'ng', 'success'), 1);
      assert.equal(metricValue(second, 'delivery_total', 'complete', 'retry'), 1);
      assert.equal(
        metricValue(second, 'delivery_errors_total', 'complete', '', 'OTHER_ERROR'), 1
      );
    } finally {
      second.close();
    }
  });

  check('watchdog recovers SENDING items and eventually dead-letters them', () => {
    const store = createStore({ dbPath: freshDbPath('watchdog') });
    try {
      enqueueNg(store, 'SN-WATCH', '2026-11-20T12:00:00.000Z');
      let now = 2000;
      let sent = store.claimNext(now);
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const recovered = store.claimNext(sent.item.sentAt + WATCHDOG_MS + 1);
        assert.equal(recovered.state, 'watchdog-retry');
        assert.equal(recovered.item.attempts, attempt);
        assert.equal(recovered.item.status, 'PENDING');
        sent = store.claimNext(recovered.item.nextAt);
        assert.equal(sent.state, 'send');
      }
      const dead = store.claimNext(sent.item.sentAt + WATCHDOG_MS + 1);
      assert.equal(dead.state, 'dead');
      assert.equal(dead.item.attempts, 4);
      assert.equal(dead.item.errorCode, 'WATCHDOG_TIMEOUT');
      assert.equal(store.status().queue.total, 0);
      assert.equal(metricValue(store, 'watchdog_total', '', 'recovered'), 3);
      assert.equal(metricValue(store, 'watchdog_total', '', 'dead'), 1);
      assert.equal(metricValue(store, 'delivery_total', 'ng', 'retry'), 3);
      assert.equal(metricValue(store, 'delivery_total', 'ng', 'dead'), 1);
      assert.equal(
        metricValue(store, 'delivery_errors_total', 'ng', '', 'WATCHDOG_TIMEOUT'), 4
      );
      void now;
    } finally {
      store.close();
    }
  });

  check('SENDING state survives store restart', () => {
    const dbPath = freshDbPath('restart-sending');
    const first = createStore({ dbPath });
    const sent = (() => {
      try {
        enqueueNg(first, 'SN-RESTART', '2026-11-20T12:00:00.000Z');
        return first.claimNext(2000);
      } finally {
        first.close();
      }
    })();
    const second = createStore({ dbPath });
    try {
      assert.equal(second.status().queue.sending, 1);
      assert.equal(second.claimNext(sent.item.sentAt + 1000).state, 'inflight');
      const recovered = second.claimNext(sent.item.sentAt + WATCHDOG_MS + 1);
      assert.equal(recovered.state, 'watchdog-retry');
      assert.equal(recovered.item.id, sent.item.id);
    } finally {
      second.close();
    }
  });

  check('queue overflow drops the oldest PENDING item', () => {
    const store = createStore({ dbPath: freshDbPath('overflow') });
    try {
      for (let index = 0; index <= MAX_QUEUE; index += 1) {
        const result = enqueueNg(
          store,
          `SN-OVERFLOW-${index}`,
          `2026-11-20T12:00:${String(index % 60).padStart(2, '0')}.000Z`,
          1000 + index
        );
        if (index === MAX_QUEUE) {
          assert.equal(result.dropped.length, 1);
          assert.equal(result.dropped[0], 'ng|SN-OVERFLOW-0|2026-11-20T12:00:00.000Z');
        }
      }
      assert.equal(store.status().queue.total, MAX_QUEUE);
    } finally {
      store.close();
    }
  });

  check('legacy flow context imports once and is cleared', () => {
    const dbPath = freshDbPath('legacy');
    const legacy = {
      buf: [{
        id: 'ng|SN-LEGACY|2026-11-20T12:00:00.000Z',
        type: 'ng',
        correlationId: 'corr-legacy',
        url: 'http://127.0.0.1/mes/ng-order',
        body: { schemaVersion: '1.0', serialNumber: 'SN-LEGACY' },
        createdAt: '2026-11-20T12:00:00.000Z',
        attempts: 0,
        nextAt: 0,
        status: 'PENDING',
      }],
      dead: [{
        id: 'done|WO-DEAD',
        type: 'complete',
        correlationId: 'corr-dead',
        url: 'http://127.0.0.1/mes/complete',
        body: { schemaVersion: '1.0', workOrderId: 'WO-DEAD' },
        attempts: 4,
        status: 'SENDING',
        errorCode: 'MES_UNAVAILABLE',
        lastStatus: 'HTTP 500',
        failedAt: '2026-11-20T12:01:00.000Z',
      }],
      ngSeen: { 'SN-LEGACY|2026-11-20T12:00:00.000Z': 1 },
      ngSnSeen: { 'SN-LEGACY': 1 },
      doneSeen: { 'WO-DEAD': 1 },
    };
    const { flow, values } = accessors();
    for (const [key, value] of Object.entries(legacy)) values.set(key, value);
    const first = createStore({ dbPath });
    try {
      first.bootstrap(flow);
      const status = first.status();
      assert.equal(status.queue.total, 1);
      assert.equal(status.deadLetters.total, 1);
      assert.equal(status.dedup.ngFingerprints, 1);
      assert.equal(status.dedup.ngSerialNumbers, 1);
      assert.equal(status.dedup.completedWorkOrders, 1);
      assert.deepEqual(flow.get('buf'), []);
      assert.deepEqual(flow.get('dead'), []);
      assert.deepEqual(flow.get('ngSeen'), {});
    } finally {
      first.close();
    }

    const { flow: secondFlow, values: secondValues } = accessors();
    for (const [key, value] of Object.entries(legacy)) secondValues.set(key, value);
    const second = createStore({ dbPath });
    try {
      second.bootstrap(secondFlow);
      assert.equal(second.status().queue.total, 1);
      assert.equal(second.status().deadLetters.total, 1);
      assert.equal(second.status().dedup.ngFingerprints, 1);
    } finally {
      second.close();
    }
  });

  check('dead letter history is capped at 100', () => {
    const store = createStore({ dbPath: freshDbPath('dead-cap') });
    try {
      for (let index = 0; index < 101; index += 1) {
        const serial = `SN-DEAD-${index}`;
        enqueueNg(store, serial, '2026-11-20T12:00:00.000Z', 1000 + index);
        const sent = store.claimNext(2000 + index);
        store.deadLetter({
          id: sent.item.id,
          correlationId: sent.item.correlationId,
          errorCode: 'MES_HTTP_4XX',
          reason: 'HTTP 400',
          now: 3000 + index,
        });
      }
      assert.equal(store.status().deadLetters.total, 100);
    } finally {
      store.close();
    }
  });

  check('dead-letter replay requeues once and preserves dedup', () => {
    const store = createStore({ dbPath: freshDbPath('dead-replay') });
    try {
      enqueueNg(store, 'SN-REPLAY', '2026-11-20T12:00:00.000Z');
      const sent = store.claimNext(2000);
      const dead = store.deadLetter({
        id: sent.item.id,
        correlationId: sent.item.correlationId,
        errorCode: 'MES_UNAVAILABLE',
        reason: 'HTTP 500',
        now: 3000,
      });
      assert.equal(dead.id, sent.item.id);
      assert.equal(store.status().deadLetters.total, 1);

      const replayed = store.replayDeadLetters({ id: sent.item.id, now: 4000 });
      assert.equal(replayed.replayed, 1);
      assert.equal(replayed.remainingDeadLetters, 0);
      assert.equal(replayed.queueLength, 1);
      const status = store.status();
      assert.equal(status.queue.pending, 1);
      assert.equal(status.dedup.ngFingerprints, 1);
      assert.equal(status.dedup.ngSerialNumbers, 1);

      const duplicateReplay = store.replayDeadLetters({ id: sent.item.id, now: 4001 });
      assert.equal(duplicateReplay.replayed, 0);

      const resent = store.claimNext(4002);
      assert.equal(resent.state, 'send');
      assert.equal(resent.item.attempts, 0);
      assert.equal(resent.item.lastStatus, 'replayed');
      assert.equal(store.complete({
        id: resent.item.id,
        correlationId: resent.item.correlationId,
      }).id, resent.item.id);
      assert.equal(store.status().queue.total, 0);
      assert.equal(store.status().deadLetters.total, 0);
    } finally {
      store.close();
    }
  });
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}

if (failures.length) {
  console.error(`\n${failures.length} SQLite queue test(s) failed`);
  for (const failure of failures) console.error(failure.error.stack || failure.error);
  process.exitCode = 1;
} else {
  console.log(`\n===== SQLITE QUEUE: ${passed}/${passed} passed =====`);
}
