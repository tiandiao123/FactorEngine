#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

namespace detail {

inline FeFloat sorted_median(const std::vector<FeFloat>& sb) {
  int sz = static_cast<int>(sb.size());
  if (sz == 0) return std::numeric_limits<FeFloat>::quiet_NaN();
  int mid = sz / 2;
  if (sz % 2 == 1) return sb[mid];
  return static_cast<FeFloat>(
      (static_cast<double>(sb[mid - 1]) + static_cast<double>(sb[mid])) * 0.5);
}

/**
 * Core rolling median with configurable min_periods.
 * Uses sorted buffer with binary-search insert/remove — O(t) per step.
 */
inline void rolling_median_core(const FeFloat* x, FeFloat* out, int n,
                                int window, int min_periods) {
  const int t = window > 0 ? window : 1;
  const int mp = min_periods > 0 ? min_periods : t;
  if (n <= 0) return;

  std::vector<FeFloat> ring(t);
  std::vector<FeFloat> sorted_buf;
  sorted_buf.reserve(t);
  int pos = 0, count = 0;

  for (int i = 0; i < n; ++i) {
    FeFloat v = x[i];

    if (count >= t) {
      FeFloat old = ring[pos % t];
      if (!fe_is_nan(old)) {
        auto it = std::lower_bound(sorted_buf.begin(), sorted_buf.end(), old);
        sorted_buf.erase(it);
      }
    }

    ring[pos % t] = v;
    if (!fe_is_nan(v)) {
      auto ins = std::lower_bound(sorted_buf.begin(), sorted_buf.end(), v);
      sorted_buf.insert(ins, v);
    }

    ++pos;
    if (count < t) ++count;

    int valid = static_cast<int>(sorted_buf.size());
    if (valid < mp) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    } else {
      out[i] = sorted_median(sorted_buf);
    }
  }
}

}  // namespace detail

/**
 * TsMed(x, t) — rolling median with min_periods=t.
 * Aligned with: x.rolling(t, min_periods=t).median()
 */
inline void rolling_median(const FeFloat* x, FeFloat* out, int n, int window) {
  detail::rolling_median_core(x, out, n, window, window);
}

/**
 * TsMad(x, t) — rolling MAD: median(|x - median(x)|).
 * Two-pass: first compute rolling median, then rolling median of |x - med|.
 * Both passes use min_periods = max(2, t/2), matching Python TsMad.
 */
inline void rolling_mad(const FeFloat* x, FeFloat* out, int n, int window) {
  const int t = window > 0 ? window : 1;
  const int mp = std::max(2, t / 2);
  if (n <= 0) return;

  // Pass 1: rolling median
  std::vector<FeFloat> med(n);
  detail::rolling_median_core(x, med.data(), n, t, mp);

  // Compute |x - med|
  std::vector<FeFloat> dev(n);
  for (int i = 0; i < n; ++i) {
    if (fe_is_nan(x[i]) || fe_is_nan(med[i]))
      dev[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    else
      dev[i] = std::abs(x[i] - med[i]);
  }

  // Pass 2: rolling median of deviations
  detail::rolling_median_core(dev.data(), out, n, t, mp);
}

}  // namespace fe::ops
