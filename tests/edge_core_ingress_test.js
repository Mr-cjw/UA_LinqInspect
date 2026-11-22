'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const qlogic = require('../nodered/queue_logic.js');
const { SenderAuthorityGuard } = require('../nodered/authority_lock.js');
const flows = require('../nodered/flows.json');

process.env.QC_DELIVERY_AUTHORITY = 'edge-core';
process.env.EDGE_API_TOKEN = 'ingress-test-token';
process.env.QC_STATION_ID = 'QCStation';

const requests = [];
let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`PASS | ${name}`);
  } catch (error) {
    failures.push(`${name}: ${error.message}`);
    console.log(`FAIL | ${name} ${error.message}`);
  }
}

function flowValues() {
  const values = new Map();
  return {
    get(key) { return values.get(key); },
    set(key, value) { values.set(key, value); },
  };
}

const node = {
  warn() {},
  error() {},
  status() {},
};

async function main() {
  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', chunk => { raw += chunk; });
    req.on('end', () => {
      let body = {};
      try { body = JSON.parse(raw); } catch (_) {}
      requests.push({ url: req.url, headers: req.headers, body });
      if (body.serialNumber === 'SN-EDGE-INGRESS-FAIL') {
        res.writeHead(503, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ code: 'UNAVAILABLE', message: 'test outage' }));
        return;
      }
      res.writeHead(202, {
        'content-type': 'application/json',
        'idempotent-replay': 'false',
      });
      res.end(JSON.stringify({
        schemaVersion: '1.0',
        deliveryId: crypto.randomUUID(),
        correlationId: body.correlationId,
        state: 'PENDING',
      }));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  process.env.EDGE_API_BASE_URL = `http://127.0.0.1:${server.address().port}`;
  try {
    const flow = flowValues();
    const ng = {
      workOrderId: 'WO-EDGE-INGRESS-001',
      serialNumber: 'SN-EDGE-INGRESS-001',
      defectType: 'SCRATCH',
      confidence: 0.95,
      imageId: 'IMG-EDGE-INGRESS-001',
      occurredAt: '2026-11-21T00:00:00.000Z',
      correlationId: 'corr-edge-ingress-ng-0001',
      sourceEventId: 'evt-edge-ingress-ng-0001',
      modelVersion: 'synthetic-vision-mlp-v1',
      metadata: { datasetVersion: 'procedural-synthetic-v1' },
    };
    const ngAck = await qlogic.edgeCoreIngressNg(ng, flow, node);
    check('NG ingress posts the canonical event', () => {
      const request = requests[0];
      assert.equal(request.url, '/v1/inspection-events');
      assert.equal(request.body.eventType, 'NG_ORDER');
      assert.equal(request.body.result, 'NG');
      assert.equal(request.body.stationId, 'QCStation');
      assert.equal(request.body.workOrderId, ng.workOrderId);
      assert.equal(request.headers['x-correlation-id'], ng.correlationId);
      assert.match(request.headers['idempotency-key'], /^[0-9a-f-]{36}$/);
      assert.equal(request.headers.authorization, 'Bearer ingress-test-token');
      assert.equal(request.body.metadata.modelVersion, 'synthetic-vision-mlp-v1');
      assert.equal(request.body.metadata.datasetVersion, 'procedural-synthetic-v1');
      assert.equal(request.body.metadata.adapter, 'node-red');
    });
    check('NG ingress ACKs only after HTTP 202', () => {
      assert.equal(ngAck.action, 'method');
      assert.equal(ngAck._qcEventId, ng.sourceEventId);
      assert.equal(ngAck.methodId, 'ns=2;s=QCStation.AcknowledgeEvent');
    });

    const done = {
      workOrderId: 'WO-EDGE-INGRESS-001',
      inspectCount: 5,
      okCount: 4,
      ngCount: 1,
      ngRate: 0.2,
      occurredAt: '2026-11-21T00:00:01.000Z',
      correlationId: 'corr-edge-ingress-done-0001',
      sourceEventId: 'evt-edge-ingress-done-0001',
    };
    const doneAck = await qlogic.edgeCoreIngressDone(done, flow, node);
    check('completion ingress posts canonical statistics and ACKs', () => {
      const request = requests[1];
      assert.equal(request.body.eventType, 'COMPLETION_REPORT');
      assert.equal(request.body.inspectCount, 5);
      assert.equal(request.body.ngRate, 0.2);
      assert.equal(request.headers['x-correlation-id'], done.correlationId);
      assert.equal(doneAck._qcEventId, done.sourceEventId);
    });

    let rejected = null;
    try {
      await qlogic.edgeCoreIngressNg({
        ...ng,
        serialNumber: 'SN-EDGE-INGRESS-FAIL',
        correlationId: 'corr-edge-ingress-fail-0001',
        sourceEventId: 'evt-edge-ingress-fail-0001',
      }, flow, node);
    } catch (error) {
      rejected = error;
    }
    check('HTTP 503 leaves the UA event unacknowledged', () => {
      assert.ok(rejected);
      assert.match(rejected.message, /HTTP 503/);
    });

    check('Node-RED flow routes both event types to Edge Core ingress', () => {
      const ng = flows.find(item => item.id === 'fn_enq_ng').func;
      const done = flows.find(item => item.id === 'fn_enq_done').func;
      assert.match(ng, /edgeCoreIngressNg/);
      assert.match(done, /edgeCoreIngressDone/);
      assert.match(ng, /deliveryAuthority\(\) === 'edge-core'/);
      assert.match(done, /deliveryAuthority\(\) === 'edge-core'/);
    });

    const lockDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sender-authority-js-'));
    const firstGuard = new SenderAuthorityGuard({ dir: lockDir, owner: 'node-red', leaseMs: 10000 });
    const secondGuard = new SenderAuthorityGuard({ dir: lockDir, owner: 'edge-core', leaseMs: 10000 });
    check('sender authority lease has exactly one owner', () => {
      assert.equal(firstGuard.tryAcquire().ok, true);
      assert.equal(secondGuard.tryAcquire().ok, false);
      assert.equal(secondGuard.tryAcquire().owner, 'node-red');
    });
    check('released sender authority can be acquired by the other sender', () => {
      firstGuard.release();
      assert.equal(secondGuard.tryAcquire().ok, true);
      secondGuard.release();
    });
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
  if (failures.length) {
    console.log(`===== EDGE CORE INGRESS: ${passed}/${passed + failures.length} passed =====`);
    for (const failure of failures) console.log(`ERROR | ${failure}`);
    process.exitCode = 1;
  } else {
    console.log(`===== EDGE CORE INGRESS: ${passed}/${passed} passed =====`);
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
