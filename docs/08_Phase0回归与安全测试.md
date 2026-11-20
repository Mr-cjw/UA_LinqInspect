# Phase 0：一键回归与安全测试

本阶段的目标是让测试与当前安全实现对齐，并让任何人都能在不污染本地运行状态的前提下复现完整验证。

## 1. 一条命令

```bash
cd ~/UA_LinqInspect
.venv/bin/python scripts/run_regression.py
```

脚本会依次执行：

1. `tests/security_test.py`：传输安全、匿名只读、管理员写/Method 权限。
2. `tests/smoke_test.py`：M1/M2 信息模型、状态机、事件和负面用例。
3. `tests/nodered_e2e.py`：UA 事件 → Node-RED 队列 → 模拟 MES → UA 回执。
4. `tests/fault_scenarios.py`：5xx/timeout/4xx/429/非法 2xx 分类、退避与死信、Method 拒绝。

默认遇到第一个失败即停止；需要收集全部结果时使用：

```bash
.venv/bin/python scripts/run_regression.py --keep-going
```

## 2. 隔离策略

每次运行都会创建 `.tmp/regression-<UTC时间>/`，其中包含：

- `ua-server.log`、`ua-server-audit.log`
- `node-red.log`
- 四个测试各自的日志
- `summary.json`
- 临时 Node-RED userDir、flow context 和凭据

隔离措施：

- UA Server 与 Node-RED 使用随机空闲端口，不占用日常 `4840/1880`。
- Node-RED 使用临时 userDir，不修改 `.node-red/context`、去重状态或死信。
- 每次生成随机管理员密码，并同步写入临时 `flows_cred.json`。
- 临时 flow 会同时替换 UA Endpoint 和流内模拟 MES 地址。
- 结束后向两个进程发送 `SIGTERM`，超时再强制结束。
- 回归产物保留在 `.tmp/` 便于排障，该目录已被 Git 忽略。

## 3. 当前结果

2026-11-20 实测：

| 测试 | 结果 | 覆盖重点 |
|---|---:|---|
| 安全回归 | 6/6 PASS | NoSecurity 拒绝、匿名只读、匿名写/Method 拒绝、管理员写/Method |
| UA 冒烟 | 36/36 PASS | 信息模型、状态机、事件、NgRate、负面用例、三类事件 CorrelationId |
| Node-RED E2E | 15/15 PASS | NG 开单、完工报工、回执写回、兼容镜像、关联 ID 贯通 |
| 故障场景 | 52/52 PASS | 5xx、timeout、4xx、429、非法 2xx、冲突工单拒绝 |
| **合计** | **109/109 PASS** | 一键隔离回归 |

实际产物示例：`.tmp/regression-20261120-130414/summary.json`。

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

Phase 0 完成后进入 Phase 1：

1. ✅ `correlationId` 贯穿 UA 事件、Node-RED、MES 请求与回执。
2. ✅ MES `4xx/429/5xx/timeout` 分类和稳定错误码。
3. ⬜ 非空队列 Node-RED 重启恢复测试。
4. ⬜ 短时断网与会话恢复测试。
5. ⬜ 长时断网/Server 重启后的订阅重建与陈旧事件过滤测试。

第 1、2 项的实测与契约见 [09_Phase1关联追踪与MES错误分类.md](09_Phase1关联追踪与MES错误分类.md)。
