# FactorEngine TODO（当前优先级）

## 当前优先级结论

当前阶段的优先级是：

1. **先做 scheduler 原型**
2. **暂缓 cache ring buffer 优化**

原因：

- dataflow 主路径已经能跑
- bars / trades / books 的数组化接口已经基本成形
- factorengine 后面的调度与计算边界还没完全落地
- 现在过早去做 ring buffer 优化，容易在 scheduler/runtime 接口还没定时就重复返工

换句话说：

**现在最重要的是先把“怎么调度计算”想清楚，而不是先把 cache 优化到极致。**

---

## 暂缓项

### 1. BarCache / TradeCache / BookCache 的 ring buffer 化

当前 review 提到的问题是成立的：

- `np.vstack` / `np.concatenate` 在高频路径上会带来 O(n) 拷贝
- 对 `TradeCache` 和 `BookCache` 尤其明显

但这件事当前先记为：

```text
TODO: defer
```

原因：

1. 当前系统更大的不确定性在 scheduler / runtime 接口
2. cache 优化方案最好等 slicing / evaluation 方式稳定后再做
3. 过早重写 cache 底层，可能和后续 runtime 设计冲突

### 2. 更复杂的性能优化

当前也暂缓：

- 多级 batch 写入
- 更复杂的 lock 优化
- 多线程因子执行优化
- 提前引入 C++ runtime

这些都应该晚于 scheduler 原型。

---

## 当前主线任务

### 1. 设计并实现 Python scheduler 原型

最小目标：

- 固定频率 tick
- 从 dataflow cache 做 slicing
- 调用最小 factor runtime
- 产出 factor snapshot

### 2. 验证 factor 计算输入输出边界

需要先明确：

- factor runtime 接收什么
- 每轮 evaluation 切什么窗口
- factor snapshot 长什么样

### 3. 再决定 cache 优化方案

等 scheduler 原型跑通后，再回来看：

- cache 应不应该改成 ring buffer
- 改的话应该先改 trades/books 还是三路一起改
- 对 factorengine 的读接口要不要一起调整

---

## 明确的下一步

当前建议的开发顺序：

1. `factorengine/factor_spec.py`
2. `factorengine/factor_snapshot.py`
3. `factorengine/factor_runtime.py`
4. `factorengine/scheduler.py`
5. `tests/test_scheduler_live.py`

等这条线跑通后，再回头处理：

- `TradeCache` / `BookCache` ring buffer
- 更激进的性能优化

---

## 一句话版本

**TODO: 先做 scheduler，cache 优化先延后。**
