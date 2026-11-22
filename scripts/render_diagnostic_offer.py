#!/usr/bin/env python3
"""Render the one-page paid diagnostic offer."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "investor-deck"
HTML_PATH = OUT / "质检-MES对账诊断_一页报价.html"
PDF_PATH = OUT / "质检-MES对账诊断_一页报价.pdf"


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>质检-MES 对账诊断 · 一页报价</title>
<style>
@page { size: A4; margin: 0; }
:root{--ink:#17202a;--muted:#5d6c7b;--line:#d8e0e7;--navy:#0d2234;--gold:#b77b20;--goldbg:#fbf4e5;--mint:#167a5b;--mintbg:#eef9f4;--red:#a5403b;--redbg:#fdf0ef}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:var(--ink)}
body{font-family:"Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",sans-serif;line-height:1.55}
.page{width:210mm;height:297mm;padding:12mm 14mm 10mm;background:#fff;overflow:hidden;position:relative}
.top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid var(--navy);padding-bottom:5mm}
.brand{display:flex;gap:3mm;align-items:center}
.mark{width:10mm;height:10mm;border-radius:2.5mm;display:grid;place-items:center;background:linear-gradient(135deg,#e4b45e,#c8892c);color:#17202a;font-weight:900;font-size:9pt}
.brand b{display:block;font-size:11pt;letter-spacing:.02em}
.brand span{display:block;font-size:7.5pt;color:var(--muted);margin-top:1mm}
.meta{text-align:right;font-size:7.5pt;color:var(--muted);line-height:1.7}
.meta b{color:var(--gold);font-size:8.5pt}
.hero{margin-top:7mm;display:grid;grid-template-columns:1.25fr .75fr;gap:6mm;align-items:start}
.eyebrow{color:var(--gold);font-size:7.5pt;font-weight:900;letter-spacing:.16em;margin-bottom:2mm}
h1{font-size:24pt;line-height:1.1;letter-spacing:-.04em;margin:0 0 3mm}
h1 em{font-style:normal;color:var(--gold)}
.lede{font-size:9.5pt;line-height:1.8;color:#3f4e5c;margin:0}
.promise{border:1px solid #e2c58f;background:var(--goldbg);border-radius:3mm;padding:5mm;font-size:9pt;line-height:1.75;color:#5a421d}
.promise b{display:block;font-size:11pt;color:#3f2e12;margin-bottom:2mm}
.section{margin-top:6mm}
.section-title{font-size:11pt;margin:0 0 3mm;padding-left:3mm;border-left:3px solid var(--gold);line-height:1.25}
.grid3{display:grid;grid-template-columns:1.15fr .95fr .9fr;gap:4mm}
.box{border:1px solid var(--line);border-radius:3mm;padding:4mm;background:#fbfcfd}
.box h3{font-size:9.5pt;margin:0 0 2.5mm;color:var(--navy)}
.box ul{margin:0;padding-left:4mm;font-size:8.2pt;line-height:1.8;color:#455463}
.box li::marker{color:var(--gold)}
.box p{font-size:8.2pt;line-height:1.75;color:#455463;margin:0}
.timeline{display:grid;grid-template-columns:repeat(5,1fr);gap:2mm;margin-top:2mm}
.step{position:relative;border:1px solid var(--line);border-radius:2.5mm;padding:3mm;min-height:22mm;background:#fff}
.step:after{content:"›";position:absolute;right:-1.5mm;top:8mm;color:var(--gold);font-size:14pt;font-weight:900;background:#fff;line-height:1}
.step:last-child:after{content:""}
.step b{display:block;font-size:7.2pt;color:var(--gold);letter-spacing:.08em;margin-bottom:1.5mm}
.step span{display:block;font-size:8pt;line-height:1.55;color:#3f4e5c}
.price{display:grid;grid-template-columns:1fr 1fr;gap:4mm;margin-top:3mm}
.price-main{border:1px solid #d9b56c;background:linear-gradient(135deg,#fff9ec,#fdf2d9);border-radius:3mm;padding:4.5mm 5mm}
.price-main .label{font-size:7.5pt;color:var(--gold);font-weight:900;letter-spacing:.12em}
.price-main .amount{font-size:25pt;font-weight:900;color:#4c3512;line-height:1.1;margin:1.5mm 0 1mm}
.price-main .sub{font-size:8pt;color:#6a5128}
.terms{border:1px solid var(--line);border-radius:3mm;padding:4mm 4.5mm;background:#fbfcfd;font-size:8.2pt;line-height:1.8;color:#455463}
.terms b{color:var(--navy)}
.bottom{display:grid;grid-template-columns:1.25fr .75fr;gap:4mm;margin-top:3mm}
.note{border:1px solid #b9dfd0;background:var(--mintbg);border-radius:3mm;padding:3.5mm 4mm;font-size:8.2pt;line-height:1.7;color:#245c49}
.note b{color:#104b39}
.boundary{border:1px solid #e3b7b3;background:var(--redbg);border-radius:3mm;padding:3.5mm 4mm;font-size:7.8pt;line-height:1.7;color:#71302c}
.next{margin-top:4mm;border-top:1px solid var(--line);padding-top:3mm;display:flex;justify-content:space-between;gap:5mm;font-size:8pt;color:var(--muted)}
.next b{color:var(--ink)}
.footer{margin-top:2.5mm;font-size:7pt;color:#8a97a3;display:flex;justify-content:space-between}
</style>
</head>
<body>
<div class="page">
  <div class="top">
    <div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>工业 AI 质检与质量交付底座</span></div></div>
    <div class="meta"><b>对外报价 · v0.1</b><br>2026-11-22 · 适用于单工位诊断</div>
  </div>

  <div class="hero">
    <div>
      <div class="eyebrow">QUALITY-MES RECONCILIATION DIAGNOSTIC</div>
      <h1>质检-MES<br><em>对账诊断</em></h1>
      <p class="lede">不卖免费 POC。先用一笔小钱，把质检结果进入 MES 的断点、接口风险和对账问题看清楚，再决定是否值得做单站试点。</p>
    </div>
    <div class="promise"><b>10 个工作日，拿到五个答案</b>现状图 · 断点清单 · MES 契约体检 · 单站 ROI 测算 · Go/No-Go 建议书。诊断只对资料和证据负责，不承诺生产改造效果。</div>
  </div>

  <div class="section">
    <h2 class="section-title">你买到什么</h2>
    <div class="grid3">
      <div class="box"><h3>五项交付物</h3><ul><li>质检到 MES 的完整流程图与责任人</li><li>漏单、重单、超时、重启风险清单</li><li>MES 字段、幂等键、回执与错误分类体检</li><li>单站 ROI 测算表与关键假设</li><li>试点范围、验收指标和人天建议</li></ul></div>
      <div class="box"><h3>适合谁</h3><ul><li>已有 MES/ERP，质检仍靠人工或 Excel</li><li>出现过 NG 漏单、重复开单或月底对账困难</li><li>有一个明确工位、产品族和 MES 接口</li><li>质量/IT 负责人愿意参加访谈与评审</li></ul></div>
      <div class="box"><h3>不包含</h3><ul><li>生产系统改造、MES/PLC 开发</li><li>相机、光源、工装选型</li><li>生产级 SLA 或模型精度承诺</li><li>无限访谈、免费驻场和无限改稿</li></ul></div>
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">10 个工作日怎么走</h2>
    <div class="timeline">
      <div class="step"><b>D1</b><span>30～60 分钟访谈，确认工位、系统、资料责任人和决策人。</span></div>
      <div class="step"><b>D2～D5</b><span>收集脱敏流程图、接口文档、字段样例、故障记录和对账样本。</span></div>
      <div class="step"><b>D3～D7</b><span>检查 MES 契约、幂等键、回执、重试、死信和对账路径。</span></div>
      <div class="step"><b>D6～D8</b><span>建立单站 ROI 假设，区分已证实数据与待验证假设。</span></div>
      <div class="step"><b>D9～D10</b><span>交付报告并评审：是否进入试点、范围、验收指标和预估人天。</span></div>
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">价格与交付</h2>
    <div class="price">
      <div class="price-main"><div class="label">诊断价格假设</div><div class="amount">1～3 万元</div><div class="sub">按工位数量、接口复杂度和资料完整度报价</div></div>
      <div class="terms"><b>周期：</b>资料齐备后 10 个工作日<br><b>付款：</b>启动 50%，评审交付 50%<br><b>形式：</b>PDF 报告 + 60 分钟评审 + 一次书面修订<br><b>差旅：</b>如需现场，实报实销或另行报价</div>
    </div>
  </div>

  <div class="bottom">
    <div class="note"><b>为什么先诊断：</b>免费 POC 容易变成无限定制，客户没有决策压力，团队也拿不到可复用证据。诊断把风险先看清楚，双方在进入试点前明确范围、验收和责任边界。</div>
    <div class="boundary"><b>诚实边界：</b>诊断不是生产验收，不替代客户内控或审计；不编客户案例，不把口头兴趣、免费试用或诊断意向写成收入。</div>
  </div>

  <div class="next"><span><b>下一步：</b>确认一个工位、一个 MES 接口、一个资料责任人，约定 10 个工作日后的评审时间。</span><span><b>诊断通过后再谈：</b>单站付费试点 5～15 万元 / 4～6 周。</span></div>
  <div class="footer"><span>UA_LinqInspect · 软件先行 PoC+ · 当前无客户案例</span><span>价格与周期为验证假设，以双方确认的范围为准</span></div>
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
