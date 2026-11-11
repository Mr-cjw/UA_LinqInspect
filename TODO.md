# UA_LinqInspect 代码审计 TODO

> 扫描日期：2026-11-11 ｜ 范围：`server/`、`nodered/flows.json`、`.node-red/`、`tests/`、启动配置与依赖。  
> 本文件只记录问题与修复规划，**本轮不修改任何业务代码**。

## High

□ [High] `server/qc_server.py:283-285`：使用 `NoSecurity`、默认 permissive user manager，并监听 `0.0.0.0`；任何能访问 4840 的客户端都可匿名读写/调用。应改为最小权限 user manager、证书信任、匿名只读，并默认只绑定回环或受控网卡。
□ [High] `.node-red/settings.js:80,129,157`：Node-RED 未启用 `adminAuth`、`httpNodeAuth` 和 `https`，`uiHost` 仍为默认全网卡监听；编辑器/Admin API 与 `/inject/*` 可被远端利用，等同于未授权流程部署与命令注入。应启用鉴权、限制管理面无网络暴露，并关闭生产环境的调试注入入口。
□ [High] `server/qc_server.py:91-98,151-199`：`DeviceStatus`、`CurrentWorkOrderId`、`CurrentPartSN`、三个回执变量都可直接写，但 Method 状态机使用另一份内部字段。已动态确认：外部把 `DeviceStatus` 写为 `2(Fault)` 后仍可成功执行 `StartInspection`；把变量写为 `Idle` 后仍可完成内部 Running 工单。应消除双源状态，把工单/设备状态改为 Method 独占，或对直接写入做统一校验与同步。
□ [High] `server/qc_server.py:151-199,220-257`：`StartInspection`、`CompleteWorkOrder`、`record_inspection` 没有并发锁；完工与正在进行的检测可交错，出现晚到检测事件、工单已清空、计数快照与事件不一致。应增加单一状态锁与一致性快照，并明确“检测中是否允许完工”。
□ [High] `server/qc_server.py:142-148,185-199,220-257`：状态、计数、UA 写入、事件发布没有事务或 outbox；`_fire()` 抛异常会让虚拟检测循环直接退出，或出现变量已更新但事件丢失、事件已发出但状态写回失败。应将状态变更、事件发布和持久化纳入可恢复的事务/outbox 流程。
□ [High] `nodered/flows.json` 节点 `fn_resp`：HTTP 2xx 后先 `q.splice()` 删除任务，再校验 `mesOrderNo/receiptNo`；回执缺失、UA 写回失败、Node-RED 崩溃都会造成“MES 已受理但 UA 无回执”。应引入 `RECEIPT_PENDING/WRITING` 状态、持久化 outbox 和写回重试，收到有效回执并成功写 UA 后才完成。
□ [High] `nodered/flows.json` 节点 `fn_resp`：`accepted=false` 的 2xx 响应被直接出队，4xx 则按通用失败重试；没有死信、人工重放或业务错误码分类。应按 `4xx` 不重试、`429` 遵循 `Retry-After`、`5xx/timeout` 退避、业务拒绝进入死信，并提供重放入口。
□ [High] `nodered/flows.json` 节点 `fn_dedup_ng`、`fn_dedup_done`、`fn_enq_ng`、`fn_enq_done`：先去重并置位，再入队；入队异常、进程崩溃或队列溢出后，同一事件会永久被当作重复丢弃。应先去重与入队原子化，或让去重状态随任务成功/死信状态回滚，并提供重放。
□ [High] `nodered/flows.json` 节点 `fn_enq_ng`、`fn_enq_done`：队列达到 100 时直接丢弃最旧 PENDING 任务且不写死信；高 NG 率或 MES 慢时会静默丢业务。应转入死信并记录丢弃原因，同时提供背压/告警。
□ [High] `server/qc_server.py:260-276` 与 `nodered/flows.json` 的持久化去重：虚拟 SN 使用进程内 `seq=0` 重置，而 Node-RED `snSeen` 跨重启保留；同日重启后会重新生成 `SN-yyyyMMdd-000001`，合法 NG 被误判为重复。应使用持久化全局序列、UUID 或工单+设备+时间组合键。
□ [High] `server/qc_server.py:151-199`：服务重启后工单、计数器、窗口、SN 序号全部丢失，Node-RED 却保留去重和队列；会丢失进行中的报工并产生状态错位。应持久化工单/统计/序列，或启动时强制进入安全恢复流程并重建状态。
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
□ [Medium] `nodered/flows.json` 节点 `fn_ua_result`、`fn_ua_err`：没有校验 UA `StatusCode/Quality`；任何 `opcua` 消息都可能清零心跳失败，Method/写回异常也会计入心跳失败，造成假健康/假断线。应区分读、写、Method、订阅错误并检查服务质量。
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
□ [Medium] `nodered/flows.json` 节点 `fn_mes_ng`、`fn_mes_done`：`orders`/`receipts`/`ngSeen` 等使用普通对象和外部字符串作键，`__proto__`、`constructor` 等键可能造成原型污染、键冲突或错误命中。应使用 `Object.create(null)`、`Map`/`Set` 或持久唯一表。
□ [Medium] `nodered/flows.json` 节点 `fn_mes_ng`、`fn_mes_done`：同一 key 的重复请求只比较 key，不比较原始 payload；同一 SN/工单携带不同缺陷或统计仍返回原回执。应校验幂等指纹，不一致时返回冲突错误。
□ [Medium] `server/qc_server.py:201-206`：`ResetCounters` 无角色校验、无 Running/Fault 守卫、无审计，生产中任意客户端可清零统计。应限制为维护角色，要求 Idle 或显式确认，并记录操作者/原因。
□ [Medium] `server/qc_server.py:220-238`：`record_inspection` 未校验 confidence 范围、缺陷码枚举、OK/NG 与缺陷字段一致性；空 SN 时仍更新结果但不更新 `CurrentPartSN`，会留下陈旧追溯号。应做输入不变量校验并拒绝不一致数据。
□ [Medium] `server/qc_server.py:215-218`：统计变量逐个写入，消费者可能观察到 `InspectCount` 已增加而 `OkCount/NgCount` 未更新的中间态；应提供原子快照或统一状态版本号。
□ [Medium] `server/qc_server.py:91-98`：三个 MES 回执变量允许客户端直接写，任何匿名客户端都能伪造回执。应改为 Method/MES 网关独占写入，或对写入值做签名/关联校验。
□ [Medium] `nodered/flows.json` 节点 `fn_ua_result`、`fn_status`：没有 `correlationId`，无法把 UA 事件、队列项、MES 请求、回执和 UA 写回逐笔关联。应生成并透传 `corrId`，日志和状态接口统一输出。
□ [Medium] `nodered/flows.json` 节点 `fn_status`：只返回死信最后一条，没有完整死信列表、重放、删除/确认接口，现场只能看日志。应提供受鉴权的 dead-letter 查询与人工重放 API。
□ [Medium] `nodered/flows.json` 节点 `fn_status`、`fn_resp`：状态接口只暴露队列/死信计数，没有成功率、重试数、MES 延迟、事件丢弃数、订阅健康等指标。应增加 Prometheus/结构化指标。
□ [Medium] `server/qc_server.py:229-238`：每次检测顺序执行 9 个 UA 变量写和 1~2 个事件发布，未提供批量写入或写入缓存；高频场景下延迟线性累积。应在压测后批处理/合并统计写入。
□ [Medium] `server/qc_server.py:280`：`logging.basicConfig(level=INFO)` 放大 asyncua 初始化日志；本次启动产生约 1000+ 行 `add_node ... does not exists`。应把 asyncua 日志降到 WARNING，并为业务日志配置轮转。
□ [Medium] `tests/smoke_test.py`：测试依赖真实服务、固定工单、随机 NG 概率和固定等待时间，默认 `QC_NG_PROBABILITY=0.3`、2 秒间隔时存在概率性失败；测试前置状态也不隔离。应提供测试夹具、固定 seed/确定数据源和独立测试实例。
□ [Medium] `tests/smoke_test.py`：测试会直接写 `LastMesOrderNo`、`LastNgMesOrderNo`、`LastCompletionReceiptNo` 等真实业务变量且不恢复，污染后续演示和状态接口。应使用测试命名空间/快照恢复。
□ [Medium] `tests/fault_scenarios.py`、`tests/nodered_e2e.py`：异常中断时可能留下 Running 工单、timeout/5xx 模式或未恢复状态；没有统一 setup/teardown。应增加 finally 清理和服务健康前置检查。
□ [Medium] 测试覆盖缺口：没有覆盖直接写变量绕过 Method、并发完工/检测、无效 MES 2xx 回执、4xx/429、队列溢出、去重回滚、服务重启、SN 重启碰撞、订阅断线与人工重放。应逐项补回归测试。
□ [Medium] `tests/_perm_probe.py:24`：把结果写到 `D:/UA_LinqInspect/_perm_probe.txt`，在 Ubuntu 直接失败；探针还会写入真实业务变量。应改为参数化输出路径/临时目录并只读检查。
□ [Medium] `.install_deps.py:4-6`：硬编码 Windows 用户路径并手工复制 site-packages，不可复现且可能引入来源不明的包。应删除该脚本，改为标准 `requirements.txt`/锁文件安装。
□ [Medium] `nodered/flows.json` 节点 `fn_alarm`：只在有下一条 NG 时输出被抑制数量；如果生产在 10 秒窗内停止，末尾抑制数永远不告警，且红色状态不会自动恢复。应使用定时汇总和明确的告警恢复事件。
□ [Medium] 运维缺口：没有服务级健康/就绪探针、systemd/Docker 进程守护、日志轮转、指标端点和备份恢复步骤；试点现场只能靠人工查看 Node-RED。应补齐部署与运维基线。

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
