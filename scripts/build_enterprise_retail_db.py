"""Build a deterministic, relational SQLite dataset for Text-to-SQL demos."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import random
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data" / "enterprise_retail.sqlite"
RNG = random.Random(20260918)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE regions (
  region_id INTEGER PRIMARY KEY, region_name TEXT NOT NULL UNIQUE,
  manager_name TEXT NOT NULL, annual_budget REAL NOT NULL
);
CREATE TABLE stores (
  store_id INTEGER PRIMARY KEY, store_name TEXT NOT NULL, region_id INTEGER NOT NULL,
  city TEXT NOT NULL, opened_date TEXT NOT NULL, area_sqm INTEGER NOT NULL,
  store_type TEXT NOT NULL CHECK(store_type IN ('旗舰店','标准店','社区店')),
  FOREIGN KEY(region_id) REFERENCES regions(region_id)
);
CREATE TABLE departments (
  department_id INTEGER PRIMARY KEY, department_name TEXT NOT NULL UNIQUE,
  cost_center TEXT NOT NULL UNIQUE
);
CREATE TABLE employees (
  employee_id INTEGER PRIMARY KEY, employee_name TEXT NOT NULL,
  department_id INTEGER NOT NULL, store_id INTEGER, job_title TEXT NOT NULL,
  hire_date TEXT NOT NULL, salary REAL NOT NULL, employment_status TEXT NOT NULL,
  manager_id INTEGER, FOREIGN KEY(department_id) REFERENCES departments(department_id),
  FOREIGN KEY(store_id) REFERENCES stores(store_id), FOREIGN KEY(manager_id) REFERENCES employees(employee_id)
);
CREATE TABLE customers (
  customer_id INTEGER PRIMARY KEY, customer_name TEXT NOT NULL,
  gender TEXT NOT NULL, birth_date TEXT NOT NULL, city TEXT NOT NULL,
  member_level TEXT NOT NULL CHECK(member_level IN ('普通','银卡','金卡','黑金')),
  registered_date TEXT NOT NULL, acquisition_channel TEXT NOT NULL,
  lifetime_points INTEGER NOT NULL, is_active INTEGER NOT NULL CHECK(is_active IN (0,1))
);
CREATE TABLE customer_addresses (
  address_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, province TEXT NOT NULL,
  city TEXT NOT NULL, district TEXT NOT NULL, address_type TEXT NOT NULL,
  is_default INTEGER NOT NULL, FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);
CREATE TABLE suppliers (
  supplier_id INTEGER PRIMARY KEY, supplier_name TEXT NOT NULL, province TEXT NOT NULL,
  contact_name TEXT NOT NULL, rating REAL NOT NULL, cooperation_since TEXT NOT NULL,
  payment_terms_days INTEGER NOT NULL
);
CREATE TABLE categories (
  category_id INTEGER PRIMARY KEY, category_name TEXT NOT NULL UNIQUE,
  parent_category_id INTEGER, FOREIGN KEY(parent_category_id) REFERENCES categories(category_id)
);
CREATE TABLE products (
  product_id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, product_name TEXT NOT NULL,
  category_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL, brand TEXT NOT NULL,
  unit_cost REAL NOT NULL, list_price REAL NOT NULL, launch_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('在售','停售','缺货')),
  FOREIGN KEY(category_id) REFERENCES categories(category_id),
  FOREIGN KEY(supplier_id) REFERENCES suppliers(supplier_id)
);
CREATE TABLE warehouses (
  warehouse_id INTEGER PRIMARY KEY, warehouse_name TEXT NOT NULL,
  region_id INTEGER NOT NULL, city TEXT NOT NULL, capacity_units INTEGER NOT NULL,
  FOREIGN KEY(region_id) REFERENCES regions(region_id)
);
CREATE TABLE inventory (
  warehouse_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
  stock_quantity INTEGER NOT NULL, reserved_quantity INTEGER NOT NULL,
  reorder_point INTEGER NOT NULL, last_restock_date TEXT NOT NULL,
  PRIMARY KEY(warehouse_id, product_id),
  FOREIGN KEY(warehouse_id) REFERENCES warehouses(warehouse_id),
  FOREIGN KEY(product_id) REFERENCES products(product_id)
);
CREATE TABLE campaigns (
  campaign_id INTEGER PRIMARY KEY, campaign_name TEXT NOT NULL, channel TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT NOT NULL, budget REAL NOT NULL,
  discount_rate REAL NOT NULL
);
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY, order_no TEXT NOT NULL UNIQUE,
  customer_id INTEGER NOT NULL, store_id INTEGER, campaign_id INTEGER,
  sales_employee_id INTEGER, order_date TEXT NOT NULL, channel TEXT NOT NULL,
  order_status TEXT NOT NULL, subtotal REAL NOT NULL, discount_amount REAL NOT NULL,
  shipping_fee REAL NOT NULL, total_amount REAL NOT NULL,
  FOREIGN KEY(customer_id) REFERENCES customers(customer_id),
  FOREIGN KEY(store_id) REFERENCES stores(store_id),
  FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
  FOREIGN KEY(sales_employee_id) REFERENCES employees(employee_id)
);
CREATE TABLE order_items (
  order_item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL, unit_price REAL NOT NULL, discount_rate REAL NOT NULL,
  line_amount REAL NOT NULL, FOREIGN KEY(order_id) REFERENCES orders(order_id),
  FOREIGN KEY(product_id) REFERENCES products(product_id)
);
CREATE TABLE payments (
  payment_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, payment_method TEXT NOT NULL,
  payment_status TEXT NOT NULL, paid_amount REAL NOT NULL, paid_at TEXT,
  transaction_no TEXT, FOREIGN KEY(order_id) REFERENCES orders(order_id)
);
CREATE TABLE shipments (
  shipment_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL,
  carrier TEXT NOT NULL, tracking_no TEXT NOT NULL UNIQUE, shipped_date TEXT,
  delivered_date TEXT, shipping_status TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(order_id),
  FOREIGN KEY(warehouse_id) REFERENCES warehouses(warehouse_id)
);
CREATE TABLE returns (
  return_id INTEGER PRIMARY KEY, order_item_id INTEGER NOT NULL, return_date TEXT NOT NULL,
  return_reason TEXT NOT NULL, return_quantity INTEGER NOT NULL,
  refund_amount REAL NOT NULL, return_status TEXT NOT NULL,
  FOREIGN KEY(order_item_id) REFERENCES order_items(order_item_id)
);
CREATE TABLE product_reviews (
  review_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
  review_text TEXT NOT NULL, review_date TEXT NOT NULL,
  FOREIGN KEY(customer_id) REFERENCES customers(customer_id),
  FOREIGN KEY(product_id) REFERENCES products(product_id),
  FOREIGN KEY(order_id) REFERENCES orders(order_id)
);
CREATE TABLE monthly_sales_targets (
  target_id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL, target_month TEXT NOT NULL,
  revenue_target REAL NOT NULL, order_target INTEGER NOT NULL,
  UNIQUE(store_id, target_month), FOREIGN KEY(store_id) REFERENCES stores(store_id)
);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_inventory_stock ON inventory(stock_quantity);
"""


def d(start: date, days: int) -> str:
    return (start + timedelta(days=days)).isoformat()


def build(target: Path = TARGET) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(target)
    conn.executescript(SCHEMA)

    regions = [(1,"华东","陈志远",8_000_000),(2,"华南","林晓雯",6_500_000),(3,"华北","赵明宇",7_200_000),(4,"西南","周雨欣",4_800_000),(5,"华中","王博文",5_300_000)]
    conn.executemany("INSERT INTO regions VALUES (?,?,?,?)", regions)
    cities = [("上海",1),("杭州",1),("南京",1),("广州",2),("深圳",2),("北京",3),("天津",3),("成都",4),("重庆",4),("武汉",5),("长沙",5)]
    stores=[]
    for i in range(1,31):
        city, region= cities[(i-1)%len(cities)]
        stores.append((i,f"{city}{i:02d}店",region,city,d(date(2016,1,1),i*79),RNG.randint(220,2200),("旗舰店" if i%8==0 else "社区店" if i%3==0 else "标准店")))
    conn.executemany("INSERT INTO stores VALUES (?,?,?,?,?,?,?)",stores)
    departments=[(1,"管理中心","CC100"),(2,"销售部","CC200"),(3,"商品部","CC300"),(4,"供应链部","CC400"),(5,"客户服务部","CC500"),(6,"市场部","CC600"),(7,"财务部","CC700"),(8,"信息技术部","CC800")]
    conn.executemany("INSERT INTO departments VALUES (?,?,?)",departments)
    surnames="赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜"; given="子轩浩宇欣怡雨桐思远嘉宁明哲若溪文博雅琪俊杰可心天佑诗涵"
    employees=[]
    for i in range(1,241):
        dep=2 if i<=150 else RNG.randint(1,8); store=(i%30)+1 if dep in {2,5} else None
        title="店长" if i<=30 else "销售顾问" if dep==2 else ["专员","主管","经理","分析师"][i%4]
        manager=None if i<=8 else 1+((i-9)%8)
        employees.append((i,surnames[i%len(surnames)]+given[i%len(given):i%len(given)+2],dep,store,title,d(date(2017,1,1),i*11),round(5500+(i%17)*650+dep*380,2),"在职" if i%19 else "离职",manager))
    conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?)",employees)
    channels=["自然到店","微信公众号","短视频","搜索广告","老客推荐","会员活动"]
    levels=["普通","普通","普通","银卡","银卡","金卡","黑金"]
    customers=[]; addresses=[]
    for i in range(1,1201):
        city,region=cities[i%len(cities)]
        customers.append((i,f"客户{i:04d}","女" if i%2 else "男",d(date(1965,1,1),(i*37)%14000),city,levels[i%len(levels)],d(date(2019,1,1),(i*13)%2400),channels[i%len(channels)],RNG.randint(0,50000),0 if i%37==0 else 1))
        addresses.append((i,i,["上海市","浙江省","江苏省","广东省","广东省","北京市","天津市","四川省","重庆市","湖北省","湖南省"][i%11],city,f"{['朝阳','滨江','天河','高新','中心'][i%5]}区","家庭",1))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?)",customers)
    conn.executemany("INSERT INTO customer_addresses VALUES (?,?,?,?,?,?,?)",addresses)
    suppliers=[]
    for i in range(1,41):
        suppliers.append((i,f"{cities[i%len(cities)][0]}优选供应链{i:02d}号",["浙江","广东","江苏","山东","福建","四川"][i%6],f"联系人{i:02d}",round(3.5+(i%16)/10,1),d(date(2014,1,1),i*101),[15,30,45,60][i%4]))
    conn.executemany("INSERT INTO suppliers VALUES (?,?,?,?,?,?,?)",suppliers)
    parent=[(1,"数码家电",None),(2,"食品饮料",None),(3,"家居生活",None),(4,"服饰运动",None),(5,"个护美妆",None)]
    children=[(6,"手机通讯",1),(7,"电脑办公",1),(8,"休闲零食",2),(9,"酒水冲饮",2),(10,"厨具",3),(11,"家纺",3),(12,"男装",4),(13,"运动户外",4),(14,"护肤",5),(15,"清洁护理",5)]
    conn.executemany("INSERT INTO categories VALUES (?,?,?)",parent+children)
    brands=["星云","青禾","极光","山海","云杉","万象","晨光","元气","匠心","远航"]
    products=[]
    for i in range(1,301):
        cost=round(8+(i%43)*7.35+(i%9)*19,2); price=round(cost*(1.25+(i%8)*.06),2)
        products.append((i,f"SKU{i:06d}",f"{brands[i%10]}{children[i%10][1]}商品{i:03d}",6+i%10,1+i%40,brands[i%10],cost,price,d(date(2020,1,1),(i*17)%2100),"停售" if i%41==0 else "缺货" if i%53==0 else "在售"))
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)",products)
    warehouses=[(i,f"{cities[i%len(cities)][0]}中心仓",1+(i-1)%5,cities[i%len(cities)][0],70000+i*15000) for i in range(1,11)]
    conn.executemany("INSERT INTO warehouses VALUES (?,?,?,?,?)",warehouses)
    inventory=[]
    for w in range(1,11):
        for p in range(1,301):
            inventory.append((w,p,RNG.randint(0,600),RNG.randint(0,40),RNG.randint(20,90),d(date(2025,1,1),RNG.randint(0,620))))
    conn.executemany("INSERT INTO inventory VALUES (?,?,?,?,?,?)",inventory)
    campaigns=[]
    for i in range(1,25):
        start=date(2024,1,1)+timedelta(days=i*28); campaigns.append((i,f"营销活动{i:02d}",["短信","App","短视频","线下","搜索"][i%5],start.isoformat(),(start+timedelta(days=18)).isoformat(),80000+i*17000,round(.05+(i%6)*.03,2)))
    conn.executemany("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",campaigns)

    item_id=payment_id=shipment_id=review_id=return_id=0
    order_rows=[]; item_rows=[]; payment_rows=[]; shipment_rows=[]; review_rows=[]; return_rows=[]
    product_prices={row[0]:row[7] for row in products}
    for oid in range(1,5001):
        customer=1+(oid*17)%1200; online=oid%3!=0; store=None if online else 1+oid%30
        order_day=date(2024,1,1)+timedelta(days=(oid*7)%975)
        campaign=1+oid%24 if oid%4==0 else None; discount=.05+(oid%6)*.02 if campaign else 0
        lines=[]
        for j in range(1,2+oid%5):
            item_id+=1; product=1+(oid*13+j*31)%300; qty=1+(oid+j)%4; price=product_prices[product]
            amount=round(qty*price*(1-discount),2); lines.append(amount)
            item_rows.append((item_id,oid,product,qty,price,discount,amount))
            if item_id%17==0:
                review_id+=1; rating=1+(item_id%5); review_rows.append((review_id,customer,product,oid,rating,["质量很好，符合预期","物流速度快","性价比不错","包装可以改进","会再次购买"][rating-1],(order_day+timedelta(days=9)).isoformat()))
            if item_id%43==0:
                return_id+=1; return_rows.append((return_id,item_id,(order_day+timedelta(days=12)).isoformat(),["尺寸不合适","质量问题","错发漏发","不再需要"][item_id%4],1,round(min(amount,price),2),["已退款","审核中","已拒绝"][item_id%3]))
        subtotal=round(sum(x/(1-discount) for x in lines),2); discounted=round(subtotal-sum(lines),2); shipping=0 if subtotal>=199 else 12
        status="已取消" if oid%29==0 else "待付款" if oid%31==0 else "已完成"
        total=round(subtotal-discounted+shipping,2)
        order_rows.append((oid,f"ORD{order_day:%Y%m%d}{oid:06d}",customer,store,campaign,1+oid%150,order_day.isoformat(),"线上" if online else "门店",status,subtotal,discounted,shipping,total))
        payment_id+=1; paid=status not in {"已取消","待付款"}
        payment_rows.append((payment_id,oid,["支付宝","微信支付","银行卡","会员余额"][oid%4],"支付成功" if paid else "未支付",total if paid else 0,(order_day.isoformat()+" 10:30:00") if paid else None,f"TXN{oid:012d}" if paid else None))
        if online and status=="已完成":
            shipment_id+=1; shipment_rows.append((shipment_id,oid,1+oid%10,["顺丰","京东物流","中通","圆通"][oid%4],f"TRK{oid:012d}",d(order_day,1),d(order_day,2+oid%6),"已签收"))
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",order_rows)
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?,?)",item_rows)
    conn.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?,?)",payment_rows)
    conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?)",shipment_rows)
    conn.executemany("INSERT INTO product_reviews VALUES (?,?,?,?,?,?,?)",review_rows)
    conn.executemany("INSERT INTO returns VALUES (?,?,?,?,?,?,?)",return_rows)
    targets=[]; tid=0
    for store in range(1,31):
        for month in range(1,13):
            tid+=1; targets.append((tid,store,f"2026-{month:02d}",350000+store*8000+month*12000,700+store*9+month*15))
    conn.executemany("INSERT INTO monthly_sales_targets VALUES (?,?,?,?,?)",targets)
    conn.commit()
    failed=conn.execute("PRAGMA foreign_key_check").fetchall()
    if failed:
        raise RuntimeError(f"foreign key check failed: {failed[:3]}")
    conn.execute("ANALYZE")
    conn.close()
    print(target)


if __name__ == "__main__":
    build()
