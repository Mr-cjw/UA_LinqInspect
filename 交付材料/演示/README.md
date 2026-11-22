# 投资人演示包

> 用途：明天现场按顺序打开、按页讲、出问题时有证据可切。详细执行步骤见 `docs/38_投资人演示当天执行清单.md`。

## 先看

1. `03_投资人演示操作卡.pdf`：一页启动检查，一页 10 分钟讲解顺序。
2. `01_项目总览与现场演示.pdf`：项目全景、四层能力、90 天计划。
3. `02_AI质检最小闭环.pdf`：视觉推理、图像质量门禁、置信度门禁和边界。

## 现场启动

```bash
cd /home/xmt/project_files/UA_LinqInspect
.venv/bin/python scripts/demo_edge_core.py --ui --no-open --port 8899
```

启动后先预热视觉模型，避免现场第一次点击等待训练：

```bash
curl -sS -H 'content-type: application/json' \
  -d '{"sample":"MISSING"}' \
  http://127.0.0.1:8899/api/vision/classify >/dev/null
```

浏览器标签页顺序：

1. `http://127.0.0.1:8899/vision`
2. `http://127.0.0.1:8899/overview`
3. `http://127.0.0.1:8899/joint`
4. `http://127.0.0.1:8899/`（主演示，备用）

## 讲解顺序

- `/vision`：先证明 AI 入口、图像质量门禁和置信度门禁是真的。
- `/overview`：跑完整“视觉 NG → Edge Core → MES 回执 → UA 确认”闭环。
- `/joint`：补充设备停机和温度异常，解释 OEE 损失与 RCA 候选。
- 最后主动讲边界：合成数据、Mock PLC、模拟 MES、没有真实产线和客户验收。

## 演示证据

`04_演示证据/` 是本轮实际 dry-run 的结果，不是编造数据：

- `01_edge_demo.json`：主演示最终 `DONE`，回执 `MES-JX-DEMO-0001`。
- `02_vision_joint.json`：视觉 NG 进入联合闭环，最终 `DONE`。
- `03_overview_joint.json`：项目总览演示最终 `DONE`。
- `04_joint.json`：联合演示最终 `DONE`。
- `05_overview_complete.png`、`06_vision_complete.png`、`07_joint_dashboard.png`：完成态截图，现场故障时备用。

完整隔离回归：44 个测试脚本、0 失败，证据 `.tmp/regression-20261122-152058/summary.json`。

## 失败时

如果 2 分钟内修不好，直接打开上面的 PDF 和完成态截图，明确说明“这是刚才本地跑通的证据，现场环境现在有问题”，不要用预录结果冒充现场运行。

