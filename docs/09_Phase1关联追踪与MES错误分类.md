# Phase 1：关联追踪与 MES 错误分类

> 日期：2026-11-20 ｜ 对应契约：[M1 v1.1](M1_业务口径与MES契约.md) ｜ 回归证据：`.tmp/regression-20261120-131213/summary.json`

> 阶段说明：本文的 158/158 是 Phase 1 当时的证据。Phase 2A/2B/2C 完成后，
> 当前一键回归为 592/592，见 [15_Phase2C部署运维与恢复演练.md](15_Phase2C部署运维与恢复演练.md)。

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

2026-11-21 当前隔离回归：

| 测试 | 结果 | 新增覆盖 |
|---|---:|---|
| Edge Core API/状态机 | 12/12 PASS | 严格输入约束、NG/完工专属回执、稳定错误码、幂等冲突、4xx/5xx、死信重放、重启后去重、API 查询 |
| MES 接收端幂等 | 10/10 PASS | 稳定 payload 指纹、重复返回原回执、同键不同内容 409、旧回执迁移、原型污染防护 |
| 安全回归 | 16/16 PASS | UA 权限、业务变量只读、Node-RED HTTP 认证与运维接口鉴权 |
| UA 冒烟 | 36/36 PASS | 三类事件均携带 `CorrelationId` |
| Node-RED E2E | 21/21 PASS | UA 事件关联 ID 传到 MES，回执关联 ID 回显，请求 schemaVersion=1.0，重复/冲突 HTTP 契约 |
| 故障场景 | 52/52 PASS | 5xx、timeout、4xx、429、非法 2xx、Method 拒绝 |
| 重启恢复 | 18/18 PASS | 非空队列 context 恢复、回执续写、无新增死信 |
| 短断网 | 10/10 PASS | 5 秒停顿后继续开检、NG 与完工闭环 |
| 陈旧事件 | 7/7 PASS | 超过 60 秒事件不入队、不改去重、不写回执、不请求 MES |
| UA Server 重启 | 14/14 PASS | StartTime 变化触发显式重连，旧 Session 的事件订阅重建 |
| **合计** | **158/158 PASS** | 一键隔离回归 |

故障矩阵新增断言：

- 4xx：不重试，死信 `MES_REJECTED`，不误写 NG 回执。
- 429：4 次尝试后死信 `MES_BUSY`，退避窗口符合 `Retry-After: 1s` 加 1s 泵轮询量化。
- 非法 2xx：不重试，死信 `INVALID_RESPONSE_SCHEMA`，不误写 NG 回执。
- 每类故障恢复为正常模式后，完工报工仍可用，队列最终清空。
- Node-RED 停止前明确保留新工单的 NG 与完工队列项；同 userDir 重启后队列恢复并排空，NG 与完工回执均续写，死信数保持不变。
- UA Server 重启后，心跳读取的 `ServerStatus.StartTime`（`ns=0;i=2257`）变化会广播 `action=reconnect`，三个 UA Client 重建会话并重放事件订阅。

## 5. Phase 1 断网恢复已完成，剩余证据工作

本轮已经纳入一键隔离回归：

1. ✅ 短时停顿后继续开单/报工。
2. ✅ UA Server 重启后显式重建事件订阅。
3. ✅ 超过 60 秒的陈旧事件不进入队列和回执链路。

仍需补充：Wireshark 的 `CreateMonitoredItems`/`Publish` 证据、真实交换机断链/防火墙切换、长时断网恢复耗时。Phase 2A 已完成 SQLite 事务队列与崩溃恢复；模拟 MES 已实现稳定指纹、重复回执与冲突拒绝，下一阶段重点是真实 MES 沙箱验证同一幂等合同。

## 6. 后续规划

### Phase 2：可靠性与可运维性

- ✅ 用 SQLite 事务表替换 flow context 队列，增加唯一约束、原子领取和崩溃恢复。
- 所有队列状态只允许一个写入者，避免 Node-RED 多实例并发写。
- `/qc/status` 增加积压时长、成功率、重试分布和最近错误聚合。
- 提供 systemd/docker compose 启动基线、日志轮转和健康检查。
- ✅ 增加 MES 契约测试和数据库崩溃恢复测试。

### Phase 3：真实集成与试点

- 对接真实 MES 沙箱，确认错误码、幂等键和回执格式。
- 接入真实视觉模型，增加模型版本、图片留存和人工复核。
- 企业 PKI、角色最小权限、证书轮换、网络隔离。
- 现场验收：断网恢复、重启恢复、重复投递、长时间压测和人工补单流程。

### 答辩表述

建议说：

> Phase 1 已把关联 ID、MES 失败分类、非空队列重启恢复、短断网、陈旧过滤和 UA Server 重启后的订阅重建跑通；Phase 2A 完成 SQLite 事务队列、UA 写回确认、MES 成功后写回前 `SIGKILL` 恢复，Phase 2B 完成 Prometheus 指标与在线备份，Phase 2C 完成 systemd/告警模板与离线恢复演练，M3 自动化补上 MES 长断网、死信重放和周期 soak，事件 outbox 补上稳定事件 ID、ACK、重启重发、容量背压、受控处置和队列满时失败关闭，模拟 MES 补上稳定 payload 指纹、重复回执和冲突拒绝，存储故障测试补上写满回滚、损坏库失败关闭与隔离恢复，备份合同补上空库/错 schema 拒绝及只读源/输出/恢复目录演练，状态源补上 SQLite 状态+Outbox 同事务、保存失败回滚内存与 UA 重同步、JSON 兼容路径失败保护，并通过受限 tmpfs 真实挂载验证 EROFS/ENOSPC，当前 592 项隔离回归全部通过。下一步补真实网络切换与抓包证据，并在真实 MES 沙箱验证同一幂等合同和端到端 RTO。

不要宣称已经生产级、exactly-once 或完成真实 MES 对接。
