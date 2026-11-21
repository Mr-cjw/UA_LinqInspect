# Phase 2A：SQLite 事务队列与崩溃恢复

> 日期：2026-11-20 ｜ Phase 2A 证据：`.tmp/regression-20261120-142021/summary.json` ｜ 当时结果：194/194 PASS
>
> Phase 2B/2C、M3、运维加固、UA 写回确认、写回前崩溃恢复、事件 outbox、队列容量处置、模拟 MES 幂等合同、SQLite 存储故障恢复、备份 schema 合同、只读目录演练、状态持久化失败演练与进程内回滚完成后当前合并回归为 559/559（含 Node-RED 认证、Edge Core、部署、长断网、死信重放与恢复测试），见 [15_Phase2C部署运维与恢复演练.md](15_Phase2C部署运维与恢复演练.md)。

## 1. 本阶段解决的问题

Phase 1 的队列、死信和去重集合保存在 Node-RED flow context 中。它可以通过
localfilesystem 降低重启丢失风险，但“去重写入”和“队列入队”不是同一个事务，
进程在两者之间崩溃时存在丢事件窗口。

Phase 2A 将以下状态迁移为 SQLite 单一事实源：

- 待发送队列 `queue`
- 死信 `dead_letters`
- 去重键 `dedup`
- 迁移/版本元数据 `meta`

Node-RED flow context 只保留 `mesMode`、最近请求/响应等运行态信息，不再作为队列的权威存储。

## 2. 组件边界

| 文件 | 职责 |
|---|---|
| `nodered/sqlite_store.js` | 建表、事务、唯一约束、原子领取、退避/死信状态迁移、旧 context 导入 |
| `nodered/queue_logic.js` | 入队、发送泵、响应契约校验、UA 写回编排与确认、`/qc/status` 输出 |
| `nodered/settings.js` | 通过 `functionGlobalContext` 注入 `qstore` 和 `qlogic` |
| `nodered/flows.json` | Function 节点只保留薄适配代码，不再内联队列实现 |
| `tests/sqlite_queue_test.js` | 存储层事务、唯一约束、单飞行、看门狗和迁移单测 |
| `tests/crash_recovery.py` | Node-RED `SIGKILL` 后数据库恢复与回执续传验证 |

选择 `node:sqlite` 的原因：当前 Node.js 26 已内置 `DatabaseSync`、`StatementSync`
和 `backup`，无需引入额外原生依赖，部署包更小。

## 3. 数据库设计

默认数据库：`data/qc-queue.sqlite`。可用环境变量 `QC_DB_PATH` 指定；也可用
`QC_DATA_DIR` 指定目录。

### 3.1 `queue`

关键字段：

| 字段 | 含义 |
|---|---|
| `id` | 业务唯一键：NG 为 `ng|SN|occurredAt`，报工为 `done|workOrderId` |
| `type` | `ng` / `complete` |
| `correlation_id` | 全链路关联 ID |
| `body_json` | MES 请求体快照 |
| `attempts` | 已失败次数 |
| `next_at` | 最早允许重试的时间戳（毫秒） |
| `status` | `PENDING` / `SENDING` |
| `sent_at` | 进入在途状态的时间，用于 15 秒看门狗 |

`id` 为唯一约束，队列上限 100；队列满时拒绝新任务并保留全部现有任务，UA outbox 事件不 ACK，等待队列排空后重放。

MES 成功回执不会立即删除 `queue` 行。任务保持 `SENDING`，并在独立的 UA 写回表中推进；
这样可以避免“MES 已受理，但回执变量尚未写成功时进程崩溃”造成业务状态丢失。

### 3.2 `dead_letters`

保存终态失败任务及 `error_code`、`last_status`、`failed_at`。最多保留最近 100 条，
`/qc/status` 的 `deadLetters.last` 仍维持原有字段形状。

### 3.3 `dedup`

逻辑主键为 `(kind, dedup_key)`：

- `ng_fp`：`serialNumber + occurredAt`
- `ng_sn`：`serialNumber` 兜底
- `done`：`workOrderId`

每类最多保留最近约 500 个键。

### 3.5 `ua_writebacks` 与 `ua_write_targets`

`ua_writebacks` 按 `queue_id` 保存 MES 回执值、写回尝试次数和下次重试时间；
`ua_write_targets` 保存本次需要写入的每个 OPC UA 目标和预期值。NG 需要确认
`LastNgMesOrderNo` 与兼容镜像 `LastMesOrderNo`，完工需要确认
`LastCompletionReceiptNo` 与 `LastMesOrderNo`。

Node-RED 不再逐变量直接 Write，而是调用一次 `RecordMesReceipt`。服务端完成具体回执变量和兼容镜像的写入与读回确认后，Node-RED 才在 SQLite 中将全部目标标记为已确认。

写回策略：

- `PENDING`：等待发送泵处理；
- `SENDING`：已发出写操作，等待目标确认；
- 15 秒未收齐确认按退避重试，最多 3 次后进入死信；
- 全部目标确认后才删除队列行和相关写回记录；
- 确认值与预期值不一致时记录 `UA_WRITE_VALUE_MISMATCH` 并进入失败路径。

### 3.4 运行参数

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
```

WAL 提升读写并发；`synchronous=FULL` 优先保证提交落盘；`busy_timeout` 用于短时锁等待。
数据库文件在 Linux 下尽量设置为 `0600`。

## 4. 事务边界

### 4.1 去重与入队原子提交

`enqueue` 在同一个 `BEGIN IMMEDIATE` 事务内完成：

1. 检查队列 ID 与去重键；
2. 必要时删除最旧 `PENDING` 项；
3. 写去重键；
4. 写队列项；
5. 提交。

任一步失败都会回滚，避免“去重已写入但队列丢失”。单测用 SQLite trigger 强制队列插入失败，
验证去重记录同时回滚。

### 4.2 单飞行领取

`claimNext` 在一个事务内检查 `SENDING`，超过 15 秒则执行看门狗恢复；否则只领取一条到期
`PENDING` 并原子更新为 `SENDING`。因此即使多个定时器触发，也不会出现两条在途任务。

### 4.3 响应终结与 UA 写回确认

`retry`、`deadLetter` 以及 `complete` 的第一阶段都要求：

- 队列项仍为 `SENDING`；
- `id` 和 `correlationId` 匹配当前在途任务。

重复回执或陈旧回执无法二次完成、二次退避或重复写死信。MES 响应契约通过后，
`complete` 在同一个事务内创建 `ua_writebacks` 和 `ua_write_targets`，并保留队列行；
只有 UA 写回回调逐个确认目标，且剩余目标数为 0 时，才在同一事务内删除任务并记录成功。
写回失败沿用单飞行语义，不会把下一条 MES 业务提前发送。

## 5. 旧 context 迁移

首次启动时 `bootstrap(flow)` 读取旧的：

```text
buf / dead / ngSeen / ngSnSeen / doneSeen
```

在一个事务中导入 SQLite，并写入 `meta.legacy_context_imported_v1`。迁移成功后清空旧
flow context，避免双写和重复导入。旧队列中的 `SENDING` 项会保留在途时间，由重启后的看门狗恢复。

## 6. 崩溃恢复测试

`crash_recovery_prepare` 执行：

1. 注入 MES timeout，制造长请求；
2. 触发 NG 开单和完工报工；
3. 确认 SQLite 中存在 1 条 `SENDING`，另有待发项；
4. 回归编排器对本机 Node-RED 发送 `SIGKILL`。

`crash_recovery_verify` 用同一 userDir、同一 SQLite 文件重启：

1. 确认队列总数与唯一 ID 数一致，无重复行；
2. 恢复 MES 为 ok；
3. 等待看门狗恢复并排空；
4. 验证 NG 回执和完工回执继续写回；
5. 验证死信数不增长、`done` 去重键只有 1 条。

最新实测：崩溃 seed 11/11、verify 12/12 PASS。重启瞬间数据库仍保留 4 条队列记录，
其中 1 条 `SENDING`；最终全部排空。

## 7. 回归结果

2026-11-20 一键隔离回归：

| 测试 | 结果 |
|---|---:|
| SQLite 队列单测 | 13/13 PASS |
| 安全回归 | 6/6 PASS |
| UA 冒烟 | 36/36 PASS |
| Node-RED E2E | 15/15 PASS |
| 故障场景 | 52/52 PASS |
| Node-RED 重启恢复 | 18/18 PASS |
| SIGKILL 崩溃恢复 | 23/23 PASS |
| 短断网 | 10/10 PASS |
| 陈旧事件过滤 | 7/7 PASS |
| UA Server 重启 | 14/14 PASS |
| **合计** | **194/194 PASS** |

复跑命令：

```bash
.venv/bin/python scripts/run_regression.py --keep-going
```

## 8. 运维检查

查看状态：

```bash
curl -s http://127.0.0.1:1880/qc/status | jq
```

查看数据库：

```bash
python3 - <<'PY'
import sqlite3
db = sqlite3.connect('data/qc-queue.sqlite')
print('queue', db.execute('select status, count(*) from queue group by status').fetchall())
print('dead', db.execute('select count(*) from dead_letters').fetchone()[0])
print('dedup', db.execute('select kind, count(*) from dedup group by kind').fetchall())
PY
```

在线备份应使用 SQLite backup API，而不是只复制主 `.sqlite` 文件。Phase 2B 已把下面的思路封装为可执行脚本：

```bash
.venv/bin/python scripts/backup_queue.py --keep 7
```

## 9. 仍然不能宣称的能力

Phase 2A 提升了进程崩溃后的一致性，但仍不是 exactly-once：

- MES 已受理、Node-RED 尚未提交成功状态时崩溃，重启后仍可能重发；必须依赖 MES 幂等键。
- 当前默认单 Node-RED 实例写一个 SQLite 文件，尚未实现多实例选主或分布式锁。
- SQLite 文件未做静态加密，备份、权限和保留周期仍需纳入现场安全方案。
- 尚未增加完整状态迁移审计表、告警规则和结构化日志；Prometheus 指标与在线备份已在 Phase 2B 补齐。

准确表述应为：**至少一次投递 + 去重 + MES 接收端幂等，具备进程崩溃恢复能力。**

## 10. 下一阶段路线

### Phase 2B：可观测性与 SQLite 备份，已完成

- ✅ 增加 Prometheus `/metrics`，覆盖队列、死信、去重、入队、投递和看门狗。
- ✅ 运行计数写入 SQLite，并与队列状态迁移共用事务。
- ✅ 增加在线备份脚本、完整性校验和保留策略。
- 结构化 JSON 日志与告警规则留到下一阶段。

### Phase 2C：部署与运维基线，已完成

- ✅ systemd service/timer 模板和渲染脚本。
- ✅ `/healthz` 健康探针、Prometheus 告警规则和备份 textfile 指标。
- ✅ SQLite 离线恢复、恢复前安全快照和完整自动化演练。
- 日志轮转、异地备份和真实工厂 RTO 记录仍属于后续运维增强。

### Phase 3：真实集成

- MES 沙箱联调，确认幂等键、限流、错误码和回执格式。
- 真实交换机断链、防火墙切换、长时断网和 Wireshark 抓包证据。
- 企业 PKI、角色最小权限、证书轮换和网络分区。

### Phase 4：真实质检模型

- YOLOv8/ONNX 推理接入。
- 模型版本、阈值、图片 URI、推理耗时与误检/漏检样本回流。
