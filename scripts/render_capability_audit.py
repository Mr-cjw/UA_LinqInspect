#!/usr/bin/env python3
"""Render the two-page capability and competitor-gap audit."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "capability-audit"
HTML_PATH = OUT / "接单能力审计.html"
PDF_PATH = ROOT / "交付材料" / "05_接单能力审计_2页.pdf"


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>接单能力与同行差距审计</title>
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
.cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3mm}
.box{border:1px solid var(--line);border-radius:2.5mm;padding:2.5mm 3mm;background:#fbfcfd}
.box.good{border-color:#b9dfd0;background:var(--mintbg)}
.box.bad{border-color:#e3b7b3;background:var(--redbg)}
.box.info{border-color:#b8d4e7;background:var(--bluebg)}
.box p,.box li{font-size:6.7pt;line-height:1.5;color:#455463}
.box ul{margin:0;padding-left:3.5mm}
.box li::marker{color:var(--gold)}
.good b{color:#104b39}.bad b{color:#4e211e}.info b{color:#174a6b}
.tag{display:inline-block;border-radius:99px;padding:.7mm 2mm;font-size:6pt;font-weight:800;margin-bottom:1.5mm}
.tag.good{background:#dff3ea;color:#116247}.tag.bad{background:#f8dedb;color:#8b332e}.tag.warn{background:#f8edcf;color:#795717}
.note{border:1px solid #b9dfd0;background:var(--mintbg);border-radius:2.5mm;padding:2.5mm 3mm;font-size:6.7pt;line-height:1.55;color:#245c49;margin-top:2.5mm}
.note b{color:#104b39}
.footer{position:absolute;left:12mm;right:12mm;bottom:4mm;border-top:1px solid var(--line);padding-top:1.2mm;display:flex;justify-content:space-between;font-size:6pt;color:#8a97a3}
.tight td{padding:.9mm 1.3mm}
.small{font-size:6.2pt;color:var(--muted)}
</style>
</head>
<body>

<section class="page">
  <div class="top"><div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>接单能力与同行差距审计 · 01</span></div></div><div class="meta"><b>2026-11-22</b><br>当前主线代码与交付材料</div></div></div>
  <div class="title"><h1>接单能力与同行差距审计</h1><span>结论先行 · 不把演示说成交付</span></div>
  <div class="verdict"><b>结论：可以演示，也可以做付费的质检/MES 技术诊断；不能按“交钥匙 AI 质检系统”接单。</b><br>项目强在“质检结果到 MES 的可靠交付与证据链”，弱在真实视觉、现场工程、安装调试和售后。现阶段应接诊断/联合验证，不应接生产交付。</div>

  <div class="cols section">
    <div class="box good"><span class="tag good">已经真实存在</span><h3>软件可靠性</h3><ul><li>SQLite Outbox、重试、死信、人工重放</li><li>MES 回执校验、UA 写回确认</li><li>断网、重启、写回前崩溃恢复</li><li>质量证据包与逐笔追溯</li></ul></div>
    <div class="box bad"><span class="tag bad">仍是模拟/占位</span><h3>真实现场</h3><ul><li>检测循环仍用随机数生成 OK/NG</li><li>训练/验证图像为程序合成</li><li>MES 是本地模拟 HTTP 端</li><li>没有真实相机、光源、工装</li></ul></div>
    <div class="box info"><span class="tag warn">接单边界</span><h3>现在怎么接</h3><ul><li>可接：演示、诊断、单站 PoC</li><li>不可接：交钥匙工位、无人值守</li><li>不可承诺：零漏检、exactly-once</li><li>不可把 HTTP 200 当成 MES 入库</li></ul></div>
  </div>

  <div class="section"><h2>1. 与同行的真实差距</h2><table class="tight">
    <tr><th style="width:18%">对比对象</th><th style="width:26%">同行强在哪里</th><th style="width:30%">你现在缺什么</th><th>你能赢在哪里</th></tr>
    <tr><td>机器视觉平台<br><span class="small">Cognex / Keyence / HALCON / Landing AI</span></td><td>相机光学选型、成像工装、标定、模型工具、现场服务、成熟案例</td><td>真实成像链路、样本管理、模型评估、误检回流、现场调参</td><td>轻量可定制的质检-MES 交付层；不绑定某一家视觉平台</td></tr>
    <tr><td>MES 质量模块<br><span class="small">Opcenter / FactoryTalk / SAP DM</span></td><td>ISA-95、批次谱系、SPC、返修处置、审计、企业级集成</td><td>真实 MES 适配器、业务主数据、质量流程深度、权限</td><td>边缘快速接入、断网补传、逐笔对账、证据包</td></tr>
    <tr><td>工业数据平台<br><span class="small">Kepware / Ignition / HighByte</span></td><td>多协议接入、Tag 管理、历史库、UNS、诊断、冗余</td><td>多协议驱动、historian、远程运维、设备模板库</td><td>聚焦“质检结果如何变成 MES 业务动作”</td></tr>
    <tr><td>视觉/自动化集成商</td><td>现场勘测、光学工装、PLC 联动、安装施工、备件和 SLA</td><td>现场施工能力、硬件供应链、服务网络、验收经验</td><td>软件可靠性、可追溯性、标准化复制</td></tr>
    <tr><td>低代码/边缘平台<br><span class="small">Tulip / Greengrass / IoT Edge</span></td><td>低代码应用、设备身份、OTA、边缘模块生命周期、集中管理</td><td>配置化、远程升级、设备群管理、多租户</td><td>单站闭环更深、MES 事务语义更明确、部署更轻</td></tr>
  </table></div>

  <div class="note"><b>差异化定位：</b>不要正面拼“通用视觉精度”或“完整 MES 功能”。真正可卖的是“质检结果不丢、不重、可对账、可追溯地进入 MES”，并能适配不同视觉前端。</div>
  <div class="footer"><span>UA_LinqInspect · 接单能力与同行差距审计</span><span>01 / 02 · 结论与同行对比</span></div>
</section>

<section class="page">
  <div class="top"><div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>接单能力与同行差距审计 · 02</span></div></div><div class="meta"><b>单站交付门槛</b><br>三关全过，才谈接单</div></div></div>
  <div class="title"><h1>先做一个能验收的单站包</h1><span>一个产品族 · 一个工位 · 3～5 类缺陷</span></div>

  <div class="section"><h2>2. 接单前必须补齐的八个门槛</h2><table class="tight">
    <tr><th style="width:18%">门槛</th><th style="width:58%">最低可交付标准</th><th>当前状态</th></tr>
    <tr><td>真实相机与光学</td><td>工业相机 SDK/GenICam 接入；镜头、光源、支架、触发方式确定；能稳定取图</td><td>未完成</td></tr>
    <tr><td>现场标定</td><td>ROI、工作距离、曝光、白平衡、图像质量门禁可按工位配置</td><td>只有门禁，无标定</td></tr>
    <tr><td>真实样本与评估</td><td>300+ 件真实样件，3～5 类缺陷，独立留出集，类别级 Recall/Precision</td><td>只有合成数据</td></tr>
    <tr><td>真实 MES 沙箱</td><td>鉴权、幂等键、错误码、回执、重复请求、冲突处理、逐笔对账</td><td>只有本地模拟</td></tr>
    <tr><td>工位调试</td><td>配置向导/检查单：相机、工单、SN、MES、阈值、试跑、签字</td><td>有模板，无向导</td></tr>
    <tr><td>安装与升级</td><td>一键安装、依赖锁定、配置备份、版本升级、失败回滚</td><td>手工部署模板</td></tr>
    <tr><td>运维与售后</td><td>日志、指标、告警、备份恢复、远程支持边界、响应时限</td><td>有基线，无 SLA</td></tr>
    <tr><td>安全与数据</td><td>企业 PKI/账号分级、网络隔离、数据归属、保留/删除、审计</td><td>本地 CA，实验级</td></tr>
  </table></div>

  <div class="section"><h2>3. 最小可售单站包：三关</h2><div class="cols">
    <div class="box"><span class="tag good">Gate A</span><h3>真实视觉台架</h3><ul><li>真实相机 + 镜头 + 光源 + 工装</li><li>300+ 件真实 OK/NG 样件</li><li>类别级指标、混淆矩阵、误检成本</li><li>未知缺陷/低置信度转人工</li></ul></div>
    <div class="box"><span class="tag warn">Gate B</span><h3>真实 MES 沙箱</h3><ul><li>真实鉴权、开单、报工、回执</li><li>重复请求和冲突处理</li><li>72h 影子运行或 3 个班次</li><li>逐笔对账，不以 HTTP 200 代替</li></ul></div>
    <div class="box"><span class="tag good">Gate C</span><h3>可交付软件包</h3><ul><li>安装脚本、配置样例、现场检查单</li><li>备份恢复、升级回滚、故障手册</li><li>非开发者独立安装演练</li><li>双方签字验收与支持边界</li></ul></div>
  </div></div>

  <div class="section"><h2>4. 建议的未来 60 天</h2><table class="tight">
    <tr><th style="width:18%">周期</th><th style="width:42%">目标</th><th>产出</th></tr>
    <tr><td>第 1～2 周</td><td>借/租工业相机，搭最小光学台架</td><td>真实图像采集记录、镜头/光源/工装清单</td></tr>
    <tr><td>第 3～4 周</td><td>接相机适配器，建立样本清单和评估脚本</td><td>真实样本集、类别级指标、失败样例</td></tr>
    <tr><td>第 5～6 周</td><td>找愿意提供 MES 沙箱的工厂做联调</td><td>真实回执、72h 影子对账、问题清单</td></tr>
    <tr><td>第 7～8 周</td><td>把安装、配置、备份、回滚和验收跑一遍</td><td>单站可交付包、一次非开发者安装演练</td></tr>
  </table></div>

  <div class="cols section">
    <div class="box bad"><h3>不要说的话</h3><ul><li>“已经可以接生产单”</li><li>“AI 准确率很高”，但没有真实标注集</li><li>“已经对接 MES”，但只是模拟服务</li><li>“零漏检、零漏单、exactly-once”</li></ul></div>
    <div class="box good"><h3>现在最值得做的一件事</h3><p>拿到一台真实工业相机和 300 件真实样件，先把“采图—评估—复核—证据包”跑通。没有真实样件或 MES 沙箱，不要继续加功能。</p></div>
  </div>

  <div class="footer"><span>UA_LinqInspect · 接单能力与同行差距审计</span><span>02 / 02 · 交付门槛与 60 天路径</span></div>
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
