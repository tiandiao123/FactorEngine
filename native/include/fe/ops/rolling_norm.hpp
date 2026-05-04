#pragma once

#include <cstdint>
#include <deque>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling min-max normalization: (x - min) / (max - min).
 * Maps values to [0, 1] range within the window.
 * Returns NaN when max == min (constant) or window not filled.
 * Requires window >= 2.
 */
inline void rolling_norm(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 2 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  const auto t = static_cast<std::uint32_t>(window);

  RollingExtremalKernel<true>  kmax(t);
  RollingExtremalKernel<false> kmin(t);

  for (int i = 0; i < n; ++i) {
    kmax.push(x[i]);
    kmin.push(x[i]);

    FeFloat mx = kmax.output();
    FeFloat mn = kmin.output();

    if (fe_is_nan(mx) || fe_is_nan(mn) || fe_is_nan(x[i])) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }

    double range = static_cast<double>(mx) - static_cast<double>(mn);
    if (range <= 0.0) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }
    out[i] = static_cast<FeFloat>((static_cast<double>(x[i]) - static_cast<double>(mn)) / range);
  }
}

}  // namespace fe::ops
