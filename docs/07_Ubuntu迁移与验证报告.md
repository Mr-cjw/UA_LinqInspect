# Ubuntu 迁移与验证报告

> 验证日期：2026-11-20 ｜ 环境：Ubuntu / Linux 7.0.0-43-generic ｜ 本报告记录 Phase 1 时点的 158 项结果。
> Phase 2A/2B/2C、M3、运维加固、UA 写回确认、写回前崩溃恢复、事件 outbox、队列容量处置、模拟 MES 幂等合同、SQLite 存储故障恢复、备份 schema 合同、只读目录演练与状态快照失败演练完成后，当前一键回归为 555/555，见
> [15_Phase2C部署运维与恢复演练.md](15_Phase2C部署运维与恢复演练.md)。

## 1. 环境结果

| 项 | 验证值 |
|---|---|
| Python | 3.12.3，项目虚拟环境 `.venv` |
| asyncua | 2.1.0 |
| Python 依赖 | `pip check`：No broken requirements found |
| Node.js | 26.13.1 |
| npm | 11.20.0 |
| Node-RED | 5.0.9，本地 `.node-red/` |
| node-red-contrib-opcua | 0.2.355 |
| OPC UA Endpoint | `opc.tcp://127.0.0.1:4840` |
| Node-RED UI | `http://127.0.0.1:1880` |
| 状态接口 | `GET http://127.0.0.1:1880/qc/status` |

复验命令：

```bash
.venv/bin/python -m pip check
node -v
npm ls --prefix .node-red --depth=0
```

## 2. 自动化结果

测试顺序：启动 UA Server → 启动 Node-RED（加载 `.node-red/flows.json`）→ 依次执行三个测试脚本。

```bash
UA_ENDPOINT_PORT=4840 .venv/bin/python tests/smoke_test.py
UA_ENDPOINT_PORT=4840 .venv/bin/python tests/nodered_e2e.py
UA_ENDPOINT_PORT=4840 .venv/bin/python tests/fault_scenarios.py
```

| 测试 | 结果 | 覆盖重点 |
|---|---|---|
| M1 冒烟 | **36/36 PASS** | 信息模型、22 变量（含 outbox 状态）、7 Method、3 Event、业务变量全 RO、回执 Method 单写、SN 追溯、关联 ID、幂等开检、完工校验 |
| M2 端到端 | **21/21 PASS** | NG 事件 → MES 开单 → UA 双回执；完工报工 → UA 双回执；关联 ID 贯通；工单号不被覆盖；MES 重复/冲突 HTTP 契约 |
| MES 接收端幂等 | **10/10 PASS** | 稳定 payload 指纹、重复返回原回执、同键不同内容 409、旧回执迁移、原型污染防护 |
| 故障场景 | **52/52 PASS** | 5xx、timeout、4xx、429、非法 2xx、Method 服务端拒绝、状态接口可观察、恢复 MES 后链路可用 |
| 重启恢复 | **18/18 PASS** | 非空队列 context 恢复、NG/完工回执续写、无新增死信 |
| 短断网 | **10/10 PASS** | 5 秒停顿后新安全会话继续开检、NG 与完工闭环 |
| 陈旧事件 | **7/7 PASS** | 超过 60 秒的事件不入队、不改去重、不写回执、不请求 MES |
| UA Server 重启 | **14/14 PASS** | StartTime 变化触发重连，事件订阅重建，新工单闭环 |
| **合计** | **158/158 PASS** | 一键隔离回归 |

关键观测：

- NG 开单后 `LastNgMesOrderNo = MES-*`，`LastMesOrderNo` 同步同一值。
- 完工报工后 `LastCompletionReceiptNo = RC-*`，`LastMesOrderNo` 同步同一值。
- `CurrentWorkOrderId` 在回执写回后保持不变。
- 5xx 重试耗尽后任务进入死信，且不会误写回执。
- timeout 模式在退避窗口内恢复，任务最终成功出队。
- 4xx 立即死信；429 按 Retry-After 退避后死信；非法 2xx 不写回执。
- UA 事件、MES 请求和 MES 回执使用同一 correlationId。
- Method 拒绝不会覆盖当前工单，也不会触发后续 MES 动作。

## 3. 本轮工程修正

| 修正 | 目的 |
|---|---|
| MES 回执拆为 `LastNgMesOrderNo` / `LastCompletionReceiptNo` | 避免 NG 开单与完工报工互相覆盖 |
| 保留 `LastMesOrderNo` 兼容镜像 | 不破坏旧客户端，同时明确它不是业务判断依据 |
| 完工事件 `NgRate` 保持 `-1.0` | 与 M1 空窗口语义一致，避免“无样本”被误报为 0% 不良 |
| 新增 `GET /qc/status` | 可在现场查看队列、死信、心跳和 MES 模式 |
| Node-RED 启用 localfilesystem context | 队列、去重、死信和模式近实时落盘 |
| 新增 `tests/fault_scenarios.py` | 把 5xx、timeout、Method 拒绝变成可重复证据 |
| 新增 `tests/restart_recovery.py` | 把非空队列重启恢复变成可重复证据 |
| 修复 M1 冒烟测试 SN 竞态 | 高频检测下断言稳定 |

## 4. 持久化观察

Node-RED 重启后会加载 `.node-red/context/qcflow/flow.json`。本次验证中，重启后的 `/qc/status` 仍能看到上一进程写入的去重计数和死信记录，说明 context 文件确实被读取。

但必须区分两件事：

- **已验证**：context 能跨 Node-RED 进程重启保留状态。
- **已验证**：5xx 下保留 NG 与完工队列项，Node-RED 用同一 userDir 重启后恢复并排空，NG/完工回执续写，死信数不增加。
- **已验证**：UA Server 重启后，心跳读取的 `ServerStatus.StartTime` 发生变化并触发三个 UA Client 显式重连，旧事件订阅重新挂载。
- **未验证**：断电一致性、重复开单/报工的跨进程严格证明。

本节是 Phase 1 时点的结论。Phase 2A 已迁移到 SQLite 事务队列，并补充 `SIGKILL`
恢复测试；但仍不能宣称断电安全或 exactly-once，详见
[10_Phase2A_SQLite事务队列与崩溃恢复.md](10_Phase2A_SQLite事务队列与崩溃恢复.md)。

## 5. 尚未完成

- 企业 PKI、证书轮换和按角色最小权限（本地 CA/管理员账号已实现）。
- M3 Wireshark 抓包、真实交换机断链/防火墙切换和长时断网证据。
- Node-RED 注入节点触发的 Method 拒绝流级录屏证据（服务端拒绝已自动化）。
- 真实 MES 接口与真实 YOLOv8/ONNX 推理数据。
- 结构化日志、集中监控和跨小时 soak 测试。

## 6. 复现与排障

完整启动步骤见 [03_运行验证与排障手册.md](03_运行验证与排障手册.md)。答辩与试点叙事见 [06_作品集答辩与工厂试点落地.md](06_作品集答辩与工厂试点落地.md)。
