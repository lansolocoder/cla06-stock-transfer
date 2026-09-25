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
同一仓库加同一商品范围内批次号必须唯一，新批次号会追加为独立批次行。

## 调拨提交

把指定商品的一个或多个批次从发出仓调拨到接收仓，整单原子落账（任一行不合法则整单拒绝）。
调拨行格式为 `批次号,数量`，数量为正整数，`--line` 可重复提供；调拨单号在台账内全局唯一：

```bash
python3 -m stock_transfer ship \
    --transfer TR-001 --from WH-A --to WH-B --product SKU-1001 \
    --line LOT-2024-001,10 --line LOT-2024-002,5
```

提交时逐行校验：批次在发出仓存在、调拨数量不超过该批次现存数量、批次号在同一单内不重复、
调拨单号未占用、发出仓与接收仓去空白后非空且不相同；任一不满足则整单拒绝，台账不变。
成功后发出仓对应批次数量立即减少、接收仓同名批次立即增加（生产日期与有效期至沿用原批次；
接收仓已有同批次号时累加到该行），调拨单状态为 `shipped`，输出调拨单号与总数量。

## 收货确认

对状态为 `shipped` 的调拨单做收货确认。不给 `--line` 时按发运数量全部收下，
状态转为 `received`：

```bash
python3 -m stock_transfer receive --transfer TR-001
```

给 `--line 批次号,数量`（可重复）则按行核对实收数量；实收批次必须在调拨单内、不重复、
数量为正整数且合计不超过该批次发运数量，否则整单拒绝，台账与调拨单状态均不变。
核对通过后接收仓按实收数量入账，发运与实收之差记为差异数量留待后续处理，
状态转为 `received-with-diff` 并输出差异总数：

```bash
python3 -m stock_transfer receive --transfer TR-001 \
    --line LOT-2024-001,8 --line LOT-2024-002,5
```

重复确认或对不存在的调拨单确认均拒绝。状态字面值精确为 `shipped`、`received`、
`received-with-diff`，不做大小写或拼写变体兼容。
