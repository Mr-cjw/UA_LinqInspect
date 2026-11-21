'use strict';

const assert = require('node:assert/strict');

const {
  mesFingerprint,
  mesNgOrder,
  mesComplete,
} = require('../nodered/queue_logic.js');

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

function nodeStub() {
  return {
    sent: [],
    warn() {},
    error() {},
    status() {},
    send(message) {
      this.sent.push(message);
    },
  };
}

function request(body, correlationId = body.correlationId) {
  return {
    payload: body,
    headers: {},
    req: { headers: { 'x-correlation-id': correlationId } },
  };
}

function ngPayload(overrides = {}) {
  return {
    schemaVersion: '1.0',
    correlationId: 'corr-ng-001',
    workOrderId: 'WO-MES-IDEM-001',
    serialNumber: 'SN-MES-IDEM-001',
    defectType: 'SCRATCH',
    confidence: 0.91,
    imageId: 'IMG-MES-IDEM-001',
    occurredAt: '2026-11-21T01:00:00.000Z',
    ...overrides,
  };
}

function completePayload(overrides = {}) {
  return {
    schemaVersion: '1.0',
    correlationId: 'corr-done-001',
    workOrderId: 'WO-MES-IDEM-DONE-001',
    inspectCount: 12,
    okCount: 9,
    ngCount: 3,
    ngRate: 0.25,
    ...overrides,
  };
}

check('NG receiver creates a receipt and records its fingerprint', () => {
  const { flow, values } = accessors();
  const result = mesNgOrder(request(ngPayload()), flow, nodeStub());
  assert.equal(result[0].statusCode, 200);
  assert.equal(result[0].payload.accepted, true);
  assert.match(result[0].payload.mesOrderNo, /^MES-\d{8}-0001$/);
  assert.equal(values.get('orders')['SN-MES-IDEM-001'], result[0].payload.mesOrderNo);
  assert.equal(typeof values.get('mesNgIdempotency')['SN-MES-IDEM-001'].fingerprint, 'string');
});

check('NG identical payload with reordered fields and new correlation is idempotent', () => {
  const { flow } = accessors();
  const first = mesNgOrder(request(ngPayload()), flow, nodeStub());
  const secondPayload = {
    occurredAt: '2026-11-21T01:00:00.000Z',
    imageId: 'IMG-MES-IDEM-001',
    confidence: 0.91,
    defectType: 'SCRATCH',
    serialNumber: 'SN-MES-IDEM-001',
    workOrderId: 'WO-MES-IDEM-001',
    correlationId: 'corr-ng-002',
    schemaVersion: '1.0',
  };
  const second = mesNgOrder(request(secondPayload), flow, nodeStub());
  assert.equal(second[0].statusCode, 200);
  assert.equal(second[0].payload.duplicated, true);
  assert.equal(second[0].payload.mesOrderNo, first[0].payload.mesOrderNo);
});

check('NG same serial with changed defect is rejected as an idempotency conflict', () => {
  const { flow } = accessors();
  mesNgOrder(request(ngPayload()), flow, nodeStub());
  const conflict = mesNgOrder(
    request(ngPayload({ correlationId: 'corr-ng-conflict', defectType: 'DENT' })),
    flow,
    nodeStub(),
  );
  assert.equal(conflict[0].statusCode, 409);
  assert.equal(conflict[0].payload.accepted, false);
  assert.equal(conflict[0].payload.errorCode, 'IDEMPOTENCY_CONFLICT');
});

check('NG same serial with a changed work order is rejected', () => {
  const { flow } = accessors();
  mesNgOrder(request(ngPayload()), flow, nodeStub());
  const conflict = mesNgOrder(
    request(ngPayload({ correlationId: 'corr-ng-wo-change', workOrderId: 'WO-OTHER' })),
    flow,
    nodeStub(),
  );
  assert.equal(conflict[0].statusCode, 409);
  assert.equal(conflict[0].payload.errorCode, 'IDEMPOTENCY_CONFLICT');
});

check('completion receiver creates a receipt and records its fingerprint', () => {
  const { flow, values } = accessors();
  const result = mesComplete(request(completePayload()), flow, nodeStub());
  assert.equal(result[0].statusCode, 200);
  assert.equal(result[0].payload.accepted, true);
  assert.match(result[0].payload.receiptNo, /^RC-\d{8}-0001$/);
  assert.equal(values.get('receipts')['WO-MES-IDEM-DONE-001'], result[0].payload.receiptNo);
  assert.equal(typeof values.get('mesCompleteIdempotency')['WO-MES-IDEM-DONE-001'].fingerprint, 'string');
});

check('completion identical statistics with new correlation is idempotent', () => {
  const { flow } = accessors();
  const first = mesComplete(request(completePayload()), flow, nodeStub());
  const second = mesComplete(
    request(completePayload({ correlationId: 'corr-done-002' })),
    flow,
    nodeStub(),
  );
  assert.equal(second[0].statusCode, 200);
  assert.equal(second[0].payload.duplicated, true);
  assert.equal(second[0].payload.receiptNo, first[0].payload.receiptNo);
});

check('completion same work order with changed statistics is rejected', () => {
  const { flow } = accessors();
  mesComplete(request(completePayload()), flow, nodeStub());
  const conflict = mesComplete(
    request(completePayload({ correlationId: 'corr-done-conflict', inspectCount: 13 })),
    flow,
    nodeStub(),
  );
  assert.equal(conflict[0].statusCode, 409);
  assert.equal(conflict[0].payload.errorCode, 'IDEMPOTENCY_CONFLICT');
});

check('prototype-named serial numbers remain isolated data keys', () => {
  const { flow } = accessors();
  const proto = mesNgOrder(
    request(ngPayload({ correlationId: 'corr-proto', serialNumber: '__proto__' })),
    flow,
    nodeStub(),
  );
  const constructor = mesNgOrder(
    request(ngPayload({ correlationId: 'corr-constructor', serialNumber: 'constructor' })),
    flow,
    nodeStub(),
  );
  assert.equal(proto[0].statusCode, 200);
  assert.equal(constructor[0].statusCode, 200);
  assert.notEqual(proto[0].payload.mesOrderNo, constructor[0].payload.mesOrderNo);
  assert.equal(Object.prototype.mesOrderNo, undefined);
  assert.equal({}.mesOrderNo, undefined);
});

check('legacy receipt is adopted once and later payload conflicts are rejected', () => {
  const { flow } = accessors({ orders: { 'SN-MES-IDEM-001': 'MES-LEGACY-0001' } });
  const duplicate = mesNgOrder(request(ngPayload()), flow, nodeStub());
  assert.equal(duplicate[0].payload.mesOrderNo, 'MES-LEGACY-0001');
  assert.equal(duplicate[0].payload.duplicated, true);
  const conflict = mesNgOrder(
    request(ngPayload({ correlationId: 'corr-legacy-conflict', defectType: 'STAIN' })),
    flow,
    nodeStub(),
  );
  assert.equal(conflict[0].statusCode, 409);
  assert.equal(conflict[0].payload.errorCode, 'IDEMPOTENCY_CONFLICT');
});

check('fingerprint ignores envelope fields but tracks business fields', () => {
  const original = ngPayload();
  const envelopeOnly = {
    ...ngPayload({ correlationId: 'corr-other' }),
    schemaVersion: '1.0',
    sourceEventId: 'evt-other',
  };
  const changed = ngPayload({ defectType: 'BURR' });
  assert.equal(mesFingerprint('ng', original), mesFingerprint('ng', envelopeOnly));
  assert.notEqual(mesFingerprint('ng', original), mesFingerprint('ng', changed));
});

console.log(`\n===== MES IDEMPOTENCY: ${passed}/${passed + failures.length} passed =====`);
if (failures.length > 0) {
  console.error(failures.map(failure => `${failure.name}: ${failure.error.stack}`).join('\n'));
  process.exitCode = 1;
}
