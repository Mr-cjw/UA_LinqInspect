# Phase 2C：部署、运维闭环与恢复演练

> 日期：2026-11-21 ｜ 回归证据：`.tmp/regression-20261121-021845/summary.json` ｜ 当前结果：523/523 PASS

## 1. 阶段目标

Phase 2B 已经具备 Prometheus 指标、SQLite 在线备份和完整性检查，但还需要回答四个现场问题：

1. 两个长期运行进程如何由 systemd 托管并自动重启？
2. 指标和告警规则如何落到 Prometheus/Alertmanager？
3. 备份由谁定时触发，失败和过期如何发现？
4. 数据库损坏或误操作后，如何安全恢复并验证业务可继续？

Phase 2C 补齐部署模板、健康探针、备份 timer、告警规则和离线恢复脚本。它证明的是**可重复的运维路径**，不等于已经完成真实工厂环境的高可用或灾备验收。

## 2. 部署拓扑

```text
systemd: ua-linqinspect-server.service   -> server/qc_server.py :4840
systemd: ua-linqinspect-nodered.service  -> Node-RED :1880 / healthz / metrics
systemd: ua-linqinspect-backup.timer     -> backup_queue.py -> data/backups
Prometheus                               -> scrape Node-RED /metrics
node_exporter textfile                   -> /var/lib/ua-linqinspect/qc-backup.prom
```

默认安装路径以 `/opt/ua-linqinspect` 为例。仓库中的 `deploy/` 是可渲染模板，不能把 `.service.in` 直接安装到 systemd。

## 3. systemd 部署

### 3.1 前置条件

先在目标目录完成仓库自带的环境安装、证书生成和首次启动验证：

```bash
cd /opt/ua-linqinspect
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
.venv/bin/python scripts/setup_pki.py
npm install --prefix .node-red --no-audit --no-fund
```

确认以下文件存在：`.venv/bin/python`、`.node-red/node_modules/.bin/node-red`、
`nodered/settings.secure.js`、`scripts/setup_node_red_security.py`、
`server/qc_server.py`、`scripts/backup_queue.py`。

### 3.2 渲染并安装

选择实际运行用户和组。默认使用当前用户；生产环境建议使用专用低权限账号：

```bash
cd /opt/ua-linqinspect
.venv/bin/python scripts/render_deploy.py \
  --repo-root /opt/ua-linqinspect \
  --user ua-linqinspect \
  --group ua-linqinspect \
  --env-file /etc/ua-linqinspect/ua-linqinspect.env \
  --output-dir .tmp/deploy-rendered
```

安装环境文件和 unit。环境文件包含管理员密码和私钥路径，不能全局可读：

```bash
sudo install -d -o root -g ua-linqinspect -m 0750 /etc/ua-linqinspect
sudo install -o root -g ua-linqinspect -m 0640 \
  .tmp/deploy-rendered/env/ua-linqinspect.env.example \
  /etc/ua-linqinspect/ua-linqinspect.env
sudo editor /etc/ua-linqinspect/ua-linqinspect.env

sudo -u ua-linqinspect bash -c 'set -a; . /etc/ua-linqinspect/ua-linqinspect.env; set +a; \
  cd /opt/ua-linqinspect && .venv/bin/python scripts/setup_node_red_security.py'

sudo install -m 0644 .tmp/deploy-rendered/systemd/*.service \
  .tmp/deploy-rendered/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ua-linqinspect-server.service
sudo systemctl enable --now ua-linqinspect-nodered.service
sudo systemctl enable --now ua-linqinspect-backup.timer
```

至少替换环境文件中的 `UA_ADMIN_PASS`、`NODERED_*_PASSWORD`、
`NODERED_CREDENTIAL_SECRET`、证书路径、私钥路径、数据库路径和备份目录。
`UA_ADMIN_USER/PASS` 必须与 `NODERED_OPCUA_USER/PASSWORD` 一致，否则 Node-RED
无法以管理员身份订阅事件和调用 `RecordMesReceipt`。安全初始化脚本会按环境变量生成
`.node-red/settings.js`、加密的 `flows_cred.json` 和本地 `.secure-env`。

`EnvironmentFile` 在 unit 中标记为可选，缺失时进程可能启动但配置不完整；部署检查不应把“能启动”当作“配置正确”。

### 3.3 Unit 边界

| Unit | 关键行为 |
|---|---|
| `ua-linqinspect-server.service` | `Restart=on-failure`，只写审计日志和 StateDirectory |
| `ua-linqinspect-nodered.service` | 等待 Server，使用 SQLite WAL 和 HTTP Basic Auth；只写 `data/` 与 `.node-red/` |
| `ua-linqinspect-backup.service` | `Type=oneshot`，执行在线备份并写 Prometheus textfile |
| `ua-linqinspect-backup.timer` | 每日 02:15，随机延迟 5 分钟，`Persistent=true` 补跑漏掉的任务 |

所有 unit 都启用 `NoNewPrivileges`、`ProtectSystem=strict`、`PrivateTmp` 和空 capability 集。若修改数据目录，必须同步调整 `ReadWritePaths`，否则 SQLite 会因只读文件系统失败。

### 3.4 启停与日志

```bash
sudo systemctl status ua-linqinspect-server.service ua-linqinspect-nodered.service
sudo journalctl -u ua-linqinspect-server.service -u ua-linqinspect-nodered.service -f
sudo systemctl restart ua-linqinspect-nodered.service
```

Node-RED 停止时仍可能留有待发送队列；重启后由 SQLite 队列和看门狗继续处理，不需要手工重建 context。

## 4. 健康检查

`GET /healthz` 返回机器可读 JSON，用于外部检查和人工排障。它和 `/metrics`、
`/qc/status` 一样受 Node-RED `httpNodeAuth` 保护：

| HTTP | `status` | 含义 |
|---:|---|---|
| 200 | `ready` | SQLite 可读，连续 UA 心跳失败少于 3 次 |
| 503 | `degraded` | SQLite 可读，但 `hbFails >= 3`，UA 连接需要检查 |
| 503 | `unavailable` | SQLite 状态查询失败，队列不能正常工作 |

```bash
set -a; . /etc/ua-linqinspect/ua-linqinspect.env; set +a
curl -fsS -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  http://127.0.0.1:1880/healthz | python3 -m json.tool
curl -s -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -o /tmp/health.json -w '%{http_code}\n' http://127.0.0.1:1880/healthz
```

`degraded` 不会自动停止发送泵；它表示运行能力已下降，需要结合 `/qc/status`、`/metrics` 和日志判断是否进入维护。HTTP Basic Auth 不能替代 TLS；跨主机访问必须放在 HTTPS 反向代理或受信任运维网络之后。

## 5. Prometheus 采集与告警

### 5.1 抓取 Node-RED

```yaml
scrape_configs:
  - job_name: ua-linqinspect-edge
    metrics_path: /metrics
    scrape_interval: 15s
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/ua-linqinspect-http.pass
    static_configs:
      - targets: ["127.0.0.1:1880"]
```

`password_file` 内容必须是环境文件中的 `NODERED_HTTP_PASS`，并由 Prometheus
运行用户只读访问。没有配置 `basic_auth` 时 `/metrics` 会返回 401，`QcEdgeDown`
会被误触发。

### 5.2 采集备份 textfile

node_exporter 只应读取备份指标文件，不应直接采集 SQLite。启动 node_exporter 时增加：

```text
--collector.textfile.directory=/var/lib/ua-linqinspect
```

`ua-linqinspect-backup.service` 会原子写入 `qc-backup.prom`，其中包含最近成功时间、备份大小、完整性结果和关键表行数。

### 5.3 规则与阈值

把 `deploy/prometheus/ua-linqinspect-alerts.yml` 放到 Prometheus 的规则目录，并在 `rule_files` 中加载。当前模板包含：

| 告警 | 条件 | 含义 |
|---|---|---|
| `QcEdgeDown` | `up == 0` 持续 2 分钟 | Node-RED 指标端点不可达 |
| `QcQueueBacklogHigh` | pending 大于 20 持续 5 分钟 | MES 处理速度不足或不可达 |
| `QcQueueEnqueueRejected` | 5 分钟内出现容量拒绝持续 1 分钟 | SQLite 队列已满；事件保留在 UA outbox，等待队列排空后重试 |
| `QcEventOutboxBackpressure` | outbox 背压标志为 1 持续 2 分钟 | UA 事件积压达到高水位，新开检会被拒绝 |
| `QcEventOutboxBacklogHigh` | 积压数不低于高水位持续 2 分钟 | Node-RED 未订阅、未入队或未 ACK |
| `QcEventOutboxOldestTooOld` | 最老未确认事件超过 120 秒持续 5 分钟 | outbox 长时间不排空 |
| `QcEventOutboxStatusStale` | 最近状态读取年龄大于 30 秒或从未读取 | 心跳读取 `OutboxStatus` 失败 |
| `QcOldestQueueItemTooOld` | 最老队列项超过 60 秒持续 2 分钟 | 队列不排空 |
| `QcDeadLettersGrowing` | 15 分钟死信增量大于 0 持续 2 分钟 | 新增需人工处置的失败 |
| `QcDeliveryRetrySpike` | 10 分钟重试超过 5 次持续 5 分钟 | MES 质量或网络抖动 |
| `QcHeartbeatDegraded` | 心跳失败数达到 3 持续 2 分钟 | UA 订阅链路异常 |
| `QcBackupStale` | 距上次成功备份超过 36 小时持续 15 分钟 | 备份 timer 失败或指标未采集 |
| `QcDatabaseWalGrowing` | WAL 超过 256 MiB 持续 15 分钟 | 长读事务、checkpoint 失败或写放大 |
| `QcDiskSpaceLow` | 文件系统剩余空间低于 10% 持续 10 分钟 | SQLite、WAL 或备份写入面临磁盘耗尽 |

这些阈值是作品集/试点默认值，不是工厂最终 SLA。上线前应按 MES 延迟、工单节拍和告警噪声调整。
渲染目录同时提供 `prometheus/alertmanager.yml`：critical 重复 30 分钟、warning 重复 4 小时，并抑制
`QcEdgeDown` 引发的队列关联告警；部署前必须把示例 webhook 替换为现场接收端。

状态日志的 logrotate 模板位于 `logrotate/ua-linqinspect`，默认每日检查、100 MiB 上限、保留 7 份并压缩。

## 6. 备份与 RPO/RTO

### 6.1 默认策略

| 项目 | 当前实现 |
|---|---|
| 触发 | 每日 02:15，随机延迟最多 5 分钟，漏跑后补跑 |
| 方式 | SQLite online backup API，不停止 Node-RED |
| 校验 | `integrity_check` 与 `foreign_key_check` |
| 保留 | 默认最近 7 份，`QC_BACKUP_KEEP` 可调整 |
| 指标 | `qc_backup_last_success_timestamp_seconds`、`qc_backup_size_bytes`、`qc_backup_integrity_ok`、`qc_backup_table_rows` |

手工触发并检查退出码：

```bash
sudo systemctl start ua-linqinspect-backup.service
systemctl show -p Result -p ExecMainStatus ua-linqinspect-backup.service
sudo journalctl -u ua-linqinspect-backup.service -n 100 --no-pager
ls -lh /opt/ua-linqinspect/data/backups
```

**RPO 口径**：按当前每日定时，正常情况下最多丢失约 24 小时数据；超过 36 小时未成功才触发告警。若现场不能接受该窗口，应把 timer 改为每小时并相应调整告警，目标 RPO 才能收敛到约 1 小时。

**RTO 口径**：自动化测试证明脚本级恢复可在秒级完成；端到端 RTO 还要计入停服务、选择备份、启动、健康检查和业务复验。当前尚未在真实工厂主机上记录端到端 RTO，不能声称已经达到固定分钟数。建议以“30 分钟内完成演练”作为首个可验证目标。

### 6.2 备份边界

- 当前只在同一主机保存备份，没有异地副本、对象存储、加密或防篡改。
- 备份成功不等于可恢复成功；必须定期做恢复演练。
- 数据库中没有原始质检图像，备份只覆盖队列、去重、死信和运行计数。

## 7. 离线恢复演练

### 7.1 强制前提

**恢复前必须停止 Node-RED。**

```bash
sudo systemctl stop ua-linqinspect-nodered.service
systemctl is-active ua-linqinspect-nodered.service
```

第二条命令应输出 `inactive`。恢复脚本不会检测 Node-RED 是否仍在运行；若进程仍持有 SQLite 连接，可能继续写旧库或产生不可预测的 WAL 状态。必要时同时停止 UA Server，避免恢复期间持续产生新事件。

### 7.2 选择备份并试跑

```bash
ls -lh /opt/ua-linqinspect/data/backups/qc-queue-*.sqlite

sudo -u ua-linqinspect /opt/ua-linqinspect/.venv/bin/python \
  /opt/ua-linqinspect/scripts/restore_queue.py \
  --backup /opt/ua-linqinspect/data/backups/qc-queue-YYYYmmdd-HHMMSS.sqlite \
  --target /opt/ua-linqinspect/data/qc-queue.sqlite \
  --dry-run
```

`--dry-run` 会复制到临时库、验证完整性和关键表，但不会替换目标库，也不会创建 pre-restore 快照。输出中的 `restoredIntegrityCheck` 应为 `ok`。

### 7.3 执行替换

目标库已存在时必须显式 `--force`：

```bash
sudo -u ua-linqinspect /opt/ua-linqinspect/.venv/bin/python \
  /opt/ua-linqinspect/scripts/restore_queue.py \
  --backup /opt/ua-linqinspect/data/backups/qc-queue-YYYYmmdd-HHMMSS.sqlite \
  --target /opt/ua-linqinspect/data/qc-queue.sqlite \
  --force
```

脚本执行顺序：

1. 校验备份 `integrity_check` 和 `foreign_key_check`。
2. 把备份复制到目标同目录的临时库。
3. 再次校验临时库。
4. 若目标已存在且可验证，在 `data/backups/qc-queue-pre-restore-*.sqlite` 创建安全快照。
5. 若目标无法打开或通过 SQLite 完整性校验，不静默覆盖，而是移动到同目录 `qc-queue-corrupt-*.sqlite` 审计隔离文件。
6. 删除目标旧 `-wal`/`-shm`。
7. 设置 `0600` 后用同目录原子替换安装已验证备份。

正常目标的输出中 `preRestore` 是回滚点，必须记录到变更单或演练记录。若输出 `quarantined`，说明原目标已损坏并被保留取证；恢复成功的判据仍是 `restored: true` 和 `restoredIntegrityCheck: ok`，隔离文件不能替代已验证备份。

### 7.4 启动与验收

```bash
sudo systemctl start ua-linqinspect-nodered.service
systemctl is-active ua-linqinspect-nodered.service
curl -fsS http://127.0.0.1:1880/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:1880/qc/status | python3 -m json.tool
curl -fsS http://127.0.0.1:1880/metrics | grep -E 'qc_(queue|dead_letters|backup_)'
sudo journalctl -u ua-linqinspect-nodered.service -n 100 --no-pager
```

验收至少包括：`/healthz` 可读，队列/死信/去重计数符合预期，发送泵继续消费，回执能继续写回 UA，日志没有 SQLite 或 schema 错误。

### 7.5 恢复失败时回滚

保持 Node-RED 停止，再次执行恢复脚本，把 `--backup` 指向本次输出中的 `preRestore` 文件，并使用新的 `--force`。原始备份不要删除，直到业务验收完成。

## 8. 自动化证据

Phase 2C 新增专项：

| 测试 | 结果 | 覆盖 |
|---|---:|---|
| `tests/health_test.js` | 10/10 PASS | ready/degraded/unavailable、边界、响应头、flow 接线 |
| `tests/mes_idempotency_test.js` | 10/10 PASS | 模拟 MES 稳定指纹、重复回执、冲突 409、旧回执迁移与原型污染防护 |
| `tests/outbox_status_test.js` | 9/9 PASS | OutboxStatus 解析、容量拒收不 ACK、背压健康降级、状态接口与指标 |
| `tests/outbox_capacity_test.py` | 9/9 PASS | 高水位/硬上限拒绝、分页导出、令牌清空与幂等 |
| `tests/outbox_recovery.py` | 18/18 PASS | 积压状态跨 SIGKILL、背压拒绝、ACK 后恢复、稳定 ID |
| `tests/deploy_templates_test.py` | 23/23 PASS | 渲染、systemd verify、Prometheus/Alertmanager/outbox/队列容量告警、logrotate、环境变量、认证配置 |
| `tests/storage_failure_test.js` | 3/3 PASS | 页数封顶模拟写满、入队/完工事务回滚、损坏库启动失败 |
| `tests/restore_queue_test.py` | 28/28 PASS | dry-run、--force、安全快照、损坏目标隔离、WAL/SHM 清理、业务继续消费 |
| `tests/backup_queue_test.py` | 20/20 PASS | 在线备份、指标 textfile、完整性、保留策略、活跃库备份 |
| `tests/long_outage_recovery.py` | 24/24 PASS | 长断网建压、重试耗尽死信、恢复排空、RTO、按 ID 重放 |
| `tests/soak_test.py` | 20/20 PASS | 6 个工单循环、回执唯一、无队列/死信漂移、单周期耗时 |
| `tests/state_persistence_test.py` | 5/5 PASS | 状态快照往返、非法 Running 回退、损坏 JSON 回退、outbox 往返与坏事件隔离 |
| `tests/qc_state_restart_test.py` | 11/11 PASS | SIGKILL 后工单、计数、SN 序列恢复并继续完工 |
| `tests/ua_write_crash_recovery.py` | 27/27 PASS | UA 冻结时注入 NG/完工、MES 成功后 SIGKILL、重启后完成 UA 写回 |

一键回归命令：

```bash
.venv/bin/python scripts/run_regression.py --keep-going
```

2026-11-21 综合回归共 31 个测试模块、**523/523 PASS**，摘要为
`.tmp/regression-20261121-021845/summary.json`。单独运行备份测试且没有活跃
`QC_DB_PATH` 时为 19/19；一键回归包含运行中的 Node-RED 数据库，因此为 20/20。

死信重放接口为 `POST /qc/dead-letters/replay`，受 Node-RED HTTP Basic Auth 保护：

```bash
curl -u "$NODERED_HTTP_USER:$NODERED_HTTP_PASS" \
  -H 'Content-Type: application/json' \
  -d '{"id":"ng|SN-...|2026-11-20T..."}' \
  http://127.0.0.1:1880/qc/dead-letters/replay
```

传 `{"all":true}` 可批量重放；接口复用原队列 ID 和去重键，重复调用不会生成重复任务。

## 9. 当前边界与后续规划

### 9.1 已具备

- systemd 部署模板、健康探针、Prometheus 告警规则和备份 timer。
- SQLite 主库/WAL/SHM 体积指标、WAL 与磁盘空间告警、Alertmanager 路由模板和 logrotate 模板。
- UA outbox 积压/年龄/容量指标、背压健康降级和 Prometheus 告警；管理员可分页导出并用令牌清空积压。
- SQLite 队列满时失败关闭：拒绝新事件、不驱逐旧任务、不 ACK UA outbox，并暴露拒绝指标与临界告警。
- SQLite 在线备份、离线恢复、恢复前安全快照和完整性校验。
- 进程重启、SIGKILL、短断网和 UA Server 重启后的队列恢复。
- MES 长断网后的积压排空、死信保留与受鉴权重放；6 工单 soak 无漂移。

### 9.2 尚不具备

- 高可用、双机热备、自动故障切换和跨主机共享存储。
- 异地/加密/不可变备份，以及备份恢复的定期自动化演练。
- MES 真实鉴权、TLS、限流和接收端幂等契约的现场验证。
- 真实交换机断链、防火墙切换、跨小时长时断网、断电和磁盘满场景证据。
- MES 请求延迟直方图、结构化日志集中采集，以及真实 Alertmanager 接收端/值班流程联调。

### 9.3 推荐下一步

1. 在试点主机完成一次有记录的全链路恢复演练，实测并记录 RTO。
2. 根据业务可接受损失把备份频率从每日提升到每小时，并增加异地副本校验。
3. 把 Alertmanager 路由接入真实值班渠道，补齐静默、升级和进程重启告警。
4. 进入 Phase 3，与真实 MES 联调错误码、幂等键、限流和回执契约。

准确边界：**至少一次投递 + 去重 + MES 接收端幂等，具备进程崩溃恢复、在线备份和离线恢复能力，但不是 exactly-once。**
