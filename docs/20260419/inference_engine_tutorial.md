# InferenceEngine 开发教程

> 日期: 2026-04-19 (更新: 2026-04-18)  
> 状态: 多线程已实现, Engine 集成已完成

---

## 目录

1. [概述与核心概念](#1-概述与核心概念)
2. [安装与环境准备](#2-安装与环境准备)
3. [FactorGraph: 因子表达式 → DAG](#3-factorgraph-因子表达式--dag)
4. [翻译因子表达式的完整指南](#4-翻译因子表达式的完整指南)
5. [SymbolRunner: 单标的多因子推理](#5-symbolrunner-单标的多因子推理)
6. [InferenceEngine: 多标的多因子推理](#6-inferenceengine-多标的多因子推理)
7. [Engine 集成: 数据流 + 因子推理一体化](#7-engine-集成-数据流--因子推理一体化)
8. [因子注册与平台管理](#8-因子注册与平台管理)
9. [添加新因子的完整流程](#9-添加新因子的完整流程)
10. [可视化与调试](#10-可视化与调试)
11. [API 参考](#11-api-参考)
12. [常见问题](#12-常见问题)

---

## 1. 概述与核心概念

### 整体架构

```
因子表达式 (数学公式)
    │
    │  人工翻译为 Python 建图代码
    v
FactorGraph (C++ DAG)        ← 编译期: 建图 + compile()
    │
    │  绑定到 SymbolRunner
    v
SymbolRunner (单标的)         ← 运行时: push_bar() 流式推理
    │
    │  由 InferenceEngine 管理 (多线程)
    v
InferenceEngine (多标的)      ← C++ 线程池并行推理
    │
    │  由 Engine 自动驱动
    v
Engine (Python 顶层入口)      ← 数据采集 + 因子推理一体化
```

### 四层对象模型

| 层级 | 类 | 语言 | 职责 | 数量关系 |
|------|-----|------|------|---------|
| L0 | `FactorGraph` | C++ | 一个因子表达式的 DAG, 编译后可流式 push | 每个(标的, 因子)一个实例 |
| L1 | `SymbolRunner` | C++ | 聚合一个标的下的所有 FactorGraph | 每个标的一个 |
| L2 | `InferenceEngine` | C++ | 管理所有标的, 内置线程池并行推理 | 全局唯一 |
| L3 | `Engine` | Python | 数据流管理 + InferenceEngine 集成 | 全局唯一 |

### 核心设计原则

- **Python 负责建图, C++ 负责推理**: 建图是一次性开销, 推理是每 bar 热路径
- **push-based 流式计算**: 每个 FactorGraph 内部所有算子维护增量状态, `push_bar()` O(1) 均摊
- **warmup 自动计算**: `compile()` 遍历 DAG 累加 window 得到 warmup_bars, warmup 阶段输出 0.0
- **多线程并行**: `InferenceEngine` 内置 C++ 线程池, `push_bars()` 自动将各标的分发到不同线程
- **GIL 释放**: `push_bars()` 调用时自动释放 Python GIL, C++ 线程真正并行

---

## 2. 安装与环境准备

```bash
# 一键安装 (自动编译 C++ 扩展)
cd FactorEngine
pip install -e ".[dev]"
```

验证:

```python
import fe_runtime as rt
print(rt.Op.MA)          # Op.MA
g = rt.FactorGraph()     # 空图
e = rt.InferenceEngine(num_threads=4)
print(f"线程数: {e.num_threads()}")  # 4
print("OK")
```

---

## 3. FactorGraph: 因子表达式 → DAG

### 生命周期

```
FactorGraph()  →  add_*()  →  compile()  →  push_bar() × N  →  output()
   创建             建图          编译           流式推理          取值
```

### 建图 API

每个 `add_*()` 方法返回一个 `int` 节点 ID, 作为后续节点的输入引用:

```python
import fe_runtime as rt
Op = rt.Op

g = rt.FactorGraph()

# 1. 输入节点: 关联 OHLCV 中的一个字段
c = g.add_input("close")    # 支持: "close", "volume", "open", "high", "low", "ret"

# 2. 一元算子: op(src)
neg_c = g.add_unary(Op.NEG, c)

# 3. 二元算子: op(src_a, src_b)
diff = g.add_binary(Op.SUB, c, neg_c)

# 4. 滚动算子: op(src, window)
ma = g.add_rolling(Op.MA, c, 120)

# 5. 双变量算子: op(src_a, src_b, window)
corr = g.add_bivariate(Op.CORR, c, ma, 60)

# 6. 标量算子: op(src, scalar_value)
centered = g.add_scalar_op(Op.SUB_SCALAR, c, 100.0)

# 7. 自相关 (特殊): autocorr(src, window, lag)
ac = g.add_autocorr(c, 120, 5)

# 最后一个 add_*() 的返回值自动成为输出节点
g.compile()
```

### 编译做了什么

`compile()` 执行三件事:
1. 为每个节点分配内部 kernel (ring buffer / 单调队列 / Treap 等)
2. 分配 `values_[]` 数组 (大小 = 节点数)
3. 计算 `warmup_bars`: DAG 从输入到输出的最长 window 累加

### push_bar() 的执行流程

```
push_bar(close, volume, open, high, low, ret)
    │
    │  按拓扑序 (节点 0 → N-1) 依次执行:
    │
    ├── 节点 0 (INPUT_CLOSE): values_[0] = close
    ├── 节点 1 (MA, w=120):   kernel.push(values_[0]); values_[1] = kernel.output()
    ├── 节点 2 (SUB):         values_[2] = values_[0] - values_[1]
    ├── 节点 3 (TS_STD, w=60):kernel.push(values_[0]); values_[3] = kernel.output()
    └── 节点 4 (DIV):         values_[4] = values_[2] / values_[3]
                                    ↑
                                output = values_[4]
```

---

## 4. 翻译因子表达式的完整指南

### 翻译规则表

| 因子表达式 | 建图代码 |
|-----------|---------|
| `close` | `c = g.add_input("close")` |
| `volume` | `v = g.add_input("volume")` |
| `open` | `o = g.add_input("open")` |
| `high` | `h = g.add_input("high")` |
| `low` | `l = g.add_input("low")` |
| `ret` | `r = g.add_input("ret")` |
| `Neg(x)` | `g.add_unary(Op.NEG, x)` |
| `Abs(x)` | `g.add_unary(Op.ABS, x)` |
| `Log(x)` | `g.add_unary(Op.LOG, x)` |
| `Sqr(x)` | `g.add_unary(Op.SQR, x)` |
| `SLog1p(x)` | `g.add_unary(Op.SLOG1P, x)` |
| `Sign(x)` | `g.add_unary(Op.SIGN, x)` |
| `Tanh(x)` | `g.add_unary(Op.TANH, x)` |
| `Inv(x)` | `g.add_unary(Op.INV, x)` |
| `Add(x, y)` | `g.add_binary(Op.ADD, x, y)` |
| `Sub(x, y)` | `g.add_binary(Op.SUB, x, y)` |
| `Mul(x, y)` | `g.add_binary(Op.MUL, x, y)` |
| `Div(x, y)` | `g.add_binary(Op.DIV, x, y)` |
| `Sub(x, 0.5)` | `g.add_scalar_op(Op.SUB_SCALAR, x, 0.5)` |
| `Add(x, 1.0)` | `g.add_scalar_op(Op.ADD_SCALAR, x, 1.0)` |
| `Mul(x, 2.0)` | `g.add_scalar_op(Op.MUL_SCALAR, x, 2.0)` |
| `Div(x, 100)` | `g.add_scalar_op(Op.DIV_SCALAR, x, 100.0)` |
| `Sub(1.0, x)` | `g.add_scalar_op(Op.SCALAR_SUB, x, 1.0)` |
| `Div(1.0, x)` | `g.add_scalar_op(Op.SCALAR_DIV, x, 1.0)` |
| `Ma(x, 120)` | `g.add_rolling(Op.MA, x, 120)` |
| `TsSum(x, 60)` | `g.add_rolling(Op.TS_SUM, x, 60)` |
| `TsStd(x, 60)` | `g.add_rolling(Op.TS_STD, x, 60)` |
| `TsVari(x, 60)` | `g.add_rolling(Op.TS_VARI, x, 60)` |
| `Ema(x, 20)` | `g.add_rolling(Op.EMA, x, 20)` |
| `TsMin(x, 120)` | `g.add_rolling(Op.TS_MIN, x, 120)` |
| `TsMax(x, 120)` | `g.add_rolling(Op.TS_MAX, x, 120)` |
| `TsRank(x, 180)` | `g.add_rolling(Op.TS_RANK, x, 180)` |
| `TsRank(x, 4320)` | `g.add_rolling(Op.TREAP_TS_RANK, x, 4320)` (大窗口优化) |
| `TsZscore(x, 240)` | `g.add_rolling(Op.TS_ZSCORE, x, 240)` |
| `Delay(x, 5)` | `g.add_rolling(Op.DELAY, x, 5)` |
| `TsDiff(x, 1)` | `g.add_rolling(Op.TS_DIFF, x, 1)` |
| `TsPct(x, 1)` | `g.add_rolling(Op.TS_PCT, x, 1)` |
| `PctChange(x)` | `g.add_rolling(Op.PCT_CHANGE, x, 1)` |
| `Corr(x, y, 120)` | `g.add_bivariate(Op.CORR, x, y, 120)` |
| `Autocorr(x, 120, 5)` | `g.add_autocorr(x, 120, 5)` |
| `TsMinMaxDiff(x, 60)` | `g.add_rolling(Op.TS_MINMAX_DIFF, x, 60)` |
| `TsSkew(x, 120)` | `g.add_rolling(Op.TS_SKEW, x, 120)` |
| `TsMed(x, 60)` | `g.add_rolling(Op.TS_MED, x, 60)` |
| `TsMad(x, 60)` | `g.add_rolling(Op.TS_MAD, x, 60)` |
| `TsWMA(x, 60)` | `g.add_rolling(Op.TS_WMA, x, 60)` |
| `TsMaxDiff(x, 120)` | `g.add_rolling(Op.TS_MAX_DIFF, x, 120)` |
| `TsMinDiff(x, 120)` | `g.add_rolling(Op.TS_MIN_DIFF, x, 120)` |

> **TsRank 选择指南**: `TS_RANK` 是 O(window) brute-force, `TREAP_TS_RANK` 是 O(log window) Treap 实现。window < 1000 时 brute-force 更快（缓存友好）, window >= 1000 时 Treap 开始胜出, window=4320 时 Treap 快 ~27%。默认使用 `TS_RANK` 即可。

### 翻译实战: 5 个真实因子

#### 因子 0001: 均线偏离度

```
表达式: Div(Sub(close, Ma(close, 120)), TsStd(close, 60))
含义:   价格偏离120日均线的程度, 用60日波动率归一化
```

```python
g = rt.FactorGraph()
c = g.add_input("close")              # [0] close
ma120 = g.add_rolling(Op.MA, c, 120)  # [1] Ma(close, 120)
dev = g.add_binary(Op.SUB, c, ma120)  # [2] close - Ma
vol = g.add_rolling(Op.TS_STD, c, 60) # [3] TsStd(close, 60)
g.add_binary(Op.DIV, dev, vol)        # [4] (close - Ma) / Std  ← 输出
g.compile()
# warmup = max(120, 60) = 120 bars
```

#### 因子 0050: 量价 Rank 相关性

```
表达式: Neg(Corr(TsRank(pct_change(close), 30), TsRank(volume, 30), 120))
含义:   收益率排名与成交量排名的120日相关系数, 取反
```

```python
g = rt.FactorGraph()
c = g.add_input("close")
v = g.add_input("volume")
pct = g.add_rolling(Op.PCT_CHANGE, c, 1)   # 收益率
rr = g.add_rolling(Op.TS_RANK, pct, 30)    # 收益率排名
vr = g.add_rolling(Op.TS_RANK, v, 30)      # 成交量排名
corr = g.add_bivariate(Op.CORR, rr, vr, 120)  # 相关系数
g.add_unary(Op.NEG, corr)                  # 取反
g.compile()
# warmup = 1 (pct) + 30 (rank) + 120 (corr) = 151 bars
```

#### 因子 0020: 日内区间效率

```
表达式: Neg(TsZscore(Mul(Sub(range_pos, 0.5), vol_ratio), 240))
  其中: range_pos = Div(Sub(close, TsMin(low,120)), Sub(TsMax(high,120), TsMin(low,120)))
        vol_ratio = Div(Ma(volume,15), Ma(volume,120))
```

```python
g = rt.FactorGraph()
c = g.add_input("close")
h = g.add_input("high")
lo = g.add_input("low")
v = g.add_input("volume")

# 区间位置: (close - rolling_low) / (rolling_high - rolling_low)
rh = g.add_rolling(Op.TS_MAX, h, 120)
rl = g.add_rolling(Op.TS_MIN, lo, 120)
rng = g.add_binary(Op.SUB, rh, rl)
pos = g.add_binary(Op.DIV, g.add_binary(Op.SUB, c, rl), rng)
centered = g.add_scalar_op(Op.SUB_SCALAR, pos, 0.5)  # 居中到 [-0.5, 0.5]

# 成交量比率
vs = g.add_rolling(Op.MA, v, 15)
vl = g.add_rolling(Op.MA, v, 120)
vr = g.add_binary(Op.DIV, vs, vl)

# 交互 + 标准化
raw = g.add_binary(Op.MUL, centered, vr)
zs = g.add_rolling(Op.TS_ZSCORE, raw, 240)
g.add_unary(Op.NEG, zs)
g.compile()
# warmup = 120 + 240 = 360 bars
```

### 翻译技巧

1. **自底向上**: 先写最内层的输入, 再逐层往外包裹
2. **每个中间结果保存为变量**: `add_*()` 返回的 `int` 就是节点 ID, 后续直接引用
3. **最后一个 `add_*()` 自动成为输出**: 不需要显式标记
4. **窗口大小直接对应**: 因子表达式中的 `Ma(x, 120)` 中的 `120` 就是 `window` 参数
5. **标量运算用 scalar_op**: `Sub(x, 0.5)` → `add_scalar_op(Op.SUB_SCALAR, x, 0.5)`

---

## 5. SymbolRunner: 单标的多因子推理

`SymbolRunner` 绑定一个标的, 管理该标的下的所有 `FactorGraph`:

```python
import fe_runtime as rt
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_group("okx_perp")

runner = rt.SymbolRunner("BTC-USDT")

# 添加因子
for fid, graph in reg.build_group("okx_perp").items():
    runner.add_factor(fid, graph)

print(f"标的: {runner.symbol()}")          # BTC-USDT
print(f"因子数: {runner.num_factors()}")    # 5

# 推理: push 一根 bar
runner.push_bar(close=100.0, volume=5000.0, open=99.5, high=101.0, low=99.0, ret=0.005)

# 获取所有因子值
outputs = runner.outputs()                # list[float], 长度 = 因子数
fids = runner.factor_ids()                # ["0001", "0010", "0020", "0050", "0100"]

# 按 ID 查询单个因子
val = runner.output_by_id("0001")

# 按索引查询
val = runner.output(0)                    # 第一个因子的值

# 重置 (清空所有状态, 重新 warmup)
runner.reset()
```

---

## 6. InferenceEngine: 多标的多因子推理

`InferenceEngine` 是 C++ 层的顶层入口, 内置线程池, 管理多个 `SymbolRunner`:

### 基础用法

```python
import fe_runtime as rt
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_group("okx_perp")

# 创建引擎 (指定线程数)
engine = rt.InferenceEngine(num_threads=4)

# 注册标的 + 因子
symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
for sym in symbols:
    engine.add_symbol(sym)
    for fid, graph in reg.build_group("okx_perp").items():
        engine.add_factor(sym, fid, graph)

print(f"标的数: {engine.num_symbols()}")    # 3
print(f"线程数: {engine.num_threads()}")    # 4
print(f"标的列表: {engine.symbols()}")      # ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
```

### 构造函数参数

```python
rt.InferenceEngine(num_threads=0)
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `num_threads` | `int` | `0` | 线程池大小。`0` = 自动检测 CPU 核心数 (`std::thread::hardware_concurrency()`) |

### 单标的推送: push_bar()

逐个标的推送 bar, 适合单标的场景或测试:

```python
engine.push_bar("BTC-USDT", close=50000.0, volume=100.0)
engine.push_bar("ETH-USDT", close=3000.0, volume=200.0, high=3050.0, low=2950.0)
```

```python
engine.push_bar(
    symbol,         # str: 标的名称
    close,          # float: 收盘价 (必填)
    volume=NaN,     # float: 成交量 (可选)
    open=NaN,       # float: 开盘价 (可选)
    high=NaN,       # float: 最高价 (可选)
    low=NaN,        # float: 最低价 (可选)
    ret=NaN,        # float: 收益率 (可选)
)
```

### 批量推送: push_bars() (多线程)

同时推送多个标的的 bar, 内部使用线程池并行处理:

```python
bars = {
    "BTC-USDT": rt.BarData(close=50000.0, volume=100.0, high=50100.0, low=49900.0),
    "ETH-USDT": rt.BarData(close=3000.0, volume=200.0),
    "SOL-USDT": rt.BarData(close=150.0, volume=500.0),
}
engine.push_bars(bars)  # 自动释放 GIL, C++ 线程并行处理
```

`push_bars()` 的特点:
- **自动释放 GIL**: 调用时自动 `py::gil_scoped_release`, C++ 线程真正并行
- **barrier 同步**: 主线程等待所有标的处理完毕后才返回
- **适用场景**: 标的数量多时 (50+), 多线程加速显著

### BarData 参数

```python
rt.BarData(
    close,          # float: 收盘价 (必填)
    volume=NaN,     # float: 成交量 (可选)
    open=NaN,       # float: 开盘价 (可选)
    high=NaN,       # float: 最高价 (可选)
    low=NaN,        # float: 最低价 (可选)
    ret=NaN,        # float: 收益率 (可选)
)
```

### 获取结果

```python
# 获取某标的所有因子值
outputs = engine.get_outputs("BTC-USDT")     # list[float]
fids = engine.get_factor_ids("BTC-USDT")     # list[str]
factor_dict = dict(zip(fids, outputs))       # {"0001": 0.123, "0010": -0.456, ...}

# 遍历所有标的
for sym in engine.symbols():
    fids = engine.get_factor_ids(sym)
    outs = engine.get_outputs(sym)
    print(f"{sym}: {dict(zip(fids, outs))}")
```

### 多线程性能参考

| 标的数 | 线程数 | per-bar 延迟 (µs) | vs 单线程加速 |
|--------|--------|-------------------|--------------|
| 10 | 1 | 34 | baseline |
| 10 | 4 | 49 | 0.70x (开销 > 收益) |
| 200 | 1 | 572 | baseline |
| 200 | 4 | 404 | 1.41x |

> 建议: 标的数 < 50 时用单线程 (`num_threads=1`), 标的数 > 50 时多线程开始有优势。

---

## 7. Engine 集成: 数据流 + 因子推理一体化

`Engine` 是 Python 层的顶层入口, 整合了数据采集 (DataflowManager / SimDataflowManager) 和 C++ 因子推理 (InferenceEngine):

### 快速上手

```python
from factorengine.engine import Engine

# 创建引擎 (simulation 模式)
engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    mode="simulation",
    sim_seed=42,
    factor_group="okx_perp",   # 加载 okx_perp 平台的因子
    num_threads=4,
)

# 启动数据流
engine.start()

# 等待数据积累
import time
time.sleep(5)

# 获取数据 (内部自动推送新 bar 到 InferenceEngine)
snapshot = engine.get_data()

# 获取因子值
factors = engine.get_factor_outputs()
# 返回: {"BTC-USDT-SWAP": {"0001": 0.123, "0010": -0.456, ...},
#         "ETH-USDT-SWAP": {"0001": 0.789, ...}}

# 查看因子列表
print(engine.factor_ids)  # ["0001", "0010", "0020", "0050", "0100"]

# 停止
engine.stop()
```

### Engine 构造函数参数

```python
Engine(
    symbols,                          # list[str]: 标的列表
    data_freq="5s",                   # str: K线周期 (live 模式)
    pull_interval="10s",              # str: 数据拉取间隔 (live 模式)
    bar_window_length=1000,           # int: bar 缓存长度
    trade_window_length=10_000,       # int: trade 缓存长度
    book_history_length=1_000,        # int: orderbook 缓存长度
    enable_bars=True,                 # bool: 是否采集 bar
    enable_trades=False,              # bool: 是否采集 trade
    trade_channels=("trades-all",),   # tuple: trade 频道
    enable_books=False,               # bool: 是否采集 orderbook
    book_channels=("books5",),        # tuple: book 频道
    mode="live",                      # str: "live" 或 "simulation"
    sim_bar_interval=None,            # float | None: simulation 模式出 bar 间隔 (秒)
    sim_seed=None,                    # int | None: simulation 随机种子
    factor_group=None,                # str | None: 因子组名 (如 "okx_perp")
    num_threads=4,                    # int: InferenceEngine 线程数
    signal_buffer_size=3,             # int: signal_deque 最大长度
    bar_queue_size=16,                # int: bar_queue 最大容量
    bar_queue_timeout=0.5,            # float: runtime 线程 queue.get() 超时 (秒)
)
```

#### 参数详解

**数据采集参数 (live 模式专用)**:

| 参数 | 说明 | 示例 |
|------|------|------|
| `data_freq` | K 线的逻辑周期, 决定每根 bar 代表多长时间 | `"5s"`, `"1min"`, `"1h"` |
| `pull_interval` | 从交易所拉取数据的轮询间隔 | `"10s"`, `"30s"` |

**数据采集参数 (simulation 模式专用)**:

| 参数 | 说明 | 示例 |
|------|------|------|
| `sim_bar_interval` | 模拟器每隔多少秒吐出一根 bar (真实时间) | `1.0` (默认), `0.01` (高速) |
| `sim_seed` | 随机种子, 确保可复现 | `42` |

> 注意: simulation 模式下 `data_freq` 和 `pull_interval` **不生效**。`sim_bar_interval` 控制的是出 bar 速度, 不代表 K 线的逻辑周期。

**因子推理参数**:

| 参数 | 说明 | 示例 |
|------|------|------|
| `factor_group` | 因子组名, 对应 `factorengine/factors/` 下的子包 | `"okx_perp"`, `None` (不启用因子) |
| `num_threads` | InferenceEngine 的 C++ 线程池大小 | `4` |
| `signal_buffer_size` | `signal_deque` 最大长度, 主线程从尾部读取最新结果 | `3` |
| `bar_queue_size` | dataflow→runtime 线程间 `bar_queue` 最大容量 | `16` |
| `bar_queue_timeout` | runtime 线程 `queue.get()` 超时秒数, 影响停机响应速度 | `0.5` |

### Engine 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `start()` | `() → None` | 启动 dataflow 线程和 runtime 线程 |
| `stop()` | `() → None` | 停止所有线程 |
| `get_data()` | `(symbols?) → dict[str, np.ndarray]` | 获取 bar cache 快照 (深拷贝) |
| `get_factor_outputs()` | `(symbols?) → dict[str, dict[str, float]]` | 获取最新因子值 (读 signal_deque[-1], O(1)) |
| `factor_ids` | `property → list[str]` | 已注册的因子 ID 列表 |
| `bars_pushed` | `property → int` | runtime 线程已推理的轮次数 |
| `signal_deque` | `property → deque` | 信号缓冲区 (调试用) |
| `bar_count` | `property → int` | 累计 bar 数 |

### 三线程架构 (v2)

```
[dataflow 线程]  sim-bars
    生成 bar → BarCache.append() → bar_queue.put()

[runtime 线程]  factor-infer
    bar_queue.get() → push_bars (C++ 并行) → signal_deque.append()

[主线程]  策略 / 消费者
    get_factor_outputs() → signal_deque[-1]   (O(1), 无计算)
    get_data()           → BarCache.snapshot() (深拷贝)
```

关键设计:
- `get_factor_outputs()` 和 `get_data()` **完全独立**, 无先后顺序要求
- 因子推理在独立的 runtime 线程中自动进行, 不需要手动触发
- `bar_queue` 是 dataflow→runtime 线程的桥梁, 保证 bar 按序消费
- `signal_deque` 只保留最近 N 轮结果, 主线程零成本读取最新信号

### 典型使用模式

```python
from factorengine.engine import Engine
import time

engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    mode="simulation", sim_seed=42, sim_bar_interval=0.02,
    factor_group="okx_perp", num_threads=4,
    signal_buffer_size=3,
)
engine.start()

for step in range(100):
    time.sleep(1.0)

    factors = engine.get_factor_outputs()   # O(1), 读最新信号
    snapshot = engine.get_data()            # bar cache 快照 (独立)

    for sym, fvals in factors.items():
        print(f"[step {step}] {sym}: {fvals}")

engine.stop()
```

---

## 8. 因子注册与平台管理

### 目录结构

```
factorengine/factors/
├── __init__.py              # 导出 FactorRegistry, register_factor
├── registry.py              # 注册核心逻辑
├── visualize.py             # 可视化工具
├── okx_perp/                # OKX 永续合约因子
│   ├── __init__.py
│   └── factor_bank.py       # @register_factor("okx_perp", "0001") ...
├── binance_perp/            # (未来) 币安永续
│   ├── __init__.py
│   └── factor_bank.py
└── stock_cn/                # (未来) A 股
    ├── __init__.py
    └── factor_bank.py
```

### FactorRegistry API 速查

| 方法 | 说明 |
|------|------|
| `reg.load_all()` | 递归扫描所有子包, 加载全部因子 |
| `reg.load_group("okx_perp")` | 只加载指定平台的因子 |
| `reg.groups` | 已加载的平台列表 |
| `reg.factor_ids` | 所有因子 ID (跨平台去重) |
| `reg.factor_ids_by_group("okx_perp")` | 指定平台的因子 ID 列表 |
| `reg.build("0001")` | 构建单个因子 (自动搜索平台, 有歧义时报错) |
| `reg.build("0001", group="okx_perp")` | 指定平台构建 |
| `reg.build_all()` | 构建所有因子, 返回 `{fid: FactorGraph}` |
| `reg.build_group("okx_perp")` | 构建指定平台的所有因子 |
| `len(reg)` | 已注册因子总数 |
| `"0001" in reg` | 检查因子是否已注册 |

---

## 9. 添加新因子的完整流程

以添加因子 `0200` (短期/长期均线比率) 为例:

### Step 1: 编写建图函数

在 `factorengine/factors/okx_perp/factor_bank.py` 中添加:

```python
@register_factor("okx_perp", "0200")
def build_factor_0200() -> rt.FactorGraph:
    """Ma(close, 20) / Ma(close, 60) - 1

    短期/长期均线比率.
    """
    g = rt.FactorGraph()
    c = g.add_input("close")
    ma20 = g.add_rolling(Op.MA, c, 20)
    ma60 = g.add_rolling(Op.MA, c, 60)
    ratio = g.add_binary(Op.DIV, ma20, ma60)
    g.add_scalar_op(Op.SUB_SCALAR, ratio, 1.0)
    g.compile()
    return g
```

无需修改任何其他文件, `FactorRegistry.load_group("okx_perp")` 会自动发现。

### Step 2: 验证注册成功

```python
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_group("okx_perp")
assert "0200" in reg
print(reg.factor_ids_by_group("okx_perp"))  # 应包含 "0200"
```

### Step 3: 编写对齐测试

在 `tests/factors/test_real_factors.py` 中添加测试类:

```python
class TestFactor0200:
    @staticmethod
    def build_graph():
        g = rt.FactorGraph()
        c = g.add_input("close")
        ma20 = g.add_rolling(Op.MA, c, 20)
        ma60 = g.add_rolling(Op.MA, c, 60)
        ratio = g.add_binary(Op.DIV, ma20, ma60)
        g.add_scalar_op(Op.SUB_SCALAR, ratio, 1.0)
        g.compile()
        return g

    @staticmethod
    def pandas_ref(close):
        """Python 参考实现 (pandas)"""
        s = pd.Series(close)
        ma20 = s.rolling(20, min_periods=20).mean()
        ma60 = s.rolling(60, min_periods=60).mean()
        return (ma20 / ma60 - 1.0).values.astype(np.float32)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_alignment(self, seed):
        close, volume, open_, high, low, ret = make_ohlcv(seed)
        g = self.build_graph()
        cpp = push_all(g, close, volume, open_, high, low, ret)
        py = self.pandas_ref(close)
        assert_aligned(cpp, py, "0200")
```

### Step 4: 运行测试

```bash
# 运行新因子的测试
pytest tests/factors/test_real_factors.py::TestFactor0200 -v

# 运行所有因子测试
pytest tests/factors/test_real_factors.py -v
```

### Step 5: 可视化确认

```python
from factorengine.factors import FactorRegistry
from factorengine.factors.visualize import print_graph

reg = FactorRegistry()
reg.load_group("okx_perp")
g = reg.build("0200", group="okx_perp")
print_graph(g)
```

### 对齐测试编写要点

1. **`build_graph()`**: 复制因子注册函数的建图逻辑
2. **`pandas_ref()`**: 用 pandas rolling API 实现相同的数学表达式
   - `rolling(w, min_periods=w).mean()` → `Ma(x, w)`
   - `rolling(w, min_periods=w).std(ddof=0)` → `TsStd(x, w)` (注意 ddof=0)
   - `rolling(w).apply(lambda w: pd.Series(w).rank(pct=True).iloc[-1])` → `TsRank(x, w)`
3. **`assert_aligned(cpp, py, label, atol, rtol)`**: 对比两组输出
   - 默认容差: `atol=1e-3, rtol=1e-3`
   - 含 TsRank/Corr 等近似算子的因子: 适当放宽到 `atol=5e-2, rtol=5e-2`
4. **`make_ohlcv(seed, n)`**: 生成合成 OHLCV 数据
5. **`push_all(g, close, volume, open_, high, low, ret)`**: 逐 bar 推送并收集输出
6. **`clean_factor(x)`**: 将 inf/NaN 置零后比较

### 新增平台

```bash
mkdir -p factorengine/factors/binance_perp
touch factorengine/factors/binance_perp/__init__.py
```

```python
# factorengine/factors/binance_perp/factor_bank.py
from factorengine.factors.registry import register_factor
import fe_runtime as rt
Op = rt.Op

@register_factor("binance_perp", "0001")
def build_binance_0001() -> rt.FactorGraph:
    g = rt.FactorGraph()
    c = g.add_input("close")
    g.add_rolling(Op.MA, c, 60)
    g.compile()
    return g
```

使用:

```python
reg = FactorRegistry()
reg.load_group("binance_perp")       # 只加载 binance
# 或
reg.load_all()                        # 加载所有平台
print(reg.groups)                     # ["binance_perp", "okx_perp"]
```

---

## 10. 可视化与调试

### ASCII 可视化

```python
from factorengine.factors.visualize import print_graph

g = reg.build("0001", group="okx_perp")
print_graph(g)
```

输出:

```
FactorGraph: 5 nodes, warmup=120 bars
============================================================
  [0] INPUT_CLOSE
  [1] MA w=120  (a←[0])
  [2] SUB  (a←[0], b←[1])
  [3] TS_STD w=60  (a←[0])
  [4] DIV  (a←[2], b←[3])  ◀ OUTPUT
============================================================

Edges:
  [0] INPUT_CLOSE ──→ [1] MA
  [0] INPUT_CLOSE ──→ [2] SUB
  [1] MA ──→ [2] SUB
  [0] INPUT_CLOSE ──→ [3] TS_STD
  [2] SUB ──→ [4] DIV
  [3] TS_STD ──→ [4] DIV
```

### Graphviz 图片

```python
from factorengine.factors.visualize import render_graph, to_dot

g = reg.build("0010", group="okx_perp")
render_graph(g, "factor_0010.png", title="Factor 0010")
```

### 运行时调试

```python
g = reg.build("0001", group="okx_perp")

print(f"节点数: {g.num_nodes()}")
print(f"warmup: {g.warmup_bars()} bars")

for i in range(200):
    g.push_bar(close=100.0 + i * 0.1, volume=1000.0)
    raw = g.raw_output()     # 原始值 (可能是 NaN / inf)
    clean = g.output()       # 清洗值 (inf/NaN → 0.0)
    ready = g.ready()        # warmup 是否完成

    if i % 50 == 0:
        print(f"bar {i:3d}: ready={ready}, raw={raw:.4f}, clean={clean:.4f}")
```

### describe() 内省

```python
for info in g.describe():
    print(f"  [{info.id}] {info.op_name} "
          f"inputs=({info.input_a},{info.input_b}) "
          f"w={info.window} "
          f"{'◀ OUTPUT' if info.is_output else ''}")
```

---

## 11. API 参考

### fe_runtime.FactorGraph

| 方法 | 签名 | 说明 |
|------|------|------|
| `add_input` | `(feature: str) → int` | 添加输入节点。feature: "close", "volume", "open", "high", "low", "ret" |
| `add_unary` | `(op: Op, src: int) → int` | 一元算子 |
| `add_binary` | `(op: Op, src_a: int, src_b: int) → int` | 二元算子 |
| `add_rolling` | `(op: Op, src: int, window: int) → int` | 滚动算子 |
| `add_bivariate` | `(op: Op, src_a: int, src_b: int, window: int) → int` | 双变量算子 |
| `add_scalar_op` | `(op: Op, src: int, scalar: float) → int` | 标量算子 |
| `add_autocorr` | `(src: int, window: int, lag: int) → int` | 自相关算子 |
| `compile` | `() → None` | 编译 DAG, 分配 kernel 和 warmup 计算 |
| `push_bar` | `(close, volume?, open?, high?, low?, ret?) → None` | 推一根 bar |
| `ready` | `() → bool` | warmup 是否完成 |
| `output` | `() → float` | 输出值 (inf/NaN → 0.0) |
| `raw_output` | `() → float` | 原始输出值 |
| `warmup_bars` | `() → int` | warmup 所需的 bar 数 |
| `bars_seen` | `() → int` | 已推入的 bar 数 |
| `num_nodes` | `() → int` | DAG 节点数 |
| `describe` | `() → list[NodeInfo]` | 导出图结构 |
| `reset` | `() → None` | 重置所有状态 |

### fe_runtime.SymbolRunner

| 方法 | 签名 | 说明 |
|------|------|------|
| `__init__` | `(symbol: str)` | 创建 runner |
| `add_factor` | `(factor_id: str, graph: FactorGraph)` | 添加因子 |
| `push_bar` | `(close, volume?, open?, high?, low?, ret?)` | 推一根 bar (所有因子) |
| `symbol` | `() → str` | 标的名 |
| `num_factors` | `() → int` | 因子数 |
| `bars_pushed` | `() → int` | 已推入的 bar 数 |
| `outputs` | `() → list[float]` | 所有因子值 |
| `output` | `(idx: int) → float` | 第 idx 个因子值 |
| `output_by_id` | `(factor_id: str) → float` | 按 ID 查值 |
| `factor_ids` | `() → list[str]` | 因子 ID 列表 |
| `reset` | `() → None` | 重置 |

### fe_runtime.InferenceEngine

| 方法 | 签名 | 说明 |
|------|------|------|
| `__init__` | `(num_threads: int = 0)` | 创建引擎。0 = 自动检测 CPU 核心数 |
| `add_symbol` | `(symbol: str)` | 注册标的 |
| `add_factor` | `(symbol: str, factor_id: str, graph: FactorGraph)` | 为标的添加因子 |
| `push_bar` | `(symbol: str, close, volume?, open?, high?, low?, ret?)` | 单标的推送 |
| `push_bars` | `(bars: dict[str, BarData])` | 批量推送 (多线程, 自动释放 GIL) |
| `get_outputs` | `(symbol: str) → list[float]` | 获取标的所有因子值 |
| `get_factor_ids` | `(symbol: str) → list[str]` | 获取标的因子 ID 列表 |
| `symbols` | `() → list[str]` | 所有标的 |
| `num_symbols` | `() → int` | 标的数 |
| `num_threads` | `() → int` | 线程池大小 |
| `reset` | `() → None` | 重置所有标的 |

### fe_runtime.BarData

| 字段 | 类型 | 说明 |
|------|------|------|
| `close` | `float` | 收盘价 (必填) |
| `volume` | `float` | 成交量 (可选, 默认 NaN) |
| `open` | `float` | 开盘价 (可选, 默认 NaN) |
| `high` | `float` | 最高价 (可选, 默认 NaN) |
| `low` | `float` | 最低价 (可选, 默认 NaN) |
| `ret` | `float` | 收益率 (可选, 默认 NaN) |

### fe_runtime.Op (算子枚举)

| 类别 | 算子 |
|------|------|
| 输入 | `INPUT_CLOSE`, `INPUT_VOLUME`, `INPUT_OPEN`, `INPUT_HIGH`, `INPUT_LOW`, `INPUT_RET` |
| P0 一元 | `NEG`, `ABS`, `LOG`, `SQR`, `INV`, `SIGN`, `TANH`, `SLOG1P` |
| P0 二元 | `ADD`, `SUB`, `MUL`, `DIV` |
| P0 标量 | `ADD_SCALAR`, `SUB_SCALAR`, `MUL_SCALAR`, `DIV_SCALAR`, `SCALAR_SUB`, `SCALAR_DIV` |
| P1 滚动 | `MA`, `TS_SUM`, `TS_STD`, `TS_VARI`, `EMA`, `TS_MIN`, `TS_MAX`, `TS_RANK`, `TS_ZSCORE`, `DELAY`, `TS_DIFF`, `TS_PCT` |
| P2 双变量 | `CORR`, `AUTOCORR`, `TS_MINMAX_DIFF`, `TS_SKEW` |
| P3 复杂 | `TS_MED`, `TS_MAD`, `TS_WMA`, `TS_MAX_DIFF`, `TS_MIN_DIFF` |
| 优化变体 | `TREAP_TS_RANK` (O(log n) Treap, 大窗口推荐) |
| 派生 | `PCT_CHANGE` |

---

## 12. 常见问题

### Q: warmup 阶段输出什么？

warmup 阶段 `g.ready()` 返回 `False`, `g.output()` 返回 `0.0`, `g.raw_output()` 通常返回 `NaN`.

### Q: 为什么 output() 和 raw_output() 不一样？

`output()` 内部做了清洗: `inf → 0.0`, `NaN → 0.0`. `raw_output()` 返回原始值, 适合调试.

### Q: 一个 FactorGraph 能给多个标的复用吗？

**不能**. 每个 FactorGraph 内部有独立状态 (ring buffer 等). 每个 (标的, 因子) 需要独立的 FactorGraph 实例. 这就是为什么 `reg.build()` 每次调用都创建新实例.

### Q: push_bar 的字段顺序重要吗？

是的, 位置参数必须是 `(close, volume, open, high, low, ret)`. 使用关键字参数更安全:

```python
engine.push_bar("BTC", close=50000.0, volume=100.0, high=50100.0, low=49900.0)
```

### Q: 如何只用 close 和 volume？

不需要的字段不传即可 (默认 NaN). 只要因子表达式没有引用 `high` / `low` 等, NaN 不会影响结果.

### Q: reset() 后需要重新 warmup 吗？

**是的**. `reset()` 清空所有 kernel 状态, 相当于回到初始状态, 需要重新 push `warmup_bars` 根 bar.

### Q: push_bar() 和 push_bars() 的区别？

| | `push_bar()` | `push_bars()` |
|---|---|---|
| 输入 | 单个标的 + OHLCV | `dict[str, BarData]` (多标的) |
| 执行 | 单线程, 立即执行 | 线程池并行, barrier 同步 |
| GIL | 不释放 | 自动释放 |
| 适用 | 单标的测试, 少量标的 | 生产环境, 大量标的 |

### Q: num_threads 设多少合适？

- 标的 < 50: `num_threads=1` (单线程, 避免线程调度开销)
- 标的 50-200: `num_threads=4`
- 标的 > 200: `num_threads=8`
- `num_threads=0`: 自动检测 CPU 核心数

### Q: TS_RANK 还是 TREAP_TS_RANK？

- `window < 1000`: 用 `TS_RANK` (brute-force, 缓存友好, 更快)
- `window >= 1000`: 两者持平
- `window >= 4320`: 用 `TREAP_TS_RANK` (O(log n), 约快 27%)
- 默认用 `TS_RANK` 即可

### Q: Engine 的 get_data() 和 get_factor_outputs() 的调用顺序？

**无顺序要求**。v2 三线程架构下, 因子推理由独立的 runtime 线程自动完成, `get_factor_outputs()` 直接读 `signal_deque[-1]` (O(1)), `get_data()` 读 BarCache 快照。两者完全独立, 可以任意顺序调用。

### Q: simulation 模式的 sim_bar_interval 和 data_freq 的关系？

它们**无关**。`data_freq` 是 live 模式下 K 线的逻辑周期 (如 5 秒 K 线)。`sim_bar_interval` 是 simulation 模式下出 bar 的真实时间间隔 (控制模拟速度)。simulation 模式的 `BarGenerator` 直接合成完整的 OHLCV bar, 不做聚合。

### Q: 如何调试因子输出不正确？

1. 用 `print_graph(g)` 确认 DAG 结构正确
2. 用 `g.raw_output()` 查看原始值 (区分 NaN 和 0.0)
3. 检查 `g.warmup_bars()` 是否推够了足够的 bar
4. 写 pandas 参考实现, 逐 bar 对比输出
5. 用 `g.describe()` 查看每个节点的详细信息
