#!/usr/bin/env python3
"""Render the one-page small validation budget request."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "investor-deck"
HTML_PATH = OUT / "小额验证资金申请_一页版.html"
PDF_PATH = OUT / "小额验证资金申请_一页版.pdf"


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>小额验证资金申请 · 一页版</title>
<style>
@page { size: A4; margin: 0; }
:root{--ink:#17202a;--muted:#5e6d7b;--line:#d9e1e8;--navy:#0d2234;--gold:#b4771c;--goldbg:#fcf5e6;--mint:#17795a;--mintbg:#eef9f4;--red:#a5403b;--redbg:#fdf0ef;--blue:#1d5f8a;--bluebg:#eef6fc}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:var(--ink)}
body{font-family:"Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",sans-serif;line-height:1.5}
.page{width:210mm;height:297mm;padding:9mm 13mm 7mm;background:#fff;overflow:hidden;position:relative}
.top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid var(--navy);padding-bottom:4mm}
.brand{display:flex;gap:3mm;align-items:center}
.mark{width:9.5mm;height:9.5mm;border-radius:2.5mm;display:grid;place-items:center;background:linear-gradient(135deg,#e5b45f,#c9892b);color:#17202a;font-weight:900;font-size:8.5pt}
.brand b{display:block;font-size:10.5pt;letter-spacing:.02em}
.brand span{display:block;font-size:7pt;color:var(--muted);margin-top:.7mm}
.meta{text-align:right;font-size:7pt;color:var(--muted);line-height:1.65}
.meta b{color:var(--gold);font-size:8pt}
.hero{display:grid;grid-template-columns:1.28fr .72fr;gap:5mm;margin-top:4mm;align-items:start}
.eyebrow{color:var(--gold);font-size:7pt;font-weight:900;letter-spacing:.15em;margin-bottom:1.5mm}
h1{font-size:20pt;line-height:1.08;letter-spacing:-.04em;margin:0 0 2mm}
h1 em{font-style:normal;color:var(--gold)}
.lede{font-size:8.5pt;line-height:1.65;color:#3e4d5a;margin:0}
.callout{border:1px solid #e2c58f;background:var(--goldbg);border-radius:3mm;padding:3mm;font-size:7.8pt;line-height:1.6;color:#5b431d}
.callout b{display:block;font-size:10pt;color:#3e2d12;margin-bottom:1.5mm}
.section{margin-top:3.6mm}
.section-title{font-size:9.8pt;margin:0 0 1.9mm;padding-left:3mm;border-left:3px solid var(--gold);line-height:1.2}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:2.5mm}
.step{border:1px solid var(--line);border-radius:2.5mm;padding:2.3mm;background:#fbfcfd;min-height:20mm}
.step b{display:block;font-size:7pt;color:var(--gold);letter-spacing:.08em;margin-bottom:1.2mm}
.step h3{font-size:9pt;margin:0 0 1.2mm}
.step p{font-size:6.9pt;line-height:1.5;color:#455463;margin:0}
.step .gate{display:inline-block;margin-top:1mm;font-size:6.4pt;color:var(--mint);background:var(--mintbg);border:1px solid #bfe1d3;border-radius:1.5mm;padding:.5mm 1.3mm}
.two-col{display:grid;grid-template-columns:1.15fr .85fr;gap:3mm}
table{width:100%;border-collapse:collapse;font-size:6.8pt}
th{background:#0d2234;color:#fff;text-align:left;padding:1.3mm 1.8mm;font-weight:700}
td{border-bottom:1px solid var(--line);padding:1.15mm 1.8mm;color:#435260;vertical-align:top}
td:last-child{text-align:right;white-space:nowrap;color:#6b4c1c;font-weight:700}
tr:nth-child(even) td{background:#fafcfd}
.manage{border:1px solid #b9dfd0;background:var(--mintbg);border-radius:2.5mm;padding:2.4mm 3mm;font-size:6.8pt;line-height:1.55;color:#245c49}
.manage b{display:block;color:#104b39;font-size:8.2pt;margin-bottom:1mm}
.accept{display:grid;grid-template-columns:repeat(3,1fr);gap:2mm}
.accept div{border:1px solid #c7dceb;background:var(--bluebg);border-radius:2mm;padding:1.8mm 2.1mm;font-size:6.6pt;line-height:1.45;color:#2d5874}
.accept b{display:block;color:#174c6d;font-size:7.6pt;margin-bottom:.6mm}
.promises{display:grid;grid-template-columns:repeat(5,1fr);gap:1.5mm}
.promise{border:1px solid var(--line);border-radius:2mm;padding:1.8mm;background:#fff;font-size:6.4pt;line-height:1.45;color:#455463}
.promise b{display:block;color:var(--gold);font-size:7.4pt;margin-bottom:.6mm}
.quote{margin-top:2.4mm;border-left:3px solid var(--gold);background:#fcfaf5;border-radius:0 2mm 2mm 0;padding:2.2mm 3mm;font-size:7.1pt;line-height:1.55;color:#493a23}
.quote b{color:#2f2414}
.bottom{display:grid;grid-template-columns:1.2fr .8fr;gap:3mm;margin-top:2.4mm}
.next{border:1px solid #e3b7b3;background:var(--redbg);border-radius:2.5mm;padding:2.2mm 3mm;font-size:6.6pt;line-height:1.5;color:#71302c}
.next b{color:#4e211e}
.ask{border:1px solid #d9b56c;background:linear-gradient(135deg,#fff9ec,#fdf2d9);border-radius:2.5mm;padding:2.2mm 3mm;font-size:6.6pt;line-height:1.5;color:#5c441d}
.ask b{display:block;color:#4c3512;font-size:9.5pt;margin-bottom:.8mm}
.footer{margin-top:1.5mm;border-top:1px solid var(--line);padding-top:1.2mm;display:flex;justify-content:space-between;font-size:6.2pt;color:#8a97a3}
</style>
</head>
<body>
<div class="page">
  <div class="top">
    <div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>工业 AI 质检与质量交付底座</span></div></div>
    <div class="meta"><b>小额验证资金申请 · v0.1</b><br>2026-11-22 · 单工位真实闭环</div>
  </div>

  <div class="hero">
    <div>
      <div class="eyebrow">SMALL VALIDATION BUDGET</div>
      <h1>先验证能力，<br>再谈 <em>1～2 万</em></h1>
      <p class="lede">第一次见面不讲 BP，您指定一个故障场景，我现场跑给您看。能力经得起核验后，再申请首笔 1～2 万元，只买一台适配产线的工业相机、一个边缘算力盒和必要的光源线缆，补真实工位的质检-MES 证据。</p>
    </div>
    <div class="callout"><b>先锁现场，再买硬件</b>MES 超时、断网、重启、重复上报，您任选一项现场跑；同时先锁定一个提供工位、样件和 MES 接口的工厂。拿不到现场，不买硬件；经得起核验，再谈 1～2 万预算。</div>
  </div>

  <div class="section">
    <h2 class="section-title">分阶段拨款，不一次给满</h2>
    <div class="steps">
      <div class="step"><b>STAGE 0 · 0 元</b><h3>现场确认</h3><p>1～2 天确认一个产品族、一个工位、一类缺陷、一个 MES 接口、资料责任人和验收人。</p><span class="gate">条件不成立就不花钱</span></div>
      <div class="step"><b>STAGE 1 · 1～2 万</b><h3>台架/离线验证</h3><p>相机、镜头/光源、边缘盒到位；对约定样本采集、门禁、推理，并跑通入队、重试、死信和回执。</p><span class="gate">达标后再申请追加</span></div>
      <div class="step"><b>STAGE 2 · 1～3 万</b><h3>真实单站闭环</h3><p>接入一个真实工位和一个 MES 测试接口/沙箱；做断网、超时、重启、重复消息演练并验收。</p><span class="gate">不达标即停止</span></div>
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">钱花在哪里</h2>
    <div class="two-col">
      <table><tr><th>项目</th><th>预算假设</th></tr><tr><td>高清工业相机（一台）</td><td>3,000～12,000 元</td></tr><tr><td>镜头、光源、支架、线缆</td><td>2,000～8,000 元</td></tr><tr><td>边缘算力盒</td><td>3,000～10,000 元</td></tr><tr><td>现场安装与耗材</td><td>1,000～5,000 元</td></tr><tr><td>不可预见费（需确认）</td><td>1,000～5,000 元</td></tr><tr><td><b>合计</b></td><td><b>约 1～4 万元</b></td></tr></table>
      <div class="manage"><b>资金管理承诺</b>专款专用；发票、照片、资产编号和台账齐全；不用于工资、招待或无关订阅；能借则借、能租则租；未使用余额退回；硬件归属、保管人和处置方式在付款前写清。</div>
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">什么才算跑通</h2>
    <div class="accept">
      <div><b>场景固定</b>一个产品族、一个工位、一类缺陷、一个 MES 接口。</div>
      <div><b>样本可复现</b>不少于 300 件或覆盖 3 个班次，具体由现场确认。</div>
      <div><b>视觉可测量</b>类别级 Precision/Recall、图像质量门禁通过率、人工复核比例。</div>
      <div><b>交付可对账</b>NG 投递成功率、重复业务单数量、MES 回执对账一致率。</div>
      <div><b>故障可恢复</b>断网、MES 超时、进程重启后的恢复时间和数据完整性。</div>
      <div><b>证据可核验</b>原始日志、业务键、回执号、视频、模型版本和测试命令。</div>
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">让对方放心的五个承诺</h2>
    <div class="promises">
      <div class="promise"><b>先让您出题</b>MES 超时、断网、重启、重复上报，现场跑给您看。</div>
      <div class="promise"><b>首笔小额</b>只申请 1～2 万，不一次给满，不铺整线。</div>
      <div class="promise"><b>随时可停</b>每个阶段有停止条件，不达标不追加。</div>
      <div class="promise"><b>资产可盘</b>相机、边缘盒、发票和资产清单归对方核对。</div>
      <div class="promise"><b>原始可查</b>周报、日志、视频和测试命令，不只发总结。</div>
    </div>
  </div>

  <div class="quote"><b>第一次见面：</b>“您先不用判断要不要投这个项目。您只需要判断：这个质检到 MES 的痛点是否值得花 1～2 万验证；这笔钱是否花得透明；如果失败，能不能及时停。如果现场没有 MES 接口、没有数据、没有负责人，我会建议不花这笔钱。”</div>

  <div class="bottom">
    <div class="next"><b>最坏情况：</b>现场图像质量不达标、MES 接口拿不到或客户不配合，导致验证无法完成。出现这种情况，停止追加，未使用余额退回，硬件保留，并提交失败原因报告。损失是可控的验证成本，不是无底洞。</div>
    <div class="ask"><b>本次申请：首笔 1～2 万元</b>先锁定现场，再采购一台相机和一个边缘盒；阶段 1 达标后追加至 3～5 万元，验证成功后再谈规模化合作与股权条款。</div>
  </div>

  <div class="footer"><span>UA_LinqInspect · 软件先行 PoC+ · 当前无客户案例</span><span>预算、周期与验收目标为待确认假设，以双方书面确认为准</span></div>
</div>
</body>
</html>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
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
