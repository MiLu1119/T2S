# 企业零售经营 SQLite 数据库

数据库文件：`data/enterprise_retail.sqlite`

生成脚本：`scripts/build_enterprise_retail_db.py`

运行以下命令可确定性重建数据库：

```bash
python scripts/build_enterprise_retail_db.py
```

脚本使用固定随机种子，适合重复进行 Text-to-SQL、Schema RAG、复杂 JOIN、聚合、时间分析和错误重写测试。

## 数据范围

- 19 张业务表；
- 5 个区域、30 家门店、10 个仓库；
- 240 名员工、1200 位客户；
- 40 个供应商、15 个品类、300 个商品；
- 3000 条仓库商品库存；
- 5000 个订单、15000 条订单明细；
- 5000 条支付、3115 条物流、348 条退货、882 条商品评价；
- 360 条月度门店销售目标；
- 订单日期覆盖 2024-01-01 至 2026-09-01。

## 表关系

核心销售链路：

```text
regions -> stores -> orders -> order_items -> products -> categories
                    |              |             |
customers ----------+           returns       suppliers
                    |
                    +-> payments
                    +-> shipments -> warehouses
                    +-> campaigns
                    +-> product_reviews
```

员工通过部门和门店关联销售订单；库存关联仓库与商品；销售目标按门店和月份保存。

## 推荐测试问题

- 2025 年各区域销售额和订单量是多少？
- 找出销售额最高的十个商品，并显示所属品类和供应商。
- 各门店 2026 年每月实际销售额与目标之间的差距是多少？
- 哪些商品的可用库存低于补货点？
- 不同营销渠道的订单转化金额和平均客单价是多少？
- 按退货原因统计退款金额，并找出退货率最高的品类。
- 金卡和黑金会员在不同城市的平均消费有什么差异？
- 各供应商商品的平均评分、销售额和退货率是多少？

## 完整性验证

生成脚本执行 `PRAGMA foreign_key_check`，发现任何外键错误时会直接失败。当前生成版本检查结果为 0 条错误。
