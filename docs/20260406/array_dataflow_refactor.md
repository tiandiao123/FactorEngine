# FactorEngine 数组化 Dataflow 重构方案（2026-04-06）

## 1. 为什么要改

当前 dataflow 重构到一半时，引入了 `BarEvent`、`TradeEvent`、`BookEvent` 这类 event class。

这种做法对“接入层调试”是方便的，但对最终的因子引擎并不合适。

核心问题有三个：

1. `collector -> event object -> cache -> factorengine` 这条链路多了一层对象封装和拆解。
2. factor 计算真正需要的是连续数组，而不是一堆 Python 对象。
3. 高频场景下，反复创建 event class、再从对象里取字段，会给后续因子计算和模型推理增加额外延迟。

因此，新的方向应该明确：

- **dataflow 的正式热路径不使用任何 event class**
- **factorengine 只看到数组**
- **collector 如需保留额外元数据，也只在 collector/debug 层保留，不进入 factorengine 主接口**

---

## 2. 目标原则

### 2.1 对 factorengine 的返回必须是数组

目标形态：

- `bars` -> `dict[symbol, ndarray]`
- `trades` -> `dict[symbol, ndarray]`
- `books` -> `dict[symbol, ndarray]`

也就是说：

```python
bar_snapshot = engine.get_data(symbols)
trade_snapshot = engine.get_trade_data(symbols)
book_snapshot = engine.get_book_data(symbols)
```

这三者返回的都应该是：

```python
dict[str, np.ndarray]
```

而不是 `TradeEvent` 列表或 `BookEvent` 对象。

### 2.2 collector 可以维护元数据，但不传给 factorengine

例如：

- `trade_id`
- `ts_recv`
- `channel`
- `seqId`
- `checksum`

这些可以：

- 在 collector 内部维护
- 在 debug file 里落盘
- 在诊断逻辑里保留

但不要进入 factorengine 主计算接口。

### 2.3 cache 的设计优先考虑因子计算，而不是协议还原

也就是说，cache 的排布方式应该围绕：

- 窗口切片方便
- 向量化方便
- C++ runtime 接入方便

而不是围绕“原始 JSON 长什么样”。

---

## 3. 正式建议的数据排布

## 3.1 Bars

bars 继续保持当前形式：

```text
dict[str, ndarray]
shape=(N, 6)
columns=[ts, open, high, low, close, vol]
```

这个结构已经足够好，不需要推翻。

### 例子

```python
bar_snapshot["BTC-USDT-SWAP"].shape == (N, 6)
```

```python
ts    = arr[:, 0]
open  = arr[:, 1]
high  = arr[:, 2]
low   = arr[:, 3]
close = arr[:, 4]
vol   = arr[:, 5]
```

---

## 3.2 Trades

trades 不再对 factorengine 返回 `TradeEvent` 列表。

### 建议正式结构

```text
dict[str, ndarray]
shape=(N, 3)
columns=[px, sz, side]
```

其中：

- `px`: 成交价
- `sz`: 成交量
- `side`: 方向编码
  - `buy = 1`
  - `sell = -1`

### 为什么去掉 `trade_id`

因为对绝大多数 factor 计算没有帮助，还会增加数据处理负担。

### 为什么可以去掉 `ts`

这个设计成立的前提是：

- factorengine 的主工作流是固定 evaluation tick
- trade cache 更偏向“最近 N 笔 / 当前缓冲区”
- 时间边界由 scheduler/runtime 控制，而不是由每条 trade 记录自己携带

如果未来需要严格时间窗，可以：

- 在 collector 内部保留 `ts`
- 或者在 runtime 内维护独立时间轴

但对 factorengine 主返回接口，不必默认暴露。

### 例子

```python
trade_snapshot["BTC-USDT-SWAP"].shape == (N, 3)
```

```python
px   = arr[:, 0]
sz   = arr[:, 1]
side = arr[:, 2]
```

### 典型 trades 因子

有了 `[px, sz, side]`，已经可以计算很多因子：

- `signed_volume = side * sz`
- `signed_notional = side * px * sz`
- `trade_imbalance = sum(side * sz)`
- `vwap = sum(px * sz) / sum(sz)`
- `avg_trade_size = sum(sz) / len(sz)`
- `large_trade_ratio`

换句话说，`px/sz/side` 已经覆盖了大部分 trade 因子的计算核心。

---

## 3.3 Books

books 不再对 factorengine 返回 `BookEvent` 对象。

### 建议正式结构

```text
dict[str, ndarray]
shape=(N, 20)
columns=[
    bid_px1, bid_px2, bid_px3, bid_px4, bid_px5,
    bid_sz1, bid_sz2, bid_sz3, bid_sz4, bid_sz5,
    ask_px1, ask_px2, ask_px3, ask_px4, ask_px5,
    ask_sz1, ask_sz2, ask_sz3, ask_sz4, ask_sz5,
]
```

这里：

- `N` = 最近 N 次 `books5` 更新
- 第二维固定为 20 列

### 为什么不单独暴露 best bid / best ask

因为：

- `best_bid_px` 本质上就是 `bid_px1`
- `best_ask_px` 本质上就是 `ask_px1`

单独再造字段没有必要。

### 为什么不返回嵌套对象

因为 factorengine 最常见的操作是：

- 直接取 L1
- 直接对 L1-L5 求和
- 算 spread / mid / imbalance / microprice

用扁平数组最快、最直接。

### 例子

```python
book_snapshot["BTC-USDT-SWAP"].shape == (N, 20)
```

```python
bid_px = arr[:, 0:5]
bid_sz = arr[:, 5:10]
ask_px = arr[:, 10:15]
ask_sz = arr[:, 15:20]
```

### 典型 book 因子

有了这 20 列，可以直接算：

- `spread = ask_px1 - bid_px1`
- `mid = (ask_px1 + bid_px1) / 2`
- `l1_imbalance = (bid_sz1 - ask_sz1) / (bid_sz1 + ask_sz1)`
- `l5_imbalance = (sum(bid_sz) - sum(ask_sz)) / (sum(bid_sz) + sum(ask_sz))`
- `microprice`
- `depth_concentration`

---

## 4. collector 层还保留什么

虽然 factorengine 接口不暴露 event class，但 collector 侧仍可以保留以下信息：

### trades collector 可保留

- `trade_id`
- 原始 `channel`
- 原始消息时间
- `count`

### books collector 可保留

- `seqId`
- `checksum`
- 原始 `channel`
- 原始消息时间

### 这些信息的用途

- debug
- 校验
- 落盘
- 线上问题排查

### 这些信息不应直接进入 factorengine 的原因

- 不是主计算列
- 会增加对象和字段拆装成本
- 会拖慢热路径

---

## 5. 新的接口建议

## 5.1 Engine 对外 API

建议最终稳定为：

```python
engine.get_data(symbols)        -> dict[str, ndarray(N, 6)]
engine.get_trade_data(symbols)  -> dict[str, ndarray(N, 3)]
engine.get_book_data(symbols)   -> dict[str, ndarray(N, 20)]
```

### 当前列顺序规范

#### Bars

```text
[ts, open, high, low, close, vol]
```

#### Trades

```text
[px, sz, side]
```

#### Books

```text
[
    bid_px1, bid_px2, bid_px3, bid_px4, bid_px5,
    bid_sz1, bid_sz2, bid_sz3, bid_sz4, bid_sz5,
    ask_px1, ask_px2, ask_px3, ask_px4, ask_px5,
    ask_sz1, ask_sz2, ask_sz3, ask_sz4, ask_sz5,
]
```

---

## 6. 对 cache 的含义也要同步重定义

当前旧思路里：

- `TradeCache` = deque of `TradeEvent`
- `BookCache` = latest + deque of `BookEvent`

新的方向应该改成：

### TradeCache

```text
dict[symbol, ring buffer of rows]
row = [px, sz, side]
```

### BookCache

```text
dict[symbol, ring buffer of rows]
row = 20 columns of books5
```

### 注意

这意味着：

- cache 的正式职责是“为 factorengine 输出数组窗口”
- 不再是“保留事件对象”

---

## 7. 为什么这比 event class 更适合因子引擎

### 7.1 少一层对象拆解

旧路径：

```text
collector
-> TradeEvent/BookEvent
-> cache
-> factorengine 再取字段
```

新路径：

```text
collector
-> row array
-> cache
-> factorengine 直接切片计算
```

### 7.2 更适合 numpy / C++

无论后面是：

- Python 向量化
- C++ runtime
- pybind11 bridge

数组都比对象结构更合适。

### 7.3 更符合真实因子需求

factor 计算几乎从来不是为了拿 `trade_id`、`seqId`、`checksum`。

真正要算的通常是：

- `px`
- `sz`
- `side`
- bid/ask price
- bid/ask size

所以主数据结构就应该围绕这些列设计。

---

## 8. 这个方案的代价

也有代价，需要明确。

### 8.1 可读性会下降一些

对象字段名比数组列索引更直观。

解决办法：

- 固定列顺序
- 在代码里定义 schema 常量
- 文档固定列含义

### 8.2 debug 需要额外通道

因为主 cache 不再保留 `trade_id` / `seqId` 等信息。

解决办法：

- collector 单独保留 debug metadata
- 必要时写 debug log / NDJSON

### 8.3 若未来恢复时间窗，可能要补内部时间轴

尤其是 trades/books 如果最终仍想做严格的时间窗因子。

但这不妨碍当前先把 factorengine 主接口收缩成数组。

---

## 9. 实施建议

如果按这个方向推进，建议开发顺序是：

1. 去掉 `events.py` 中的 dataclass 依赖
2. 把 `TradeCache` 改成 `dict[str, ndarray/list-of-rows]`
3. 把 `BookCache` 改成 `dict[str, ndarray/list-of-rows]`
4. 修改 `trade_collector.py`
   - collector 内部可保留 metadata
   - 但对 cache 只写 `[px, sz, side]`
5. 修改 `book_collector.py`
   - 对 cache 只写 20 列 books5
6. 修改 `Engine.get_trade_data()`
7. 修改 `Engine.get_book_data()`
8. 更新 `test_dataflow_live.py`
9. 更新单测和 schema 文档

---

## 10. 最终结论

这次重构里最重要的一条新决策是：

**dataflow 主热路径不要再以 event class 为中心，而要以 factorengine 友好的数组结构为中心。**

对 factorengine 而言，推荐最终只暴露：

- bars: `ndarray(N, 6)`
- trades: `ndarray(N, 3)`
- books: `ndarray(N, 20)`

collector 如需保留 `trade_id`、`seqId`、`checksum` 等信息，可以在 collector/debug 层单独维护，但不要进入主计算接口。

这条边界一旦定下来，后续不管是 Python 因子计算还是 C++ runtime，都会简单很多。
