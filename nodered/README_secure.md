# Node-RED OPC UA 安全连接与断线恢复

本文记录 `node-red-contrib-opcua@0.2.355` 在 OPC UA Server 重启/网络中断场景下的
行为、已实施的修复，以及验证结果。当前连接继续使用
`Basic256Sha256 + SignAndEncrypt`，没有降级到 `NoSecurity`。

## 1. 节点与插件调查结论

- `ep1` 是 `OpcUa-Endpoint`。该类型的实现只读取
  `endpoint/secpol/secmode/login/usercert/...`；流里的 `keepsession` 和
  `autoconnect` 不会被 `105-opcuaendpoint.js` 使用。
- 真正可用的 keep-alive 属性在 `OpcUa-Client` 节点上是
  `keepsessionalive`。三个客户端节点 `opc_evt`、`opc_evt_done`、`opcua`
  已显式设置为 `true`。
- 插件源码 `opcua/102-opcuaclient.js` 内部有 15 秒一次的真实 Read 探活，
  连续两次失败会强制重建会话；但原实现的断线路径有两个缺陷：
  1. 重连前先进入 `reconnect` 状态，探活看到该状态会直接返回；如果旧
     TCP socket 已死，`client.disconnect()` 的回调可能永远不触发，状态会
     永久卡在 `reconnect`。
  2. 事件订阅没有进入 `autoResubscribe`，重连后只能依赖外部再次注入
     订阅消息。
- 另外，周期性事件订阅/重连重放如果与死连接竞争，会产生未处理的
  Promise rejection；原代码会因此让 Node-RED 进程崩溃。

## 2. 已实施的修复

### 插件补丁

补丁文件为 `patches/node-red-contrib-opcua+0.2.355.patch`，同时应用于根目录
`node_modules` 和 `.node-red/node_modules`。修复内容：

- 将 `connectionStrategy.maxRetry` 从 `0` 改为 `10512000`（约 10 年），
  并保留 5 秒初始、30 秒上限的指数退避；这是 node-opcua 中可用的近似
  “无限重连”。
- 新增 `disconnect_client_with_timeout()`：断连回调 3 秒未完成时强制
  继续 teardown，避免永久卡在 `reconnect`。
- 新增 `close_session_with_timeout()`：会话关闭同样有 3 秒上限。
- `reset_opcua_client()` 在 `node.client === null` 时也会重新创建客户端，
  不再静默失败。
- 新增 `autoResubscribeEvents`：记录事件订阅参数，重连成功后自动重放，
  不依赖人工 redeploy。
- `subscribe_events_input()` 捕获异步订阅失败，避免死连接期间出现
  unhandled rejection 并导致 Node-RED 崩溃。
- 保留此前客户端证书/私钥文件传递修复（`certificateFile` /
  `privateKeyFile`）。

### 流配置

`nodered/flows.json` 与 `.node-red/flows.json` 中：

- `opc_evt`、`opc_evt_done`、`opcua` 设置 `keepsessionalive: true`。
- NG/完工事件启动 Inject 由单次触发改为每 30 秒重新武装一次，作为插件
  自动重订阅之外的兜底；重复订阅同一 topic 在插件内会被去重。

## 3. 本地 Restart 测试方案

测试使用临时端口和目录，不影响常驻服务：

- OPC UA Server：`127.0.0.1:4841`
- Node-RED：`127.0.0.1:1881`
- 临时 flows 中仅把 `ep1.endpoint` 从 4840 改为 4841
- 服务端环境：`UA_ADMIN_USER=XunMengTong`、`UA_ADMIN_PASS` 使用现有凭据，
  `UA_SERVER_CERT=certs/server/server_cert.der`，
  `UA_SERVER_PRIVATE_KEY=certs/server/server_key.pem`，
  `UA_TRUSTED_CLIENT_CERTS_DIR=certs/trusted_clients`

步骤：

1. 启动 OPC UA Server 与 Node-RED，等待订阅建立。
2. 通过 Node-RED 注入 `inj_start`，确认 Method 调用和事件写回正常。
3. 停止 OPC UA Server，保持 Node-RED 进程运行，等待探活失败并触发重连。
4. 重新启动 OPC UA Server。
5. 不执行 redeploy、不重启 Node-RED，观察日志、订阅和读写是否自动恢复。
6. 再次注入 `inj_start`，确认 Method 调用与事件写回恢复。

## 4. 实测结果

基线（Server 正常）：

- `StartInspection` 成功，审计日志为
  `user=XunMengTong ... result=accepted`。
- NG 事件到达 Node-RED，`LastNgMesOrderNo` 与 `LastMesOrderNo` 写回成功。
- 心跳读 `DeviceStatus` 正常，Node-RED flow context 中 `hbFails = 0`。

Server 停止期间：

- `18:20:25` 第一次探活失败，`18:20:40` 第二次失败后输出
  `Server not responding, forcing reconnection`。
- 另外两个事件客户端在 `18:20:55` 也完成同样的强制重连。
- Node-RED 进程保持存活；30 秒事件重订阅兜底继续运行，没有再出现
  uncaught exception / 进程退出。

Server 重启后（未人工干预）：

- Node-RED 在 Server 恢复后自动重建 SecureChannel/session，事件订阅恢复，
  ConditionRefresh 调用继续出现。
- `18:21:53` 再次注入 `inj_start` 成功：
  `StartInspection user=XunMengTong ... result=accepted`。
- `18:21:54` 起 NG 事件恢复，Node-RED 日志显示
  `[UA写回成功] ns=2;s=QCStation.LastNgMesOrderNo` 和
  `[UA写回成功] ns=2;s=QCStation.LastMesOrderNo`。
- 重连后再次读 `DeviceStatus`，`hbFails` 仍为 `0`。

结论：Server 重启后 Node-RED 可以自动重连并恢复读取、Method 调用、
事件订阅和写回，无需 redeploy 或重启 Node-RED。

## 5. 注意事项

- `maxRetry: 10512000` 是 node-opcua 的近似无限重连，不是数学意义上的
  `Infinity`；按 5–30 秒退避计算可使用约 10 年。
- 断网期间的瞬时读/写/Method 请求可能失败或进入插件内部队列；重连后
  只保证连接与订阅恢复，不提供业务消息的 exactly-once 保证。
- 事件订阅的中断窗口内事件可能丢失，恢复后不会补发服务端已丢弃的事件。
- Node-RED 的 OPC UA PKI 目录独立于服务端。轮换 CA/Server 证书后，需要按
  `README.md` 把新的 CA 与 CRL 同步到 Node-RED 的 `node-red-opcua-nodejs/PKI`
  目录并重启 Node-RED。
- 本地补丁依赖 `patch-package`。重新安装依赖后必须确认
  `patches/node-red-contrib-opcua+0.2.355.patch` 仍被 postinstall 成功应用，
  否则断线恢复和客户端证书修复都会失效。
