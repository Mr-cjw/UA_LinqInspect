'use strict';

const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

const { createStore } = require('../nodered/sqlite_store.js');

const root = process.argv[2];
if (!root) {
  console.error('usage: mount_sqlite_storage_test.js <mounted-directory>');
  process.exit(2);
}

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`PASS | mount-sqlite: ${name}`);
  } catch (error) {
    failures.push(`${name}: ${error.message}`);
    console.log(`FAIL | mount-sqlite: ${name} ${error.message}`);
  }
}

function capture(fn) {
  try {
    fn();
  } catch (error) {
    return error;
  }
  return null;
}

function enqueueNg(store, serialNumber, padding = '') {
  return store.enqueue({
    id: `ng|${serialNumber}|2026-11-21T00:00:00.000Z`,
    type: 'ng',
    correlationId: `corr-${serialNumber}`,
    url: 'http://127.0.0.1/mes/ng-order',
    body: { schemaVersion: '1.0', serialNumber, padding },
    createdAt: '2026-11-21T00:00:00.000Z',
    dedupKeys: [
      { kind: 'ng_fp', key: `${serialNumber}|2026-11-21T00:00:00.000Z` },
      { kind: 'ng_sn', key: serialNumber },
    ],
    now: 1000,
  });
}

function fillFilesystem(directory) {
  const filler = path.join(directory, 'capacity-filler.bin');
  const block = Buffer.alloc(64 * 1024, 0x78);
  const descriptor = fs.openSync(filler, 'w');
  try {
    while (true) {
      try {
        const written = fs.writeSync(descriptor, block, 0, block.length);
        if (written === 0) {
          return filler;
        }
      } catch (error) {
        if (error.code === 'ENOSPC') {
          return filler;
        }
        throw error;
      }
    }
  } finally {
    fs.closeSync(descriptor);
  }
}

function remount(mode) {
  childProcess.execFileSync('mount', ['-o', `remount,${mode}`, root], { stdio: 'pipe' });
}

try {
  const dbPath = path.join(root, 'queue.sqlite');
  let store = createStore({ dbPath });
  check('seed enqueue is accepted', () => {
    const result = enqueueNg(store, 'SN-SEED');
    assert.equal(result.enqueued, true);
    assert.equal(store.status().queue.total, 1);
  });

  const filler = fillFilesystem(root);
  check('bounded tmpfs reaches ENOSPC', () => {
    assert.ok(fs.existsSync(filler));
    assert.ok(fs.statSync(filler).size > 0);
  });
  const diskFull = capture(() => enqueueNg(store, 'SN-DISK-FULL', 'A'.repeat(256 * 1024)));
  check('enqueue surfaces SQLite disk-full', () => {
    assert.ok(diskFull, 'expected an enqueue error');
    assert.equal(diskFull.errcode, 13);
    assert.match(diskFull.message, /database or disk is full/i);
  });
  check('disk-full enqueue leaves queue unchanged', () => {
    assert.equal(store.status().queue.total, 1);
  });
  check('disk-full enqueue leaves dedup unchanged', () => {
    const dedup = store.status().dedup;
    assert.equal(dedup.ngFingerprints, 1);
    assert.equal(dedup.ngSerialNumbers, 1);
  });
  check('disk-full enqueue leaves counters unchanged', () => {
    const accepted = store.metricSnapshot().find(item =>
      item.name === 'enqueue_total' && item.type === 'ng' && item.outcome === 'accepted'
    );
    assert.equal(accepted.value, 1);
  });

  fs.unlinkSync(filler);
  check('enqueue recovers after capacity is released', () => {
    assert.equal(enqueueNg(store, 'SN-RECOVERED').enqueued, true);
    assert.equal(store.status().queue.total, 2);
  });

  store.db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
  store.close();
  const readonlyBytes = fs.readFileSync(dbPath);
  remount('ro');
  try {
    let readonlyError = null;
    let roStore = null;
    try {
      roStore = createStore({ dbPath });
      enqueueNg(roStore, 'SN-READONLY-BLOCKED');
    } catch (error) {
      readonlyError = error;
    } finally {
      if (roStore) {
        roStore.close();
      }
    }
    check('read-only mount rejects queue writes', () => {
      assert.ok(readonlyError, 'expected a read-only queue error');
    });
    check('read-only mount error identifies blocked storage', () => {
      assert.match(
        readonlyError.message,
        /readonly|read-only|attempt to write|disk i\/o|unable to open database file/i
      );
    });
    check('read-only mount preserves queue database bytes', () => {
      assert.deepEqual(fs.readFileSync(dbPath), readonlyBytes);
    });
  } finally {
    remount('rw');
  }

  check('queue reopens after remount to read-write', () => {
    store = createStore({ dbPath });
    assert.equal(store.status().queue.total, 2);
  });
  check('post-remount enqueue is accepted', () => {
    assert.equal(enqueueNg(store, 'SN-AFTER-RO').enqueued, true);
    assert.equal(store.status().queue.total, 3);
  });
  store.close();
} finally {
  if (failures.length) {
    for (const failure of failures) {
      console.log(`ERROR | mount-sqlite: ${failure}`);
    }
    console.log(`===== MOUNT-SQLITE: ${passed}/${passed + failures.length} passed =====`);
    process.exitCode = 1;
  } else {
    console.log(`===== MOUNT-SQLITE: ${passed}/${passed} passed =====`);
  }
}
