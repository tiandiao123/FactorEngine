#pragma once

#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling sample skewness via brute-force recomputation from ring buffer.
 * Internal accumulation in double for numerical stability.
 *
 * Aligned with: pandas rolling(t, min_periods=t).skew()
 *
 * pandas uses the adjusted Fisher-Pearson standardized moment:
 *   G1 = [n / ((n-1)(n-2))] * sum[((xi - mean) / s)^3]
 * where s = sample std (ddof=1).
 *
 * For numerical stability we recompute centered moments from the ring buffer
 * each step, avoiding catastrophic cancellation in raw-moment formulas.
 */
inline void rolling_skew(const FeFloat* x, FeFloat* out, int n, int window) {
  const auto t = static_cast<std::uint32_t>(window);
  if (t < 3 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }

  std::vector<FeFloat> ring(t);
  std::uint32_t pos = 0, count = 0, nan_count = 0;
  double sum_ = 0.0;

  for (int i = 0; i < n; ++i) {
    FeFloat v = x[i];

    if (count >= t) {
      FeFloat old = ring[pos % t];
      if (fe_is_nan(old)) --nan_count;
      else sum_ -= static_cast<double>(old);
    }

    ring[pos % t] = v;
    if (fe_is_nan(v)) ++nan_count;
    else sum_ += static_cast<double>(v);

    ++pos;
    if (count < t) ++count;

    if (count < t || nan_count > 0) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }

    double dn = static_cast<double>(t);
    double mean = sum_ / dn;

    double m2 = 0.0, m3 = 0.0;
    for (std::uint32_t j = 0; j < t; ++j) {
      double d = static_cast<double>(ring[j]) - mean;
      double d2 = d * d;
      m2 += d2;
      m3 += d2 * d;
    }

    if (m2 <= 0.0) {
      out[i] = 0.0f;
      continue;
    }

    // pandas adjusted Fisher-Pearson:
    //   G1 = (m3/n) / (m2/n)^(3/2) * sqrt(n*(n-1)) / (n-2)
    //      = m3 * n * sqrt(n-1) / ((n-2) * m2^(3/2))
    double m2_32 = m2 * std::sqrt(m2);
    double skew = (m3 * dn * std::sqrt(dn - 1.0)) / ((dn - 2.0) * m2_32);
    out[i] = static_cast<FeFloat>(skew);
  }
}

}  // namespace fe::ops
