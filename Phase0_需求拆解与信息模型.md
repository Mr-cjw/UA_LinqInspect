# Phase 0 交付：AI质检 → OPC UA → Node-RED → 模拟MES 工单闭环原型

> 主理人：乔贯通（FDE方案架构师）｜ 日期：2026-11-08 ｜ 环境：零成本软件仿真（WSL/本机，无硬件投入）
>
> 文档导航：[docs/README.md](docs/README.md)｜全景：[00_项目全景与架构.md](docs/00_项目全景与架构.md)｜速查：[01_信息模型与业务规则速查.md](docs/01_信息模型与业务规则速查.md)

---

## 1. 需求与方案摘要

**一句话定位**：在零成本软件仿真环境下，用 asyncua 搭 OPC UA Server 承载 AI 质检（YOLOv8 推理结果仿真）数据，Node-RED 订阅质检事件并按规则分支报警，回调模拟 MES 完成工单闭环，回执写回 OPC UA——完整复现工厂质检工单的 OT-IT 链路。

**目标链路（唯一，禁止发散）**：

```
[虚拟AI质检] ──写入──> [asyncua OPC UA Server] ──订阅/事件──> [Node-RED]
                                                            │  ├─ NG分支：报警
                                                            │  ├─ 边缘缓冲（Node-RED flow context / SQLite）：
                                                            │  │    MES 不可达时暂存待补传
                                                            │  └─ 回调 ──> [模拟MES (http in 假扮)]
                                                            │                    │ 工单回执
                                                            └──────── 写回 UA <──┘
```

> **边缘缓冲设计**（支撑 M3 断网重连测试）：缓冲组件位于 Node-RED 与模拟 MES 之间——这是链路中唯一的跨进程 HTTP 断点，也是现场网络不稳定时最可能失败的位置。网络中断时，NG 开单/报工请求先进缓冲队列（M2 用 Node-RED flow context 实现，并由 localfilesystem 近实时落盘；事务型 SQLite 队列见 §6-5），恢复后按 FIFO 补传。
>
> **UA↔Node-RED 断网恢复机制（精确化，不做乐观的"自愈"表述）**：
> - **短时断网（秒级）**：客户端应在新 SecureChannel 上 **ActivateSession 原会话**（而非新建会话），Subscription/MonitoredItem 才能继续存活、无需重建即恢复推送；`transferSubscriptions()` 列为进阶验证项。
> - **长时断网 / Server 重启**：订阅可能因 lifetime 超时或会话丢失被服务端删除，且 MonitoredItem 通知队列溢出会丢弃旧值（overflow 标记数据缺口）。此时**可接受行为**是：Node-RED 能检测到订阅失效并主动重建，且不会对断网期间溢出的陈旧事件误处理。
> - 上述两个子场景分别对应 M3 的两条测试用例，各有独立验收预期。

**关键设计决策**：

| 决策点 | 结论 | 依据 |
|---|---|---|
| 交互范式 | 只用 OPC UA 的 Variable 读写、Method 调用、Event 通知、Subscription/MonitoredItem 订阅 | IEC 62541 Part 4 §5（Services）；禁用 Web 后端思路 |
| 模拟MES | Node-RED `http in` 节点假扮 MES 接收端，是仿真道具而非架构范式 | 全局规则 5 |
| AI 数据源 | 阶段一用虚拟传感器脚本模拟 YOLOv8 推理输出（OK/NG、缺陷类型、置信度、图像ID），YOLOv8 真实对接作为可切换项 | 全局规则 2（零成本优先） |
| NodeId 风格 | 自定义命名空间用 String NodeId（`ns=2;s=...`），比数字型易读易教 | IEC 62541 Part 3 §5.2（NodeId 两种形式均可） |
| 历史数据 | 本期不启用 UA Historical Access，InfluxDB/SQLite 存储列为可选扩展 | IEC 62541 Part 11（本期不涉及） |

---

## 2. OPC UA 信息模型初稿

命名空间：`ns=2`，NamespaceUri：`urn:fde:linqinspect:qc`（`ns=0` 标准命名空间、`ns=1` Server 命名空间由服务器自带）。

依据：IEC 62541 Part 3（Address Space，对象层级与引用类型）、Part 5（Information Model，BaseEventType / BaseDataVariableType）。

### 2.1 Object 层级

| NodeId | BrowseName | NodeClass | TypeDefinition | 语义说明 |
|---|---|---|---|---|
| `ns=2;s=QCStation` | 1:AI质检工位 (QCStation) | Object | BaseObjectType | 链路根对象，下挂全部质检功能 |
| `ns=2;s=QCStation.Inspection` | 1:检测单元 (Inspection) | Object | BaseObjectType | 当前检测状态与最近一次结果 |
| `ns=2;s=QCStation.Statistics` | 1:统计 (Statistics) | Object | BaseObjectType | 班次累计统计 |
| `ns=2;s=QCStation.Events` | 1:事件源 (Events) | Object | BaseObjectType | 事件的 Notifier 挂载点 |

> BrowseName 与 NodeId **统一使用 ns=2**：本原型不引入标准 Property（如 EngineeringUnits 等需借用标准命名空间的场景），二者同用 ns=2 可保证 UaExpert 浏览时命名空间前缀一致、模型自洽（Part 3 §8.2）。

### 2.2 Variable 清单

| NodeId | BrowseName | NodeClass | DataType | 读写权限 | 语义说明 |
|---|---|---|---|---|---|
| `...s=QCStation.DeviceStatus` | DeviceStatus | Variable | Int32 | ReadWrite | 数字枚举含义：**0=Idle（空闲）、1=Running（运行中）、2=Fault（故障）**。工程化做法是定义自定义 EnumDataType（Part 3 §8.42），教学期以 Int32 + 注释简化，后续可升级 |
| `...s=QCStation.CurrentWorkOrderId` | CurrentWorkOrderId | Variable | String | ReadWrite | 当前工单号，Node-RED/MES 写入 |
| `...s=QCStation.CurrentPartSN` | CurrentPartSN | Variable | String | ReadWrite | **单件序列号（Part Traceability）**：本次检测产品的唯一 SN，由数据源/Node-RED 写入，是跨系统质量追溯的基石（关联 imageId → SN → 工单） |
| `...s=QCStation.LastMesOrderNo` | LastMesOrderNo | Variable | String | ReadWrite | **兼容镜像**：最近一次 MES 回执号，便于旧客户端继续读取；新逻辑不得依赖它区分开单与报工。绝不写回 CurrentWorkOrderId |
| `...s=QCStation.LastNgMesOrderNo` | LastNgMesOrderNo | Variable | String | ReadWrite | **NG 开单回执落地**：最近一次 `mesOrderNo`（`MES-*`），与完工报工回执语义分离 |
| `...s=QCStation.LastCompletionReceiptNo` | LastCompletionReceiptNo | Variable | String | ReadWrite | **完工报工回执落地**：最近一次 `receiptNo`（`RC-*`），不被后续 NG 开单覆盖 |
| `...s=QCStation.Inspection.LastResult` | LastResult | Variable | Boolean | ReadOnly | 最近一次判定：True=OK，False=NG |
| `...s=QCStation.Inspection.LastDefectType` | LastDefectType | Variable | String | ReadOnly | 缺陷码（OK 时为空串），如 SCRATCH/DENT/STAIN |
| `...s=QCStation.Inspection.LastConfidence` | LastConfidence | Variable | Double | ReadOnly | 置信度 0.0~1.0 |
| `...s=QCStation.Inspection.LastImageId` | LastImageId | Variable | String | ReadOnly | 图像/样本编号（追溯键，仿真阶段为模拟ID） |
| `...s=QCStation.Inspection.LastTimestamp` | LastTimestamp | Variable | DateTime | ReadOnly | 检测时间戳（UTC） |
| `...s=QCStation.Inspection.NgRate` | NgRate | Variable | Double | ReadOnly | 滑动窗口 NG 率（0.0~1.0），报警阈值判定依据 |
| `...s=QCStation.Statistics.InspectCount` | InspectCount | Variable | UInt32 | ReadOnly | 累计检测数 |
| `...s=QCStation.Statistics.OkCount` | OkCount | Variable | UInt32 | ReadOnly | 累计 OK 数 |
| `...s=QCStation.Statistics.NgCount` | NgCount | Variable | UInt32 | ReadOnly | 累计 NG 数 |

### 2.3 Method 清单（IEC 62541 Part 4 §5.10 Call Service）

| NodeId | BrowseName | NodeClass | 输入参数 | 输出参数 | 语义说明 |
|---|---|---|---|---|---|
| `...s=QCStation.StartInspection` | StartInspection | Method | workOrderId:String, targetCount:UInt32 | accepted:Boolean, summary:String | 开检：绑定工单号与目标数量，状态切 Running。**幂等/状态校验**：DeviceStatus=Running 且 CurrentWorkOrderId 与入参一致 → accepted=True 且 summary="already running"（幂等接受，状态不破坏）；工单不一致 → accepted=False 及原因（当前工单不被覆盖）；DeviceStatus=Fault → accepted=False 及原因 |
| `...s=QCStation.CompleteWorkOrder` | CompleteWorkOrder | Method | workOrderId:String | accepted:Boolean, summary:String | 完工报工：**内部校验入参 workOrderId 与 CurrentWorkOrderId 一致性**，不一致返回 accepted=False；无活动工单（CurrentWorkOrderId 为空）或 DeviceStatus≠Running 时同样拒绝，summary 返回拒绝原因；成功时触发 WorkOrderCompleted 事件 |
| `...s=QCStation.ResetCounters` | ResetCounters | Method | （无） | （无） | 班次清零 |

### 2.4 Event 清单（IEC 62541 Part 5 §6.4，事件字段继承 BaseEventType 的 Message/Timestamp/SourceNode/SourceName/Severity）

| Event NodeId | BrowseName | 触发条件 | 自定义字段（Property） | 消费方 |
|---|---|---|---|---|
| `...s=QCStation.Events.InspectionFinishedEvent` | InspectionFinishedEvent | 每完成一次检测 | workOrderId, **serialNumber**, result(OK/NG), defectType, confidence, imageId | Node-RED 订阅（UA Event 节点） |
| `...s=QCStation.Events.NgDetectedEvent` | NgDetectedEvent | result=NG 时 | workOrderId, **serialNumber**, defectType, confidence, imageId | Node-RED NG 分支 → 报警 + MES 工单 |
| `...s=QCStation.Events.WorkOrderCompletedEvent` | WorkOrderCompletedEvent | CompleteWorkOrder 被调用 | workOrderId, inspectCount, okCount, ngCount, ngRate | Node-RED → MES 报工回执 |

> 教学简化：NG 报警走"订阅 NgDetectedEvent → Node-RED 分支"实现，不另建 UA Alarm & Condition（Part 5 §9）对象；该扩展列为 M4 后可选进阶。

### 2.5 MES 消费 UA 的调用契约（初稿，待郝讲明在 M1 确认业务口径）

| 动作 | UA 通道 | 方向 |
|---|---|---|
| 下发/绑定工单 | 调用 `StartInspection` Method | MES侧(Node-RED) → UA Server |
| 读取实时判定 | 订阅 Inspection 变量 / 事件 | Node-RED ← UA Server |
| NG 工单生成 | Node-RED 订阅 NgDetectedEvent 后 POST 模拟MES | Node-RED → 模拟MES |
| 完工报工 | 调用 `CompleteWorkOrder`，模拟MES 回执后 Node-RED 将 `receiptNo` 写入 `LastCompletionReceiptNo`，并同步 `LastMesOrderNo` 兼容镜像（**不覆盖** `CurrentWorkOrderId`） | 双向闭环 |

### 2.6 编码硬约束（全体成员遵守，评审依据）

1. **所有自定义 Event 必须继承 `BaseEventType`**（IEC 62541 Part 5 §6.4），自定义字段以 Property 形式挂载，禁止凭空定义游离于类型体系之外的事件。
2. **服务端口不得硬编码**：通过环境变量（如 `UA_ENDPOINT_PORT`，默认 4840）或命令行参数注入；Endpoint URL 随配置生效。这同时保证 M3 阶段 GetEndpoints 发现机制验证（Part 4 §5.4）不受写死端口干扰。
3. **Method 必须做业务校验**：CompleteWorkOrder 的一致性/状态校验模式适用于所有后续新增 Method——先校验前置条件，再执行业务，拒绝时给出原因。
4. NodeId 一律使用 `ns=2;s=<可读名>` 的 String 形式；**BrowseName 命名空间索引同为 2**（本原型不引入标准 Property，二者同用 ns=2 保持浏览一致性）。
5. 变量读写权限按表执行，ReadOnly 变量不得提供 Write 能力（Part 4 §5.8）。

---

## 3. 需求-任务拆解表

| 需求编号 | 需求 | 落地物 | 责任成员 | 阶段 |
|---|---|---|---|---|
| R1 | AI 质检数据源仿真 | 虚拟检测脚本：按可配比例产生 OK/NG、缺陷码、置信度、imageId、**单件 SN**，写入 UA；预留 YOLOv8 真实推理切换点 | 苏跑通 | M1 |
| R2 | OPC UA Server 信息模型落地 | asyncua Server：按上表建 Object/Variable/Method/Event，端口经 `UA_ENDPOINT_PORT` 配置（默认 4840） | 苏跑通（模型由主理人设计） | M1 |
| R3 | 质检工单业务口径 | **必须产出三项**：① 缺陷码枚举集合（含编码规则，如 SCRATCH/DENT/STAIN…）；② NgRate 滑动窗口规则（**窗口大小、计算方式、空窗口保护策略**——窗口内无样本时 NgRate 的返回语义，避免 0 值假阴性）；③ MES 调用契约细节（字段、时序、错误处理） | 郝讲明 | M1 |
| R4 | 订阅与逻辑编排 | Node-RED 流：订阅变量+事件 → NG/完工分支 → 报警 → 回调模拟MES → 回执写回 UA；**含边缘缓冲（MES 不可达暂存/补传）** | 连得通 | M2 |
| R5 | 模拟 MES 接收端 | `http in` 假扮 MES：收 NG 单→生成工单→回执 | 连得通（口径依 R3） | M2 |
| R6 | 测试与评审 | 测试用例（连通/订阅/断网重连/状态码/**负面用例：RO写入拒绝、错误工单、Idle 完工**）+ Wireshark 抓包要点（**CreateMonitoredItems + Publish 报文流**）+ 代码评审 | 查分明 | M3 |
| R7 | 交付汇编与简历话术 | 最终交付包五件套 | 主理人（乔贯通） | M4 |
| R8 | （可选）硬件扩展 | open62541 裁剪/树莓派/STM32 移植——**仅在用户明确提出时启动** | 崔裁峰 | 后置 |

## 4. 里程碑排期

| 里程碑 | 交付物 | 前置依赖 | 责任成员 | 验收标准 |
|---|---|---|---|---|
| **M1** 业务口径 + UA Server 骨架（并行） | ① MES 消费契约表 ② asyncua Server 可运行代码 + requirements.txt | Phase 0 信息模型初稿（本文件） | 郝讲明 ＋ 苏跑通 | UaExpert 连上服务端口能读到全部变量、订阅到事件；**契约表必须含缺陷码枚举集合、NgRate 滑动窗口规则（含空窗口保护）、MES 调用契约细节三项，缺一不验收** |
| **M2** Node-RED 流 + 模拟 MES | 可导入 flows.json（带状态的边缘节点，非透传管道）：事件解析/陈旧过滤 → 报警削峰 → 去重 → **缓冲队列状态机**（PENDING→SENDING→SUCCESS/FAILED，单飞行）→ 3次指数退避1s/2s/4s → 死信 → 按类型写回 `LastNgMesOrderNo` / `LastCompletionReceiptNo`，并同步兼容镜像；模拟MES 含故障注入（ok/5xx/timeout）+ 侧防重 | M1 确认后的节点名与事件字段 | 连得通 | **场景①~③自动化验证；④服务端拒绝自动化、流级日志人工验收**：①正常 NG 流（回执写回 `LastNgMesOrderNo`）②MES 超时流（3 次退避后成功）③MES 5xx 流（转 FAILED 死信 + 红灯）④UA Method 拒绝流（原因打印且阻断报工）；报文与 M1 契约 §3.1 字段完全一致，occurredAt 为 ISO8601 UTC；报工仅由 WorkOrderCompletedEvent 驱动 |
| **M3** 测试验证 + 评审 | 测试用例集 + 抓包要点 + 评审意见 | M2 完整链路 | 查分明 | 连通性/异常状态码/负面用例（RO 写入、错误工单、Idle 完工、**重复开检幂等**）全部有结论；**断网重连拆两子场景验收**：① 短时断网——ActivateSession 原会话恢复、订阅不重建即续推；② 长时断网/Server 重启——检测订阅失效并主动重建、不误处理 overflow 陈旧事件（transferSubscriptions 为进阶项）；**抓包重点：CreateMonitoredItems 与 Publish 请求-响应流（Part 4 §5.12~5.13）** |
| **M4** 汇编交付包 | 交付包五件套 + FDE 简历话术 | M1~M3 | 主理人 | 文档齐备、链路可复现、简历话术含量化结果与追问预判 |
| （后置）M5 硬件扩展 | open62541 裁剪配置、移植文档 | 用户明确提出 | 崔裁峰 | 默认不启动 |

## 5. IEC 62541 规范引用索引

- **Part 3**（Address Space）：对象层级组织、引用类型、NodeId/BrowseName 命名空间（§5.2、§8.2）
- **Part 4**（Services）：Read/Write（§5.7~5.8）、Call（Method 调用，§5.10）、CreateSubscription/MonitoredItem（§5.12~5.13）
- **Part 5**（Information Model）：BaseDataVariableType、BaseEventType（§6.4）、Method 语义
- **Part 6**（Mappings）：opc.tcp 传输与编码（测试阶段 Wireshark 解析依据）
- **Part 11**（Historical Access）：本期不启用，列为可选扩展

## 6. 可选扩展（评审采纳，按阶段排期）

1. **DeviceStatus 升级为真枚举类型（M4 阶段）**：将 `Int32 + 注释` 升级为自定义 `QCDeviceStatus` EnumDataType（IEC 62541 Part 3 §8.42），定义 Idle/Running/Fault 枚举值。升级后 UaExpert 中变量自描述、可显示枚举字符串，信息模型规范性显著提升。当前 Int32 实现保持兼容，升级时仅改 DataType 绑定。
2. **OPC UA + MQTT 协同模式（M4 后进阶）**："OPC UA for Control, MQTT for Streaming" 是当前工业架构常见实践——OPC UA 负责语义化信息模型、工单控制与事件通知；高频原始检测数据流（如每件产品的全量推理输出）可经 MQTT（Sparkplug B 或自定义 Topic）传输至云侧/数据分析平台。本期原型链路保持纯 OPC UA，不引入 MQTT 组件。
3. **UA Alarm & Condition 对象（Part 5 §9）**：将 NG 报警从"事件分支"升级为规范告警模型，支持确认（Acknowledge）/退避（Shelving）。
4. **Historical Access（Part 11）**：为统计变量开启历史存档，支撑 MES 侧历史查询。
5. **边缘缓冲持久化（M3 可选）**：将 Node-RED flow context 队列替换为 SQLite 本地缓冲（`node-red-node-sqlite`），写入即落盘、进程重启不丢，恢复后按 FIFO 补传，并基于 `serialNumber + occurredAt`（NG单）/`workOrderId`（报工）做唯一约束防重；flow context 仅保留为轻量 fallback。并发控制：**单飞行发送泵**（同一时刻仅一条请求在途）+ 集中写库，规避 `SQLITE_BUSY`。
6. **MCP 语义工具层（M4 后独立 Demo）**：基于 asyncua 生态（OPCUA4MCP 思路：asyncua + FastMCP）封装质检专用语义工具 `start_work_order` / `complete_work_order` / `get_inspection_summary` / `list_recent_ng_events`——不重新发明通用 OPC UA MCP，工具内部复用既有 Method 校验逻辑（AI Agent 调用同样会遇到工单不一致/状态校验拒绝，安全边界不放宽）；配合 Skill 沉淀"质检工单闭环"SOP，构成可演示的质检 Agent 原型，初期只读。
