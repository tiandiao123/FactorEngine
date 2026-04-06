# Scheduler 与 Cache Slicing 设计说明（2026-04-07）

## 1. 文档目的

这份文档只回答两个问题：

1. `scheduler` 到底负责什么
2. “cache slicing” 到底是什么意思

这两个问题看起来简单，但它们实际上决定了后面：

- factorengine 的执行模型
- Python 原型怎么写
- C++ runtime 最后怎么落地

如果这两件事没想清楚，后面因子计算层很容易越写越乱。

---

## 2. 先说结论

### 2.1 Scheduler 是“定时发起因子计算”的组件

它不负责采集数据，也不负责具体算因子。

它负责的是：

- 决定什么时候开始一轮 factor evaluation
- 决定一轮 evaluation 的时间边界
- 触发 factorengine 去读取所需数据并完成本轮计算

一句话：

**scheduler = 敲钟的人**

### 2.2 Cache slicing 是“从大块缓存里切出本轮因子需要的那一段数据”

dataflow 会持续把 `bars` / `trades` / `books` 写进 cache。

但因子计算时，不是每次都要把整块 cache 全拿来算。

而是只取：

- 最近若干根 bar
- 最近若干笔 trade
- 最近若干次 books5 更新

这个“取某一段”的动作，就是 cache slicing。

一句话：

**cache slicing = 因子计算时，从缓存里按窗口切出需要的那段数据**

---

## 3. 当前系统里谁负责什么

### 3.1 Dataflow

当前 dataflow 已经负责：

- 持续连接 OKX
- 持续采集 `bars`
- 持续采集 `trades`
- 持续采集 `books`
- 持续写入各自的 cache

也就是说，dataflow 的职责是：

```text
实时生产数据
```

### 3.2 FactorEngine

factorengine 的职责不是采集，而是：

```text
在某个时刻，把当前所需数据拿出来，算出因子
```

### 3.3 Scheduler

那么剩下的关键问题就是：

```text
什么时候开始这一轮因子计算？
```

这个问题不应该让测试脚本随便 `while True: sleep(...)` 来决定。

它应该由独立的 scheduler 控制。

---

## 4. Scheduler 到底干什么

假设系统设定：

```text
evaluation_interval = 10s
```

那么 scheduler 的典型工作流是：

1. 等待 10 秒
2. 触发一次 evaluation tick
3. 告诉 factorengine：
   - 现在开始算本轮因子
4. factorengine 读取当前快照或窗口切片
5. 计算完所有目标因子
6. 产出一个 factor snapshot

然后再进入下一轮 10 秒。

### 4.1 它不做什么

scheduler 不做：

- WebSocket 接入
- cache 写入
- 具体因子公式计算
- 模型推理
- 下单

它只是调度时钟和任务边界。

### 4.2 它要输出什么

scheduler 至少要定义：

- 当前 tick 的编号
- 当前 tick 的时间点
- 本轮 evaluation 的截止时刻

例如：

```text
tick_id = 42
ts_eval = 2026-04-07 10:00:10
```

这样 factorengine 才知道这轮因子的时间语义是什么。

---

## 5. 为什么不能没有 Scheduler

如果没有 scheduler，系统通常会退化成测试脚本式写法：

```python
while True:
    time.sleep(10)
    bar_snapshot = engine.get_data()
    trade_snapshot = engine.get_trade_data()
    book_snapshot = engine.get_book_data()
    compute_factors(...)
```

这种写法用于 smoke test 是可以的，但不适合作为正式系统设计，因为：

1. 调度逻辑散落在脚本里
2. 不方便统一管理多频率因子
3. 不方便接线程池
4. 不方便和未来 C++ runtime 对齐
5. 不方便定义 evaluation tick 的边界

所以 scheduler 要独立出来。

---

## 6. 什么叫 Cache Slicing

当前 dataflow 会持续往 cache 里写数据：

- bars: `dict[symbol, ndarray(N, 6)]`
- trades: `dict[symbol, ndarray(N, 3)]`
- books: `dict[symbol, ndarray(N, 20)]`

但因子计算时通常不会把整个 `N` 全拿来。

而是按窗口取其中一段。

### 6.1 Bars 的 slicing

例如某个 bar 因子只需要最近 20 根 bar：

```python
arr = bar_snapshot["BTC-USDT-SWAP"]
recent_20 = arr[-20:]
close_20 = arr[-20:, 4]
```

这里：

- `arr[-20:]`
  就是对 bar cache 的切片
- `arr[-20:, 4]`
  就是对 bar cache 的“行 + 列”切片

### 6.2 Trades 的 slicing

例如某个 trade 因子只需要最近 200 笔：

```python
arr = trade_snapshot["BTC-USDT-SWAP"]
recent_200 = arr[-200:]
px = recent_200[:, 0]
sz = recent_200[:, 1]
side = recent_200[:, 2]
```

这里：

- `recent_200`
  就是对 trade cache 的切片

### 6.3 Books 的 slicing

例如某个盘口因子只需要最近 50 次 `books5` 更新：

```python
arr = book_snapshot["BTC-USDT-SWAP"]
recent_50 = arr[-50:]

bid_px = recent_50[:, 0:5]
bid_sz = recent_50[:, 5:10]
ask_px = recent_50[:, 10:15]
ask_sz = recent_50[:, 15:20]
```

这里：

- `recent_50`
  就是对 book cache 的切片

---

## 7. 为什么要提前想清楚 Slicing

因为 scheduler 每次 tick 来了以后，factorengine 并不是无脑把全部 cache 扫一遍。

而是：

- 因子 A 只要最近 20 根 bar
- 因子 B 只要最近 500 笔 trade
- 因子 C 只要最近 50 次 books5

这意味着系统要回答：

1. 每个因子需要哪类数据
2. 每个因子需要多少窗口
3. 每个因子需要哪些列

这就是 cache slicing 设计的本质。

如果不先设计 slicing，后面很容易出现：

- 每轮都复制整个 cache
- 每个因子都扫全量数据
- 重复切同样的窗口
- 多个因子重复算同一中间量

这些都会拖慢系统。

---

## 8. Scheduler 和 Cache Slicing 的关系

二者是连在一起的。

### Scheduler 决定

- 什么时候触发
- 本轮 evaluation 的时间边界是什么

### Cache slicing 决定

- 本轮触发后从 cache 里取哪一段
- 每个因子到底拿什么数据来算

所以它们配合起来就是：

```text
evaluation tick
-> choose window
-> slice cache
-> compute factors
```

---

## 9. 一个完整例子

假设系统每 10 秒计算一次因子。

### 已有 cache

```text
bars["BTC-USDT-SWAP"]   = ndarray(1000, 6)
trades["BTC-USDT-SWAP"] = ndarray(10000, 3)
books["BTC-USDT-SWAP"]  = ndarray(1000, 20)
```

### 因子定义

#### 因子 A

- 名称：`bar_momentum`
- 输入：bars
- 需要：最近 20 根 bar 的 close

切片：

```python
close_20 = bars[-20:, 4]
```

#### 因子 B

- 名称：`trade_imbalance`
- 输入：trades
- 需要：最近 500 笔 trade 的 `sz` 和 `side`

切片：

```python
window = trades[-500:]
sz = window[:, 1]
side = window[:, 2]
```

#### 因子 C

- 名称：`book_l5_imbalance`
- 输入：books
- 需要：最近 50 次 `books5` 的前 5 档 size

切片：

```python
window = books[-50:]
bid_sz = window[:, 5:10]
ask_sz = window[:, 15:20]
```

### Scheduler 在 tick 上做什么

```text
tick @ 10:00:10
-> 取 bars 最近 20 根
-> 取 trades 最近 500 笔
-> 取 books 最近 50 次
-> 算完 A/B/C
-> 输出一份 factor snapshot
```

---

## 10. 这部分最终是不是要进 C++

结论：

- **最终实现大概率要进 C++ runtime**
- **但第一版原型建议先在 Python 做**

原因：

1. 现在 dataflow 刚稳定
2. 因子输入输出接口还在收敛
3. 不适合马上把不确定接口写死在 C++

更合理的顺序是：

1. 先在 Python 里实现最小 scheduler 原型
2. 先验证 tick、窗口、切片方式
3. 再把成熟接口迁移到 C++

这样后面返工更少。

---

## 11. 建议的下一步

如果按当前进度继续推进，最合理的下一步是：

1. 先写一个最小 `scheduler.py`
2. 只支持单一频率，例如 `10s`
3. 在 tick 上对 `bars/trades/books` 做简单切片
4. 先做 2-3 个示例因子
5. 输出一个最小 factor snapshot

这一步的目标不是性能极致，而是把：

- 调度边界
- cache slicing 方式
- 因子输入输出格式

先定下来。

等这一步稳定后，再把它搬进 C++，会更稳。
