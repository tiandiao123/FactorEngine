# OKX Trades WebSocket 吞吐量基准测试报告

> 日期: 2026-05-03
> 环境: Linux (Ubuntu), Python 3.x, aiohttp
> 数据源: OKX 公共 WS (`wss://ws.okx.com:8443/ws/v5/public`), `trades` 频道

---

## 1. 测试目的

验证 Python 流式处理 OKX trades 数据的可行性, 评估以下指标:

- 单品种 / 多品种的 trades 吞吐量 (trades/sec)
- `json.loads` 解析延迟
- 峰值与常态的差异
- 外推 300 symbols 场景下是否需要 C++ 聚合模块

---

## 2. 测试工具

```bash
# BTC 单品种, 30 秒
python -m tests.trades.websocket.bench_trades_throughput --duration 30

# 10 个主流品种, 30 秒
python -m tests.trades.websocket.bench_trades_throughput --duration 30 \
    --symbols BTC-USDT-SWAP ETH-USDT-SWAP SOL-USDT-SWAP \
              DOGE-USDT-SWAP XRP-USDT-SWAP ADA-USDT-SWAP \
              AVAX-USDT-SWAP LINK-USDT-SWAP DOT-USDT-SWAP \
              MATIC-USDT-SWAP
```

---

## 3. 测试结果

### 3.1 BTC 单品种 (30s)

| 指标 | 值 |
|------|-----|
| 平均 trades/sec | **10.9** |
| 中位数 (p50) | 4 |
| 峰值 (max) | 50 |
| 最小值 (min) | 1 |
| WS 消息数 | 329 |
| json.loads 平均耗时 | 17.1 μs/msg |

> 备注: 测试时段为非极端行情, BTC 流动性正常。

### 3.2 10 个主流品种 (30s)

| 品种 | avg/s | p50 | peak | min | 采样秒数 |
|------|-------|-----|------|-----|---------|
| BTC-USDT-SWAP | 41.6 | 6 | **650** | 1 | 31 |
| ETH-USDT-SWAP | 43.7 | 18 | **375** | 2 | 31 |
| SOL-USDT-SWAP | 3.8 | 2 | 28 | 1 | 22 |
| DOGE-USDT-SWAP | 3.3 | 2 | 13 | 1 | 26 |
| XRP-USDT-SWAP | 3.8 | 2 | 14 | 1 | 12 |
| ADA-USDT-SWAP | 3.4 | 1 | 22 | 1 | 10 |
| AVAX-USDT-SWAP | 2.9 | 1 | 13 | 1 | 12 |
| LINK-USDT-SWAP | 2.4 | 1 | 12 | 1 | 15 |
| DOT-USDT-SWAP | 6.0 | 1 | 21 | 1 | 4 |
| MATIC-USDT-SWAP | — | — | — | — | 0 (无数据) |

**汇总:**

| 指标 | 值 |
|------|-----|
| WS 消息总数 | 2,999 |
| trades 总数 | 2,989 |
| 合计吞吐 | **~100 trades/s** |
| json.loads 平均耗时 | 12.7 μs/msg |
| msg 吞吐 | 100 msg/s |

---

## 4. 关键发现

### 4.1 常态负载远低于理论峰值

之前设计文档中估算 BTC 单品种高波动时可达 200-500 笔/s, 但实测常态仅 **~11 笔/s**。
10 个品种合计 **~100 trades/s**, 说明大多数时间负载非常轻。

### 4.2 BTC 突发 peak = 650

某一秒内出现 650 笔成交 (可能是大单拆分或瞬间高波动), 这是实测到的单秒峰值。
即使这种突发, 对 Python 而言也只是一次性解析 650 条 JSON, 耗时约 8ms, 无压力。

### 4.3 山寨币流动性极低

除 BTC/ETH 外, 其余品种平均只有 2-6 trades/sec, 中位数多为 1-2。
这意味着 300 symbols 中大量山寨币几乎不贡献负载。

### 4.4 json.loads 性能充裕

实测 12-17 μs/msg, Python `json.loads` 极限约 50K-100K msg/s。
当前负载仅用掉单线程能力的 **~0.1%**。

---

## 5. 300 symbols 外推

基于实测数据进行线性外推:

| 假设 | 估算 trades/s | json.loads CPU | Accumulator CPU |
|------|-------------|---------------|-----------------|
| 常态 (非高波动) | ~500-1,000 | 6-12 ms/s | < 0.3 ms/s |
| 中等波动 | ~3,000-5,000 | 36-60 ms/s | < 1.5 ms/s |
| 极端行情 (全市场暴动) | ~10,000-30,000 | 120-360 ms/s | < 9 ms/s |
| Python 单线程天花板 | ~50,000-100,000 | 1,000 ms/s | — |

**常态下余量 50-100x, 极端行情下余量仍有 1.5-5x。**

---

## 6. 结论

### Python 流式处理完全可行

- **300 symbols 常态**: 仅消耗单线程 ~1% CPU → **无压力**
- **极端峰值**: 消耗 ~12-36% CPU → **安全范围**
- **`TradesAccumulator` O(1) 更新**: 即使 30K trades/s, 累加器 CPU < 10ms/s → **可忽略**

### 不需要 C++ 聚合模块

当前设计的两个关键选择保证了性能:

1. **流式增量累加**: `accumulator.update()` 每笔 trade 只做几次浮点加法, O(1)
2. **桶边界才触发推理**: 不是每笔 trade 都 push_bars, 而是每个时间桶 (1s/5s/60s) 才推一次

这两层把高频负载 (trades) 和重计算 (因子推理) 完全解耦。

### 何时需要 C++

| 场景 | 预估 trades/s | 是否需要 C++ |
|------|-------------|-------------|
| 300 symbols 常态 | ~1,000 | 不需要 |
| 300 symbols 极端 | ~30,000 | 不需要 (换 orjson 更安全) |
| 1000+ symbols | ~10,000-100,000 | 可能需要 |
| tick-by-tick 因子推理 | 每笔都 push | 需要 |

---

## 7. 优化备选方案 (未来)

如果将来逼近 Python 天花板, 可按以下顺序逐步优化:

1. **`orjson` 替换 `json`**: 零成本, 提速 3-5x, 天花板推到 200K+ msg/s
2. **多进程 collector**: 按品种分组到不同进程, 线性扩展
3. **Rust/C++ collector**: 将 WS 接收 + JSON 解析下沉到 native, Python 只负责桶读取
4. **C++ accumulator**: 通过 pybind11 暴露, 消除 GIL 竞争

当前阶段 (Phase 1/2) **不需要任何优化**, Python 原生方案即可。
