'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const { health } = require('../nodered/queue_logic.js');

let passed = 0;
const failures = [];

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

function accessors(initial = {}) {
  const values = new Map(Object.entries(initial));
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

function storeStatus(overrides = {}) {
  return {
    queue: { pending: 2, sending: 1, retrying: 0, total: 3 },
    deadLetters: { total: 4 },
    ...overrides,
  };
}

function makeStore({ status = storeStatus(), error = null } = {}) {
  return {
    status() {
      if (error) throw error;
      return status;
    },
  };
}

check('ready probe returns HTTP 200', () => {
  const { flow } = accessors();
  const msg = health({}, flow, makeStore());
  assert.equal(msg.statusCode, 200);
  assert.equal(msg.payload.ok, true);
});

check('ready probe reports service and status', () => {
  const { flow } = accessors();
  const msg = health({}, flow, makeStore());
  assert.equal(msg.payload.service, 'ua-linqinspect-edge');
  assert.equal(msg.payload.status, 'ready');
});

check('ready probe exposes queue and dead-letter counts', () => {
  const { flow } = accessors();
  const msg = health({}, flow, makeStore());
  assert.deepEqual(msg.payload.queue, { pending: 2, sending: 1, retrying: 0, total: 3 });
  assert.equal(msg.payload.deadLetters, 4);
});

check('health response uses JSON and disables caching', () => {
  const { flow } = accessors();
  const msg = health({}, flow, makeStore());
  assert.equal(msg.headers['content-type'], 'application/json; charset=utf-8');
  assert.equal(msg.headers['cache-control'], 'no-store');
});

check('health records edge start time once', () => {
  const { values, flow } = accessors({ edgeStartedAt: 1000 });
  health({}, flow, makeStore());
  assert.equal(values.get('edgeStartedAt'), 1000);
  assert.equal(typeof values.get('edgeStartedAt'), 'number');
});

check('two heartbeat failures remain ready', () => {
  const { flow } = accessors({ hbFails: 2 });
  const msg = health({}, flow, makeStore());
  assert.equal(msg.statusCode, 200);
  assert.equal(msg.payload.heartbeatFailures, 2);
});

check('three heartbeat failures degrade the probe', () => {
  const { flow } = accessors({ hbFails: 3 });
  const msg = health({}, flow, makeStore());
  assert.equal(msg.statusCode, 503);
  assert.equal(msg.payload.ok, false);
  assert.equal(msg.payload.status, 'degraded');
  assert.equal(msg.payload.heartbeatFailures, 3);
});

check('SQLite status failure reports unavailable', () => {
  const { flow } = accessors();
  const msg = health({}, flow, makeStore({ error: new Error('database is locked') }));
  assert.equal(msg.statusCode, 503);
  assert.equal(msg.payload.ok, false);
  assert.equal(msg.payload.status, 'unavailable');
  assert.equal(msg.payload.errorCode, 'SQLITE_UNAVAILABLE');
});

check('SQLite failure response still uses safe headers', () => {
  const { flow } = accessors();
  const msg = health({}, flow, makeStore({ error: new Error('database is locked') }));
  assert.equal(msg.headers['content-type'], 'application/json; charset=utf-8');
  assert.equal(msg.headers['cache-control'], 'no-store');
});

check('flow wires GET /healthz through the health function', () => {
  const flowPath = path.join(__dirname, '..', 'nodered', 'flows.json');
  const nodes = JSON.parse(fs.readFileSync(flowPath, 'utf8'));
  const request = nodes.find(node => node.id === 'httpin_health');
  const handler = nodes.find(node => node.id === 'fn_health');
  const response = nodes.find(node => node.id === 'httpres_health');
  assert.equal(request.type, 'http in');
  assert.equal(request.url, '/healthz');
  assert.equal(request.method, 'get');
  assert.deepEqual(request.wires, [['fn_health']]);
  assert.match(handler.func, /qlogic'\)\.health/);
  assert.deepEqual(handler.wires, [['httpres_health']]);
  assert.equal(response.type, 'http response');
});

console.log(`\n===== HEALTH: ${passed}/${passed + failures.length} passed =====`);
if (failures.length > 0) {
  console.error(failures.map(failure => `${failure.name}: ${failure.error.stack}`).join('\n'));
  process.exitCode = 1;
}
