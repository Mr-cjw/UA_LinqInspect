#!/usr/bin/env python3
"""Render the investor deck from the current project evidence."""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "investor-deck"
ASSETS = OUT / "assets"
HTML_PATH = OUT / "UA_LinqInspect_投资人路演版.html"
PDF_PATH = OUT / "UA_LinqInspect_投资人路演版.pdf"


def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def crop_evidence(source: Path, target: Path) -> None:
    image = Image.open(source).convert("RGB")
    width, height = image.size
    crop_height = min(height, int(width * 0.583))
    image.crop((0, 0, width, crop_height)).resize((1200, 700), Image.Resampling.LANCZOS).save(target)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>UA_LinqInspect · 投资人路演版</title>
<style>
@page { size: 13.333in 7.5in; margin: 0; }
:root{
  --bg:#08111b; --bg2:#0c1a28; --ink:#f7f3ea; --muted:#a9bac9;
  --line:#294257; --gold:#e5b45f; --gold2:#f3d394; --mint:#56d3a2;
  --red:#ef746b; --blue:#7cb9e8; --panel:#102233; --panel2:#0b1b2a;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink)}
body{width:1280px;font-family:"Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",sans-serif;line-height:1.45}
.slide{
  position:relative;width:1280px;height:720px;overflow:hidden;padding:54px 70px 44px;
  background:
    radial-gradient(circle at 88% -8%,#5b3b1f 0,transparent 30%),
    radial-gradient(circle at 4% 4%,#143b59 0,transparent 34%),
    linear-gradient(155deg,var(--bg),#07101a 68%,#091521);
  break-after:page;page-break-after:always;
}
.slide:last-child{break-after:auto;page-break-after:auto}
.slide:before{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(90deg,#ffffff05 1px,transparent 1px),linear-gradient(#ffffff04 1px,transparent 1px);background-size:56px 56px;opacity:.32}
.topbar,.footer,.content{position:relative;z-index:1}
.topbar{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #ffffff1c;padding-bottom:13px;font-size:11px;letter-spacing:.08em;color:#c7d5e2}
.brand{display:flex;align-items:center;gap:10px;font-weight:900;letter-spacing:.02em}
.mark{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--gold),var(--gold2));color:#12202a;font-size:12px;font-weight:900}
.page{color:var(--gold);font-weight:900}
.footer{position:absolute;left:70px;right:70px;bottom:20px;display:flex;justify-content:space-between;border-top:1px solid #ffffff14;padding-top:10px;color:#73879a;font-size:10px;letter-spacing:.04em}
.content{padding-top:30px;height:570px}
.eyebrow{color:var(--gold);font-size:11px;font-weight:900;letter-spacing:.2em;text-transform:uppercase;margin-bottom:14px}
h1,h2,h3,p{margin-top:0}
h1{font-size:62px;line-height:1.03;letter-spacing:-.055em;margin-bottom:22px;font-weight:900}
h1 em{font-style:normal;color:var(--gold2)}
h2{font-size:38px;line-height:1.14;letter-spacing:-.04em;margin-bottom:12px;font-weight:900}
h3{font-size:16px;line-height:1.3;margin-bottom:7px;font-weight:900}
.lede{font-size:17px;line-height:1.85;color:#c8d6e2;max-width:760px}
.muted{color:var(--muted)}
.gold{color:var(--gold)}.mint{color:var(--mint)}.red{color:var(--red)}.blue{color:var(--blue)}
.chips{display:flex;gap:9px;flex-wrap:wrap;margin-top:25px}
.chip{border:1px solid #3d566b;background:#0d2030cc;border-radius:99px;padding:7px 11px;font-size:11px;font-weight:800;color:#d7e4ef}
.chip.gold{border-color:#72562c;background:#2a2114;color:var(--gold2)}
.cover-grid{display:grid;grid-template-columns:1.32fr .68fr;gap:48px;align-items:center;height:100%}
.cover-panel{border:1px solid #66502d;border-radius:20px;padding:24px;background:linear-gradient(160deg,#1c2a33f2,#0d1b28f2);box-shadow:0 24px 70px #00000045}
.panel-label{font-size:10px;letter-spacing:.18em;color:var(--gold);font-weight:900;margin-bottom:10px}
.big{font-size:26px;line-height:1.28;font-weight:900;margin-bottom:15px}
.mini{font-size:12px;line-height:1.75;color:var(--muted);border-top:1px solid #ffffff16;padding-top:14px;margin-bottom:14px}
.metric{display:flex;justify-content:space-between;gap:14px;border-top:1px solid #ffffff12;padding:10px 0;font-size:12px;color:var(--muted)}
.metric b{font-size:16px;color:var(--ink)}
.metric b.mint{color:var(--mint)}.metric b.gold{color:var(--gold)}
.quote{border-left:4px solid var(--gold);padding:17px 20px;background:#1a1a14aa;border-radius:0 14px 14px 0;font-size:18px;line-height:1.7;color:#f3e7cf;margin:20px 0}
.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0 18px}
.flow-step{position:relative;border:1px solid #38536a;border-radius:14px;padding:16px 15px;background:linear-gradient(180deg,#122738,#0d1c2a);min-height:108px}
.flow-step:after{content:"→";position:absolute;right:-13px;top:38px;color:var(--gold);font-size:18px;font-weight:900;z-index:2}
.flow-step:last-child:after{content:""}
.flow-step .n{font-size:10px;color:var(--gold);font-weight:900;letter-spacing:.13em;margin-bottom:8px}
.flow-step p{font-size:12px;line-height:1.65;color:var(--muted);margin:0}
.breakline{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
.break{font-size:11px;color:var(--red);border-top:1px dashed #7b3b38;padding-top:7px}
.break span{display:inline-block;background:#2b1719;border:1px solid #5d302f;border-radius:6px;padding:3px 7px;margin-right:4px}
.three{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}
.card{border:1px solid var(--line);border-radius:16px;padding:19px;background:linear-gradient(160deg,#122637,#0c1a27);min-height:150px}
.card .num{font-size:11px;color:var(--gold);font-weight:900;letter-spacing:.14em;margin-bottom:14px}
.card p{font-size:12px;line-height:1.7;color:var(--muted);margin:0}
.card.highlight{border-color:#72562d;background:linear-gradient(160deg,#2a2418,#121f2b)}
.callout{margin-top:18px;border:1px solid #2c5a4a;background:#10271f;border-radius:12px;padding:13px 16px;color:#bcebd7;font-size:13px}
.callout b{color:var(--mint)}
.shots{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:18px}
.shot{border:1px solid #355166;border-radius:14px;overflow:hidden;background:#0a1722}
.shot img{display:block;width:100%;height:252px;object-fit:cover;object-position:top}
.shot .cap{display:flex;justify-content:space-between;gap:10px;padding:9px 12px;font-size:10px;color:#a9bac9;border-top:1px solid #ffffff12}
.shot .cap b{color:var(--gold)}
.proof-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}
.proof{border:1px solid #2d465a;border-radius:11px;padding:10px 12px;background:#0c1d2b}
.proof b{display:block;font-size:19px;color:var(--gold2);margin-bottom:2px}
.proof span{font-size:10px;color:var(--muted)}
.boundary{margin-top:12px;border:1px solid #68413e;background:#25181a;border-radius:10px;padding:9px 12px;color:#e6b8b4;font-size:11px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.list{margin:0;padding-left:18px;color:#c8d6e2;font-size:13px;line-height:1.9}
.list li::marker{color:var(--gold)}
.mini-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0 22px}
.mini-card{border:1px solid #314b60;border-radius:12px;padding:12px;background:#0d2030;min-height:82px}
.mini-card b{display:block;font-size:13px;margin-bottom:4px;color:#e7eff5}
.mini-card span{font-size:10px;color:var(--muted);line-height:1.55}
.ladder{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}
.step{position:relative;border:1px solid #3a566d;border-radius:13px;padding:15px 13px;background:linear-gradient(180deg,#12283a,#0d1c29);min-height:150px}
.step:after{content:"›";position:absolute;right:-11px;top:48px;color:var(--gold);font-size:25px;font-weight:900}
.step:last-child:after{content:""}
.step .tag{font-size:10px;color:var(--gold);font-weight:900;letter-spacing:.12em;margin-bottom:9px}
.step h3{font-size:15px;margin-bottom:5px}
.step .price{font-size:21px;color:var(--gold2);font-weight:900;margin:7px 0 4px}
.step p{font-size:10px;color:var(--muted);line-height:1.55;margin:0}
.economics{display:grid;grid-template-columns:1.1fr .9fr;gap:16px;margin-top:18px}
.econ-box{border:1px solid #2f4b60;border-radius:12px;padding:13px 15px;background:#0b1b29;font-size:11px;line-height:1.7;color:var(--muted)}
.econ-box b{color:var(--ink)}
.timeline{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:20px}
.week{border:1px solid #345167;border-radius:13px;padding:14px;background:#0d1f2e;min-height:210px}
.week .w{font-size:10px;color:var(--gold);font-weight:900;letter-spacing:.12em;margin-bottom:8px}
.week h3{font-size:15px;margin-bottom:8px}
.week ul{margin:0;padding-left:15px;color:var(--muted);font-size:11px;line-height:1.75}
.week li::marker{color:var(--gold)}
.gate{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:18px}
.gate-box{border:1px solid #2d5c4b;background:#10271f;border-radius:11px;padding:12px 14px;font-size:11px;line-height:1.65;color:#bcebd7}
.gate-box.stop{border-color:#6d3a38;background:#271719;color:#e9b5b1}
.gate-box b{display:block;margin-bottom:4px}
.matrix{width:100%;border-collapse:separate;border-spacing:0 6px;font-size:11px}
.matrix th{text-align:left;color:var(--gold);font-size:10px;letter-spacing:.1em;padding:0 10px 4px}
.matrix td{background:#0e2030;border-top:1px solid #2e4b61;border-bottom:1px solid #2e4b61;padding:9px 10px;color:#b9cad8;vertical-align:top}
.matrix td:first-child{color:#f1e5cf;font-weight:900;border-left:1px solid #2e4b61;border-radius:9px 0 0 9px}
.matrix td:last-child{border-right:1px solid #2e4b61;border-radius:0 9px 9px 0}
.moat{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}
.moat div{border:1px solid #3a566c;border-radius:11px;padding:11px 12px;background:#0c1d2b;font-size:10px;line-height:1.55;color:var(--muted)}
.moat b{display:block;color:var(--gold2);font-size:12px;margin-bottom:4px}
.risk-grid{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-top:16px}
.risk{border:1px solid #3c566b;border-radius:12px;padding:13px 15px;background:#0d1e2d}
.risk b{display:block;color:#f2e5cf;font-size:13px;margin-bottom:4px}
.risk p{font-size:10px;line-height:1.65;color:var(--muted);margin:0}
.ask-grid{display:grid;grid-template-columns:.9fr 1.1fr;gap:22px;margin-top:18px}
.ask-box{border:1px solid #7a5b2c;border-radius:16px;padding:20px;background:linear-gradient(160deg,#2b2417,#111f2b)}
.ask-box .amount{font-size:42px;line-height:1;color:var(--gold2);font-weight:900;letter-spacing:-.03em;margin:8px 0 6px}
.ask-box .sub{font-size:11px;color:var(--muted);margin-bottom:16px}
.use-row{display:flex;align-items:center;gap:8px;font-size:10px;color:#d6e2ec;margin:8px 0}
.bar{height:7px;border-radius:99px;background:#263c4d;overflow:hidden;flex:1}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--gold),var(--gold2));border-radius:99px}
.use-row b{width:34px;color:var(--gold);text-align:right}
.use-row span{width:76px;color:var(--muted)}
.milestones{display:grid;gap:8px}
.milestone{display:flex;gap:12px;align-items:flex-start;border:1px solid #315065;border-radius:11px;padding:10px 12px;background:#0c1d2b;font-size:11px;color:#c8d7e3}
.milestone i{display:grid;place-items:center;flex:0 0 22px;height:22px;border-radius:50%;background:#2b2114;color:var(--gold);font-style:normal;font-weight:900;font-size:10px}
.milestone b{color:var(--ink)}
.closing{margin-top:16px;border-top:1px solid #ffffff16;padding-top:12px;display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:10px}
.closing b{color:var(--gold2)}
</style>
</head>
<body>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">UA</span>UA_LinqInspect · 工业 AI 质检与质量交付底座</div><div class="page">01 / 10</div></div>
  <div class="content cover-grid">
    <div>
      <div class="eyebrow">PRE-SEED / ANGEL · SOFTWARE-FIRST</div>
      <h1>让每一张 NG<br><em>不丢、不重、可对账</em></h1>
      <p class="lede">工厂不缺摄像头，也不缺 MES。真正缺的是质检结果进入 MES 的最后一公里，以及发生设备损失时能解释清楚。我们已经把这段底座做成可运行软件，下一步用 90 天买出付费证据。</p>
      <div class="chips"><span class="chip gold">可运行软件</span><span class="chip">本地真实推理</span><span class="chip">682/682 自动回归</span><span class="chip">0 客户案例 · 不编案例</span></div>
    </div>
    <aside class="cover-panel">
      <div class="panel-label">当前阶段 / 诚实边界</div>
      <div class="big">软件已跑通<br><span class="gold">需求待付费验证</span></div>
      <div class="mini">合成数据不是现场精度；本地 MES/PLC 是模拟；还没有真实客户、付费试点或投资承诺。</div>
      <div class="metric"><span>自动回归</span><b class="mint">682 / 682</b></div>
      <div class="metric"><span>视觉入口</span><b>5 类缺陷</b></div>
      <div class="metric"><span>本轮目标</span><b class="gold">3 个付费诊断</b></div>
      <div class="metric"><span>融资用途</span><b>买 90 天证据</b></div>
    </aside>
  </div>
  <div class="footer"><span>UA_LinqInspect · 软件先行 PoC+</span><span>2026-11 · 对外路演版</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">01</span>问题：最后一公里</div><div class="page">02 / 10</div></div>
  <div class="content">
    <div class="eyebrow">THE REAL LEAK</div>
    <h2>一条 NG 被“发现”之后，<br>真正的漏损才开始</h2>
    <div class="quote">质检结果像工厂的一笔“质量应收款”：发现 NG 只是开单，真正容易漏掉的是开单、重复开单和月底对账。</div>
    <div class="flow">
      <div class="flow-step"><div class="n">01 · 发现</div><h3>视觉或人工判 NG</h3><p>模型/质检员给出结果，但结果还停留在工位。</p></div>
      <div class="flow-step"><div class="n">02 · 登记</div><h3>Excel、纸单、微信群</h3><p>换班、断网或忙线时，记录开始依赖人。</p></div>
      <div class="flow-step"><div class="n">03 · 开单</div><h3>MES 超时或重复提交</h3><p>没有幂等和回执，漏单与重单同时出现。</p></div>
      <div class="flow-step"><div class="n">04 · 对账</div><h3>月底靠人找差异</h3><p>质量、设备、生产各有一套时间线，责任说不清。</p></div>
    </div>
    <div class="breakline"><div class="break"><span>漏单</span>NG 没进 MES</div><div class="break"><span>重单</span>同一 NG 开两次</div><div class="break"><span>对不上</span>回执与证据脱节</div><div class="break"><span>说不清</span>设备损失无上下文</div></div>
    <div class="callout"><b>我们切的不是“识别”</b>，而是识别之后的结果交付、回执、审计和设备解释。这个断点越靠近业务，越需要可验收的软件。</div>
  </div>
  <div class="footer"><span>问题定义</span><span>不是又一个 AI 模型</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">02</span>产品：三层底座</div><div class="page">03 / 10</div></div>
  <div class="content">
    <div class="eyebrow">PRODUCT ARCHITECTURE</div>
    <h2>视觉是入口，交付是主线，<br>设备解释是增值</h2>
    <div class="three">
      <div class="card"><div class="num">01 · AI 质检入口</div><h3>先确认图像可用，再判缺陷</h3><p>图像质量门禁、本地模型推理、类别/置信度/概率分布、模型卡。低置信度和不可用图像转人工复核，不自动放行或开单。</p></div>
      <div class="card highlight"><div class="num">02 · 质量交付底座</div><h3>让结果可靠进入 MES</h3><p>标准输入、OPC UA/MES 适配、SQLite Outbox、幂等、重试、死信、人工重放、回执校验与审计。断网、重启、MES 故障都可观察、可恢复。</p></div>
      <div class="card"><div class="num">03 · 设备上下文</div><h3>把质量损失放进设备时间线</h3><p>采集 PLC 状态、产量、温度、报警，给出 OEE 的 A/P/Q 分解和 RCA 候选。RCA 只表示相关性与候选原因，不冒充因果证明。</p></div>
    </div>
    <div class="callout"><b>客户不用推倒现有系统：</b>我们不替代 MES、不替代相机厂商，也不要求一次改造全厂。先在一个工位补上“结果交付 + 回执 + 审计”，再复制到多站。</div>
  </div>
  <div class="footer"><span>产品边界：可运行、可恢复、可验收</span><span>视觉为可选增值</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">03</span>证据：不是概念页</div><div class="page">04 / 10</div></div>
  <div class="content">
    <div class="eyebrow">RUNNABLE EVIDENCE</div>
    <h2>软件已经可以现场运行，<br>证据每次重新生成</h2>
    <div class="shots">
      <div class="shot"><img src="__OVERVIEW_IMG__" alt="项目总览与现场演示"><div class="cap"><b>/overview</b><span>质量闭环 + 设备上下文</span></div></div>
      <div class="shot"><img src="__VISION_IMG__" alt="AI 质检最小闭环"><div class="cap"><b>/vision</b><span>图像质量门禁 + 本地推理</span></div></div>
    </div>
    <div class="proof-row">
      <div class="proof"><b>682 / 682</b><span>当前合并自动回归</span></div>
      <div class="proof"><b>5 类</b><span>OK / 划痕 / 凹坑 / 污渍 / 缺件</span></div>
      <div class="proof"><b>本地</b><span>推理不依赖外部云</span></div>
      <div class="proof"><b>可复跑</b><span>每次生成新业务编号与回执</span></div>
    </div>
    <div class="boundary">边界：训练/验证图像为程序合成，不代表生产精度；本地 MES/PLC 为模拟；尚无客户现场验收。我们只把已发生的证据说成证据。</div>
  </div>
  <div class="footer"><span>演示可复现 · 结果可审计</span><span>不是预录动画</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">04</span>切口：一个工位</div><div class="page">05 / 10</div></div>
  <div class="content">
    <div class="eyebrow">WHY NOW / WHERE TO START</div>
    <h2>MES 已普及，最后一公里<br>仍然靠人补</h2>
    <div class="mini-grid">
      <div class="mini-card"><b>MES 下沉</b><span>中小制造企业已有业务系统，接口和交付成为新瓶颈。</span></div>
      <div class="mini-card"><b>人工变贵</b><span>登记、对账、补录和追溯越来越难靠加班解决。</span></div>
      <div class="mini-card"><b>追溯变严</b><span>客诉、审核和客户要求需要逐笔证据，而不是口头解释。</span></div>
      <div class="mini-card"><b>边缘可用</b><span>本地推理、SQLite 和 OPC UA 已能在低成本设备上运行。</span></div>
    </div>
    <div class="two-col">
      <div><h3 class="gold">理想客户画像</h3><ul class="list"><li>100～1000 人、2～10 个质检工位</li><li>已有 MES/ERP，但质检结果仍靠人工或 Excel</li><li>质量负责人能说出最近一次具体故障</li><li>IT 能提供脱敏接口或测试环境</li><li>愿意先付费诊断，再决定是否试点</li></ul></div>
      <div><h3 class="gold">江西本地切口</h3><ul class="list"><li>电子 / 连接器 / PCB</li><li>铜加工与有色金属</li><li>陶瓷、汽车零部件</li><li>锂电 / 新能源、医药包装</li><li>标准：两周内能见到 5 个质量负责人</li></ul></div>
    </div>
    <div class="callout"><b>首单不是全厂改造：</b>一个产品族 + 一个工位 + 一类缺陷 + 一个 MES 接口。先证明一个站能交付，再谈复制。</div>
  </div>
  <div class="footer"><span>切口选择：能触达、能付费、能复制</span><span>不从“AI 大市场”讲起</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">05</span>商业模式：诊断 → 试点 → 复制</div><div class="page">06 / 10</div></div>
  <div class="content">
    <div class="eyebrow">COMMERCIAL LADDER</div>
    <h2>先卖诊断，再卖试点，<br>最后卖年度许可与渠道</h2>
    <div class="ladder">
      <div class="step"><div class="tag">STEP 01</div><h3>质检-MES 对账诊断</h3><div class="price">1～3 万</div><p>1～2 周；流程断点、MES 契约体检、ROI 测算、Go/No-Go 建议。</p></div>
      <div class="step"><div class="tag">STEP 02</div><h3>单站付费试点</h3><div class="price">5～15 万</div><p>4～6 周；单工位联调、故障恢复、回执对账和验收报告。</p></div>
      <div class="step"><div class="tag">STEP 03</div><h3>年度软件许可</h3><div class="price">2～6 万</div><p>每工位/年；从一次性项目转向持续收入与扩站。</p></div>
      <div class="step"><div class="tag">STEP 04</div><h3>渠道 / OEM</h3><div class="price">按年授权</div><p>MES、自动化、视觉集成商复用适配器与交付包。</p></div>
    </div>
    <div class="economics">
      <div class="econ-box"><b>单位经济目标（不是现状）：</b><br>标准单站实施 5～10 人天；试点周期 4～6 周；销售周期 2～4 个月；软件许可收入占比逐步超过一次性实施；同客户扩站率成为核心增长指标。</div>
      <div class="econ-box"><b>算术示例（不是预测）：</b><br>10 个客户 × 5 个工位 × 2～6 万/工位/年 = <span class="gold">100～300 万 ARR 潜力</span>。先证明单站，再验证这个乘数。</div>
    </div>
    <div class="boundary">以上价格均为验证假设，不是已成交价格；每完成 3 个访谈或 1 个付费项目就更新一次。</div>
  </div>
  <div class="footer"><span>第一笔收入：诊断，不是免费 POC</span><span>用合同和付款验证需求</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">06</span>执行：90 天买证据</div><div class="page">07 / 10</div></div>
  <div class="content">
    <div class="eyebrow">90-DAY VALIDATION PLAN</div>
    <h2>钱不是用来烧规模，<br>是用来买付费证据</h2>
    <div class="timeline">
      <div class="week"><div class="w">W1～W2</div><h3>选切口，做报价</h3><ul><li>1 个江西产业带</li><li>30 个目标企业</li><li>20 个 MES/集成商</li><li>约到 8 次访谈</li></ul></div>
      <div class="week"><div class="w">W3～W4</div><h3>访谈，卖第一个诊断</h3><ul><li>20 次有效访谈</li><li>5 个明确痛点</li><li>1 个付费诊断</li><li>记录预算与决策人</li></ul></div>
      <div class="week"><div class="w">W5～W8</div><h3>交付诊断，转试点</h3><ul><li>3 个诊断报告</li><li>1 个设计伙伴/LOI</li><li>1 个 MES 沙箱</li><li>1 个试点报价</li></ul></div>
      <div class="week"><div class="w">W9～W12</div><h3>试点与复盘</h3><ul><li>1 个付费试点或联调</li><li>实施人天可测量</li><li>标准交付包</li><li>更新融资材料</li></ul></div>
    </div>
    <div class="gate">
      <div class="gate-box"><b>继续推进的条件</b>3 个付费诊断或 1 个付费试点；5 个客户描述同一痛点；至少 1 个真实数据/测试环境；实施人天开始收敛。</div>
      <div class="gate-box stop"><b>停止或转向的条件</b>20 次访谈后没人愿意付费诊断；客户只要免费试用；每个客户都要求完全不同的定制；只能靠关系拿项目。</div>
    </div>
  </div>
  <div class="footer"><span>里程碑拨款 · Go/No-Go</span><span>不把融资当收入</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">07</span>壁垒：交付与复用</div><div class="page">08 / 10</div></div>
  <div class="content">
    <div class="eyebrow">COMPETITION / MOAT</div>
    <h2>壁垒不在模型参数，<br>在最后一公里的交付效率</h2>
    <table class="matrix">
      <tr><th>角色</th><th>强项</th><th>缺口</th><th>与我们的关系</th></tr>
      <tr><td>视觉厂商</td><td>相机、光源、模型</td><td>MES 交付、回执与对账</td><td>可集成为上游入口</td></tr>
      <tr><td>MES 厂商</td><td>业务系统与客户关系</td><td>设备/质检边缘与故障恢复</td><td>可白标为模块</td></tr>
      <tr><td>系统集成商</td><td>现场关系与交付</td><td>重复开发、难复用</td><td>渠道与交付伙伴</td></tr>
      <tr><td>我们</td><td>质检到 MES 的可靠交付</td><td>尚无客户案例与规模验证</td><td>补最后一公里</td></tr>
    </table>
    <div class="moat"><div><b>MES 适配器</b>字段映射、错误分类、行业模板可复用。</div><div><b>故障样本</b>超时、断网、重启、重复消息形成测试资产。</div><div><b>回执审计</b>业务键、回执、证据和时间线逐笔对应。</div><div><b>交付手册</b>单站人天、验收清单、渠道复制与运维包。</div></div>
    <div class="callout"><b>我们不跟大厂抢通用平台：</b>做 MES/集成商愿意嵌入的最后一公里模块。技术可被替代，交付证据和渠道关系更难。</div>
  </div>
  <div class="footer"><span>可合作，不替代</span><span>壁垒来自复用与现场证据</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">08</span>风险：明着管</div><div class="page">09 / 10</div></div>
  <div class="content">
    <div class="eyebrow">RISKS / CONTROLS</div>
    <h2>没有案例不是秘密，<br>风险要明着管</h2>
    <div class="risk-grid">
      <div class="risk"><b>需求风险</b><p>客户说“有兴趣”不等于愿意付费。控制：先卖 1～3 万诊断，20 次访谈后无人付费就停止扩张。</p></div>
      <div class="risk"><b>交付风险</b><p>无边界定制会吞掉团队。控制：单站、单接口、单产品族；变更单；5～10 人天标准上限。</p></div>
      <div class="risk"><b>技术风险</b><p>合成数据不等于现场精度；模拟 MES/PLC 不等于生产。控制：真实样本、沙箱、类别级指标、人工复核和合同责任上限。</p></div>
      <div class="risk"><b>现金风险</b><p>融资到账不是收入，回款可能延迟。控制：里程碑拨款、50/30/20 付款、硬件不压库存、保留 5% 现金储备。</p></div>
    </div>
    <div class="gate">
      <div class="gate-box"><b>我们不会说</b>exactly-once、未知缺陷零漏检、RCA 因果证明、合成准确率等于现场精度、模拟环境等于客户验收。</div>
      <div class="gate-box stop"><b>90 天不达标</b>不扩编、不烧钱、不硬讲规模化；转成连接器诊断服务，继续验证需求或及时止损。</div>
    </div>
  </div>
  <div class="footer"><span>诚实边界是融资材料的一部分</span><span>不为故事付高价</span></div>
</section>

<section class="slide">
  <div class="topbar"><div class="brand"><span class="mark">09</span>融资：买 90 天验证速度</div><div class="page">10 / 10</div></div>
  <div class="content">
    <div class="eyebrow">THE ASK</div>
    <h2>这轮融资买什么：<br>把“技术可行”变成“有人付钱”</h2>
    <div class="ask-grid">
      <div class="ask-box"><div class="panel-label">建议融资区间 / 假设</div><div class="amount">100～300 万</div><div class="sub">9～12 个月 runway · 天使/预种子 · 可接受里程碑拨款</div><div class="econ-box" style="margin-bottom:12px">当前风险更高，但估值和条款通常更灵活；90 天后用付费证据重新定价。</div><div class="use-row"><span>产品工程</span><div class="bar"><i style="width:40%"></i></div><b>40%</b></div><div class="use-row"><span>客户与试点</span><div class="bar"><i style="width:30%"></i></div><b>30%</b></div><div class="use-row"><span>安全合规</span><div class="bar"><i style="width:15%"></i></div><b>15%</b></div><div class="use-row"><span>采图实验</span><div class="bar"><i style="width:10%"></i></div><b>10%</b></div><div class="use-row"><span>现金储备</span><div class="bar"><i style="width:5%"></i></div><b>5%</b></div></div>
      <div class="milestones"><div class="milestone"><i>1</i><div><b>3 个付费诊断</b><br>证明有人愿意为对账问题掏第一笔钱。</div></div><div class="milestone"><i>2</i><div><b>1 个设计伙伴 / LOI</b><br>证明客户愿意共同定义产品和验收。</div></div><div class="milestone"><i>3</i><div><b>1 个付费试点或 MES 沙箱</b><br>证明能进入真实场景并形成交付证据。</div></div><div class="milestone"><i>4</i><div><b>1 个可复用交付包</b><br>证明单站人天、适配器和验收可以复制。</div></div></div>
    </div>
    <div class="closing"><span><b>下一步：</b>30 分钟现场演示 + 一页《质检-MES 对账诊断》Offer。</span><span>达到里程碑再谈复制；达不到就停，不烧钱。</span></div>
  </div>
  <div class="footer"><span>UA_LinqInspect · 工业 AI 质检与质量交付底座</span><span>无客户案例 · 不编案例 · 用付款验证</span></div>
</section>

</body>
</html>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    overview_source = ROOT / ".tmp" / "overview" / "overview-complete.png"
    vision_source = ROOT / ".tmp" / "vision" / "vision-complete.png"
    if not overview_source.exists() or not vision_source.exists():
        raise SystemExit("missing evidence screenshots; run the overview and vision demos first")

    overview_crop = ASSETS / "overview-proof.png"
    vision_crop = ASSETS / "vision-proof.png"
    crop_evidence(overview_source, overview_crop)
    crop_evidence(vision_source, vision_crop)

    html = (
        HTML_TEMPLATE.replace("__OVERVIEW_IMG__", data_uri(overview_crop))
        .replace("__VISION_IMG__", data_uri(vision_crop))
    )
    HTML_PATH.write_text(html, encoding="utf-8")

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
            "--virtual-time-budget=3000",
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
