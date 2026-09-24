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

## 调拨提交与在途跟踪

把指定商品按批次从来源仓调出到目标仓，一次提交生成一张调拨单。
调拨行格式为 `批次号,调出数量`（调出数量为正整数，是本行唯一的数量含义），
`--line` 可重复提供以在一单内调出同一商品的多个批次：

```bash
python3 -m stock_transfer transfer \
    --order TR-0001 --source WH-A --target WH-B --product SKU-1001 \
    --line LOT-2024-001,5 \
    --line LOT-2024-002,3
```

校验规则：调拨单号、来源仓、目标仓、商品代码与批次号去首尾空白后非空，
目标仓不得与来源仓相同；批次号在同一单内不得重复，且必须是来源仓该商品
已落账的批次；任一行调出数量超出来源仓该批次现存数量即整单拒绝。
调拨单号在全部调拨单中唯一，重复单号拒绝且不改动任何记录。

提交成功后整单一次落账：来源仓各批次现存数量立即扣减（扣至零的批次行移除），
目标仓不立即收货，单据状态为 `in_transit`，实收数量为 0。
状态值只允许 `in_transit`、`received`、`cancelled`（区分大小写）。

按调拨单号查询单据的来源仓、目标仓、商品、状态、实收数量与各批次调出数量；
无此单时输出空结果，退出码为 0：

```bash
python3 -m stock_transfer transfer-query --order TR-0001
```

调拨数据与登记数据保存在同一个台账库中，可用 `--db` 指定路径。

台账数据保存在当前工作目录的 `stock_ledger.db`（SQLite），可用 `--db` 指定其他路径。
同一仓库加同一商品范围内批次号必须唯一；新批次号会追加为独立批次行。
