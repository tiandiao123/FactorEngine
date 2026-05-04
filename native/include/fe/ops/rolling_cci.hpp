#pragma once

#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling CCI (Commodity Channel Index) kernel.
 *
 * CCI = (TP - SMA(TP, w)) / (0.015 * MAD(TP, w))
 *
 * where TP = typical price (fed as single input),
 *       MAD = mean absolute deviation = (1/w) * Σ|TP_i - SMA|.
 *
 * push() is O(1), output() is O(w) due to MAD computation.
 * Requires window >= 2.
 *
 * Lambert's constant 0.015 normalizes CCI so ~70-80% of values
 * fall within [-100, +100].
 */
class CciKernel {
 public:
  explicit CciKernel(int window)
      : t_(window >= 2 ? window : 2),
        ring_(t_),
        pos_(0), count_(0), nan_count_(0), sum_(0.0) {}

  void reset() {
    pos_ = 0; count_ = 0; nan_count_ = 0; sum_ = 0.0;
  }

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

  [[nodiscard]] bool ready() const { return count_ >= t_ && nan_count_ == 0; }

  [[nodiscard]] FeFloat output() const {
    if (!ready()) return std::numeric_limits<FeFloat>::quiet_NaN();

    double dn = static_cast<double>(t_);
    double mean = sum_ / dn;

    double mad = 0.0;
    for (int j = 0; j < t_; ++j) {
      mad += std::fabs(static_cast<double>(ring_[j]) - mean);
    }
    mad /= dn;

    if (mad < kCciMadEps) return static_cast<FeFloat>(0.0);

    FeFloat cur = ring_[(pos_ - 1) % t_];
    double cci = (static_cast<double>(cur) - mean) / (kCciLambert * mad);
    return static_cast<FeFloat>(cci);
  }

 private:
  int t_;
  std::vector<FeFloat> ring_;
  int pos_, count_, nan_count_;
  double sum_;
};

inline void rolling_cci(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 2 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  CciKernel k(window);
  for (int i = 0; i < n; ++i) { k.push(x[i]); out[i] = k.output(); }
}

}  // namespace fe::ops
