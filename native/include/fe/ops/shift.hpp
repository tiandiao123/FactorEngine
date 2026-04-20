#pragma once

#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Delay kernel: x.shift(t) — returns the value from t steps ago.
 * Also used as building block for TsDiff and TsPct.
 */
class DelayKernel {
 public:
  explicit DelayKernel(std::uint32_t lag)
      : lag_(lag > 0 ? lag : 0), ring_(lag_ + 1), pos_(0), count_(0) {}

  void reset() { pos_ = 0; count_ = 0; }

  void push(FeFloat x) {
    ring_[pos_ % (lag_ + 1)] = x;
    ++pos_;
    if (count_ <= lag_) ++count_;
  }

  [[nodiscard]] bool ready() const { return count_ > lag_; }

  [[nodiscard]] FeFloat output() const {
    if (!ready())
      return std::numeric_limits<FeFloat>::quiet_NaN();
    return ring_[(pos_ - 1 - lag_) % (lag_ + 1)];
  }

  [[nodiscard]] FeFloat current() const {
    if (pos_ == 0) return std::numeric_limits<FeFloat>::quiet_NaN();
    return ring_[(pos_ - 1) % (lag_ + 1)];
  }

 private:
  std::uint32_t lag_;
  std::vector<FeFloat> ring_;
  std::uint32_t pos_, count_;
};

// Layer 2: array-level functions (batch-optimized, no per-element kernel overhead)

inline void delay(const FeFloat* x, FeFloat* out, int n, int lag) {
  const int t = lag;
  for (int i = 0; i < t && i < n; ++i)
    out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
  for (int i = t; i < n; ++i)
    out[i] = x[i - t];
}

inline void ts_diff(const FeFloat* x, FeFloat* out, int n, int lag) {
  const int t = lag;
  for (int i = 0; i < t && i < n; ++i)
    out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
  for (int i = t; i < n; ++i)
    out[i] = x[i] - x[i - t];
}

inline void ts_pct(const FeFloat* x, FeFloat* out, int n, int lag) {
  const int t = lag;
  for (int i = 0; i < t && i < n; ++i)
    out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
  for (int i = t; i < n; ++i)
    out[i] = x[i] / (x[i - t] + kEps) - 1.0f;
}

}  // namespace fe::ops
