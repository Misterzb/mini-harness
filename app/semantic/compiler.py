"""SemanticPlan → SQL 纯函数。字段白名单 + 参数绑定 + 口径由 FactDef 决定。"""
from ..runtime.models import SemanticPlan
from .facts import FactDef
from .normalize import resolve_time, load_manifest


# 字段 → (列表达式, 是否需 join users, 是否需 join order_item/product)
FIELD_MAP: dict[str, tuple[str, bool, bool]] = {
    "platform":   ("o.platform", False, False),
    "city":       ("u.city", True, False),
    "user_level": ("u.user_level", True, False),
    "month":      ("strftime(o.order_time, '%Y-%m')", False, False),
    "category":   ("p.category", False, True),
}


def compile_plan(plan: SemanticPlan, fact: FactDef) -> tuple[str, list]:
    """返回 (sql, params)。不接受自由文本 SQL，杜绝注入。"""
    used = set(plan.dimensions) | {f.field for f in plan.filters}
    unknown = used - set(FIELD_MAP)
    if unknown:
        raise ValueError(f"字段不在白名单: {sorted(unknown)}")

    # 品类维度的口径约束：category 需要 join 明细，只有 gmv 定义了 item 粒度口径。
    # 比值型指标(discount_rate/aov)带 category 会口径失真 → 直接拒绝，不做字符串替换。
    if "category" in used:
        if fact.name != "gmv":
            raise ValueError(f"指标 {fact.name} 不支持按 category 拆分（比值型口径不可下推到商品粒度）")

    # 相对时间锚定数据集最大日期；解析失败用 manifest 的下界，绝不猜"今天"
    bounds = None
    if plan.time_range:
        bounds = resolve_time(plan.time_range)
    if bounds is None:
        lo = load_manifest().get("min_order_time", "2025-01-01 00:00:00")
        bounds = (lo, "9999-12-31 23:59:59")

    need_u = any(FIELD_MAP[f][1] for f in used)
    need_i = any(FIELD_MAP[f][2] for f in used) or fact.need_item_join

    dims = ", ".join(FIELD_MAP[d][0] for d in plan.dimensions)
    # 口径完全来自 FactDef：按品类时用 gmv 的 item 粒度口径，不改写 metric_sql 字符串
    metric = fact.metric_sql if not (need_i and fact.name == "gmv") else "SUM(oi.item_total)"

    where = ["o.payment_time IS NOT NULL", "o.order_time >= ?", "o.order_time < ?"]
    params: list = [bounds[0], bounds[1]]
    for f in plan.filters:
        where.append(f"{FIELD_MAP[f.field][0]} = ?")
        params.append(f.value)

    sql = f"SELECT {dims + ', ' if dims else ''}{metric} AS value FROM orders o "
    if need_u:
        sql += "LEFT JOIN users u ON o.global_user_id = u.global_user_id "
    if need_i:
        sql += "LEFT JOIN order_item oi ON o.order_id = oi.order_id "
        sql += "LEFT JOIN product p ON oi.global_product_id = p.global_product_id "
    sql += "WHERE " + " AND ".join(where) + " "
    if dims:
        sql += f"GROUP BY {dims} "
    sql += "ORDER BY value DESC LIMIT 1000"
    return sql, params
