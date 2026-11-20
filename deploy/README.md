# 部署模板

这些文件是可渲染模板。不要把 `.service.in` 直接复制到 systemd。

```bash
cd ~/UA_LinqInspect
repo_root="$(pwd)"
.venv/bin/python scripts/render_deploy.py \
  --repo-root "$repo_root" \
  --user ua-linqinspect \
  --group ua-linqinspect \
  --env-file /etc/ua-linqinspect/ua-linqinspect.env \
  --output-dir .tmp/deploy-rendered
sudo install -d -o root -g ua-linqinspect -m 0750 /etc/ua-linqinspect
sudo install -o root -g ua-linqinspect -m 0640 .tmp/deploy-rendered/env/ua-linqinspect.env.example \
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

Prometheus 规则文件位于 `.tmp/deploy-rendered/prometheus/ua-linqinspect-alerts.yml`。
Node-RED 的 `/metrics`、`/healthz`、`/qc/status` 受 HTTP Basic Auth 保护；Prometheus
抓取配置必须用 `basic_auth` 指向密码文件。完整示例见 Phase 2C 文档。
完整说明、告警口径和恢复演练见
[../docs/15_Phase2C部署运维与恢复演练.md](../docs/15_Phase2C部署运维与恢复演练.md)。
