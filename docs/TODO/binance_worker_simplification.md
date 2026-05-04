# TODO: Binance Worker 简化设计

> 日期：2026-05-04
> 优先级：中（Binance 接入时实施）
> 关联：`dataflow/okx/kline/` 四组件链路

---

## 现状：OKX Kline 链路过长

当前 OKX kline 数据流经过 4 个组件串联：

```
OKXKlineCollector → KlineAdapter → KlinePublisher → KlineWorker.flush()
     WS 生命周期       过滤+聚合      round 凑批        对外接口
```

这种拆分是 **OKX API 特性决定的**：

| OKX 特性 | 导致的组件 |
|----------|-----------|
| candle 逐条推送，需要管理 WS 连接/重连 | Collector |
| 有 `confirm` 字段，未确认的 candle 需要过滤 | Adapter |
| 没有 5s/10s 原生频道，需要从 1s 本地聚合 | Adapter |
| 不同 symbol 的 candle 异步到达，需要等凑齐 | Publisher |

这些"被迫"的拆分让链路变得复杂，理解成本高。

---

## 目标：Binance 接入时合并组件

如果 Binance 的 API 设计与 OKX 不同（例如批量推送、无 confirm、直接提供多种频率），可以大幅简化：

### 理想架构

```
BinanceKlineWorker.flush() → dict[str, ndarray]
  │
  └── 内部：WS 连接 + 直接构建 symbol→bar dict
      不需要 Adapter（无 confirm、无本地聚合）
      不需要 Publisher（批量推送不需要凑批）
```

### 设计约束

唯一的硬性约束是 **实现 `FlushableWorker` 协议**：

```python
class BinanceKlineWorker:
    def flush(self) -> dict[str, np.ndarray] | None:
        """ClockDriver 调用，返回 {symbol: BarPoint ndarray(8,)}"""
        ...

    def start(self): ...
    def stop(self): ...
```

ClockDriver 不关心 Worker 内部有几层组件，只调 `flush()`。

### 可能的简化点

| 维度 | OKX（现状） | Binance（预期） |
|------|-----------|----------------|
| confirm 过滤 | 需要 Adapter | 不需要（Binance kline 直接是最终值） |
| 本地聚合 | Adapter 聚合 1s→5s/10s | 不需要（Binance 有 `kline_5s` 等原生频道）|
| round 凑批 | Publisher 等所有 symbol 到齐 | 可能不需要（如果用 `!kline@arr` 批量频道） |
| WS 端点 | Business WS | 普通 WS |

### 如果 Binance 也是逐条推送

即使 Binance 也逐条推送，仍可以简化为 2 层：

```
BinanceKlineCollector → BinanceKlineWorker
    WS + 解析              攒 dict + flush
```

把 Adapter 和 Publisher 的逻辑合入 Worker 内部，因为没有 `confirm` 过滤和本地聚合的需求，代码量会少很多。

---

## Trades Worker 同理

OKX 的 TradesWorker 已经是比较简洁的 2 层结构（Collector → Accumulator），Binance 版本可以保持同样的设计，只替换 Collector：

```
BinanceTradesCollector → TradesAccumulator (复用)
```

`TradesAccumulator` 的聚合逻辑与交易所无关，可以直接复用。

---

## 实施检查清单

- [ ] 调研 Binance WS API（kline 推送模式、频道列表、数据格式）
- [ ] 确认是否有 confirm 机制、是否支持批量推送
- [ ] 设计 `BinanceKlineWorker`（目标：单文件 <200 行）
- [ ] 设计 `BinanceTradesCollector`（复用 `TradesAccumulator`）
- [ ] 注册到 ClockDriver，验证 `flush()` 协议兼容
- [ ] 在 `Engine` 中添加 `exchange` 参数（`"okx"` / `"binance"`）

---

## 一句话版本

**OKX 的 4 组件链路是被 API 特性逼出来的，Binance 接入时应根据其 API 特点大胆简化，只要满足 `FlushableWorker.flush()` 协议即可。**
