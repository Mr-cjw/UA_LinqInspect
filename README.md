# UA_LinqInspect — AI 质检 OPC UA 原型链路

零成本软件仿真环境下的工业质检原型：**AI 质检（虚拟） → OPC UA Server → Node-RED → 模拟 MES 工单闭环**。

设计与排期依据：[Phase0_需求拆解与信息模型.md](Phase0_需求拆解与信息模型.md)（已冻结，含信息模型表、编码硬约束、里程碑 M1~M4）。

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

## 项目结构

```
UA_LinqInspect/
├── README.md                  # 本文件：项目入口
├── Phase0_需求拆解与信息模型.md  # 设计基准（NodeId/变量/Method/Event 全部以此为准）
├── server/                    # asyncua OPC UA Server + 虚拟 AI 质检数据源
│   ├── qc_server.py           # M1：Server 主体（信息模型 §2 全量落地）
│   └── requirements.txt
├── nodered/
│   └── flows.json             # M2：带状态的边缘节点流（缓冲队列状态机/去重/退避/削峰/心跳）
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
│   ├── M1_业务口径与MES契约.md  # 缺陷码枚举 / NgRate 滑窗规则 / MES 调用契约
│   └── M2_验收与测试场景.md     # 四种验收场景（正向/超时/5xx死信/Method拒绝）
└── tests/
    ├── smoke_test.py          # M1 冒烟测试（UA Client 视角，33 项断言）
    ├── nodered_e2e.py         # M2 正常链路端到端测试（10 项断言）
    └── fault_scenarios.py     # M2/M3 故障场景（5xx、timeout、Method 拒绝）
```

## 关键设计速览

- **命名空间**：`ns=2`，NamespaceUri `urn:fde:linqinspect:qc`
- **NodeId 风格**：全部 `ns=2;s=<可读名>`（如 `ns=2;s=QCStation.Inspection.NgRate`）
- **模型骨架**：QCStation（工位）→ Inspection / Statistics / Events；15 变量（含 **CurrentPartSN 单件追溯** 和拆分后的 MES 回执变量）、3 Method（StartInspection / CompleteWorkOrder / ResetCounters）、3 Event（均携带 SerialNumber）
- **可选扩展**（Phase0 §6）：M4 计划将 DeviceStatus 升级为 `QCDeviceStatus` 真枚举（Part 3 §8.42）；进阶讨论 OPC UA for Control + MQTT for Streaming 协同
- **编码硬约束**（详见 Phase0 文档 §2.6）：
  - 自定义 Event 必须继承 `BaseEventType`
  - 服务端口不硬编码，经 `UA_ENDPOINT_PORT` 环境变量配置（默认 4840）
  - Method 先校验前置条件（工单一致性、设备状态），拒绝时返回原因
  - 只读变量（`LastResult`/`LastDefectType`/`LastConfidence`/`LastImageId`/`LastTimestamp`、`NgRate`、统计项）不提供 Write 能力；三个 MES 回执变量为 ReadWrite
- **交互范式**：只用 OPC UA 的 Variable 读写 / Method 调用 / Event 订阅；模拟 MES 用 Node-RED `http in` 假扮，仅是仿真道具

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
UA_ENDPOINT_PORT=4840 \
UA_SERVER_CERT="$PWD/certs/server/server_cert.der" \
UA_SERVER_PRIVATE_KEY="$PWD/certs/server/server_key.pem" \
UA_TRUSTED_CLIENT_CERTS_DIR="$PWD/certs/trusted_clients" \
UA_ADMIN_USER=admin \
UA_ADMIN_PASS='replace-with-a-strong-password' \
QC_AUTO_INSPECT=1 \
QC_NG_PROBABILITY=0.5 \
QC_INSPECT_INTERVAL=1.0 \
.venv/bin/python server/qc_server.py
```

`QC_AUTO_INSPECT=0` 可关闭内置虚拟质检（后续接真实 YOLOv8 时使用）。
Server 只启用 `Basic256Sha256_SignAndEncrypt`；证书、私钥或信任目录缺失/无效时会
拒绝启动，不会回退到 `NoSecurity`。`UA_ADMIN_USER` / `UA_ADMIN_PASS` 是唯一可写
身份，匿名客户端即使携带受信任证书也只能只读。

### 4. 启动 Node-RED

本地 Node-RED 运行时位于 `.node-red/`。首次安装或重建时执行：

```bash
cd ~/UA_LinqInspect
npm install --prefix .node-red --no-audit --no-fund
```

启动后打开 `http://127.0.0.1:1880`，确认当前部署的流与 `nodered/flows.json` 一致：

```bash
cd ~/UA_LinqInspect
.node-red/node_modules/.bin/node-red \
    --userDir "$PWD/.node-red" --port 1880 --no-telemetry
```

### 5. 运行验证

Server 与 Node-RED 都在运行时，另开终端执行：

```bash
cd ~/UA_LinqInspect
UA_ENDPOINT_PORT=4840 .venv/bin/python tests/smoke_test.py
UA_ENDPOINT_PORT=4840 .venv/bin/python tests/nodered_e2e.py
UA_ENDPOINT_PORT=4840 .venv/bin/python tests/fault_scenarios.py
```

MES 故障注入入口：

```bash
curl -X POST http://127.0.0.1:1880/inject/inj_ok
curl -X POST http://127.0.0.1:1880/inject/inj_5xx
curl -X POST http://127.0.0.1:1880/inject/inj_to
```

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
& '.conda\python.exe' 'tests\smoke_test.py'       # M1：当前 33/33 PASS
& '.conda\python.exe' 'tests\nodered_e2e.py'      # M2：当前 10/10 PASS
& '.conda\python.exe' 'tests\fault_scenarios.py'  # M2/M3：当前 19/19 PASS
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
- 缓冲队列、去重集合和死信队列使用 flow context；`.node-red/settings.js` 已启用 localfilesystem 持久化（`cache:true`、`flushInterval:1`），可降低重启丢失风险，但它是非事务型存储，不能据此宣称 exactly-once。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 需求拆解 + 信息模型 + 排期 | ✅ 完成 |
| M1 | 业务口径契约表 + asyncua Server 骨架 + 单件追溯 + 幂等开检 | ✅ 完成（冒烟测试 33/33 PASS） |
| M2 | Node-RED 订阅/报警流 + 模拟 MES + 持久化 context + 状态接口 | ✅ 完成（正常链路 10/10、故障场景 19/19 PASS） |
| M3 | 连通性/断网重连测试 + IEC 62541 评审 | 🟡 订阅/心跳/陈旧过滤已实现；断网重连与抓包证据待验收 |
| M4 | 最终交付包 + 简历话术 | ⬜ |

> 硬件扩展（open62541 / 树莓派 / STM32）为后置可选项，默认不启动。
