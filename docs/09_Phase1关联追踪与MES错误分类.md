# Phase 1：关联追踪与 MES 错误分类

> 日期：2026-11-20 ｜ 对应契约：[M1 v1.1](M1_业务口径与MES契约.md) ｜ 回归证据：`.tmp/regression-20261120-131213/summary.json`

## 1. 本阶段解决的问题

Phase 0 已经证明链路能跑通，但异常发生时还缺三样东西：

1. 一条检测/完工记录从 UA 事件到 MES 回执没有统一标识，日志无法稳定串联。
2. MES 响应只要 HTTP 2xx 就被当作成功，非法响应可能污染 UA 回执变量。
3. 4xx、429、5xx、超时都走同一条重试路径，无法区分“立刻人工处理”和“可以退避重试”。

Phase 1 把这三件事补齐：**事件带关联 ID、请求和回执校验关联 ID、失败按稳定错误码分类**。

## 2. 关联追踪链路

```text
UA InspectionFinishedEvent(corr-X) ─┐
UA NgDetectedEvent(corr-X) ──────────┼─> Node-RED 入队
                                     │      body.correlationId = corr-X
                                     │      X-Correlation-Id  = corr-X
                                     └────> MES 校验后回显 corr-X
                                            └─> Node-RED 校验匹配后写 UA

UA WorkOrderCompletedEvent(corr-Y) ─> Node-RED 入队 -> MES -> 回执 corr-Y -> UA
```

- 一次 NG 检测的两个事件共用同一个 `corr-<uuid4>`；完工事件单独生成。
- Node-RED 的 `/qc/status` 增加 `lastRequest`、`lastRequestByType`、`lastResponse`，可直接查看当前关联 ID。
- 死信记录保留 `correlationId`、`attempts`、`lastStatus`、`errorCode` 和 `failedAt`，便于人工补单。
- MES 缺少关联 ID 或与请求不一致时返回 `400 CORRELATION_MISMATCH`，不会误写回执。

## 3. MES 契约与失败分类

| 输入 | Node-RED 行为 | 结果/错误码 |
|---|---|---|
| 合法 2xx | 校验 schema、关联 ID、accepted、业务号，然后出队并写回 UA | `SUCCESS` |
| 非法 2xx（schema/关联 ID/accepted/业务号不合法） | 立即死信，不重试 | `INVALID_RESPONSE_SCHEMA`、`CORRELATION_MISMATCH`、`MES_REJECTED`、`MISSING_MES_ORDER_NO`、`MISSING_RECEIPT_NO` |
| 4xx（429 除外） | 业务终态失败，立即死信，不重试 | 优先使用响应体 `errorCode`，缺省 `MES_HTTP_4XX` |
| 429 | 优先按 `Retry-After` 退避，最长 30s；总共 4 次尝试 | `MES_BUSY` |
| 5xx / 超时 / 传输异常 | 按 1s/2s/4s 退避，总共 4 次尝试 | `MES_UNAVAILABLE`、`TRANSPORT_ERROR` 等 |
| 在途超过 15s | 看门狗恢复为 PENDING；超过上限进入死信 | `WATCHDOG_TIMEOUT` |

关键原则：**只有响应完全匹配当前队列项，才允许写回 UA**；4xx 和非法 2xx 不进入无意义重试。

## 4. 自动化验证

2026-11-20 完整隔离回归：

| 测试 | 结果 | 新增覆盖 |
|---|---:|---|
| 安全回归 | 6/6 PASS | 无回归 |
| UA 冒烟 | 36/36 PASS | 三类事件均携带 `CorrelationId` |
| Node-RED E2E | 15/15 PASS | UA 事件关联 ID 传到 MES，回执关联 ID 回显，请求 schemaVersion=1.0 |
| 故障场景 | 52/52 PASS | 5xx、timeout、4xx、429、非法 2xx、Method 拒绝 |
| 重启恢复 | 15/15 PASS | 非空队列 context 恢复、回执续写、无新增死信 |
| **合计** | **124/124 PASS** | 一键隔离回归 |

故障矩阵新增断言：

- 4xx：不重试，死信 `MES_REJECTED`，不误写 NG 回执。
- 429：4 次尝试后死信 `MES_BUSY`，退避窗口符合 `Retry-After: 1s` 加 1s 泵轮询量化。
- 非法 2xx：不重试，死信 `INVALID_RESPONSE_SCHEMA`，不误写 NG 回执。
- 每类故障恢复为正常模式后，完工报工仍可用，队列最终清空。
- Node-RED 停止前保留 3 条非空队列；同 userDir 重启后队列恢复并排空，NG 与完工回执均续写，死信数保持不变。

## 5. Phase 1 仍未完成

以下属于可靠性验证，不与本阶段的关联/错误分类混在一起：

1. 短时断网，验证 UA 原会话恢复和订阅续推。
2. 长时断网/Server 重启，验证订阅重建和 60 秒陈旧事件过滤。
3. 把上述场景纳入 `scripts/run_regression.py`，避免依赖人工观察。

完成这些场景后再进入 Phase 2：SQLite 事务队列、单一状态写入者、可观测性与部署基线。

## 6. 后续规划

### Phase 2：可靠性与可运维性

- 用 SQLite 事务表替换 flow context 队列，增加唯一约束和状态迁移审计。
- 所有队列状态只允许一个写入者，避免 Node-RED 多实例并发写。
- `/qc/status` 增加积压时长、成功率、重试分布和最近错误聚合。
- 提供 systemd/docker compose 启动基线、日志轮转和健康检查。
- 增加 MES 契约测试和数据库崩溃恢复测试。

### Phase 3：真实集成与试点

- 对接真实 MES 沙箱，确认错误码、幂等键和回执格式。
- 接入真实视觉模型，增加模型版本、图片留存和人工复核。
- 企业 PKI、角色最小权限、证书轮换、网络隔离。
- 现场验收：断网恢复、重启恢复、重复投递、长时间压测和人工补单流程。

### 答辩表述

建议说：

> Phase 1 已把关联 ID、MES 失败分类和非空队列重启恢复跑通，124 项隔离回归全部通过；下一步不是继续堆功能，而是证明断网后订阅和队列仍可恢复，再把非事务型 context 队列升级为 SQLite 事务队列。

不要宣称已经生产级、exactly-once 或完成真实 MES 对接。
