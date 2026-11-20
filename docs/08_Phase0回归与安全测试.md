# Phase 0：一键回归与安全测试

本阶段的目标是让测试与当前安全实现对齐，并让任何人都能在不污染本地运行状态的前提下复现完整验证。

## 1. 一条命令

```bash
cd ~/UA_LinqInspect
.venv/bin/python scripts/run_regression.py
```

脚本会依次执行：

1. `tests/sqlite_queue_test.js`：SQLite 唯一约束、事务原子性、指标持久化、单飞行、退避/看门狗和旧 context 导入。
2. `tests/security_test.py`：传输安全、匿名只读、管理员写/Method 权限。
3. `tests/smoke_test.py`：M1/M2 信息模型、状态机、事件和负面用例。
4. `tests/nodered_e2e.py`：UA 事件 → SQLite 队列 → 模拟 MES → UA 回执。
5. `tests/metrics_test.py`：验证 Prometheus Content-Type、HELP/TYPE、gauge 一致性、计数增量和 correlationId 契约。
6. `tests/backup_queue_test.py`：验证 WAL 在线备份、完整性检查、行数一致和保留策略。
7. `tests/fault_scenarios.py`：5xx/timeout/4xx/429/非法 2xx 分类、退避与死信、Method 拒绝。
8. `tests/restart_recovery.py seed`：制造非空 SQLite 队列后退出。
9. `tests/restart_recovery.py verify`：重启 Node-RED 后恢复 MES，验证队列排空与回执续写。
10. `tests/crash_recovery.py prepare`：让一条请求处于 `SENDING` 并制造待恢复任务。
11. `tests/crash_recovery.py verify`：`SIGKILL` 重启后验证看门狗续传、唯一性和回执续写。
12. `tests/outage_recovery.py stall`：5 秒 `SIGSTOP/SIGCONT` 后验证继续闭环。
13. `tests/outage_recovery.py stale`：验证超过 60 秒的陈旧事件不污染队列和回执。
14. `tests/outage_recovery.py prepare_restart/verify_restart`：重启 UA Server，验证 StartTime 触发的显式重连与事件订阅重建。

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
- 结束后向两个进程发送 `SIGTERM`，超时再强制结束。
- 回归产物保留在 `.tmp/` 便于排障，该目录已被 Git 忽略。

## 3. 当前结果

2026-11-20 实测：

| 测试 | 结果 | 覆盖重点 |
|---|---:|---|
| SQLite 队列单测 | 14/14 PASS | 唯一约束、事务回滚、指标持久化、单飞行、退避、看门狗、迁移、上限 |
| 安全回归 | 6/6 PASS | NoSecurity 拒绝、匿名只读、匿名写/Method 拒绝、管理员写/Method |
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
| **合计** | **255/255 PASS** | 一键隔离回归 |

实际产物示例：`.tmp/regression-20261120-145650/summary.json`。

## 4. 手工执行单个测试

手工运行前必须使用与 Server 相同的管理员账号，并提供安全端点：

```bash
export UA_ENDPOINT_PORT=4840
export UA_ADMIN_USER=admin
export UA_ADMIN_PASS='your-local-password'
export NODERED_BASE=http://127.0.0.1:1880

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

剩余证据工作：Wireshark 抓包、真实交换机断链/防火墙切换和长时断网。

第 1、2 项的实测与契约见 [09_Phase1关联追踪与MES错误分类.md](09_Phase1关联追踪与MES错误分类.md)。
