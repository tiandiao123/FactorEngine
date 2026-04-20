#pragma once

#include <cstdint>
#include <deque>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * TsMinMaxDiff(x, t) = rolling_max - rolling_min with min_periods=1.
 *
 * Unlike the standard rolling min/max kernels (min_periods=t), this one
 * starts producing output from the very first element.
 * Aligned with: x.rolling(t, min_periods=1).max() - x.rolling(t, min_periods=1).min()
 */
inline void ts_minmax_diff(const FeFloat* x, FeFloat* out, int n, int window) {
  const auto t = static_cast<std::uint32_t>(window > 0 ? window : 1);
  if (n <= 0) return;

  std::vector<FeFloat> ring(t);
  std::deque<std::uint32_t> dq_max, dq_min;
  std::uint32_t pos = 0, count = 0, nan_count = 0;

  for (int i = 0; i < n; ++i) {
    FeFloat v = x[i];

    if (count >= t) {
      FeFloat old = ring[pos % t];
      if (fe_is_nan(old)) {
        --nan_count;
      } else {
        if (!dq_max.empty() && dq_max.front() == pos - t) dq_max.pop_front();
        if (!dq_min.empty() && dq_min.front() == pos - t) dq_min.pop_front();
      }
    }

    ring[pos % t] = v;

    if (fe_is_nan(v)) {
      ++nan_count;
    } else {
      while (!dq_max.empty()) {
        FeFloat bv = ring[dq_max.back() % t];
        if (bv <= v) dq_max.pop_back(); else break;
      }
      dq_max.push_back(pos);

      while (!dq_min.empty()) {
        FeFloat bv = ring[dq_min.back() % t];
        if (bv >= v) dq_min.pop_back(); else break;
      }
      dq_min.push_back(pos);
    }

    ++pos;
    if (count < t) ++count;

    // min_periods=1: output as soon as we have at least 1 non-NaN
    std::uint32_t valid_count = count - nan_count;
    if (valid_count == 0 || dq_max.empty() || dq_min.empty()) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    } else {
      FeFloat mx = ring[dq_max.front() % t];
      FeFloat mn = ring[dq_min.front() % t];
      out[i] = mx - mn;
    }
  }
}

/**
 * TsMaxDiff(x, t) = x - rolling_max(x, t) with min_periods=1.
 * Always <= 0 for finite x.
 */
inline void ts_max_diff(const FeFloat* x, FeFloat* out, int n, int window) {
  const auto t = static_cast<std::uint32_t>(window > 0 ? window : 1);
  if (n <= 0) return;

  std::vector<FeFloat> ring(t);
  std::deque<std::uint32_t> dq_max;
  std::uint32_t pos = 0, count = 0, nan_count = 0;

  for (int i = 0; i < n; ++i) {
    FeFloat v = x[i];

    if (count >= t) {
      FeFloat old = ring[pos % t];
      if (fe_is_nan(old))
        --nan_count;
      else if (!dq_max.empty() && dq_max.front() == pos - t)
        dq_max.pop_front();
    }

    ring[pos % t] = v;

    if (fe_is_nan(v)) {
      ++nan_count;
    } else {
      while (!dq_max.empty()) {
        FeFloat bv = ring[dq_max.back() % t];
        if (bv <= v) dq_max.pop_back(); else break;
      }
      dq_max.push_back(pos);
    }

    ++pos;
    if (count < t) ++count;

    if (fe_is_nan(v) || dq_max.empty()) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    } else {
      FeFloat mx = ring[dq_max.front() % t];
      out[i] = v - mx;
    }
  }
}

/**
 * TsMinDiff(x, t) = x - rolling_min(x, t) with min_periods=1.
 * Always >= 0 for finite x.
 */
inline void ts_min_diff(const FeFloat* x, FeFloat* out, int n, int window) {
  const auto t = static_cast<std::uint32_t>(window > 0 ? window : 1);
  if (n <= 0) return;

  std::vector<FeFloat> ring(t);
  std::deque<std::uint32_t> dq_min;
  std::uint32_t pos = 0, count = 0, nan_count = 0;

  for (int i = 0; i < n; ++i) {
    FeFloat v = x[i];

    if (count >= t) {
      FeFloat old = ring[pos % t];
      if (fe_is_nan(old))
        --nan_count;
      else if (!dq_min.empty() && dq_min.front() == pos - t)
        dq_min.pop_front();
    }

    ring[pos % t] = v;

    if (fe_is_nan(v)) {
      ++nan_count;
    } else {
      while (!dq_min.empty()) {
        FeFloat bv = ring[dq_min.back() % t];
        if (bv >= v) dq_min.pop_back(); else break;
      }
      dq_min.push_back(pos);
    }

    ++pos;
    if (count < t) ++count;

    if (fe_is_nan(v) || dq_min.empty()) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    } else {
      FeFloat mn = ring[dq_min.front() % t];
      out[i] = v - mn;
    }
  }
}

}  // namespace fe::ops
