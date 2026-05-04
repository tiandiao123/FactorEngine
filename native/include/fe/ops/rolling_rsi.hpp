#pragma once

#include <cstdint>
#include <limits>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling RSI (Relative Strength Index) kernel using Wilder's smoothing.
 *
 * RSI = 100 - 100 / (1 + RS)
 * RS  = avg_gain / avg_loss
 *
 * First `window` deltas use simple average to seed, then exponential:
 *   avg_gain = (prev_avg_gain * (w-1) + gain) / w
 *   avg_loss = (prev_avg_loss * (w-1) + loss) / w
 *
 * Aligned with: pandas_ta.rsi(close, length=w) or talib.RSI(close, timeperiod=w)
 * Requires window >= 2. Output starts at index `window` (first valid delta at index 1,
 * need `window` deltas to seed).
 */
class RsiKernel {
 public:
  explicit RsiKernel(int window)
      : t_(window >= 2 ? window : 2) {}

  void reset() {
    prev_ = std::numeric_limits<FeFloat>::quiet_NaN();
    delta_count_ = 0;
    gains_sum_ = 0; losses_sum_ = 0;
    avg_gain_ = 0; avg_loss_ = 0;
    initialized_ = false;
  }

  void push(FeFloat x) {
    if (fe_is_nan(prev_)) {
      prev_ = x;
      return;
    }
    if (fe_is_nan(x)) {
      prev_ = x;
      return;
    }

    double delta = static_cast<double>(x) - static_cast<double>(prev_);
    prev_ = x;
    double gain = delta > 0 ? delta : 0.0;
    double loss = delta < 0 ? -delta : 0.0;

    if (!initialized_) {
      gains_sum_ += gain;
      losses_sum_ += loss;
      ++delta_count_;
      if (delta_count_ == t_) {
        avg_gain_ = gains_sum_ / t_;
        avg_loss_ = losses_sum_ / t_;
        initialized_ = true;
      }
    } else {
      avg_gain_ = (avg_gain_ * (t_ - 1) + gain) / t_;
      avg_loss_ = (avg_loss_ * (t_ - 1) + loss) / t_;
    }
  }

  [[nodiscard]] bool ready() const { return initialized_; }

  [[nodiscard]] FeFloat output() const {
    if (!initialized_) return std::numeric_limits<FeFloat>::quiet_NaN();
    if (avg_loss_ < kRsiLossEps) {
      return (avg_gain_ < kRsiLossEps) ? 50.0f : 100.0f;
    }
    double rs = avg_gain_ / avg_loss_;
    return static_cast<FeFloat>(100.0 - 100.0 / (1.0 + rs));
  }

 private:
  int t_;
  FeFloat prev_ = std::numeric_limits<FeFloat>::quiet_NaN();
  int delta_count_ = 0;
  double gains_sum_ = 0, losses_sum_ = 0;
  double avg_gain_ = 0, avg_loss_ = 0;
  bool initialized_ = false;
};

inline void rolling_rsi(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 2 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  RsiKernel k(window);
  for (int i = 0; i < n; ++i) { k.push(x[i]); out[i] = k.output(); }
}

}  // namespace fe::ops
