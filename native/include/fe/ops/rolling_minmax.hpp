#pragma once

#include <cstdint>
#include <deque>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling min/max via monotonic deque — O(1) amortized per push.
 * Aligned with pandas rolling(t, min_periods=t).min() / .max()
 */
template <bool IsMax>
class RollingExtremalKernel {
 public:
  explicit RollingExtremalKernel(std::uint32_t window)
      : t_(window > 0 ? window : 1), ring_(t_), pos_(0), count_(0), nan_count_(0) {}

  void reset() { pos_ = 0; count_ = 0; nan_count_ = 0; dq_.clear(); }

  void push(FeFloat x) {
    // evict oldest if window full
    if (count_ >= t_) {
      FeFloat old = ring_[pos_ % t_];
      if (fe_is_nan(old)) {
        --nan_count_;
      } else {
        // remove front if it's the element leaving the window
        if (!dq_.empty() && dq_.front() == pos_ - t_) {
          dq_.pop_front();
        }
      }
    }

    ring_[pos_ % t_] = x;

    if (fe_is_nan(x)) {
      ++nan_count_;
    } else {
      // maintain monotonic deque
      while (!dq_.empty()) {
        FeFloat back_val = ring_[dq_.back() % t_];
        bool should_pop = IsMax ? (back_val <= x) : (back_val >= x);
        if (should_pop)
          dq_.pop_back();
        else
          break;
      }
      dq_.push_back(pos_);
    }

    ++pos_;
    if (count_ < t_) ++count_;
  }

  [[nodiscard]] bool ready() const { return count_ >= t_; }

  [[nodiscard]] FeFloat output() const {
    if (!ready() || nan_count_ > 0)
      return std::numeric_limits<FeFloat>::quiet_NaN();
    if (dq_.empty())
      return std::numeric_limits<FeFloat>::quiet_NaN();
    return ring_[dq_.front() % t_];
  }

 private:
  std::uint32_t t_;
  std::vector<FeFloat> ring_;
  std::uint32_t pos_, count_, nan_count_;
  std::deque<std::uint32_t> dq_;
};

using RollingMaxKernel = RollingExtremalKernel<true>;
using RollingMinKernel = RollingExtremalKernel<false>;

// Layer 2: array-level functions

inline void rolling_max(const FeFloat* x, FeFloat* out, int n, int window) {
  RollingMaxKernel kernel(static_cast<std::uint32_t>(window));
  for (int i = 0; i < n; ++i) { kernel.push(x[i]); out[i] = kernel.output(); }
}

inline void rolling_min(const FeFloat* x, FeFloat* out, int n, int window) {
  RollingMinKernel kernel(static_cast<std::uint32_t>(window));
  for (int i = 0; i < n; ++i) { kernel.push(x[i]); out[i] = kernel.output(); }
}

}  // namespace fe::ops
