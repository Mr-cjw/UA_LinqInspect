'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { createStore } = require('../nodered/sqlite_store.js');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qc-storage-failure-'));
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

function enqueueNg(store, serialNumber, occurredAt = '2026-11-21T00:00:00.000Z', now = 1000) {
  return store.enqueue({
    id: `ng|${serialNumber}|${occurredAt}`,
    type: 'ng',
    correlationId: `corr-${serialNumber}`,
    url: 'http://127.0.0.1/mes/ng-order',
    body: { schemaVersion: '1.0', serialNumber, occurredAt },
    createdAt: occurredAt,
    dedupKeys: [
      { kind: 'ng_fp', key: `${serialNumber}|${occurredAt}` },
      { kind: 'ng_sn', key: serialNumber },
    ],
    now,
  });
}

function uaTargets(receipt) {
  return [
    { topic: 'ns=2;s=QCStation.LastNgMesOrderNo', value: receipt },
    { topic: 'ns=2;s=QCStation.LastMesOrderNo', value: receipt },
  ];
}

function capAtCurrentSize(store) {
  const pageCount = Number(store.db.prepare('PRAGMA page_count').get().page_count);
  store.db.exec(`PRAGMA max_page_count = ${pageCount}`);
  return pageCount;
}

function restorePageCapacity(store) {
  store.db.exec('PRAGMA max_page_count = 100000');
}

function expectDiskFull(fn) {
  let caught = null;
  try {
    fn();
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, 'expected SQLite disk-full error');
  assert.equal(caught.code, 'ERR_SQLITE_ERROR');
  assert.equal(caught.errcode, 13);
  assert.match(caught.message, /database or disk is full/);
}

try {
  check('disk-full enqueue rolls back queue, dedup and metrics', () => {
    const store = createStore({ dbPath: freshDbPath('enqueue-full') });
    try {
      const pageCount = capAtCurrentSize(store);
      assert.ok(pageCount > 0);
      expectDiskFull(() => store.enqueue({
        id: 'ng|SN-FULL|2026-11-21T00:00:00.000Z',
        type: 'ng',
        correlationId: 'corr-full',
        url: 'http://127.0.0.1/mes/ng-order',
        body: { schemaVersion: '1.0', serialNumber: 'SN-FULL', padding: 'A'.repeat(200000) },
        createdAt: '2026-11-21T00:00:00.000Z',
        dedupKeys: [
          { kind: 'ng_fp', key: 'SN-FULL|2026-11-21T00:00:00.000Z' },
          { kind: 'ng_sn', key: 'SN-FULL' },
        ],
        now: 1000,
      }));

      const status = store.status();
      assert.equal(status.queue.total, 0);
      assert.equal(status.dedup.ngFingerprints, 0);
      assert.equal(status.dedup.ngSerialNumbers, 0);
      assert.deepEqual(store.metricSnapshot(), []);

      restorePageCapacity(store);
      assert.equal(enqueueNg(store, 'SN-RECOVERED', undefined, 2000).enqueued, true);
      assert.equal(store.status().queue.total, 1);
      assert.equal(store.metricSnapshot().find(item =>
        item.name === 'enqueue_total' && item.outcome === 'accepted'
      ).value, 1);
    } finally {
      store.close();
    }
  });

  check('disk-full completion preserves the SENDING item and commits no writeback', () => {
    const store = createStore({ dbPath: freshDbPath('complete-full') });
    try {
      enqueueNg(store, 'SN-COMPLETE-FULL');
      const claimed = store.claimNext(2000);
      assert.equal(claimed.state, 'send');
      capAtCurrentSize(store);
      expectDiskFull(() => store.complete({
        id: claimed.item.id,
        correlationId: claimed.item.correlationId,
        receipt: 'R'.repeat(200000),
        targets: uaTargets('R'.repeat(200000)),
        now: 2001,
      }));

      const failed = store.status();
      assert.equal(failed.queue.total, 1);
      assert.equal(failed.queue.sending, 1);
      assert.equal(failed.uaWriteback.pending, 0);
      assert.equal(failed.uaWriteback.sending, 0);
      assert.equal(Number(store.db.prepare('SELECT COUNT(*) AS count FROM ua_writebacks').get().count), 0);
      assert.equal(Number(store.db.prepare('SELECT COUNT(*) AS count FROM ua_write_targets').get().count), 0);
      assert.equal(store.metricSnapshot().some(item => item.name === 'delivery_total'), false);

      restorePageCapacity(store);
      const receipt = 'MES-STORAGE-RECOVERED';
      const targets = uaTargets(receipt);
      const completed = store.complete({
        id: claimed.item.id,
        correlationId: claimed.item.correlationId,
        receipt,
        targets,
        now: 2002,
      });
      assert.equal(completed.id, claimed.item.id);
      assert.equal(store.status().queue.sending, 1);
      assert.equal(store.status().uaWriteback.sending, 1);
      assert.equal(Number(store.db.prepare('SELECT COUNT(*) AS count FROM ua_write_targets').get().count), 2);
      assert.equal(store.metricSnapshot().find(item =>
        item.name === 'delivery_total' && item.outcome === 'success'
      ).value, 1);
    } finally {
      store.close();
    }
  });

  check('corrupt database fails explicitly instead of being recreated', () => {
    const dbPath = freshDbPath('corrupt');
    const corruptBytes = Buffer.alloc(4096, 0x41);
    fs.writeFileSync(dbPath, corruptBytes);
    assert.throws(() => createStore({ dbPath }), /file is not a database/);
    assert.deepEqual(fs.readFileSync(dbPath), corruptBytes);
  });
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}

if (failures.length) {
  console.error(`\n${failures.length} storage failure test(s) failed`);
  for (const failure of failures) console.error(failure.error.stack || failure.error);
  process.exitCode = 1;
} else {
  console.log(`\n===== STORAGE FAILURE: ${passed}/${passed} passed =====`);
}
