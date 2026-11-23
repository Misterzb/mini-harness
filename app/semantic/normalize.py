"""别名归一化 + 相对时间锚定 —— Loop 与 LLM mock 共用同一套规则。

相对时间不猜"今天"，而是锚定数据集最大日期（data/manifest.json），
这样演示数据固定在 2025 年时，"上个月"也有确定含义、可核对。
"""
import json
import re
from calendar import monthrange
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "manifest.json"

FACT_ALIASES = {
    "成交额": "gmv", "销售额": "gmv", "成交金额": "gmv", "gmv": "gmv", "GMV": "gmv",
    "订单量": "order_cnt", "订单数": "order_cnt", "单量": "order_cnt", "order_cnt": "order_cnt",
    "客单价": "aov", "aov": "aov",
    "买家数": "buyer_cnt", "下单用户数": "buyer_cnt", "buyer_cnt": "buyer_cnt",
    "优惠率": "discount_rate", "折扣率": "discount_rate", "discount_rate": "discount_rate",
}
DIMENSION_ALIASES = {
    "平台": "platform", "城市": "city", "用户等级": "user_level",
    "品类": "category", "月份": "month",
}
PLATFORM_ALIASES = {"淘宝": "taobao", "京东": "jd", "抖音": "douyin"}
CITY_ALIASES = {"上海": "上海", "北京": "北京", "深圳": "深圳", "杭州": "杭州", "广州": "广州",
                "武汉": "武汉", "成都": "成都", "南京": "南京"}


def load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"max_order_time": "2025-12-31 23:59:00", "min_order_time": "2025-01-01 00:00:00"}


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """返回该月的 [start, end) 字符串（闭开区间）。"""
    start = f"{year:04d}-{month:02d}-01 00:00:00"
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    end = f"{ny:04d}-{nm:02d}-01 00:00:00"
    return start, end


def resolve_time(expr: str | None) -> tuple[str, str] | None:
    """把相对/绝对时间解析为 [start, end)；无法解析返回 None（编译器用默认全区间）。"""
    if not expr:
        return None
    anchor = datetime.strptime(load_manifest()["max_order_time"][:19], "%Y-%m-%d %H:%M:%S")
    e = expr.strip()
    if e in ("上个月", "上月", "last_month"):
        y, m = (anchor.year - 1, 12) if anchor.month == 1 else (anchor.year, anchor.month - 1)
        return _month_bounds(y, m)
    if e in ("这个月", "本月", "this_month"):
        return _month_bounds(anchor.year, anchor.month)
    if e in ("上上个月",):
        m0 = anchor.month - 2
        y, m = (anchor.year - 1, 12 + m0) if m0 < 1 else (anchor.year, m0)
        return _month_bounds(y, m)
    # 绝对月份 YYYY-MM
    if len(e) == 7 and e[4] == "-":
        try:
            y, m = int(e[:4]), int(e[5:7])
            if 1 <= m <= 12:
                return _month_bounds(y, m)
        except ValueError:
            pass
    return None


def normalize_fact(value: str | None) -> str | None:
    if value is None:
        return None
    return FACT_ALIASES.get(value, value)  # 未知指标原样保留 → pre-verify 拦截（幻觉演示）


def normalize_plan(plan: dict) -> dict:
    """把 LLM/mock 的口语 plan 归一化为标准 plan（不改未知指标，留给校验拦截）。"""
    out = dict(plan)
    if "fact" in out:
        out["fact"] = normalize_fact(out.get("fact"))
    out["dimensions"] = [DIMENSION_ALIASES.get(d, d) for d in (out.get("dimensions") or [])]
    norm_filters = []
    for f in (out.get("filters") or []):
        if isinstance(f, dict):
            field = DIMENSION_ALIASES.get(f.get("field"), f.get("field"))
            value = f.get("value")
            if field == "platform":
                value = PLATFORM_ALIASES.get(value, value)
            norm_filters.append({"field": field, "value": value})
        else:
            norm_filters.append(f)
    out["filters"] = norm_filters
    if "time_range" in out and out["time_range"]:
        bounds = resolve_time(out["time_range"])
        out["time_bounds"] = list(bounds) if bounds else None
    return out


def user_names_fact(text: str) -> bool:
    """用户是否明确点名了某个指标（用来决定多轮是否继承上一轮口径）。"""
    return any(alias in text for alias in FACT_ALIASES)


# 原话相对时间词：长词在前，避免「上上个月」被「上个月」先截断。
RELATIVE_TIME_WORDS = ("上上个月", "上个月", "上月", "这个月", "本月")
# 只有明确要求「按月/每月」才是分组查询；否则即便模型加了 month 也要去掉。
MONTH_GROUP_WORDS = ("按月", "每月", "分月", "各月", "每个月", "按月份")
# 绝对月份：2025年3月 / 2025年03月 / 2025-03。真实模型常把这类原话漏成 time_range=None，
# 结果查成全年总量——与「上个月」同类缺陷，必须按原话强制回填。
_ABSOLUTE_MONTH_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月|(20\d{2})-(\d{1,2})(?!\d)")
# 原话点名的未知指标：即使模型擅自映射成白名单 fact 也要拒答，不能偷换成 gmv。
UNKNOWN_METRIC_WORDS = ("净利润率", "销售利润率", "成本利润率", "净利润", "毛利润", "利润率", "利润")
# 原话点名的维度：重规划时模型可能悄悄丢掉，导致「按品类折扣率」被答成总折扣率。
DIMENSION_PATTERNS = (
    ("按月份", "month"), ("按月", "month"), ("每月", "month"), ("各月", "month"),
    ("按品类", "category"), ("分品类", "category"), ("各品类", "category"), ("品类分布", "category"),
    ("按平台", "platform"), ("分平台", "platform"), ("各平台", "platform"),
    ("按城市", "city"), ("分城市", "city"), ("各城市", "city"),
    ("按用户等级", "user_level"), ("分用户等级", "user_level"), ("各用户等级", "user_level"),
)


def utterance_time_token(utterance: str) -> str | None:
    """原话里的时间表达 → 可被 resolve_time 解析的 token。相对词优先于绝对月。"""
    token = next((w for w in RELATIVE_TIME_WORDS if w in utterance), None)
    if token:
        return token
    m = _ABSOLUTE_MONTH_RE.search(utterance)
    if m:
        year = m.group(1) or m.group(3)
        month = int(m.group(2) or m.group(4))
        if 1 <= month <= 12:
            return f"{int(year):04d}-{month:02d}"
    return None


def constrain_utterance_time(plan: dict, utterance: str) -> dict:
    """用户原话优先于模型计划（真实模型常漏填 time_range 或乱加 month）。

    必须在 normalize_plan / _inherit 之后调用，是写入 ctx.plan 前的最后一道口径约束：
    - 原话含相对时间词或绝对月份 → 覆盖模型的 time_range（含漏填 None / 填错）。
    - 原话没有「按月/每月」→ 从 dimensions 去掉 month。
      「上个月 GMV 是多少」要的是一个月合计，不是 12 行分组。
    返回重新归一化的 plan（time_bounds 随之刷新，轨迹与执行一致）。
    """
    out = dict(plan)
    token = utterance_time_token(utterance)
    if token:
        out["time_range"] = token
    if not any(w in utterance for w in MONTH_GROUP_WORDS):
        out["dimensions"] = [d for d in (out.get("dimensions") or []) if d != "month"]
    return normalize_plan(out)


def constrain_utterance_fact(plan: dict, utterance: str) -> dict:
    """原话点名未知指标时，把 fact 固定成未知词，交给 _precheck 拒答。

    真实模型可能把「毛利润」擅自映射成 gmv，若不拦就会答出一个看似合理的错误数字。
    """
    word = next((w for w in UNKNOWN_METRIC_WORDS if w in utterance), None)
    if not word:
        return plan
    out = dict(plan)
    out["fact"] = word
    return normalize_plan(out)


def constrain_utterance_dimensions(plan: dict, utterance: str) -> dict:
    """原话点名的维度不能被模型/重规划悄悄丢掉。

    尤其「按品类折扣率」：模型第一次给出 category 被 compiler 拒，重规划却去掉 category
    答成总折扣率——问 A 答 B。把原话维度强制回填后，第二次仍被拒 → 正常拒答。
    """
    forced = [dim for zh, dim in DIMENSION_PATTERNS if zh in utterance]
    if not forced:
        return plan
    out = dict(plan)
    dims = list(out.get("dimensions") or [])
    for dim in forced:
        if dim not in dims:
            dims.append(dim)
    out["dimensions"] = dims
    return normalize_plan(out)
