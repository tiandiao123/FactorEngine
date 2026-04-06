# FactorEngine 重构开发文档（2026-04-06）

## 1. 文档目的

这份文档不再讨论“目标架构是否合理”，而是回答一个更直接的问题：

**基于当前仓库的实际代码，应该如何一步一步重构到新的架构。**

文档重点包括：

- 当前代码现状
- 当前实现与目标架构的差距
- 建议的目录和模块拆分
- 分阶段开发步骤
- 每一步的验收标准
- 哪些文件保留、哪些文件拆分、哪些文件只作为兼容层存在

配套设计文档见：

- [refactor_design.md](/home/ubuntu/workspace/FactorEngine/docs/20260406/refactor_design.md)

---

## 2. 当前代码现状

### 2.1 当前主路径

当前系统的主路径非常简单：

```text
OKX candle1s
-> dataflow.collector.OKXCollector
-> dataflow.dataflow.BarAggregator
-> shared dict[str, np.ndarray]
-> factorengine.engine.Engine.get_data()
-> 外部 while loop 做因子计算
```

这个路径目前只覆盖了 `bars`，没有把 `trades` 和 `books` 纳入正式运行时。

### 2.2 当前关键文件

#### `factorengine/engine.py`

当前职责：

- 对外提供单一入口 `Engine`
- 解析 `data_freq` / `pull_interval`
- 创建一个共享 `_data_cache`
- 创建一个 `Dataflow`
- 提供 `get_data()` 深拷贝当前 bar cache

当前特点：

- `Engine` 仍然是 bar-only 的
- `get_data()` 返回的是 `dict[str, ndarray]`
- 这个接口目前只适用于 OHLCV 窗口拉取，不适合 `trade` / `book` 事件流

#### `dataflow/dataflow.py`

当前职责：

- 在独立线程中跑 `asyncio` loop
- 持有 `BarAggregator`
- 收到 `candle1s` 后进行 bar 聚合
- 将聚合结果 append 到共享 cache

当前特点：

- `BarAggregator` 和 `Dataflow` 强耦合
- `Dataflow` 同时承担线程管理、事件循环、聚合、缓存写入
- `_data_cache` 只有一类结构，不具备扩展到 `trades` / `books` 的自然形态

#### `dataflow/collector.py`

当前职责：

- 连接 OKX business WS
- 订阅 `candle1s`
- 按 symbol 分片
- 把原始数据回调给 `Dataflow`

当前特点：

- 只支持 `candle1s`
- `OKXCollector` 名称偏泛化，但实现实际上是 bar collector
- 若继续叠加 `trades` / `books` 逻辑，这个文件会很快失控

#### `tests/test_live.py`

当前职责：

- 手动启动 `Engine`
- 每 `pull_interval` 秒拉一次 bar snapshot
- 打印和保存当前快照

当前特点：

- 本质上是 bar-only 的集成手动测试
- 工作模式是“定时拉快照”
- 这个脚本未来应该保留，但定位要改成 `bars live test`

#### `tests/test_micro_ws.py`

当前职责：

- 手动测试 `books5`、`trades`、`trades-all`
- 观察 OKX 微观数据的流速和粒度
- 落盘 NDJSON 方便人工检查

当前特点：

- 这个脚本已经证明：`trades` / `books5` 路径值得正式接入
- 但它仍然是孤立测试脚本，没有进入正式 Dataflow 模块

---

## 3. 当前实现与目标架构的差距

### 3.1 数据类型还没有分层

当前只有：

```text
dict[symbol, ndarray(N, 6)]
```

这只适合 bar。

目标架构需要至少三类数据：

- `BarEvent / BarCache`
- `TradeEvent / TradeCache`
- `BookEvent / BookCache`

### 3.2 Dataflow 还是单路实现

当前 Dataflow 实际上等于：

```text
BarDataflow
```

而目标是：

```text
Bars Dataflow
Trades Dataflow
Books Dataflow
```

再由更高层的 manager 统一管理生命周期。

### 3.3 Engine API 过于单一

当前只有：

- `start()`
- `stop()`
- `get_data()`

这套 API 还没有表达出：

- 多路 cache
- 定时评估调度
- factor snapshot 输出
- runtime bridge

### 3.4 运行时边界不存在

当前因子计算仍然发生在外部用户 while loop 中。

这意味着：

- 没有 scheduler
- 没有 worker pool
- 没有统一 factor evaluation tick
- 没有 runtime state
- 没有 C++ bridge 边界

### 3.5 测试组织还是“脚本式”

目前测试主要是：

- `test_live.py`
- `test_micro_ws.py`

这对探索阶段是够的，但对正式重构不够。后续需要：

- schema 单测
- cache 单测
- scheduler 单测
- collector 集成测试
- 回放测试

---

## 4. 重构原则

### 原则 1：先拆职责，再上功能

不要先把 `trades` / `books` 全塞进现有类里。

正确顺序应该是：

1. 先把 bar-only 实现拆出清晰边界
2. 再按同样模式接入 `trades`
3. 再接入 `books`

### 原则 2：先稳定 Python 侧模块边界，再做 C++ runtime

如果 Python 侧连事件模型、cache 模型、生命周期边界都没定，C++ runtime 很容易做成一次性原型，后面反复返工。

### 原则 3：兼容层先保留

`Engine.get_data()` 当前已经能跑。

即使未来内部结构重构了，短期也建议保留一个 bar 兼容层，让现有脚本继续能跑。这样重构过程风险小很多。

### 原则 4：先建立事件和 cache 模型，再建立 scheduler

没有统一 `BarEvent / TradeEvent / BookEvent`，scheduler 和 runtime 的接口就很难定。

---

## 5. 建议的目标目录结构

下面不是最终唯一方案，但建议接近这个形态：

```text
FactorEngine/
  dataflow/
    __init__.py
    events.py
    cache.py
    okx/
      __init__.py
      common.py
      symbols.py
      bar_collector.py
      trade_collector.py
      book_collector.py
    bars/
      __init__.py
      aggregator.py
      worker.py
    trades/
      __init__.py
      worker.py
    books/
      __init__.py
      worker.py
    manager.py
  factorengine/
    __init__.py
    engine.py
    scheduler.py
    runtime_bridge.py
    snapshot.py
    config.py
  cpp/
    runtime/
      ...
  tests/
    test_live.py
    test_micro_ws.py
    test_events.py
    test_cache.py
    test_scheduler.py
  docs/
    20260406/
      refactor_design.md
      development_plan.md
```

这里有几个关键点：

- `collector.py` 不再承载所有 OKX 逻辑
- `dataflow.py` 不再同时承担 manager、worker、aggregator 三个角色
- `factorengine` 目录开始出现 runtime 相关模块
- `cpp/` 目录为后续 C++ runtime 预留

---

## 6. 当前文件如何重构

### 6.1 `factorengine/engine.py`

当前建议：

- 短期保留文件名不变
- 让它继续作为外部唯一入口
- 但内部职责收缩为 orchestration 层

建议修改方向：

1. 保留 `parse_freq()`
2. 将 Dataflow 的创建迁移到 `dataflow.manager.DataflowManager`
3. 新增 scheduler / runtime bridge 的挂载位置
4. `get_data()` 作为 bar 兼容接口先保留
5. 后续再增加：
   - `get_bar_data()`
   - `get_factor_snapshot()`
   - `start_runtime()`

不建议做的事：

- 不要在 `Engine` 里直接长出 `trades` / `books` 细节逻辑
- 不要让 `Engine` 直接持有过多底层 collector 引用

### 6.2 `dataflow/dataflow.py`

当前建议：

- 不要继续往这个文件里加新功能
- 它现在已经同时包含：
  - `BarAggregator`
  - `Dataflow` thread lifecycle
  - cache write
  - callback dispatch

建议拆分为：

1. `dataflow/bars/aggregator.py`
   - 存放 `BarAggregator`
2. `dataflow/bars/worker.py`
   - 负责 bar stream 的生命周期、聚合和写 cache
3. `dataflow/manager.py`
   - 统一启动/停止 bars/trades/books workers

迁移策略：

- 第一阶段只做代码搬迁，不改变 bar 行为
- bar 行为稳定后再接 `trades` / `books`

### 6.3 `dataflow/collector.py`

当前建议：

- 这个文件要拆

原因：

- 当前 `OKXCollector` 名字太泛
- 实际只支持 `candle1s`
- 后续很快会出现 `trade collector`、`book collector`

建议拆分为：

1. `dataflow/okx/common.py`
   - 公共 ws connect / subscribe / reconnect 模板
2. `dataflow/okx/symbols.py`
   - `fetch_all_swap_symbols()`
3. `dataflow/okx/bar_collector.py`
4. `dataflow/okx/trade_collector.py`
5. `dataflow/okx/book_collector.py`

第一阶段不追求完全抽象到极致，先把功能边界切干净。

### 6.4 `tests/test_live.py`

建议保留，但重新定位。

新定位：

- `bars live integration test`

未来可以继续保留当前行为：

- 每 10 秒拉一次 bar snapshot
- 保存 CSV
- 验证 bar cache 是否稳定

但文案和注释要改成：

- 这是 bars 路径测试
- 不是整个新架构的完整测试

### 6.5 `tests/test_micro_ws.py`

建议保留，并逐步演化成 collector 验证工具。

新定位：

- `microstructure collector smoke test`

未来可以分出两类脚本：

1. `test_micro_ws.py`
   - 手动观察 websocket 流
2. 后续更正式的：
   - `test_trades_live.py`
   - `test_books_live.py`

---

## 7. 第一阶段必须先补的基础模块

这部分是整个重构最值得优先做的内容。

### 7.1 `dataflow/events.py`

这是最先应该创建的文件。

定义统一事件模型：

```text
BarEvent
TradeEvent
BookEvent
```

最低要求字段：

#### `BarEvent`

- `symbol`
- `channel`
- `ts_event`
- `ts_recv`
- `open`
- `high`
- `low`
- `close`
- `vol`

#### `TradeEvent`

- `symbol`
- `channel`
- `ts_event`
- `ts_recv`
- `trade_id`
- `px`
- `sz`
- `side`
- `count`
- `is_aggregated`

#### `BookEvent`

- `symbol`
- `channel`
- `ts_event`
- `ts_recv`
- `best_bid_px`
- `best_bid_sz`
- `best_ask_px`
- `best_ask_sz`
- `bids`
- `asks`

第一阶段不要求这些类型直接给 C++ 用，但必须保证 Python 侧有统一内部 schema。

### 7.2 `dataflow/cache.py`

目标：

- 用明确类取代裸 `dict[str, ndarray]`

建议先做三个 cache wrapper：

```text
BarCache
TradeCache
BookCache
```

每个 cache 最低要求：

- append / update
- get latest
- get window
- get snapshot
- trim / ring buffer
- thread-safe access

第一阶段 bar cache 可以内部仍然用 numpy。
trade/book cache 可以先用 deque 或 list + trim。

### 7.3 `dataflow/manager.py`

目标：

- 统一管理多路 dataflow

最低职责：

- `start()`
- `stop()`
- 启动 bars worker
- 预留 trades/books worker 接口

第一阶段哪怕只有 bars worker，也建议先把 manager 抽出来。

---

## 8. 建议的开发阶段

### Phase 0：冻结现有 bar 行为

目标：

- 先确认当前 bar 路径行为不继续漂移

要做的事：

1. 保留当前 `Engine -> Dataflow -> BarAggregator -> get_data()` 路径
2. 不再往现有 `dataflow.py` 和 `collector.py` 里叠加新功能
3. 把当前 live script 当作 baseline

验收标准：

- `tests/test_live.py` 行为不变
- 当前 bar snapshot 输出仍然稳定

### Phase 1：抽出事件模型和 cache 抽象

目标：

- 建立后续所有重构的地基

要做的事：

1. 新增 `dataflow/events.py`
2. 新增 `dataflow/cache.py`
3. 给 bar 路径先套上 `BarCache` 抽象
4. 让 `Engine.get_data()` 继续通过兼容层返回旧格式

验收标准：

- 外部脚本仍可运行
- bar cache 不再是散落在 `Engine` 和 `Dataflow` 里的裸 dict

### Phase 2：拆 bar-only Dataflow

目标：

- 把当前单文件 `dataflow/dataflow.py` 拆成可扩展结构

要做的事：

1. 搬出 `BarAggregator`
2. 新建 `bars/worker.py`
3. 新建 `manager.py`
4. 让 `Engine` 改为依赖 manager 而不是旧 `Dataflow`

验收标准：

- bar 行为不变
- 文件职责明显清晰

### Phase 3：拆 OKX collector

目标：

- 为多路数据源接入做好准备

要做的事：

1. 拆 `collector.py`
2. 保留 `fetch_all_swap_symbols()`
3. 新建：
   - `bar_collector.py`
   - `trade_collector.py`
   - `book_collector.py`
4. 公共重连模板抽到 `common.py`

验收标准：

- 现有 bar collector 功能不回退
- `test_micro_ws.py` 中的逻辑开始能复用正式 collector 代码

### Phase 4：正式接入 trades cache

目标：

- 把当前实验脚本里的 `trades` 逻辑接到正式 dataflow

要做的事：

1. 新增 `TradeCollector`
2. 新增 `TradeCache`
3. 新增 trades worker
4. 在 manager 中管理 bars + trades
5. 先不接 C++ runtime

验收标准：

- 可以稳定采集 `trades` / `trades-all`
- 可以查询最近窗口
- 有单独 live test

### Phase 5：正式接入 books cache

目标：

- 建立浅簿正式路径

要做的事：

1. 新增 `BookCollector`
2. 新增 `BookCache`
3. 新增 books worker
4. 在 manager 中管理三路

验收标准：

- `books5` 稳定采集
- 能拿到最新快照和短历史

### Phase 6：建立 Python scheduler 原型

目标：

- 在没有 C++ runtime 之前，先验证“定时评估”模式

要做的事：

1. 新增 `factorengine/scheduler.py`
2. 支持 `evaluation_interval`
3. 在每个 tick 上读取所需窗口
4. 先做一个最小 Python 版 factor job runner

注意：

- 这不是最终 runtime
- 这是为了验证 scheduling 模型和接口形状

验收标准：

- 可以每 `10s` 统一触发一次 factor evaluation
- 线程池可以在 tick 内算完全部目标因子

### Phase 7：引入 C++ runtime bridge

目标：

- 将 Python scheduler 原型替换成正式 C++ runtime

要做的事：

1. 建立 `cpp/runtime/`
2. 建立 `pybind11` 桥接
3. 定义：
   - `push_bar`
   - `push_trade`
   - `push_book`
   - `evaluate`
   - `poll_snapshot`
4. 迁移首批因子

验收标准：

- Python 只做 dataflow 和 orchestration
- 因子热路径已迁入 C++

---

## 9. 推荐的首批开发顺序

如果只看“下周先做什么”，建议按下面顺序推进：

1. 建 `dataflow/events.py`
2. 建 `dataflow/cache.py`
3. 建 `dataflow/manager.py`
4. 拆 `dataflow/dataflow.py`
5. 拆 `dataflow/collector.py`
6. 把 `test_micro_ws.py` 的 trades/books 逻辑收编为正式 collector
7. 再做 scheduler 原型
8. 最后再碰 C++ runtime

这个顺序的好处是：

- 先把 Python 代码的结构理顺
- 再扩展数据类型
- 最后才引入跨语言复杂度

---

## 10. 验收标准

### 10.1 Python 侧重构完成的标准

满足以下条件时，说明 Python 侧结构已经足够进入 C++ 阶段：

1. bars / trades / books 三路都有独立 collector
2. 三路都有独立 cache
3. 有统一事件 schema
4. 有统一 manager 生命周期
5. 有定时评估 scheduler 原型
6. 旧 bar 路径仍可通过兼容层运行

### 10.2 C++ runtime 接入前必须明确的接口

必须先定：

1. Python 如何把事件推给 runtime
2. runtime 如何返回 factor snapshot
3. evaluation tick 由谁驱动
4. symbol 分片由谁负责
5. cache 与 runtime state 如何分工

---

## 11. 风险点

### 风险 1：一边重构一边堆功能

如果在还没拆清楚 `dataflow.py` / `collector.py` 之前就强上 trades/books，后面返工成本会很高。

### 风险 2：过早绑定 C++ 细节

如果 Python 侧事件模型还不稳定，就过早开始 C++ runtime，接口会频繁推倒重来。

### 风险 3：兼容层一次性删除

直接删掉当前 `Engine.get_data()` 会让现有脚本和验证路径全部失效，不利于迁移。

### 风险 4：把 scheduler 和 factor runtime 写成同一层

建议：

- scheduler 是 orchestration 层
- factor runtime 是 execution 层

两者不要一开始就写死在一个类里。

---

## 12. 开发建议总结

从开发顺序上看，最合理的路径不是：

```text
直接上 C++ runtime
-> 再想 Python 采集怎么接
```

而是：

```text
先把当前 Python bar-only 系统拆出清晰边界
-> 再正式接入 trades / books
-> 再验证 timer-driven factor evaluation
-> 最后引入 C++ factor runtime
```

从当前仓库出发，最先该动的不是新因子表达式，而是下面三个基础模块：

1. `dataflow/events.py`
2. `dataflow/cache.py`
3. `dataflow/manager.py`

这三个模块一旦建立起来，整个重构会变得非常顺；如果这三个模块不先做，后面加任何新功能都只是在继续扩大旧结构的技术债。
