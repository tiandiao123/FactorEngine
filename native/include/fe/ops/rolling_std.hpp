#pragma once

#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling population std (ddof=0) via Welford online algorithm.
 * Internal accumulation in double for numerical stability.
 * Aligned with: TsStd(x, t, ddof=0) in ts_ops.py
 */
class RollingStdKernel {
 public:
  explicit RollingStdKernel(std::uint32_t window)
      : t_(window > 0 ? window : 1), ring_(t_), pos_(0), count_(0),
        sum_(0.0), sum_sq_(0.0), nan_count_(0) {}

  void reset() { pos_ = 0; count_ = 0; sum_ = 0.0; sum_sq_ = 0.0; nan_count_ = 0; }

  void push(FeFloat x) {
    if (count_ >= t_) {
      FeFloat old = ring_[pos_ % t_];
      if (fe_is_nan(old)) {
        --nan_count_;
      } else {
        double d = static_cast<double>(old);
        sum_ -= d;
        sum_sq_ -= d * d;
      }
    }
    ring_[pos_ % t_] = x;
    if (fe_is_nan(x)) {
      ++nan_count_;
    } else {
      double d = static_cast<double>(x);
      sum_ += d;
      sum_sq_ += d * d;
    }
    ++pos_;
    if (count_ < t_) ++count_;
  }

  [[nodiscard]] bool ready() const { return count_ >= t_; }

  [[nodiscard]] FeFloat output() const {
    if (!ready() || nan_count_ > 0)
      return std::numeric_limits<FeFloat>::quiet_NaN();
    double n = static_cast<double>(t_);
    double mean = sum_ / n;
    double var = sum_sq_ / n - mean * mean;
    if (var < 0.0) var = 0.0;
    return static_cast<FeFloat>(std::sqrt(var));
  }

 private:
  std::uint32_t t_;
  std::vector<FeFloat> ring_;
  std::uint32_t pos_, count_;
  double sum_, sum_sq_;
  std::uint32_t nan_count_;
};

inline void rolling_std(const FeFloat* x, FeFloat* out, int n, int window) {
  RollingStdKernel kernel(static_cast<std::uint32_t>(window));
  for (int i = 0; i < n; ++i) { kernel.push(x[i]); out[i] = kernel.output(); }
}

inline void rolling_var(const FeFloat* x, FeFloat* out, int n, int window) {
  RollingStdKernel kernel(static_cast<std::uint32_t>(window));
  for (int i = 0; i < n; ++i) {
    kernel.push(x[i]);
    FeFloat s = kernel.output();
    out[i] = fe_is_nan(s) ? std::numeric_limits<FeFloat>::quiet_NaN() : s * s;
  }
}

}  // namespace fe::ops
