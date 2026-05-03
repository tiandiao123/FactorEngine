# OKX Kline → Factor Engine 完整链路讲解

> 日期: 2026-05-03
> 版本: v2 (round batching)
> 适用代码: `dataflow/okx/kline/`, `factorengine/engine.py`, `factorengine/factors/`

---

## 全局架构图

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                        OKX 交易所 (云端)                                 │
│                                                                         │
│   candle1s WebSocket: wss://ws.okx.com:8443/ws/v5/business              │
│   每秒推送 BTC/ETH/SOL 的 1s K线 (含 OHLCV + confirm 标记)              │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ JSON over WebSocket
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  线程 1: okx-kline (KlineWorker daemon thread)                          │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────┐        │
│  │  OXKlineCollector                                            │        │
│  │  • 连接 WS, 订阅 candle1s (按 200 symbol/连接 分组)          │        │
│  │  • 解析 JSON → on_candle(symbol, raw_array) 回调             │        │
│  │  • 断线自动重连 (3~5s)                                        │        │
│  └────────────────────────────┬─────────────────────────────────┘        │
│                               │ on_candle("BTC", ["1714700005000",       │
│                               │   "95000.5","95100","94900","95050",     │
│                               │   "12.3","1169115","1169115","1"])       │
│                               ▼                                          │
│  ┌──────────────────────────────────────────────────────────────┐        │
│  │  KlineAdapter                                                │        │
│  │  • 过滤: raw[-1] != "1" → 返回 None (丢弃未确认 candle)      │        │
│  │  • 解析: raw → BarPoint (np.ndarray, shape=(8,), float64)    │        │
│  │  • 聚合: freq < 60s → 攒 N 根 1s candle, 合并为 1 根 Ns bar  │        │
│  │          freq >= 60s → 直接输出 (WS 频道已匹配)               │        │
│  │  • Gap 检测: ts 跳变 > 2×interval → 清空 buffer, 重新聚合    │        │
│  └────────────────────────────┬─────────────────────────────────┘        │
│                               │ BarPoint (5s bar) 或 None                │
│                               ▼                                          │
│  ┌──────────────────────────────────────────────────────────────┐        │
│  │  KlinePublisher (v2 round batching)                          │        │
│  │  • 去重: ts <= last_pushed_ts[symbol] → 丢弃                 │        │
│  │  • 攒批: 收集所有 symbol 的 bar 到 _pending_round             │        │
│  │  • 到齐: 所有 symbol 到达 → 立即 flush                        │        │
│  │  • 超时: min(freq*0.8, 4s) → forward-fill 缺失 symbol        │        │
│  │  • 第一轮: 必须等所有 symbol 到齐 (无历史可填充)               │        │
│  │  • 推送: bar_queue.put(round_bars, block=False)              │        │
│  │  • 可选: debug_cache.append(symbol, bar)                     │        │
│  └────────────────────────────┬─────────────────────────────────┘        │
│                               │                                          │
└───────────────────────────────┼──────────────────────────────────────────┘
                                │
             ╔══════════════════╧═══════════════════╗
             ║  bar_queue (queue.Queue, maxsize=16)  ║
             ║                                       ║
             ║  每个元素:                             ║
             ║  {                                     ║
             ║    "BTC-USDT-SWAP": ndarray(8,),      ║
             ║    "ETH-USDT-SWAP": ndarray(8,),      ║
             ║    "SOL-USDT-SWAP": ndarray(8,),      ║
             ║  }                                     ║
             ╚══════════════════╤═══════════════════╝
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  线程 2: factor-infer (Engine._runtime_loop daemon thread)              │
│                                                                          │
│  while not stop:                                                         │
│      round_bars = bar_queue.get(timeout=0.5)                            │
│                                                                          │
│      ┌─────────────────────────────────────────────────────────┐        │
│      │  构造 rt.BarData (per symbol)                           │        │
│      │                                                         │        │
│      │  bar[0]=ts  bar[1]=open  bar[2]=high  bar[3]=low       │        │
│      │  bar[4]=close  bar[5]=vol                               │        │
│      │                                                         │        │
│      │  ret = (close / prev_close - 1.0)   ← 涨跌幅           │        │
│      │  batch[sym] = rt.BarData(close, vol, open, high, low,  │        │
│      │                          ret)                           │        │
│      └────────────────────────────┬────────────────────────────┘        │
│                                   │                                      │
│      ┌────────────────────────────▼────────────────────────────┐        │
│      │  inference.push_bars(batch)                             │        │
│      │  → C++ InferenceEngine (num_threads=4 并行)              │        │
│      │  → 对每个 symbol × 每个 factor 的 DAG 做前向推理         │        │
│      │                                                         │        │
│      │  例: factor 0001 的 DAG:                                │        │
│      │    close → MA(120) → Sub(close, ma) → Div(dev, vol)    │        │
│      │    close → TsStd(60) ──────────────┘                   │        │
│      │                                                         │        │
│      │  内部维护每个节点的滚动窗口状态                           │        │
│      └────────────────────────────┬────────────────────────────┘        │
│                                   │                                      │
│      result = inference.get_all_outputs()                               │
│      → {"BTC": {"0001": 0.34, "0002": -0.12, ...},                     │
│         "ETH": {"0001": 0.18, ...},                                     │
│         "SOL": {"0001": 0.52, ...}}                                     │
│                                                                          │
│      signal_deque.append({"ts": ts_ms, "bar_index": N,                  │
│                           "factors": result})                            │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                                │
             ╔══════════════════╧═══════════════════╗
             ║  signal_deque (deque, maxlen=3)       ║
             ║                                       ║
             ║  保留最近 3 轮因子结果                  ║
             ║  主线程 O(1) 读取 deque[-1]            ║
             ╚══════════════════╤═══════════════════╝
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  线程 3: 主线程 (策略 / 用户脚本)                                         │
│                                                                          │
│  factors = engine.get_factor_outputs()    ← 读 signal_deque[-1]         │
│  snapshot = engine.get_data()             ← 读 debug_cache (可选)        │
│                                                                          │
│  factors = {                                                             │
│      "BTC-USDT-SWAP": {"0001": 0.342, "0002": -0.128, ..., "0060": ..} │
│      "ETH-USDT-SWAP": {"0001": 0.189, ...}                              │
│      "SOL-USDT-SWAP": {"0001": 0.521, ...}                              │
│  }                                                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 各阶段详解

### 阶段 1: OKX WebSocket 数据收集

**组件**: `OXKlineCollector` (`dataflow/okx/kline/collector.py`)

```text
用户配置 data_freq="5s"
    → resolve_bar_channel(5) → channel="candle1s" (因为 5 < 60)
    → Collector 订阅 candle1s 频道
```

OKX 每秒推送一条 JSON:

```json
{
  "arg": {"channel": "candle1s", "instId": "BTC-USDT-SWAP"},
  "data": [["1714700001000", "95000.5", "95100", "94900", "95050",
            "12.3", "1169115", "1169115", "1"]]
}
```

字段含义:

```text
index  字段           示例
0      ts (ms)        "1714700001000"
1      open           "95000.5"
2      high           "95100"
3      low            "94900"
4      close          "95050"
5      vol (base)     "12.3"         ← BTC 数量
6      volCcy         "1169115"      ← USDT 计价
7      volCcyQuote    "1169115"
8      confirm        "1"            ← "0"=进行中, "1"=已确认
```

Collector 收到后, 对每条 `data` 记录调用 `on_candle("BTC-USDT-SWAP", raw_array)`.

### 阶段 2: 过滤 + 聚合

**组件**: `KlineAdapter` (`dataflow/okx/kline/adapter.py`)

**步骤 1: 过滤未确认 candle**

```text
raw[-1] == "0" → 返回 None, 忽略
raw[-1] == "1" → 继续处理
```

OKX 在一秒内可能推送多次同一 candle (进行中 + 最终确认), 只处理确认的.

**步骤 2: 解析为 BarPoint**

```text
raw (string list) → np.array([ts, open, high, low, close, vol, volCcy, volCcyQuote], dtype=float64)
```

**步骤 3: 聚合 (仅 freq < 60s)**

以 `data_freq="5s"` 为例, 攒 5 根 1s candle 后合并:

```text
1s candle 1:  ts=T+0  open=100 high=105 low=95  close=102 vol=10  volCcy=1020
1s candle 2:  ts=T+1  open=102 high=108 low=101 close=107 vol=12  volCcy=1284
1s candle 3:  ts=T+2  open=107 high=109 low=103 close=104 vol=8   volCcy=832
1s candle 4:  ts=T+3  open=104 high=106 low=100 close=105 vol=15  volCcy=1575
1s candle 5:  ts=T+4  open=105 high=110 low=104 close=109 vol=11  volCcy=1199

聚合结果 (5s bar):
  ts       = T+0       (第一根的 ts)
  open     = 100       (第一根的 open)
  high     = 110       (所有 high 的最大值)
  low      = 95        (所有 low 的最小值)
  close    = 109       (最后一根的 close)
  vol      = 56        (求和: 10+12+8+15+11)
  volCcy   = 5910      (求和)
  volCcyQt = 5910      (求和)
```

前 4 根返回 `None`, 第 5 根返回聚合后的 `BarPoint`.

### 阶段 3: 攒批推送

**组件**: `KlinePublisher` (`dataflow/okx/kline/publisher.py`)

这是 v2 的核心改进. 不再逐 symbol 推送, 而是攒齐一轮:

```text
时间线 (5s 频率, 3 个 symbol):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  T+5.10s  BTC 5s bar 到达
           └→ _pending_round["BTC"] = bar       (1/3)

  T+5.18s  ETH 5s bar 到达
           └→ _pending_round["ETH"] = bar       (2/3)

  T+5.25s  SOL 5s bar 到达
           └→ _pending_round["SOL"] = bar       (3/3 ✓ 全部到齐!)
           └→ bar_queue.put({"BTC":bar, "ETH":bar, "SOL":bar})
           └→ 清空 _pending_round
```

**异常情况: SOL 超时**

```text
  T+5.10s  BTC 5s bar 到达   → pending (1/3), 记录 _round_start_time
  T+5.18s  ETH 5s bar 到达   → pending (2/3)
  T+9.10s  超时! (4s elapsed > batch_timeout_s=4.0)
           └→ SOL 缺失, 用上一轮的 SOL bar forward-fill
           └→ bar_queue.put({"BTC":新, "ETH":新, "SOL":旧})
```

### 阶段 4: 因子推理

**组件**: `Engine._runtime_loop` (`factorengine/engine.py`)

```text
round_bars = bar_queue.get()

遍历所有 symbol:
  BTC:  close=95050, prev_close=95000 → ret=+0.000526
  ETH:  close=3200,  prev_close=3195  → ret=+0.001565
  SOL:  close=148.5, prev_close=148.0 → ret=+0.003378

  batch = {
    "BTC": rt.BarData(95050, 12.3, 95000.5, 95100, 94900, 0.000526),
    "ETH": rt.BarData(3200,  8.5,  3195,    3210,  3190,  0.001565),
    "SOL": rt.BarData(148.5, 50.0, 148.0,   149.5, 147.5, 0.003378),
  }

inference.push_bars(batch)   ← C++ 并行计算
```

`push_bars` 内部对每个 symbol 的每个 factor graph (共 60 个) 做前向推理:

```text
Factor 0001: Div(Sub(close, Ma(close,120)), TsStd(close,60))

  C++ DAG 执行:
    node 0: INPUT_CLOSE → 获取本轮 close = 95050
    node 1: MA(node0, window=120) → 滚动均值 (内部维护 120 长度的环形缓冲)
    node 2: SUB(node0, node1) → close - ma120
    node 3: TS_STD(node0, window=60) → 滚动标准差
    node 4: DIV(node2, node3) → 价格偏离 / 波动率 → 输出 0.342

  结果: factor_0001 = 0.342
```

60 个因子全部计算完, 取出结果:

```text
result = inference.get_all_outputs()
→ {"BTC": {"0001": 0.342, "0002": -0.128, ..., "0060": 1.23},
   "ETH": {"0001": 0.189, ...},
   "SOL": {"0001": 0.521, ...}}
```

存入 `signal_deque`, 主线程随时可读.

### 阶段 5: 策略消费

**组件**: `Engine.get_factor_outputs()` (主线程)

```python
factors = engine.get_factor_outputs()
# → {"BTC-USDT-SWAP": {"0001": 0.342, "0002": -0.128, ...}, ...}

# 策略逻辑示例:
for sym, fvals in factors.items():
    alpha = fvals["0001"] * 0.5 + fvals["0003"] * 0.3
    if alpha > threshold:
        place_order(sym, "buy")
```

---

## 三线程模型总览

```text
线程 1: okx-kline          线程 2: factor-infer         线程 3: 主线程
(数据采集)                  (因子计算)                    (策略消费)
    │                           │                            │
    │  OKX WS → Collector       │                            │
    │  → Adapter (过滤+聚合)     │                            │
    │  → Publisher (攒批)        │                            │
    │                           │                            │
    │  bar_queue.put(           │                            │
    │    {BTC:bar,ETH:bar,      │                            │
    │     SOL:bar}              │                            │
    │  ) ─────────────────→     │                            │
    │                           │  bar_queue.get()           │
    │                           │  构造 BarData              │
    │                           │  push_bars() ← C++并行     │
    │                           │  get_all_outputs()         │
    │                           │  signal_deque.append()     │
    │                           │  ──────────────────────→   │
    │                           │                            │  get_factor_outputs()
    │                           │                            │  读 deque[-1]
    │                           │                            │  → 策略决策
    │                           │                            │
    ▼                           ▼                            ▼
  5s/轮                       5s/轮                        自由节奏
  持续运行                    持续运行                      sleep + poll
```

---

## 数据格式流转

```text
阶段              数据格式                                  示例
─────────────────────────────────────────────────────────────────────────
OKX WS 消息       list[str] (9个字段)                       ["1714700005000","95000.5",...]

Adapter 输出      np.ndarray(8,) float64                   [1.7147e+12, 95000.5, 95100, ...]
(BarPoint)        [ts, open, high, low, close,
                   vol, volCcy, volCcyQuote]

Publisher 推送    dict[str, ndarray]                        {"BTC": ndarray(8,),
(round_bars)      所有 symbol 凑齐为一轮                      "ETH": ndarray(8,),
                                                             "SOL": ndarray(8,)}

runtime_loop      dict[str, rt.BarData]                     {"BTC": BarData(close=95050,
构造的 batch      C++ 结构体                                          vol=12.3, ...),
                                                             "ETH": BarData(...)}

push_bars 输出    dict[str, dict[str, float]]               {"BTC": {"0001": 0.342, ...},
(因子结果)                                                    "ETH": {"0001": 0.189, ...}}

signal_deque      dict                                      {"ts": 1714700005000,
                  {ts, bar_index, factors}                   "bar_index": 42,
                                                             "factors": {上面的dict}}

get_factor_       dict[str, dict[str, float]]               同 push_bars 输出
outputs() 返回    直接从 deque[-1]["factors"] 取
```

---

## 因子加载机制

```text
Engine.__init__(factor_group="okx_perp")
    │
    ▼
_init_inference("okx_perp", 4)
    │
    ├── FactorRegistry().load_group("okx_perp")
    │       │
    │       ├── _scan_package("factorengine.factors.okx_perp")
    │       │       │
    │       │       └── importlib.import_module("factorengine.factors.okx_perp.factor_bank")
    │       │               │
    │       │               └── @register_factor("okx_perp", "0001")  ← 装饰器自动注册
    │       │                   @register_factor("okx_perp", "0002")
    │       │                   ... (共 60 个)
    │       │
    │       └── 全局 _GLOBAL_REGISTRY 中现有 60 个 builder
    │
    ├── rt.InferenceEngine(num_threads=4)   ← 创建 C++ 推理引擎
    │
    └── for sym in symbols:
            for fid, graph in reg.build_group("okx_perp").items():
                inference.add_factor(sym, fid, graph)
                ← 每个 symbol 注册 60 个编译好的 FactorGraph DAG

最终: 3 symbols × 60 factors = 180 个 DAG 实例
```

---

## 关键配置参数

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `data_freq` | `"5s"` | K 线频率, 决定聚合策略 |
| `factor_group` | `"okx_perp"` | 因子组, 决定加载哪些因子 |
| `num_threads` | `4` | C++ 推理并行线程数 |
| `bar_queue_size` | `16` | bar_queue 最大容量 |
| `signal_buffer_size` | `3` | signal_deque 保留最近几轮结果 |
| `batch_timeout_s` | `min(freq*0.8, 4.0)` | 攒批超时阈值 |
| `enable_debug_cache` | `True` | 是否保留 bar 历史快照 |

---

## 一句话总结

OKX 1s K线 → Collector 收取 → Adapter 过滤聚合为 5s bar →
Publisher 攒齐所有 symbol 为一轮 → bar_queue → 推理线程把 bar 喂给
C++ InferenceEngine 并行计算 60 个因子 → signal_deque → 主线程随时读取最新因子值.
