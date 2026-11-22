#!/usr/bin/env python3
"""Render the two-page investor demo operation card."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "investor-demo-card"
HTML_PATH = OUT / "投资人演示操作卡.html"
PDF_PATH = ROOT / "交付材料" / "演示" / "03_投资人演示操作卡.pdf"


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>投资人演示操作卡</title>
<style>
@page { size: A4; margin: 0; }
:root{--ink:#17202a;--muted:#5d6c7b;--line:#d7dfe6;--navy:#0d2234;--gold:#b4771c;--goldbg:#fcf5e6;--mint:#17795a;--mintbg:#eef9f4;--red:#a5403b;--redbg:#fdf0ef;--blue:#1d5f8a;--bluebg:#eef6fc}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:var(--ink)}
body{font-family:"Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",sans-serif;line-height:1.42}
.page{width:210mm;height:297mm;padding:9mm 12mm 7mm;background:#fff;overflow:hidden;position:relative;page-break-after:always}
.page:last-child{page-break-after:auto}
.top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid var(--navy);padding-bottom:3mm}
.brand{display:flex;gap:2.5mm;align-items:center}
.mark{width:9mm;height:9mm;border-radius:2.5mm;display:grid;place-items:center;background:linear-gradient(135deg,#e5b45f,#c9892b);color:#17202a;font-weight:900;font-size:8pt}
.brand b{display:block;font-size:10pt;letter-spacing:.02em}
.brand span{display:block;font-size:6.8pt;color:var(--muted);margin-top:.5mm}
.meta{text-align:right;font-size:6.8pt;color:var(--muted);line-height:1.55}
.meta b{color:var(--gold);font-size:8pt}
.title{display:flex;justify-content:space-between;align-items:flex-end;margin:4mm 0 2.5mm}
h1{font-size:17pt;line-height:1.1;margin:0;letter-spacing:-.03em}
.title span{font-size:7pt;color:var(--muted)}
.verdict{border:1px solid #e2c58f;background:var(--goldbg);border-radius:2.5mm;padding:3mm 3.5mm;font-size:8pt;line-height:1.55;color:#5b431d;margin-bottom:3mm}
.verdict b{color:#3e2d12;font-size:9pt}
.section{margin-top:3mm}
h2{font-size:9pt;margin:0 0 1.6mm;padding-left:2.5mm;border-left:2.5px solid var(--gold);line-height:1.2}
h3{font-size:8pt;margin:0 0 1.2mm;color:var(--navy)}
table{width:100%;border-collapse:collapse;font-size:6.7pt;table-layout:fixed}
th{background:var(--navy);color:#fff;text-align:left;padding:1.25mm 1.5mm;font-weight:700;font-size:6.5pt}
td{border:1px solid var(--line);padding:1.2mm 1.5mm;vertical-align:top;color:#435260;word-break:break-word}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:3mm}
.cols3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3mm}
.box{border:1px solid var(--line);border-radius:2.5mm;padding:2.5mm 3mm;background:#fbfcfd}
.box.good{border-color:#b9dfd0;background:var(--mintbg)}
.box.bad{border-color:#e3b7b3;background:var(--redbg)}
.box.info{border-color:#b8d4e7;background:var(--bluebg)}
.box p,.box li{font-size:6.7pt;line-height:1.5;color:#455463}
.box ul{margin:0;padding-left:3.5mm}
.box li::marker{color:var(--gold)}
.good b{color:#104b39}.bad b{color:#4e211e}.info b{color:#174a6b}
.code{font-family:"DejaVu Sans Mono",monospace;background:#0f1d28;color:#d8e5ee;border-radius:2.5mm;padding:2.5mm 3mm;font-size:6.5pt;line-height:1.55;white-space:pre-wrap;word-break:break-all}
.tag{display:inline-block;border-radius:99px;padding:.7mm 2mm;font-size:6pt;font-weight:800;margin-bottom:1.5mm}
.tag.good{background:#dff3ea;color:#116247}.tag.bad{background:#f8dedb;color:#8b332e}.tag.warn{background:#f8edcf;color:#795717}
.note{border:1px solid #b9dfd0;background:var(--mintbg);border-radius:2.5mm;padding:2.5mm 3mm;font-size:6.7pt;line-height:1.55;color:#245c49;margin-top:2.5mm}
.note b{color:#104b39}
.footer{position:absolute;left:12mm;right:12mm;bottom:4mm;border-top:1px solid var(--line);padding-top:1.2mm;display:flex;justify-content:space-between;font-size:6pt;color:#8a97a3}
.tight td{padding:.95mm 1.3mm}
.small{font-size:6.2pt;color:var(--muted)}
</style>
</head>
<body>

<section class="page">
  <div class="top"><div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>投资人演示操作卡 · 01</span></div></div><div class="meta"><b>2026-11-23</b><br>现场启动与检查</div></div></div>
  <div class="title"><h1>明天先按这张卡启动</h1><span>现场运行 · 不用预录</span></div>
  <div class="verdict"><b>演示目标：</b>用 8～12 分钟讲清“AI 看见缺陷之后，结果如何可靠进入 MES，并用设备上下文解释损失”。现场只跑真实本地代码；合成数据、Mock PLC、模拟 MES 必须主动说明。</div>

  <div class="section"><h2>1. 演示前 30 分钟</h2><div class="code">cd /home/xmt/project_files/UA_LinqInspect
git pull --ff-only origin main
.venv/bin/python tests/demo_edge_core_test.py
.venv/bin/python tests/vision_demo_test.py
test -d /home/xmt/project_files/rpi4-opcua-oee-rca-gateway
test -d /home/xmt/project_files/rpi4-opcua-oee-rca-gateway/node_modules
node --version</div></div>

  <div class="section"><h2>2. 启动演示</h2><div class="code">.venv/bin/python scripts/demo_edge_core.py --ui --no-open --port 8899</div>
  <p class="small">如果 8899 被占用：把 --port 改成 8900。启动后终端应显示“五分钟软件体验已启动”。</p></div>

  <div class="section"><h2>3. 先预热视觉模型</h2><div class="code">curl -sS -H 'content-type: application/json' \
  -d '{"sample":"MISSING"}' \
  http://127.0.0.1:8899/api/vision/classify &gt;/dev/null</div>
  <p class="small">本轮实测首次视觉请求约 8.8 秒，包含模型训练；不预热会让投资人现场等模型训练。</p></div>

  <div class="section"><h2>4. 浏览器标签页顺序</h2><table class="tight">
    <tr><th style="width:18%">顺序</th><th style="width:45%">地址</th><th>用途</th></tr>
    <tr><td>1</td><td>http://127.0.0.1:8899/vision</td><td>AI 推理、图像质量门禁、置信度门禁</td></tr>
    <tr><td>2</td><td>http://127.0.0.1:8899/overview</td><td>视觉 NG → Edge Core → MES 回执 → UA 确认</td></tr>
    <tr><td>3</td><td>http://127.0.0.1:8899/joint</td><td>设备停机/温度异常、OEE 损失与 RCA 候选</td></tr>
    <tr><td>备用</td><td>http://127.0.0.1:8899/</td><td>主演示：超时、重启、死信、人工重放</td></tr>
  </table></div>

  <div class="section"><h2>5. 本轮已经跑通的证据</h2><div class="cols3">
    <div class="box good"><span class="tag good">主演示</span><h3>DONE</h3><p>9 步状态迁移；最终回执 `MES-JX-DEMO-0001`；UA 写回确认。</p></div>
    <div class="box good"><span class="tag good">视觉 + 联合</span><h3>DONE</h3><p>NG 输入质量门禁 PASS；OEE 约 0.51；设备上下文与质量事件对齐。</p></div>
    <div class="box good"><span class="tag good">项目总览</span><h3>DONE</h3><p>总览演示 OEE 约 0.57；RCA 候选为温度异常，明确非因果证明。</p></div>
  </div></div>

  <div class="note"><b>今天不要临场加新功能。</b>演示前只做拉取、依赖检查、启动和试跑。任何临时改动都可能让明天现场多一个变量。</div>
  <div class="footer"><span>UA_LinqInspect · 投资人演示操作卡</span><span>01 / 02 · 启动与检查</span></div>
</section>

<section class="page">
  <div class="top"><div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>投资人演示操作卡 · 02</span></div></div><div class="meta"><b>10 分钟</b><br>讲解顺序与边界</div></div></div>
  <div class="title"><h1>按顺序讲，不要跳步</h1><span>问题 → 证据 → 边界 → 下一步</span></div>

  <div class="section"><h2>6. 10 分钟讲解顺序</h2><table class="tight">
    <tr><th style="width:17%">时间</th><th style="width:20%">页面</th><th>说什么 / 做什么</th></tr>
    <tr><td>0:00–1:00</td><td>开场</td><td>工厂不缺“看见缺陷”的模型，缺的是缺陷被发现之后，结果能不能可靠进入 MES；MES 超时、断网、重启会造成漏单、重复开单和人工对账。</td></tr>
    <tr><td>1:00–3:00</td><td>/vision</td><td>选 NG 样本；指出图像质量门禁、缺陷类别、置信度、概率分布；打开模型卡，主动说明合成数据和低置信度转人工。</td></tr>
    <tr><td>3:00–7:00</td><td>/overview</td><td>点击“现场运行演示”；说明这是本地视觉模型 + 真实 Edge Core API/SQLite/Worker + Mock PLC，不是视频；结果出来按 DONE → 回执 → OEE → RCA → 审计讲。</td></tr>
    <tr><td>7:00–9:00</td><td>/joint</td><td>只讲三件事：视觉 NG 与设备停机/温度异常同时间窗；OEE 损失可下钻；两个项目通过业务键、correlationId 和时间线关联。</td></tr>
    <tr><td>9:00–10:00</td><td>收束</td><td>主动讲边界：没有真实相机、客户样本、真实 PLC/MES；下一步是单站付费验证，不是直接铺整线。</td></tr>
  </table></div>

  <div class="section"><h2>7. 结果页必须指出的五个点</h2><div class="cols">
    <div class="box info"><h3>1. DONE</h3><p>只有 MES 回执和 OPC UA 写回都确认，任务才结束。不是“HTTP 200 就算成功”。</p></div>
    <div class="box info"><h3>2. 回执</h3><p>`MES-JX-DEMO-0001`：同一个业务键不会重复形成业务结果；系统是 at-least-once + 业务幂等。</p></div>
    <div class="box info"><h3>3. OEE</h3><p>Availability / Performance / Quality 都能下钻到来源，不是一张漂亮的总分截图。</p></div>
    <div class="box info"><h3>4. RCA</h3><p>`TEMPERATURE_SPIKE` 是同一时间窗的候选原因，有时间证据，但不是因果证明。</p></div>
    <div class="box info"><h3>5. 审计</h3><p>失败、重试、死信、重启、人工重放都有轨迹；correlationId 能串起质量、交付和设备上下文。</p></div>
    <div class="box bad"><h3>不要说</h3><p>“生产级”“零漏检”“exactly-once”“已经对接客户 MES”“合成准确率等于现场精度”。</p></div>
  </div></div>

  <div class="section"><h2>8. 现场失败时的切换</h2><div class="cols">
    <div class="box bad"><h3>2 分钟内修不好</h3><ul><li>换端口：`--port 8900`</li><li>检查同级设备上下文仓库和 `node_modules`</li><li>检查 `node`、`numpy`、`Pillow`</li><li>看终端 Mock PLC / OPC UA 错误</li></ul></div>
    <div class="box good"><h3>直接切备用证据</h3><ul><li>`01_项目总览与现场演示.pdf`</li><li>`02_AI质检最小闭环.pdf`</li><li>`04_演示证据/05_overview_complete.png`</li><li>`04_演示证据/07_joint_dashboard.png`</li></ul></div>
  </div></div>

  <div class="section"><h2>9. 可能被追问的四个问题</h2><div class="cols">
    <div class="box info"><h3>为什么不是又一个视觉公司？</h3><p>视觉是入口；我们做的是缺陷结果进入 MES 的可靠交付、回执对账和设备上下文，可以接不同视觉前端。</p></div>
    <div class="box info"><h3>没有客户怎么证明？</h3><p>今天证明软件底座和故障闭环；下一步用付费单站试点验证真实样本和 MES 沙箱，不拿演示冒充需求。</p></div>
    <div class="box info"><h3>为什么现在投？</h3><p>用 1～2 万小额验证资金锁定真实相机、样件和 MES 沙箱，低成本拿到可复现结果后再决定规模化。</p></div>
    <div class="box info"><h3>钱怎么花？</h3><p>相机/镜头/光源/工装、边缘算力盒、真实样本采集、MES 联调和现场验收；不铺整线、不压库存。</p></div>
  </div></div>

  <div class="note"><b>收尾话术：</b>“今天证明的是软件底座和故障闭环，不是生产视觉精度。下一步用一笔小额验证资金，拿一台工业相机、300 件真实样件和一个 MES 沙箱，跑通单站验收，再决定是否扩站。”</div>
  <div class="footer"><span>UA_LinqInspect · 投资人演示操作卡</span><span>02 / 02 · 讲解与边界</span></div>
</section>

</body>
</html>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(HTML, encoding="utf-8")
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        raise SystemExit("Chrome/Chromium not found; HTML was generated but PDF was not rendered")
    subprocess.run(
        [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=2000",
            f"--print-to-pdf={PDF_PATH}",
            HTML_PATH.resolve().as_uri(),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(HTML_PATH)
    print(PDF_PATH)


if __name__ == "__main__":
    main()
