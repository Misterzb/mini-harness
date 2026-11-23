"""
生成与天池 222889「电商运营分析数据」结构等价的模拟数据。
字段/编码/中文状态与天池数据集一致，数据为随机生成，与任何真实业务无关。
输出 CSV 到 data/raw/，再由 build_db.py 导入 DuckDB。
"""
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(20261121)
RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

PLATFORMS = ["taobao", "jd", "douyin"]
PLATFORM_NAMES = {"taobao": "淘宝", "jd": "京东", "douyin": "抖音"}
STATUSES = ["待付款", "待发货", "已发货", "已完成", "已取消", "退款中"]
CATEGORIES = {
    "电子产品": ["手机", "电脑", "耳机"],
    "服装": ["上衣", "裤子", "外套"],
    "家居": ["家具", "厨具", "收纳"],
    "美妆": ["护肤", "彩妆", "香水"],
    "书籍": ["文学", "技术", "教辅"],
    "运动": ["健身", "户外", "球类"],
    "玩具": ["积木", "手办", "益智"],
}
BEHAVIORS = ["浏览", "加购", "下单"]
CITIES = ["上海", "北京", "深圳", "杭州", "广州", "成都", "武汉", "南京"]
LEVELS = ["普通", "银卡", "金卡", "钻石"]
BRANDS = ["华为", "小米", "苹果", "耐克", "优衣库", "欧莱雅", "索尼", "联想"]


def write(name, header, rows):
    with (RAW / name).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  {name}: {len(rows)} rows")


def main():
    print("生成模拟数据 ...")
    # 平台 / 状态字典
    write("platform.csv", ["code", "name", "description", "created_at", "updated_at"],
          [[c, PLATFORM_NAMES[c], f"{PLATFORM_NAMES[c]}平台", "2025-01-01 00:00:00", "2025-01-01 00:00:00"] for c in PLATFORMS])
    order_status_rows = []
    for i, s in enumerate(STATUSES):
        code = ["pending", "unshipped", "shipped", "completed", "cancelled", "refunding"][i]
        order_status_rows.append([code, s, "badge", s, "2025-01-01 00:00:00", "2025-01-01 00:00:00"])
    write("order_status.csv", ["code", "name", "badge_class", "description", "created_at", "updated_at"], order_status_rows)
    write("payment_method.csv", ["code", "name", "description", "created_at", "updated_at"],
          [["alipay", "支付宝", "", "2025-01-01 00:00:00", "2025-01-01 00:00:00"],
           ["wechat", "微信支付", "", "2025-01-01 00:00:00", "2025-01-01 00:00:00"]])
    write("shipping_method.csv", ["code", "name", "description", "created_at", "updated_at"],
          [["express", "快递", "", "2025-01-01 00:00:00", "2025-01-01 00:00:00"],
           ["standard", "普通", "", "2025-01-01 00:00:00", "2025-01-01 00:00:00"]])

    # 用户
    n_users = 500
    users, user_ids = [], []
    for i in range(n_users):
        uid = f"U{i:04d}"
        user_ids.append(uid)
        pf = random.choice(PLATFORMS)
        users.append([uid, f"{pf}_{i}", pf, f"用户{i}", random.choice(["M", "F"]),
                      random.randint(18, 60), random.choice(CITIES),
                      "2024-01-01", f"138****{i:04d}", f"u{i}@example.com",
                      random.choice(LEVELS)])
    write("user.csv", ["global_user_id", "platform_user_id", "platform", "user_name", "gender",
                       "age", "city", "registration_date", "phone", "email", "user_level"], users)

    # 商品
    products, product_ids = [], []
    for i in range(120):
        pid = f"P{i:04d}"
        product_ids.append(pid)
        cat = random.choice(list(CATEGORIES))
        sub = random.choice(CATEGORIES[cat])
        price = round(random.uniform(20, 3000), 2)
        products.append([pid, f"SKU{i}", random.choice(PLATFORMS),
                         f"{sub}商品{i}", cat, sub, price, random.choice(BRANDS),
                         random.choice(["有货", "缺货"]), "新品,热卖"])
    write("product.csv", ["global_product_id", "platform_product_id", "platform", "product_name",
                          "category", "subcategory", "price", "brand", "stock_status", "tags"], products)

    # 订单（覆盖 2025 全年，3 月/4 月故意做成 GMV 下降，供诊断演示）
    orders, order_items, behaviors = [], [], []
    base = datetime(2025, 1, 1)
    oid_seq = 0
    bid_seq = 0
    for day in range(365):
        d = base + timedelta(days=day)
        month = d.month
        # 3、4 月订单量与金额下降 → 诊断主线有真实可查的下跌
        factor = 0.55 if month in (3, 4) else 1.0
        n_orders = max(1, int(random.gauss(14, 4) * factor))
        for _ in range(n_orders):
            oid_seq += 1
            oid = f"O{oid_seq:06d}"
            uid = random.choice(user_ids)
            pf = random.choice(PLATFORMS)
            status = random.choices(STATUSES, weights=[5, 10, 15, 60, 8, 2])[0]
            order_time = d + timedelta(hours=random.randint(0, 23), minutes=random.randint(0, 59))
            paid = status not in ("待付款", "已取消")
            payment_time = order_time + timedelta(minutes=random.randint(1, 60)) if paid else ""
            total, discount = 0.0, 0.0
            n_items = random.randint(1, 3)
            for _ in range(n_items):
                pid = random.choice(product_ids)
                qty = random.randint(1, 3)
                unit = round(random.uniform(20, 1500), 2)
                item_total = round(qty * unit, 2)
                total += item_total
                order_items.append([len(order_items) + 1, oid, pid, qty, unit, item_total, ""])
            discount = round(total * random.uniform(0, 0.2), 2)
            orders.append([oid, uid, pf, order_time.strftime("%Y-%m-%d %H:%M:%S"),
                           payment_time.strftime("%Y-%m-%d %H:%M:%S") if paid else "",
                           random.choice(["alipay", "wechat"]), "地址", status,
                           round(total, 2), discount, 0.0, 0.0, "", "",
                           random.choice(["express", "standard"])])
            # 用户行为漏斗：下单前通常有浏览/加购
            for btype in random.sample(BEHAVIORS, k=random.randint(1, 3)):
                bid_seq += 1
                behaviors.append([bid_seq, uid, random.choice(product_ids), pf,
                                  f"S{oid_seq}", btype,
                                  (order_time - timedelta(minutes=random.randint(5, 120))).strftime("%Y-%m-%d %H:%M:%S"),
                                  random.randint(5, 300), "/page", "", random.choice(["mobile", "pc"]),
                                  "1.0", "", "", ""])
    write("order.csv", ["order_id", "global_user_id", "platform", "order_time", "payment_time",
                        "payment_method", "shipping_address", "order_status", "total_amount",
                        "discount_amount", "shipping_fee", "tax_amount", "promotion_id",
                        "coupon_code", "shipping_method"], orders)
    write("order_item.csv", ["order_item_id", "order_id", "global_product_id", "quantity",
                             "unit_price", "item_total", "sku_info"], order_items)
    write("user_behavior.csv", ["behavior_id", "global_user_id", "global_product_id", "platform",
                                "session_id", "behavior_type", "behavior_time", "duration_seconds",
                                "page_url", "referrer", "device_type", "app_version",
                                "latitude", "longitude", "extra_data"], behaviors)
    print(f"完成：orders={len(orders)}, order_item={len(order_items)}, user_behavior={len(behaviors)}")


if __name__ == "__main__":
    main()
