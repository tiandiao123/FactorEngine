#pragma once

#include <cmath>
#include <cstdint>

namespace fe::ops {

/** Matches `factorlib.ops.ts_ops` (`EPS`, `DTYPE=float32`). */
inline constexpr float kEps = 1e-8f;

using FeFloat = float;

inline bool fe_is_nan(FeFloat x) { return std::isnan(static_cast<double>(x)); }

/** Bitwise / NaN-aware equality for golden checks. */
inline bool fe_close(FeFloat a, FeFloat b, FeFloat tol) {
  if (fe_is_nan(a) && fe_is_nan(b)) {
    return true;
  }
  if (fe_is_nan(a) || fe_is_nan(b)) {
    return false;
  }
  const double da = static_cast<double>(a);
  const double db = static_cast<double>(b);
  return std::abs(da - db) <= static_cast<double>(tol);
}

}  // namespace fe::ops
