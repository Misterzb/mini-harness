"""FactDef：语义层的声明式事实定义。口径唯一来源，LLM 不能自己写 SQL。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class FactDef:
    name: str
    description: str
    metric_sql: str                      # 唯一合法口径
    allowed_dimensions: tuple[str, ...]
    time_field: str = "order_time"
    need_item_join: bool = False         # 是否必须 join order_item（品类维度）


FACTS: dict[str, FactDef] = {
    "gmv": FactDef(
        name="gmv",
        description="成交金额（已支付订单的应付总额）",
        metric_sql="SUM(o.total_amount)",
        allowed_dimensions=("platform", "city", "user_level", "month", "category"),
    ),
    "order_cnt": FactDef(
        name="order_cnt",
        description="订单数",
        metric_sql="COUNT(DISTINCT o.order_id)",
        allowed_dimensions=("platform", "city", "user_level", "month", "category"),
    ),
    "aov": FactDef(
        name="aov",
        description="客单价 = GMV / 订单数",
        metric_sql="SUM(o.total_amount) / NULLIF(COUNT(DISTINCT o.order_id), 0)",
        allowed_dimensions=("platform", "city", "user_level", "month", "category"),
    ),
    "buyer_cnt": FactDef(
        name="buyer_cnt",
        description="下单用户数",
        metric_sql="COUNT(DISTINCT o.global_user_id)",
        allowed_dimensions=("platform", "city", "user_level", "month", "category"),
    ),
    "discount_rate": FactDef(
        name="discount_rate",
        description="优惠占应付金额比例（诊断 GMV 下降是否因折扣变化）",
        metric_sql="SUM(o.discount_amount) / NULLIF(SUM(o.total_amount), 0)",
        allowed_dimensions=("platform", "city", "user_level", "month", "category"),
    ),
}


def get_fact(name: str) -> FactDef | None:
    return FACTS.get(name)
