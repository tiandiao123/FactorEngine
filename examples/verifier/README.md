# Audit Verifier 使用说明

这个目录放审计日志校验脚本，用于把 FactorEngine 落盘的 aggregate audit 数据和外部参考数据做对比。

当前主要脚本：

```text
verify_binance_audit_vs_kline.py
```

它用于校验：

```text
FactorEngine audit 5s aggregate bars
  -> 合成 1m bars

Binance reference
  -> official 1m kline 或 aggTrades REST 聚合成 1m

按 (symbol, minute open_time) 对比 OHLCV
```

---

## 1. 校验逻辑

脚本读取 `AuditWriter` 写出的 JSONL：

```text
aggregate_bars_*.jsonl
```

然后：

1. 根据 `--symbols` 过滤币种。
2. 根据 `--start` / `--end` 过滤时间范围。
3. 把 audit 里的 5s bars 聚合成 1m bars。
4. 按 `--reference` 选择参考源：

```text
--reference kline      -> https://fapi.binance.com/fapi/v1/klines
--reference aggtrades  -> https://fapi.binance.com/fapi/v1/aggTrades
```

5. 对比：

```text
open
high
low
close
volume
```

`kline` 是官方 1m kline sanity check；`aggtrades` 会把 Binance aggTrades REST 聚合成 1m，更适合校验 trades-only / exchange-time 聚合结果。

---

## 1.1 参考源差异

`kline` 模式调用：

```text
https://fapi.binance.com/fapi/v1/klines
```

`aggtrades` 模式调用：

```text
https://fapi.binance.com/fapi/v1/aggTrades
```

对于 trades-only engine，尤其是 Binance `aggTrade` WS，`aggtrades` 通常是更直接的参考源。官方 1m kline 可能和 aggTrades 在少数分钟存在 volume / trade count 口径差异，所以 `kline` 更适合检查时间错位和 OHLC 大体一致性。

---

## 2. 时间语义

Binance 官方 kline 的时间戳是 `open_time`。

例如：

```text
open_time = 11:01:00
```

表示这一根 1m kline 覆盖：

```text
[11:01:00, 11:02:00)
```

所以如果你要校验完整分钟，`--end` 应该使用已经闭合的分钟边界。

例如当前是：

```text
11:01:05
```

安全校验范围最多到：

```text
--end "11:01:00"
```

不要把 `[11:01:00, 11:02:00)` 这种未完成分钟放进去。

---

## 3. 为什么需要 `--audit-ts-is-end`

当前 trades-only audit 里，`bucket_ts_ms` 更接近 **bucket 结束边界**。

例如：

```text
bucket_ts_ms = 10:20:00
freq_seconds = 5s
```

这条 5s bar 实际对应：

```text
[10:19:55, 10:20:00)
```

而不是：

```text
[10:20:00, 10:20:05)
```

所以校验 Binance 官方 kline 时，需要加：

```bash
--audit-ts-is-end
```

脚本会做：

```text
effective_ts = bucket_ts_ms - freq_seconds * 1000
```

然后再把 `effective_ts` 归到 1m bucket。

如果不加这个参数，5s bar 会被错归到下一分钟，容易出现系统性 mismatch。

---

## 4. 示例命令

```bash
cd /home/ubuntu/workspace/FactorEngine

python examples/verifier/verify_binance_audit_vs_kline.py \
  --audit-file /home/ubuntu/workspace/FactorEngine/examples/engine/data/audit/binance_trades_demo/aggregate_bars_20260524_101827_part000001.jsonl \
  --symbols BTC-USDT,ETH-USDT,SOL-USDT \
  --start "2026-05-24T10:19:00Z" \
  --end "2026-05-24T10:24:00Z" \
  --audit-ts-is-end \
  --volume-rtol 0.001 \
  --max-mismatches 5
```

参数说明：

```text
--audit-file        AuditWriter 生成的 aggregate_bars_*.jsonl
--symbols           逗号分隔的币种，例如 BTC-USDT,ETH-USDT
--start             校验开始时间，包含
--end               校验结束时间，不包含
--audit-ts-is-end   把 audit bucket_ts_ms 当成 bucket 结束边界
--reference         参考源：kline 或 aggtrades，默认 kline
--price-tol         price 字段的绝对误差阈值，默认 1e-8
--price-rtol        price 字段的相对误差阈值，默认 0
--volume-tol        volume 字段的绝对误差阈值，默认 1e-8
--volume-rtol       volume 字段的相对误差阈值，默认 0
--max-mismatches    每个 symbol 最多打印多少条 mismatch 明细
```

---

## 5. 误差阈值

脚本现在支持绝对误差和相对误差两套标准。某个字段只要满足其中一个条件，就算 match：

```text
abs(audit_val - official_val) <= atol
```

或者：

```text
abs(audit_val - official_val) / max(abs(audit_val), abs(official_val)) <= rtol
```

其中：

```text
price 字段使用 --price-tol / --price-rtol
volume 字段使用 --volume-tol / --volume-rtol
```

例如：

```bash
--volume-rtol 0.001
```

表示 volume 相对误差小于等于 `0.1%` 时认为通过。这个规则适合 volume，因为不同币种、不同分钟的成交量量级差别很大，用固定绝对误差不够稳。

price 默认仍然可以保持严格：

```bash
--price-tol 1e-8 --price-rtol 0
```

如果后续发现官方 kline 和实时聚合在 open/close 边界上存在极小偏差，也可以给 price 配一个很小的相对阈值。

`trade_count` 当前不参与 match 判定，只在 mismatch 明细里展示。原因是官方 kline 的 count 和当前 audit 的聚合来源不一定是同一个计数口径。

---

## 6. `--max-mismatches`

`--max-mismatches` 只控制打印多少条 mismatch 明细，不影响统计。

例如：

```bash
--max-mismatches 5
```

表示：

```text
BTC-USDT 最多打印 5 条 mismatch 明细
ETH-USDT 最多打印 5 条 mismatch 明细
SOL-USDT 最多打印 5 条 mismatch 明细
```

summary 仍然会统计全部分钟：

```text
matched
missing_audit
missing_official
mismatched
```

如果只想看 summary，可以用：

```bash
--max-mismatches 0
```

---

## 7. 全部 match 时的输出

如果全部对上，输出类似：

```text
BTC-USDT:
  official minutes: 5
  audit minutes:    5
  matched:          5
  missing_audit:    0
  missing_official: 0
  mismatched:       0
```

不会打印具体 mismatch 明细。

脚本退出码：

```text
0 = 全部 match
1 = 存在 mismatch / missing
```

所以可以这样判断：

```bash
python examples/verifier/verify_binance_audit_vs_kline.py ... --audit-ts-is-end
echo $?
```

---

## 8. 注意事项

这个 verifier 是 sanity check，不是严格的 raw trades 证明。

原因：

1. audit 文件来自 FactorEngine online aggregate。
2. Binance 官方 kline 来自 Binance kline endpoint。
3. 当前系统使用的是 Binance `aggTrade` stream。
4. 如果要严格证明每个 5s bucket 聚合正确，需要保存 normalized trades 或从 REST aggTrades replay。

当前 verifier 最适合用来检查：

```text
时间边界是否错位
OHLCV 是否大体一致
是否缺分钟
是否出现明显 volume / price 异常
```
