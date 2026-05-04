#pragma once

#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling excess kurtosis via brute-force recomputation from ring buffer.
 * Internal accumulation in double for numerical stability.
 *
 * Aligned with: pandas rolling(t, min_periods=t).kurt()
 *
 * pandas uses the adjusted Fisher kurtosis (excess, i.e. normal → 0):
 *
 *   K = [n(n+1) / ((n-1)(n-2)(n-3))] * Σ[((xi-mean)/s)^4]
 *       - 3(n-1)^2 / ((n-2)(n-3))
 *
 * where s = sample std (ddof=1), i.e. s^2 = m2 / (n-1).
 *
 * Rewriting in terms of raw centered moments m2 = Σ(xi-mean)^2
 * and m4 = Σ(xi-mean)^4:
 *
 *   K = [n(n+1)(n-1) / ((n-2)(n-3))] * (m4 / m2^2)
 *       - 3(n-1)^2 / ((n-2)(n-3))
 *
 * Requires window >= 4.
 */
inline void rolling_kurt(const FeFloat* x, FeFloat* out, int n, int window) {
  if (window < 4 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  const auto t = static_cast<std::uint32_t>(window);

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

    double m2 = 0.0, m4 = 0.0;
    for (std::uint32_t j = 0; j < t; ++j) {
      double d = static_cast<double>(ring[j]) - mean;
      double d2 = d * d;
      m2 += d2;
      m4 += d2 * d2;
    }

    if (m2 <= 0.0) {
      out[i] = 0.0f;
      continue;
    }

    // pandas adjusted Fisher excess kurtosis:
    //   K = [n(n+1)(n-1) / ((n-2)(n-3))] * (m4 / m2^2)
    //       - 3(n-1)^2 / ((n-2)(n-3))
    double n1 = dn - 1.0;
    double n2 = dn - 2.0;
    double n3 = dn - 3.0;
    double kurt = (dn * (dn + 1.0) * n1 / (n2 * n3)) * (m4 / (m2 * m2))
                  - 3.0 * n1 * n1 / (n2 * n3);
    out[i] = static_cast<FeFloat>(kurt);
  }
}

}  // namespace fe::ops
