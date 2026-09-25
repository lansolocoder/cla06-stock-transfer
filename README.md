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

## 调拨提交

把指定商品的若干批次从发出仓调拨到接收仓，整单原子落账（任一行不合法则整单拒绝，
已登记批次数量不变）。调拨行格式为 `批次号,数量`，数量为正整数，`--line` 可重复提供；
调拨单号在台账内全局唯一，发出仓与接收仓去空白后不能为空且不能相同：

```bash
python3 -m stock_transfer transfer \
    --transfer-no TR-2024-001 \
    --from-warehouse WH-A --to-warehouse WH-B --product SKU-1001 \
    --line LOT-2024-001,6 \
    --line LOT-2024-002,4
```

提交成功后发出仓对应批次数量立即减少、接收仓同名批次立即增加（批次生产日期与有效期至
沿用原批次行；接收仓已有同批次号时不新建行，数量累加到该行），调拨单状态为 `shipped`，
标准输出给出调拨单号与总数量。

## 收货确认

确认一张状态为 `shipped` 的调拨单。不提供实收行时按发运数量全部收下，
确认后状态为 `received`：

```bash
python3 -m stock_transfer receive --transfer-no TR-2024-001
```

也可提供实收行按行核对（格式同为 `批次号,数量`，可重复）。实收与发运的数量差记为
差异数量，留作后续差异处理，确认后状态为 `received-with-diff`，差异总数写入标准输出：

```bash
python3 -m stock_transfer receive --transfer-no TR-2024-001 \
    --line LOT-2024-001,5 --line LOT-2024-002,4
```

重复确认、对不存在的调拨单确认、实收行批次号不在单中、实收数量超过发运数量等
均被拒绝，台账与调拨单状态不变。

台账数据保存在当前工作目录的 `stock_ledger.db`（SQLite），可用 `--db` 指定其他路径。
同一仓库加同一商品范围内批次号必须唯一；新批次号会追加为独立批次行，
调拨时接收仓已有同批次号则直接累加数量。
