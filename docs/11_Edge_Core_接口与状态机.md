# Edge Core Service：接口、状态机与持久化契约

> 版本：v0.1 ｜ 日期：2026-11-20 ｜ API：[edge-core.openapi.yaml](api/edge-core.openapi.yaml)
>
> 状态：产品与工程契约草案。本文先固定边界，后续实现必须通过故障矩阵验证，不能仅以接口可调用为完成标准。

## 1. 设计目标

Edge Core Service 是质检-MES 可靠连接器的业务核心，负责：

- 接收并校验标准化质检结果；
- 在 SQLite 中原子化完成去重、入队和状态记录；
- 驱动 MES 投递、回执校验和 UA 写回；
- 支持断网、超时、重复消息和重启恢复；
- 提供死信查询、确认和人工重放；
- 输出可对账的关联 ID、错误码和轻量指标。

Node-RED 保留为可视化编排、设备适配和操作界面。它不得成为权威业务状态、事务队列或回执状态的唯一存储。

## 2. 权威边界

| 域 | 权威来源 | 说明 |
|---|---|---|
| 工单、检测统计、设备状态 | OPC UA Server | 通过受控 Method 变更 |
| 投递、重试、回执和死信状态 | Edge Core SQLite | 单一本机写入者 |
| MES 实际受理结果 | MES | 通过幂等键和回执确认 |
| 可视化编排 | Node-RED | 不持有权威 Outbox 状态 |

跨域一致性原则：**at-least-once + 业务幂等 + 回执对账**。不承诺 exactly-once。

## 3. 输入契约

来源可以是人工、第三方视觉或自研 YOLO，但必须统一到标准事件：

```json
{
  "schemaVersion": "1.0",
  "sourceEventId": "station-01-20261120-000001",
  "stationId": "STATION-01",
  "occurredAt": "2026-11-20T12:34:56.789Z",
  "result": "NG",
  "serialNumber": "SN-20261120-000001",
  "workOrderId": "WO-20261120-001",
  "defectType": "SCRATCH",
  "confidence": 0.93,
  "imageId": "IMG-20261120-000001",
  "correlationId": "corr-7d35b5d0-6811-4d66-b7db-42242543b9b1",
  "metadata": {}
}
```

### 3.1 幂等键

建议业务幂等键：

```text
inspection:<stationId>:<sourceEventId>
```

如果来源无法提供稳定 `sourceEventId`：

- 网关可以生成唯一 ID；
- 必须在来源接入文档中写明“来源重发可能造成重复业务效果”；
- 不能对外承诺该类来源具备完整幂等能力。

### 3.2 冲突规则

- 同幂等键、同载荷：返回已有任务，标记 `Idempotent-Replay: true`；
- 同幂等键、不同载荷：返回 `409 PAYLOAD_CONFLICT`，进入可审计冲突记录；
- 幂等键已存在但任务已 `DONE`：返回已完成结果；
- 幂等键已存在但任务已 `DEAD`：返回死信状态，不自动重置。

## 4. 投递状态机

```text
收到新事件
   │
   ▼
PENDING ──claimed──> SENDING ──valid receipt──> ACKED
   ▲                    │                         │
   │                    ├─retryable failure───────┤
   │                    │                         ▼
   └──── RETRY_WAIT <───┘                  UA_WRITE_PENDING
                                                   │
                                      ┌────────────┴────────────┐
                                      │ UA write success        │ UA write failure
                                      ▼                         ▼
                                    DONE                ACKED + UA retry

不可重试失败 / 重试耗尽 / 非法回执
   └──────────────────────────────> DEAD
```

### 4.1 状态定义

| 状态 | 含义 | 允许下一步 |
|---|---|---|
| `PENDING` | 已事务化入队，等待发送 | `SENDING` |
| `SENDING` | 已取得发送租约，请求在途 | `ACKED`、`RETRY_WAIT`、`DEAD` |
| `RETRY_WAIT` | 可重试失败，等待退避 | `SENDING`、`DEAD` |
| `ACKED` | 已收到合法 MES 回执 | `UA_WRITE_PENDING`、`DONE` |
| `UA_WRITE_PENDING` | 回执有效，等待写回 UA | `DONE`、`ACKED`（写失败重试） |
| `DONE` | MES 回执和 UA 写回都已完成 | 终态 |
| `DEAD` | 需要人工处置 | 人工重放后可回到 `PENDING` |
| `DISCARDED` | 经授权明确废弃，必须记录原因 | 终态 |

### 4.2 关键不变量

1. MES 回执验证通过前不得写 UA 回执变量。
2. UA 写回成功前不得将任务标记为 `DONE`。
3. 发送中的任务必须有租约；租约过期后才能被恢复线程重新取得。
4. 同一幂等键最多存在一个业务任务。
5. 死信重放必须复用原幂等键和原业务标识。
6. `DEAD`、`DONE` 和 `DISCARDED` 都不可被普通重试路径隐式改写。

## 5. MES 响应契约

最小成功响应：

```json
{
  "accepted": true,
  "schemaVersion": "1.0",
  "correlationId": "corr-7d35b5d0-6811-4d66-b7db-42242543b9b1",
  "receiptNo": "MES-20261120-0001"
}
```

校验条件：

- HTTP 2xx；
- `accepted === true`；
- `schemaVersion` 匹配；
- `correlationId` 与请求一致；
- 回执号非空且格式合法；
- 重复请求返回相同回执时允许 `duplicated: true`。

失败分类：

| 类别 | 处理 |
|---|---|
| 400/401/403/404/409 等业务终态 | `DEAD`，不自动重试 |
| 429 | 读取 `Retry-After`，进入 `RETRY_WAIT` |
| 5xx | 有界退避重试 |
| timeout / 传输异常 | 有界退避重试，复用同一幂等键 |
| 非法 2xx | `DEAD`，错误码 `INVALID_RESPONSE_SCHEMA` |
| 同键不同回执 | `DEAD`，错误码 `RECEIPT_CONFLICT` |

如果客户 MES 不支持幂等键，合同必须明确：连接器只能降低重复概率，不能保证 MES 不产生重复业务单。

## 6. SQLite 持久化建议

### 6.1 初始化

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

### 6.2 核心表

```sql
CREATE TABLE deliveries (
    delivery_id       TEXT PRIMARY KEY,
    business_key      TEXT NOT NULL UNIQUE,
    source_event_id   TEXT NOT NULL,
    station_id        TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    correlation_id    TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   TEXT,
    lease_until       TEXT,
    receipt_json      TEXT,
    last_error_code   TEXT,
    last_error        TEXT,
    ua_write_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE dead_letters (
    delivery_id       TEXT PRIMARY KEY REFERENCES deliveries(delivery_id),
    reason_code       TEXT NOT NULL,
    reason            TEXT NOT NULL,
    failed_at         TEXT NOT NULL,
    replayed_at       TEXT,
    acknowledged_at   TEXT,
    acknowledged_by   TEXT,
    resolution        TEXT
);

CREATE TABLE delivery_events (
    sequence          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id       TEXT NOT NULL REFERENCES deliveries(delivery_id),
    from_state        TEXT,
    to_state          TEXT NOT NULL,
    correlation_id    TEXT NOT NULL,
    detail_json       TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE metrics_counters (
    name              TEXT PRIMARY KEY,
    value             INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);
```

### 6.3 事务规则

- 入队事务同时写入 `deliveries`、初始 `delivery_events` 和指标；
- 去重由 `business_key` 唯一约束保证；
- 发送状态和租约取得在 `BEGIN IMMEDIATE` 中完成；
- 回执、UA 写回状态和状态迁移审计在同一逻辑事务内更新；
- 队列满时进入背压或死信，不静默删除最旧任务；
- 永久数据不得只存在于 Node-RED flow context。

## 7. 轻量观测

`GET /v1/status` 至少提供：

- 队列总数；
- `PENDING`、`SENDING`、`RETRY_WAIT` 数量；
- 最老任务年龄；
- 死信总数和最老死信年龄；
- MES 成功/失败计数和最近时间；
- UA 写回成功/失败计数；
- 数据库状态、版本和运行时长。

健康状态不能固定返回 `ok`。数据库不可写、队列已停止推进或关键依赖持续失败时，必须返回 `degraded` 或 `failed`。

## 8. 验收矩阵

实现完成至少需要验证：

- 同事件重复提交不重复入队；
- 同键不同载荷返回冲突；
- MES 断网后恢复并完成投递；
- MES 超时后使用同一幂等键重试；
- 4xx 直接死信；
- 429 遵循 `Retry-After`；
- 非法 2xx 不写 UA；
- 发送中进程被杀后租约到期恢复；
- 回执已收到但 UA 写回前进程被杀，重启后继续写回；
- 非空队列重启后继续发送；
- 死信重放不改变原业务键；
- 每个业务效果可通过 `correlationId` 还原。

## 9. 当前非目标

- 多租户云端平台；
- 分布式消息队列；
- 跨站点高可用；
- 自研完整 MES；
- 在没有客户 MES 幂等能力时承诺零重复；
- 将视觉准确率纳入连接器验收。

## 10. 当前实现状态（2026-11-20）

已落地的垂直切片位于 `edge_core/`：

- stdlib `sqlite3` + WAL + `BEGIN IMMEDIATE`；
- `business_key` 唯一约束和 payload hash 冲突检测；
- `PENDING → SENDING → UA_WRITE_PENDING → DONE` 主链路；
- 5xx/timeout 有界重试，4xx/非法 2xx 进入死信；
- lease 过期恢复；
- 死信查询、确认和重放；
- Bearer token 保护的控制面 API；
- 轻量队列、死信、MES 和 UA 写回指标；
- `tests/edge_core_test.py` 7 项自动化测试。

**当前限制：** 在线 Node-RED 链路已使用另一套 SQLite WAL 队列（`nodered/sqlite_store.js`），Edge Core 仍作为外部控制面/接口垂直切片，两者尚未收敛。Edge Core 也尚未内置 OPC UA 回执写入器；MES 回执成功后任务停在 `UA_WRITE_PENDING`，需要外部 UA 写回适配器调用 `/v1/deliveries/{id}/ua-write-result`，只有写回成功才进入 `DONE`。
