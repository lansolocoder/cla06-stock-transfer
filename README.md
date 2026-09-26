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

## 调拨取消

对状态为 `shipped` 的在途调拨单做取消，整单原子处理（任一行退回不合法则整次拒绝，
台账与调拨单状态不变）：

```bash
python3 -m stock_transfer cancel --transfer TR-001 --reason 客户撤单
```

取消按调拨行逐行把发运数量全额退回发出仓对应批次：发出仓同批次行仍在则在原行数量上
累加，已不存在则追加新行（生产日期与有效期至沿用该批次现状）；接收仓数量保持 ship
时已入账的数值不动。调拨单状态精确变为 `canceled`，各调拨行实收数量全部记为 0，
与该单相关的差异不再挂账。成功后输出：

```text
取消成功：调拨单号=TR-001 退回总数=15
```

`--reason` 可选，去空白后为空则拒绝（原因仅校验非空，不参与输出）。调拨单不存在、
状态不是 `shipped`（`received`、`received-with-diff` 及重复取消均拒绝）、
`--transfer` 去空白后为空，统一写 stderr、退出码 1、stdout 为空且台账不变。
取消成功后不能再对该单收货确认。状态字面值精确为 `canceled`，不做大小写或拼写变体
兼容。

## 盘点调整

按仓库+商品对单个批次修正现存数量，并把修正原因留存备查：

```bash
python3 -m stock_transfer adjust \
    --warehouse WH-A --product SKU-1001 \
    --batch LOT-2024-001 --delta -3 --reason 盘点损耗
```

`--delta` 为带符号非零整数（如 `-3` 或 `+2`），`--reason` 去空白后非空。成功时按
delta 原子修改该批次现存数量（生产日期与有效期至不变；调整为 0 时该批次行按既有
惯例移除），输出一行仓库、商品、批次号、调整量与调整后数量（十进制整数）。批次号
在同一仓库同一商品内精确匹配唯一批次，不接受模糊匹配或大小写变体；调整不影响其他
仓库或其他商品的同名批次，也不改动任何调拨单及其状态。批次不存在、delta 为 0 或
非整数、reason 去空白为空、调整后数量小于 0，统一写 stderr、退出码 1、stdout 为
空且台账不变（不产生负库存）。

## 盘点记录查询

按仓库+商品列出历次盘点调整，含批次号、调整量、调整后数量、原因与发生时间
（`YYYY-MM-DD HH:MM:SS`），按发生时间升序、同一时刻按录入先后；无记录时输出空
列表，退出码 0：

```bash
python3 -m stock_transfer adjustments --warehouse WH-A --product SKU-1001
```

与其他命令一致，可用 `--db` 指定台账路径；成功与查询结果写 stdout，各失败情形写
stderr、stdout 为空且退出码 1。
