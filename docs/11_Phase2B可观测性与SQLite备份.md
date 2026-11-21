# Phase 2B：可观测性与 SQLite 在线备份

> 日期：2026-11-21 ｜ Phase 2B 证据：`.tmp/regression-20261120-145650/summary.json`（255/255）｜ 当前合并回归：632/632，证据：`.tmp/regression-20261121-142934/summary.json`

## 1. 这一阶段解决了什么

Phase 2A 已经让队列、去重和死信具备 SQLite 事务语义，但运维侧仍有两个缺口：

- 只能看 `/qc/status` 的瞬时快照，没有可供 Prometheus 采集的标准指标。
- WAL 模式下的 SQLite 不能靠直接复制主文件获得一致备份。

Phase 2B 补齐了两件事：

1. 在 SQLite 中持久化有界运行计数，并暴露 `GET /metrics`。
2. 提供在线备份脚本，完成备份、完整性检查、原子落盘和保留策略。

## 2. 指标设计

### 2.1 持久化表

`nodered/sqlite_store.js` 新增 `metrics` 表：

```text
name, type, outcome, error_code, value, updated_at
```

主键为 `(name, type, outcome, error_code)`。标签只允许低基数枚举：

- `type`：`ng` / `complete`
- `outcome`：`accepted` / `duplicate` / `rejected` / `success` / `retry` / `dead` / `recovered`
- `error_code`：代码内稳定的错误码，不使用 SN、工单号或 correlationId 作为标签；未知 MES 错误统一收敛为 `OTHER_ERROR`

计数更新与队列状态迁移位于同一 SQLite 事务。例如入队、完成、退避或死信提交失败时，计数会一起回滚；Node-RED 重启后计数仍可恢复。

### 2.2 Prometheus 指标

`GET /metrics` 返回 `text/plain; version=0.0.4`：

| 指标 | 类型 | 含义 |
|---|---|---|
| `qc_queue_depth{status}` | gauge | `pending` / `sending` 队列深度 |
| `qc_queue_oldest_age_seconds` | gauge | 最老队列项年龄 |
| `qc_dead_letters` | gauge | 当前保留的死信数 |
| `qc_dedup_keys{kind}` | gauge | 三类去重键数量 |
| `qc_heartbeat_failures` | gauge | 连续 UA 心跳失败数（0-3） |
| `qc_event_outbox_pending` | gauge | 等待 Node-RED ACK 的 NG/完工事件数 |
| `qc_event_outbox_oldest_age_seconds` | gauge | 最老未确认事件年龄 |
| `qc_event_outbox_capacity` | gauge | outbox 硬上限 |
| `qc_event_outbox_high_watermark` | gauge | outbox 背压阈值 |
| `qc_event_outbox_backpressure` | gauge | 达到高水位时为 1 |
| `qc_event_outbox_status_age_seconds` | gauge | 最近一次成功读取 OutboxStatus 的年龄；-1 表示从未读取 |
| `qc_uptime_seconds` | gauge | 本流程首次被状态接口观察到后的秒数 |
| `qc_database_bytes{kind}` | gauge | SQLite 主库、WAL、SHM 文件体积 |
| `qc_enqueue_total{type,outcome}` | counter | 入队成功、重复与容量拒绝次数 |
| `qc_delivery_total{type,outcome}` | counter | 成功、退避、死信次数 |
| `qc_delivery_errors_total{type,error_code}` | counter | 按稳定错误码聚合的投递失败 |
| `qc_watchdog_total{outcome}` | counter | 看门狗恢复/最终死信次数 |
| `qc_ua_write_pending` | gauge | 等待 UA 写回全部目标确认的任务数 |
| `qc_ua_write_total{type,outcome}` | counter | UA 写回成功、重试、死信次数 |
| `qc_ua_write_errors_total{type,error_code}` | counter | 按稳定错误码聚合的 UA 写回失败 |

注意：`qc_delivery_errors_total` 统计的是失败尝试，不是失败任务数。一个任务经历 3 次退避后进入死信，会贡献 4 次 `WATCHDOG_TIMEOUT` 失败尝试。错误码采用白名单，非白名单值记为 `OTHER_ERROR`，避免 MES 返回任意字符串导致指标基数膨胀。

采集示例：

```bash
curl -s http://127.0.0.1:1880/metrics | head -40
```

`/qc/status` 的 `queue`、`deadLetters.last`、`dedup`、`lastRequest*` 等结构保持兼容；新增 `uaWriteback.pending/sending` 反映 UA 写回阶段，新增 `outbox` 对象反映 UA 事件积压、容量、高水位和最近一次心跳读取的新鲜度。队列容量拒绝会记录 `lastEnqueueRejection`，且事件不会 ACK。MES 成功回执只把任务推进到写回阶段，两个目标变量都确认后才删除队列项。

### 2.3 安全边界

`/metrics` 与 `/qc/status` 在安全基线中由 Node-RED `httpNodeAuth` 保护，并只监听回环地址。生产试点仍应在反向代理、防火墙或网关层限制来源，不要直接暴露到公网。

## 3. SQLite 在线备份

### 3.1 命令

```bash
.venv/bin/python scripts/backup_queue.py
```

默认行为：

- 源库：`QC_DB_PATH`；未设置时为 `data/qc-queue.sqlite`
- 输出目录：源库同级的 `backups/`
- 文件名：`qc-queue-YYYYmmdd-HHMMSS.sqlite`
- 保留：最近 7 份

显式参数：

```bash
.venv/bin/python scripts/backup_queue.py \
  --source data/qc-queue.sqlite \
  --output-dir /var/backups/ua-linqinspect \
  --keep 14
```

脚本输出 JSON 摘要，包含源路径、备份路径、大小、完整性结果、关键表行数、保留与删除列表。

### 3.2 一致性流程

1. 以只读连接打开源库。
2. 使用 Python 标准库 `sqlite3.Connection.backup` 备份到临时文件。
3. 对临时文件执行 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`。
4. 校验必需表和关键列，空 SQLite 或错 schema 不会被当作有效备份。
5. 文件权限设为 `0600`，再使用原子重命名替换为最终文件名。
6. 按文件时间清理超出保留策略的旧备份。

不能在 WAL 模式下只复制主 `.sqlite` 文件，因为已提交数据可能仍在 `-wal` 中。

### 3.3 当前边界

- 已自动化：在线一致性备份、完整性检查、原子落盘、保留策略、备份文件可读与行数一致。
- 未完成：systemd timer/定时任务、异地备份、加密、告警、完整灾备恢复演练。
- 备份脚本不会删除 WAL 文件，也不会停止 Node-RED；适合在线热备。

## 4. 自动化测试

新增：

- `tests/metrics_test.py`：80 项断言，验证 Content-Type、HELP/TYPE、指标解析、gauge 与 `/qc/status` 一致、UA 写回计数、outbox 积压指标、SQLite/WAL/SHM 体积、NG/完工闭环计数增量、correlationId 与响应契约仍可观察。
- `tests/backup_queue_test.py`：一键回归 31 项断言（独立运行 30 项），验证活跃 WAL 库在线备份、完整性检查、必需表/列合同、空库与缺列拒绝、只读源库备份、只读输出目录拒绝、只读指标目录降级、关键行数一致、保留策略、缺失源文件错误，以及运行中的 Node-RED 数据库备份。

扩展：

- `tests/sqlite_queue_test.js`：新增 UA 写回目标确认、重启续写、失败重试、值冲突和共享投递读模型断言，现为 20/20。

一键回归：

```bash
.venv/bin/python scripts/run_regression.py --keep-going
```

2026-11-20 Phase 2B 实测 **255/255 PASS**；加入 Node-RED 认证、Edge Core、Phase 2C 运维、M3、存储告警、状态持久化、UA 写回确认、写回前崩溃恢复、事件 outbox、队列容量处置、模拟 MES 幂等合同、SQLite 存储故障恢复、真实挂载 EROFS/ENOSPC、备份 schema 合同、只读目录演练、状态持久化失败演练、进程内回滚、无硬件演示与发送泵迁移工具与 UA 写回适配器后当前合并回归为 **632/632 PASS**：

| 测试 | 结果 |
|---|---:|
| SQLite 队列单测 | 20/20 |
| SQLite 存储故障 | 3/3 |
| 真实挂载存储 | 25/25 |
| MES 接收端幂等 | 10/10 |
| 状态工具单测 | 19/19 |
| 事件 outbox 单测 | 3/3 |
| outbox 容量/导出单测 | 9/9 |
| outbox 状态接口 | 9/9 |
| 工位状态重启 | 13/13 |
| 事件 outbox 崩溃恢复 | 18/18 |
| 健康探针 | 10/10 |
| Edge Core | 14/14 |
| Edge Core 入站 | 7/7 |
| 发送权并发 | 3/3 |
| 受控迁移/回滚 | 1/1 |
| Node-RED 在线全链路 | 14/14 |
| 无硬件演示 | 4/4 |
| Node-RED 迁移 | 5/5 |
| UA 写回适配器 | 3/3 |
| 安全 | 16/16 |
| UA 冒烟 | 36/36 |
| Node-RED E2E | 21/21 |
| Prometheus 指标 | 80/80 |
| SQLite 备份 | 31/31 |
| 离线恢复 | 39/39 |
| 部署模板 | 24/24 |
| 故障场景 | 52/52 |
| MES 长断网 | 24/24 |
| 周期 soak | 20/20 |
| 正常重启 | 18/18 |
| SIGKILL 崩溃恢复 | 23/23 |
| UA 写回前崩溃恢复 | 27/27 |
| 短断网 | 10/10 |
| 陈旧过滤 | 7/7 |
| UA Server 重启 | 14/14 |
| **合计** | **632/632** |

## 5. 下一阶段

### Phase 2C：部署与运维闭环

1. 用 systemd timer 或 cron 包装 `scripts/backup_queue.py`，加入失败告警。
2. 做一次从备份库启动 Node-RED 的完整恢复演练，并记录 RPO/RTO。
3. 增加 Node-RED/Server 的 systemd 或 Compose 模板、健康检查和日志轮转。
4. 在 Prometheus 侧补充告警规则：队列持续增长、死信增加、心跳连续失败、备份过期。

### Phase 3：真实集成

1. 与真实 MES 确认幂等键、限流、错误码和回执格式。
2. 补充真实交换机断链、防火墙切换、长时断网与抓包证据。
3. 落地企业 PKI、最小权限、证书轮换和网络分区。

准确边界仍是：**至少一次投递 + 去重 + MES 接收端幂等，具备进程崩溃恢复和在线备份能力，但不是 exactly-once。**
