"""导入 CSV → shop.db(只读分析库) + ledger.db(独立可写退款库)。"""
import json
import duckdb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

TABLES = {
    "order.csv": "orders",       # order 是 SQL 保留字 → 改名
    "order_item.csv": "order_item",
    "product.csv": "product",
    "user.csv": "users",         # user 是保留字 → 改名
    "user_behavior.csv": "user_behavior",
    "platform.csv": "platform",
    "order_status.csv": "order_status",
}


def main():
    shop = DATA / "shop.db"
    if shop.exists():
        shop.unlink()
    con = duckdb.connect(str(shop))
    for csv, table in TABLES.items():
        src = DATA / "raw" / csv
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto(?, header=true)", [str(src)])
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n} rows")
    con.execute("CREATE INDEX idx_orders_time ON orders(order_time)")
    con.execute("CREATE INDEX idx_oi_order ON order_item(order_id)")
    con.execute("CREATE INDEX idx_ub_time ON user_behavior(behavior_time)")
    statuses = con.execute("SELECT DISTINCT order_status FROM orders ORDER BY 1").fetchall()
    print("  订单状态:", [s[0] for s in statuses])
    # 数据时间锚点：相对时间（上个月）一律以数据集最大日期为基准，不猜"今天"
    mx = con.execute("SELECT max(order_time), min(order_time) FROM orders").fetchone()
    (DATA / "manifest.json").write_text(
        json.dumps({"max_order_time": str(mx[0]), "min_order_time": str(mx[1])},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  时间锚点: max={mx[0]}  min={mx[1]}")
    con.close()

    ledger = DATA / "ledger.db"
    if ledger.exists():
        ledger.unlink()
    led = duckdb.connect(str(ledger))
    # 提案账本：状态机 pending → approved/rejected；执行账本：proposal_id 主键保证幂等
    led.execute("""CREATE TABLE proposals(
        proposal_id VARCHAR PRIMARY KEY, order_id VARCHAR, amount DECIMAL(12,2),
        reason VARCHAR, risk_level VARCHAR, evidence_refs VARCHAR,
        status VARCHAR DEFAULT 'pending', created_at TIMESTAMP)""")
    led.execute("""CREATE TABLE refunds(
        proposal_id VARCHAR PRIMARY KEY, order_id VARCHAR, amount DECIMAL(12,2),
        approver VARCHAR, decided_at TIMESTAMP, status VARCHAR)""")
    led.close()
    print(f"完成：{shop.name}(只读分析) + {ledger.name}(proposals 提案账本 / refunds 执行账本)")


if __name__ == "__main__":
    main()
