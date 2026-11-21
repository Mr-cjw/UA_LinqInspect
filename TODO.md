# UA_LinqInspect 代码审计 TODO

> 扫描日期：2026-11-11 ｜ 范围：`server/`、`nodered/flows.json`、`.node-red/`、`tests/`、启动配置与依赖。  
> 2026-11-21 状态更新：安全通道、Phase 1 关联追踪/MES 错误分类、Node-RED 管理/HTTP 认证基线、状态持久化、MES 回执 Method 单写者和 Edge Core SQLite 垂直切片已完成。未勾选项仍为真实缺口；在线 Node-RED 已使用 SQLite WAL 队列，但仍需与外部 Edge Core 控制面收敛。

## High

✅ [High] ~~`server/qc_server.py:283-285`：使用 `NoSecurity`、默认 permissive user manager，并监听 `0.0.0.0`~~：已改为本地 CA、`Basic256Sha256_SignAndEncrypt`、默认 `127.0.0.1`、匿名只读、业务变量只读和管理员 Method，并有安全回归。
□ [High] Node-RED 已有 `adminAuth`、`httpNodeAuth`、`127.0.0.1` 基线、加密凭据和受保护的实验模式 HTTP 路由；HTTPS、反向代理、网络分区、企业密钥托管和按角色授权仍未完成。
✅ [High] ~~匿名写已拒绝，但管理员仍可直接写 `DeviceStatus`、`CurrentWorkOrderId`、`CurrentPartSN` 和三个回执变量，而 Method 状态机使用另一份内部字段~~：控制、追溯和回执变量已全部改为外部只读；回执只能通过 `RecordMesReceipt(receiptType, receiptNo, workOrderId, correlationId)` 写入，并在服务端读回确认。
✅ [High] ~~`server/qc_server.py:151-199,220-257`：`StartInspection`、`CompleteWorkOrder`、`record_inspection` 没有并发锁；完工与正在进行的检测可交错，出现晚到检测事件、工单已清空、计数快照与事件不一致~~：已由同一把 `asyncio.Lock` 串行化 Method、出站队列变更和检测落地，迟到检测在锁内复查工单状态；重启和故障场景已覆盖。
✅ [High] ~~`server/qc_server.py:142-148,185-199,220-257`：状态、计数、UA 写入、事件发布没有事务或 outbox；`_fire()` 抛异常会让虚拟检测循环直接退出，或出现变量已更新但事件丢失、事件已发出但状态写回失败~~：已把 NG/完工事件与工位状态写入同一 JSON 快照 outbox；Node-RED 成功入队后调用 `AcknowledgeEvent`，未确认事件周期重发，稳定 `SourceEventId`/`CorrelationId` 支持消费端幂等，检测循环也不会因单次发布异常退出。
✅ [Medium] ~~`server/qc_server.py` 的 outbox 当前没有容量上限、压缩和人工清理接口；Node-RED 长时间不可用时会持续增大状态快照~~：已增加 `QC_OUTBOX_MAX_PENDING`/`QC_OUTBOX_HIGH_WATERMARK`，高水位拒绝新开检、硬上限拒绝检测与完工且不静默丢事件；Node-RED 暴露 `qc_event_outbox_*` 指标和告警；新增管理员 `ExportPendingEvents` 分页导出与令牌校验的 `PurgeOutbox`，并有匿名拒绝、背压恢复和清空幂等测试。仍缺事件合并/压缩策略和真实磁盘满降级演练。
✅ [High] ~~在线 Node-RED 已使用 SQLite 队列，但仍有“MES 回执成功后先完成任务、UA 写回失败缺少最终确认”的语义缺口~~：已用持久化 `ua_writebacks`/`ua_write_targets` 把 MES 成功与 UA 写回确认分离；两个目标全部确认后才删除任务，失败按退避重试并进入死信。
✅ [High] ~~MES 2xx/4xx/429/5xx/timeout 未分类~~：Phase 1 已实现回执 schema、关联 ID、错误码、4xx 终态死信、429 `Retry-After`、5xx/timeout 退避和非法 2xx 拒绝；Edge Core 另有事务化死信重放接口。
✅ [High] ~~`nodered/flows.json` 节点 `fn_dedup_ng`、`fn_dedup_done`、`fn_enq_ng`、`fn_enq_done`：先去重并置位，再入队；入队异常、进程崩溃或队列溢出后，同一事件会永久被当作重复丢弃~~：去重检查、去重键写入和队列插入已在同一 SQLite 事务；容量拒绝不会写入去重键，重复事件只有在队列/去重已有持久记录时才 ACK。
✅ [High] ~~`nodered/flows.json` 节点 `fn_enq_ng`、`fn_enq_done`：队列达到 100 时直接丢弃最旧 PENDING 任务且不写死信~~：已改为失败关闭，队列满时拒绝新事件且不 ACK，UA outbox 保留并周期重试；新增 `lastEnqueueRejection`、`qc_enqueue_total{outcome="rejected"}` 和 `QcQueueEnqueueRejected` 告警，不再静默丢业务。
✅ [High] ~~虚拟 SN 使用进程内 `seq=0` 重置，而 Node-RED `snSeen` 跨重启保留；同日重启后会重新生成 `SN-yyyyMMdd-000001`~~：SN 日期与序号已进入 `qc-state.json` 快照，`SIGKILL` 重启回归验证序号继续递增。
✅ [High] ~~服务重启后工单、计数器、窗口、SN 序号全部丢失，Node-RED 却保留去重和队列~~：工单、目标数、计数、窗口、SN 序号、最后检测结果和 outbox 已持久化；重启和 `SIGKILL` 恢复均有自动化覆盖。
□ [High] `nodered/flows.json` 的 `opc_evt`、`opc_evt_done`、`fn_ua_err`：事件订阅只在部署时建立并在 5 秒后重发；断线/Server 重启后没有订阅重建、事件缺口检测或补读机制，心跳只打印告警。应实现重连状态机、重新 `CreateSubscription/CreateMonitoredItems`、事件序号缺口检测和可恢复补读策略。
□ [High] `nodered/flows.json` 节点 `fn_parse`、`fn_dedup_ng`、`fn_enq_ng`、`fn_pump`、`fn_resp`：除 HTTP/UA 节点外没有为 Function 节点配置 `catch`；任一 JS 异常都会让事件或队列项静默丢失。应为关键节点增加 catch 到死信/人工处置的兜底路径。
□ [High] `server/qc_server.py:76` 与 M1 契约：文档要求 `NgRate >= 0.30` 触发 NG 率报警，但 Node-RED 只有逐条 NG 10 秒抑制，没有读取 `NgRate` 或阈值判断。应实现速率报警分支并补测试，或删除错误的契约承诺。
□ [High] `nodered/flows.json` 的 `/mes/ng-order`、`/mes/complete`：模拟 MES 无鉴权、无请求体大小/字段类型/长度/范围限制，外部可直接造单或造回执，并持续膨胀 context。应增加认证/签名、schema 校验、速率限制、请求体上限和审计。
□ [High] `server/qc_server.py:142-148`：每个事件都调用 `get_event_generator()`，会重新读取事件类型属性、设置 notifier 并添加引用；高频检测下造成不必要延迟和地址空间操作。应在启动时缓存并复用三个 EventGenerator，或在压测后改用批量发布。

## Medium

□ [Medium] `nodered/flows.json` 节点 `fn_pump`、`fn_resp`：单飞行 + 1 秒泵驱动把吞吐上限锁在约 1 请求/秒；MES 超时会阻塞后续所有任务。应按目标节拍做并发受限队列/独立发送进程，并压测最大 backlog。
□ [Medium] `server/qc_server.py:162-166,274-276`：`targetCount` 只用于日志提示，不限制检测、不校验完工时的完成量；已动态确认 start target=5 后立即完工仍返回成功（inspect=0）。应定义目标未达成时的完工策略并补测试。
□ [Medium] `server/qc_server.py:151-170`：同一工单重复 `StartInspection` 会返回 `already running`，但新的 `targetCount` 被静默忽略；同一工单完工后可再次开工且计数器不重置。应引入工单执行批次/attemptId，明确幂等键和目标更新语义。
□ [Medium] `nodered/flows.json` 节点 `fn_dedup_done`：`doneSeen` 只按 `workOrderId` 永久去重；工单返修、重新开检或第二批次完工会被视为重复报工，而服务端又没有禁止工单复用。应使用 `workOrderId + attemptId` 或明确工单唯一生命周期。
□ [Medium] `nodered/flows.json` 节点 `fn_resp`：只校验 `2xx`，未要求 `accepted === true` 或响应 schema/Content-Type；任意 2xx 空对象或 HTML 都可能被当作成功。应严格校验回执字段、类型和业务状态。
□ [Medium] `nodered/flows.json` 节点 `fn_ua_result`、`fn_ua_err`：写回异常已分流到 UA 写回重试/死信，但读、Method 和订阅结果仍未完整校验 `StatusCode/Quality`；应继续区分各类错误，避免假健康或假断线。
□ [Medium] `nodered/flows.json` 节点 `/qc/status`：`ok` 永远为 `true`，死信、积压、心跳失败和 MES 故障都不会改变健康状态；`uptimeSec` 从首次状态查询/持久化时间计算，重启后也不是真实进程 uptime。应改为分项健康判定和真实运行时长。
□ [Medium] `server/qc_server.py:30-34`：环境变量在导入时直接 `int/float` 转换，端口越界、`QC_NG_WINDOW<=0`、`QC_INSPECT_INTERVAL<=0`、概率越界均未校验；负间隔会造成忙循环和高频事件洪峰。应集中校验配置并在启动时给出可读错误。
□ [Medium] `server/qc_server.py:283` 与 `nodered/flows.json`：UA Server 端口可配置，但 Node-RED endpoint 仍写死 `opc.tcp://localhost:4840`；队列目标写死 `127.0.0.1:1880`，换成其他端口即断路。应统一使用环境变量/配置注入。
□ [Medium] `.gitignore:11`：整个 `.node-red/` 被忽略，导致 `settings.js`、`package-lock.json` 和实际运行的 `flows.json` 不进版本控制；新环境无法复现 localfilesystem context 和锁定依赖。应保留模板/锁文件并只忽略运行态 context 与凭据。
□ [Medium] `.node-red/settings.js:361-368`：localfilesystem context 不是事务存储，`flushInterval:1` 仍可能丢失最近 1 秒写入；大对象反复全量序列化也会增加 I/O。试点前应迁移 SQLite/独立服务并做崩溃恢复测试。
□ [Medium] `nodered/flows.json` 节点 `fn_mes_ng`、`fn_mes_done`：`orders`、`receipts` 无容量上限、无 TTL、无清理策略；长跑会持续膨胀 context、拖慢 flush 和状态查询。
□ [Medium] `nodered/flows.json` 节点 `fn_dedup_ng`、`fn_dedup_done`：去重表固定 500 条并删除最旧项，之后旧事件可再次进入；高节拍生产下 500 条覆盖窗口过短。应使用带时间窗的持久唯一索引。
□ [Medium] `nodered/flows.json` 的 `opcea.queueSize=100`：事件订阅队列溢出没有计数、告警或补偿，断线/MES 阻塞时可能静默丢事件。应暴露 overflow 指标并补事件缺口检测。
□ [Medium] `nodered/flows.json` 节点 `fn_parse`：时间戳解析失败时回退为 `now`，会绕过 60 秒陈旧过滤；未来时间戳也没有拒绝。应区分缺失/非法/未来时间并提供明确的丢弃或死信策略。
□ [Medium] `nodered/flows.json` 节点 `fn_parse`：`isDone`/`isNg` 使用事件类型子串和字段猜测兜底，未知或伪造事件可能被误判为开单/报工。应只接受精确 EventType，所有兜底路径记录告警并纳入测试。
□ [Medium] `nodered/flows.json` 节点 `fn_mes_ng`、`fn_mes_done`：只检查字段是否存在，不校验 `confidence` 类型/范围、计数一致性、`ngRate` 范围和 ID 长度；会生成不合法 MES 记录。应使用 JSON Schema/手写严格校验并返回稳定错误码。
✅ [Medium] ~~`nodered/flows.json` 节点 `fn_mes_ng`、`fn_mes_done`：`orders`/`receipts`/`ngSeen` 等使用普通对象和外部字符串作键，`__proto__`、`constructor` 等键可能造成原型污染、键冲突或错误命中~~：模拟 MES 已抽到 `queue_logic.js`，`orders`/`receipts` 和幂等表均使用 null-prototype 安全映射，并有 `__proto__`/`constructor` 键回归。
✅ [Medium] ~~`nodered/flows.json` 节点 `fn_mes_ng`、`fn_mes_done`：同一 key 的重复请求只比较 key，不比较原始 payload；同一 SN/工单携带不同缺陷或统计仍返回原回执~~：已增加稳定 payload SHA-256 指纹；同键同内容返回原回执和 `duplicated:true`，同键不同缺陷/统计返回 `409 IDEMPOTENCY_CONFLICT`，单测 10/10、HTTP E2E 覆盖。
□ [Medium] `server/qc_server.py:201-206`：`ResetCounters` 无角色校验、无 Running/Fault 守卫、无审计，生产中任意客户端可清零统计。应限制为维护角色，要求 Idle 或显式确认，并记录操作者/原因。
□ [Medium] `server/qc_server.py:220-238`：`record_inspection` 未校验 confidence 范围、缺陷码枚举、OK/NG 与缺陷字段一致性；空 SN 时仍更新结果但不更新 `CurrentPartSN`，会留下陈旧追溯号。应做输入不变量校验并拒绝不一致数据。
□ [Medium] `server/qc_server.py:215-218`：统计变量逐个写入，消费者可能观察到 `InspectCount` 已增加而 `OkCount/NgCount` 未更新的中间态；应提供原子快照或统一状态版本号。
✅ [Medium] ~~三个 MES 回执变量允许客户端直接写，任何匿名客户端都能伪造回执~~：三个变量已改为只读，匿名和管理员直接写均被拒绝；Node-RED 只能调用 `RecordMesReceipt`，Method 校验类型、业务号、工单和关联 ID。
✅ [Medium] ~~没有 `correlationId`~~：UA 事件、Node-RED 请求/回执、死信和 Edge Core API 已透传并校验关联 ID。
□ [Medium] `nodered/flows.json` 节点 `fn_status`：已提供受鉴权的按 ID/批量死信重放，但仍只返回最后一条死信，缺少分页列表、详情过滤、删除/确认和重放审计。应补完整 dead-letter 查询与操作记录。
□ [Medium] `nodered/flows.json` 节点 `fn_status`、`fn_resp`：状态接口只暴露队列/死信计数，没有成功率、重试数、MES 延迟、事件丢弃数、订阅健康等指标。应增加 Prometheus/结构化指标。
□ [Medium] `server/qc_server.py:229-238`：每次检测顺序执行 9 个 UA 变量写和 1~2 个事件发布，未提供批量写入或写入缓存；高频场景下延迟线性累积。应在压测后批处理/合并统计写入。
□ [Medium] `server/qc_server.py:280`：`logging.basicConfig(level=INFO)` 放大 asyncua 初始化日志；本次启动产生约 1000+ 行 `add_node ... does not exists`。应把 asyncua 日志降到 WARNING，并为业务日志配置轮转。
□ [Medium] `tests/smoke_test.py`：测试依赖真实服务、固定工单、随机 NG 概率和固定等待时间，默认 `QC_NG_PROBABILITY=0.3`、2 秒间隔时存在概率性失败；测试前置状态也不隔离。应提供测试夹具、固定 seed/确定数据源和独立测试实例。
✅ [Medium] ~~`tests/smoke_test.py` 会直接写三个 MES 回执变量且不恢复~~：冒烟测试已改为调用 `RecordMesReceipt`，并验证直接写被拒绝；不再绕过业务 Method 污染状态。
□ [Medium] `tests/fault_scenarios.py`、`tests/nodered_e2e.py`：异常中断时可能留下 Running 工单、timeout/5xx 模式或未恢复状态；没有统一 setup/teardown。应增加 finally 清理和服务健康前置检查。
□ [Medium] 测试覆盖缺口：已覆盖直接写绕过 Method、4xx/429、非法 2xx、队列溢出、去重回滚、服务重启、SN 重启碰撞、订阅断线恢复和人工重放；仍缺高并发开检/完工压力、订阅缺口检测、磁盘满、SQLite 损坏和真实网络分区演练。
□ [Medium] `tests/_perm_probe.py:24`：把结果写到 `D:/UA_LinqInspect/_perm_probe.txt`，在 Ubuntu 直接失败；探针还会尝试写入已被收紧为只读的业务变量。应改为参数化输出路径/临时目录并只读检查。
□ [Medium] `.install_deps.py:4-6`：硬编码 Windows 用户路径并手工复制 site-packages，不可复现且可能引入来源不明的包。应删除该脚本，改为标准 `requirements.txt`/锁文件安装。
□ [Medium] `nodered/flows.json` 节点 `fn_alarm`：只在有下一条 NG 时输出被抑制数量；如果生产在 10 秒窗内停止，末尾抑制数永远不告警，且红色状态不会自动恢复。应使用定时汇总和明确的告警恢复事件。
□ [Medium] 运维缺口：健康/就绪探针、systemd 守护、WAL/磁盘告警、Alertmanager 路由模板、logrotate 与 SQLite 备份恢复已完成；仍缺异地/加密备份、真实值班渠道联调、进程重启告警以及试点主机实测 RTO。

## Low

□ [Low] `server/qc_server.py:142-148`、`nodered/flows.json` 多个 Function：NodeId、端口、超时、队列上限、去重上限等魔法字符串/数字重复出现，配置变更容易漂移。应提炼共享常量/配置。
□ [Low] `nodered/flows.json` 节点 `fn_mes_ng`、`fn_mes_done`、`fn_enq_ng`、`fn_enq_done`：NG/报工处理与入队逻辑高度重复，后续错误处理容易只改一侧。应抽取公共函数/子流。
□ [Low] `nodered/flows.json` 的 `MES-*`/`RC-*` 编号使用 4 位 `padStart`，序号超过 9999 后测试正则 `\d{4}` 会失败且编号长度变化。应统一为固定宽度或完整 UUID/雪花 ID。
□ [Low] `nodered/flows.json` 节点 `fn_sub_log_ng`、`fn_sub_log_done`：每次部署都创建 5 秒定时器，未在节点关闭时清理；重复部署会残留旧定时器。应在 close/清理生命周期中取消。
□ [Low] `server/qc_server.py:291-299`：取消虚拟检测任务后没有 `await` 任务结束；`asyncio.Future()` 也没有明确关闭信号。应增加 SIGTERM/SIGINT 优雅停机和任务收敛。
□ [Low] `nodered/flows.json` 节点 `fn_enq_ng`：NG 队列项没有 `createdAt`，`/qc/status` 计算最老任务年龄时对未发送 NG 显示 0。应统一所有队列项的时间字段。
□ [Low] `nodered/flows.json` 节点 `fn_status`：`FAILED` 计数永远为 0，因为失败项被移入 `dead`；该字段会误导运维。应删除或改为真实任务状态统计。
□ [Low] `nodered/flows.json` 和 `.node-red/` 同时保存两份 `flows.json`，且实际运行文件被 git 忽略；已出现过同步风险。应保留单一源文件并使用部署脚本复制/校验。
□ [Low] `nodered/flows.legacy.json`：历史流文件仍可被误导入，和当前契约不一致。应移入 archive 或删除。
□ [Low] `server/requirements.txt` 只锁直接依赖，`package.json` 使用 `^` 且 lock 文件被忽略；依赖供应链不可复现。应补 Python 锁/哈希、提交 npm lock，并加入依赖审计。
□ [Low] 项目没有统一的 lint/type-check/format 配置，也没有 CI；Python 只有语法检查，Node-RED Function 没有单元测试。应加入 `ruff`/`mypy`（或等价）与 Node 语法/单元测试流水线。
□ [Low] 根目录仍有运行日志、`.tmp` 补丁脚本、历史探针和 zip 交付物；没有清理策略，容易误运行或误交付。应移入 `archive/`/`tools/` 并加入忽略规则。
□ [Low] `docs/05_工程化优化路线图.md`、`docs/07_Ubuntu迁移与验证报告.md` 的测试数量和完成度依赖手工维护，已经出现过时口径。应让文档从测试结果生成或至少加 CI 校验。
