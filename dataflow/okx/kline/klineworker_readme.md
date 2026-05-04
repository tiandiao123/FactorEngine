# KlineWorker 工作流

## 概览

KlineWorker 是 OKX K 线数据的采集和预处理模块，运行在一个独立的 daemon 线程中。它负责从 OKX WebSocket 接收原始 candle 数据，经过过滤、聚合、批次组装后，将完整的多 symbol 数据 round 缓存在内部，等待 ClockDriver 来拉取。

**KlineWorker 不知道 trades、orderbook 等其他数据源的存在，也不直接推送数据到 `bar_queue`。**

## 架构位置

```
Engine._init_live()
  │
  ├── KlineWorker ◄── 本文档
  │     └── flush() ──→ ClockDriver
  │
  ├── TradesWorker
  │     └── flush() ──→ ClockDriver
  │
  └── ClockDriver
        └── _aggregate() → bar_queue → factor-infer 线程
```

## 内部组件

KlineWorker 内部串联了三个组件，数据单向流动：

```
OKX WebSocket
    │
    v
┌─────────────────────────────────────────────────┐
│  OKXKlineCollector  (collector.py)               │
│  - 连接 OKX Business WebSocket                   │
│  - 订阅 candle 频道 (candle1s / candle1m / ...)  │
│  - JSON 解析，触发 on_candle(symbol, raw) 回调    │
│  - 自动重连                                      │
└──────────────────────┬──────────────────────────┘
                       │ on_candle(symbol, raw)
                       v
┌─────────────────────────────────────────────────┐
│  KlineAdapter  (adapter.py)                      │
│  - 过滤未确认 candle (confirm != "1")             │
│  - freq >= 60s: 直通，返回 BarPoint              │
│  - freq < 60s:  聚合 N 根 1s candle 为 1 根 bar  │
│  - 时间窗口对齐: ts % (freq * 1000) == 0         │
└──────────────────────┬──────────────────────────┘
                       │ BarPoint ndarray(8,)
                       v
┌─────────────────────────────────────────────────┐
│  KlinePublisher  (publisher.py)                  │
│  - 按 round 收集所有 symbol 的 bar               │
│  - 去重 (同一 ts 的重复 bar)                      │
│  - 时间 gap 检测和告警                            │
│  - 超时后 forward-fill 缺失 symbol               │
│  - 完成的 round 放入 _ready_round 缓冲区         │
│  - 暴露 flush_for_clock() 给 ClockDriver         │
└─────────────────────────────────────────────────┘
```

## 数据格式

每一步处理的数据格式：

| 阶段 | 格式 | 示例 |
|------|------|------|
| OKX 原始 | `list[str]` 长度 9 | `["1714704000000", "67000.0", "67100.0", "66900.0", "67050.0", "10.5", "703025.0", "703025.0", "1"]` |
| Adapter 输出 | `np.ndarray(8,)` float64 | `[ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]` |
| Publisher round | `dict[str, np.ndarray]` | `{"BTC-USDT-SWAP": ndarray(8,), "ETH-USDT-SWAP": ndarray(8,)}` |

## 文件说明

### `collector.py` — OKXKlineCollector

WebSocket 客户端，职责单一：

- 连接 `wss://ws.okx.com:8443/ws/v5/business`
- 按 `channel` 订阅（如 `candle1s`），每个连接最多 200 个 symbol
- 收到 JSON 推送后解析，对每条 candle 调用 `on_candle(symbol, raw)`
- 自动处理断线重连

Collector 不做任何数据过滤或转换，这些都交给下游。

### `adapter.py` — KlineAdapter

数据过滤和聚合，是 Collector 和 Publisher 之间的桥梁：

**直通模式** (`freq >= 60s` 或 `freq == 1s`)：
- 只做一件事：过滤掉 `confirm != "1"` 的未确认 candle
- 确认后的 candle 直接转为 `ndarray(8,)` 返回

**聚合模式** (`1s < freq < 60s`，如 5s, 10s, 30s)：
- OKX 没有 5s/10s 等频道，所以订阅 `candle1s`，本地聚合
- 按时间窗口缓冲连续的 1s candle
- 窗口边界对齐到 `ts % (freq * 1000) == 0`（保证所有 symbol 同一窗口）
- 收满 N 根后合并：`open=第一根, high=max, low=min, close=最后一根, vol=sum`
- 窗口不完整或有 gap → 丢弃，重新开始（数据质量优先）

### `publisher.py` — KlinePublisher

多 symbol 批次组装，是 Worker 对外暴露数据的出口：

**核心问题**：不同 symbol 的 bar 不会同时到达，但下游需要"所有 symbol 的一整轮数据"。

**解决方案**：round batching — 收集所有 symbol 的 bar 到一个 pending round，通过以下条件判断何时完成：

1. **全部到齐** — 所有 expected symbol 都有 bar → 立即完成
2. **超时** — 等了 `batch_timeout_s` 后仍有 symbol 缺失 → forward-fill（用上一轮的 bar 填充）
3. **时间戳跳变** — 新 bar 的 ts 跟 pending round 不同 → 上一轮强制完成

**第一轮特殊处理**：没有历史数据可以 forward-fill，所以必须等所有 symbol 到齐。

**对外接口** — `flush_for_clock()`，被 ClockDriver 线程调用，三条路径：
1. 有已完成的 `_ready_round` → 直接返回（正常快速路径）
2. 有部分到达的 `_pending_round` → 组装 + forward-fill 返回
3. 什么都没有 → 返回上一轮的完整拷贝（全量 forward-fill，极少发生）

### `worker.py` — KlineWorker

编排上述三个组件的胶水层：

- `start()` → 创建 daemon 线程，启动 asyncio event loop
- event loop 中运行 `OKXKlineCollector.run()`
- 每收到一条 candle → `_on_candle()` → `adapter.on_candle()` → `publisher.publish()`
- `flush()` → 委托给 `publisher.flush_for_clock()`，供 ClockDriver 调用
- `stop()` → 停止 collector、取消 asyncio tasks、join 线程

## 线程模型

```
okx-kline 线程 (daemon)
  │
  │  asyncio event loop
  │    └── OKXKlineCollector.run()
  │          └── on_candle 回调 (同步)
  │                ├── KlineAdapter.on_candle()
  │                └── KlinePublisher.publish()
  │                      └── 写入 _pending_round / _ready_round
  │
  ╎ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  │
clock-driver 线程 (每 N 秒一次)
  │
  └── kline_worker.flush()
        └── publisher.flush_for_clock()
              └── 读取 _ready_round（_ready_lock 保护）
```

两个线程共享 Publisher 的状态：
- `_ready_round`：用 `_ready_lock` 互斥锁保护
- `_pending_round` / `_last_round`：CPython GIL 保证原子读写

## 频率与 OKX 频道映射

| `data_freq` | OKX 频道 | Adapter 模式 |
|-------------|----------|-------------|
| `1s` | `candle1s` | 直通 |
| `5s` | `candle1s` | 聚合 5 根 |
| `10s` | `candle1s` | 聚合 10 根 |
| `30s` | `candle1s` | 聚合 30 根 |
| `1min` | `candle1m` | 直通 |
| `5min` | `candle5m` | 直通 |
| `1h` | `candle1H` | 直通 |

## 监控指标

通过 `worker.stats()` 获取：

| 指标 | 含义 |
|------|------|
| `adapter_bar_count` | Adapter 输出的 bar 总数 |
| `bars_received` | Publisher 收到的 bar 总数 |
| `rounds_completed` | 已完成的 round 数 |
| `forward_fill_count` | 累计 forward-fill 的 symbol 次数 |
| `dup_count` | 去重丢弃的 bar 数 |
| `gap_count` | 检测到的时间 gap 数 |

`forward_fill_count` 过高说明某些 symbol 的数据延迟严重；`gap_count` 过高说明 WebSocket 连接不稳定。
