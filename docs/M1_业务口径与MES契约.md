# M1 交付：质检业务口径与 MES 消费契约

> 版本：v1.0 ｜ 2026-11-08 ｜ 信息模型基准：[Phase0_需求拆解与信息模型.md](../Phase0_需求拆解与信息模型.md)

## 1. 缺陷码枚举集合（M1 强制产出 ①）

| 缺陷码 | 中文含义 | 典型来源 | 默认 Severity | 备注 |
|---|---|---|---|---|
| SCRATCH | 划痕 | 搬运/装配摩擦 | 3 | 最常见 NG 类型，仿真中权重最高 |
| DENT | 凹陷 | 碰撞、模具压伤 | 3 | |
| STAIN | 污渍 | 油污、水渍 | 2 | 部分可返工，MES 侧可走返修单 |
| BURR | 毛刺 | 机加工残留 | 3 | |
| MISSING | 缺件 | 装配漏装 | 4 | 最严重，建议直接锁批 |

**编码规则**：全大写英文单词， snake_case 不使用；扩展缺陷码只需在 Server 的 `DEFECT_CODES` 常量与本文档同步追加，UA 侧 `LastDefectType` 为 String，无需改模型。
**空串语义**：`LastDefectType=""` 且 `LastResult=True` 表示 OK；禁止出现 `LastResult=True` 但缺陷码非空的不一致状态。
**单件追溯（SN）**：每次检测必须绑定唯一 SN（`CurrentPartSN` 变量 + 事件 `SerialNumber` 字段），仿真数据源按 `SN-<yyyyMMdd>-<seq>` 生成；MES 开单请求体必须携带 serialNumber，实现 imageId → SN → 工单 三键关联追溯。

## 2. NgRate 滑动窗口规则（M1 强制产出 ②）

| 项 | 规则 |
|---|---|
| 窗口类型 | 按样本数滑动（非时间窗），FIFO 队列 |
| 窗口大小 | 默认 **20** 个样本，经 `QC_NG_WINDOW` 环境变量配置 |
| 计算方式 | `NgRate = 窗口内 NG 样本数 / 窗口内样本总数`，保留 3 位小数 |
| **空窗口保护** | 窗口内无任何样本（冷启动或 ResetCounters 后）时 **NgRate = -1.0**，与 0.0 明确区分，避免"无数据"被误读为"零不良"造成假阴性报警 |
| 消费方阈值约定 | Node-RED 报警分支：`NgRate >= 0.30` 且 `NgRate != -1.0` 时触发 NG 率报警；-1.0 一律跳过 |
| 与累计统计的关系 | `NgCount/InspectCount` 是**累计口径**（整班次），NgRate 是**窗口口径**（近期趋势），两者不得混用 |

**实现锚点**：`server/qc_server.py` 中 `QCStation._ng_rate()` 与 `record_inspection()`。

## 3. MES 调用契约细节（M1 强制产出 ③）

模拟 MES 由 Node-RED `http in` 节点假扮（仿真道具，非架构范式）。角色分工：**Node-RED 是 UA 客户端 + MES 前端**，模拟 MES 是 HTTP 接收端。

### 3.1 接口清单（M2 实现）

| # | 接口 | 方法与路径 | 触发时机 | 请求体字段 |
|---|---|---|---|---|
| 1 | NG 开单 | `POST /mes/ng-order` | Node-RED 订阅到 `NgDetectedEvent` | workOrderId, **serialNumber**, defectType, confidence, imageId, occurredAt(ISO8601) |
| 2 | 完工报工 | `POST /mes/complete` | Node-RED 订阅到 `WorkOrderCompletedEvent` | workOrderId, inspectCount, okCount, ngCount, ngRate |
| 3 | 开单回执 | `POST` 回 Node-RED 由模拟MES返回 | 接口1 同步响应 | `{ "mesOrderNo": "MES-<yyyyMMdd>-<seq>", "accepted": true }`；重复 SN 返回原单号 + `duplicated:true`（MES 侧防重第二道防线） |
| 4 | 报工回执 | 同上 | 接口2 同步响应 | `{ "accepted": true, "receiptNo": "RC-<...>" }`；重复工单号同理防重 |

**回执写回目标（M2 红线 4 修订）**：MES 回执写入 UA 变量 **`LastMesOrderNo`**（新增的独立 ReadWrite 变量）。原设计"写回 CurrentWorkOrderId"会覆盖派工单号、破坏工单状态机——派工单号（我方经 StartInspection 下发）与 MES 回执号（对方受理凭证）语义分离，禁止混写。

### 3.2 时序约定

```
UA Server                    Node-RED                     模拟MES
   │ NgDetectedEvent            │                            │
   │──────────────────────────>│ POST /mes/ng-order         │
   │                            │───────────────────────────>│ 生成工单
   │                            │<───────────────────────────│ 回执 mesOrderNo
   │ 写回回执相关 Variable        │                            │
   │<───────────────────────────│                            │
   │ WorkOrderCompletedEvent    │                            │
   │──────────────────────────>│ POST /mes/complete         │
   │                            │───────────────────────────>│ 报工归档
   │                            │<───────────────────────────│ 回执 receiptNo
   │<───────────────────────────│ 写回 receiptNo              │
```

### 3.3 错误处理约定

| 场景 | 约定 |
|---|---|
| 模拟MES 5xx / 超时(>3s) | Node-RED 重试 3 次，间隔 1s/2s/4s（指数退避）；仍失败则流转 **FAILED（死信）** 并在 debug 亮红灯，不无限重试阻塞队列 |
| 缓冲状态机 | 单向闭环 `PENDING → SENDING → SUCCESS(出队) / PENDING(退避等待) / FAILED(死信)`；**单飞行发送**（同一时刻仅一条在途），规避并发写回与未来 SQLite 的 `SQLITE_BUSY` |
| 防重 | 双侧防线：Node-RED 按 `serialNumber`（NG单）/`workOrderId`（报工）指纹去重；MES 侧同指纹重复请求返回原单号+`duplicated:true`——**绝不重复生成两张 NG 工单** |
| 回执 accepted=false | Node-RED 打 warning 日志并亮 debug 节点，不重试 |
| UA Method 拒绝（accepted=False） | summary 中原因必须打印（debug error 级），**阻断后续 MES 报工动作**，绝不带病继续 |
| 完工报工触发源 | **唯一发令枪是 UA Server 的 WorkOrderCompletedEvent**，Node-RED 禁止自行推断"数量够了就报工" |
| 时间戳 | 报文 occurredAt 一律 ISO8601 UTC（`new Date().toISOString()`，带 Z 后缀），规避时区偏移 |
| 告警疲劳 | NG 报警设 10s 抑制窗：窗内合并计数、超窗一次性亮灯并携带合并条数（具备告警抑制机制） |
| 陈旧事件 | 断网恢复后 age>60s 的事件直接丢弃（防 overflow 洪峰）；心跳每 5s 读 DeviceStatus，连续 3 次异常判定连接假死并告警 |
| 空窗口 NgRate=-1.0 | 完工报工时按 3.2 传 -1.0，MES 侧显示"样本不足"，不算异常 |
| **MES 不可达（断网）** | NG 开单/报工请求进入**边缘缓冲队列**（Node-RED 内部 queue，状态机见上），恢复后按 FIFO 补传；缓冲上限 100 条，溢出丢弃最旧并告警（M2 实现，M3 断网重连测试验证；SQLite 持久化见 Phase0 §6-5） |

### 3.4 M2 验收口径

触发一次 NG → 模拟MES 侧生成一条含缺陷码/imageId 的工单 → 回执 mesOrderNo 出现在 Node-RED debug → CompleteWorkOrder 后 MES 收到汇总且回执写回 UA。链路任一环断开即 M2 不验收。
