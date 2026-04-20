#pragma once

#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

class RollingSumKernel {
 public:
  explicit RollingSumKernel(std::uint32_t window)
      : t_(window > 0 ? window : 1), ring_(t_), pos_(0), count_(0),
        sum_(0.0), nan_count_(0) {}

  void reset() { pos_ = 0; count_ = 0; sum_ = 0.0; nan_count_ = 0; }

  void push(FeFloat x) {
    if (count_ >= t_) {
      FeFloat old = ring_[pos_ % t_];
      if (fe_is_nan(old)) --nan_count_;
      else sum_ -= static_cast<double>(old);
    }
    ring_[pos_ % t_] = x;
    if (fe_is_nan(x)) ++nan_count_;
    else sum_ += static_cast<double>(x);
    ++pos_;
    if (count_ < t_) ++count_;
  }

  [[nodiscard]] bool ready() const { return count_ >= t_; }

  [[nodiscard]] FeFloat output() const {
    if (!ready() || nan_count_ > 0)
      return std::numeric_limits<FeFloat>::quiet_NaN();
    return static_cast<FeFloat>(sum_);
  }

 private:
  std::uint32_t t_;
  std::vector<FeFloat> ring_;
  std::uint32_t pos_, count_;
  double sum_;
  std::uint32_t nan_count_;
};

inline void rolling_sum(const FeFloat* x, FeFloat* out, int n, int window) {
  RollingSumKernel kernel(static_cast<std::uint32_t>(window));
  for (int i = 0; i < n; ++i) { kernel.push(x[i]); out[i] = kernel.output(); }
}

}  // namespace fe::ops
