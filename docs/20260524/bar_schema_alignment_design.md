# Bar Schema 对齐机制与重构设计

> 日期：2026-05-24
> 状态：设计说明
> 目标：解释当前 `round_bars` / `BarData` 字段如何对齐，以及后续如何降低字段顺序错位风险

---

## 1. 问题背景

在 `factorengine/engine.py` 的 runtime loop 中，有如下代码：

```python
for sym, bar in round_bars.items():
    ts_ms = int(bar[0])
    close = float(bar[4])
    volume = float(bar[5])
    open_ = float(bar[1])
    high = float(bar[2])
    low = float(bar[3])
```

直观看会有疑问：

1. 为什么 `close` 是 `bar[4]`？
2. 为什么 `open` 是 `bar[1]`？
3. 这些 index 是怎么保证所有 worker 都对得上的？
4. 传给 C++ `BarData` 时为什么又换了顺序？

这份文档解释当前机制，并提出后续重构建议。

---

## 2. 当前 BarPoint 约定

当前 dataflow 层内部使用一个固定顺序的 `np.ndarray(8,)` 表示 bar。

约定如下：

```text
BarPoint ndarray(8,)

index  field
-----  ----------------
0      ts
1      open
2      high
3      low
4      close
5      volume
6      vol_ccy
7      vol_ccy_quote
```

也就是：

```text
[ts, open, high, low, close, volume, vol_ccy, vol_ccy_quote]
```

所以 `Engine._runtime_loop()` 中：

```python
ts_ms = int(bar[0])
close = float(bar[4])
volume = float(bar[5])
open_ = float(bar[1])
high = float(bar[2])
low = float(bar[3])
```

是按照这个固定 schema 取值。

---

## 3. C++ BarData 的顺序不同

`fe_runtime.BarData` 的构造顺序不是 dataflow `BarPoint` 顺序。

当前 C++ `BarData` 语义是：

```text
BarData(
    close,
    volume,
    open,
    high,
    low,
    ret,
    vwap,
    buy_vol,
    sell_vol,
)
```

所以 `Engine._runtime_loop()` 实际在做一次字段转换：

```text
dataflow BarPoint:
  [ts, open, high, low, close, volume, vol_ccy, vol_ccy_quote]

Engine extracts:
  open   = bar[1]
  high   = bar[2]
  low    = bar[3]
  close  = bar[4]
  volume = bar[5]

runtime BarData:
  BarData(close, volume, open, high, low, ret, vwap, buy_vol, sell_vol)
```

这不是同一个数组顺序，而是两个层次之间的显式映射。

---

## 4. 当前哪些地方依赖这个约定

### 4.1 Engine runtime

文件：

```text
factorengine/engine.py
```

职责：

1. 从 `bar_queue` 读取 `round_bars`。
2. 按 `BarPoint` index 取 OHLCV。
3. 计算 `ret`。
4. 合并 trades 字段 `vwap / buy_vol / sell_vol`。
5. 构造 C++ `rt.BarData`。

### 4.2 TradesWorker

文件：

```text
dataflow/okx/trades/worker.py
dataflow/binance/trades/worker.py
```

当前 trades-only bar 构造：

```python
bar = np.array([
    ts_ms,
    snap.get("open", _NAN),
    snap.get("high", _NAN),
    snap.get("low", _NAN),
    snap.get("close", _NAN),
    snap.get("volume", 0.0),
    _NAN,
    _NAN,
], dtype=np.float64)
```

它符合：

```text
[ts, open, high, low, close, volume, vol_ccy, vol_ccy_quote]
```

### 4.3 Kline 链路

文件大致包括：

```text
dataflow/okx/kline/adapter.py
dataflow/binance/kline/adapter.py
dataflow/kline/publisher.py
```

这些模块也应产出同样的 `BarPoint ndarray(8,)`。

### 4.4 BarCache / get_data

`Engine.get_data()` 的 docstring 也写了：

```text
[ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]
```

这说明 debug cache 层同样依赖这个 schema。

### 4.5 Aggregate Audit

新增的 audit logging 也读取同样的 bar index：

```text
bar[0] -> bucket_ts_ms
bar[1] -> open
bar[2] -> high
bar[3] -> low
bar[4] -> close
bar[5] -> volume
```

所以 audit 也应该复用统一 schema，避免再写 magic index。

---

## 5. 当前是如何“保证对齐”的

目前主要靠三件事：

### 5.1 代码约定

所有 worker 都约定输出：

```text
dict[str, np.ndarray]
```

其中每个 ndarray 都是：

```text
[ts, open, high, low, close, volume, vol_ccy, vol_ccy_quote]
```

### 5.2 测试覆盖

当前已有 tests 覆盖：

1. kline adapter alignment。
2. trades worker flush。
3. ClockDriver aggregation。
4. Engine integration。
5. runtime factor input。

这些测试能间接发现明显错位。

### 5.3 文档描述

README / dataflow README / Engine docstring 多处写了 bar column 含义。

但这些都是“约定”，不是强类型保证。

---

## 6. 当前风险

### 6.1 Magic index 分散

现在多处直接写：

```python
bar[4]
bar[5]
bar[1]
```

读代码时不直观。

### 6.2 新 worker 容易写错顺序

如果未来新增 orderbook、funding、其他 exchange worker，开发者可能错误构造：

```text
[ts, close, volume, open, high, low]
```

这类错误不一定立刻报错，而是产生 silent data corruption。

### 6.3 Audit 也会继承错位

如果 worker 的 bar schema 错了，audit 会忠实记录错误字段。
这能帮助发现问题，但不能防止问题。

### 6.4 C++ BarData 顺序不同

dataflow `BarPoint` 和 runtime `BarData` 顺序不同，容易让人误读：

```text
BarPoint: [ts, open, high, low, close, volume, ...]
BarData:  close, volume, open, high, low, ret, ...
```

因此 `Engine` 中的字段重排必须非常清楚。

---

## 7. 推荐重构：统一 Bar Schema 常量

第一步不需要大改结构，只需要去掉 magic index。

新增文件：

```text
dataflow/bar_schema.py
```

内容：

```python
BAR_TS = 0
BAR_OPEN = 1
BAR_HIGH = 2
BAR_LOW = 3
BAR_CLOSE = 4
BAR_VOLUME = 5
BAR_VOL_CCY = 6
BAR_VOL_CCY_QUOTE = 7

BAR_NUM_FIELDS = 8

BAR_COLUMNS = (
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vol_ccy",
    "vol_ccy_quote",
)
```

然后所有读取改为：

```python
from dataflow.bar_schema import (
    BAR_TS,
    BAR_OPEN,
    BAR_HIGH,
    BAR_LOW,
    BAR_CLOSE,
    BAR_VOLUME,
)

ts_ms = int(bar[BAR_TS])
close = float(bar[BAR_CLOSE])
volume = float(bar[BAR_VOLUME])
open_ = float(bar[BAR_OPEN])
high = float(bar[BAR_HIGH])
low = float(bar[BAR_LOW])
```

所有构造也可以改为：

```python
bar = np.empty(BAR_NUM_FIELDS, dtype=np.float64)
bar[BAR_TS] = ts_ms
bar[BAR_OPEN] = open_
bar[BAR_HIGH] = high
bar[BAR_LOW] = low
bar[BAR_CLOSE] = close
bar[BAR_VOLUME] = volume
bar[BAR_VOL_CCY] = np.nan
bar[BAR_VOL_CCY_QUOTE] = np.nan
```

这样即使还是 ndarray，也能降低字段错位风险。

---

## 8. 可选增强：BarPoint helper

可以增加 helper 函数：

```python
def make_bar_point(
    ts_ms: float,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    vol_ccy: float = np.nan,
    vol_ccy_quote: float = np.nan,
) -> np.ndarray:
    ...
```

TradesWorker 就可以写：

```python
bar = make_bar_point(
    ts_ms=ts_ms,
    open_=snap.get("open", _NAN),
    high=snap.get("high", _NAN),
    low=snap.get("low", _NAN),
    close=snap.get("close", _NAN),
    volume=snap.get("volume", 0.0),
)
```

Engine 侧可以增加：

```python
def unpack_bar_point(bar: np.ndarray) -> dict:
    ...
```

但第一步可以只做常量，不急着引入 helper。

---

## 9. 为什么不直接改成 dataclass

可以想象：

```python
@dataclass
class BarPoint:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    vol_ccy: float
    vol_ccy_quote: float
```

优点：

1. 可读性强。
2. 字段名清楚。
3. 不容易错位。

缺点：

1. 当前代码大量使用 numpy arrays。
2. `BarCache` / kline publisher / tests 都按 ndarray 工作。
3. 改动面更大。
4. 性能和内存开销需要评估。

所以短期建议：

```text
保留 ndarray
引入统一 index 常量
集中构造 helper
```

后续如果接口稳定，再考虑 dataclass / structured array。

---

## 10. 推荐实施顺序

### Phase 1：新增 `dataflow/bar_schema.py`

新增常量：

```text
BAR_TS
BAR_OPEN
BAR_HIGH
BAR_LOW
BAR_CLOSE
BAR_VOLUME
BAR_VOL_CCY
BAR_VOL_CCY_QUOTE
BAR_NUM_FIELDS
BAR_COLUMNS
```

### Phase 2：替换读取侧 magic index

优先改：

```text
factorengine/engine.py
dataflow/audit/writer.py
```

因为这两个地方是消费 bar schema 的核心。

### Phase 3：替换构造侧 magic index

改：

```text
dataflow/okx/trades/worker.py
dataflow/binance/trades/worker.py
dataflow/okx/kline/adapter.py
dataflow/binance/kline/adapter.py
dataflow/kline/publisher.py
```

### Phase 4：补测试

新增：

```text
tests/dataflow/test_bar_schema.py
```

测试：

1. `BAR_NUM_FIELDS == len(BAR_COLUMNS)`。
2. `BAR_CLOSE == 4`。
3. `make_bar_point()` 构造出的数组字段顺序正确。
4. `Engine` / audit 使用同一套常量。

---

## 11. 一句话结论

当前字段能对上，是因为所有 dataflow worker 约定输出统一的 `BarPoint ndarray(8,)`：

```text
[ts, open, high, low, close, volume, vol_ccy, vol_ccy_quote]
```

`Engine` 再把它映射成 C++ `BarData(close, volume, open, high, low, ret, ...)`。

这个设计目前可用，但依赖 magic index。后续建议新增 `dataflow/bar_schema.py`，用统一常量和 helper 替代散落的 `bar[4] / bar[5]`，降低新 worker 或 audit 代码写错字段顺序的风险。
