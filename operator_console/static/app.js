'use strict';

const state = { busy: false, lastStatus: null, currentEvidence: null, evidenceName: "" };
const byId = id => document.getElementById(id);
const alertBox = byId('alert');

function showAlert(message) {
  alertBox.textContent = message;
  alertBox.classList.remove('hidden');
}

function clearAlert() {
  alertBox.textContent = '';
  alertBox.classList.add('hidden');
}

function formatValue(value, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

async function request(path, options = {}) {
  const headers = { accept: 'application/json', ...(options.headers || {}) };
  if (options.method && options.method !== 'GET') headers['X-Operator-Request'] = '1';
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : {};
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function setPill(node, text, tone = 'neutral') {
  node.textContent = text;
  node.className = `status-pill ${tone}`;
}

function renderStation(station) {
  if (!station || !station.available) {
    setPill(byId('device-status'), 'UA 不可用', 'ng');
    byId('action-message').textContent = station?.error || '无法读取工位状态';
    return;
  }
  const data = station.data;
  const status = Number(data.deviceStatus);
  setPill(byId('device-status'), data.deviceStatusText || `状态 ${status}`, status === 0 ? 'ok' : status === 1 ? 'warn' : 'ng');
  byId('current-work-order').textContent = formatValue(data.currentWorkOrderId);
  byId('current-sn').textContent = formatValue(data.currentPartSN);
  const hasResult = Boolean(data.lastImageId);
  byId('last-result').textContent = hasResult ? (data.lastResult === true ? 'OK' : 'NG') : '—';
  byId('last-defect').textContent = formatValue(data.lastDefectType);
  byId('last-image').textContent = formatValue(data.lastImageId);
  byId('last-timestamp').textContent = formatTime(data.lastTimestamp);
  byId('inspect-count').textContent = formatValue(data.InspectCount, '0');
  byId('ok-count').textContent = formatValue(data.OkCount, '0');
  byId('ng-count').textContent = formatValue(data.NgCount, '0');
  byId('ng-rate').textContent = data.NgRate === undefined || data.NgRate === null ? '—' : Number(data.NgRate).toFixed(3);
  byId('last-mes-order').textContent = formatValue(data.lastMesOrderNo);
  byId('last-ng-receipt').textContent = formatValue(data.lastNgMesOrderNo);
  byId('last-completion-receipt').textContent = formatValue(data.lastCompletionReceiptNo);
  byId('last-confidence').textContent = hasResult && data.LastConfidence !== undefined
    ? Number(data.LastConfidence).toFixed(4)
    : '—';
  if (status === 1 && !byId('work-order-input').value && data.currentWorkOrderId) {
    byId('work-order-input').value = data.currentWorkOrderId;
  }
}

function renderEdge(edge) {
  if (!edge || !edge.available) {
    setPill(byId('edge-status'), 'Edge Core 不可用', 'ng');
    byId('queue-total').textContent = '—';
    byId('dead-total').textContent = '—';
    return;
  }
  const data = edge.data;
  setPill(byId('edge-status'), 'Edge Core 正常', 'ok');
  byId('queue-total').textContent = formatValue(data.queue?.total, '0');
  byId('dead-total').textContent = formatValue(data.deadLetters?.total, '0');
}

function appendCell(row, text) {
  const cell = document.createElement('td');
  cell.textContent = formatValue(text);
  row.appendChild(cell);
}

function renderDeliveries(payload) {
  const body = byId('delivery-rows');
  body.replaceChildren();
  const items = payload?.data?.items;
  if (!Array.isArray(items) || items.length === 0) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 6;
    cell.className = 'empty';
    cell.textContent = '暂无投递任务';
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of items.slice(0, 30)) {
    const row = document.createElement('tr');
    appendCell(row, item.eventType === 'COMPLETION_REPORT' ? '完工' : 'NG');
    appendCell(row, item.workOrderId);
    appendCell(row, item.state);
    appendCell(row, item.mesReceipt?.receiptNo || item.mesReceipt?.mesOrderNo || '');
    appendCell(row, formatTime(item.updatedAt));
    const evidenceCell = document.createElement('td');
    const evidenceButton = document.createElement('button');
    evidenceButton.type = 'button';
    evidenceButton.className = 'button small secondary';
    evidenceButton.textContent = '查看';
    evidenceButton.addEventListener('click', () => showEvidence(item.deliveryId, item.workOrderId));
    evidenceCell.appendChild(evidenceButton);
    row.appendChild(evidenceCell);
    body.appendChild(row);
  }
}

function deadActionButton(label, className, handler) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `button small ${className}`;
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
}

async function deadAction(id, action) {
  const actor = window.prompt('操作人', 'operator');
  if (!actor) return;
  const promptText = action === 'replay' ? '重放原因' : '处置结论';
  const reason = window.prompt(promptText);
  if (!reason) return;
  try {
    await request(`/api/dead-letters/${encodeURIComponent(id)}/${action}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ actor, reason, resolution: reason }),
    });
    byId('action-message').textContent = action === 'replay' ? '死信已重放。' : '死信已确认处置。';
    await refresh();
  } catch (error) {
    showAlert(error.message);
  }
}

function renderDeadLetters(payload) {
  const list = byId('dead-letter-list');
  list.replaceChildren();
  const items = payload?.data?.items;
  if (!Array.isArray(items) || items.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '暂无死信';
    list.appendChild(empty);
    return;
  }
  for (const item of items.slice(0, 30)) {
    const article = document.createElement('article');
    article.className = 'dead-item';
    const head = document.createElement('div');
    head.className = 'dead-head';
    const title = document.createElement('strong');
    title.textContent = `${item.eventType === 'COMPLETION_REPORT' ? '完工' : 'NG'} · ${item.workOrderId || '无工单'}`;
    const code = document.createElement('span');
    code.className = 'status-pill ng';
    code.textContent = item.reasonCode || item.lastErrorCode || 'DEAD';
    head.append(title, code);
    const meta = document.createElement('div');
    meta.className = 'dead-meta';
    meta.textContent = `关联ID=${item.correlationId || '—'} · 失败=${formatTime(item.failedAt)} · 原因=${item.reason || item.lastErrorMessage || '—'}`;
    const actions = document.createElement('div');
    actions.className = 'dead-actions';
    actions.append(
      deadActionButton('重放', 'primary', () => deadAction(item.deliveryId, 'replay')),
      deadActionButton('确认处置', 'secondary', () => deadAction(item.deliveryId, 'acknowledge')),
    );
    article.append(head, meta, actions);
    list.appendChild(article);
  }
}

async function showEvidence(deliveryId, workOrderId) {
  try {
    const payload = await request(`/api/evidence/${encodeURIComponent(deliveryId)}`);
    const evidence = payload.data;
    state.currentEvidence = evidence;
    state.evidenceName = `qc-evidence-${workOrderId || deliveryId}.json`;
    const inspection = evidence.inspection || {};
    const lineage = evidence.visionLineage || {};
    const mes = evidence.mes || {};
    const lines = [
      `工单：${formatValue(inspection.workOrderId)}`,
      `SN：${formatValue(inspection.serialNumber)}`,
      `结果：${formatValue(inspection.result)} · 缺陷：${formatValue(inspection.defectType)} · 置信度：${inspection.confidence ?? '—'}`,
      `图片：${formatValue(inspection.imageId)}`,
      `模型：${formatValue(lineage.modelVersion)} · 模型哈希：${formatValue(lineage.modelSha256)}`,
      `MES 回执：${formatValue(mes.receipt?.receiptNo)} · UA 写回：${evidence.ua?.writeConfirmed ? '已确认' : '未确认'}`,
      `correlationId：${formatValue(evidence.correlationId)}`,
      `bundleSha256：${formatValue(evidence.bundleSha256)}`,
      `完整性：${evidence.completeness?.complete ? '字段完整' : '缺少 ' + (evidence.completeness?.missingFields || []).join(', ')}`,
    ];
    byId('evidence-summary').textContent = lines.join('\n');
    byId('evidence-json').textContent = JSON.stringify(evidence, null, 2);
    byId('evidence-panel').classList.remove('hidden');
    byId('evidence-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) {
    showAlert(error.message);
  }
}

function downloadEvidence() {
  if (!state.currentEvidence) return;
  const blob = new Blob([JSON.stringify(state.currentEvidence, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = state.evidenceName || 'qc-evidence.json';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    const results = await Promise.allSettled([
      request('/api/status'),
      request('/api/deliveries?limit=30'),
      request('/api/dead-letters?limit=30'),
    ]);
    const status = results[0].status === 'fulfilled' ? results[0].value : null;
    const deliveries = results[1].status === 'fulfilled' ? results[1].value : null;
    const dead = results[2].status === 'fulfilled' ? results[2].value : null;
    if (status) {
      state.lastStatus = status;
      renderStation(status.station);
      renderEdge(status.edge);
      byId('generated-at').textContent = `更新于 ${formatTime(status.generatedAt)}`;
      setPill(byId('refresh-state'), '已连接', 'ok');
      clearAlert();
    } else {
      setPill(byId('refresh-state'), '连接失败', 'ng');
      showAlert(results[0].reason?.message || '无法连接操作台后端');
    }
    if (deliveries) renderDeliveries(deliveries);
    if (dead) renderDeadLetters(dead);
  } finally {
    state.busy = false;
  }
}

async function submitStart(event) {
  event.preventDefault();
  const workOrderId = byId('work-order-input').value.trim();
  const targetCount = Number(byId('target-input').value);
  if (!workOrderId) return;
  byId('start-button').disabled = true;
  try {
    const result = await request('/api/work-orders/start', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ workOrderId, targetCount }),
    });
    byId('action-message').textContent = `开检：${result.message || '已接受'}`;
    await refresh();
  } catch (error) {
    showAlert(error.message);
  } finally {
    byId('start-button').disabled = false;
  }
}

async function completeWorkOrder() {
  const workOrderId = byId('work-order-input').value.trim() || state.lastStatus?.station?.data?.currentWorkOrderId || '';
  if (!workOrderId) {
    showAlert('请输入或读取当前工单号。');
    return;
  }
  if (!window.confirm(`确认完成工单 ${workOrderId}？`)) return;
  byId('complete-button').disabled = true;
  try {
    const result = await request('/api/work-orders/complete', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ workOrderId }),
    });
    byId('action-message').textContent = `完工：${result.message || '已接受'}`;
    await refresh();
  } catch (error) {
    showAlert(error.message);
  } finally {
    byId('complete-button').disabled = false;
  }
}

byId('start-form').addEventListener('submit', submitStart);
byId('complete-button').addEventListener('click', completeWorkOrder);
byId('refresh-button').addEventListener('click', refresh);
byId('download-report').addEventListener('click', () => window.location.assign('/api/report'));
byId('download-evidence').addEventListener('click', downloadEvidence);
byId('close-evidence').addEventListener('click', () => byId('evidence-panel').classList.add('hidden'));
refresh();
window.setInterval(refresh, 3000);
