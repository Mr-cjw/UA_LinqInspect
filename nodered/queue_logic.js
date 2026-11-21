'use strict';

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

function ngKeys(serialNumber, occurredAt) {
  return [
    { kind: 'ng_fp', key: `${serialNumber}|${occurredAt}` },
    { kind: 'ng_sn', key: serialNumber },
  ];
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
  return (targets || []).map(entry => {
    const topic = entry.topic || entry.target || '';
    const value = entry.value == null ? entry.expectedValue : entry.value;
    return {
      topic,
      action: 'write',
      payload: String(value),
      datatype: 'String',
      _uaKind: 'write',
      _qc: {
        id: item.id,
        type: item.type,
        correlationId: item.correlationId || '',
        uaTarget: topic,
      },
    };
  });
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
  validateNg,
  validateDone,
  enqueueNg,
  enqueueDone,
  pump,
  pumpOutputs,
  response,
  uaWriteSuccess,
  uaWriteFailure,
  status,
  metrics,
  health,
  outboxSnapshot,
  recordOutboxStatus,
  replayDeadLetters,
};
