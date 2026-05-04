#pragma once

#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling SumPos kernel — sum of positive values in a sliding window.
 *
 * Only accumulates x > 0 (strictly positive). NaN is tracked separately.
 * O(1) push, O(1) output.
 * Requires window >= 1.
 */
class SumPosKernel {
 public:
  explicit SumPosKernel(int window)
      : t_(window > 0 ? window : 1),
        ring_(t_), pos_(0), count_(0), nan_count_(0), sum_pos_(0.0) {}

  void reset() {
    pos_ = 0; count_ = 0; nan_count_ = 0; sum_pos_ = 0.0;
  }

  void push(FeFloat x) {
    if (count_ >= t_) {
      FeFloat old = ring_[pos_ % t_];
      if (fe_is_nan(old)) --nan_count_;
      else if (old > 0.0f) sum_pos_ -= static_cast<double>(old);
    }
    ring_[pos_ % t_] = x;
    if (fe_is_nan(x)) ++nan_count_;
    else if (x > 0.0f) sum_pos_ += static_cast<double>(x);
    ++pos_;
    if (count_ < t_) ++count_;
  }

  [[nodiscard]] bool ready() const { return count_ >= t_ && nan_count_ == 0; }

  [[nodiscard]] FeFloat output() const {
    if (!ready()) return std::numeric_limits<FeFloat>::quiet_NaN();
    return static_cast<FeFloat>(sum_pos_);
  }

 private:
  int t_;
  std::vector<FeFloat> ring_;
  int pos_, count_, nan_count_;
  double sum_pos_;
};

/**
 * Rolling PosRatio kernel — fraction of positive values in a sliding window.
 *
 * PosRatio = count(x > 0) / window.
 * O(1) push, O(1) output.
 * Requires window >= 1.
 */
class PosRatioKernel {
 public:
  explicit PosRatioKernel(int window)
      : t_(window > 0 ? window : 1),
        ring_(t_), pos_(0), count_(0), nan_count_(0), pos_count_(0) {}

  void reset() {
    pos_ = 0; count_ = 0; nan_count_ = 0; pos_count_ = 0;
  }

  void push(FeFloat x) {
    if (count_ >= t_) {
      FeFloat old = ring_[pos_ % t_];
      if (fe_is_nan(old)) --nan_count_;
      else if (old > 0.0f) --pos_count_;
    }
    ring_[pos_ % t_] = x;
    if (fe_is_nan(x)) ++nan_count_;
    else if (x > 0.0f) ++pos_count_;
    ++pos_;
    if (count_ < t_) ++count_;
  }

  [[nodiscard]] bool ready() const { return count_ >= t_ && nan_count_ == 0; }

  [[nodiscard]] FeFloat output() const {
    if (!ready()) return std::numeric_limits<FeFloat>::quiet_NaN();
    return static_cast<FeFloat>(static_cast<double>(pos_count_) / static_cast<double>(t_));
  }

 private:
  int t_;
  std::vector<FeFloat> ring_;
  int pos_, count_, nan_count_, pos_count_;
};

/**
 * Rolling UpDownRatio kernel — sum(positive) / sum(|negative|).
 *
 * When sum of negatives is near zero, returns NaN (all-positive window
 * is degenerate for this ratio). O(1) push, O(1) output.
 * Requires window >= 1.
 */
class UpDownRatioKernel {
 public:
  explicit UpDownRatioKernel(int window)
      : t_(window > 0 ? window : 1),
        ring_(t_), pos_(0), count_(0), nan_count_(0),
        sum_pos_(0.0), sum_neg_(0.0) {}

  void reset() {
    pos_ = 0; count_ = 0; nan_count_ = 0;
    sum_pos_ = 0.0; sum_neg_ = 0.0;
  }

  void push(FeFloat x) {
    if (count_ >= t_) {
      FeFloat old = ring_[pos_ % t_];
      if (fe_is_nan(old)) --nan_count_;
      else if (old > 0.0f) sum_pos_ -= static_cast<double>(old);
      else if (old < 0.0f) sum_neg_ -= static_cast<double>(-old);
    }
    ring_[pos_ % t_] = x;
    if (fe_is_nan(x)) ++nan_count_;
    else if (x > 0.0f) sum_pos_ += static_cast<double>(x);
    else if (x < 0.0f) sum_neg_ += static_cast<double>(-x);
    ++pos_;
    if (count_ < t_) ++count_;
  }

  [[nodiscard]] bool ready() const { return count_ >= t_ && nan_count_ == 0; }

  [[nodiscard]] FeFloat output() const {
    if (!ready()) return std::numeric_limits<FeFloat>::quiet_NaN();
    if (sum_neg_ < 1e-15) {
      return (sum_pos_ < 1e-15)
          ? 0.0f
          : std::numeric_limits<FeFloat>::quiet_NaN();
    }
    return static_cast<FeFloat>(sum_pos_ / sum_neg_);
  }

 private:
  int t_;
  std::vector<FeFloat> ring_;
  int pos_, count_, nan_count_;
  double sum_pos_, sum_neg_;
};

// Batch wrappers

inline void rolling_sumpos(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 1 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  SumPosKernel k(window);
  for (int i = 0; i < n; ++i) { k.push(x[i]); out[i] = k.output(); }
}

inline void rolling_posratio(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 1 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  PosRatioKernel k(window);
  for (int i = 0; i < n; ++i) { k.push(x[i]); out[i] = k.output(); }
}

inline void rolling_updown_ratio(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 1 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  UpDownRatioKernel k(window);
  for (int i = 0; i < n; ++i) { k.push(x[i]); out[i] = k.output(); }
}

}  // namespace fe::ops
