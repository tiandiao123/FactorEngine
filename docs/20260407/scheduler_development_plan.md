# Scheduler 原型开发文档（2026-04-07）

## 1. 文档目的

这份文档不再讨论 scheduler 是什么，而是直接回答：

**如果现在要在代码层面开始做 scheduler 原型，应该怎么设计。**

目标是：

- 基于当前已经可用的数组化 dataflow
- 先做一个最小 Python scheduler 原型
- 为未来 C++ runtime 铺路

这份文档关注的是：

- 模块拆分
- 类职责
- 输入输出
- 调度流程
- 第一版实现顺序

配套文档：

- [scheduler_and_cache_slicing.md](/home/ubuntu/workspace/FactorEngine/docs/20260407/scheduler_and_cache_slicing.md)
- [array_dataflow_refactor.md](/home/ubuntu/workspace/FactorEngine/docs/20260406/array_dataflow_refactor.md)

---

## 2. 当前代码现状

当前系统已经具备：

### 2.1 dataflow 三路采集

当前 `Engine` 已经可以提供：

- `get_data()` -> bars
- `get_trade_data()` -> trades
- `get_book_data()` -> books

也就是：

```python
bar_snapshot   = engine.get_data(symbols)        # dict[str, ndarray(N, 6)]
trade_snapshot = engine.get_trade_data(symbols)  # dict[str, ndarray(N, 3)]
book_snapshot  = engine.get_book_data(symbols)   # dict[str, ndarray(N, 20)]
```

### 2.2 还没有独立 scheduler

当前因子计算仍然只能写成：

```python
while True:
    time.sleep(...)
    snapshot = ...
    compute(...)
```

也就是说：

- dataflow 已经成形
- scheduler 还不存在
- factorengine 还没有从“脚本式调用”升级成正式运行时

---

## 3. 第一版 Scheduler 原型的目标

第一版不要追求“大而全”，只解决最核心的问题：

1. 定时发起 factor evaluation
2. 在每个 tick 上从 cache 做 slicing
3. 把 slicing 结果交给一个统一的 factor execution 函数
4. 产出一份最小 factor snapshot

### 3.1 第一版不做什么

第一版不要做：

- 多频率 scheduler
- DAG 依赖图
- 动态因子注册系统
- C++ bridge
- 高级 worker pool 优化
- 跨进程或分布式调度

这些都是第二阶段的事。

---

## 4. 建议的新模块

建议在 `factorengine/` 下增加以下文件：

```text
factorengine/
  engine.py
  scheduler.py
  factor_runtime.py
  factor_snapshot.py
  factor_spec.py
```

下面是每个文件的职责。

### 4.1 `scheduler.py`

职责：

- 维护 evaluation interval
- 维护 tick 循环
- 触发每一轮 factor evaluation

这是“敲钟的人”。

### 4.2 `factor_runtime.py`

职责：

- 接收当前 tick 的时间点
- 从 dataflow cache 中切出本轮需要的数据
- 调用具体因子计算逻辑
- 输出 factor snapshot

这是“干活的人”。

### 4.3 `factor_snapshot.py`

职责：

- 定义 factor snapshot 的最小结构
- 统一表达“一轮 evaluation 的结果”

### 4.4 `factor_spec.py`

职责：

- 定义一个因子需要哪些输入
- 定义窗口大小
- 定义取哪些列

第一版不用做复杂 DSL，只要能描述：

- 用 bars / trades / books 哪一路
- 切多少窗口
- 计算函数是谁

---

## 5. 建议的最小类设计

## 5.1 `Scheduler`

第一版建议长成：

```python
class Scheduler:
    def __init__(self, interval_seconds: int, on_tick):
        ...

    def start(self):
        ...

    def stop(self):
        ...
```

### 字段

- `interval_seconds`
  每隔多久发起一次 evaluation
- `on_tick`
  每次 tick 被调用的回调函数
- `_running`
  调度循环状态
- `_tick_id`
  当前 tick 序号

### 行为

每轮循环：

1. `sleep(interval_seconds)`
2. `tick_id += 1`
3. `ts_eval = now`
4. 调用 `on_tick(tick_id, ts_eval)`

注意：

第一版允许直接同步调用，不需要先引入复杂线程池。

---

## 5.2 `FactorRuntime`

第一版建议长成：

```python
class FactorRuntime:
    def __init__(self, engine: Engine, symbols: list[str], factor_specs: list[FactorSpec]):
        ...

    def evaluate(self, tick_id: int, ts_eval: float) -> FactorSnapshot:
        ...
```

### 输入

- `engine`
  用于读取当前 cache
- `symbols`
  本轮要算哪些 symbol
- `factor_specs`
  本轮要算哪些因子

### 核心逻辑

`evaluate()` 做三件事：

1. 拉当前 cache
2. 对每个因子做 slicing
3. 计算并汇总结果

即：

```text
read snapshots
-> slice per factor
-> compute
-> merge outputs
```

---

## 5.3 `FactorSpec`

第一版建议不要设计太复杂，只要能表达这些字段就够了：

```python
class FactorSpec:
    name: str
    source: str           # "bars" / "trades" / "books"
    window: int
    compute_fn: callable
```

### 示例

```python
FactorSpec(
    name="bar_momentum_20",
    source="bars",
    window=20,
    compute_fn=compute_bar_momentum,
)
```

```python
FactorSpec(
    name="trade_imbalance_500",
    source="trades",
    window=500,
    compute_fn=compute_trade_imbalance,
)
```

```python
FactorSpec(
    name="book_l5_imbalance_50",
    source="books",
    window=50,
    compute_fn=compute_book_l5_imbalance,
)
```

第一版先只支持：

- 一个因子只依赖一类输入

多源因子可以放到第二版再做。

---

## 5.4 `FactorSnapshot`

第一版建议结构尽量简单：

```python
class FactorSnapshot:
    tick_id: int
    ts_eval: float
    values: dict[str, dict[str, float]]
```

其中：

- 第一层 key = symbol
- 第二层 key = factor name

例如：

```python
{
    "BTC-USDT-SWAP": {
        "bar_momentum_20": 0.0012,
        "trade_imbalance_500": -0.034,
    },
    "ETH-USDT-SWAP": {
        ...
    },
}
```

---

## 6. evaluate() 的代码级工作流

第一版 `FactorRuntime.evaluate()` 可以按下面顺序来写。

### Step 1：拉 dataflow cache

```python
bar_snapshot = engine.get_data(symbols)
trade_snapshot = engine.get_trade_data(symbols)
book_snapshot = engine.get_book_data(symbols)
```

### Step 2：按 factor spec 做 slicing

#### bars

```python
window = bar_snapshot[symbol][-spec.window:]
```

#### trades

```python
window = trade_snapshot[symbol][-spec.window:]
```

#### books

```python
window = book_snapshot[symbol][-spec.window:]
```

### Step 3：调用 compute_fn

```python
value = spec.compute_fn(window)
```

### Step 4：写入结果

```python
values[symbol][spec.name] = value
```

### Step 5：返回 snapshot

```python
return FactorSnapshot(...)
```

---

## 7. 第一版建议的示例因子

第一版不要上太多，只要三类各一个就够了。

### 7.1 bars 因子

```python
def compute_bar_momentum(window: np.ndarray) -> float:
    close = window[:, 4]
    if len(close) < 2:
        return 0.0
    return float(close[-1] / close[0] - 1.0)
```

### 7.2 trades 因子

```python
def compute_trade_imbalance(window: np.ndarray) -> float:
    sz = window[:, 1]
    side = window[:, 2]
    denom = np.sum(np.abs(sz))
    if denom == 0:
        return 0.0
    return float(np.sum(sz * side) / denom)
```

### 7.3 books 因子

```python
def compute_book_l1_imbalance(window: np.ndarray) -> float:
    latest = window[-1]
    bid_sz1 = latest[5]
    ask_sz1 = latest[15]
    denom = bid_sz1 + ask_sz1
    if denom == 0:
        return 0.0
    return float((bid_sz1 - ask_sz1) / denom)
```

这三个足够验证整个调度和 slicing 流程。

---

## 8. 第一版推荐的文件内容

## 8.1 `factorengine/scheduler.py`

建议包含：

- `Scheduler`

## 8.2 `factorengine/factor_spec.py`

建议包含：

- `FactorSpec`

## 8.3 `factorengine/factor_snapshot.py`

建议包含：

- `FactorSnapshot`

## 8.4 `factorengine/factor_runtime.py`

建议包含：

- `FactorRuntime`
- 2-3 个示例 `compute_fn`

---

## 9. 与当前 Engine 的关系

第一版不建议立刻把 scheduler 深深塞进 `Engine`。

更稳妥的做法是：

### 当前阶段

`Engine` 继续只负责：

- dataflow 启停
- cache 读取接口

### scheduler 原型阶段

外部测试脚本显式创建：

```python
engine = Engine(...)
runtime = FactorRuntime(engine, symbols, factor_specs)
scheduler = Scheduler(interval_seconds=10, on_tick=runtime.evaluate)
```

这样做的好处：

- 改动范围小
- 易于调试
- 不会太早把 `Engine` 变成大杂烩

等 Python 原型稳定后，再决定是否把 scheduler/runtime 合并进 `Engine`。

---

## 10. 第一版开发顺序

建议按这个顺序实现：

1. `factor_spec.py`
2. `factor_snapshot.py`
3. `factor_runtime.py`
4. `scheduler.py`
5. 新增一个测试脚本，例如：
   - `tests/test_scheduler_live.py`

这个脚本负责：

- 启动 `Engine`
- 启动 `Scheduler`
- 每个 tick 打印 factor snapshot

---

## 11. 第一版验收标准

如果达到下面几个条件，就说明 Python scheduler 原型已经成功：

1. 可以固定频率触发 evaluation tick
2. 可以在每个 tick 上读取 `bars/trades/books`
3. 可以按窗口切片
4. 可以计算至少 3 个示例因子
5. 可以输出一份结构清晰的 factor snapshot

这时就可以进入下一阶段：

- 讨论 worker pool
- 讨论多因子并行
- 讨论 C++ runtime 边界

---

## 12. 最终建议

这一步不要过度设计。

第一版 scheduler 原型只要做到：

- 一个频率
- 一组 symbol
- 少量因子
- 清晰的 slicing
- 清晰的 snapshot 输出

就已经足够。

真正重要的是：

1. 把调度边界定下来
2. 把 cache slicing 方式定下来
3. 把因子输入输出的代码接口定下来

这三件事一旦在 Python 原型里跑顺，后面迁移到 C++ 会容易很多。
