# Dynamic Symbol List 设计思考

> 日期：2026-05-23
> 状态：设计草案
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
5. 变更在 ClockDriver tick 边界生效，避免半个 bar 的数据污染。
6. dataflow、runtime、cache、outputs 的 active symbol 集合一致。
7. reconnect 后 worker 能恢复最新 active subscriptions。

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

### 4.3 变更在 ClockDriver tick 边界提交

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
ClockDriver tick 边界提交
        │
        ├── workers.apply_symbol_diff(diff)
        ├── inference.apply_symbol_diff(diff)
        ├── cache.apply_symbol_diff(diff)
        └── active_symbols = target_symbols
```

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

---

## 8. 各模块改造点

### 8.1 Engine

新增职责：

1. 初始化 `SymbolProvider` / `SymbolManager`。
2. 启动 symbol refresh thread 或让 ClockDriver 周期性触发 refresh。
3. 在 tick 边界提交 pending diff。
4. 调用 worker/cache/runtime 的 symbol diff API。
5. `get_factor_outputs()` 默认只返回 active symbols。

### 8.2 ClockDriver

新增一个可选 hook：

```python
ClockDriver(
    freq_seconds=1,
    bar_queue=q,
    before_flush=engine.commit_pending_symbol_diff,
)
```

tick 顺序：

```text
1. before_flush()
2. worker.flush()
3. _aggregate()
4. bar_queue.put()
```

这样 symbol 变更会在新的 bar bucket 开始前生效。

### 8.3 Workers

Kline / trades worker 新增：

```python
def add_symbols(self, symbols: list[str]) -> None
def remove_symbols(self, symbols: list[str]) -> None
def active_symbols(self) -> set[str]
```

TradesWorker 的新增 symbol：

1. 创建 `TradesAccumulator`。
2. collector 发送 subscribe。
3. reconnect 订阅列表更新。

TradesWorker 的移除 symbol：

1. collector 发送 unsubscribe。
2. 删除或 inactive accumulator。
3. `flush()` 不再返回该 symbol。

KlineWorker 同理，需要额外处理 publisher 内部 round buffer。

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

- Binance：优先动态 `SUBSCRIBE` / `UNSUBSCRIBE`。
- OKX：如果动态订阅复杂，可以先采用重建连接策略。

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
| active symbols | Engine | ClockDriver tick boundary |
| worker subscriptions | Worker | ClockDriver tick boundary |
| C++ runner active set | Engine/runtime wrapper | ClockDriver tick boundary |
| outputs filtering | Engine | read time |

核心原则：**refresh thread 只产生 pending diff，不直接改 worker/runtime。**

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

## 12. 故障和降级策略

### 12.1 SymbolProvider 拉取失败

建议行为：

- 保留当前 active universe。
- `provider_errors += 1`。
- 超过 `ttl_seconds` 后告警。
- 不自动清空 symbols。

### 12.2 上游返回空 list

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

### 12.3 大规模变更

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

## 13. 实施阶段

### Phase 0：文档和测试用例设计

- 明确外部 symbol universe API。
- 明确删除 / 重新加入语义。
- 写 `SymbolManager` 单元测试。
- 写 fake worker 动态 add/remove 测试。

### Phase 1：Engine 内部动态 active set

- 新增 `StaticSymbolProvider` / `SymbolManager`。
- 保持 worker 仍静态。
- 先支持 runtime 输出过滤和手动 `update_symbols()` 的 diff 计算。

### Phase 2：Trades 动态订阅

- 给 OKX/Binance TradesWorker 增加 `add_symbols/remove_symbols`。
- 给 TradesCollector 增加动态 subscribe/unsubscribe 或重连。
- 支持 `data_source="trades"` 动态 universe。

优先做 trades 是因为 trades 链路更短：

```text
Collector -> TradesAccumulator -> flush()
```

### Phase 3：Kline 动态订阅

- 给 KlineWorker / KlinePublisher / Adapter 增加 symbol 增删。
- 清理 removed symbol 的 round buffer。
- 支持 `data_source="kline"` 和 `data_source="kline_trades"` 动态 universe。

### Phase 4：Warmup 和历史回填

- 新增 per-symbol warmup 状态。
- 新增历史数据 loader。
- 新 symbol 先 warmup，ready 后进入输出。

### Phase 5：C++ runtime 原生 remove/reset

- 增加 `remove_symbol` 或 `reset_symbol`。
- 降低长期运行时 inactive runner 的内存增长。

---

## 14. 测试计划

### 14.1 SymbolManager

- 静态 list 初始化。
- 全量快照 diff。
- 旧 version 被拒绝。
- 空 list 默认拒绝。
- 大规模变更超过阈值被拒绝。
- symbol normalize / dedupe / sort。

### 14.2 Worker 动态订阅

- add symbol 后 accumulator/publisher 创建。
- remove symbol 后 flush 不再返回。
- remove 后同名 re-add 状态重置。
- reconnect 后订阅 active symbols。

### 14.3 Engine 集成

- `update_symbols()` 后下一 tick 生效。
- `get_factor_outputs()` 只返回 active symbols。
- runtime thread push 时不包含 inactive symbols。
- provider 失败时保留旧 universe。
- dynamic trades-only simulation/fake worker e2e。

### 14.4 Live 风险测试

- Binance trades 1s，运行中新增 5 个 symbols。
- Binance trades 1s，运行中移除 5 个 symbols。
- 频繁变更：每 30s 调整一次 universe，运行 30 分钟。
- provider 连续失败，验证不清空 active symbols。

---

## 15. 推荐的第一版最小方案

如果要尽快落地，建议先做下面这条最小闭环：

```text
HttpSymbolProvider
  -> SymbolManager(diff)
  -> Engine pending diff
  -> ClockDriver tick boundary commit
  -> BinanceTradesWorker dynamic subscribe
  -> InferenceEngine add new symbols
  -> active output filtering
```

约束：

1. 先只支持 `exchange="binance"`。
2. 先只支持 `data_source="trades"`。
3. 移除 symbol 只做 inactive，不做 C++ 物理删除。
4. 新增 symbol 不做历史 warmup，输出自然从 `NaN` 过渡。
5. 上游接口必须提供全量 snapshot。

这条路径最符合当前架构，因为 Binance trades-only 本来就是 1s 动态 universe 最可能使用的场景，并且 trades worker 比 kline worker 简单。

---

## 16. 开放问题

1. 上游 symbol list 接口的 version 用 timestamp 还是递增整数？
2. 上游返回的是全市场可交易 symbols，还是策略筛选后的 target universe？
3. 删除 symbol 后重新加入，是否一定 reset state？
4. 新增 symbol 是否需要历史 warmup 才能输出？
5. 是否允许 active universe 变为空？
6. 单次最大允许变更比例是多少？
7. 对 OKX 是否先用重连实现动态订阅，还是直接实现 WS subscribe/unsubscribe？
8. `factor_group` 是否也需要未来动态切换？

---

## 17. 一句话结论

动态 symbol list 不应该只是给 worker 加一个 `symbols.append()`，而应该引入一个版本化的 `SymbolProvider -> SymbolManager -> tick-boundary commit` 标准流程。第一版优先支持 Binance trades-only，删除 symbol 采用 inactive/filter 语义，新增 symbol 自然 warmup，这样能在不大改 C++ runtime 的前提下把动态 universe 跑起来。
