#pragma once

#include <cstdint>
#include <limits>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Exponential moving average aligned with pandas:
 *   ewm(span=t, min_periods=t, adjust=False).mean()
 * alpha = 2.0 / (span + 1)
 */
class EmaKernel {
 public:
  explicit EmaKernel(std::uint32_t span)
      : span_(span > 0 ? span : 1),
        alpha_(2.0 / (static_cast<double>(span_) + 1.0)),
        count_(0), ema_(0.0), initialized_(false) {}

  void reset() { count_ = 0; ema_ = 0.0; initialized_ = false; }

  void push(FeFloat x) {
    ++count_;
    if (fe_is_nan(x)) {
      // NaN breaks the chain — pandas behavior: output NaN from here
      initialized_ = false;
      count_ = 0;
      return;
    }
    if (!initialized_) {
      ema_ = static_cast<double>(x);
      initialized_ = true;
    } else {
      ema_ = alpha_ * static_cast<double>(x) + (1.0 - alpha_) * ema_;
    }
  }

  [[nodiscard]] bool ready() const { return initialized_ && count_ >= span_; }

  [[nodiscard]] FeFloat output() const {
    if (!ready())
      return std::numeric_limits<FeFloat>::quiet_NaN();
    return static_cast<FeFloat>(ema_);
  }

 private:
  std::uint32_t span_;
  double alpha_;
  std::uint32_t count_;
  double ema_;
  bool initialized_;
};

inline void ema(const FeFloat* x, FeFloat* out, int n, int span) {
  EmaKernel kernel(static_cast<std::uint32_t>(span));
  for (int i = 0; i < n; ++i) { kernel.push(x[i]); out[i] = kernel.output(); }
}

}  // namespace fe::ops
