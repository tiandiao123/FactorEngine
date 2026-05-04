#pragma once

#include <cmath>
#include <cstdint>
#include <limits>

#include "fe/ops/spec.hpp"
#include "fe/ops/rolling_mean.hpp"
#include "fe/ops/rolling_std.hpp"

namespace fe::ops {

/**
 * Rolling coefficient of variation (CV = std / |mean|), ddof=0.
 * Returns NaN when |mean| < kCvMeanEps or window not filled.
 * Requires window >= 2.
 * Delegates to RollingMeanKernel + RollingStdKernel (no per-call allocation).
 */
inline void rolling_cv(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 2 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }

  RollingMeanKernel mean_k(static_cast<std::uint32_t>(window));
  RollingStdKernel  std_k(static_cast<std::uint32_t>(window));

  for (int i = 0; i < n; ++i) {
    mean_k.push(x[i]);
    std_k.push(x[i]);

    FeFloat m = mean_k.output();
    FeFloat s = std_k.output();
    if (fe_is_nan(m) || fe_is_nan(s)) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }
    double abs_m = std::fabs(static_cast<double>(m));
    if (abs_m < kCvMeanEps) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }
    out[i] = static_cast<FeFloat>(static_cast<double>(s) / abs_m);
  }
}

}  // namespace fe::ops
