# 多仓库存调拨与批次追溯

用于本地多仓库库存调拨与批次追溯管理的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m stock_transfer --help
python3 -m stock_transfer --version
python3 -m unittest discover -s tests -v
```

## 库存登记（入库）

把入库商品按批次登记到指定仓库，台账数据保存在当前工作目录的
`stock_ledger.db`（sqlite3），重复运行命令会复用该文件：

```bash
python3 -m stock_transfer register --warehouse WH-A --product SKU-1001 \
    --batch LOT-2024-001,2024-03-01,2025-03-01,18 \
    --batch LOT-2024-002,2024-04-02,2025-04-02,12
```

`--batch` 可重复，每行格式为 `批次号,生产日期,有效期至,数量`：

- 日期为 `YYYY-MM-DD`，有效期至必须晚于生产日期；
- 数量必须为正整数；
- 批次号在同一仓库加同一商品范围内唯一（与已落账批次或本次其它行重复均拒绝）；
- 仓库代码、商品代码、批次号去首尾空白后不能为空。

任一批次行不合法时整次登记全部拒绝：不产生部分记录，已有批次数量不变，
命令以非零状态退出，并在标准错误中指出批次行序号与失败字段。
登记成功时输出仓库、商品、本次总数量与批次数。

## 批次查询

```bash
python3 -m stock_transfer query --warehouse WH-A --product SKU-1001
```

输出 JSON，列出该仓库该商品下每个批次的批次号、生产日期、有效期至与现存数量；
无数据时 `batches` 为空列表，退出码为 0。当前版本只做登记，不做任何扣减。

无参数显示帮助，未知参数以非零状态退出。
