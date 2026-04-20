#pragma once

#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * TsWMA(x, t) — linear weighted moving average.
 * Weights = [1, 2, ..., t] (latest element gets weight t).
 * Normalized: w_i / sum(w), where sum = t*(t+1)/2.
 * min_periods = t.
 *
 * Aligned with Python TsWMA: np.dot(window, weights) where
 *   weights = np.arange(1, t+1) / sum(np.arange(1, t+1))
 */
inline void rolling_wma(const FeFloat* x, FeFloat* out, int n, int window) {
  const int t = window > 0 ? window : 1;
  if (n <= 0) return;

  // Precompute normalised weights (double precision for accuracy)
  const double wsum = static_cast<double>(t) * (t + 1) / 2.0;
  std::vector<double> weights(t);
  for (int k = 0; k < t; ++k)
    weights[k] = static_cast<double>(k + 1) / wsum;

  std::vector<FeFloat> ring(t);
  int pos = 0, count = 0;

  for (int i = 0; i < n; ++i) {
    ring[pos % t] = x[i];
    ++pos;
    if (count < t) ++count;

    if (count < t) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }

    // ring[pos % t] is the oldest, ring[(pos-1) % t] is the newest
    // weights[0] (=1/wsum, smallest) goes to oldest, weights[t-1] (=t/wsum) goes to newest
    double acc = 0.0;
    bool has_nan = false;
    for (int k = 0; k < t; ++k) {
      FeFloat val = ring[(pos - t + k) % t];
      if (fe_is_nan(val)) { has_nan = true; break; }
      acc += weights[k] * static_cast<double>(val);
    }

    if (has_nan)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    else
      out[i] = static_cast<FeFloat>(acc);
  }
}

}  // namespace fe::ops
