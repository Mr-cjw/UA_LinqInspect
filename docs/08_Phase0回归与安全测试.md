# Phase 0：一键回归与安全测试

本阶段的目标是让测试与当前安全实现对齐，并让任何人都能在不污染本地运行状态的前提下复现完整验证。

## 1. 一条命令

```bash
cd ~/UA_LinqInspect
.venv/bin/python scripts/run_regression.py
```

脚本会依次执行：

1. `tests/sqlite_queue_test.js`：SQLite 唯一约束、事务原子性、指标持久化、单飞行、退避/看门狗和旧 context 导入。
2. `tests/state_persistence_test.py`：状态快照往返、非法 Running 回退和损坏 JSON 回退。
3. `tests/qc_state_restart_test.py`：`SIGKILL` 后验证运行中工单、计数和 SN 序列恢复，并完成恢复后的工单。
4. `tests/edge_core_test.py`：Edge Core 控制面契约、幂等冲突、重试/死信和 UA 写回确认。
5. `tests/security_test.py`：UA 传输安全、匿名只读、管理员写/Method、控制变量直接写拒绝，以及 Node-RED HTTP 认证。
6. `tests/smoke_test.py`：M1/M2 信息模型、状态机、事件和负面用例。
7. `tests/nodered_e2e.py`：UA 事件 → SQLite 队列 → 模拟 MES → UA 回执。
8. `tests/metrics_test.py`：Prometheus Content-Type、HELP/TYPE、gauge 一致性、计数增量和 correlationId 契约。
9. `tests/backup_queue_test.py`：WAL 在线备份、完整性检查、行数一致和保留策略。
10. `tests/fault_scenarios.py`：5xx/timeout/4xx/429/非法 2xx 分类、退避与死信、Method 拒绝。
11. `tests/restart_recovery.py seed/verify`：非空 SQLite 队列重启、排空和回执续写。
12. `tests/crash_recovery.py prepare/verify`：请求处于 `SENDING` 时 `SIGKILL`，验证看门狗续传、唯一性和回执续写。
13. `tests/outage_recovery.py stall`：5 秒 `SIGSTOP/SIGCONT` 后验证继续闭环。
14. `tests/outage_recovery.py stale`：超过 60 秒的陈旧事件不污染队列和回执。
15. `tests/outage_recovery.py prepare_restart/verify_restart`：重启 UA Server，验证 StartTime 触发的显式重连与事件订阅重建。

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
| SQLite 队列单测 | 14/14 PASS | 唯一约束、事务回滚、指标持久化、单飞行、退避、看门狗、迁移、上限 |
| 状态工具单测 | 3/3 PASS | 快照往返、非法 Running 回退、损坏 JSON 回退 |
| 工位状态重启 | 11/11 PASS | `SIGKILL` 后工单、计数、SN 续增、恢复后完工 |
| Edge Core API/状态机 | 7/7 PASS | 幂等冲突、有效回执、4xx/5xx、死信重放、重启后去重、API 查询 |
| 安全回归 | 12/12 PASS | UA NoSecurity/匿名权限、控制变量直接写拒绝、Node-RED 未认证 401/认证 200 |
| UA 冒烟 | 36/36 PASS | 信息模型、状态机、事件、NgRate、负面用例、三类事件 CorrelationId |
| Node-RED E2E | 15/15 PASS | NG 开单、完工报工、回执写回、兼容镜像、关联 ID 贯通 |
| Prometheus 指标 | 43/43 PASS | 文本格式、HELP/TYPE、gauge 一致性、计数增量、响应契约 |
| SQLite 备份 | 17/17 PASS | WAL 热备、完整性校验、行数一致、保留策略、运行中数据库 |
| 故障场景 | 52/52 PASS | 5xx、timeout、4xx、429、非法 2xx、冲突工单拒绝 |
| 重启恢复 | 18/18 PASS | 非空 SQLite 队列恢复、队列排空、NG/完工回执续写、无新增死信 |
| SIGKILL 崩溃恢复 | 23/23 PASS | 在途状态持久化、看门狗续传、无重复队列行、回执续写 |
| 短断网 | 10/10 PASS | 5 秒停顿后继续开检、NG 与完工闭环 |
| 陈旧事件 | 7/7 PASS | 不入队、不改去重、不写回执、不请求 MES |
| UA Server 重启 | 14/14 PASS | StartTime 变化触发重连、旧订阅重建、新工单闭环 |
| **合计** | **282/282 PASS** | 18 个测试模块；含 3 项状态单测和 7 项 Edge Core 单测 |

实际证据：`.tmp/regression-20261120-164546/summary.json`。

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

Phase 0/1 与 Phase 2A 已完成：

1. ✅ `correlationId` 贯穿 UA 事件、Node-RED、MES 请求与回执。
2. ✅ MES `4xx/429/5xx/timeout` 分类和稳定错误码。
3. ✅ 非空队列 Node-RED 重启恢复测试。
4. ✅ 短时断网与会话恢复测试。
5. ✅ UA Server 重启后的订阅重建与 60 秒陈旧事件过滤测试。
6. ✅ SQLite 事务队列、去重/入队原子性与 `SIGKILL` 崩溃恢复。
7. ✅ Prometheus 指标与 SQLite WAL 在线备份。
8. ✅ Node-RED 管理/HTTP 认证、回环绑定和加密凭据基线。
9. ✅ 工位控制状态单写者、快照持久化和 `SIGKILL` 重启恢复。

剩余证据工作：Wireshark 抓包、真实交换机断链/防火墙切换、长时断网和现场 soak。工位状态与 MES 队列仍分属两套持久化边界，后续需要收敛。

第 1、2 项的实测与契约见 [09_Phase1关联追踪与MES错误分类.md](09_Phase1关联追踪与MES错误分类.md)。
