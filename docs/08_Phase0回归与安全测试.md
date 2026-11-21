# Phase 0：一键回归与安全测试

本阶段的目标是让测试与当前安全实现对齐，并让任何人都能在不污染本地运行状态的前提下复现完整验证。

## 1. 一条命令

```bash
cd ~/UA_LinqInspect
.venv/bin/python scripts/run_regression.py
```

脚本会依次执行：

1. `tests/sqlite_queue_test.js`：SQLite 唯一约束、事务原子性、UA 写回确认、指标持久化、单飞行、退避/看门狗、死信重放和旧 context 导入。
2. `tests/storage_failure_test.js`：页数封顶模拟磁盘写满，验证入队/完工完整回滚；损坏库启动显式失败且不被重建。
   另有 `tests/mount_storage_test.py`：在 Docker `--privileged` 的受限 tmpfs 上执行真实内核 `EROFS`/`ENOSPC`、状态回滚、临时文件清理与释放容量后的恢复；同一日志包含 SQLite 队列挂载演练。
3. `tests/health_test.js`：`/healthz` 的 ready/degraded/unavailable、响应头和 flow 接线。
4. `tests/state_persistence_test.py`：JSON/SQLite 状态与 Outbox 往返、SQLite 事务回滚、旧 JSON 迁移、损坏回退、写入失败保护，以及保存失败后回滚内存状态并重同步 UA 变量。
5. `tests/qc_state_restart_test.py`：`SIGKILL` 后验证 SQLite 工单、计数、SN 与 Outbox 恢复，并完成恢复后的工单。
6. `tests/edge_core_test.py`：Edge Core 控制面契约、严格输入校验、事件类型专属回执、幂等冲突、重试/死信和 UA 写回确认。
7. `tests/security_test.py`：UA 传输安全、匿名只读、管理员 Method、控制/追溯/回执变量直接写拒绝、Node-RED HTTP 认证和死信重放鉴权。
8. `tests/smoke_test.py`：M1/M2 信息模型、状态机、事件和负面用例。
9. `tests/nodered_e2e.py`：UA 事件 → SQLite 队列 → 模拟 MES → UA 回执。
10. `tests/metrics_test.py`：Prometheus 指标格式、UA 写回、SQLite/WAL/SHM 体积和 correlationId 契约。
11. `tests/backup_queue_test.py`：WAL 在线备份、完整性检查、行数一致、保留策略、Prometheus textfile、只读源库备份与只读输出目录拒绝。
12. `tests/restore_queue_test.py`：dry-run、`--force`、恢复前快照、损坏目标隔离、WAL/SHM 清理、只读目标拒绝和恢复后继续消费。
13. `tests/deploy_templates_test.py`：模板渲染、systemd verify、Alertmanager、logrotate、告警规则和环境变量。
14. `tests/fault_scenarios.py`：5xx/timeout/4xx/429/非法 2xx 分类、退避与死信、Method 拒绝。
15. `tests/long_outage_recovery.py`：MES 长断网、死信重放和恢复 RTO。
16. `tests/soak_test.py`：多工单周期 soak、回执唯一性和资源漂移。
17. `tests/restart_recovery.py seed/verify`：非空 SQLite 队列重启、排空和回执续写。
18. `tests/crash_recovery.py prepare/verify`：请求处于 `SENDING` 时 `SIGKILL`，验证看门狗续传、唯一性和回执续写。
19. `tests/outage_recovery.py stall`：5 秒 `SIGSTOP/SIGCONT` 后验证继续闭环。
20. `tests/outage_recovery.py stale`：超过 60 秒的陈旧事件不污染队列和回执。
21. `tests/outage_recovery.py prepare_restart/verify_restart`：重启 UA Server，验证 StartTime 触发的显式重连与事件订阅重建。
22. `tests/mes_idempotency_test.js`：模拟 MES 的 payload 指纹、重复回执、冲突拒绝和原型污染防护。

默认遇到第一个失败即停止；需要收集全部结果时使用：

```bash
.venv/bin/python scripts/run_regression.py --keep-going
```

## 2. 隔离策略

每次运行都会创建 `.tmp/regression-<UTC时间>/`，其中包含：

- `ua-server.log`、`ua-server-audit.log`
- `node-red.log`
- 各测试阶段的日志
- `summary.json`
- 临时 Node-RED userDir、独立 `qc-queue.sqlite` 和凭据

隔离措施：

- UA Server 与 Node-RED 使用随机空闲端口，不占用日常 `4840/1880`。
- Node-RED 使用临时 userDir 和 `QC_DB_PATH=<artifact>/qc-queue.sqlite`，不修改本地 SQLite 队列。
- 每次生成随机管理员密码，并同步写入临时 `flows_cred.json`。
- 临时 flow 会同时替换 UA Endpoint 和流内模拟 MES 地址。
- 临时 Node-RED 启用 `adminAuth`、`httpNodeAuth` 和 `127.0.0.1` 绑定；安全回归验证未认证 401 与带认证 200。
- 每次运行使用独立 `XDG_CONFIG_HOME`，将 `node-red-opcua` 的 CA/CRL 写入临时 PKI。
- 结束后向两个进程发送 `SIGTERM`，超时再强制结束。
- 回归产物保留在 `.tmp/` 便于排障，该目录已被 Git 忽略。

## 3. 当前结果

2026-11-21 实测：

| 测试 | 结果 | 覆盖重点 |
|---|---:|---|
| SQLite 队列单测 | 20/20 PASS | 唯一约束、事务回滚、共享投递读模型、UA 写回确认、单飞行、退避、看门狗、迁移、上限、死信重放 |
| SQLite 存储故障 | 3/3 PASS | 写满时入队/完工回滚、损坏库启动失败、恢复后继续可用 |
| 真实挂载存储 | 25/25 PASS | 受限 tmpfs 的 `EROFS`/`ENOSPC`、状态回滚、SQLite 事务保持、临时文件清理与恢复 |
| MES 接收端幂等 | 10/10 PASS | 稳定指纹、重复返回原回执、冲突 409、旧回执迁移、原型污染防护 |
| 健康探针 | 10/10 PASS | ready/degraded/unavailable、心跳边界、响应头、flow 接线 |
| outbox 状态接口 | 9/9 PASS | `OutboxStatus` 解析、容量拒收不 ACK、背压健康降级、`/qc/status` 与 Prometheus 指标 |
| 状态/Outbox 单测 | 19/19 PASS | JSON/SQLite 往返、事务回滚、旧状态迁移、损坏回退、写入失败保护与 UA 重同步 |
| 事件 outbox 单测 | 3/3 PASS | 发布异常不丢状态、稳定 ID 重发、ACK 幂等 |
| outbox 容量/导出单测 | 9/9 PASS | 高水位/硬上限拒绝、不丢事件、分页导出、令牌校验、清空幂等、重启恢复积压 |
| 工位状态重启 | 13/13 PASS | `SIGKILL` 后工单、计数、SN 与 Outbox 恢复、继续完工 |
| 事件 outbox 崩溃恢复 | 18/18 PASS | 未 ACK 事件跨 `SIGKILL` 保留、稳定 ID 重发、ACK 后停止、积压状态恢复与背压拒绝 |
| Edge Core API/状态机 | 14/14 PASS | 严格输入约束、MES Bearer/Basic 鉴权、NG/完工专属回执、稳定错误码、幂等冲突、4xx/5xx、死信重放、重启后去重、API 查询 |
| Edge Core 入站 | 7/7 PASS | Node-RED 直写 202 后才 ACK，503 保持 UA outbox 未确认 |
| 发送权并发与迁移/回滚 | 4/4 PASS | Node/Python 单赢家、Edge Core 启动互锁、离线迁移与源库保留 |
| Node-RED 在线全链路 | 14/14 PASS | Node-RED 直写、Edge Core Worker、MES Basic、真实 UA 写回、回滚与审计保留 |
| MES 契约体检 | 5/5 PASS | dry-run 不联网、严格 MES 通过、非幂等警告、非法 2xx 失败、写入确认保护 |
| 无需硬件演示 | 4/4 PASS | 真实 API/SQLite/Worker、超时、服务重启、死信重放、MES 唯一回执与 UA 写回确认 |
| Node-RED 迁移 | 5/5 PASS | dry-run、幂等导入、状态/回执/死信保留、冲突拒绝与确定性 ID |
| UA 写回适配器 | 3/3 PASS | 真实安全 Method、成功 `DONE`、失败回报与缺失回执拒绝 |
| 安全回归 | 16/16 PASS | UA NoSecurity/匿名权限、控制变量直接写拒绝、匿名 outbox 导出/清空拒绝、Node-RED 认证 |
| UA 冒烟 | 36/36 PASS | 信息模型、状态机、事件、NgRate、负面用例、三类事件 CorrelationId |
| Node-RED E2E | 21/21 PASS | NG 开单、完工报工、回执写回、兼容镜像、关联 ID 贯通、MES 重复/冲突 HTTP 契约 |
| Prometheus 指标 | 80/80 PASS | 文本格式、HELP/TYPE、gauge 一致性、UA 写回计数、outbox 积压指标、SQLite/WAL/SHM 体积、响应契约 |
| SQLite 备份 | 31/31 PASS | WAL 热备、完整性/外键/schema 校验、空库/缺列拒绝、只读源/输出演练、行数一致、保留策略、运行中数据库、textfile 指标 |
| 离线恢复 | 39/39 PASS | dry-run、`--force`、安全快照、损坏目标隔离、只读目标拒绝、WAL/SHM 清理、恢复后继续消费 |
| 部署模板 | 24/24 PASS | 模板渲染、systemd verify、Prometheus/Alertmanager/outbox/队列容量告警、logrotate、环境变量、Node-RED 认证配置 |
| 故障场景 | 52/52 PASS | 5xx、timeout、4xx、429、非法 2xx、冲突工单拒绝 |
| MES 长断网 | 24/24 PASS | 重试耗尽死信、积压保留、恢复排空、实测 RTO、受鉴权重放 |
| 周期 soak | 20/20 PASS | 6 个工单循环、回执唯一、无新增死信、无队列漂移 |
| 重启恢复 | 18/18 PASS | 非空 SQLite 队列恢复、队列排空、NG/完工回执续写、无新增死信 |
| SIGKILL 崩溃恢复 | 23/23 PASS | 在途状态持久化、看门狗续传、无重复队列行、回执续写 |
| UA 写回前崩溃恢复 | 27/27 PASS | MES 成功后冻结 UA、注入 NG/完工、SIGKILL 后从持久化写回续跑 |
| 短断网 | 10/10 PASS | 5 秒停顿后继续开检、NG 与完工闭环 |
| 陈旧事件 | 7/7 PASS | 不入队、不改去重、不写回执、不请求 MES |
| UA Server 重启 | 14/14 PASS | StartTime 变化触发重连、旧订阅重建、新工单闭环 |
| **合计** | **637/637 PASS** | 40 个测试模块 |

实际产物：`.tmp/regression-20261121-152442/summary.json`。

## 4. 手工执行单个测试

手工运行前必须使用与 Server 相同的管理员账号，并提供安全端点：

```bash
source .node-red/.secure-env
export UA_ENDPOINT_PORT=4840
export UA_ADMIN_USER="$NODERED_OPCUA_USER"
export UA_ADMIN_PASS="$NODERED_OPCUA_PASSWORD"
export NODERED_BASE=http://127.0.0.1:1880
export NODERED_HTTP_AUTH="Basic $(printf '%s:%s' "$NODERED_HTTP_USER" "$NODERED_HTTP_PASS" | base64 -w0)"

.venv/bin/python tests/security_test.py
.venv/bin/python tests/smoke_test.py
.venv/bin/python tests/nodered_e2e.py
.venv/bin/python tests/fault_scenarios.py
```

共享客户端配置位于 `tests/ua_test_client.py`，默认使用 `certs/client/` 与 `certs/server/` 下的证书。

## 5. 已知提示

测试日志可能出现：

```text
certificate does not contain the hostname in DNSNames ...
```

这是 asyncua 对客户端证书主机名 SAN 的提示；当前证书 URI、证书链和签名加密连接均已验证成功，不影响测试结论。后续轮换证书时可在客户端证书中加入本机 DNS SAN。

## 6. 下一步

Phase 0/1、Phase 2A/2B/2C 已完成：

1. ✅ `correlationId` 贯穿 UA 事件、Node-RED、MES 请求与回执。
2. ✅ MES `4xx/429/5xx/timeout` 分类和稳定错误码。
3. ✅ 非空队列 Node-RED 重启恢复测试。
4. ✅ 短时断网与会话恢复测试。
5. ✅ UA Server 重启后的订阅重建与 60 秒陈旧事件过滤测试。
6. ✅ SQLite 事务队列、去重/入队原子性与 `SIGKILL` 崩溃恢复。
7. ✅ Prometheus 指标与 SQLite WAL 在线备份。
8. ✅ Node-RED 管理/HTTP 认证、回环绑定和加密凭据基线。
9. ✅ 健康探针、systemd 部署模板、Prometheus 告警规则和备份 timer。
10. ✅ 离线恢复脚本、恢复前安全快照和完整恢复演练。

剩余证据工作：Wireshark 抓包、真实交换机断链/防火墙切换、长时断网和现场 soak。

第 1、2 项的实测与契约见 [09_Phase1关联追踪与MES错误分类.md](09_Phase1关联追踪与MES错误分类.md)。
