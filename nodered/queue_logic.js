'use strict';

const crypto = require('node:crypto');
const { SenderAuthorityGuard } = require('./authority_lock');

const RETRY_DELAYS = [1000, 2000, 4000];

function mesBaseUrl(flow) {
  const configured = flow.get('mesBaseUrl') || process.env.NODERED_BASE || 'http://127.0.0.1:1880';
  return String(configured).replace(/\/+$/, '');
}

function timestamp() {
  return new Date().toISOString();
}

function fallbackCorrelationId() {
  return 'corr-fallback-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

function deliveryAuthority() {
  const value = String(process.env.QC_DELIVERY_AUTHORITY || 'node-red').trim().toLowerCase();
  if (value !== 'node-red' && value !== 'edge-core') {
    throw new Error('QC_DELIVERY_AUTHORITY must be node-red or edge-core');
  }
  return value;
}

let senderGuard = null;
let senderGuardWarnedFor = '';

function ensureNodeRedSenderLease(flow, node) {
  const authority = deliveryAuthority();
  if (authority !== 'node-red') {
    if (senderGuard) {
      senderGuard.release();
      senderGuard = null;
    }
    flow.set('senderAuthority', { owner: 'edge-core', held: false, currentOwner: 'edge-core', leaseFresh: true });
    return false;
  }
  if (!senderGuard) senderGuard = new SenderAuthorityGuard({ owner: 'node-red' });
  let result = senderGuard.tryAcquire();
  if (!result.ok) {
    flow.set('senderAuthority', senderGuard.status());
    const key = String(result.owner || 'unknown');
    if (key !== senderGuardWarnedFor) {
      node.error('[发送泵互锁] Node-RED 未获得发送权，当前持有者=' + key);
      senderGuardWarnedFor = key;
    }
    return false;
  }
  senderGuard.heartbeat();
  flow.set('senderAuthority', senderGuard.status());
  senderGuardWarnedFor = '';
  return true;
}

process.once('exit', () => {
  if (senderGuard) senderGuard.release();
});

function deterministicUuid(value) {
  const hex = crypto.createHash('sha256').update(String(value)).digest('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

function edgeCoreMetadata(msg, source) {
  const metadata = { adapter: 'node-red', source };
  const allowed = [
    'modelVersion', 'modelHash', 'modelSha256', 'datasetVersion',
    'imageSha256', 'imageHashScope', 'inferenceMs', 'qualityGate',
    'visionDecision', 'decisionReason',
  ];
  for (const key of allowed) {
    if (msg[key] !== undefined) metadata[key] = msg[key];
  }
  if (msg.metadata && typeof msg.metadata === 'object' && !Array.isArray(msg.metadata)) {
    for (const key of Object.keys(msg.metadata)) {
      if (key !== '__proto__' && key !== 'constructor' && key !== 'prototype') {
        metadata[key] = msg.metadata[key];
      }
    }
  }
  return metadata;
}

function edgeCoreSettings(flow) {
  const baseUrl = String(flow.get('edgeCoreBaseUrl') || process.env.EDGE_API_BASE_URL || 'http://127.0.0.1:8770')
    .replace(/\/+$/, '');
  const token = String(flow.get('edgeCoreToken') || process.env.EDGE_API_TOKEN || '').trim();
  const stationId = String(flow.get('stationId') || process.env.QC_STATION_ID || 'QCStation').trim();
  if (!token) throw new Error('EDGE_API_TOKEN is required for edge-core delivery authority');
  return { baseUrl, token, stationId };
}

async function postEdgeCoreIngress(flow, event, sourceEventId) {
  const settings = edgeCoreSettings(flow);
  const idempotencyKey = deterministicUuid(`${settings.stationId}:${sourceEventId}`);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(settings.baseUrl + '/v1/inspection-events', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: 'Bearer ' + settings.token,
        'x-correlation-id': event.correlationId,
        'idempotency-key': idempotencyKey,
      },
      body: JSON.stringify(event),
      signal: controller.signal,
    });
    const raw = await response.text();
    let payload = {};
    try { payload = raw ? JSON.parse(raw) : {}; } catch (_) { payload = {}; }
    if (response.status !== 202) {
      throw new Error('Edge Core ingest HTTP ' + response.status + ': ' +
        String(payload.error || payload.message || raw || 'rejected'));
    }
    if (!payload.deliveryId) throw new Error('Edge Core ingest response missing deliveryId');
    if (payload.correlationId && payload.correlationId !== event.correlationId) {
      throw new Error('Edge Core ingest correlationId mismatch');
    }
    return payload;
  } finally {
    clearTimeout(timeout);
  }
}

async function edgeCoreIngressNg(msg, flow, node) {
  if (deliveryAuthority() !== 'edge-core') return null;
  const correlationId = String(msg.correlationId || fallbackCorrelationId()).trim();
  const sourceEventId = String(msg.sourceEventId || correlationId).trim();
  const settings = edgeCoreSettings(flow);
  const event = {
    schemaVersion: '1.0',
    sourceEventId,
    stationId: settings.stationId,
    eventType: 'NG_ORDER',
    occurredAt: String(msg.occurredAt || new Date().toISOString()),
    result: 'NG',
    serialNumber: String(msg.serialNumber || '').trim(),
    workOrderId: String(msg.workOrderId || '').trim(),
    defectType: String(msg.defectType || '').trim(),
    confidence: msg.confidence,
    imageId: String(msg.imageId || '').trim(),
    correlationId,
    metadata: edgeCoreMetadata(msg, 'NgDetectedEvent'),
  };
  const delivery = await postEdgeCoreIngress(flow, event, sourceEventId);
  node.warn('[Edge Core入队][' + correlationId + '] NG ' + event.serialNumber +
    ' delivery=' + delivery.deliveryId);
  return eventAckMessage(msg, node);
}

async function edgeCoreIngressDone(msg, flow, node) {
  if (deliveryAuthority() !== 'edge-core') return null;
  const correlationId = String(msg.correlationId || fallbackCorrelationId()).trim();
  const sourceEventId = String(msg.sourceEventId || correlationId).trim();
  const settings = edgeCoreSettings(flow);
  const event = {
    schemaVersion: '1.0',
    sourceEventId,
    stationId: settings.stationId,
    eventType: 'COMPLETION_REPORT',
    occurredAt: String(msg.occurredAt || new Date().toISOString()),
    workOrderId: String(msg.workOrderId || '').trim(),
    inspectCount: Number(msg.inspectCount || 0),
    okCount: Number(msg.okCount || 0),
    ngCount: Number(msg.ngCount || 0),
    ngRate: Number(msg.ngRate == null ? -1 : msg.ngRate),
    correlationId,
    metadata: edgeCoreMetadata(msg, 'WorkOrderCompletedEvent'),
  };
  const delivery = await postEdgeCoreIngress(flow, event, sourceEventId);
  node.warn('[Edge Core入队][' + correlationId + '] 报工 ' + event.workOrderId +
    ' delivery=' + delivery.deliveryId);
  return eventAckMessage(msg, node);
}

function ngKeys(serialNumber, occurredAt) {
  return [
    { kind: 'ng_fp', key: `${serialNumber}|${occurredAt}` },
    { kind: 'ng_sn', key: serialNumber },
  ];
}

const MES_IDEMPOTENCY_STATE = {
  ng: 'mesNgIdempotency',
  complete: 'mesCompleteIdempotency',
};

function safeRecord(value) {
  const result = Object.create(null);
  if (!value || typeof value !== 'object' || Array.isArray(value)) return result;
  for (const key of Object.keys(value)) result[key] = value[key];
  return result;
}

function canonicalJson(value) {
  if (value === undefined) return 'null';
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map(key => JSON.stringify(key) + ':' + canonicalJson(value[key]))
    .join(',') + '}';
}

function mesFingerprint(kind, payload) {
  const ignored = new Set(kind === 'ng'
    ? ['schemaVersion', 'correlationId', 'sourceEventId']
    : ['schemaVersion', 'correlationId', 'sourceEventId']);
  const businessPayload = Object.create(null);
  for (const key of Object.keys(payload || {})) {
    if (!ignored.has(key)) businessPayload[key] = payload[key];
  }
  return crypto.createHash('sha256')
    .update(kind + '|' + canonicalJson(businessPayload))
    .digest('hex');
}

function mesIdempotency(flow, kind, businessKey, payload, existingReceipt = '') {
  const stateKey = MES_IDEMPOTENCY_STATE[kind];
  if (!stateKey) throw new Error('unsupported MES receipt kind: ' + kind);
  const key = String(businessKey || '');
  if (!key) throw new Error('empty MES idempotency key');
  const fingerprint = mesFingerprint(kind, payload);
  const records = safeRecord(flow.get(stateKey));
  const record = records[key];
  if (record && record.fingerprint) {
    if (record.fingerprint !== fingerprint) {
      return { state: 'conflict', fingerprint, receiptNo: String(record.receiptNo || '') };
    }
    return {
      state: 'duplicate',
      fingerprint,
      receiptNo: String(record.receiptNo || existingReceipt || ''),
    };
  }
  if (existingReceipt) {
    records[key] = {
      fingerprint,
      receiptNo: String(existingReceipt),
      seenAt: timestamp(),
    };
    flow.set(stateKey, records);
    return { state: 'duplicate-legacy', fingerprint, receiptNo: String(existingReceipt) };
  }
  return { state: 'new', fingerprint, receiptNo: '' };
}

function rememberMesReceipt(flow, kind, businessKey, fingerprint, receiptNo) {
  const stateKey = MES_IDEMPOTENCY_STATE[kind];
  if (!stateKey) throw new Error('unsupported MES receipt kind: ' + kind);
  const key = String(businessKey || '');
  if (!key) throw new Error('empty MES idempotency key');
  if (!fingerprint) throw new Error('empty MES idempotency fingerprint');
  if (!receiptNo) throw new Error('empty MES receipt number');
  const records = safeRecord(flow.get(stateKey));
  records[key] = {
    fingerprint: String(fingerprint),
    receiptNo: String(receiptNo),
    seenAt: timestamp(),
  };
  flow.set(stateKey, records);
  return String(receiptNo);
}

function activeMesMode(flow) {
  const now = Date.now();
  let mode = String(flow.get('mesMode') || 'ok').toLowerCase();
  const until = Number(flow.get('timeoutUntil') || 0);
  if (mode === 'timeout' && until && now >= until) {
    mode = 'ok';
    flow.set('mesMode', 'ok');
    flow.set('timeoutUntil', 0);
  }
  return mode;
}

function mesFailure(msg, statusCode, errorCode, error, headers, correlationId) {
  msg.statusCode = statusCode;
  msg.headers = Object.assign({}, msg.headers, headers || {});
  msg.payload = {
    accepted: false,
    schemaVersion: '1.0',
    correlationId: correlationId || '',
    errorCode,
    error,
  };
  return [msg, null];
}

function mesNgOrder(msg, flow, node) {
  const mode = activeMesMode(flow);
  const body = msg.payload;
  const requestHeaders = (msg.req && msg.req.headers) || {};
  const headerCorrelationId = String(requestHeaders['x-correlation-id'] || '').trim();
  const responseCorrelationId = (body && typeof body === 'object' && body.correlationId) || headerCorrelationId;
  const fail = (statusCode, errorCode, error, headers) =>
    mesFailure(msg, statusCode, errorCode, error, headers, responseCorrelationId);
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return fail(400, 'INVALID_BODY', 'invalid JSON body');
  }
  const required = ['schemaVersion', 'correlationId', 'workOrderId', 'serialNumber', 'defectType', 'confidence', 'imageId', 'occurredAt'];
  const missing = required.filter(key => !(key in body));
  if (missing.length) return fail(400, 'INVALID_BODY', 'missing fields: ' + missing.join(','));
  if (body.schemaVersion !== '1.0') {
    return fail(400, 'INVALID_SCHEMA_VERSION', 'schemaVersion must be 1.0');
  }
  if (!headerCorrelationId || headerCorrelationId !== body.correlationId) {
    return fail(400, 'CORRELATION_MISMATCH', 'X-Correlation-Id must match body.correlationId');
  }
  if (typeof body.occurredAt !== 'string' || !/Z$/.test(body.occurredAt)) {
    return fail(400, 'INVALID_TIMESTAMP', 'occurredAt must be ISO8601 UTC with Z');
  }
  const businessKey = String(body.serialNumber || '').trim();
  if (!businessKey) return fail(400, 'INVALID_SERIAL_NUMBER', 'serialNumber must be non-empty');

  const orders = safeRecord(flow.get('orders'));
  const existing = orders[businessKey] == null ? '' : String(orders[businessKey]);
  const verdict = mesIdempotency(flow, 'ng', businessKey, body, existing);
  if (verdict.state === 'conflict') {
    return fail(409, 'IDEMPOTENCY_CONFLICT', 'serialNumber already has a receipt for a different payload');
  }
  if (verdict.state === 'duplicate' || verdict.state === 'duplicate-legacy') {
    msg.statusCode = 200;
    msg.payload = {
      accepted: true,
      schemaVersion: '1.0',
      correlationId: body.correlationId,
      mesOrderNo: verdict.receiptNo || existing,
      duplicated: true,
    };
    return [msg, null];
  }

  const makeOrder = () => {
    const seq = (flow.get('mesSeq') || 0) + 1;
    flow.set('mesSeq', seq);
    const date = new Date();
    const no = 'MES-' + date.toISOString().slice(0, 10).replace(/-/g, '') + '-' + String(seq).padStart(4, '0');
    orders[businessKey] = no;
    flow.set('orders', orders);
    rememberMesReceipt(flow, 'ng', businessKey, verdict.fingerprint, no);
    return no;
  };

  if (mode === '4xx') return fail(400, 'MES_REJECTED', 'simulated business rejection');
  if (mode === '429') {
    return fail(429, 'MES_BUSY', 'simulated rate limit', { 'Retry-After': '1' });
  }
  if (mode === 'invalid2xx') {
    msg.statusCode = 200;
    msg.payload = {
      accepted: true,
      schemaVersion: '0.9',
      correlationId: 'corr-invalid',
      mesOrderNo: 'MES-INVALID',
    };
    return [msg, null];
  }
  if (mode === '5xx') return fail(500, 'MES_UNAVAILABLE', 'simulated 5xx');
  if (mode === 'timeout') {
    node.warn('[MES注入timeout][' + body.correlationId + '] NG单 ' + businessKey + ' 延迟 10s 响应');
    setTimeout(() => {
      try {
        if (msg.res && (msg.res.finished || msg.res.writableEnded)) {
          node.warn('[MES超时模拟] 客户端已超时断开，丢弃延迟响应');
          return;
        }
        const latestOrders = safeRecord(flow.get('orders'));
        const current = latestOrders[businessKey];
        const no = current || makeOrder();
        msg.statusCode = 200;
        msg.payload = {
          accepted: true,
          schemaVersion: '1.0',
          correlationId: body.correlationId,
          mesOrderNo: no,
          duplicated: Boolean(current),
        };
        node.send([msg, null]);
      } catch (error) {
        node.warn('[MES超时模拟] 响应失败：' + error.message);
      }
    }, 10000);
    return null;
  }
  const no = makeOrder();
  msg.statusCode = 200;
  msg.payload = {
    accepted: true,
    schemaVersion: '1.0',
    correlationId: body.correlationId,
    mesOrderNo: no,
  };
  return [msg, null];
}

function mesComplete(msg, flow, node) {
  const mode = activeMesMode(flow);
  const body = msg.payload;
  const requestHeaders = (msg.req && msg.req.headers) || {};
  const headerCorrelationId = String(requestHeaders['x-correlation-id'] || '').trim();
  const responseCorrelationId = (body && typeof body === 'object' && body.correlationId) || headerCorrelationId;
  const fail = (statusCode, errorCode, error, headers) =>
    mesFailure(msg, statusCode, errorCode, error, headers, responseCorrelationId);
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return fail(400, 'INVALID_BODY', 'invalid JSON body');
  }
  const required = ['schemaVersion', 'correlationId', 'workOrderId', 'inspectCount', 'okCount', 'ngCount', 'ngRate'];
  const missing = required.filter(key => !(key in body));
  if (missing.length) return fail(400, 'INVALID_BODY', 'missing fields: ' + missing.join(','));
  if (body.schemaVersion !== '1.0') {
    return fail(400, 'INVALID_SCHEMA_VERSION', 'schemaVersion must be 1.0');
  }
  if (!headerCorrelationId || headerCorrelationId !== body.correlationId) {
    return fail(400, 'CORRELATION_MISMATCH', 'X-Correlation-Id must match body.correlationId');
  }
  const businessKey = String(body.workOrderId || '').trim();
  if (!businessKey) return fail(400, 'INVALID_WORK_ORDER_ID', 'workOrderId must be non-empty');

  const receipts = safeRecord(flow.get('receipts'));
  const existing = receipts[businessKey] == null ? '' : String(receipts[businessKey]);
  const verdict = mesIdempotency(flow, 'complete', businessKey, body, existing);
  if (verdict.state === 'conflict') {
    return fail(409, 'IDEMPOTENCY_CONFLICT', 'workOrderId already has a receipt for different statistics');
  }
  if (verdict.state === 'duplicate' || verdict.state === 'duplicate-legacy') {
    msg.statusCode = 200;
    msg.payload = {
      accepted: true,
      schemaVersion: '1.0',
      correlationId: body.correlationId,
      receiptNo: verdict.receiptNo || existing,
      duplicated: true,
    };
    return [msg, null];
  }

  const makeReceipt = () => {
    const seq = (flow.get('rcSeq') || 0) + 1;
    flow.set('rcSeq', seq);
    const date = new Date();
    const no = 'RC-' + date.toISOString().slice(0, 10).replace(/-/g, '') + '-' + String(seq).padStart(4, '0');
    receipts[businessKey] = no;
    flow.set('receipts', receipts);
    rememberMesReceipt(flow, 'complete', businessKey, verdict.fingerprint, no);
    return no;
  };

  if (mode === '4xx') return fail(400, 'MES_REJECTED', 'simulated business rejection');
  if (mode === '429') {
    return fail(429, 'MES_BUSY', 'simulated rate limit', { 'Retry-After': '1' });
  }
  if (mode === 'invalid2xx') {
    msg.statusCode = 200;
    msg.payload = {
      accepted: true,
      schemaVersion: '0.9',
      correlationId: 'corr-invalid',
      receiptNo: 'RC-INVALID',
    };
    return [msg, null];
  }
  if (mode === '5xx') return fail(500, 'MES_UNAVAILABLE', 'simulated 5xx');
  if (mode === 'timeout') {
    node.warn('[MES注入timeout][' + body.correlationId + '] 报工 ' + businessKey + ' 延迟 10s 响应');
    setTimeout(() => {
      try {
        if (msg.res && (msg.res.finished || msg.res.writableEnded)) {
          node.warn('[MES超时模拟] 客户端已超时断开，丢弃延迟响应');
          return;
        }
        const latestReceipts = safeRecord(flow.get('receipts'));
        const current = latestReceipts[businessKey];
        const no = current || makeReceipt();
        msg.statusCode = 200;
        msg.payload = {
          accepted: true,
          schemaVersion: '1.0',
          correlationId: body.correlationId,
          receiptNo: no,
          duplicated: Boolean(current),
        };
        node.send([msg, null]);
      } catch (error) {
        node.warn('[MES超时模拟] 响应失败：' + error.message);
      }
    }, 10000);
    return null;
  }
  const no = makeReceipt();
  msg.statusCode = 200;
  msg.payload = {
    accepted: true,
    schemaVersion: '1.0',
    correlationId: body.correlationId,
    receiptNo: no,
  };
  return [msg, null];
}

const UA_WRITE_TOPICS = {
  ng: ['ns=2;s=QCStation.LastNgMesOrderNo', 'ns=2;s=QCStation.LastMesOrderNo'],
  complete: ['ns=2;s=QCStation.LastCompletionReceiptNo', 'ns=2;s=QCStation.LastMesOrderNo'],
};

function uaTargetsFor(item, receipt) {
  return (UA_WRITE_TOPICS[item.type] || []).map(topic => ({
    topic,
    value: String(receipt),
  }));
}

function uaWriteMessages(item, targets) {
  const receipt = (targets || [])
    .map(entry => entry.value == null ? entry.expectedValue : entry.value)
    .find(value => value != null && String(value) !== '');
  if (receipt == null) return [];
  const methodId = 'ns=2;s=QCStation.RecordMesReceipt';
  return [{
    action: 'method',
    _uaKind: 'write',
    objectId: 'ns=2;s=QCStation',
    methodId,
    topic: methodId,
    inputArguments: [
      { dataType: 'String', value: String(item.type) },
      { dataType: 'String', value: String(receipt) },
      { dataType: 'String', value: String(item.body && item.body.workOrderId || '') },
      { dataType: 'String', value: String(item.correlationId || '') },
    ],
    _qc: {
      id: item.id,
      type: item.type,
      correlationId: item.correlationId || '',
      uaTarget: 'RecordMesReceipt',
      receiptMethod: true,
      targets: targets || [],
    },
  }];
}

function pumpOutputs(result) {
  if (result && result.__qcOutputs) {
    return [result.http || [], result.ua || []];
  }
  return [result ? [result] : [], []];
}

function validateNg(msg, node, store) {
  const serialNumber = String(msg.serialNumber || '').trim();
  if (!serialNumber) {
    node.error('[契约校验失败] NG 事件缺少 serialNumber，已丢弃');
    return null;
  }
  const occurredAt = String(msg.occurredAt || '');
  if (store.hasDedup('ng_fp', `${serialNumber}|${occurredAt}`) || store.hasDedup('ng_sn', serialNumber)) {
    node.warn('[重复NG丢弃] serialNumber=' + serialNumber + ' occurredAt=' + occurredAt);
    msg._duplicateEvent = true;
  }
  return msg;
}

function validateDone(msg, node, store) {
  const workOrderId = String(msg.workOrderId || '').trim();
  if (!workOrderId) {
    node.error('[契约校验失败] 报工事件缺少 workOrderId，已丢弃');
    return null;
  }
  if (store.hasDedup('done', workOrderId)) {
    node.warn('[重复报工丢弃] ' + workOrderId);
    msg._duplicateEvent = true;
  }
  return msg;
}

function eventAckMessage(msg, node) {
  // New servers send SourceEventId. CorrelationId is a stable per-business-event
  // fallback for older subscriptions whose event filter does not expose it.
  const sourceEventId = String(msg.sourceEventId || msg.correlationId || '').trim();
  if (!sourceEventId) {
    node.warn('[事件ACK] 事件缺少 sourceEventId，等待服务端后续重发');
    return null;
  }
  return {
    action: 'method',
    _uaKind: 'ack',
    _qcEventId: sourceEventId,
    objectId: 'ns=2;s=QCStation',
    methodId: 'ns=2;s=QCStation.AcknowledgeEvent',
    topic: 'ns=2;s=QCStation.AcknowledgeEvent',
    inputArguments: [{ dataType: 'String', value: sourceEventId }],
  };
}

function asNonNegativeNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}

function unwrapUaValue(value) {
  let current = value;
  let guard = 0;
  while (current && typeof current === 'object' && current.value !== undefined && guard < 8) {
    current = current.value;
    guard += 1;
  }
  return current;
}

function outboxSnapshot(flow) {
  const pendingCount = asNonNegativeNumber(flow.get('outboxPendingCount'), 0);
  const capacity = asNonNegativeNumber(flow.get('outboxCapacity'), 0);
  const highWatermark = asNonNegativeNumber(flow.get('outboxHighWatermark'), 0);
  const oldestAgeSec = asNonNegativeNumber(flow.get('outboxOldestAgeSec'), 0);
  const receivedAt = asNonNegativeNumber(flow.get('outboxStatusReceivedAt'), 0);
  const statusAgeSec = receivedAt > 0
    ? Math.max(0, Math.round((Date.now() - receivedAt) / 1000))
    : -1;
  const configuredBackpressure = flow.get('outboxBackpressure') === true;
  const backpressure = configuredBackpressure ||
    (highWatermark > 0 && pendingCount >= highWatermark);
  return {
    pendingCount,
    oldestAgeSec,
    oldestPendingAt: String(flow.get('outboxOldestPendingAt') || ''),
    capacity,
    highWatermark,
    backpressure,
    lastAckAt: String(flow.get('outboxLastAckAt') || ''),
    purgedTotal: asNonNegativeNumber(flow.get('outboxPurgedTotal'), 0),
    rejectedTotal: asNonNegativeNumber(flow.get('outboxRejectedTotal'), 0),
    statusAgeSec,
    statusFresh: receivedAt > 0 && statusAgeSec <= 30,
  };
}

function recordOutboxStatus(msg, flow, node) {
  let raw = unwrapUaValue(msg.payload);
  if (typeof raw === 'string') {
    try {
      raw = JSON.parse(raw);
    } catch (error) {
      node.warn('[Outbox状态解析失败] ' + error.message);
      return null;
    }
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    node.warn('[Outbox状态解析失败] payload 不是对象: ' + JSON.stringify(raw));
    return null;
  }
  const pendingCount = Number(raw.pendingCount);
  if (!Number.isFinite(pendingCount) || pendingCount < 0) {
    node.warn('[Outbox状态校验失败] pendingCount 非法: ' + JSON.stringify(raw.pendingCount));
    return null;
  }
  const capacity = asNonNegativeNumber(raw.capacity, 0);
  const highWatermark = asNonNegativeNumber(raw.highWatermark, 0);
  flow.set('outboxPendingCount', Math.trunc(pendingCount));
  flow.set('outboxOldestAgeSec', asNonNegativeNumber(raw.oldestAgeSeconds, 0));
  flow.set('outboxOldestPendingAt', String(raw.oldestPendingAt || ''));
  flow.set('outboxCapacity', Math.trunc(capacity));
  flow.set('outboxHighWatermark', Math.trunc(highWatermark));
  flow.set('outboxBackpressure', raw.backpressure === true);
  flow.set('outboxLastAckAt', String(raw.lastAckAt || ''));
  flow.set('outboxPurgedTotal', Math.trunc(asNonNegativeNumber(raw.purgedTotal, 0)));
  flow.set('outboxRejectedTotal', Math.trunc(asNonNegativeNumber(raw.rejectedTotal, 0)));
  flow.set('outboxStatusReceivedAt', Date.now());
  node.status({
    fill: raw.backpressure === true ? 'red' : 'grey',
    shape: 'dot',
    text: 'Outbox=' + Math.trunc(pendingCount) + '/' + Math.trunc(highWatermark),
  });
  return null;
}

function enqueueNg(msg, flow, node, store) {
  const serialNumber = String(msg.serialNumber || '').trim();
  if (!serialNumber) {
    node.error('[契约校验失败] NG 事件缺少 serialNumber，已丢弃');
    return null;
  }
  const occurredAt = String(msg.occurredAt || '');
  const id = `ng|${serialNumber}|${occurredAt}`;
  const correlationId = msg.correlationId || fallbackCorrelationId();
  const baseUrl = mesBaseUrl(flow);
  try {
    const result = store.enqueue({
      id,
      type: 'ng',
      correlationId,
      url: baseUrl + '/mes/ng-order',
      body: {
        schemaVersion: '1.0',
        correlationId,
        workOrderId: msg.workOrderId,
        serialNumber: msg.serialNumber,
        defectType: msg.defectType,
        confidence: msg.confidence,
        imageId: msg.imageId,
        occurredAt: msg.occurredAt,
      },
      createdAt: msg.occurredAt,
      dedupKeys: ngKeys(serialNumber, occurredAt),
    });
    if (!result.enqueued) {
      if (result.duplicate) {
        node.warn('[重复入队丢弃][' + correlationId + '] ' + id);
        return eventAckMessage(msg, node);
      }
      const rejection = {
        correlationId,
        type: 'ng',
        id,
        reason: result.reason || 'capacity',
        queueLength: result.queueLength || 0,
        capacity: result.capacity || 0,
        at: timestamp(),
      };
      flow.set('lastEnqueueRejection', rejection);
      node.error('[SQLite入队拒绝][' + correlationId + '] NG ' + serialNumber +
        ' 队列=' + rejection.queueLength + '/' + rejection.capacity +
        '；事件未 ACK，保留在 UA outbox 等待重试', msg);
      node.status({ fill: 'red', shape: 'ring', text: '队列拒收 ' + rejection.queueLength + '/' + rejection.capacity });
      return null;
    }
    node.status({ fill: 'yellow', shape: 'dot', text: '队列=' + result.queueLength });
    node.warn('[入队][' + correlationId + '] NG ' + serialNumber + ' 队列=' + result.queueLength);
    return eventAckMessage(msg, node);
  } catch (error) {
    node.error('[SQLite入队失败][' + correlationId + '] ' + error.message, msg);
  }
  return null;
}

function enqueueDone(msg, flow, node, store) {
  const workOrderId = String(msg.workOrderId || '').trim();
  if (!workOrderId) {
    node.error('[契约校验失败] 报工事件缺少 workOrderId，已丢弃');
    return null;
  }
  const id = `done|${workOrderId}`;
  const correlationId = msg.correlationId || fallbackCorrelationId();
  const baseUrl = mesBaseUrl(flow);
  try {
    const result = store.enqueue({
      id,
      type: 'complete',
      correlationId,
      url: baseUrl + '/mes/complete',
      body: {
        schemaVersion: '1.0',
        correlationId,
        workOrderId: msg.workOrderId,
        inspectCount: msg.inspectCount,
        okCount: msg.okCount,
        ngCount: msg.ngCount,
        ngRate: msg.ngRate,
      },
      createdAt: msg.occurredAt,
      dedupKeys: [{ kind: 'done', key: workOrderId }],
    });
    if (!result.enqueued) {
      if (result.duplicate) {
        node.warn('[重复入队丢弃][' + correlationId + '] ' + id);
        return eventAckMessage(msg, node);
      }
      const rejection = {
        correlationId,
        type: 'complete',
        id,
        reason: result.reason || 'capacity',
        queueLength: result.queueLength || 0,
        capacity: result.capacity || 0,
        at: timestamp(),
      };
      flow.set('lastEnqueueRejection', rejection);
      node.error('[SQLite入队拒绝][' + correlationId + '] 报工 ' + workOrderId +
        ' 队列=' + rejection.queueLength + '/' + rejection.capacity +
        '；事件未 ACK，保留在 UA outbox 等待重试', msg);
      node.status({ fill: 'red', shape: 'ring', text: '队列拒收 ' + rejection.queueLength + '/' + rejection.capacity });
      return null;
    }
    node.status({ fill: 'yellow', shape: 'dot', text: '队列=' + result.queueLength });
    node.warn('[入队][' + correlationId + '] 报工 ' + workOrderId + ' 队列=' + result.queueLength);
    return eventAckMessage(msg, node);
  } catch (error) {
    node.error('[SQLite入队失败][' + correlationId + '] ' + error.message, msg);
  }
  return null;
}

function pump(msg, flow, node, store) {
  if (!ensureNodeRedSenderLease(flow, node)) {
    node.status({ fill: 'blue', shape: 'ring', text: '发送泵未持有发送权' });
    return null;
  }
  let result;
  try {
    result = store.claimNext(Date.now());
  } catch (error) {
    node.error('[SQLite发送泵失败] ' + error.message);
    return null;
  }

  if (result.state === 'ua-write') {
    node.warn('[UA写回][' + (result.item.correlationId || '') + '] ' + result.item.id +
      ' → ' + (result.targets || []).map(target => target.target).join(', '));
    node.status({ fill: 'blue', shape: 'dot', text: 'UA写回 ' + result.item.type });
    return {
      __qcOutputs: true,
      http: [],
      ua: uaWriteMessages(result.item, result.targets),
    };
  }
  if (result.state === 'ua-write-complete') {
    node.warn('[UA写回完成][' + (result.item.correlationId || '') + '] ' + result.item.id);
    return null;
  }
  if (result.state === 'ua-write-retry') {
    node.warn('[UA写回重试][' + (result.item.correlationId || '') + '] ' + result.delay + 'ms');
    node.status({ fill: 'yellow', shape: 'ring', text: 'UA写回退避 ' + result.delay + 'ms' });
    return null;
  }
  if (result.state === 'ua-write-dead') {
    const item = result.item;
    flow.set('lastResponse', {
      correlationId: item.correlationId || '',
      type: item.type,
      status: 'DEAD',
      errorCode: item.errorCode,
      at: timestamp(),
    });
    node.error('[死信FAILED][' + (item.correlationId || '') + '][' + item.errorCode + '] ' +
      item.type + ' ' + item.id + '：' + item.lastStatus + '，需人工处置', item.body);
    node.status({ fill: 'red', shape: 'dot', text: '死信 ' + item.errorCode });
    return null;
  }
  if (result.state === 'ua-write-inflight') {
    node.status({ fill: 'blue', shape: 'dot', text: 'UA写回在途，队列=' + result.queueLength });
    return null;
  }
  if (result.state === 'inflight') {
    node.status({ fill: 'blue', shape: 'dot', text: '在途 1 条，队列=' + result.queueLength });
    return null;
  }
  if (result.state === 'idle') {
    node.status({ fill: 'green', shape: 'dot', text: '空闲，队列=' + result.queueLength });
    return null;
  }
  if (result.state === 'watchdog-retry') {
    node.warn('[重试][' + (result.item.correlationId || '') + '] 看门狗判定超时，' +
      result.delay + 'ms 后第 ' + result.item.attempts + ' 次重试');
    node.status({ fill: 'yellow', shape: 'ring', text: '看门狗退避 ' + result.delay + 'ms' });
    return null;
  }
  if (result.state === 'dead') {
    const item = result.item;
    flow.set('lastResponse', {
      correlationId: item.correlationId || '',
      type: item.type,
      status: 'DEAD',
      errorCode: item.errorCode,
      at: timestamp(),
    });
    node.error('[死信FAILED][' + (item.correlationId || '') + '][' + item.errorCode + '] ' +
      item.type + ' ' + item.id + '：' + item.lastStatus + '，需人工处置', item.body);
    node.status({ fill: 'red', shape: 'dot', text: '死信 ' + item.errorCode });
    return null;
  }
  if (result.state !== 'send' || !result.item) return null;

  const item = result.item;
  const requestMeta = {
    correlationId: item.correlationId || '',
    type: item.type,
    id: item.id,
    schemaVersion: (item.body && item.body.schemaVersion) || '',
    attempt: (item.attempts || 0) + 1,
    at: timestamp(),
  };
  flow.set('lastRequest', requestMeta);
  const lastByType = flow.get('lastRequestByType') || {};
  lastByType[item.type] = requestMeta;
  flow.set('lastRequestByType', lastByType);
  msg.url = item.url;
  msg.payload = item.body;
  const httpUser = process.env.NODERED_HTTP_USER || '';
  const httpPass = process.env.NODERED_HTTP_PASS || '';
  const httpHeaders = {
    'Content-Type': 'application/json',
    'X-Correlation-Id': item.correlationId || '',
  };
  if (httpUser && httpPass) {
    httpHeaders.Authorization = 'Basic ' + Buffer.from(`${httpUser}:${httpPass}`, 'utf8').toString('base64');
  }
  msg.headers = Object.assign({}, msg.headers, httpHeaders);
  msg._qc = { id: item.id, type: item.type, correlationId: item.correlationId || '' };
  node.status({ fill: 'blue', shape: 'dot', text: '发送 ' + item.type + ' ' + item.id });
  return msg;
}

function response(msg, flow, node, store) {
  const meta = msg._qc || {};
  let item;
  try {
    item = store.getInflight(meta.id || '', meta.correlationId || '');
  } catch (error) {
    node.error('[SQLite响应查询失败] ' + error.message, msg);
    return null;
  }
  if (!item) {
    node.warn('[响应忽略] 队列项不存在或已完成：' + (meta.id || '(无指纹)'));
    return null;
  }

  const statusCode = Number(msg.statusCode) || 0;
  const body = (msg.payload && typeof msg.payload === 'object' && !Array.isArray(msg.payload))
    ? msg.payload : {};
  const headers = msg.headers || {};
  const headerValue = name => {
    const wanted = String(name).toLowerCase();
    for (const key of Object.keys(headers)) {
      if (String(key).toLowerCase() === wanted) return headers[key];
    }
    return '';
  };
  const errorText = msg.error ? String(msg.error.message || msg.error.code || msg.error) : '';
  const correlationId = item.correlationId || meta.correlationId || '';
  const ok = !msg.error && statusCode >= 200 && statusCode < 300;

  const deadLetter = (errorCode, reason) => {
    let dead;
    try {
      dead = store.deadLetter({
        id: item.id,
        correlationId: item.correlationId || '',
        errorCode,
        reason,
      });
    } catch (error) {
      node.error('[SQLite死信写入失败] ' + error.message, item.body);
      return;
    }
    if (!dead) {
      node.warn('[响应忽略] 死信状态迁移失败或任务已结束：' + item.id);
      return;
    }
    flow.set('lastResponse', {
      correlationId,
      type: item.type,
      status: 'DEAD',
      statusCode,
      errorCode,
      at: timestamp(),
    });
    node.error('[死信FAILED][' + correlationId + '][' + errorCode + '] ' + item.type + ' ' + item.id +
      '：' + reason + '，需人工处置', item.body);
    node.status({ fill: 'red', shape: 'dot', text: '死信 ' + errorCode });
  };

  if (ok) {
    let contractCode = '';
    if (body.schemaVersion !== '1.0') contractCode = 'INVALID_RESPONSE_SCHEMA';
    else if (!body.correlationId || body.correlationId !== correlationId) contractCode = 'CORRELATION_MISMATCH';
    else if (body.accepted !== true) contractCode = body.errorCode || 'MES_REJECTED';
    else if (item.type === 'ng' && !body.mesOrderNo) contractCode = 'MISSING_MES_ORDER_NO';
    else if (item.type === 'complete' && !body.receiptNo) contractCode = 'MISSING_RECEIPT_NO';
    if (contractCode) {
      deadLetter(contractCode, 'HTTP ' + statusCode + ' 响应契约不合法');
      return null;
    }

    const receiptNo = item.type === 'ng' ? body.mesOrderNo : body.receiptNo;
    const targets = uaTargetsFor(item, receiptNo);
    let completed;
    try {
      completed = store.complete({
        id: item.id,
        correlationId: item.correlationId || '',
        receipt: receiptNo,
        targets,
      });
    } catch (error) {
      node.error('[SQLite完成事务失败] ' + error.message, item.body);
      return null;
    }
    if (!completed) {
      node.warn('[响应忽略] 任务已进入写回阶段或已被其他响应完成：' + item.id);
      return null;
    }
    flow.set('lastResponse', {
      correlationId,
      type: item.type,
      status: 'UA_WRITE_PENDING',
      mesStatus: 'SUCCESS',
      statusCode,
      receiptNo,
      at: timestamp(),
    });
    if (item.type === 'ng') {
      const orderNo = body.mesOrderNo;
      node.warn('[回执][' + correlationId + '] MES工单号=' + orderNo + ' → 等待 UA 写回确认');
      node.status({ fill: 'blue', shape: 'dot', text: '开单成功，待UA确认 ' + orderNo });
      return uaWriteMessages(completed, targets);
    }
    node.warn('[报工回执][' + correlationId + '] receiptNo=' + receiptNo + ' → 等待 UA 写回确认');
    node.status({ fill: 'blue', shape: 'dot', text: '报工成功，待UA确认 ' + receiptNo });
    return uaWriteMessages(completed, targets);
  }

  if (statusCode >= 400 && statusCode < 500 && statusCode !== 429) {
    deadLetter(body.errorCode || 'MES_HTTP_4XX', errorText || ('HTTP ' + statusCode));
    return null;
  }

  const errorCode = body.errorCode || (statusCode === 429 ? 'MES_BUSY' :
    statusCode >= 500 ? 'MES_UNAVAILABLE' : errorText ? 'TRANSPORT_ERROR' : 'UNKNOWN_ERROR');
  const reason = errorText || ('HTTP ' + statusCode);
  let delay = RETRY_DELAYS[Math.min(item.attempts || 0, RETRY_DELAYS.length - 1)];
  if (statusCode === 429) {
    const retryAfter = headerValue('retry-after');
    const seconds = Number(retryAfter);
    if (isFinite(seconds) && retryAfter !== '' && seconds >= 0) {
      delay = Math.min(30000, Math.max(100, seconds * 1000));
    } else {
      const parsed = Date.parse(retryAfter);
      if (isFinite(parsed)) delay = Math.min(30000, Math.max(100, parsed - Date.now()));
    }
  }

  let retried;
  try {
    retried = store.retry({
      id: item.id,
      correlationId: item.correlationId || '',
      delayMs: delay,
      errorCode,
      reason,
    });
  } catch (error) {
    node.error('[SQLite重试事务失败] ' + error.message, item.body);
    return null;
  }
  if (!retried) {
    node.warn('[响应忽略] 重试状态迁移失败或任务已结束：' + item.id);
    return null;
  }
  if (retried.outcome === 'dead') {
    flow.set('lastResponse', {
      correlationId,
      type: item.type,
      status: 'DEAD',
      statusCode,
      errorCode,
      at: timestamp(),
    });
    node.error('[死信FAILED][' + correlationId + '][' + errorCode + '] ' + item.type + ' ' + item.id +
      '：' + reason + '，需人工处置', item.body);
    node.status({ fill: 'red', shape: 'dot', text: '死信 ' + errorCode });
    return null;
  }

  flow.set('lastResponse', {
    correlationId,
    type: item.type,
    status: 'RETRY',
    statusCode,
    errorCode,
    attempt: retried.item.attempts,
    at: timestamp(),
  });
  node.warn('[重试][' + correlationId + '][' + errorCode + '] ' + item.type + ' ' + item.id +
    ' 失败(' + reason + ')，' + retried.delay + 'ms 后第 ' + retried.item.attempts + ' 次重试');
  node.status({ fill: 'yellow', shape: 'ring', text: '退避 ' + retried.delay + 'ms' });
  return null;
}

function uaWriteSuccess(msg, flow, node, store) {
  const meta = msg._qc || {};
  if (!meta.id || !meta.uaTarget) return null;
  if (meta.receiptMethod) {
    const raw = msg.payload;
    let outputs = null;
    if (Array.isArray(raw)) outputs = raw;
    else if (raw && Array.isArray(raw.OutputArguments)) outputs = raw.OutputArguments;
    else if (raw && Array.isArray(raw.outputArguments)) outputs = raw.outputArguments;
    else if (raw && Array.isArray(raw.value)) outputs = raw.value;
    const accepted = outputs && outputs.length ? unwrapUaValue(outputs[0]) : null;
    const summaryValue = outputs && outputs.length > 1 ? unwrapUaValue(outputs[1]) : '';
    const summary = summaryValue == null ? '' : String(summaryValue);
    if (accepted !== true) {
      uaWriteFailure({
        _qc: meta,
        error: { message: summary || 'RECEIPT_METHOD_REJECTED' },
      }, flow, node, store);
      return null;
    }
    let receiptResult;
    try {
      receiptResult = store.confirmUaWriteReceipt({
        id: meta.id,
        correlationId: meta.correlationId || '',
      });
    } catch (error) {
      node.error('[UA回执确认失败] ' + error.message);
      return null;
    }
    if (!receiptResult || receiptResult.outcome !== 'complete') {
      node.warn('[UA回执确认忽略] ' + meta.id);
      return null;
    }
    flow.set('lastResponse', Object.assign({}, flow.get('lastResponse') || {}, {
      status: 'SUCCESS',
      uaWrite: 'confirmed',
      at: timestamp(),
    }));
    node.warn('[UA写回完成][' + (meta.correlationId || '') + '] ' + meta.id +
      ' RecordMesReceipt 已确认');
    node.status({ fill: 'green', shape: 'dot', text: 'UA写回完成 ' + meta.id });
    return null;
  }
  let result;
  try {
    result = store.confirmUaWriteTarget({
      id: meta.id,
      correlationId: meta.correlationId || '',
      target: meta.uaTarget,
      value: undefined,
    });
  } catch (error) {
    node.error('[UA写回确认失败] ' + error.message);
    return null;
  }
  if (!result) {
    node.warn('[UA写回确认忽略] ' + meta.id + ' ' + meta.uaTarget);
    return null;
  }
  if (result.outcome === 'mismatch') {
    node.error('[UA写回值不一致] ' + meta.uaTarget + ' expected=' + result.expected +
      ' actual=' + result.actual);
    uaWriteFailure({
      _qc: meta,
      error: { message: 'UA_WRITE_VALUE_MISMATCH' },
    }, flow, node, store);
    return null;
  }
  if (result.outcome === 'complete') {
    flow.set('lastResponse', Object.assign({}, flow.get('lastResponse') || {}, {
      status: 'SUCCESS',
      uaWrite: 'confirmed',
      at: timestamp(),
    }));
    node.warn('[UA写回完成][' + (meta.correlationId || '') + '] ' + meta.id + ' 全部变量已确认');
    node.status({ fill: 'green', shape: 'dot', text: 'UA写回完成 ' + meta.id });
    return null;
  }
  node.warn('[UA写回确认][' + (meta.correlationId || '') + '] ' + meta.uaTarget +
    '，剩余 ' + result.confirmed);
  return null;
}

function uaWriteFailure(msg, flow, node, store) {
  const meta = msg._qc || {};
  if (!meta.id || !meta.uaTarget) return null;
  const detail = msg.error ? String(msg.error.message || msg.error.code || msg.error) : 'unknown';
  const errorCode = /VALUE_MISMATCH/.test(detail) ? 'UA_WRITE_VALUE_MISMATCH' : 'UA_WRITE_FAILED';
  let result;
  try {
    result = store.failUaWrite({
      id: meta.id,
      correlationId: meta.correlationId || '',
      errorCode,
      reason: detail,
    });
  } catch (error) {
    node.error('[UA写回失败迁移异常] ' + error.message);
    return null;
  }
  if (!result) return null;
  if (result.outcome === 'dead') {
    flow.set('lastResponse', {
      correlationId: meta.correlationId || '',
      type: meta.type || '',
      status: 'DEAD',
      errorCode,
      at: timestamp(),
    });
    node.error('[死信FAILED][' + (meta.correlationId || '') + '][' + errorCode + '] ' +
      meta.id + '：' + detail + '，需人工处置');
    node.status({ fill: 'red', shape: 'dot', text: '死信 ' + errorCode });
    return null;
  }
  node.warn('[UA写回失败][' + (meta.correlationId || '') + '] ' + meta.uaTarget + '：' +
    detail + '，' + result.delay + 'ms 后重试');
  node.status({ fill: 'yellow', shape: 'ring', text: 'UA写回退避 ' + result.delay + 'ms' });
  return null;
}

function prometheusValue(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(number) : '0';
}

function prometheusLabel(value) {
  return String(value == null ? '' : value)
    .replace(/\\/g, '\\\\')
    .replace(/\n/g, '\\n')
    .replace(/"/g, '\\"');
}

function metricKey(name, type = '', outcome = '', errorCode = '') {
  return [name, type, outcome, errorCode].join('\u0000');
}

function renderMetrics(snapshot, state, heartbeatFailures, uptimeSec, outbox = {}) {
  const values = new Map();
  for (const item of snapshot) {
    values.set(metricKey(item.name, item.type, item.outcome, item.errorCode), item.value);
  }
  const value = (name, type = '', outcome = '', errorCode = '') =>
    values.get(metricKey(name, type, outcome, errorCode)) || 0;
  const lines = [];
  const family = (name, help, type) => {
    lines.push(`# HELP ${name} ${help}`);
    lines.push(`# TYPE ${name} ${type}`);
  };

  family('qc_queue_depth', 'Current queue depth by status.', 'gauge');
  lines.push(`qc_queue_depth{status="pending"} ${prometheusValue(state.queue.pending)}`);
  lines.push(`qc_queue_depth{status="sending"} ${prometheusValue(state.queue.sending)}`);
  family('qc_queue_oldest_age_seconds', 'Age in seconds of the oldest queued item.', 'gauge');
  lines.push(`qc_queue_oldest_age_seconds ${prometheusValue(state.queue.oldestAgeSec)}`);
  family('qc_dead_letters', 'Current dead-letter records retained in SQLite.', 'gauge');
  lines.push(`qc_dead_letters ${prometheusValue(state.deadLetters.total)}`);
  family('qc_dedup_keys', 'Retained deduplication keys by business key kind.', 'gauge');
  lines.push(`qc_dedup_keys{kind="ng_fingerprint"} ${prometheusValue(state.dedup.ngFingerprints)}`);
  lines.push(`qc_dedup_keys{kind="ng_serial"} ${prometheusValue(state.dedup.ngSerialNumbers)}`);
  lines.push(`qc_dedup_keys{kind="work_order"} ${prometheusValue(state.dedup.completedWorkOrders)}`);
  family('qc_heartbeat_failures', 'Consecutive OPC UA heartbeat failures (0-3).', 'gauge');
  lines.push(`qc_heartbeat_failures ${prometheusValue(heartbeatFailures)}`);
  family('qc_event_outbox_pending', 'NG/completion events awaiting Node-RED acknowledgement in the UA outbox.', 'gauge');
  lines.push(`qc_event_outbox_pending ${prometheusValue(outbox.pendingCount)}`);
  family('qc_event_outbox_oldest_age_seconds', 'Age in seconds of the oldest unacknowledged UA outbox event.', 'gauge');
  lines.push(`qc_event_outbox_oldest_age_seconds ${prometheusValue(outbox.oldestAgeSec)}`);
  family('qc_event_outbox_capacity', 'Configured hard capacity of the UA event outbox.', 'gauge');
  lines.push(`qc_event_outbox_capacity ${prometheusValue(outbox.capacity)}`);
  family('qc_event_outbox_high_watermark', 'Configured backpressure threshold of the UA event outbox.', 'gauge');
  lines.push(`qc_event_outbox_high_watermark ${prometheusValue(outbox.highWatermark)}`);
  family('qc_event_outbox_backpressure', '1 when the UA outbox has reached its backpressure threshold.', 'gauge');
  lines.push(`qc_event_outbox_backpressure ${outbox.backpressure ? 1 : 0}`);
  family('qc_event_outbox_status_age_seconds', 'Age of the last successful OutboxStatus read; -1 means never observed.', 'gauge');
  lines.push(`qc_event_outbox_status_age_seconds ${prometheusValue(outbox.statusAgeSec)}`);
  family('qc_uptime_seconds', 'Seconds since this Node-RED flow was first observed.', 'gauge');
  lines.push(`qc_uptime_seconds ${prometheusValue(uptimeSec)}`);
  const storage = state.storage || {};
  family('qc_database_bytes', 'SQLite database, WAL and shared-memory file sizes in bytes.', 'gauge');
  lines.push(`qc_database_bytes{kind="main"} ${prometheusValue(storage.mainBytes)}`);
  lines.push(`qc_database_bytes{kind="wal"} ${prometheusValue(storage.walBytes)}`);
  lines.push(`qc_database_bytes{kind="shm"} ${prometheusValue(storage.shmBytes)}`);

  family('qc_enqueue_total', 'Enqueue attempts by event type and outcome.', 'counter');
  for (const type of ['ng', 'complete']) {
    for (const outcome of ['accepted', 'duplicate', 'rejected']) {
      lines.push(
        `qc_enqueue_total{type="${type}",outcome="${outcome}"} ` +
        prometheusValue(value('enqueue_total', type, outcome))
      );
    }
  }

  family('qc_delivery_total', 'Delivery outcomes by event type.', 'counter');
  for (const type of ['ng', 'complete']) {
    for (const outcome of ['success', 'retry', 'dead']) {
      lines.push(
        `qc_delivery_total{type="${type}",outcome="${outcome}"} ` +
        prometheusValue(value('delivery_total', type, outcome))
      );
    }
  }

  family('qc_delivery_errors_total', 'Delivery failures by event type and stable error code.', 'counter');
  const errorRows = snapshot.filter(item => item.name === 'delivery_errors_total');
  for (const item of errorRows) {
    lines.push(
      `qc_delivery_errors_total{type="${prometheusLabel(item.type)}",` +
      `error_code="${prometheusLabel(item.errorCode)}"} ` +
      prometheusValue(item.value)
    );
  }

  family('qc_watchdog_total', 'Watchdog outcomes for stale in-flight requests.', 'counter');
  for (const outcome of ['recovered', 'dead']) {
    lines.push(
      `qc_watchdog_total{outcome="${outcome}"} ` +
      prometheusValue(value('watchdog_total', '', outcome))
    );
  }

  const uaWriteback = state.uaWriteback || { pending: 0, sending: 0 };
  family('qc_ua_write_pending', 'Queued UA write-backs awaiting all target confirmations.', 'gauge');
  lines.push(`qc_ua_write_pending ${prometheusValue(
    Number(uaWriteback.pending || 0) + Number(uaWriteback.sending || 0)
  )}`);
  family('qc_ua_write_total', 'UA write-back outcomes by event type.', 'counter');
  for (const type of ['ng', 'complete']) {
    for (const outcome of ['success', 'retry', 'dead']) {
      lines.push(
        `qc_ua_write_total{type="${type}",outcome="${outcome}"} ` +
        prometheusValue(value('ua_write_total', type, outcome))
      );
    }
  }
  family('qc_ua_write_errors_total', 'UA write-back failures by event type and stable error code.', 'counter');
  const uaErrorRows = snapshot.filter(item => item.name === 'ua_write_errors_total');
  for (const item of uaErrorRows) {
    lines.push(
      `qc_ua_write_errors_total{type="${prometheusLabel(item.type)}",` +
      `error_code="${prometheusLabel(item.errorCode)}"} ` +
      prometheusValue(item.value)
    );
  }
  return lines.join('\n') + '\n';
}

function metrics(msg, flow, store) {
  const snapshot = store.metricSnapshot();
  const state = store.status();
  const startedAt = flow.get('edgeStartedAt') || Date.now();
  if (!flow.get('edgeStartedAt')) flow.set('edgeStartedAt', startedAt);
  const uptimeSec = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  msg.payload = renderMetrics(
    snapshot,
    state,
    flow.get('hbFails') || 0,
    uptimeSec,
    outboxSnapshot(flow)
  );
  msg.headers = {
    'content-type': 'text/plain; version=0.0.4; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  };
  return msg;
}

function health(msg, flow, store) {
  const startedAt = flow.get('edgeStartedAt') || Date.now();
  if (!flow.get('edgeStartedAt')) flow.set('edgeStartedAt', startedAt);
  const uptimeSec = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  const heartbeatFailures = Number(flow.get('hbFails') || 0);
  const outbox = outboxSnapshot(flow);
  try {
    const snapshot = store.status();
    const ok = heartbeatFailures < 3 && !outbox.backpressure;
    msg.statusCode = ok ? 200 : 503;
    msg.payload = {
      ok,
      service: 'ua-linqinspect-edge',
      status: ok ? 'ready' : 'degraded',
      reasonCode: outbox.backpressure
        ? 'OUTBOX_BACKPRESSURE'
        : (heartbeatFailures >= 3 ? 'UA_HEARTBEAT_FAILURES' : ''),
      uptimeSec,
      heartbeatFailures,
      queue: snapshot.queue,
      deadLetters: snapshot.deadLetters.total,
      outbox,
      at: timestamp(),
    };
  } catch (_) {
    msg.statusCode = 503;
    msg.payload = {
      ok: false,
      service: 'ua-linqinspect-edge',
      status: 'unavailable',
      errorCode: 'SQLITE_UNAVAILABLE',
      at: timestamp(),
    };
  }
  msg.headers = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' };
  return msg;
}

function status(msg, flow, store) {
  const snapshot = store.status();
  const startedAt = flow.get('edgeStartedAt') || Date.now();
  if (!flow.get('edgeStartedAt')) flow.set('edgeStartedAt', startedAt);
  const lastDead = snapshot.deadLetters.last;
  msg.payload = {
    ok: true,
    service: 'ua-linqinspect-edge',
    uptimeSec: Math.max(0, Math.round((Date.now() - startedAt) / 1000)),
    mesMode: flow.get('mesMode') || 'ok',
    deliveryAuthority: deliveryAuthority(),
    senderAuthority: flow.get('senderAuthority') || (senderGuard ? senderGuard.status() : null),
    heartbeatFailures: flow.get('hbFails') || 0,
    outbox: outboxSnapshot(flow),
    queue: snapshot.queue,
    deadLetters: {
      total: snapshot.deadLetters.total,
      last: lastDead ? {
        id: lastDead.id || '',
        type: lastDead.type || '',
        status: lastDead.status || '',
        correlationId: lastDead.correlationId || '',
        attempts: lastDead.attempts || 0,
        lastStatus: lastDead.lastStatus || '',
        errorCode: lastDead.errorCode || '',
        failedAt: lastDead.failedAt || '',
      } : null,
    },
    lastRequest: flow.get('lastRequest') || null,
    lastRequestByType: flow.get('lastRequestByType') || {},
    lastResponse: flow.get('lastResponse') || null,
    lastEnqueueRejection: flow.get('lastEnqueueRejection') || null,
    dedup: snapshot.dedup,
    uaWriteback: snapshot.uaWriteback,
    storage: snapshot.storage,
  };
  msg.headers = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' };
  return msg;
}

function replayDeadLetters(msg, flow, store) {
  let body = msg.payload;
  if (typeof body === 'string') {
    try {
      body = JSON.parse(body);
    } catch (_) {
      body = {};
    }
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) body = {};
  const id = typeof body.id === 'string' ? body.id.trim() : '';
  const all = body.all === true;
  if (!id && !all) {
    msg.statusCode = 400;
    msg.payload = {
      ok: false,
      errorCode: 'REPLAY_TARGET_REQUIRED',
      error: 'provide {"id":"<dead-letter id>"} or {"all":true}'
    };
    return msg;
  }
  try {
    const result = store.replayDeadLetters({
      id,
      limit: body.limit,
      now: Date.now(),
    });
    const snapshot = store.status();
    msg.statusCode = 200;
    msg.payload = {
      ok: true,
      ...result,
      queue: snapshot.queue,
      deadLetters: snapshot.deadLetters.total,
      at: timestamp(),
    };
  } catch (error) {
    msg.statusCode = 500;
    msg.payload = {
      ok: false,
      errorCode: 'REPLAY_FAILED',
      error: error.message,
      at: timestamp(),
    };
  }
  msg.headers = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' };
  return msg;
}

module.exports = {
  safeRecord,
  mesFingerprint,
  mesIdempotency,
  rememberMesReceipt,
  mesNgOrder,
  mesComplete,
  validateNg,
  validateDone,
  enqueueNg,
  enqueueDone,
  deliveryAuthority,
  edgeCoreIngressNg,
  edgeCoreIngressDone,
  pump,
  pumpOutputs,
  response,
  uaWriteMessages,
  uaWriteSuccess,
  uaWriteFailure,
  status,
  metrics,
  health,
  outboxSnapshot,
  recordOutboxStatus,
  replayDeadLetters,
};
