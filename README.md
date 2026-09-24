# 多仓库存调拨与批次追溯

用于本地多仓库库存调拨与批次追溯管理的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m stock_transfer --help
python3 -m stock_transfer --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助，未知参数以非零状态退出。

## 库存登记

把入库商品按批次登记到指定仓库，一次提交一次落账（任一行不合法则整次全部拒绝）。
批次行格式为 `批次号,生产日期,有效期至,数量`，日期为 `YYYY-MM-DD`，数量为正整数，
`--batch` 可重复提供：

```bash
python3 -m stock_transfer register \
    --warehouse WH-A --product SKU-1001 \
    --batch LOT-2024-001,2024-03-01,2025-03-01,18 \
    --batch LOT-2024-002,2024-04-02,2025-04-02,12
```

成功后输出仓库、商品、本次总数量与批次数。

## 批次查询

查询某仓库某商品下每个批次的批次号、生产日期、有效期至与现存数量；
无数据时输出空批次列表，退出码为 0：

```bash
python3 -m stock_transfer query --warehouse WH-A --product SKU-1001
```

台账数据保存在当前工作目录的 `stock_ledger.db`（SQLite），可用 `--db` 指定其他路径。
同一仓库加同一商品范围内批次号必须唯一；本项目目前只登记不扣减，
新批次号会追加为独立批次行。
