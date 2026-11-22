#!/usr/bin/env python3
"""Render the three-page site validation pack."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "investor-deck"
HTML_PATH = OUT / "现场验证三件套.html"
PDF_PATH = OUT / "现场验证三件套.pdf"


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>现场验证三件套</title>
<style>
@page { size: A4; margin: 0; }
:root{--ink:#17202a;--muted:#5d6c7b;--line:#d7dfe6;--navy:#0d2234;--gold:#b4771c;--goldbg:#fcf5e6;--mint:#17795a;--mintbg:#eef9f4;--red:#a5403b;--redbg:#fdf0ef;--blue:#1d5f8a;--bluebg:#eef6fc}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:var(--ink)}
body{font-family:"Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",sans-serif;line-height:1.4}
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
.intro{border:1px solid #e2c58f;background:var(--goldbg);border-radius:2.5mm;padding:2.5mm 3.5mm;font-size:7.2pt;line-height:1.55;color:#5b431d;margin-bottom:3mm}
.intro b{color:#3e2d12}
.section{margin-top:3mm}
h2{font-size:9pt;margin:0 0 1.6mm;padding-left:2.5mm;border-left:2.5px solid var(--gold);line-height:1.2}
h3{font-size:8pt;margin:0 0 1.2mm;color:var(--navy)}
table{width:100%;border-collapse:collapse;font-size:6.7pt;table-layout:fixed}
th{background:var(--navy);color:#fff;text-align:left;padding:1.2mm 1.5mm;font-weight:700;font-size:6.5pt}
td{border:1px solid var(--line);padding:1.15mm 1.5mm;vertical-align:top;color:#435260;word-break:break-word}
td.fill{background:#fcfdfe}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:3mm}
.box{border:1px solid var(--line);border-radius:2.5mm;padding:2.5mm 3mm;background:#fbfcfd}
.box p,.box li{font-size:6.7pt;line-height:1.5;color:#455463}
.box ul{margin:0;padding-left:3.5mm}
.box li::marker{color:var(--gold)}
.check{display:grid;grid-template-columns:1fr 1fr;gap:1mm 3mm;font-size:6.6pt;color:#455463}
.check div:before{content:"□";color:var(--gold);margin-right:1.2mm;font-weight:900}
.stop{border:1px solid #e3b7b3;background:var(--redbg);border-radius:2.5mm;padding:2.5mm 3mm;font-size:6.6pt;line-height:1.55;color:#71302c}
.stop b{color:#4e211e}
.note{border:1px solid #b9dfd0;background:var(--mintbg);border-radius:2.5mm;padding:2.5mm 3mm;font-size:6.6pt;line-height:1.55;color:#245c49}
.note b{color:#104b39}
.sign{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3mm;margin-top:3mm;font-size:6.6pt;color:#455463}
.sign div{border-top:1px solid #7f8e9a;padding-top:1.2mm}
.footer{position:absolute;left:12mm;right:12mm;bottom:4mm;border-top:1px solid var(--line);padding-top:1.2mm;display:flex;justify-content:space-between;font-size:6pt;color:#8a97a3}
.tight td{padding:.95mm 1.4mm}
.small{font-size:6.2pt;color:var(--muted)}
</style>
</head>
<body>

<section class="page">
  <div class="top"><div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>现场验证三件套 · 01 现场确认</span></div></div><div class="meta"><b>现场验证确认单 · v0.1</b><br>采购硬件前填写</div></div></div>
  <div class="title"><h1>现场验证确认单</h1><span>先锁现场，再买硬件</span></div>
  <div class="intro"><b>用途：</b>确认一个真实工位、样件、MES 接口和验收责任人。没有样件、接口或负责人，不采购硬件。本单不是采购合同、生产验收或投资承诺。</div>

  <div class="cols">
    <div class="section"><h2>1. 基本信息</h2><table class="tight"><tr><th style="width:34%">项目</th><th>填写</th></tr><tr><td>工厂名称</td><td class="fill"></td></tr><tr><td>地址/行业</td><td class="fill"></td></tr><tr><td>产品族</td><td class="fill"></td></tr><tr><td>产线/车间</td><td class="fill"></td></tr><tr><td>质检工位</td><td class="fill"></td></tr><tr><td>确认日期</td><td class="fill"></td></tr><tr><td>项目方联系人</td><td class="fill"></td></tr><tr><td>质量负责人</td><td class="fill"></td></tr><tr><td>IT/MES 负责人</td><td class="fill"></td></tr></table></div>
    <div class="section"><h2>2. 验证范围</h2><table class="tight"><tr><th style="width:38%">项目</th><th>填写</th></tr><tr><td>一个产品族</td><td class="fill"></td></tr><tr><td>一个质检工位</td><td class="fill"></td></tr><tr><td>一类主要缺陷</td><td class="fill"></td></tr><tr><td>当前质检方式</td><td class="fill"></td></tr><tr><td>节拍/班次</td><td class="fill"></td></tr><tr><td>允许验证时间</td><td class="fill"></td></tr><tr><td>设备进线</td><td>是 / 否 / 仅台架</td></tr><tr><td>允许拍照/录像</td><td>是 / 否</td></tr><tr><td>允许脱敏留档</td><td>是 / 否</td></tr></table></div>
  </div>

  <div class="cols">
    <div class="section"><h2>3. 样件与标注</h2><table class="tight"><tr><th style="width:38%">项目</th><th>填写</th></tr><tr><td>OK 样件数量</td><td class="fill"></td></tr><tr><td>NG 样件数量</td><td class="fill"></td></tr><tr><td>缺陷类别</td><td class="fill"></td></tr><tr><td>标注人/标准</td><td class="fill"></td></tr><tr><td>样件归还时间</td><td class="fill"></td></tr></table><div class="small">最低要求：50～100 件 OK/NG 样件，能说明缺陷位置和判定标准。</div></div>
    <div class="section"><h2>4. MES 接口</h2><table class="tight"><tr><th style="width:38%">项目</th><th>填写</th></tr><tr><td>MES 厂商/版本</td><td class="fill"></td></tr><tr><td>接口类型</td><td>REST / OPC UA / 数据库 / 文件 / 其他</td></tr><tr><td>测试环境/沙箱</td><td>是 / 否</td></tr><tr><td>字段样例</td><td>是 / 否</td></tr><tr><td>回执/鉴权</td><td class="fill"></td></tr><tr><td>接口联系人</td><td class="fill"></td></tr></table></div>
  </div>

  <div class="cols">
    <div class="section"><h2>5. 现场条件</h2><table class="tight"><tr><th style="width:38%">项目</th><th>填写</th></tr><tr><td>光照/补光</td><td class="fill"></td></tr><tr><td>相机安装位置</td><td class="fill"></td></tr><tr><td>网络/电源</td><td class="fill"></td></tr><tr><td>振动/粉尘/油污</td><td class="fill"></td></tr><tr><td>安全要求/陪同人</td><td class="fill"></td></tr></table></div>
    <div class="section"><h2>6. 角色与确认</h2><table class="tight"><tr><th style="width:34%">角色</th><th>姓名/职责</th><th style="width:22%">签字/日期</th></tr><tr><td>工厂负责人</td><td class="fill"></td><td class="fill"></td></tr><tr><td>质量负责人</td><td class="fill"></td><td class="fill"></td></tr><tr><td>IT/MES 负责人</td><td class="fill"></td><td class="fill"></td></tr><tr><td>项目方</td><td class="fill"></td><td class="fill"></td></tr><tr><td>投资人观察员</td><td class="fill"></td><td class="fill"></td></tr></table></div>
  </div>

  <div class="cols section">
    <div class="box"><h3>进入下一阶段的条件</h3><div class="check"><div>产品族/工位/缺陷确认</div><div>50～100 件样件可用</div><div>拍照/录像/留档许可</div><div>MES 测试环境或字段样例</div><div>质量与 IT 负责人指定</div><div>现场访问和验收时间约定</div></div></div>
    <div class="stop"><b>出现任意一项，不采购硬件：</b>没有样件或无法标注；没有 MES 接口/测试环境/联系人；没有质量负责人或验收人；不允许拍照/录像/留档；现场不允许设备进入且无台架方案；工厂只愿意“看看”，不愿投入时间。</div>
  </div>
  <div class="sign"><div>项目方签字 / 日期</div><div>工厂代表签字 / 日期</div><div>投资人观察员签字 / 日期</div></div>
  <div class="footer"><span>UA_LinqInspect · 现场验证三件套 · 01</span><span>本单只确认验证条件，不替代采购合同或生产验收</span></div>
</section>

<section class="page">
  <div class="top"><div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>现场验证三件套 · 02 硬件与资产</span></div></div><div class="meta"><b>硬件询价与资产清单 · v0.1</b><br>先租后买，先买一台再扩</div></div></div>
  <div class="title"><h1>硬件询价与资产清单</h1><span>不买整线，不压库存</span></div>
  <div class="intro"><b>用途：</b>逐项询价、明确资产归属和退出方式。优先租/借/可退方案；不在现场确认前下单；超范围需求先报价再决定。</div>

  <div class="cols">
    <div class="section"><h2>1. 项目信息</h2><table class="tight"><tr><th style="width:38%">项目</th><th>填写</th></tr><tr><td>项目名称</td><td>单工位质检-MES 验证</td></tr><tr><td>现场确认单编号</td><td class="fill"></td></tr><tr><td>询价/有效期</td><td class="fill"></td></tr><tr><td>采购负责人</td><td class="fill"></td></tr><tr><td>资产归属方</td><td>投资人 / 公司 / 工厂 / 其他</td></tr><tr><td>资产保管人</td><td class="fill"></td></tr></table></div>
    <div class="section"><h2>2. 预算汇总</h2><table class="tight"><tr><th style="width:46%">项目</th><th>金额/说明</th></tr><tr><td>首笔硬件预算</td><td>建议 1～2 万元</td></tr><tr><td>不可预见费</td><td class="fill"></td></tr><tr><td>租赁/借测替代</td><td class="fill"></td></tr><tr><td>未使用余额处理</td><td>退回 / 留存 / 转入下一阶段</td></tr></table></div>
  </div>

  <div class="section"><h2>3. 询价清单</h2><table><tr><th style="width:19%">项目</th><th style="width:22%">型号/规格</th><th style="width:6%">数量</th><th style="width:11%">单价</th><th style="width:11%">小计</th><th style="width:13%">供应商</th><th style="width:9%">交期</th><th style="width:9%">质保/退租</th></tr><tr><td>工业相机</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td>镜头</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td>光源/控制器</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td>支架/工装</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td>线缆/配件</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td>边缘算力盒</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td>存储/网卡</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td>现场耗材</td><td class="fill"></td><td>1</td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td><td class="fill"></td></tr><tr><td><b>合计</b></td><td></td><td></td><td></td><td class="fill"></td><td></td><td></td><td></td></tr></table></div>

  <div class="cols section">
    <div class="box"><h3>采购规则</h3><div class="check"><div>已取得现场确认单</div><div>2～3 家供应商报价</div><div>优先租/借/可退</div><div>现场确认前不下单</div><div>不买整线/不压库存</div><div>超范围先报价</div><div>发票/序列号/照片留档</div><div>资产编号登记</div></div></div>
    <div class="box"><h3>到货验收</h3><div class="check"><div>型号与报价一致</div><div>序列号已登记</div><div>外观无损</div><div>上电启动正常</div><div>相机可采集</div><div>边缘盒可运行软件</div><div>配件/线缆齐全</div><div>发票/保修齐全</div></div></div>
  </div>

  <div class="section"><h2>4. 资产归属与退出</h2><table class="tight"><tr><th style="width:25%">情形</th><th>处理方式</th></tr><tr><td>验证正常完成</td><td>资产留在项目/公司，继续用于下一阶段</td></tr><tr><td>阶段 1 不达标</td><td>停止追加；资产保留、归还或按约定折价转给工厂</td></tr><tr><td>阶段 2 不达标</td><td>停止追加；按约定处理硬件和未使用余额</td></tr><tr><td>工厂终止配合</td><td>资产撤回；已发生费用按发票结算</td></tr><tr><td>投资人决定停止</td><td>未使用余额退回；硬件按归属约定处置</td></tr></table></div>
  <div class="sign"><div>项目方签字 / 日期</div><div>投资人/出资方签字 / 日期</div><div>资产保管人签字 / 日期</div></div>
  <div class="footer"><span>UA_LinqInspect · 现场验证三件套 · 02</span><span>本清单不是采购合同；正式采购前确认付款、发票、保修、保险和归属</span></div>
</section>

<section class="page">
  <div class="top"><div class="brand"><div class="mark">UA</div><div><b>UA_LinqInspect</b><span>现场验证三件套 · 03 验收与停止</span></div></div><div class="meta"><b>验收指标与停止条件 · v0.1</b><br>花钱前约定“什么叫跑通”</div></div></div>
  <div class="title"><h1>验收指标与停止条件</h1><span>只对约定范围成立</span></div>
  <div class="intro"><b>原则：</b>指标只对约定产品族、工位、缺陷类别、样件和 MES 接口成立；不外推到整线或所有缺陷。未达标不称为“跑通”。</div>

  <div class="cols">
    <div class="section"><h2>1. 验证范围</h2><table class="tight"><tr><th style="width:38%">项目</th><th>填写</th></tr><tr><td>产品族/工位</td><td class="fill"></td></tr><tr><td>缺陷类别</td><td class="fill"></td></tr><tr><td>OK / NG 样件</td><td>____ 件 / ____ 件</td></tr><tr><td>MES 接口</td><td class="fill"></td></tr><tr><td>测试环境</td><td>真实沙箱 / 模拟 MES / 其他</td></tr><tr><td>周期/验收人</td><td class="fill"></td></tr></table></div>
    <div class="section"><h2>2. 阶段 0：现场确认</h2><table class="tight"><tr><th style="width:52%">指标</th><th>目标</th><th style="width:18%">结果</th></tr><tr><td>现场确认单完整</td><td>是</td><td class="fill"></td></tr><tr><td>样件可用</td><td>≥ 50 件</td><td class="fill"></td></tr><tr><td>MES 接口/字段样例</td><td>已提供</td><td class="fill"></td></tr><tr><td>质量/IT 联系人</td><td>已指定</td><td class="fill"></td></tr><tr><td>拍照/录像/留档许可</td><td>已确认</td><td class="fill"></td></tr></table></div>
  </div>

  <div class="section"><h2>3. 阶段 1：台架/离线验证</h2><table class="tight"><tr><th style="width:30%">指标</th><th style="width:18%">目标值</th><th style="width:34%">测量方法</th><th style="width:18%">结果</th></tr><tr><td>图像质量门禁通过率</td><td>≥ ____%</td><td>真实样件采集记录</td><td class="fill"></td></tr><tr><td>缺陷可见性</td><td>可判读</td><td>样件对照和人工复核</td><td class="fill"></td></tr><tr><td>类别级 Recall</td><td>≥ ____%</td><td>约定样件集</td><td class="fill"></td></tr><tr><td>类别级 Precision</td><td>≥ ____%</td><td>约定样件集</td><td class="fill"></td></tr><tr><td>人工复核比例</td><td>≤ ____%</td><td>低置信度/质量门禁统计</td><td class="fill"></td></tr><tr><td>证据完整性</td><td>可追溯</td><td>图片/哈希/模型版本导出</td><td class="fill"></td></tr></table><div class="small">未通过：停止，不进入 MES 联调；记录原因，不追加预算。</div></div>

  <div class="section"><h2>4. 阶段 2：真实单站闭环</h2><table class="tight"><tr><th style="width:30%">指标</th><th style="width:18%">目标值</th><th style="width:34%">测量方法</th><th style="width:18%">结果</th></tr><tr><td>测试样本量</td><td>≥ 300 件/3 班次</td><td>现场记录</td><td class="fill"></td></tr><tr><td>NG 投递成功率</td><td>≥ ____%</td><td>业务键和 MES 回执</td><td class="fill"></td></tr><tr><td>重复业务单数量</td><td>测试集内 ____ 单</td><td>同一业务键去重检查</td><td class="fill"></td></tr><tr><td>MES 回执对账一致率</td><td>≥ ____%</td><td>回执号/业务键逐笔核对</td><td class="fill"></td></tr><tr><td>断网恢复时间</td><td>≤ ____ 分钟</td><td>故障演练</td><td class="fill"></td></tr><tr><td>MES 超时恢复时间</td><td>≤ ____ 分钟</td><td>故障演练</td><td class="fill"></td></tr><tr><td>进程重启恢复时间</td><td>≤ ____ 分钟</td><td>故障演练</td><td class="fill"></td></tr><tr><td>证据完整率</td><td>≥ ____%</td><td>质量证据包导出</td><td class="fill"></td></tr><tr><td>现场验收签字</td><td>是</td><td>质量/IT/项目方</td><td class="fill"></td></tr></table><div class="small">未通过：停止追加；提交失败原因报告；按约定处理硬件和未使用余额。</div></div>

  <div class="cols section">
    <div class="box"><h3>测量与证据规则</h3><ul><li>使用约定产品族、工位、缺陷和样件集</li><li>测试集、标注规则、验收人启动前确认</li><li>结果可追溯到业务键、日志、回执或证据包</li><li>不以一次演示成功替代重复测试</li><li>不以合成数据替代真实样件</li><li>不以模拟 MES 替代真实 MES 沙箱</li></ul></div>
    <div class="stop"><b>明确不承诺：</b>零漏检、零漏单或 exactly-once；任意缺陷/光照/产品都能识别；真实产线长期稳定；客户一定采购或项目一定回本；相关性分析不是因果证明。</div>
  </div>
  <div class="note section"><b>停止条件：</b>真实 MES 接口无法获得；回执无法对账；重复/丢失无法收敛；现场无法持续配合；目标值未达到。出现任意一项，停止追加，不用“再给一点钱就能成”延长项目。</div>
  <div class="sign"><div>质量负责人签字 / 日期</div><div>IT/MES 负责人签字 / 日期</div><div>项目方签字 / 日期</div></div>
  <div class="footer"><span>UA_LinqInspect · 现场验证三件套 · 03</span><span>本文件是验收口径，不是生产合同；正式试点前确认商务、责任、数据和安全条款</span></div>
</section>

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
