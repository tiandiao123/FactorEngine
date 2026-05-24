# Dynamic Symbol List 设计思考

> 日期：2026-05-23
> 状态：设计草案，2026-05-24 结合当前 audit/verifier 和代码结构重新 review 后修订
> 目标：让 FactorEngine 从启动时静态 `symbols` 进化到可运行时动态调整的 symbol universe

---

## 1. 背景

当前 `Engine` 的 `symbols` 是启动时静态传入的：

```python
engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT"],
    exchange="binance",
    data_source="trades",
    factor_group="okx_perp_trades",
)
engine.start()
```

这适合固定小篮子策略，但不适合以下场景：

1. 全市场扫描：需要周期性拉取交易所全标的。
2. 动态 universe：上游服务会根据成交额、流动性、上线/下线、风控黑名单维护一个可交易 symbol list。
3. 策略滚动选币：例如每 5 分钟只保留成交额前 N 个合约。
4. 交易所标的变更：新币上线、合约下线、临时维护，需要运行时处理。

因此需要一个标准：**Engine 不应该只接受一个启动时列表，而应该支持一个可版本化、可增量同步、可安全切换的 symbol universe。**

---

## 2. 当前限制

### 2.1 Engine 初始化绑定 symbols

`factorengine.Engine` 在 `__init__` 中直接使用 `symbols` 初始化：

- dataflow worker
- C++ `InferenceEngine`
- factor graph for each symbol
- debug cache
- simulation manager

这意味着 symbol 集合在启动后基本不可变。

### 2.2 Worker 订阅生命周期固定

OKX / Binance worker 当前假设构造后 symbol list 固定：

```text
Engine.__init__
  -> KlineWorker(symbols=...)
  -> TradesWorker(symbols=...)
  -> worker.start()
       -> collector connects and subscribes symbols
```

如果运行时新增 symbol，目前没有统一的：

- subscribe
- unsubscribe
- accumulator 初始化
- publisher/cache 初始化
- reconnect 后恢复订阅

### 2.3 C++ runtime 缺少 remove/reset 语义

`fe_runtime.InferenceEngine` 当前支持：

- `add_symbol(symbol)`
- `add_factor(symbol, factor_id, graph)`
- `push_bars({symbol: BarData})`
- `get_all_outputs()`

动态新增 symbol 相对容易；动态删除 symbol 需要明确语义：

- 是从 runtime 完全删除？
- 还是标记为 inactive，保留历史状态？
- 输出接口是否还返回 inactive symbol？
- 重新加入同一个 symbol 时是否复用旧状态？

---

## 3. 设计目标

### 3.1 必须支持

1. 运行时新增 symbol。
2. 运行时移除 symbol。
3. 上游 symbol list 服务可周期性同步。
4. 全量快照和增量 diff 都可表达。
5. 变更在 ClockDriver tick 边界生效，并且不丢失刚刚结束的上一根 bar。
6. dataflow、runtime、cache、outputs 的 active symbol 集合一致。
7. reconnect 后 worker 能恢复最新 active subscriptions。
8. audit log 能解释某个时间段的 active universe，便于事后核查 missing symbol 是异常还是当时不在 universe。

### 3.2 暂不追求

1. 毫秒级 symbol 切换。
2. 在一个 bar 中途立即生效。
3. 删除 symbol 后释放所有 C++ 内存。
4. 跨进程分布式 symbol universe 协调。
5. 不停机迁移 factor_group。

第一版建议优先做 **tick-boundary eventual consistency**：上游更新后，Engine 在下一次或下几次 ClockDriver tick 安全收敛到目标 symbol set。

---

## 4. 核心原则

### 4.1 Symbol universe 是版本化快照

上游接口返回的不只是 list，而是带版本的 universe：

```json
{
  "version": "2026-05-23T10:15:00.000Z",
  "exchange": "binance",
  "symbols": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
  "ttl_seconds": 30,
  "reason": "top_volume_200"
}
```

Engine 只接受 version 单调前进的更新，避免旧响应覆盖新状态。

### 4.2 Runtime 以 active/inactive 管理，不急于物理删除

第一版建议：

- 新增 symbol：初始化 worker 状态、cache、C++ runner、factor graphs。
- 移除 symbol：从订阅中移除，标记 runtime inactive，输出层过滤掉。
- 不立即从 C++ `InferenceEngine` 物理删除，降低并发和内存安全风险。

这样可以避免在 runtime thread 正在 `push_bars()` 时删除 runner。

### 4.3 变更在 ClockDriver tick 边界提交，但不能早于 flush

不要让 collector 线程、runtime 线程、用户线程同时直接修改 symbol 集合。

推荐流程：

```text
SymbolProvider 拉取目标 universe
        │
        v
SymbolManager 计算 diff
        │
        v
pending diff
        │
        v
ClockDriver tick 边界：先 flush 当前已结束 bucket
        │
        v
aggregate + audit + enqueue
        │
        v
提交 diff，使其从下一根 bucket 生效
        │
        ├── Engine 更新 active_symbols / symbol_version
        ├── 通知 workers 切换 target symbols（不得长时间阻塞 ClockDriver）
        ├── 向 runtime thread 投递 symbol control message
        ├── cache 更新输出过滤集合
        └── payload / outputs 携带各自对应的 active_symbols
```

原因：当前 ClockDriver tick 到达时，`worker.flush()` 收集的是刚刚结束的上一根 bar。如果在 `flush()` 之前先 remove symbol，可能会丢掉上一根 bucket 中已经收集好的数据。因此第一版建议采用 **post-flush commit for next bucket**：

```text
tick T:
  flush bucket [T-freq, T)
  write audit / enqueue runtime payload
  commit pending symbol diff（只做轻量提交和通知）
  diff applies to bucket [T, T+freq)
```

注意：这里的 “commit” 是提交版本和通知各组件切换目标状态，不意味着在 ClockDriver 线程里直接执行所有重活。尤其不要在 ClockDriver 线程里直接重建 WebSocket 连接、等待 worker 线程 join，或者并发修改 C++ `InferenceEngine`。这些动作应交给各自拥有状态的线程顺序处理。

### 4.4 Worker 对外暴露统一动态订阅接口

所有 live worker 应实现同一组方法：

```python
class DynamicWorker:
    def add_symbols(self, symbols: list[str]) -> None: ...
    def remove_symbols(self, symbols: list[str]) -> None: ...
    def active_symbols(self) -> set[str]: ...
    def flush(self): ...
```

ClockDriver 不需要知道 worker 内部是 OKX、Binance、kline 还是 trades。

---

## 5. 建议新增组件

### 5.1 SymbolProvider

负责获取目标 symbol universe。

```python
class SymbolUniverse:
    version: str
    exchange: str
    symbols: list[str]
    ttl_seconds: int | None
    metadata: dict


class SymbolProvider:
    def fetch(self) -> SymbolUniverse:
        ...
```

第一版可以支持三种 provider：

| Provider | 用途 |
|----------|------|
| `StaticSymbolProvider` | 兼容当前 `symbols=[...]` |
| `HttpSymbolProvider` | 对接外部动态 symbol list 接口 |
| `ExchangeSymbolProvider` | 直接拉交易所全标的，再本地过滤 |

### 5.2 SymbolManager

负责版本控制、diff 计算和提交。

```python
class SymbolDiff:
    version: str
    added: set[str]
    removed: set[str]
    unchanged: set[str]
    target: set[str]


class SymbolManager:
    def refresh(self) -> SymbolDiff | None:
        ...

    def stage(self, diff: SymbolDiff) -> None:
        ...

    def commit_pending(self) -> SymbolDiff | None:
        ...
```

职责：

1. 周期性调用 provider。
2. 校验 exchange 是否匹配。
3. 标准化 symbol 格式。
4. 去重、排序、过滤非法 symbol。
5. 计算 added / removed。
6. 保证 version 单调。
7. 把 diff 暂存到 pending，等待 tick 边界提交。

### 5.3 SymbolNormalizer

不同交易所的 symbol 格式不同：

| 内部统一格式 | OKX WS | Binance WS |
|--------------|--------|------------|
| `BTC-USDT-SWAP` | `BTC-USDT-SWAP` | 不适用 |
| `BTC-USDT` | 可用于 spot | `btcusdt` |

建议 Engine 内部始终使用统一格式，collector 内部再转换为交易所格式。

```python
class SymbolNormalizer:
    def normalize_external(self, raw: str, exchange: str) -> str:
        ...

    def to_exchange(self, symbol: str, exchange: str) -> str:
        ...
```

---

## 6. 外部 symbol list 接口标准

如果上游由另一个服务维护 symbol list，建议接口必须满足以下标准。

### 6.1 HTTP API

```http
GET /v1/symbol-universe?exchange=binance&market=perp_usdt&strategy=alpha_1
```

响应：

```json
{
  "version": "2026-05-23T10:15:00.000Z",
  "exchange": "binance",
  "market": "perp_usdt",
  "strategy": "alpha_1",
  "symbols": [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT"
  ],
  "ttl_seconds": 30,
  "generated_at": "2026-05-23T10:15:00.000Z",
  "metadata": {
    "source": "top_turnover",
    "limit": 200
  }
}
```

### 6.2 字段要求

| 字段 | 必须 | 说明 |
|------|------|------|
| `version` | 是 | 单调递增版本，建议 ISO timestamp 或递增整数 |
| `exchange` | 是 | `okx` / `binance` |
| `symbols` | 是 | 目标全量 symbol list，不是增量 |
| `ttl_seconds` | 建议 | 超时后 Engine 可降级或告警 |
| `generated_at` | 建议 | 便于排查延迟 |
| `metadata` | 可选 | 记录生成逻辑 |

### 6.3 为什么优先全量快照

第一版建议上游提供 **全量快照**，不要只提供增量事件。

原因：

1. Engine 重启后可以直接恢复目标状态。
2. 网络抖动不会丢失 remove 事件。
3. diff 可以在 Engine 内部确定性计算。
4. 接口更容易测试和回放。

增量事件可以作为未来优化，但不能替代全量快照。

---

## 7. Engine API 设计

### 7.1 向后兼容

保留当前写法：

```python
engine = Engine(symbols=["BTC-USDT", "ETH-USDT"], ...)
```

内部等价于：

```python
engine = Engine(
    symbol_provider=StaticSymbolProvider(["BTC-USDT", "ETH-USDT"]),
    ...
)
```

### 7.2 新增动态模式

```python
engine = Engine(
    exchange="binance",
    data_source="trades",
    data_freq="1s",
    factor_group="okx_perp_trades",
    symbol_provider=HttpSymbolProvider(
        url="http://symbol-service/v1/symbol-universe",
        params={
            "exchange": "binance",
            "market": "perp_usdt",
            "strategy": "alpha_1",
        },
        refresh_interval_seconds=30,
    ),
)
```

### 7.3 手动更新接口

为了测试和策略内控制，也可以提供手动接口：

```python
engine.update_symbols(["BTC-USDT", "ETH-USDT", "SOL-USDT"])
```

语义：

- 传入的是目标全量 list。
- Engine 计算 diff。
- diff 进入 pending。
- 下一次 tick 边界提交。

### 7.4 状态查询接口

```python
engine.active_symbols
engine.pending_symbol_diff
engine.symbol_version
engine.symbol_stats
```

`symbol_stats` 示例：

```python
{
    "version": "2026-05-23T10:15:00.000Z",
    "active_count": 200,
    "pending_added": 3,
    "pending_removed": 5,
    "last_refresh_ts": 1790000000000,
    "last_commit_ts": 1790000001000,
    "provider_errors": 0,
}
```

### 7.5 Runtime payload 需要带 universe version

动态 universe 后，`bar_queue` 里的 payload 不能只是一组 bars。原因是 runtime thread 可能滞后于 ClockDriver：当 Engine 已经提交了新的 active set，runtime thread 可能还在处理旧 universe 的 payload。

第一版建议把 ClockDriver payload 从 tuple 扩展为结构化对象，例如：

```python
{
    "bars": merged_bars,
    "trades": merged_trades,
    "bucket_ts_ms": bucket_ts_ms,
    "symbol_version": "2026-05-23T10:15:00.000Z",
    "active_symbols": {"BTC-USDT", "ETH-USDT", "SOL-USDT"},
}
```

这样 `_runtime_loop` 和 `get_factor_outputs()` 可以按 payload 对应的 universe version 做过滤，而不是只依赖“当前最新 active set”。这能避免队列延迟时的输出错配。

### 7.6 Runtime symbol diff 需要顺序化

动态新增 / reset symbol 不能由 ClockDriver 线程直接调用 C++ `InferenceEngine`。当前 C++ runtime 没有保护 `add_symbol()`、`add_factor()`、`push_bars()`、`get_all_outputs()` 之间的并发访问；如果 ClockDriver 在 runtime thread 正在 `push_bars()` 时修改 runner map，会引入数据竞争。

第一版建议把 runtime 相关变更放进和 bar payload 同一条控制流：

```python
{
    "type": "symbol_diff",
    "diff": diff,
    "effective_from_ts_ms": next_bucket_ts_ms,
}
```

`Engine._runtime_loop()` 顺序消费：

```text
bar payload for bucket [T-freq, T)
symbol_diff effective from T
bar payload for bucket [T, T+freq)
```

这样可以保证：

1. 旧 bucket 先按旧 universe 推理。
2. symbol add/reset 在 runtime thread 内串行执行。
3. 新 bucket 再按新 universe 推理。
4. 不需要第一版就在 C++ 层加复杂锁。

---

## 8. 各模块改造点

### 8.1 Engine

新增职责：

1. 初始化 `SymbolProvider` / `SymbolManager`。
2. 启动 symbol refresh thread 或让 ClockDriver 周期性触发 refresh。
3. 在 ClockDriver 完成当前 tick 的 `flush -> aggregate -> audit/enqueue` 后，提交 pending diff，使其从下一根 bucket 生效。
4. 只在 commit 阶段做轻量状态更新和通知；不要在 ClockDriver 线程里做耗时重连或 C++ runtime 结构修改。
5. `get_factor_outputs()` 默认只返回 active symbols。
6. 维护 `symbol_version`，并写入 runtime payload / audit records。
7. 把 runtime 相关 symbol diff 投递给 runtime thread 串行处理。

### 8.2 ClockDriver

新增一个可选 hook，但不建议叫 `before_flush`。更准确的语义是 `after_flush_commit` 或 `after_tick_commit`：

```python
ClockDriver(
    freq_seconds=1,
    bar_queue=q,
    after_flush_commit=engine.commit_pending_symbol_diff,
)
```

tick 顺序：

```text
1. worker.flush()
2. _aggregate()
3. audit.write_aggregate_batch()
4. bar_queue.put()
5. after_flush_commit()
```

这样当前 tick 对应的上一根 bucket 不会丢数据，symbol 变更从下一根 bucket 开始生效。

`after_flush_commit()` 必须是轻量 hook。它可以：

1. 从 `SymbolManager` 取出 pending diff。
2. 更新 Engine 的 active version / active set。
3. 给 worker/runtime/cache 投递切换请求。

它不应该：

1. 等待 WebSocket collector 完整停止和重连。
2. 直接调用 C++ `InferenceEngine.add_symbol()` / `reset_symbol()`。
3. 做网络请求或长时间持锁。

### 8.3 Workers

Kline / trades worker 新增：

```python
def add_symbols(self, symbols: list[str]) -> None
def remove_symbols(self, symbols: list[str]) -> None
def active_symbols(self) -> set[str]
```

TradesWorker 的新增 symbol：

1. 创建 `TradesAccumulator`。
2. 更新 worker 内部 active/target symbols。
3. 通知 collector 切换订阅。
4. reconnect 订阅列表更新。

TradesWorker 的移除 symbol：

1. 更新 worker 内部 active/target symbols。
2. 通知 collector 取消订阅或重建连接。
3. 删除或 inactive accumulator。
4. `flush()` 不再返回该 symbol。

KlineWorker 同理，需要额外处理 publisher 内部 round buffer。

实现细节：

1. `symbols` / `_accumulators` / publisher symbol 集合必须加锁保护。
2. `flush()` 应先复制 active symbols 快照，再逐个 snapshot，避免遍历时集合变化。
3. `_on_trade()` / `_on_candle()` 必须容忍已经被移除的 symbol，收到迟到消息时直接忽略。
4. worker 的动态切换接口不应长时间阻塞 ClockDriver。

### 8.4 Collectors

collector 应支持动态订阅：

```python
async def subscribe(self, symbols: list[str]) -> None: ...
async def unsubscribe(self, symbols: list[str]) -> None: ...
```

实现上可以有两种策略：

| 策略 | 优点 | 缺点 |
|------|------|------|
| WS 内动态 SUBSCRIBE/UNSUBSCRIBE | 不断连接，切换快 | 代码复杂，需处理并发发送 |
| 重建连接 | 简单可靠 | symbol 变更时短暂断流 |

第一版建议：

- Binance trades：第一版先采用 **重建连接策略**，避免改造 collector 的 ws handle / 命令队列 / 并发发送模型。
- OKX：后续再评估，第一版不做。
- WS 内动态 `SUBSCRIBE` / `UNSUBSCRIBE` 作为第二阶段优化。

当前 `BaseCollector.run()` 是启动时把 `symbols` 分组，然后每组开固定 `_ws_loop`。collector 没有保存活动 websocket handle，也没有命令队列。直接做 WS 内动态订阅需要改动更大：

1. collector 需要保存每个 connection 的 ws handle。
2. worker 需要能跨线程向 asyncio loop 投递 subscribe/unsubscribe 命令。
3. 多连接分组需要处理 symbol 从一个 connection 迁移到另一个 connection。
4. reconnect 后要恢复最新 active subscriptions。

因此第一版更稳的做法是：

```text
commit symbol diff
  -> worker 更新 target symbols / accumulators
  -> worker 自己的线程收到 rebuild request
  -> stop old collector
  -> start new collector with active symbols
```

短暂断流可以接受，因为第一版目标是 correctness，而不是毫秒级切换。

不要在 ClockDriver 线程里同步执行 `stop old collector -> join -> start new collector`。现有 worker 的 `stop()` 可能等待 event loop 退出，最坏会卡住一个或多个 tick。第一版应让 worker 持有一个 “需要重建连接” 标记或命令队列，由 worker 自己的线程在安全点完成 collector 重建。

### 8.5 InferenceEngine

第一版需要 Python 层封装 active set，不一定马上改 C++ 删除能力。

新增 symbol：

```python
self._inference.add_symbol(sym)
for group in self._factor_groups:
    for fid, graph in reg.build_group(group).items():
        self._inference.add_factor(sym, fid, graph)
```

移除 symbol：

- Python 侧 `self._active_symbols.remove(sym)`。
- `_runtime_loop` 只 push active symbols。
- `get_factor_outputs()` 只返回 active symbols。
- C++ runner 保留但不再更新、不再输出给用户。

为支持“remove 后 re-add 默认 reset state”，第一版至少建议新增 C++/pybind 的 symbol 级 reset：

```cpp
void reset_symbol(const std::string& symbol);
```

如果不加 `reset_symbol`，那么 re-add 只能复用旧 runner state，和本文档推荐的 `reset_on_readd=True` 语义不一致。

并发约束：

1. `add_symbol()` / `add_factor()` / `reset_symbol()` 必须和 `push_bars()` 串行。
2. 第一版推荐由 `_runtime_loop()` 消费 symbol control message 后执行这些操作。
3. 如果选择在其他线程执行，则必须给所有 C++ inference 访问加同一把锁，包括 `push_bars()` 和 `get_all_outputs()`。

未来再考虑 C++：

```cpp
void remove_symbol(const std::string& symbol);
void set_symbol_active(const std::string& symbol, bool active);
```

### 8.6 BarCache

新增：

```python
def add_symbol(self, symbol: str) -> None: ...
def remove_symbol(self, symbol: str) -> None: ...
```

第一版 remove 可以只从 `snapshot()` 输出中过滤，不必清理历史数组。

---

## 9. 并发模型

### 9.1 线程边界

当前系统主要线程：

```text
collector worker thread(s)
ClockDriver thread
factor-infer runtime thread
main strategy thread
```

动态 symbol list 后新增：

```text
symbol-refresh thread
```

### 9.2 推荐所有权

| 状态 | 所有者 | 修改时机 |
|------|--------|----------|
| target universe | SymbolManager | refresh thread |
| pending diff | SymbolManager | refresh thread / manual update |
| active symbols | Engine | ClockDriver post-flush commit |
| worker target symbols | Worker | ClockDriver post-flush 轻量通知 |
| worker WS subscriptions | Worker thread / collector loop | worker 自己的安全点 |
| C++ runner active set | runtime thread | 按 control message 顺序处理 |
| outputs filtering | Engine | read time |

核心原则：**refresh thread 只产生 pending diff，不直接改 worker/runtime。**

同理，ClockDriver 也不直接拥有 worker WebSocket 生命周期和 C++ runner map。ClockDriver 只提供一个确定性的提交时机；真正修改线程内状态的动作应由对应状态的 owner 线程完成。

### 9.3 两个 active set

实现时建议区分两个概念：

| 名称 | 含义 |
|------|------|
| `current_active_symbols` | Engine 当前已经提交、对下一根 bucket 生效的 active set |
| `payload_active_symbols` | 某个 runtime payload 生成时对应的 active set |

`_runtime_loop` 处理队列里的 payload 时，应优先使用 `payload_active_symbols`。`get_factor_outputs()` 对最新输出做过滤时，可以使用输出里附带的 `symbol_version` / `active_symbols`，避免当前 active set 和旧 payload 混用。

---

## 10. 删除 symbol 的语义

删除 symbol 是最容易踩坑的地方，需要提前定标准。

### 10.1 第一版建议语义

当 symbol 被移除：

1. 停止订阅该 symbol。
2. worker `flush()` 不再返回该 symbol。
3. runtime 不再 push 该 symbol。
4. `get_factor_outputs()` 不再返回该 symbol。
5. C++ 内部历史状态暂时保留。
6. 如果同一个 symbol 后续重新加入，建议默认重新初始化 factor state，而不是复用旧 state。

### 10.2 为什么重新初始化

如果 symbol 移除期间没有持续推 bars，旧 rolling window 会产生时间断层。重新加入后复用旧窗口容易让因子值看起来连续，但实际数据缺失。

因此重新加入同一个 symbol 时应有明确策略：

| 策略 | 说明 | 推荐 |
|------|------|------|
| `reset_on_readd=True` | 重新创建 factor state | 第一版推荐 |
| `keep_warm_state=True` | 保留旧窗口继续推 | 仅适合短暂 unsubscribe |
| `warmup_from_history` | 从历史 kline/trades 回填 | 未来高级模式 |

当前 C++ `InferenceEngine` 只有全局 `reset()`，没有 `reset_symbol()`。因此如果第一版坚持 `reset_on_readd=True`，需要先增加 symbol 级 reset binding；否则第一版只能临时采用 `keep_warm_state=True`，但这会和“移除期间数据断层”的真实语义不一致。

推荐：第一版增加 `reset_symbol(symbol)`，暂不做物理 remove。

---

## 11. Warmup 问题

新增 symbol 后，rolling factor 需要窗口长度达到要求才有稳定输出。例如 `MA(120)` 至少需要 120 个 bar。

第一版可以接受新增 symbol 输出 `NaN` 一段时间，但需要暴露状态：

```python
engine.symbol_warmup_status
```

示例：

```python
{
    "BTC-USDT": {"bars_pushed": 500, "ready": True},
    "NEW-USDT": {"bars_pushed": 12, "ready": False},
}
```

未来可以支持：

1. 启动前加载历史 bars warmup。
2. 新增 symbol 时异步拉历史数据 warmup。
3. 未 ready 的 symbol 默认不输出给策略。

---

## 12. Audit 和事后核查

动态 symbol list 后，audit 需要能回答一个问题：

```text
某一分钟某个 symbol 没有 audit bar，是因为数据缺失，还是因为当时它不在 active universe？
```

因此 audit 不能只记录 aggregate bars，还需要记录 universe 版本和变更事件。

### 12.1 Aggregate bar 增加 universe 信息

建议 `AggregateBarEvent` 增加：

```python
symbol_version: str | None
```

可选增加：

```python
universe_reason: str | None
```

这样 verifier 可以按当时的 universe 判断 missing。

### 12.2 单独记录 universe change events

建议新增一个 JSONL 文件类型：

```text
symbol_universe_events_YYYYMMDD_HHMMSS_part000001.jsonl
```

事件示例：

```json
{
  "event_ts_ms": 1790000001000,
  "effective_from_ts_ms": 1790000005000,
  "exchange": "binance",
  "data_source": "trades",
  "version": "2026-05-23T10:15:00.000Z",
  "active_count": 200,
  "added": ["SOL-USDT"],
  "removed": ["DOGE-USDT"],
  "target_symbols": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
  "reason": "top_volume_200"
}
```

字段语义：

| 字段 | 说明 |
|------|------|
| `event_ts_ms` | diff 实际 commit 的时间 |
| `effective_from_ts_ms` | diff 从哪根 bucket 开始生效 |
| `version` | 上游 universe version |
| `added` / `removed` | 本次变更 |
| `target_symbols` | commit 后完整 active universe，便于离线重建 |

### 12.3 verifier 的影响

后续 verifier 应该支持：

1. 读取 aggregate bars。
2. 读取 symbol universe events。
3. 对每个 `(symbol, minute)` 判断 symbol 当时是否 active。
4. 只有 active 但缺 audit bar，才算真正 `missing_audit`。

这对动态 universe 很关键，否则 verifier 会把正常移除的 symbol 误判成数据缺失。

---

## 13. 故障和降级策略

### 13.1 SymbolProvider 拉取失败

建议行为：

- 保留当前 active universe。
- `provider_errors += 1`。
- 超过 `ttl_seconds` 后告警。
- 不自动清空 symbols。

### 13.2 上游返回空 list

空 list 是危险操作，应区分：

```json
{
  "symbols": [],
  "allow_empty": false
}
```

默认策略：

- 如果 `allow_empty` 不为 true，拒绝提交空 universe。
- 记录错误。
- 保留旧 active universe。

### 13.3 大规模变更

例如 200 个 symbols 一次变成 200 个完全不同 symbols。

需要限制：

```python
max_change_ratio = 0.5
max_added_per_commit = 50
max_removed_per_commit = 50
```

超过阈值：

- 可以拒绝提交。
- 或拆分成多次 commit。
- 或要求上游 metadata 标记 `force=True`。

---

## 14. 实施阶段

### Phase 0：文档和测试用例设计

- 明确外部 symbol universe API。
- 明确删除 / 重新加入语义。
- 写 `SymbolManager` 单元测试。
- 写 fake worker 动态 add/remove 测试。
- 明确 ClockDriver 使用 post-flush commit，而不是 before-flush commit。
- 明确 audit universe event schema。

### Phase 1：SymbolManager + fake worker 闭环

- 新增 `StaticSymbolProvider` / `SymbolManager`。
- 用 fake worker 验证 post-flush commit 时序。
- 验证 bar_queue payload 携带 `symbol_version` / `active_symbols`。
- 验证 runtime control message 与 bar payload 的顺序：旧 bar -> diff -> 新 bar。
- 验证 ClockDriver hook 不执行阻塞式 worker 重连或 C++ runtime 修改。
- 先不要把“只过滤输出”的半成品接入真实 live Engine，避免制造 dataflow/runtime 不一致。

### Phase 2：Binance trades 动态 universe

- 给 BinanceTradesWorker 增加 `apply_symbol_diff`。
- 第一版采用 collector 重建连接策略，但重建动作由 worker 自己线程异步完成，不阻塞 ClockDriver。
- 支持 `data_source="trades"` 动态 universe。
- 新增 symbol 时初始化 accumulator 和 C++ runner/factors。
- 移除 symbol 时停止输出、停止订阅，C++ runner 保留 inactive。
- 为 worker 的 `symbols` / `_accumulators` 加锁，`flush()` 使用 active symbols 快照。

优先做 trades 是因为 trades 链路更短：

```text
Collector -> TradesAccumulator -> flush()
```

### Phase 3：Audit + verifier 支持动态 universe

- aggregate audit records 写入 `symbol_version`。
- audit writer 记录 symbol universe change events。
- verifier 支持按 universe events 判断 missing 是否真实异常。

### Phase 4：Kline 动态订阅

- 给 KlineWorker / KlinePublisher / Adapter 增加 symbol 增删。
- 清理 removed symbol 的 round buffer。
- 支持 `data_source="kline"` 和 `data_source="kline_trades"` 动态 universe。

### Phase 5：Warmup 和历史回填

- 新增 per-symbol warmup 状态。
- 新增历史数据 loader。
- 新 symbol 先 warmup，ready 后进入输出。

### Phase 6：C++ runtime 原生 remove/reset

- 增加 `reset_symbol`，未来再考虑 `remove_symbol`。
- 降低长期运行时 inactive runner 的内存增长。

---

## 15. 测试计划

### 15.1 SymbolManager

- 静态 list 初始化。
- 全量快照 diff。
- 旧 version 被拒绝。
- 空 list 默认拒绝。
- 大规模变更超过阈值被拒绝。
- symbol normalize / dedupe / sort。

### 15.2 Worker 动态订阅

- add symbol 后 accumulator/publisher 创建。
- remove symbol 后 flush 不再返回。
- remove 后同名 re-add 状态重置。
- reconnect 后订阅 active symbols。
- post-flush commit 不丢上一根 bucket。
- `flush()` 与 `apply_symbol_diff()` 并发时不抛错、不返回半更新集合。
- collector 重建不阻塞 ClockDriver tick。

### 15.3 Engine 集成

- `update_symbols()` 后下一根 bucket 生效。
- `get_factor_outputs()` 只返回 active symbols。
- runtime thread 按 payload `active_symbols` 过滤，不受当前 active set 超前影响。
- runtime thread 顺序处理 `symbol_diff` control message，C++ `add_symbol/reset_symbol` 不与 `push_bars` 并发。
- provider 失败时保留旧 universe。
- dynamic trades-only simulation/fake worker e2e。
- re-add symbol 时 `reset_symbol()` 生效。

### 15.4 Audit / verifier

- universe change event 能重建任意时刻 active set。
- aggregate audit bar 带正确 `symbol_version`。
- verifier 不把 inactive symbol 的 missing bar 算作异常。
- verifier 仍能发现 active symbol 的真实 missing。

### 15.5 Live 风险测试

- Binance trades 1s，运行中新增 5 个 symbols。
- Binance trades 1s，运行中移除 5 个 symbols。
- 频繁变更：每 30s 调整一次 universe，运行 30 分钟。
- provider 连续失败，验证不清空 active symbols。

---

## 16. 推荐的第一版最小方案

如果要尽快落地，建议先做下面这条最小闭环：

```text
HttpSymbolProvider
  -> SymbolManager(diff)
  -> Engine pending diff
  -> ClockDriver post-flush lightweight commit for next bucket
  -> BinanceTradesWorker receives async reconnect-based universe switch
  -> runtime thread consumes symbol_diff and add/reset symbols
  -> bar_queue payload carries symbol_version / active_symbols
  -> output filtering by payload universe
  -> audit writes symbol_version + universe change events
```

约束：

1. 先只支持 `exchange="binance"`。
2. 先只支持 `data_source="trades"`。
3. 移除 symbol 只做 inactive，不做 C++ 物理删除。
4. re-add symbol 默认 `reset_symbol()`，不复用断层前 rolling state。
5. 新增 symbol 不做历史 warmup，输出自然从 `NaN` 过渡。
6. 上游接口必须提供全量 snapshot。
7. 第一版 worker subscription 用重建连接，暂不做 WS 内动态 SUBSCRIBE/UNSUBSCRIBE。
8. ClockDriver commit hook 只能做轻量通知；worker 重连和 C++ runtime 结构变更必须由各自 owner 线程执行。

这条路径最符合当前架构，因为 Binance trades-only 本来就是 1s 动态 universe 最可能使用的场景，并且 trades worker 比 kline worker 简单。

---

## 17. 开放问题

1. 上游 symbol list 接口的 version 用 timestamp 还是递增整数？
2. 上游返回的是全市场可交易 symbols，还是策略筛选后的 target universe？
3. `reset_symbol(symbol)` 是否先于 dynamic universe 实现？
4. 新增 symbol 是否需要历史 warmup 才能输出？
5. 是否允许 active universe 变为空？
6. 单次最大允许变更比例是多少？
7. Binance trades 第一版重连策略的最大可接受断流时间是多少？
8. `factor_group` 是否也需要未来动态切换？
9. universe change events 是写入独立 JSONL，还是复用 audit writer 的同一队列和 rotate 策略？

---

## 18. 一句话结论

动态 symbol list 不应该只是给 worker 加一个 `symbols.append()`，而应该引入一个版本化的 `SymbolProvider -> SymbolManager -> post-flush tick-boundary commit` 标准流程。第一版优先支持 Binance trades-only，用重建连接实现 universe 切换；删除 symbol 采用 inactive/filter 语义；re-add 通过 `reset_symbol()` 避免 rolling state 断层；audit 必须记录 universe version 和 universe change events，这样动态 universe 才能事后解释和校准。
