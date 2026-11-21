'use strict';

const assert = require('node:assert/strict');

const qlogic = require('../nodered/queue_logic.js');

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

function makeStore() {
  return {
    status() {
      return {
        queue: { pending: 0, sending: 0, retrying: 0, total: 0 },
        deadLetters: { total: 0, last: null },
        dedup: { ngFingerprints: 0, ngSerialNumbers: 0, completedWorkOrders: 0 },
        uaWriteback: { pending: 0, sending: 0 },
        storage: { mainBytes: 1, walBytes: 0, shmBytes: 0 },
      };
    },
    metricSnapshot() {
      return [];
    },
  };
}

function nodeRecorder() {
  return {
    warnings: [],
    errors: [],
    statuses: [],
    warn(message) {
      this.warnings.push(String(message));
    },
    error(message) {
      this.errors.push(String(message));
    },
    status(value) {
      this.statuses.push(value);
    },
  };
}

function outboxPayload(overrides = {}) {
  return JSON.stringify({
    pendingCount: 7,
    oldestPendingAt: '2026-11-21T00:00:00.000+00:00',
    oldestAgeSeconds: 42,
    capacity: 1000,
    highWatermark: 800,
    backpressure: false,
    lastAckAt: '2026-11-21T00:01:00.000+00:00',
    purgedTotal: 2,
    rejectedTotal: 1,
    updatedAt: '2026-11-21T00:01:01.000+00:00',
    ...overrides,
  });
}

check('recordOutboxStatus stores a Variant-wrapped JSON string', () => {
  const { flow } = accessors();
  const node = nodeRecorder();
  qlogic.recordOutboxStatus({ payload: { value: outboxPayload() } }, flow, node);
  const snapshot = qlogic.outboxSnapshot(flow);
  assert.equal(snapshot.pendingCount, 7);
  assert.equal(snapshot.oldestAgeSec, 42);
  assert.equal(snapshot.capacity, 1000);
  assert.equal(snapshot.highWatermark, 800);
  assert.equal(snapshot.backpressure, false);
  assert.equal(snapshot.purgedTotal, 2);
  assert.equal(snapshot.rejectedTotal, 1);
  assert.equal(snapshot.statusFresh, true);
  assert.equal(node.warnings.length, 0);
});

check('recordOutboxStatus publishes a visible backpressure state', () => {
  const { flow } = accessors();
  const node = nodeRecorder();
  qlogic.recordOutboxStatus(
    { payload: outboxPayload({ pendingCount: 800, backpressure: true }) },
    flow,
    node,
  );
  assert.equal(qlogic.outboxSnapshot(flow).backpressure, true);
  assert.equal(node.statuses.at(-1).fill, 'red');
  assert.match(node.statuses.at(-1).text, /800\/800/);
});

check('malformed outbox payload does not overwrite the last good state', () => {
  const { flow } = accessors();
  const node = nodeRecorder();
  qlogic.recordOutboxStatus({ payload: outboxPayload() }, flow, node);
  qlogic.recordOutboxStatus({ payload: '{not-json' }, flow, node);
  const snapshot = qlogic.outboxSnapshot(flow);
  assert.equal(snapshot.pendingCount, 7);
  assert.equal(node.warnings.length, 1);
});

check('missing outbox read is reported as never observed', () => {
  const { flow } = accessors();
  const snapshot = qlogic.outboxSnapshot(flow);
  assert.equal(snapshot.pendingCount, 0);
  assert.equal(snapshot.statusAgeSec, -1);
  assert.equal(snapshot.statusFresh, false);
});

check('health degrades when the outbox is under backpressure', () => {
  const { flow } = accessors();
  qlogic.recordOutboxStatus(
    { payload: outboxPayload({ pendingCount: 800, backpressure: true }) },
    flow,
    nodeRecorder(),
  );
  const msg = qlogic.health({}, flow, makeStore());
  assert.equal(msg.statusCode, 503);
  assert.equal(msg.payload.status, 'degraded');
  assert.equal(msg.payload.reasonCode, 'OUTBOX_BACKPRESSURE');
  assert.equal(msg.payload.outbox.pendingCount, 800);
});

check('health returns ready below the outbox high-water mark', () => {
  const { flow } = accessors();
  qlogic.recordOutboxStatus({ payload: outboxPayload() }, flow, nodeRecorder());
  const msg = qlogic.health({}, flow, makeStore());
  assert.equal(msg.statusCode, 200);
  assert.equal(msg.payload.status, 'ready');
  assert.equal(msg.payload.reasonCode, '');
});

check('status exposes the outbox snapshot', () => {
  const { flow } = accessors();
  qlogic.recordOutboxStatus({ payload: outboxPayload() }, flow, nodeRecorder());
  const msg = qlogic.status({}, flow, makeStore());
  assert.equal(msg.payload.outbox.pendingCount, 7);
  assert.equal(msg.payload.outbox.oldestAgeSec, 42);
});

check('metrics expose outbox pending, age, capacity and backpressure', () => {
  const { flow } = accessors();
  qlogic.recordOutboxStatus(
    { payload: outboxPayload({ pendingCount: 800, backpressure: true }) },
    flow,
    nodeRecorder(),
  );
  const msg = qlogic.metrics({}, flow, makeStore());
  assert.match(msg.payload, /^# HELP qc_event_outbox_pending/m);
  assert.match(msg.payload, /^qc_event_outbox_pending 800$/m);
  assert.match(msg.payload, /^qc_event_outbox_oldest_age_seconds 42$/m);
  assert.match(msg.payload, /^qc_event_outbox_capacity 1000$/m);
  assert.match(msg.payload, /^qc_event_outbox_high_watermark 800$/m);
  assert.match(msg.payload, /^qc_event_outbox_backpressure 1$/m);
  assert.match(msg.payload, /^qc_event_outbox_status_age_seconds \d+$/m);
});

check('capacity rejection keeps the event unacknowledged for UA replay', () => {
  const { flow } = accessors();
  const node = nodeRecorder();
  let response = {
    enqueued: false,
    duplicate: false,
    rejected: true,
    reason: 'capacity',
    queueLength: 100,
    capacity: 100,
  };
  const store = {
    enqueue() {
      return response;
    },
  };
  const event = {
    workOrderId: 'WO-CAPACITY',
    serialNumber: 'SN-CAPACITY',
    defectType: 'DENT',
    confidence: 0.9,
    imageId: 'IMG-CAPACITY',
    occurredAt: '2026-11-21T00:00:00.000Z',
    correlationId: 'corr-capacity',
    sourceEventId: 'evt-capacity',
  };

  const rejected = qlogic.enqueueNg(event, flow, node, store);
  assert.equal(rejected, null);
  assert.equal(node.errors.length, 1);
  assert.match(node.errors[0], /未 ACK/);
  assert.equal(flow.get('lastEnqueueRejection').queueLength, 100);

  response = {
    enqueued: false,
    duplicate: true,
    rejected: false,
    reason: 'dedup',
    queueLength: 100,
    capacity: 100,
  };
  const duplicate = qlogic.enqueueNg(event, flow, node, store);
  assert.equal(duplicate.action, 'method');
  assert.equal(duplicate.methodId, 'ns=2;s=QCStation.AcknowledgeEvent');
  assert.equal(duplicate.inputArguments[0].value, 'evt-capacity');

  const messages = qlogic.uaWriteMessages(
    {
      id: 'ng|SN-CAPACITY|2026-11-21T00:00:00.000Z',
      type: 'ng',
      correlationId: 'corr-capacity',
      body: { workOrderId: 'WO-CAPACITY' },
    },
    [
      { topic: 'ns=2;s=QCStation.LastNgMesOrderNo', value: 'MES-CAPACITY-0001' },
      { topic: 'ns=2;s=QCStation.LastMesOrderNo', value: 'MES-CAPACITY-0001' },
    ],
  );
  assert.equal(messages.length, 1);
  assert.equal(messages[0].action, 'method');
  assert.equal(messages[0].methodId, 'ns=2;s=QCStation.RecordMesReceipt');
  assert.deepEqual(messages[0].inputArguments.map(item => item.value), [
    'ng',
    'MES-CAPACITY-0001',
    'WO-CAPACITY',
    'corr-capacity',
  ]);
});

console.log(`\n===== OUTBOX STATUS: ${passed}/${passed + failures.length} passed =====`);
if (failures.length > 0) {
  console.error(failures.map(failure => `${failure.name}: ${failure.error.stack}`).join('\n'));
  process.exitCode = 1;
}
