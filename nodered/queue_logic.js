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

function validateNg(msg, node, store) {
  const serialNumber = String(msg.serialNumber || '').trim();
  if (!serialNumber) {
    node.error('[契约校验失败] NG 事件缺少 serialNumber，已丢弃');
    return null;
  }
  const occurredAt = String(msg.occurredAt || '');
  if (store.hasDedup('ng_fp', `${serialNumber}|${occurredAt}`) || store.hasDedup('ng_sn', serialNumber)) {
    node.warn('[重复NG丢弃] serialNumber=' + serialNumber + ' occurredAt=' + occurredAt);
    return null;
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
    return null;
  }
  return msg;
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
      node.warn('[重复入队丢弃][' + correlationId + '] ' + id);
      return null;
    }
    for (const dropped of result.dropped) {
      node.warn('[缓冲溢出] 上限 100，丢弃最旧 ' + dropped);
    }
    node.status({ fill: 'yellow', shape: 'dot', text: '队列=' + result.queueLength });
    node.warn('[入队][' + correlationId + '] NG ' + serialNumber + ' 队列=' + result.queueLength);
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
      node.warn('[重复入队丢弃][' + correlationId + '] ' + id);
      return null;
    }
    for (const dropped of result.dropped) {
      node.warn('[缓冲溢出] 上限 100，丢弃最旧 ' + dropped);
    }
    node.status({ fill: 'yellow', shape: 'dot', text: '队列=' + result.queueLength });
    node.warn('[入队][' + correlationId + '] 报工 ' + workOrderId + ' 队列=' + result.queueLength);
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
  msg.headers = Object.assign({}, msg.headers, {
    'Content-Type': 'application/json',
    'X-Correlation-Id': item.correlationId || '',
  });
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

    let completed;
    try {
      completed = store.complete({
        id: item.id,
        correlationId: item.correlationId || '',
      });
    } catch (error) {
      node.error('[SQLite完成事务失败] ' + error.message, item.body);
      return null;
    }
    if (!completed) {
      node.warn('[响应忽略] 任务已被其他响应完成：' + item.id);
      return null;
    }
    flow.set('lastResponse', {
      correlationId,
      type: item.type,
      status: 'SUCCESS',
      statusCode,
      receiptNo: item.type === 'ng' ? body.mesOrderNo : body.receiptNo,
      at: timestamp(),
    });
    if (item.type === 'ng') {
      const orderNo = body.mesOrderNo;
      node.warn('[回执][' + correlationId + '] MES工单号=' + orderNo + ' → 写回 LastNgMesOrderNo / LastMesOrderNo');
      node.status({ fill: 'green', shape: 'dot', text: '开单成功 ' + orderNo });
      return [
        { topic: 'ns=2;s=QCStation.LastNgMesOrderNo', action: 'write', payload: orderNo, datatype: 'String', _uaKind: 'write' },
        { topic: 'ns=2;s=QCStation.LastMesOrderNo', action: 'write', payload: orderNo, datatype: 'String', _uaKind: 'write' },
      ];
    }
    const receiptNo = body.receiptNo;
    node.warn('[报工回执][' + correlationId + '] receiptNo=' + receiptNo + ' → 写回 LastCompletionReceiptNo / LastMesOrderNo');
    node.status({ fill: 'green', shape: 'dot', text: '报工成功 ' + receiptNo });
    return [
      { topic: 'ns=2;s=QCStation.LastCompletionReceiptNo', action: 'write', payload: receiptNo, datatype: 'String', _uaKind: 'write' },
      { topic: 'ns=2;s=QCStation.LastMesOrderNo', action: 'write', payload: receiptNo, datatype: 'String', _uaKind: 'write' },
    ];
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
    dedup: snapshot.dedup,
  };
  msg.headers = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' };
  return msg;
}

module.exports = {
  validateNg,
  validateDone,
  enqueueNg,
  enqueueDone,
  pump,
  response,
  status,
};
