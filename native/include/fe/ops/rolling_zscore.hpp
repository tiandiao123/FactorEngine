#pragma once

#include <cstdint>
#include <limits>

#include "fe/ops/spec.hpp"
#include "fe/ops/rolling_mean.hpp"
#include "fe/ops/rolling_std.hpp"

namespace fe::ops {

/**
 * Rolling z-score: (x - Ma(x,t)) / (TsStd(x,t) + EPS)
 * Output NaN when |std| < EPS or during warmup.
 */
inline void rolling_zscore(const FeFloat* x, FeFloat* out, int n, int window) {
  RollingMeanKernel mean_k(static_cast<std::uint32_t>(window));
  RollingStdKernel  std_k(static_cast<std::uint32_t>(window));

  for (int i = 0; i < n; ++i) {
    mean_k.push(x[i]);
    std_k.push(x[i]);

    FeFloat m = mean_k.output();
    FeFloat s = std_k.output();

    if (fe_is_nan(m) || fe_is_nan(s) || fe_is_nan(x[i]) || s < kEps) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    } else {
      out[i] = (x[i] - m) / (s + kEps);
    }
  }
}

}  // namespace fe::ops
