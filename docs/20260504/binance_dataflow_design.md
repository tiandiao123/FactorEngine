# Binance Dataflow 兼容设计

> 日期：2026-05-04
> 分支：feature/binance_dataflow
> 目标：在不改动 ClockDriver / Engine / InferenceEngine 核心的前提下，接入 Binance Futures (USDT-M) 的 kline + trades 数据流

---

## 1. 现有架构回顾

```
                     ┌──────────────┐
                     │   Engine     │
                     │ _init_live() │
                     └──────┬───────┘
                            │ creates
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
      ┌──────────────┐ ┌──────────┐ ┌──────────────┐
      │ KlineWorker  │ │ Trades   │ │ ClockDriver  │
      │ (OKX)       │ │ Worker   │ │ (通用)       │
      │ flush()      │ │ flush()  │ │ register()   │
      └──────┬───────┘ └────┬─────┘ └──────┬───────┘
             │              │              │
      OKXKlineCollector  OKXTradesCollector │
      KlineAdapter       TradesAccumulator │
      KlinePublisher                       │
             │              │              │
             └──────────────┴──────────────┘
                            │ _flush_and_aggregate()
                            ▼
                      ┌──────────┐
                      │ bar_queue │
                      └────┬─────┘
                           ▼
                    InferenceEngine
```

### 交易所特定 vs 通用

| 层级 | OKX 特定 | 通用（可复用） |
|------|---------|--------------|
| WS 连接 | `OKXKlineCollector`, `OKXTradesCollector` | — |
| 消息解析 | OKX JSON 格式、candle array、confirm flag | — |
| Symbol | `BTC-USDT-SWAP` | — |
| URL/Channel | `OKX_WS_PUBLIC/BUSINESS`, `candle1s` | — |
| 数据聚合 | `KlineAdapter` (OKX candle→BarPoint) | `TradesAccumulator` (通用 trade tuple) |
| 轮次批处理 | — | `KlinePublisher` (exchange-agnostic round batching) |
| 统一调度 | — | `ClockDriver`, `FlushableWorker` protocol |
| 引擎 | — | `Engine`, `_runtime_loop`, `InferenceEngine` |

---

## 2. Binance Futures API 关键差异

### 2.1 WebSocket

| 项目 | OKX | Binance Futures |
|------|-----|-----------------|
| URL | `wss://ws.okx.com:8443/ws/v5/public` (trades) / `business` (kline) | `wss://fstream.binance.com/stream?streams=...` |
| 订阅格式 | `{"op":"subscribe","args":[{"channel":"trades","instId":"BTC-USDT-SWAP"}]}` | `{"method":"SUBSCRIBE","params":["btcusdt@aggTrade"],"id":1}` |
| Symbol | `BTC-USDT-SWAP` | `btcusdt` (小写, 无分隔符) |
| 最大流数 | ~200/连接 | 1024/连接 |

### 2.2 Kline

| 项目 | OKX | Binance Futures |
|------|-----|-----------------|
| **1s kline** | ✅ 支持 (`candle1s`) | ❌ **不支持** (最短 1m) |
| 数据格式 | Array: `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]` | Object: `k.t,k.o,k.h,k.l,k.c,k.v,k.q,k.x,...` |
| 确认标志 | `confirm == "1"` (string) | `k.x == true` (boolean) |
| 推送频率 | 每秒 1 次 | 250ms 1 次 (in-progress updates) |
| Volume 单位 | 张 (contracts, 需 ctVal 换算) | base asset (BTC 等, 无需换算) |

### 2.3 Trades

| 项目 | OKX | Binance Futures |
|------|-----|-----------------|
| Channel | `trades` (逐笔) | `aggTrade` (100ms 聚合) |
| 买卖方向 | 显式 `side: "buy"/"sell"` | `m: true/false` (is_buyer_maker) |
| 方向映射 | 直接使用 | `side = "sell" if m else "buy"` |
| Price/Qty | string | string |
| 字段名 | `px, sz, side, ts` | `p, q, m, T` |

### 2.4 关键限制

**Binance Futures 不支持 1s kline！** 这意味着：
- `data_source="kline"` 模式在 Binance 上最短只能用 1m
- 对于 1s~59s 频率，**必须**使用 `data_source="trades"` 或 `data_source="kline_trades"`
- 这正好验证了我们 `TradesAccumulator` 从 trades 构建 OHLCV 的设计价值

---

## 3. 设计方案

### 3.1 目录结构

```
dataflow/
├── __init__.py
├── cache.py                    # 通用
├── clock_driver.py             # 通用
├── okx/                        # OKX 特定
│   ├── common.py               # OKX URL/channel
│   ├── symbols.py
│   ├── kline/
│   │   ├── collector.py        # OKXKlineCollector
│   │   ├── adapter.py          # KlineAdapter (OKX 格式)
│   │   ├── publisher.py        # KlinePublisher (可复用)
│   │   └── worker.py           # KlineWorker (可复用)
│   └── trades/
│       ├── collector.py        # OKXTradesCollector
│       ├── accumulator.py      # TradesAccumulator (通用)
│       └── worker.py           # TradesWorker (可复用)
├── binance/                    # 新增 Binance 特定
│   ├── __init__.py
│   ├── common.py               # Binance URL/channel/symbol 映射
│   ├── symbols.py              # Binance symbol 列表
│   ├── kline/
│   │   ├── __init__.py
│   │   ├── collector.py        # BinanceKlineCollector
│   │   ├── adapter.py          # BinanceKlineAdapter
│   │   ├── publisher.py        # 复用 okx/kline/publisher.py → 提升到通用层
│   │   └── worker.py           # BinanceKlineWorker
│   └── trades/
│       ├── __init__.py
│       ├── collector.py        # BinanceTradesCollector
│       └── worker.py           # BinanceTradesWorker (复用 TradesAccumulator)
└── simulation/
    └── ...
```

### 3.2 重构策略：提取通用组件

以下组件当前在 `okx/` 下但实际是 exchange-agnostic，应提升到 `dataflow/` 通用层：

```
dataflow/
├── kline/
│   ├── publisher.py            # KlinePublisher (round batching, forward-fill)
│   └── base_worker.py          # BaseKlineWorker (flush protocol)
├── trades/
│   ├── accumulator.py          # TradesAccumulator (通用 OHLCV 聚合)
│   └── base_worker.py          # BaseTradesWorker (flush protocol)
```

**但为了最小化改动**，Phase 1 先不移动文件，Binance worker 直接 import okx 的通用组件：

```python
# dataflow/binance/kline/worker.py
from dataflow.okx.kline.publisher import KlinePublisher  # 复用
from dataflow.binance.kline.collector import BinanceKlineCollector
from dataflow.binance.kline.adapter import BinanceKlineAdapter
```

Phase 2 再统一提取到通用层。

### 3.3 核心新增类

#### BinanceKlineCollector

```python
class BinanceKlineCollector:
    """Binance Futures kline WS collector.
    
    连接 wss://fstream.binance.com/stream?streams=...
    订阅 <symbol>@kline_<interval>
    """
    
    BINANCE_FSTREAM_URL = "wss://fstream.binance.com/stream"
    
    def __init__(self, symbols: list[str], interval: str = "1m",
                 on_candle: Callable | None = None):
        self.symbols = symbols          # ["btcusdt", "ethusdt", ...]
        self.interval = interval        # "1m", "5m", etc.
        self.on_candle = on_candle      # callback(symbol, bar_array)
    
    async def run(self):
        # 构建 combined stream URL
        streams = [f"{s}@kline_{self.interval}" for s in self.symbols]
        url = f"{self.BINANCE_FSTREAM_URL}?streams={'/'.join(streams)}"
        # 或使用动态订阅:
        # {"method": "SUBSCRIBE", "params": streams, "id": 1}
        
    def _dispatch(self, msg: dict):
        # combined stream wrapper: {"stream": "btcusdt@kline_1m", "data": {...}}
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        if data.get("e") != "kline":
            return
        symbol = data["s"]  # "BTCUSDT"
        k = data["k"]
        # 转换为统一格式调用 on_candle
        self.on_candle(symbol, k)
```

#### BinanceKlineAdapter

```python
class BinanceKlineAdapter:
    """将 Binance kline JSON 转换为 BarPoint ndarray(8,)."""
    
    def parse(self, symbol: str, k: dict) -> np.ndarray | None:
        if not k.get("x", False):  # 只处理已确认的 kline
            return None
        return np.array([
            k["t"],            # ts (ms)
            float(k["o"]),     # open
            float(k["h"]),     # high
            float(k["l"]),     # low
            float(k["c"]),     # close
            float(k["v"]),     # volume (base asset, 无需换算)
            float(k["q"]),     # quote volume (USDT)
            0.0,               # placeholder (OKX 有 volCcyQuote)
        ], dtype=np.float64)
```

#### BinanceTradesCollector

```python
class BinanceTradesCollector:
    """Binance Futures aggTrade WS collector.
    
    连接 wss://fstream.binance.com/stream?streams=...
    订阅 <symbol>@aggTrade
    """
    
    def __init__(self, symbols: list[str],
                 on_trade: Callable | None = None):
        self.symbols = symbols
        self.on_trade = on_trade  # callback(symbol, px, sz, side, ts_ms)
    
    def _dispatch(self, msg: dict):
        data = msg.get("data", {})
        if data.get("e") != "aggTrade":
            return
        symbol = data["s"]       # "BTCUSDT"
        px = float(data["p"])    # price
        sz = float(data["q"])    # quantity (base asset)
        ts_ms = int(data["T"])   # trade time
        # Binance: m=true → seller is taker → sell
        side = "sell" if data["m"] else "buy"
        self.on_trade(symbol, px, sz, side, ts_ms)
```

### 3.4 Symbol 映射

需要一个双向映射，因为 Engine 用户传入的是统一格式，WS 需要交易所格式：

```python
# dataflow/binance/common.py

def to_binance_symbol(unified: str) -> str:
    """BTC-USDT-SWAP → btcusdt, ETH-USDT → ethusdt"""
    parts = unified.upper().replace("-SWAP", "").split("-")
    return "".join(parts).lower()

def from_binance_symbol(binance: str) -> str:
    """btcusdt → BTC-USDT (Binance Futures 默认是永续)
    BTCUSDT → BTC-USDT"""
    # 简单实现：去掉 USDT 后缀
    base = binance.upper().replace("USDT", "")
    return f"{base}-USDT"

# Binance Futures 不同频率对应
BINANCE_KLINE_INTERVALS = {
    60: "1m", 180: "3m", 300: "5m", 900: "15m",
    1800: "30m", 3600: "1h", 7200: "2h", 14400: "4h",
}
```

### 3.5 Engine 集成

Engine 新增 `exchange` 参数：

```python
class Engine:
    def __init__(
        self,
        symbols: list[str],
        mode: str = "simulation",
        exchange: str = "okx",          # 新增: "okx" | "binance"
        data_source: str = "kline",
        data_freq: str = "1s",
        ...
    ):
        self.exchange = exchange
        # 验证: Binance 不支持 1s kline
        if exchange == "binance" and data_source in ("kline", "kline_trades"):
            freq_sec = parse_freq(data_freq)
            if freq_sec < 60:
                raise ValueError(
                    f"Binance Futures does not support kline below 1m. "
                    f"Use data_source='trades' for {data_freq} frequency."
                )
```

`_init_live()` 根据 `exchange` 选择 Worker：

```python
def _init_live(self, ...):
    if self.exchange == "okx":
        if need_kline:
            self._kline_worker = OKXKlineWorker(...)
        if need_trades:
            self._trades_worker = OKXTradesWorker(...)
    elif self.exchange == "binance":
        if need_kline:
            self._kline_worker = BinanceKlineWorker(...)
        if need_trades:
            self._trades_worker = BinanceTradesWorker(...)
    
    # ClockDriver 完全不变 — 只关心 FlushableWorker.flush()
    if self._bar_queue is not None:
        self._clock_driver = ClockDriver(freq_seconds, self._bar_queue)
        if self._kline_worker:
            self._clock_driver.register("kline", self._kline_worker)
        if self._trades_worker:
            self._clock_driver.register("trades", self._trades_worker)
```

---

## 4. 数据流对比

### OKX kline 模式 (data_source="kline", freq=1s)

```
OKX WS (candle1s) → OKXKlineCollector._on_candle()
  → KlineAdapter.parse() [confirm=="1" filter]
  → KlinePublisher.publish()
  → ClockDriver tick → KlineWorker.flush()
  → bar_queue → InferenceEngine
```

### Binance trades 模式 (data_source="trades", freq=1s)

```
Binance WS (aggTrade) → BinanceTradesCollector._on_trade()
  → TradesAccumulator.update(px, sz, side, ts)  [复用]
  → ClockDriver tick → BinanceTradesWorker.flush()
    → accumulator.snapshot_and_reset()
    → 构建 BarPoint + trades dict
  → bar_queue → InferenceEngine
```

### Binance kline_trades 模式 (data_source="kline_trades", freq=1m)

```
Binance WS (kline_1m) → BinanceKlineCollector
  → BinanceKlineAdapter [k.x==true filter]
  → KlinePublisher (复用)
  → BinanceKlineWorker.flush()

Binance WS (aggTrade) → BinanceTradesCollector
  → TradesAccumulator (复用)
  → BinanceTradesWorker.flush()

ClockDriver tick → aggregate(kline + trades) → bar_queue
```

---

## 5. 实施计划

### Phase 1: Binance Trades 数据流 ✅ 已完成

| 步骤 | 工作 | 状态 |
|------|------|------|
| 1-1 | `dataflow/binance/common.py` — URL、symbol 映射、interval 映射 | ✅ |
| 1-2 | `dataflow/binance/trades/collector.py` — BinanceTradesCollector (aggTrade WS) | ✅ |
| 1-3 | `dataflow/binance/trades/worker.py` — BinanceTradesWorker (复用 TradesAccumulator) | ✅ |
| 1-4 | Engine 添加 `exchange` + `testnet` 参数 + Binance worker 路径 | ✅ |
| 1-5 | 37 tests: WS parsing + worker flush + engine integration | ✅ |
| 1-6 | `examples/engine/engine_rt_binance_trades.py` (testnet verified) | ✅ |

**发现**: 生产 Binance WS 从此服务器 IP 被限制，已加入 testnet 支持。

### Phase 2: Binance Kline 数据流 (1m+) ✅ 已完成

| 步骤 | 工作 | 状态 |
|------|------|------|
| 2-1 | `dataflow/binance/kline/collector.py` — BinanceKlineCollector (inherits BaseCollector) | ✅ |
| 2-2 | `dataflow/binance/kline/adapter.py` — BinanceKlineAdapter (k.x filter → BarPoint) | ✅ |
| 2-3 | `dataflow/binance/kline/worker.py` — BinanceKlineWorker (reuses KlinePublisher) | ✅ |
| 2-4 | Engine kline + kline_trades path for Binance | ✅ |
| 2-5 | 28 additional tests (65 total Binance tests) | ✅ |

### Phase 3: 通用层重构 ✅ 已完成

| 步骤 | 工作 | 状态 |
|------|------|------|
| 3-1 | `dataflow/kline/publisher.py` — KlinePublisher 提升到通用层 (OKX re-export shim) | ✅ |
| 3-2 | `dataflow/trades/accumulator.py` — TradesAccumulator 提升到通用层 (OKX re-export shim) | ✅ |
| 3-3 | `dataflow/base_collector.py` — BaseCollector ABC (auto-reconnect loop) | ✅ |
| 3-4 | OKX + Binance 全部 4 个 Collector 继承 BaseCollector | ✅ |

**测试**: 全量 1080 tests 通过，0 failures。

---

## 6. Binance Symbol 列表 (热门永续)

```python
# dataflow/binance/symbols.py

BINANCE_PERP_SYMBOLS = [
    "btcusdt", "ethusdt", "solusdt", "bnbusdt", "dogeusdt",
    "xrpusdt", "adausdt", "avaxusdt", "linkusdt", "dotusdt",
    "maticusdt", "uniusdt", "atomusdt", "filusdt", "ltcusdt",
    "nearusdt", "aptusdt", "arbusdt", "opusdt", "suiusdt",
    "pepeusdt", "wifusdt", "seiusdt", "tiausdt", "injusdt",
    "fetusdt", "renderusdt", "jupusdt", "wldusdt", "ondousdt",
]

# 统一格式 (用于 Engine)
DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "DOGE-USDT",
    "XRP-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT",
    ...
]
```

---

## 7. 注意事项

### 7.1 Binance aggTrade 是聚合的

OKX trades 是逐笔 (每笔独立推送)，Binance aggTrade 在同一 100ms 窗口内聚合同价同方向的交易。这意味着：
- Binance 的 `trade_count` 实际是 aggTrade count，可能少于真实笔数
- 不影响 OHLCV/vwap/buy_vol/sell_vol 的正确性（价格和量是准确的）

### 7.2 Volume 单位差异

- OKX: `sz` 是合约张数（需要 `ctVal` 换算成 base asset）
- Binance: `q` 直接是 base asset 数量（如 BTC）

当前 `TradesAccumulator.update(px, sz, side, ts_ms)` 的 `sz` 参数不关心单位，只做加法聚合。所以 **无需修改 TradesAccumulator**，但要注意最终 volume 的单位含义不同。

### 7.3 连接稳定性

Binance WS 有 24 小时自动断开机制，需要在 Collector 中实现自动重连逻辑。OKX 也有类似问题但当前代码中可能没有完善处理。建议在 Phase 1 就加入基础重连。

### 7.4 kline_trades 模式在 Binance 的限制

当 `data_source="kline_trades"` + Binance 时：
- Kline 最短 1m，但 trades aggregation 可以更快 (1s)
- ClockDriver freq 必须 ≥ 60s（受 kline 限制）
- 或者 ClockDriver freq=1s 但 kline worker 只在每分钟有新数据时返回 non-None

建议：Binance 高频场景统一用 `data_source="trades"`。

---

## 8. 不做的事情 (本期)

| 内容 | 原因 |
|------|------|
| Binance Spot | 架构一样但 URL/symbol 不同，后续扩展 |
| OrderBook 数据 | 需要新的 Worker 类型，独立 feature |
| Funding Rate 数据 | REST API，不是 WS stream，独立 feature |
| 多交易所同时运行 | 一个 Engine 实例只连一个交易所 |
| Symbol 自动发现 | 手动配置 symbols 列表即可 |
