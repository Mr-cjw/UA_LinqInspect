# Ubuntu 迁移与验证报告

> 验证日期：2026-11-11 ｜ 环境：Ubuntu / Linux 7.0.0-43-generic ｜ 结论：M1、M2 与故障场景自动化全部通过；M3 断网重连仍未验收。

## 1. 环境结果

| 项 | 验证值 |
|---|---|
| Python | 3.14.4，项目虚拟环境 `.venv` |
| asyncua | 2.1.0 |
| Python 依赖 | `pip check`：No broken requirements found |
| Node.js | 26.13.0 |
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
| M1 冒烟 | **33/33 PASS** | 信息模型、15 变量、3 Method、3 Event、RO/RW 权限、SN 追溯、幂等开检、完工校验 |
| M2 端到端 | **10/10 PASS** | NG 事件 → MES 开单 → UA 双回执；完工报工 → UA 双回执；工单号不被覆盖 |
| 故障场景 | **19/19 PASS** | 5xx 死信、timeout 退避恢复、Method 服务端拒绝、状态接口可观察、恢复 MES 后链路可用 |

关键观测：

- NG 开单后 `LastNgMesOrderNo = MES-*`，`LastMesOrderNo` 同步同一值。
- 完工报工后 `LastCompletionReceiptNo = RC-*`，`LastMesOrderNo` 同步同一值。
- `CurrentWorkOrderId` 在回执写回后保持不变。
- 5xx 重试耗尽后任务进入死信，且不会误写回执。
- timeout 模式在退避窗口内恢复，任务最终成功出队。
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
| 修复 M1 冒烟测试 SN 竞态 | 高频检测下断言稳定 |

## 4. 持久化观察

Node-RED 重启后会加载 `.node-red/context/qcflow/flow.json`。本次验证中，重启后的 `/qc/status` 仍能看到上一进程写入的去重计数和死信记录，说明 context 文件确实被读取。

但必须区分两件事：

- **已验证**：context 能跨 Node-RED 进程重启保留状态。
- **未验证**：非空发送队列在重启/断电后恢复，且不重复开单/报工。

因此当前只能表述为“具备近实时落盘基础，降低重启丢失风险”，不能宣称 exactly-once。工厂试点前应迁移到 SQLite 事务队列或独立边缘发送服务，并补重启恢复测试。

## 5. 尚未完成

- OPC UA 证书、信任列表、写操作角色权限。
- M3 短时断网恢复、长时断网订阅重建、Wireshark 抓包证据。
- Node-RED 注入节点触发的 Method 拒绝流级录屏证据（服务端拒绝已自动化）。
- 真实 MES 接口与真实 YOLOv8/ONNX 推理数据。
- `correlationId` 贯穿、MES 4xx/429/5xx 分类、结构化日志与集中监控。
- 非空队列重启恢复、断电恢复、长时间 soak 测试。

## 6. 复现与排障

完整启动步骤见 [03_运行验证与排障手册.md](03_运行验证与排障手册.md)。答辩与试点叙事见 [06_作品集答辩与工厂试点落地.md](06_作品集答辩与工厂试点落地.md)。
