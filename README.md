# UA_LinqInspect — AI 质检 OPC UA 原型链路

零成本软件仿真环境下的工业质检原型：**AI 质检（虚拟） → OPC UA Server → Node-RED → 模拟 MES 工单闭环**。

设计与排期依据：[Phase0_需求拆解与信息模型.md](Phase0_需求拆解与信息模型.md)（已冻结，含信息模型表、编码硬约束、里程碑 M1~M4）。

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
│   ├── M1_业务口径与MES契约.md  # 缺陷码枚举 / NgRate 滑窗规则 / MES 调用契约
│   └── M2_验收与测试场景.md     # 四种验收场景（正向/超时/5xx死信/Method拒绝）
└── tests/
    ├── smoke_test.py          # M1 冒烟测试（UA Client 视角，31 项断言）
    └── nodered_e2e.py         # M2 端到端测试（UA 事件 → Node-RED → MES → 回执写回）
```

## 关键设计速览

- **命名空间**：`ns=2`，NamespaceUri `urn:fde:linqinspect:qc`
- **NodeId 风格**：全部 `ns=2;s=<可读名>`（如 `ns=2;s=QCStation.Inspection.NgRate`）
- **模型骨架**：QCStation（工位）→ Inspection / Statistics / Events；12 变量（含 **CurrentPartSN 单件追溯**）、3 Method（StartInspection / CompleteWorkOrder / ResetCounters）、3 Event（均携带 SerialNumber）
- **可选扩展**（Phase0 §6）：M4 计划将 DeviceStatus 升级为 `QCDeviceStatus` 真枚举（Part 3 §8.42）；进阶讨论 OPC UA for Control + MQTT for Streaming 协同
- **编码硬约束**（详见 Phase0 文档 §2.6）：
  - 自定义 Event 必须继承 `BaseEventType`
  - 服务端口不硬编码，经 `UA_ENDPOINT_PORT` 环境变量配置（默认 4840）
  - Method 先校验前置条件（工单一致性、设备状态），拒绝时返回原因
  - ReadOnly 变量（Last*/NgRate/统计项）不提供 Write 能力
- **交互范式**：只用 OPC UA 的 Variable 读写 / Method 调用 / Event 订阅；模拟 MES 用 Node-RED `http in` 假扮，仅是仿真道具

## 快速开始（Windows PowerShell）

### 1. 创建隔离 Python 环境

项目已在工作区内创建 conda 前缀环境 `.conda`（Python 3.11.16）。从零重建时执行：

```powershell
cd D:\UA_LinqInspect
conda create --prefix .conda python=3.11 -y
& '.conda\python.exe' -m pip install -r 'server\requirements.txt'
& '.conda\python.exe' -m pip check
```

### 2. 启动 OPC UA Server

另开一个 PowerShell 窗口：

```powershell
cd D:\UA_LinqInspect
$env:UA_ENDPOINT_PORT = '4840'
$env:QC_AUTO_INSPECT = '1'
$env:QC_NG_PROBABILITY = '0.5'
$env:QC_INSPECT_INTERVAL = '1.0'
& '.conda\python.exe' 'server\qc_server.py'
```

`QC_AUTO_INSPECT=0` 可关闭内置虚拟质检（后续接真实 YOLOv8 时使用）。

### 3. 启动 Node-RED

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

### 4. 运行验证

```powershell
cd D:\UA_LinqInspect
$env:UA_ENDPOINT_PORT = '4840'
& '.conda\python.exe' 'tests\smoke_test.py'    # M1：当前 31/31 PASS
& '.conda\python.exe' 'tests\nodered_e2e.py'   # M2：当前 8/8 PASS
```

MES 故障注入入口：

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:1880/inject/inj_ok'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:1880/inject/inj_5xx'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:1880/inject/inj_to'
```

### 5. Node-RED 事件订阅注意点

- 必须显式订阅 `ns=2;s=NgDetectedEventType` 与 `ns=2;s=WorkOrderCompletedEventType`；
  只订阅 BaseEventType（`i=2041`）在当前 Server/contrib 组合下可能收不到自定义字段。
- `node-red-contrib-opcua@0.2.355` 的首条 `action=events` 消息只建立 Subscription，
  流内会在 5 秒后重发同一订阅消息，确保 Event MonitoredItem 真正挂载。
- 内存缓冲队列、去重集合和死信队列使用 flow context；Node-RED 重启后会清空。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 需求拆解 + 信息模型 + 排期 | ✅ 完成 |
| M1 | 业务口径契约表 + asyncua Server 骨架 + 单件追溯 + 幂等开检 | ✅ 完成（冒烟测试 31/31 PASS） |
| M2 | Node-RED 订阅/报警流 + 模拟 MES | ✅ 正常链路 8/8 PASS；5xx/超时/死信场景已实现并完成故障注入验证 |
| M3 | 连通性/断网重连测试 + IEC 62541 评审 | ⬜ |
| M4 | 最终交付包 + 简历话术 | ⬜ |

> 硬件扩展（open62541 / 树莓派 / STM32）为后置可选项，默认不启动。
