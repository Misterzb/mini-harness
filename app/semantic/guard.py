"""SQL AST 白名单校验：Hard Controls 的实物证据。"""
import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {"orders", "order_item", "product", "users", "user_behavior", "platform", "order_status"}


def guard(sql: str) -> tuple[bool, str]:
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception as e:
        return False, f"SQL 解析失败: {e}"
    if len(statements) != 1 or statements[0] is None:
        return False, "禁止多语句"
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        return False, "只允许 SELECT"
    for t in tree.find_all(exp.Table):
        if t.name not in ALLOWED_TABLES:
            return False, f"表 {t.name} 不在白名单"
    for _ in tree.find_all(exp.Drop, exp.Insert, exp.Update, exp.Delete, exp.Union):
        return False, "禁止写操作/多语句节点"
    if tree.find(exp.Limit) is None:
        return False, "缺少 LIMIT 保护"
    return True, "ok"
