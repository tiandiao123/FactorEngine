# FactorEngine Event Schema（2026-04-06）

## 1. 文档目的

这份文档定义当前重构阶段下的统一事件模型。

目标是回答四个问题：

1. `dataflow` 内部到底有哪些标准事件类型
2. 每种事件包含哪些字段
3. 每个字段的语义、单位和来源是什么
4. 哪些字段属于 dataflow 的接入级标准化职责，哪些不属于

这份文档是：

- Python `dataflow` 层的内部 schema 规范
- 后续 `scheduler` 和 `C++ runtime bridge` 的输入约束
- `cache` 设计和 live test 的共同基础

配套文档：

- [refactor_design.md](/home/ubuntu/workspace/FactorEngine/docs/20260406/refactor_design.md)
- [development_plan.md](/home/ubuntu/workspace/FactorEngine/docs/20260406/development_plan.md)

---

## 2. 总体原则

### 2.1 统一事件基类

所有 market event 都共享四个基础元字段：

- `symbol`
- `channel`
- `ts_event`
- `ts_recv`

这些字段在当前代码中由 [events.py](/home/ubuntu/workspace/FactorEngine/dataflow/events.py) 中的 `MarketEvent` 表达。

### 2.2 两种时间必须同时存在

每个事件至少必须带：

- `ts_event`
  交易所事件时间
- `ts_recv`
  本机接收时间

原因：

- 后续要支持延迟观测
- 后续要支持事件时间对齐
- 后续要支持回放和诊断

### 2.3 dataflow 只做接入级标准化

这里的 schema 只用于表达：

- 原始市场数据
- 接入层最小标准化后的内部表示

这里明确不表达：

- VWAP
- imbalance
- microprice
- 分钟级窗口统计
- 因子表达式结果

这些属于 factor runtime 层，不属于 event schema 本身。

---

## 3. 当前统一事件类型

当前 dataflow 内部定义了四个主要结构：

1. `MarketEvent`
2. `BarEvent`
3. `TradeEvent`
4. `BookEvent`

另外 `BookEvent` 内部还使用：

5. `BookLevel`

当前代码位置：

- [events.py](/home/ubuntu/workspace/FactorEngine/dataflow/events.py)

---

## 4. MarketEvent

`MarketEvent` 是所有市场事件的共同父结构。

### 字段表

| 字段 | 类型 | 含义 | 示例 |
|------|------|------|------|
| `symbol` | `str` | 交易标的 ID | `BTC-USDT-SWAP` |
| `channel` | `str` | 事件来源 channel | `bar_5s` / `trades-all` / `books5` |
| `ts_event` | `int` | 交易所事件时间，毫秒 | `1712385600000` |
| `ts_recv` | `int` | 本机接收时间，毫秒 | `1712385600027` |

### 字段约束

- `symbol` 必须是 OKX `instId`
- `channel` 必须明确标识事件来源
- `ts_event` / `ts_recv` 一律使用毫秒整数

### 说明

`MarketEvent` 本身不直接单独使用，更多是 `BarEvent`、`TradeEvent`、`BookEvent` 的共同字段集合。

---

## 5. BarEvent

`BarEvent` 表达一根已经完成的 bar。

当前代码位置：

- [events.py](/home/ubuntu/workspace/FactorEngine/dataflow/events.py#L18)

当前由：

- [aggregator.py](/home/ubuntu/workspace/FactorEngine/dataflow/bars/aggregator.py)

生成。

### 字段表

| 字段 | 类型 | 含义 | 单位 |
|------|------|------|------|
| `symbol` | `str` | 交易标的 | OKX instId |
| `channel` | `str` | bar 频道名 | 例如 `bar_5s` |
| `ts_event` | `int` | 该 bar 的起始事件时间 | ms |
| `ts_recv` | `int` | 聚合完成时最后一条输入记录的接收时间 | ms |
| `open` | `float` | 开盘价 | price |
| `high` | `float` | 最高价 | price |
| `low` | `float` | 最低价 | price |
| `close` | `float` | 收盘价 | price |
| `vol` | `float` | 成交量 | 当前实现使用 OKX candle `vol` |

### 当前来源

当前 bar 路径是：

```text
OKX candle1s
-> BarAggregator
-> BarEvent
```

### 当前聚合语义

以 `agg_seconds = 5` 为例：

- `open` = 第 1 根 1s candle 的 open
- `high` = 窗口内最高价
- `low` = 窗口内最低价
- `close` = 最后 1 根 1s candle 的 close
- `vol` = 窗口内 `vol` 求和

### 注意

当前实现的聚合方式是：

- **按收到的确认 1s candle 数量计数聚合**

不是严格的绝对时钟边界对齐聚合。

这意味着：

- 它适合作为当前 bar cache 的内部表示
- 但如果未来对时钟对齐要求更高，可能要升级聚合器

### 当前 cache 映射

`BarEvent` 进入 [BarCache](/home/ubuntu/workspace/FactorEngine/dataflow/cache.py#L16) 后，会被转换为：

```text
ndarray shape=(N, 6)
columns=[ts, open, high, low, close, vol]
```

也就是说：

- `BarEvent` 是逻辑层结构
- `BarCache` 当前仍为了兼容性保留 numpy 二维数组存储

---

## 6. TradeEvent

`TradeEvent` 表达一条 trade-level 事件。

当前代码位置：

- [events.py](/home/ubuntu/workspace/FactorEngine/dataflow/events.py#L29)

当前由：

- [trade_collector.py](/home/ubuntu/workspace/FactorEngine/dataflow/okx/trade_collector.py)

生成。

### 字段表

| 字段 | 类型 | 含义 | 单位 |
|------|------|------|------|
| `symbol` | `str` | 交易标的 | OKX instId |
| `channel` | `str` | trade 来源 channel | `trades` / `trades-all` |
| `ts_event` | `int` | 交易所成交时间 | ms |
| `ts_recv` | `int` | 本机接收时间 | ms |
| `trade_id` | `str \| None` | 交易所 trade id | string |
| `px` | `float` | 成交价 | price |
| `sz` | `float` | 成交量 | size |
| `side` | `str` | 主动方向 | `buy` / `sell` |
| `count` | `int` | 该消息中聚合的成交笔数 | count |
| `is_aggregated` | `bool` | 是否是聚合 trade 事件 | bool |

### 当前 channel 语义

支持两种：

#### `trades`

- 来自 OKX public WS
- 可能一条更新里带多笔成交
- 因此：
  - `count` 可能大于 `1`
  - `is_aggregated = True`

#### `trades-all`

- 来自 OKX business WS
- 更接近逐笔成交
- 当前视为：
  - `count = 1` 为主
  - `is_aggregated = False`

### 当前 schema 约束

- `px`、`sz` 都转成 `float`
- `count` 强制转成 `int`
- 若原始数据缺 `count`，默认记为 `1`

### 当前 cache 映射

`TradeEvent` 进入 [TradeCache](/home/ubuntu/workspace/FactorEngine/dataflow/cache.py#L70) 后，以：

```text
dict[str, deque[TradeEvent]]
```

形式保存。

### 为什么 TradeCache 不转成 ndarray

因为 trade 是天然事件流，不像 bar 那样天然适合二维表。

后续 runtime 更可能需要：

- 最近 N 笔
- 最近 N 秒
- 保留原始事件顺序

因此当前阶段 `TradeEvent` 直接存对象更合适。

---

## 7. BookLevel

`BookLevel` 表达 order book 中的一个价格档位。

当前代码位置：

- [events.py](/home/ubuntu/workspace/FactorEngine/dataflow/events.py#L41)

### 字段表

| 字段 | 类型 | 含义 |
|------|------|------|
| `px` | `float` | 档位价格 |
| `sz` | `float` | 档位总挂单量 |
| `orders` | `int \| None` | 该档位订单数，当前可能为空 |

### 当前来源

当前来自 OKX `books5` 每个 level 的原始 list。

当前 collector 只提取：

- `px`
- `sz`
- `orders`

不保留原始 list 的其他无用占位字段。

---

## 8. BookEvent

`BookEvent` 表达一次浅簿快照事件。

当前代码位置：

- [events.py](/home/ubuntu/workspace/FactorEngine/dataflow/events.py#L50)

当前由：

- [book_collector.py](/home/ubuntu/workspace/FactorEngine/dataflow/okx/book_collector.py)

生成。

### 字段表

| 字段 | 类型 | 含义 |
|------|------|------|
| `symbol` | `str` | 交易标的 |
| `channel` | `str` | 当前为 `books5` |
| `ts_event` | `int` | 盘口快照时间 |
| `ts_recv` | `int` | 本机接收时间 |
| `best_bid_px` | `float` | 买一价 |
| `best_bid_sz` | `float` | 买一量 |
| `best_ask_px` | `float` | 卖一价 |
| `best_ask_sz` | `float` | 卖一量 |
| `bids` | `list[BookLevel]` | 买盘档位列表 |
| `asks` | `list[BookLevel]` | 卖盘档位列表 |

### 当前语义

当前只支持：

- `books5`

因此 `bids` / `asks` 理论上最多各 5 档。

### 当前未纳入的字段

OKX book 原始数据里还可能有：

- `seqId`
- `checksum`

这些字段当前还没有纳入 `BookEvent`。

原因：

- 当前重构阶段目标是先建立浅簿主路径
- `seqId/checksum` 更偏向深簿恢复和一致性校验场景

后续如果接增量簿，建议再扩展：

- `sequence_id`
- `checksum`
- `is_snapshot`

### 当前 cache 映射

`BookEvent` 进入 [BookCache](/home/ubuntu/workspace/FactorEngine/dataflow/cache.py#L122) 后，以两部分保存：

1. `latest`
   - 每个 symbol 一份最新快照
2. `history`
   - 每个 symbol 一个短历史 ring buffer

这符合浅簿数据最常见的两类读取方式：

- 取当前最新盘口
- 回看最近一小段盘口变化

---

## 9. Cache 级 schema

这一节不是事件定义本身，但用于说明“事件进 cache 后长什么样”。

### 9.1 BarCache

逻辑输入：

- `BarEvent`

物理存储：

```text
dict[str, ndarray]
shape=(N, 6)
columns=[ts, open, high, low, close, vol]
```

### 9.2 TradeCache

逻辑输入：

- `TradeEvent`

物理存储：

```text
dict[str, deque[TradeEvent]]
```

### 9.3 BookCache

逻辑输入：

- `BookEvent`

物理存储：

```text
latest:  dict[str, BookEvent]
history: dict[str, deque[BookEvent]]
```

---

## 10. Engine 级读接口

当前 `Engine` 对外暴露三个读取入口：

- [get_data()](/home/ubuntu/workspace/FactorEngine/factorengine/engine.py#L88)
- [get_trade_data()](/home/ubuntu/workspace/FactorEngine/factorengine/engine.py#L102)
- [get_book_data()](/home/ubuntu/workspace/FactorEngine/factorengine/engine.py#L106)

对应返回：

### `get_data()`

```python
dict[str, np.ndarray]
```

当前仍是 bar 兼容接口。

### `get_trade_data()`

```python
dict[str, list[TradeEvent]]
```

### `get_book_data()`

```python
dict[str, BookEvent]
```

这三个接口共同体现了一个事实：

- 当前系统已经不是“只有一种 data_cache”
- 而是三类数据各自有不同 schema

---

## 11. 未来建议新增的事件

当前 schema 已够搭建 dataflow 主路径，但后续很可能还会新增：

### 11.1 TimerEvent

作用：

- scheduler 在 evaluation tick 上触发 factor runtime

建议字段：

- `ts_event`
- `ts_recv`
- `interval_ms`
- `tick_id`

### 11.2 FactorSnapshot

作用：

- 表达某一评估时刻的最终因子输出

建议字段：

- `symbol`
- `ts_eval`
- `factor_values: dict[str, float]`
- `metadata`

### 11.3 RuntimeMetricEvent

作用：

- 表达内部延迟、背压、丢弃等运行时观测信息

这个可以帮助后续做监控和诊断。

---

## 12. 非职责范围

这一节非常重要，用来避免 schema 被无限膨胀。

当前 event schema **不负责定义**：

- 因子表达式
- 因子窗口配置
- 因子依赖关系
- 模型输入张量格式
- 信号触发规则
- 下单事件

这些应分别属于：

- scheduler / runtime
- inference layer
- execution layer

---

## 13. 当前最重要的约束

如果只保留三条约束，当前阶段最重要的是：

1. 所有 market event 都必须带 `symbol`、`channel`、`ts_event`、`ts_recv`
2. dataflow 只做接入级标准化，不在 event schema 中掺入因子逻辑
3. cache schema 可以不同，但内部事件对象必须统一、清晰、稳定

只要这三条守住，后续接 scheduler 和 C++ runtime 时，接口会清晰很多。
