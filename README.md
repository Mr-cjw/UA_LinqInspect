# UA_LinqInspect — 工业质检 OPC UA OT-IT 闭环原型

零成本软件仿真环境下的工业质检原型：**AI 质检（虚拟） → OPC UA Server → Node-RED → 模拟 MES 工单闭环**。

设计与排期依据：[Phase0_需求拆解与信息模型.md](Phase0_需求拆解与信息模型.md)（已冻结，含信息模型表、编码硬约束、里程碑 M1~M4）。

## 项目简介

UA_LinqInspect 演示的是“质检结果如何安全、可追溯地进入 MES”，不是训练视觉模型本身。

它把产线质检结果表达为标准 OPC UA 对象模型，通过事件通知把 NG/完工信息送到
Node-RED；Node-RED 负责去重、削峰、缓冲、重试和死信处理，调用模拟 MES 完成
开单/报工，再把 MES 回执写回 OPC UA 变量，形成完整闭环。

| 层次 | 当前实现 | 负责什么 |
|---|---|---|
| 质检数据源 | 虚拟 AI 质检循环 | 产生 OK/NG、缺陷类型、置信度、SN、图片 ID |
| OPC UA Server | `server/qc_server.py`（asyncua） | QCStation 信息模型、工单状态机、Method、Event、审计与权限 |
| 安全通道 | 本地 Root CA + `Basic256Sha256_SignAndEncrypt` | 服务端/客户端证书校验；匿名只读；管理员可调 Method；工单控制与 MES 回执变量均外部只读，由服务端受控写入 |
| 边缘网关 | Node-RED + `node-red-contrib-opcua` + SQLite | 订阅事件、事务去重/入队、单飞行发送、退避重试、死信、MES 调用、指标、健康探针与在线备份 |
| 模拟 MES | Node-RED `http in` 节点 | 模拟 NG 开单、完工报工、回执与故障注入 |
| 硬件扩展 | `hardware/` | 可选的树莓派 5 + open62541 + STM32 现场节点方案 |

一句话理解：**虚拟质检产生数据，OPC UA 定义语义和权限，Node-RED 保证可靠传输，MES 完成工单闭环。**

当前边界：

- 真实实现：OPC UA 信息模型、Method 业务校验、事件订阅、证书与账号权限、Node-RED 队列与重试。
- 仿真实现：AI 视觉推理和 MES HTTP 服务，二者都是零成本替代品。
- 未纳入主链路：树莓派/STM32 硬件版本，仅在 `hardware/` 中提供架构和骨架。

## 启动流程总览

启动顺序固定为：**初始化依赖 → 生成/同步证书 → 启动 OPC UA Server → 启动 Node-RED → 运行验证**。

### 首次初始化（每台机器只做一次）

```bash
cd ~/UA_LinqInspect
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
.venv/bin/python scripts/setup_pki.py
npm install --prefix .node-red --no-audit --no-fund
.venv/bin/python scripts/setup_node_red_security.py
```

安全脚本会生成 `.node-red/settings.js`、`.node-red/.secure-env`（权限 600）和加密的
`.node-red/flows_cred.json`。这些文件均不得提交。启动 Node-RED 前必须先
`source .node-red/.secure-env`。

然后按 [第 2 节](#2-生成本地-ca-证书首次运行或轮换时) 把 CA 证书和 CRL 同步到
Node-RED 的 `node-opcua` PKI 目录。证书不同步时，Node-RED 通常会报
`BadCertificateChainIncomplete` 或 `BadCertificateRevocationUnknown`。

### 日常启动（建议开 3 个终端）

1. **终端 1 启动 OPC UA Server**：配置证书、私钥、信任目录和管理员账号，看到
   `OPC UA Server listening` 即成功。
2. **终端 2 启动 Node-RED**：先 `source .node-red/.secure-env`，再执行
   `.node-red/node_modules/.bin/node-red --userDir "$PWD/.node-red" --port 1880 --no-telemetry`。
   浏览器打开 `http://127.0.0.1:1880`，使用 `NODERED_ADMIN_USER` / `NODERED_ADMIN_PASSWORD` 登录。
3. **终端 3 运行验证**：先跑 `tests/smoke_test.py` 验证 UA 模型，再跑
   `tests/nodered_e2e.py` 验证完整链路，最后跑 `tests/fault_scenarios.py` 验证重试与死信。

Node-RED 默认把事务队列写到 `data/qc-queue.sqlite`；可用 `QC_DB_PATH` 指定其他位置。
OPC UA Server 默认把工位控制状态写到 `data/qc-state.json`；可用 `QC_STATE_PATH` 指定其他位置。
运行指标可访问 `GET /metrics`；SQLite 热备可执行
`.venv/bin/python scripts/backup_queue.py`，默认保留最近 7 份并做完整性校验。

Server 只接受 `Basic256Sha256_SignAndEncrypt`；证书、私钥或信任目录缺失/无效时
会直接拒绝启动，不会回退到 `NoSecurity`。匿名客户端只能读；只有
`UA_ADMIN_USER` / `UA_ADMIN_PASS` 对应的会话可以调用 Method，但 QCStation 业务变量全部只读。若未配置
管理员账号，Server 仍可启动，但所有客户端都只能只读。

完整的证书生成、Server 启动参数、Node-RED 启动命令和测试步骤见下文
[快速开始（Ubuntu / Linux）](#快速开始ubuntu--linux)。

## 文档导航

第一次阅读建议从 [docs/README.md](docs/README.md) 开始，它提供 30 分钟、2 小时和实操验收三条学习路径。核心速查入口：

- [项目全景与架构](docs/00_项目全景与架构.md)
- [信息模型与业务规则速查](docs/01_信息模型与业务规则速查.md)
- [代码与 Node-RED 导读](docs/02_代码与NodeRED导读.md)
- [运行验证与排障手册](docs/03_运行验证与排障手册.md)
- [掌握检查与答辩问答](docs/04_掌握检查与答辩问答.md)
- [工程化优化路线图](docs/05_工程化优化路线图.md)
- [作品集答辩与工厂试点落地](docs/06_作品集答辩与工厂试点落地.md)
- [Ubuntu 迁移与验证报告](docs/07_Ubuntu迁移与验证报告.md)
- [Phase 2A：SQLite 事务队列与崩溃恢复](docs/10_Phase2A_SQLite事务队列与崩溃恢复.md)
- [Phase 2B：可观测性与 SQLite 在线备份](docs/11_Phase2B可观测性与SQLite备份.md)
- [Phase 2C：部署运维与恢复演练](docs/15_Phase2C部署运维与恢复演练.md)

## 项目结构

```
UA_LinqInspect/
├── README.md                  # 本文件：项目入口
├── Phase0_需求拆解与信息模型.md  # 设计基准（NodeId/变量/Method/Event 全部以此为准）
├── server/                    # asyncua OPC UA Server + 虚拟 AI 质检数据源
│   ├── qc_server.py           # M1：Server 主体（信息模型 §2 全量落地）
│   ├── qc_state.py            # 工位控制状态快照、校验与原子持久化
│   └── requirements.txt
├── nodered/
│   ├── flows.json             # M2：Node-RED 流（薄 Function 节点 + 回执写回）
│   ├── sqlite_store.js        # 事务队列、死信、去重、看门狗与旧 context 迁移
│   ├── queue_logic.js         # 入队、发送泵、模拟MES幂等/冲突校验、状态接口与指标
│   └── settings.js            # 注入 qstore/qlogic 的 Node-RED 配置模板
├── scripts/
│   ├── setup_pki.py           # 本地 Root CA、Server/Client 证书与 CRL
│   ├── backup_queue.py        # SQLite 在线备份、完整性校验与保留策略
│   ├── restore_queue.py       # 离线恢复、恢复前快照与原子替换
│   ├── render_deploy.py       # 渲染 systemd/环境文件部署模板
│   ├── setup_node_red_security.py  # 安装 Node-RED 认证、加密凭据和本地 secrets
│   └── run_regression.py      # 隔离启动 UA Server + Node-RED 并执行全部测试
├── deploy/                    # systemd、Prometheus 告警与环境变量模板
├── edge_core/                  # 外部 Edge Core 控制面/OpenAPI 垂直切片
├── hardware/                  # 可选硬件扩展：树莓派 open62541 Server、STM32 客户端方案
├── docs/
│   ├── README.md              # 文档导航与学习路径
│   ├── 00_项目全景与架构.md
│   ├── 01_信息模型与业务规则速查.md
│   ├── 02_代码与NodeRED导读.md
│   ├── 03_运行验证与排障手册.md
│   ├── 04_掌握检查与答辩问答.md
│   ├── 05_工程化优化路线图.md
│   ├── 06_作品集答辩与工厂试点落地.md
│   ├── 07_Ubuntu迁移与验证报告.md
│   ├── 08_Phase0回归与安全测试.md
│   ├── 09_Phase1关联追踪与MES错误分类.md
│   ├── 10_Phase2A_SQLite事务队列与崩溃恢复.md
│   ├── 11_Phase2B可观测性与SQLite备份.md
│   ├── 10_内部经营宪章.md
│   ├── 11_Edge_Core_接口与状态机.md
│   ├── 12_连接器SOW与验收模板.md
│   ├── 13_视觉增值包SOW与验收模板.md
│   ├── 14_投资人BP叙事.md
│   ├── 15_Phase2C部署运维与恢复演练.md
│   ├── 16_状态源重构与持久化恢复.md
│   ├── api/edge-core.openapi.yaml
│   ├── M1_业务口径与MES契约.md  # 缺陷码枚举 / NgRate 滑窗规则 / MES 调用契约
│   └── M2_验收与测试场景.md     # 四种验收场景（正向/超时/5xx死信/Method拒绝）
└── tests/
    ├── ua_test_client.py      # 安全 OPC UA 客户端测试夹具
    ├── state_persistence_test.py # 状态快照校验与恢复单测（5 项）
    ├── outbox_test.py         # 持久化 outbox、重发与 ACK 单测（3 项）
    ├── outbox_capacity_test.py # 背压、分页导出与清空令牌单测（9 项）
    ├── outbox_recovery.py     # outbox SIGKILL 恢复与容量降级（18 项）
    ├── qc_state_restart_test.py  # 工位状态 SIGKILL 重启恢复（11 项）
    ├── security_test.py       # UA 权限、控制变量只读、HTTP 认证与运维接口鉴权（16 项断言）
    ├── edge_core_test.py      # Edge Core 契约、幂等、死信重放与 UA 确认（7 项断言）
    ├── smoke_test.py          # M1 冒烟测试（UA Client 视角，36 项断言）
    ├── mes_idempotency_test.js # 模拟 MES 重复投递与 payload 冲突（10 项断言）
    ├── nodered_e2e.py         # M2 正常链路与 MES 幂等 HTTP 契约（21 项断言）
    ├── metrics_test.py        # Prometheus 指标、outbox、UA 写回与状态一致性（80 项断言）
    ├── health_test.js         # /healthz ready/degraded/unavailable（10 项断言）
    ├── outbox_status_test.js  # OutboxStatus 解析、队列拒收 ACK、健康降级与指标（9 项断言）
    ├── backup_queue_test.py   # WAL 在线备份、完整性、schema 合同与保留策略（23 项断言；活跃库回归时 24）
    ├── restore_queue_test.py  # dry-run、安全快照、损坏库隔离、schema 拒绝与继续消费（32 项断言）
    ├── deploy_templates_test.py # 模板渲染、systemd/Alertmanager/logrotate 与告警（23 项断言）
    ├── fault_scenarios.py     # 可靠性故障矩阵（52 项断言）
    ├── long_outage_recovery.py # MES 长断网、死信重放与实测 RTO（24 项断言）
    ├── soak_test.py           # 周期工单 soak、回执唯一性与资源漂移（20 项断言）
    ├── sqlite_queue_test.js   # SQLite 事务、UA 写回、看门狗与重放单测（19 项断言）
    ├── storage_failure_test.js # SQLite 写满回滚、损坏库失败关闭与恢复后可用性（3 项断言）
    ├── restart_recovery.py    # Node-RED 非空队列重启恢复（seed/verify，18 项断言）
    ├── crash_recovery.py      # SIGKILL 崩溃恢复（prepare/verify，23 项断言）
    └── outage_recovery.py     # 短断网、陈旧事件过滤、UA Server 重启订阅恢复（31 项断言）
```

## 关键设计速览

- **命名空间**：`ns=2`，NamespaceUri `urn:fde:linqinspect:qc`
- **NodeId 风格**：全部 `ns=2;s=<可读名>`（如 `ns=2;s=QCStation.Inspection.NgRate`）
- **模型骨架**：QCStation（工位）→ Inspection / Statistics / Events；22 变量（含 **CurrentPartSN 单件追溯**、拆分后的 MES 回执变量和 outbox 积压状态）、7 Method（StartInspection / CompleteWorkOrder / RecordMesReceipt / AcknowledgeEvent / ExportPendingEvents / PurgeOutbox / ResetCounters）、3 Event（均携带 SerialNumber）
- **可选扩展**（Phase0 §6）：M4 计划将 DeviceStatus 升级为 `QCDeviceStatus` 真枚举（Part 3 §8.42）；进阶讨论 OPC UA for Control + MQTT for Streaming 协同
- **编码硬约束**（详见 Phase0 文档 §2.6）：
  - 自定义 Event 必须继承 `BaseEventType`
  - 服务端口不硬编码，经 `UA_ENDPOINT_PORT` 环境变量配置（默认 4840）
  - Method 先校验前置条件（工单一致性、设备状态），拒绝时返回原因
  - QCStation 业务变量全部只读；`DeviceStatus`、工单号、SN 和三个 MES 回执变量由服务端 Method 或检测入口更新，客户端不能直接 Write
- **交互范式**：只用 OPC UA 的 Variable 读写 / Method 调用 / Event 订阅；模拟 MES 用 Node-RED `http in` 假扮，仅是仿真道具
- **关联追踪**：每次检测/完工生成 `corr-<uuid4>`，经 UA 事件、Node-RED 队列、MES 请求与回执全链路透传
- **失败分类**：4xx 立即死信、429 按 `Retry-After` 退避、5xx/timeout 指数退避、非法 2xx 拒绝写回
- **写回确认**：MES 成功回执不会立即删除队列项；任务进入 UA 写回阶段，只有两个目标变量都写成功才完成，写回失败按退避重试并最终进入死信
- **重启恢复**：一键回归会保留非空队列，停止/重启 Node-RED 或 `SIGKILL` 进程，验证 SQLite 续传、UA 写回确认与回执续写；UA Server 的工单、计数和 SN 序号也会从快照恢复
- **可观测性**：`GET /metrics` 暴露队列深度、死信、去重、入队/投递、UA 写回、看门狗和 UA outbox 积压；计数与队列状态同事务持久化
- **outbox 容量保护**：NG/完工事件积压达到高水位时拒绝新开检，达到硬上限时拒绝新检测与完工，不静默丢弃业务事件；管理员可分页导出并用一次性令牌清空
- **队列容量保护**：SQLite 队列达到 100 条时拒绝新事件且不 ACK，保留 UA outbox 等待重试，不驱逐已有任务；`lastEnqueueRejection`、`qc_enqueue_total{outcome="rejected"}` 与 `QcQueueEnqueueRejected` 告警提供诊断
- **在线备份**：`scripts/backup_queue.py` 使用 SQLite backup API，先校验完整性、外键、必需表和关键列，再原子落盘；空库或错 schema 不会被当成有效备份
- **健康探针**：`GET /healthz` 在 SQLite 不可用时返回 503 `unavailable`；连续 UA 心跳失败达到 3 次或 outbox 进入背压时返回 503 `degraded`
- **部署与恢复**：systemd unit/timer、Prometheus 告警和 `restore_queue.py` 形成可重复的运维路径；恢复前必须停止 Node-RED。损坏目标不会静默覆盖，而是先移入同目录 `qc-queue-corrupt-*.sqlite` 审计隔离文件

## 快速开始（Ubuntu / Linux）

### 1. 创建 Python 环境

```bash
cd ~/UA_LinqInspect
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r server/requirements.txt
python -m pip check
```

### 2. 生成本地 CA 证书（首次运行或轮换时）

本项目使用本地 Root CA 签发 Server 与 Client 证书，证书目录 `certs/` 已被 Git 忽略。
首次运行、或需要轮换证书时执行：

```bash
cd ~/UA_LinqInspect
.venv/bin/python scripts/setup_pki.py
```

脚本会生成：

- Root CA：`certs/ca/ca_key.pem`、`certs/ca/ca_cert.pem`（另有 DER 副本）
- CA 吊销列表：`certs/ca/ca_crl.pem`（另有 DER 副本；Node-RED 的 node-opcua 校验需要）
- Server：`certs/server/server_cert.der`、`certs/server/server_key.pem`
- Client：`certs/client/client_cert.der`、`certs/client/client_key.pem`
- 信任材料：`certs/server/`、`certs/client/`、`certs/trusted_clients/` 下的
  `ca_cert.*`，以及 `certs/server/`、`certs/client/` 下的 `ca_crl.*`

Server 证书的 SAN 包含 `urn:freeopcua:python:server`、`localhost`、本机 hostname 和
`127.0.0.1`。已有证书默认不会被覆盖；确认要重新签发整套证书时使用 `--force`：

```bash
.venv/bin/python scripts/setup_pki.py --force
```

轮换 CA 会使旧 Server/Client 证书全部失效。执行 `--force` 后必须重启 OPC UA Server
和 Node-RED，并确认 Node-RED `ep1` 仍指向 `certs/client/client_cert.der` 与
`certs/client/client_key.pem`。`certs/ca/ca_key.pem` 是 Root CA 私钥，必须保密，
不要提交或复制到客户端机器。

Node-RED 使用的 `node-opcua` 有独立的 PKI 目录，需要把 CA 证书和 CRL 复制进去，
否则会报 `BadCertificateChainIncomplete` 或 `BadCertificateRevocationUnknown`。
Linux 下默认目录为 `${XDG_CONFIG_HOME:-$HOME/.config}/node-red-opcua-nodejs/PKI`：

```bash
cd ~/UA_LinqInspect
NR_PKI="${XDG_CONFIG_HOME:-$HOME/.config}/node-red-opcua-nodejs/PKI"
mkdir -p "$NR_PKI/trusted/certs" "$NR_PKI/trusted/crl" \
         "$NR_PKI/issuers/certs" "$NR_PKI/issuers/crl"
cp certs/server/ca_cert.pem "$NR_PKI/trusted/certs/ua-linqinspect-root-ca.pem"
cp certs/server/ca_cert.pem "$NR_PKI/issuers/certs/ua-linqinspect-root-ca.pem"
cp certs/server/ca_crl.pem  "$NR_PKI/trusted/crl/ua-linqinspect-root-ca-crl.pem"
cp certs/server/ca_crl.pem  "$NR_PKI/issuers/crl/ua-linqinspect-root-ca-crl.pem"
```

如果日志中的 `clientCertificateManager.rootDir` 不是上述路径，以日志打印的实际路径
为准。`certs/trusted_clients/` 只放 CA 证书，不要放 CRL。

Server 启动时会检查自身证书有效期：剩余 30 天以内或已经过期时输出 `WARNING`，
应尽快重新运行脚本并重启两端服务。

### 3. 启动 OPC UA Server

另开一个终端：

```bash
cd ~/UA_LinqInspect
source .node-red/.secure-env
UA_ENDPOINT_PORT=4840 \
UA_SERVER_CERT="$PWD/certs/server/server_cert.der" \
UA_SERVER_PRIVATE_KEY="$PWD/certs/server/server_key.pem" \
UA_TRUSTED_CLIENT_CERTS_DIR="$PWD/certs/trusted_clients" \
UA_ADMIN_USER="$NODERED_OPCUA_USER" \
UA_ADMIN_PASS="$NODERED_OPCUA_PASSWORD" \
QC_AUTO_INSPECT=1 \
QC_NG_PROBABILITY=0.5 \
QC_INSPECT_INTERVAL=1.0 \
.venv/bin/python server/qc_server.py
```

`QC_AUTO_INSPECT=0` 可关闭内置虚拟质检（后续接真实 YOLOv8 时使用）。
Server 只启用 `Basic256Sha256_SignAndEncrypt`；证书、私钥或信任目录缺失/无效时会
拒绝启动，不会回退到 `NoSecurity`。`UA_ADMIN_USER` / `UA_ADMIN_PASS` 是唯一可调用
Method 的身份；匿名客户端即使携带受信任证书也只能只读，管理员也不能绕过 Method
直接改写业务变量。

### 4. 启动 Node-RED

本地 Node-RED 运行时位于 `.node-red/`。首次安装或重建时执行：

```bash
cd ~/UA_LinqInspect
npm install --prefix .node-red --no-audit --no-fund
```

启动后打开 `http://127.0.0.1:1880`，确认当前部署的流与 `nodered/flows.json` 一致：

```bash
cd ~/UA_LinqInspect
source .node-red/.secure-env
.node-red/node_modules/.bin/node-red \
    --userDir "$PWD/.node-red" --port 1880 --no-telemetry
```

Node-RED 管理端和 HTTP 节点均要求认证，默认只监听 `127.0.0.1`。

### 5. 运行验证

推荐直接运行隔离的一键回归；脚本会自动选择端口、生成临时凭据、清理进程并保存日志：

```bash
cd ~/UA_LinqInspect
.venv/bin/python scripts/run_regression.py
```

结果与日志位于 `.tmp/regression-<时间>/`，详细说明见
[docs/08_Phase0回归与安全测试.md](docs/08_Phase0回归与安全测试.md) 和
[docs/09_Phase1关联追踪与MES错误分类.md](docs/09_Phase1关联追踪与MES错误分类.md)。

如果 Server 与 Node-RED 已手工启动，也可以逐个执行；此时必须提供 Server 对应的管理员账号：

```bash
cd ~/UA_LinqInspect
source .node-red/.secure-env
export UA_ENDPOINT_PORT=4840
.venv/bin/python tests/security_test.py
.venv/bin/python tests/smoke_test.py
.venv/bin/python tests/nodered_e2e.py
.venv/bin/python tests/fault_scenarios.py
```

MES 故障注入入口：

```bash
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -X POST http://127.0.0.1:1880/lab/mode/ok
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -X POST http://127.0.0.1:1880/lab/mode/5xx
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -X POST http://127.0.0.1:1880/lab/mode/timeout
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -X POST http://127.0.0.1:1880/lab/mode/4xx
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -X POST http://127.0.0.1:1880/lab/mode/429
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -X POST http://127.0.0.1:1880/lab/mode/invalid2xx
```

测试用新鲜事件注入（受同一 HTTP Basic Auth 保护，仅用于隔离回归/演练）：

```bash
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" -X POST \
  "http://127.0.0.1:1880/lab/fresh-event/ng?workOrderId=WO-DEMO&serialNumber=SN-DEMO"
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" -X POST \
  "http://127.0.0.1:1880/lab/fresh-event/done?workOrderId=WO-DEMO"
```

### 6. 运行 Edge Core 垂直切片

`edge_core/` 是面向外部连接器/控制面的 SQLite Outbox 与状态机垂直切片。Node-RED 在线链路已有独立 SQLite WAL 队列；两者接口和职责仍需收敛。

```bash
export EDGE_API_TOKEN='replace-with-strong-token'
export EDGE_DB_PATH="$PWD/data/edge_core.sqlite3"
export MES_BASE_URL='http://127.0.0.1:1880'
.venv/bin/python -m edge_core.main
```

完整契约见 [docs/11_Edge_Core_接口与状态机.md](docs/11_Edge_Core_接口与状态机.md) 和 [docs/api/edge-core.openapi.yaml](docs/api/edge-core.openapi.yaml)。

## 快速开始（Windows PowerShell）


### 1. 创建隔离 Python 环境

项目已在工作区内创建 conda 前缀环境 `.conda`（Python 3.11.16）。从零重建时执行：

```powershell
cd D:\UA_LinqInspect
conda create --prefix .conda python=3.11 -y
& '.conda\python.exe' -m pip install -r 'server\requirements.txt'
& '.conda\python.exe' -m pip check
```

### 2. 生成本地 CA 证书

```powershell
cd D:\UA_LinqInspect
& '.conda\python.exe' 'scripts\setup_pki.py'
# 轮换整套 CA/Server/Client 证书：
& '.conda\python.exe' 'scripts\setup_pki.py' --force
```

证书路径和轮换注意事项与 Linux 章节相同；轮换后重启 Server 与 Node-RED。

Windows 下 Node-RED 的 PKI 目录由 `APPDATA` 决定（本项目启动脚本会把它指向
`.appdata`），同步 CA 和 CRL：

```powershell
$nrPki = "$PWD\.appdata\node-red-opcua-nodejs\Config\PKI"
New-Item -ItemType Directory -Force `
    "$nrPki\trusted\certs", "$nrPki\trusted\crl", `
    "$nrPki\issuers\certs", "$nrPki\issuers\crl" | Out-Null
Copy-Item 'certs\server\ca_cert.pem' "$nrPki\trusted\certs\ua-linqinspect-root-ca.pem" -Force
Copy-Item 'certs\server\ca_cert.pem' "$nrPki\issuers\certs\ua-linqinspect-root-ca.pem" -Force
Copy-Item 'certs\server\ca_crl.pem'  "$nrPki\trusted\crl\ua-linqinspect-root-ca-crl.pem" -Force
Copy-Item 'certs\server\ca_crl.pem'  "$nrPki\issuers\crl\ua-linqinspect-root-ca-crl.pem" -Force
```

### 3. 启动 OPC UA Server

另开一个 PowerShell 窗口：

```powershell
cd D:\UA_LinqInspect
$env:UA_ENDPOINT_PORT = '4840'
$env:UA_SERVER_CERT = "$PWD\certs\server\server_cert.der"
$env:UA_SERVER_PRIVATE_KEY = "$PWD\certs\server\server_key.pem"
$env:UA_TRUSTED_CLIENT_CERTS_DIR = "$PWD\certs\trusted_clients"
$env:UA_ADMIN_USER = 'admin'
$env:UA_ADMIN_PASS = 'replace-with-a-strong-password'
$env:QC_AUTO_INSPECT = '1'
$env:QC_NG_PROBABILITY = '0.5'
$env:QC_INSPECT_INTERVAL = '1.0'
& '.conda\python.exe' 'server\qc_server.py'
```

`QC_AUTO_INSPECT=0` 可关闭内置虚拟质检（后续接真实 YOLOv8 时使用）。

### 4. 启动 Node-RED

本项目使用工作区内的 Node-RED 运行时（Node-RED 5.0.9 / Node 24.14.0 /
`node-red-contrib-opcua@0.2.355`）。首次安装或重建时执行：

```powershell
cd D:\UA_LinqInspect
$env:APPDATA = "$PWD\.appdata"
$env:LOCALAPPDATA = "$PWD\.localappdata"
npm install --prefix .node-red --no-audit --no-fund
```

启动并把 `nodered/flows.json` 导入 `http://127.0.0.1:1880` 后部署：

```powershell
$env:APPDATA = "$PWD\.appdata"
$env:LOCALAPPDATA = "$PWD\.localappdata"
& '.node-red\node_modules\.bin\node-red.cmd' `
    --userDir "$PWD\.node-red" --port 1880 --no-telemetry
```

> 覆盖 `APPDATA` / `LOCALAPPDATA` 是必需的，用于避开 `node-opcua` 在用户目录创建
> PKI 文件时的权限问题。

### 5. 运行验证

```powershell
cd D:\UA_LinqInspect
$env:UA_ENDPOINT_PORT = '4840'
& '.conda\python.exe' 'tests\smoke_test.py'       # M1：当前 36/36 PASS
& '.conda\python.exe' 'tests\nodered_e2e.py'      # M2：当前 21/21 PASS
& '.conda\python.exe' 'tests\fault_scenarios.py'  # 可靠性：当前 52/52 PASS
```

MES 故障注入入口：

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:1880/inject/inj_ok'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:1880/inject/inj_5xx'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:1880/inject/inj_to'
```

### 6. Node-RED 事件订阅注意点

- 必须显式订阅 `ns=2;s=NgDetectedEventType` 与 `ns=2;s=WorkOrderCompletedEventType`；
  只订阅 BaseEventType（`i=2041`）在当前 Server/contrib 组合下可能收不到自定义字段。
- `node-red-contrib-opcua@0.2.355` 的首条 `action=events` 消息只建立 Subscription，
  流内会在 5 秒后重发同一订阅消息，确保 Event MonitoredItem 真正挂载。
- 心跳每 5 秒同时读取 `DeviceStatus` 和标准 `ServerStatus.StartTime`（`ns=0;i=2257`）。
  UA Server 重启后 StartTime 变化，流会主动向三个客户端广播 `action=reconnect`，
  重建旧 Session 上的事件订阅；仅靠读写心跳恢复不足以证明 Event Subscription 已恢复。
- 心跳同时读取 `Events.OutboxStatus`；有积压时 UA Server 每个重发周期刷新最老事件年龄，Node-RED 据此暴露 `/qc/status`、`/healthz` 和 Prometheus 背压指标。
- 缓冲队列、去重集合、死信队列和运行计数使用 SQLite 事务存储（WAL + `synchronous=FULL`）；去重与入队在同一事务提交，发送泵用原子 `claimNext` 保持单飞行。当前是“至少一次投递 + 接收端幂等”，模拟 MES 已用稳定 payload 指纹验证重复返回原单号、同键不同内容返回 `409 IDEMPOTENCY_CONFLICT`；真实 MES 沙箱仍需验证同一合同，仍不能宣称 exactly-once。
- `GET /metrics` 提供 Prometheus 文本指标；`GET /qc/status` 保持原有 JSON 契约。运维接口当前依赖网络边界保护，生产部署应在反向代理/防火墙限制来源。
- `/metrics` 额外暴露 `qc_database_bytes{kind="main|wal|shm"}`；Prometheus 模板包含 WAL 超过 256 MiB 与文件系统剩余空间低于 10% 的告警。
- UA 写回阶段暴露 `qc_ua_write_pending`、`qc_ua_write_total{type,outcome}` 与 `qc_ua_write_errors_total{type,error_code}`；`/qc/status` 同时返回 `uaWriteback` 计数。
- 在线备份使用 `.venv/bin/python scripts/backup_queue.py`。WAL 模式下不要只复制主 `.sqlite` 文件；脚本会校验 `integrity_check`、`foreign_key_check` 和关键表行数。
- 死信可通过受 HTTP Basic Auth 保护的 `POST /qc/dead-letters/replay` 重放：请求体传 `{"id":"..."}` 精确重放，或传 `{"all":true}` 批量重放；重放复用原队列 ID 和去重键。
- 就绪探针使用 `GET /healthz`；systemd 模板通过 `scripts/render_deploy.py` 渲染；恢复时先停 Node-RED，再用 `scripts/restore_queue.py --dry-run` 验证备份、以 `--force` 替换。完整步骤见 [Phase 2C 运维文档](docs/15_Phase2C部署运维与恢复演练.md)。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 需求拆解 + 信息模型 + 排期 | ✅ 完成 |
| M1 | 业务口径契约表 + asyncua Server 骨架 + 单件追溯 + 幂等开检 | ✅ 完成（冒烟测试 36/36 PASS） |
| M2 | Node-RED 订阅/报警流 + 模拟 MES + SQLite WAL 队列 + 状态接口 | ✅ 完成（正常链路 21/21、故障矩阵 52/52 PASS） |
| Phase 1 | correlationId 贯通 + MES 错误分类 + 重启/断网订阅恢复 | ✅ 完成（当前合并回归 531/531 PASS） |
| Phase 2A | SQLite 事务队列 + 去重/入队原子性 + UA 写回确认 + SIGKILL 崩溃恢复 | ✅ 完成（当前合并回归 531/531 PASS，含 MES 成功、UA 确认前崩溃恢复） |
| Phase 2B | Prometheus 指标 + 事务计数 + SQLite 在线备份与完整性校验 | ✅ 完成（当前合并回归 531/531 PASS） |
| Phase 2C | systemd 部署 + 健康探针 + 告警规则 + 备份 timer + 离线恢复演练 | ✅ 完成（一键回归 531/531 PASS） |
| 状态源 0.1 | 工位控制变量只读、单写者锁、JSON 快照和 SIGKILL 续跑 | ✅ 完成（当前合并回归 531/531 PASS；与投递队列尚未同事务） |
| MES 幂等 0.1 | 模拟 MES payload 指纹、重复回执、冲突拒绝与原型污染防护 | ✅ 完成（单测 10/10、E2E 21/21 PASS；真实 MES 沙箱待对接） |
| 事件 outbox 0.1 | NG/完工事件与状态同快照持久化、稳定事件 ID、周期重发和 ACK | ✅ 完成（事件 outbox 3/3、崩溃恢复 18/18；仍为 at-least-once） |
| outbox 容量 0.1 | 高水位/硬上限、只读积压状态、Prometheus 告警、管理员导出与令牌清空 | ✅ 完成（容量/导出 9/9、状态接口 9/9、积压恢复 18/18） |
| Edge Core 0.2 | 外部控制面 OpenAPI、SQLite 状态机、死信重放和 UA 写回确认接口 | 🟡 7 项测试通过；Node-RED 在线链路使用独立 SQLite 队列 |
| M3 | 连通性/断网重连测试 + IEC 62541 评审 | 🟡 长断网、死信重放、soak、短断网和 Server 重启已自动化；抓包与真实网络切换证据待补 |
| M4 | 最终交付包 + 简历话术 | ⬜ |

> 硬件扩展（open62541 / 树莓派 / STM32）为后置可选项，默认不启动。
