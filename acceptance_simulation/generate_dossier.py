#!/usr/bin/env python3
"""生成工作空间全格式仿真测试案卷（TEST-ONLY）。

只生成静态测试文件，不连接数据库、不修改正式运行状态。
"""
from __future__ import annotations

import csv
from decimal import Decimal
import io
from pathlib import Path
import sys

from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
DOSSIER = ROOT / "dossier"

SONGTI_FONT = "/System/Library/Fonts/Supplemental/Songti.ttc"
HEITI_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"

USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
CN_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
CN_ID_CHECK = "10X98765432"


def make_uscc(prefix17: str) -> str:
    """按 GB 32100 计算第 18 位统一社会信用代码校验位。"""
    assert len(prefix17) == 17
    total = sum(USCC_CHARSET.index(ch) * w for ch, w in zip(prefix17, USCC_WEIGHTS))
    return prefix17 + USCC_CHARSET[(31 - (total % 31)) % 31]


def make_cn_id(prefix17: str) -> str:
    """按 GB 11643 计算第 18 位公民身份号码校验位。"""
    assert len(prefix17) == 17 and prefix17.isdigit()
    total = sum(int(ch) * w for ch, w in zip(prefix17, CN_ID_WEIGHTS))
    return prefix17 + CN_ID_CHECK[total % 11]


USCC_HUACHEN = make_uscc("91310115MA1K9X284")
USCC_QIHANG = make_uscc("91320506MA1P7L392")
USCC_HONGXIN = make_uscc("91320100MA1T4B816")
CN_ID_HANDLER = make_cn_id("31011519880512123")

BANNER = "【TEST-ONLY 仿真验收材料 — 仅用于法律工作台功能验收，非真实案件】"


def _reset_dir(path: Path) -> None:
    if path.exists():
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
    path.mkdir(parents=True, exist_ok=True)


def write_docx_indictment(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.styles["Normal"].font.name = "Songti SC"
    doc.styles["Normal"].font.size = Pt(11)

    doc.add_paragraph(BANNER)
    doc.add_heading("民事起诉状（仿真验收样本）", level=1)

    doc.add_paragraph(
        f"原告：上海华辰智能制造有限公司（简称“华辰智造”），住所地：上海市浦东新区张江路188号，"
        f"统一社会信用代码：{USCC_HUACHEN}。"
    )
    doc.add_paragraph(
        f"被告一：苏州启航建设工程有限公司（简称“启航建工”），住所地：苏州市吴中区东吴南路66号，"
        f"统一社会信用代码：{USCC_QIHANG}。"
    )
    doc.add_paragraph(
        f"被告二：江苏宏信商业保理有限公司（简称“宏信保理”），住所地：南京市建邺区庐山路99号，"
        f"统一社会信用代码：{USCC_HONGXIN}。"
    )

    doc.add_heading("一、诉讼请求", level=2)
    doc.add_paragraph("1. 判令被告一启航建工向原告华辰智造支付尚欠成套设备采购款本金人民币 8,500,000.00 元；")
    doc.add_paragraph("2. 判令被告一支付逾期付款违约金人民币 637,500.00 元（或作为替代性利息主张按 LPR 计算至 2026年10月31日止利息人民币 510,000.00 元，两项不重复计入总额）；")
    doc.add_paragraph("3. 判令被告一承担原告支付的律师代理费人民币 180,000.00 元及财产保全担保费人民币 12,800.00 元；")
    doc.add_paragraph("4. 上述请求扣减被告一已付履约保证金抵扣人民币 300,000.00 元后净额主张；")
    doc.add_paragraph("5. 判令被告二宏信保理在最高担保限额人民币 6,000,000.00 元范围内承担连带清偿责任。")

    doc.add_heading("二、诉请金额与抵扣明细表（嵌套表格测试）", level=2)
    rows = [
        ("分组", "项目名称", "金额（人民币）", "性质", "拟定证据出处"),
        ("主诉请请求组", "成套设备采购款本金", "8,500,000.00 元", "计入本金", "主合同 / 催款函"),
        ("主诉请请求组", "逾期付款违约金", "637,500.00 元", "计入违约金", "主合同违约条款"),
        ("主诉请请求组", "律师代理费", "180,000.00 元", "计入费用", "代理费发票说明"),
        ("主诉请请求组", "财产保全担保费", "12,800.00 元", "计入费用", "保全保险单"),
        ("主诉请请求组", "已收履约保证金抵扣", "-300,000.00 元", "抵扣项", "标准往来台账第2-3笔"),
        ("替代利息分组（重叠排除）", "替代性法定孳息主张", "510,000.00 元", "不计入全案总计", "替代诉讼方案备忘"),
    ]
    table = doc.add_table(rows=len(rows), cols=5)
    table.style = "Table Grid"
    for r_idx, row in enumerate(rows):
        for c_idx, val in enumerate(row):
            table.cell(r_idx, c_idx).text = val

    doc.add_heading("三、关键办案期限建议（用于时间轴验收）", level=2)
    doc.add_paragraph("1. 举证期限届满日：2026-12-05（外部期限）；")
    doc.add_paragraph("2. 第一次开庭排期：2026-12-18（外部期限）；")
    doc.add_paragraph("3. 财产保全续封申请提醒：2027-01-15（内部计划）。")

    doc.save(str(out_path))


def write_text_pdf_contract(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(TTFont("SongtiCN", SONGTI_FONT, subfontIndex=0))

    c = canvas.Canvas(str(out_path), pagesize=A4)
    width, height = A4

    pages = [
        [
            BANNER,
            "成套设备采购主合同（合同编号：HC-2025-EQ-0108）",
            "",
            "甲方（买方）：苏州启航建设工程有限公司（以下简称“启航建工”）",
            f"统一社会信用代码：{USCC_QIHANG}",
            "乙方（卖方）：上海华辰智能制造有限公司（以下简称“华辰智造”）",
            f"统一社会信用代码：{USCC_HUACHEN}",
            "",
            "第一条 标的与合同总价",
            "甲乙双方经友好协商，甲方就智能装配生产线设备采购事宜向乙方订购成套设备。",
            "本合同固定总价为人民币 12,000,000.00 元（大写：壹仟贰佰万元整）。",
            "",
            "第二条 分期付款节点",
            "1. 第一期预付款：甲方应于 2025年3月15日 前向乙方支付人民币 3,500,000.00 元；",
            "2. 第二期到货款：甲方应于 2025年7月20日 前向乙方支付人民币 5,000,000.00 元；",
            "3. 第三期验收尾款：甲方应于 2025年11月30日 前向乙方支付人民币 3,500,000.00 元。",
        ],
        [
            BANNER,
            "第二页 资金账户、联络信息与物流约定",
            "",
            "第三条 指定收款账户与项目联络",
            "1. 乙方指定银行收款账号：6225880123456789012（开户银行：中国建设银行上海张江支行）。",
            "2. 甲乙双方确认日常履约与催告联络手机：13918826655。",
            "3. 法务合同送达电子邮箱：legal.contract@huachen-sim.example。",
            "",
            "第四条 经办身份核验与物流跟踪（防误报测试字段）",
            f"1. 现场交付经办人身份证号：{CN_ID_HANDLER}（仅供个人身份登记，非企业信用代码）。",
            "2. 本批次主设备货物快递物流运单编号：9000123456789012（仅供仓库收货核对，无任何结算用途）。",
        ],
        [
            BANNER,
            "第三页 违约责任、担保衔接与签署",
            "",
            "第五条 逾期违约金及担保衔接",
            "甲方逾期付款的，应按未付金额及约定标准支付违约金；",
            "本合同项下货款债权由江苏宏信商业保理有限公司另行出具《最高额连带责任保证函》提供担保。",
            "",
            "第六条 争议解决",
            "因履行本合同发生争议的，提交原告所在地有管辖权的人民法院诉讼解决。",
            "签署日期：2025年3月8日。",
        ],
    ]

    for page_lines in pages:
        y = height - 60
        for idx, line in enumerate(page_lines):
            font_size = 13 if idx == 1 else 10.5
            c.setFont("SongtiCN", font_size)
            c.drawString(48, y, line)
            y -= 24 if idx == 1 else 21
        c.showPage()
    c.save()


def _render_chinese_document_image(
    title: str,
    lines: list[str],
    seal_text: str,
    size: tuple[int, int] = (1654, 2339),
) -> Image.Image:
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype(HEITI_FONT, 44)
    body_font = ImageFont.truetype(HEITI_FONT, 31)
    small_font = ImageFont.truetype(HEITI_FONT, 25)

    draw.rectangle((48, 48, size[0] - 48, size[1] - 48), outline=(210, 210, 210), width=3)
    draw.text((88, 90), BANNER, fill=(110, 30, 30), font=small_font)
    draw.text((88, 155), title, fill=(15, 15, 15), font=title_font)

    y = 245
    for line in lines:
        if line == "":
            y += 28
            continue
        draw.text((88, y), line, fill=(20, 20, 20), font=body_font)
        y += 58

    # 绘制红色仿真印章于右下角（不遮挡正文文本）
    cx, cy, radius = size[0] - 290, size[1] - 320, 130
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(195, 35, 35), width=7)
    draw.text((cx - 88, cy - 22), seal_text, fill=(195, 35, 35), font=small_font)
    return img


def write_scanned_guarantee_pdf(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = _render_chinese_document_image(
        title="最高额连带责任保证函",
        lines=[
            "致债权人：上海华辰智能制造有限公司",
            "",
            "主债务人：苏州启航建设工程有限公司",
            "保证人：江苏宏信商业保理有限公司",
            f"保证人信用代码：{USCC_HONGXIN}",
            "",
            "第一条 保证担保范围与方向",
            "江苏宏信商业保理有限公司同意为苏州启航建设工程有限公司",
            "向上海华辰智能制造有限公司履行成套设备采购合同（编号 HC-2025-EQ-0108）",
            "项下全部付款义务提供不可撤销的最高额连带责任保证。",
            "",
            "第二条 最高担保限额",
            "本保证函项下最高担保限额为人民币 6000000.00 元（大写：陆佰万元整）。",
            "",
            "第三条 保证期间",
            "保证期间为主合同约定的每笔债务履行期届满之日起 2 年。",
            "",
            "保证人盖章：江苏宏信商业保理有限公司",
            "签署日期：2025年3月10日",
        ],
        seal_text="宏信保理测试章",
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    c.drawImage(ImageReader(buf), 0, 0, width=w, height=h)
    c.showPage()
    c.save()


def write_receipt_png(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = _render_chinese_document_image(
        title="催款函与对账签收回执",
        lines=[
            "致：苏州启航建设工程有限公司",
            "抄送并知会：苏州鑫悦贸易有限公司、南京鼎盛重工科技有限公司",
            "",
            "就编号 HC-2025-EQ-0108 成套设备采购合同项下未付货款事宜，",
            "发函方：上海华辰智能制造有限公司。",
            "",
            "截至 2025年10月15日，经核对双方交付及付款记录，",
            "苏州启航建设工程有限公司确认尚欠设备款本金 8500000.00 元。",
            "",
            "请贵司于收函后十日内汇入主合同指定账户。",
            "催收及对账联络手机：13918826655。",
            "",
            "签收确认人：启航建工财务部",
            "签收日期：2025年10月16日",
        ],
        seal_text="启航建工签收章",
    )
    img.save(str(out_path), format="PNG")


def write_intent_txt(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = f"""{BANNER}
战略合作意向书（未生效草案备忘）

签署双方：
甲方：上海华辰智能制造有限公司（简称“华辰智造”）
乙方：南京鼎盛重工科技有限公司（简称“鼎盛重工”）

第一条 初步合作意向
华辰智造与南京鼎盛重工科技有限公司拟就华东区配套零部件供应开展技术接洽，初步磋商合作框架。

第二条 效力特别声明（证明力边界）
双方明确确认：本意向书仅为双方初步接洽备忘，不具有法律约束力，对双方不产生正式合同义务；
正式权利义务及具体采购、担保安排以双方另行正式盖章签署的书面协议为准。

签署日期：2025年4月20日（未成立正式协议）
"""
    out_path.write_text(text, encoding="utf-8")


def write_standard_xlsx(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "往来明细"
    headers = ["交易日期", "付款方", "收款方", "交易金额", "币种", "方向", "摘要", "回单号"]
    ws.append(headers)
    rows = [
        ["2025-03-18", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("3500000.00"), "CNY", "付款", "一期设备预付款", "HD-20250318-01"],
        ["2025-04-02", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("200000.00"), "CNY", "付款", "履约保证金（第一笔）", "HD-20250402-02"],
        ["2025-04-15", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("100000.00"), "CNY", "付款", "履约保证金（第二笔）", "HD-20250415-03"],
        ["2025-06-10", "苏州鑫悦贸易有限公司", "上海华辰智能制造有限公司", Decimal("1200000.00"), "CNY", "付款", "受托代付二期设备款", "HD-20250610-04"],
        ["2025-07-25", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("1500000.00"), "CNY", "付款", "二期设备款部分支付", "HD-20250725-05"],
        ["2025-08-05", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("50000.00"), "USD", "付款", "进口核心阀组美元配件款", "HD-20250805-06"],
        ["2025-09-01", "上海华辰智能制造有限公司", "苏州启航建设工程有限公司", Decimal("50000.00"), "CNY", "退款", "多收调试费差额退回", "HD-20250901-07"],
        ["2025-09-20", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", "待银行回单确认", "CNY", "付款", "补正差额待核对（异常行）", "HD-20250920-08"],
    ]
    for row in rows:
        ws.append(row)

    ws2 = wb.create_sheet("统计摘要")
    ws2.append(["说明", BANNER])
    ws2.append(["CNY 有效数值行合计公式", "=SUM(往来明细!D2:D6,往来明细!D8)"])
    ws2.append(["备注", "第9行含非数值金额，用于检验表格列映射 skipped_rows 与整数分精度。"])
    wb.save(str(out_path))


def write_nonstandard_csv(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[list[str]] = [
        [BANNER, "", "", "", "", "", ""],
        ["某商业银行企业网银对账明细导出（非标多表头）", "账户：6225880123456789012", "币种：CNY", "", "", "", ""],
        ["查询区间：2025-03-01 至 2025-10-31", "导出制表：仿真银行模板", "", "", "", "", ""],
        ["流水基本信息", "", "资金变动（借贷拆列）", "", "对手方与摘要", "", ""],
        ["记账日期", "流水号", "借贷标志", "发生额（元）", "对方户名", "对方账号", "附言摘要"],
    ]
    # 25 条正常流水（含收入/贷 与 支出/借、千分位逗号、2位小数分）
    tx_rows = [
        ("2025-03-18", "BNK-001", "贷（收）", "3,500,000.00", "苏州启航建设工程有限公司", "3220001001", "一期设备预付款"),
        ("2025-03-25", "BNK-002", "借（支）", "125,400.55", "南京鼎盛重工科技有限公司", "3201008811", "外协机身壳体采购"),
        ("2025-04-02", "BNK-003", "贷（收）", "200,000.00", "苏州启航建设工程有限公司", "3220001001", "履约保证金1"),
        ("2025-04-15", "BNK-004", "贷（收）", "100,000.00", "苏州启航建设工程有限公司", "3220001001", "履约保证金2"),
        ("2025-04-28", "BNK-005", "借（支）", "88,120.40", "南京鼎盛重工科技有限公司", "3201008811", "机加工配套件尾款"),
        ("2025-05-06", "BNK-006", "贷（收）", "45,000.15", "苏州鑫悦贸易有限公司", "3205009922", "配件调试补充款"),
        ("2025-05-14", "BNK-007", "借（支）", "19,800.00", "苏州物流专线服务部", "3205007711", "干线运费"),
        ("2025-05-22", "BNK-008", "贷（收）", "12,345.67", "苏州启航建设工程有限公司", "3220001001", "零星耗材结算"),
        ("2025-06-03", "BNK-009", "借（支）", "64,500.00", "南京鼎盛重工科技有限公司", "3201008811", "工装模具费"),
        ("2025-06-10", "BNK-010", "贷（收）", "1,200,000.00", "苏州鑫悦贸易有限公司", "3205009922", "代启航建工付二期款"),
        ("2025-06-19", "BNK-011", "借（支）", "5,432.10", "上海张江检测中心", "3100004411", "出厂第三方检测费"),
        ("2025-06-30", "BNK-012", "贷（收）", "8,888.88", "苏州启航建设工程有限公司", "3220001001", "软件模块授权费"),
        ("2025-07-08", "BNK-013", "借（支）", "31,200.00", "苏州物流专线服务部", "3205007711", "大型主机特种运输"),
        ("2025-07-15", "BNK-014", "借（支）", "42,000.25", "南京鼎盛重工科技有限公司", "3201008811", "备品备件结算"),
        ("2025-07-25", "BNK-015", "贷（收）", "1,500,000.00", "苏州启航建设工程有限公司", "3220001001", "二期设备款部分"),
        ("2025-08-02", "BNK-016", "借（支）", "18,000.00", "上海律协测试鉴定所", "3100005511", "公证及保全预付"),
        ("2025-08-11", "BNK-017", "贷（收）", "6,600.00", "苏州鑫悦贸易有限公司", "3205009922", "代付技术培训费"),
        ("2025-08-20", "BNK-018", "借（支）", "15,500.50", "苏州物流专线服务部", "3205007711", "返修部件回运运费"),
        ("2025-09-01", "BNK-019", "借（支）", "50,000.00", "苏州启航建设工程有限公司", "3220001001", "退回多收调试费"),
        ("2025-09-10", "BNK-020", "贷（收）", "33,000.00", "苏州鑫悦贸易有限公司", "3205009922", "代理备件款"),
        ("2025-09-18", "BNK-021", "借（支）", "9,999.99", "上海张江检测中心", "3100004411", "复检服务费"),
        ("2025-09-25", "BNK-022", "贷（收）", "15,000.00", "苏州启航建设工程有限公司", "3220001001", "现场工程师差旅补偿"),
        ("2025-10-08", "BNK-023", "借（支）", "22,500.00", "南京鼎盛重工科技有限公司", "3201008811", "非标夹具加工费"),
        ("2025-10-14", "BNK-024", "贷（收）", "4,500.00", "苏州鑫悦贸易有限公司", "3205009922", "零星配件回款"),
        ("2025-10-20", "BNK-025", "借（支）", "12,800.00", "江苏保函服务有限公司", "3201006611", "诉讼保全担保费"),
    ]
    for row in tx_rows:
        lines.append(list(row))
    lines.append([])  # 空行
    lines.append(["2025-10-25", "BNK-ERR", "贷（收）", "待冲正核查", "苏州启航建设工程有限公司", "3220001001", "异常格式测试行"])
    lines.append(["合计", "共25笔有效", "", "需外部Python脚本汇总校验", "", "", ""])

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(lines)


def write_background_archive_txt(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = f"""{BANNER}
工商变更与关联暗线核查底档

一、核心当事方工商与联系线索
1. 苏州启航建设工程有限公司
   - 统一社会信用代码：{USCC_QIHANG}
   - 对公联络手机：13918826655
   - 法务与商务接收邮箱：legal.contract@huachen-sim.example
   - 历史关联收款账号：6225880123456789012（与华辰智造主合同账号存在混用线索，须核查）。

二、关联机构暗线排查记录（孤儿机构测试）
2. 关联方一：南京鼎盛重工科技有限公司曾于 2025年5月与苏州启航建设工程有限公司人员接洽配套零件转包；
   工商档案再次提及南京鼎盛重工科技有限公司注册地址变更及设备转售往来。
3. 关联方二：苏州鑫悦贸易有限公司曾在 2025年6月代付二期货款；
   税务往来底档再次显示苏州鑫悦贸易有限公司法定代表人与启航建工监事曾共用同一联络方式。

三、仅以简称出现的对账纪要（双主体别名全案共现测试）
2025年9月18日，启航建工与华辰智造确认未付货款8,500,000.00元，双方约定由宏信保理继续在担保额度内承保。
"""
    out_path.write_text(text, encoding="utf-8")


def write_evidence_catalog_xlsx(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "证据目录清单草稿"
    ws.append(["证据编号", "证据分组", "证据名称", "证明目的", "关联文件名", "页码"])
    rows = [
        ["原告证据1", "合同与担保类", "成套设备采购主合同", "证明买卖合同关系、1200万元总价及三期付款期限", "02_成套设备采购主合同.pdf", "1-3"],
        ["原告证据2", "合同与担保类", "最高额连带责任保证函（扫描件）", "证明宏信保理在600万元限额内承担连带保证责任", "03_最高额连带责任保证函（扫描件）.pdf", "1"],
        ["原告证据3", "参考备忘类", "战略合作意向书（未生效）", "仅作背景接洽说明，不具有法律约束力", "04_战略合作意向书（未生效）.txt", "1"],
        ["原告证据4", "财务与履行类", "标准往来明细台账", "证明已收预付款、保证金30万元及代付、退款情况", "05_标准往来明细台账.xlsx", "1"],
        ["原告证据5", "财务与履行类", "某商业银行企业网银对账明细", "配合外部Python脚本核对全量25笔银行流水收支", "06_非标银行对账单_多表头混列.csv", "1"],
        ["原告证据6", "催告与核查类", "催款函与对账签收回执（拍照件）", "证明启航建工确认尚欠设备款本金850万元及催收事实", "07_催款函与签收回执（拍照件）.png", "1"],
        ["原告证据7", "催告与核查类", "工商变更与关联核查底档", "证明主名别名对账纪要及跨主体强指纹关联线索", "08_工商变更与关联核查底档.txt", "1"],
        ["原告证据8", "诉讼程序类", "民事起诉状", "载明全部诉讼请求明细与期限节点", "01_民事起诉状.docx", "1"],
    ]
    for r in rows:
        ws.append(r)
    wb.save(str(out_path))


def write_corrupted_pdf(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"%PDF-1.7\n%TEST-ONLY-CORRUPTED-EOF\n%%broken-xref-stream\n")


def main() -> int:
    _reset_dir(DOSSIER)
    write_docx_indictment(DOSSIER / "01_诉讼文书" / "01_民事起诉状.docx")
    write_text_pdf_contract(DOSSIER / "02_核心合同与担保" / "02_成套设备采购主合同.pdf")
    write_scanned_guarantee_pdf(DOSSIER / "02_核心合同与担保" / "03_最高额连带责任保证函（扫描件）.pdf")
    write_intent_txt(DOSSIER / "02_核心合同与担保" / "04_战略合作意向书（未生效）.txt")
    write_standard_xlsx(DOSSIER / "03_财务流水与对账" / "05_标准往来明细台账.xlsx")
    write_nonstandard_csv(DOSSIER / "03_财务流水与对账" / "06_非标银行对账单_多表头混列.csv")
    write_receipt_png(DOSSIER / "04_履约往来与暗线材料" / "07_催款函与签收回执（拍照件）.png")
    write_background_archive_txt(DOSSIER / "04_履约往来与暗线材料" / "08_工商变更与关联核查底档.txt")
    write_evidence_catalog_xlsx(DOSSIER / "04_履约往来与暗线材料" / "09_证据目录清单草稿.xlsx")
    write_corrupted_pdf(DOSSIER / "04_履约往来与暗线材料" / "10_损坏扫描附件（模拟损坏）.pdf")

    print(f"USCC_HUACHEN={USCC_HUACHEN}")
    print(f"USCC_QIHANG={USCC_QIHANG}")
    print(f"USCC_HONGXIN={USCC_HONGXIN}")
    print(f"CN_ID_HANDLER={CN_ID_HANDLER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
