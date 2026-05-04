#pragma once

#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling coefficient of variation (CV = std / |mean|), ddof=0.
 * Returns NaN when |mean| < eps or window not filled.
 * Requires window >= 2.
 */
inline void rolling_cv(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 2 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  const auto t = static_cast<std::uint32_t>(window);

  std::vector<FeFloat> ring(t);
  double sum = 0, sum_sq = 0;
  std::uint32_t pos = 0, count = 0, nan_count = 0;

  for (int i = 0; i < n; ++i) {
    FeFloat v = x[i];

    if (count >= t) {
      FeFloat old = ring[pos % t];
      if (fe_is_nan(old)) { --nan_count; }
      else { double d = static_cast<double>(old); sum -= d; sum_sq -= d * d; }
    }

    ring[pos % t] = v;
    if (fe_is_nan(v)) { ++nan_count; }
    else { double d = static_cast<double>(v); sum += d; sum_sq += d * d; }

    ++pos;
    if (count < t) ++count;

    if (count < t || nan_count > 0) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }

    double dn = static_cast<double>(t);
    double mean = sum / dn;
    double abs_mean = std::fabs(mean);
    if (abs_mean < 1e-12) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }
    double var = sum_sq / dn - mean * mean;
    if (var < 0.0) var = 0.0;
    out[i] = static_cast<FeFloat>(std::sqrt(var) / abs_mean);
  }
}

}  // namespace fe::ops
