# 本轮演示证据摘要

日期：2026-11-22  
来源：本地实际 dry-run，不是预录或编造。

## 主演示 `01_edge_demo.json`

- 最终状态：`DONE`
- MES 回执：`MES-JX-DEMO-0001`
- 累计尝试：4 次（含超时/503 与人工重放）
- 未完成队列：0
- UA 写回确认：是
- 死信历史：1 条，已重放

## 视觉 + 联合 `02_vision_joint.json`

- 视觉判定：`NG`
- 缺陷类别：`MISSING`
- 图像质量门禁：`PASS`
- 置信度：1.0（合成样本，不代表现场精度）
- 最终状态：`DONE`
- OEE：0.5102（A 0.7349 / P 0.7810 / Q 0.8889）
- RCA 候选：`TEMPERATURE_SPIKE`，中高置信，仅时间重合，不是因果证明

注意：首次视觉请求约 8.8 秒，包含模型训练；演示前应预热一次。

## 项目总览 `03_overview_joint.json`

- 最终状态：`DONE`
- OEE：0.5685（A 0.7407 / P 0.8529 / Q 0.9000）
- RCA 候选：`TEMPERATURE_SPIKE`

## 联合演示 `04_joint.json`

- 最终状态：`DONE`
- OEE：0.4989（A 0.7405 / P 0.7580 / Q 0.8889）
- RCA 候选：`TEMPERATURE_SPIKE`

## 完整回归

- 44 个测试脚本全部通过，0 失败。
- 证据：`.tmp/regression-20261122-152058/summary.json`

