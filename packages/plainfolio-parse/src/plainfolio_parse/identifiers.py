"""强指纹与机构识别（纯零数据库）。

从工作台 ``legal_workbench/graph_assist.py`` 迁出的提取逻辑：手机号、带校验码的
统一社会信用代码（USCC）、银行账号、邮箱，以及「严格后缀」机构名。规则原样保留：

* USCC 必须通过 GB 32100 校验位；数字串若同时长成身份证（GB 11643 校验位通过）或
  邻域出现「身份证」字样，则**不算** USCC（防误报诱饵）；
* 银行账号必须邻域出现「银行/账号/开户行…」等关键字，纯 16 位物流单号不得命中；
* 机构名只在严格后缀（有限公司/银行…支行/律师事务所…）下成立，并剔除
  「上述/该公司/本公司」等指代前缀。

``scan_identifier_bridges`` 是本包新增的零数据库封装：对多份本地文件解析后，找
跨文件共同出现的同一强指纹（"暗线排查"）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .config import ParseConfig
from .errors import ParseError
from .formats.base import ParsedUnit
from .formats.dispatch import is_supported, parse_file
from .textnorm import normalize_with_map

# --- 机构名 ---------------------------------------------------------------

_GENERIC_TERMS = frozenset({"公司", "集团", "银行", "甲方", "乙方", "丙方", "原告", "被告", "对方", "本方"})

_STRICT_ORG_SUFFIX_CORE = (
    r"银行[^,，。;；:：、()（）\s]{0,24}(?:支行|分行)"
    r"|(?:有限责任公司|股份有限公司|有限公司)[^,，。;；:：、()（）\s]{0,20}(?:分公司|营业部)"
    r"|有限责任公司|股份有限公司|有限公司"
    r"|\((?:有限合伙|普通合伙|特殊普通合伙)\)"
    r"|(?:有限合伙|普通合伙|合伙企业)(?:\((?:有限合伙|普通合伙|特殊普通合伙)\))?"
    r"|人民法院|仲裁委员会"
    r"|(?:律师事务所|会计师事务所|资产评估事务所)(?:\((?:特殊的?普通合伙|普通合伙|有限合伙)\))?"
)
_STRICT_ORG_SUFFIX_END_RE = re.compile(rf"(?:{_STRICT_ORG_SUFFIX_CORE})$")
_ORG_CHAR_CLASS = r"[\u4e00-\u9fffA-Za-z0-9·&\-]"
_ORG_PREFIX_EXPR = rf"{_ORG_CHAR_CLASS}(?:{_ORG_CHAR_CLASS}|\({_ORG_CHAR_CLASS}{{1,12}}\))*?"
_ORG_INLINE_RE = re.compile(rf"({_ORG_PREFIX_EXPR})({_STRICT_ORG_SUFFIX_CORE})")
_LEGAL_FORM_SUFFIX_RE = re.compile(
    r"(?:有限责任公司|股份有限公司|有限公司"
    r"|\((?:有限合伙|普通合伙|特殊普通合伙)\)"
    r"|(?:有限合伙|普通合伙|合伙企业)(?:\((?:有限合伙|普通合伙|特殊普通合伙)\))?)$"
)
_FIRM_SUFFIX_RE = re.compile(
    r"(?:律师事务所|会计师事务所|资产评估事务所)(?:\((?:特殊的?普通合伙|普通合伙|有限合伙)\))?$"
)
_HEADER_PARTY_ORGS_RE = re.compile(
    r"^(?:原告|被告|第三人|申请人|被申请人|上诉人|被上诉人)\s*[:：]\s*(.+)$"
)
_REGION_PREFIX_RE = re.compile(
    r"^(?:中国|北京|上海|天津|重庆|广东|深圳|广州|浙江|杭州|宁波|江苏|南京|苏州|无锡|"
    r"四川|成都|湖北|武汉|湖南|长沙|河南|郑州|河北|山东|济南|青岛|福建|福州|厦门|"
    r"安徽|合肥|陕西|西安|辽宁|沈阳|大连|吉林|黑龙江|云南|昆明|贵州|广西|海南|"
    r"山西|江西|内蒙古|新疆|宁夏|青海|西藏|甘肃|香港|澳门|台湾)(?:省|市|自治区|区|县)?"
)
_INDUSTRY_WORDS = (
    "科技", "贸易", "商贸", "实业", "发展", "投资", "管理", "咨询", "建设",
    "工程", "建筑", "物流", "信息", "网络", "电子", "软件", "技术", "文化",
    "传媒", "置业", "地产", "能源", "环保", "医药", "健康", "金融", "服务",
    "控股", "集团",
)
_MULTI_BOUNDARY_RE = re.compile(
    r"(?:法定代表人|委托诉讼代理人|委托代理人|诉讼代理人|反诉原告|反诉被告|"
    r"被申请人|被上诉人|被执行人|申请执行人|开户银行|开户行|收款单位|付款单位|"
    r"收款人|付款人|以下简称|原告|被告|第三人|申请人|上诉人|甲方|乙方|丙方|丁方|"
    r"担保人|保证人|借款人|出借人|买方|卖方|转让方|受让方|发包人|承包人|出租人|承租人|"
    r"本公司|贵公司|该公司|本案|涉案|上述|前述|关于|委托|根据|按照|依照|同意|确认|"
    r"通知|要求|请求|支付|转入|转至|汇入|汇至|汇往|交付|起诉|诉请|简称|"
    r"另有|还有|以及|另由|另向|另与)"
)
_LEADING_DATE_RE = re.compile(r"^(?:\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?|\d{1,2}月(?:\d{1,2}日)?|\d{1,2}日)")
_LEADING_CONNECTORS = frozenset(("与", "向", "由", "为", "及", "和", "对", "被", "给"))
_LEADING_REGION_PREPOSITIONS = frozenset(("经", "在", "自", "从", "于", "至", "系", "即", "诉", "据", "按", "往", "把", "将", "让"))
_MID_STRONG_SPLITTERS = frozenset(("与", "及", "被", "给"))
_MID_CONTEXT_SPLITTERS = frozenset(("由", "向", "对"))
_STOP_ORG_PREFIXES = frozenset({
    "上述", "前述", "涉案", "相关", "其他", "有关", "任何", "全部", "本案", "该案",
    "双方", "各方", "公司", "集团", "银行", "单位", "企业", "机构", "某", "该",
    "开户", "收款", "付款", "汇款", "指定", "对方", "本方", "我方",
})
_NON_ALIAS_TERMS = _GENERIC_TERMS | frozenset({
    "丁方", "卖方", "买方", "出借人", "借款人", "保证人", "担保人", "发包人", "承包人",
    "转让方", "受让方", "第三人", "申请人", "被申请人", "上诉人", "被上诉人",
    "法定代表人", "委托诉讼代理人", "有限公司", "有限责任公司", "股份有限公司",
    "科技公司", "贸易公司", "实业公司", "投资公司", "控股公司", "集团公司",
    "律师事务所", "会计师事务所", "资产评估事务所", "上述公司", "该公司", "本公司", "贵公司",
})

# --- 强指纹 ---------------------------------------------------------------

IDENTIFIER_KINDS = ("cn_mobile", "uscc", "bank_account", "email")

_CN_MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_USCC_RE = re.compile(
    r"(?<![0-9A-Za-z])([0-9A-HJ-NPQRTUWXYa-hj-npqrtuwxy]{2}\d{6}"
    r"[0-9A-HJ-NPQRTUWXYa-hj-npqrtuwxy]{10})(?![0-9A-Za-z])"
)
_BANK_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{15,19})(?!\d)")
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")

_USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
_BANK_CONTEXT_KEYWORDS = (
    "银行", "账号", "帐号", "卡号", "开户行", "开户银行",
    "户名", "收款账户", "付款账户", "对公账户", "账户",
)
_BANK_CONTEXT_RADIUS = 30
_USCC_CONTEXT_KEYWORDS = ("信用代码", "社会信用")
_CN_ID_NEGATIVE_KEYWORDS = ("身份证", "公民身份号码", "公民身份")
_CN_ID_RE = re.compile(r"^\d{6}(?:19\d{2}|20\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dX]$")
_CN_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CN_ID_CHECK = "10X98765432"

_EXCERPT_RADIUS = 40


@dataclass
class IdentifierHit:
    kind: str  # cn_mobile | uscc | bank_account | email
    value: str
    start: int  # 归一化文本下标
    end: int

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value,
                "span": {"start": self.start, "end": self.end}}


@dataclass
class OrgMention:
    name: str
    start: int
    end: int
    source: str = "inline"  # inline | header

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "span": {"start": self.start, "end": self.end},
                "source": self.source}


# --- 文本小工具 -----------------------------------------------------------


def norm_key(value: str) -> str:
    return re.sub(r"\s+", "", normalize_with_map(value)[0]).lower()


def compact_cjk(value: str) -> str:
    norm = normalize_with_map(value)[0].strip()
    return re.sub(r"(?<=[\u4e00-\u9fff()（）])\s+|\s+(?=[\u4e00-\u9fff()（）])", "", norm).strip()


def window_has_keyword(text: str, start: int, end: int, keywords: tuple[str, ...],
                       radius: int) -> bool:
    ctx_before = text[max(0, start - radius):start]
    ctx_after = text[end:min(len(text), end + radius)]
    window = ctx_before + " " + ctx_after
    return any(kw in window for kw in keywords)


def looks_like_cn_id(code: str) -> bool:
    upper = code.upper()
    if not _CN_ID_RE.fullmatch(upper):
        return False
    total = sum(int(ch) * w for ch, w in zip(upper[:17], _CN_ID_WEIGHTS))
    return upper[17] == _CN_ID_CHECK[total % 11]


def valid_uscc(code: str) -> bool:
    if len(code) != 18:
        return False
    upper = code.upper()
    try:
        total = sum(_USCC_CHARSET.index(ch) * w for ch, w in zip(upper[:17], _USCC_WEIGHTS))
    except ValueError:
        return False
    check_idx = (31 - (total % 31)) % 31
    return upper[17] == _USCC_CHARSET[check_idx]


def extract_identifiers(text: str) -> list[IdentifierHit]:
    """按公司/银行/身份证校验规则提取强指纹（返回归一化坐标）。"""
    norm_text = normalize_with_map(text)[0]
    hits: list[IdentifierHit] = []
    seen: set[tuple[str, str]] = set()

    bank_spans: set[tuple[int, int]] = set()
    for match in _BANK_ACCOUNT_RE.finditer(norm_text):
        start, end = match.start(1), match.end(1)
        if window_has_keyword(norm_text, start, end, _BANK_CONTEXT_KEYWORDS, _BANK_CONTEXT_RADIUS):
            val = match.group(1)
            bank_spans.add((start, end))
            if ("bank_account", val) not in seen:
                seen.add(("bank_account", val))
                hits.append(IdentifierHit("bank_account", val, start, end))

    for match in _USCC_RE.finditer(norm_text):
        start, end = match.start(1), match.end(1)
        val = match.group(1).upper()
        if not valid_uscc(val):
            continue
        has_uscc_ctx = window_has_keyword(norm_text, start, end, _USCC_CONTEXT_KEYWORDS,
                                          _BANK_CONTEXT_RADIUS)
        # Exclude 18-digit citizen ID numbers (Mod 11-2 or explicit ID keywords) unless explicitly labeled USCC.
        if (
            (looks_like_cn_id(val) or window_has_keyword(norm_text, start, end,
                                                        _CN_ID_NEGATIVE_KEYWORDS,
                                                        _BANK_CONTEXT_RADIUS))
            and not has_uscc_ctx
        ):
            continue
        # An all-digit 18-char string already matched as a bank account without USCC keywords is a bank account.
        if val.isdigit() and (start, end) in bank_spans and not has_uscc_ctx:
            continue
        if ("uscc", val) not in seen:
            seen.add(("uscc", val))
            hits.append(IdentifierHit("uscc", val, start, end))

    for match in _CN_MOBILE_RE.finditer(norm_text):
        val = match.group(1)
        if ("cn_mobile", val) not in seen:
            seen.add(("cn_mobile", val))
            hits.append(IdentifierHit("cn_mobile", val, match.start(1), match.end(1)))

    for match in _EMAIL_RE.finditer(norm_text):
        val = match.group(1).lower()
        if ("email", val) not in seen:
            seen.add(("email", val))
            hits.append(IdentifierHit("email", val, match.start(1), match.end(1)))

    return hits


# --- 机构名提取 -----------------------------------------------------------


def split_mid_connectors(prefix: str) -> str:
    """Trim sentence phrases/connectors embedded at the front of an org prefix."""
    best = 0
    for marker in _MULTI_BOUNDARY_RE.finditer(prefix):
        if len(prefix) - marker.end() >= 2:
            best = max(best, marker.end())
    if best > 0:
        prefix = prefix[best:]

    date_match = _LEADING_DATE_RE.match(prefix)
    if date_match and len(prefix) - date_match.end() >= 2:
        prefix = prefix[date_match.end():]

    split_pos = 0
    for idx, ch in enumerate(prefix):
        if idx == 0:
            continue
        rem = prefix[idx + 1:]
        if len(rem) < 2:
            continue
        if ch in _MID_STRONG_SPLITTERS:
            split_pos = idx + 1
        elif ch in _MID_CONTEXT_SPLITTERS and (
            _REGION_PREFIX_RE.match(rem) or (idx >= 2 and len(rem) >= 4)
        ):
            split_pos = idx + 1
        elif ch == "和" and idx >= 2 and _REGION_PREFIX_RE.match(rem):
            split_pos = idx + 1
    if split_pos > 0:
        prefix = prefix[split_pos:]

    while prefix:
        if prefix[0] in _LEADING_CONNECTORS and len(prefix) - 1 >= 2:
            prefix = prefix[1:]
        elif (
            prefix[0] in _LEADING_REGION_PREPOSITIONS
            and _REGION_PREFIX_RE.match(prefix[1:]) is not None
            and len(prefix) - 1 >= 2
        ):
            prefix = prefix[1:]
        else:
            break
    return prefix.strip("·&-. ")


def clean_org_parts(raw_prefix: str, suffix: str) -> tuple[str, str] | None:
    prefix = compact_cjk(raw_prefix).strip("·&-. ")
    prefix = split_mid_connectors(prefix)
    if len(prefix) < 2 or prefix in _STOP_ORG_PREFIXES:
        return None
    full = f"{prefix}{suffix}"
    if full in _NON_ALIAS_TERMS or len(full) < 4:
        return None
    return prefix, full


def extract_org_spans(norm_text: str) -> list[tuple[str, int, int]]:
    """Return [(cleaned_org_name, start, end)] from normalized text."""
    results: list[tuple[str, int, int]] = []
    seen_spans: set[tuple[int, int]] = set()
    for match in _ORG_INLINE_RE.finditer(norm_text):
        cleaned = clean_org_parts(match.group(1), match.group(2))
        if cleaned is None:
            continue
        _prefix, full = cleaned
        start = match.end() - len(full)
        end = match.end()
        if start < match.start():
            start = match.start()
        if (start, end) not in seen_spans:
            seen_spans.add((start, end))
            results.append((full, start, end))
    return results


def extract_header_org_names(raw_text: str) -> list[str]:
    """Inspect litigation party header lines ONLY when ending with a strict org suffix."""
    found: list[str] = []
    for line in raw_text.splitlines():
        norm_line = normalize_with_map(line)[0].strip()
        match = _HEADER_PARTY_ORGS_RE.match(norm_line)
        if not match:
            continue
        rest = match.group(1).strip()
        clause = re.split(r"[,，。;；]", rest, maxsplit=1)[0].strip()
        compact = re.sub(r"\s+", "", clause)
        m_inline = _ORG_INLINE_RE.match(compact)
        if m_inline:
            cleaned = clean_org_parts(m_inline.group(1), m_inline.group(2))
            if cleaned is not None and m_inline.start() == 0:
                tail = compact[m_inline.end():]
                if not tail or tail.startswith("("):
                    found.append(cleaned[1])
    return found


def infer_entity_kind(name: str) -> str:
    if name.endswith("人民法院"):
        return "court"
    if _STRICT_ORG_SUFFIX_END_RE.search(name):
        return "org"
    return "person"


def extract_org_names(text: str) -> list[OrgMention]:
    """机构名提及：正文严格后缀命中 + 诉状当事人栏抬头，按首次出现去重。"""
    norm_text = normalize_with_map(text)[0]
    mentions: list[OrgMention] = []
    seen: set[str] = set()
    for name, start, end in extract_org_spans(norm_text):
        key = norm_key(name)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(OrgMention(name=name, start=start, end=end, source="inline"))
    inline_keys = set(seen)
    for name in extract_header_org_names(text):
        key = norm_key(name)
        if key in inline_keys:
            continue
        inline_keys.add(key)
        mentions.append(OrgMention(name=name, start=0, end=len(name), source="header"))
    return mentions


# --- 跨文件强指纹碰撞（零数据库"暗线排查"） --------------------------------


@dataclass
class BridgeOccurrence:
    file: str
    unit_key: str
    locator: dict[str, Any] = field(default_factory=dict)
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "unit_key": self.unit_key, "locator": self.locator,
                "excerpt": self.excerpt}


@dataclass
class IdentifierBridge:
    kind: str
    value: str
    file_count: int
    files: list[str]
    occurrences: list[BridgeOccurrence]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "file_count": self.file_count,
            "files": self.files,
            "occurrences": [item.to_dict() for item in self.occurrences],
        }


def _unit_label(unit: ParsedUnit) -> dict[str, Any]:
    locator = dict(unit.locator or {})
    label: dict[str, Any] = {}
    if isinstance(locator.get("page"), int):
        label["page"] = locator["page"]
    if isinstance(locator.get("sheet"), str):
        label["sheet"] = locator["sheet"]
        if isinstance(locator.get("row"), int):
            label["row"] = locator["row"]
    if isinstance(locator.get("line_start"), int):
        label["line_start"] = locator["line_start"]
    return label


def _excerpt(norm_text: str, start: int, end: int) -> str:
    left = max(0, start - _EXCERPT_RADIUS)
    right = min(len(norm_text), end + _EXCERPT_RADIUS)
    return norm_text[left:right].strip()


def scan_identifier_bridges(
    files: Sequence[Path], *, config: ParseConfig | None = None,
) -> dict[str, Any]:
    """解析多份本地文件，输出跨文件共同出现的强指纹碰撞清单。"""
    grouped: dict[tuple[str, str], list[BridgeOccurrence]] = {}
    parsed: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for path in files:
        path = Path(path)
        if not is_supported(path):
            skipped.append({"file": str(path), "reason": "unsupported_format"})
            continue
        try:
            result = parse_file(path, config)
        except ParseError as exc:
            failed.append({"file": str(path), "code": exc.code, "message": exc.message})
            continue
        except Exception as exc:  # noqa: BLE001 - 单份文件失败不得中断整批排查
            failed.append({"file": str(path), "code": "internal_error",
                           "message": f"{type(exc).__name__}: {exc}"})
            continue
        parsed.append(str(path))
        for unit in result.units:
            if not unit.text:
                continue
            norm_text = normalize_with_map(unit.text)[0]
            for hit in extract_identifiers(norm_text):
                grouped.setdefault((hit.kind, hit.value), []).append(BridgeOccurrence(
                    file=str(path),
                    unit_key=unit.unit_key,
                    locator=_unit_label(unit),
                    excerpt=_excerpt(norm_text, hit.start, hit.end),
                ))

    bridges: list[IdentifierBridge] = []
    for (kind, value), occurrences in grouped.items():
        files_for_value = list(dict.fromkeys(item.file for item in occurrences))
        if len(files_for_value) < 2:
            continue
        bridges.append(IdentifierBridge(kind=kind, value=value,
                                        file_count=len(files_for_value),
                                        files=files_for_value,
                                        occurrences=occurrences))
    bridges.sort(key=lambda item: (-item.file_count, item.kind, item.value))

    return {
        "file_count": len(parsed),
        "parsed_files": parsed,
        "failed_files": failed,
        "skipped_files": skipped,
        "bridge_count": len(bridges),
        "bridges": [bridge.to_dict() for bridge in bridges],
    }
