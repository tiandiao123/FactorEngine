#pragma once

#include <cmath>
#include <limits>

#include "fe/ops/spec.hpp"

namespace fe::ops {

// ── Layer 1: scalar kernels ─────────────────────────────────

inline FeFloat neg(FeFloat x) { return -x; }

inline FeFloat abs_op(FeFloat x) { return std::abs(x); }

inline FeFloat log_op(FeFloat x) {
  if (fe_is_nan(x) || x <= 0.0f) {
    return std::numeric_limits<FeFloat>::quiet_NaN();
  }
  return static_cast<FeFloat>(std::log(static_cast<double>(x)));
}

inline FeFloat sqr(FeFloat x) { return x * x; }

inline FeFloat inv(FeFloat x) {
  return 1.0f / (x + kEps);
}

inline FeFloat sign(FeFloat x) {
  if (fe_is_nan(x)) return std::numeric_limits<FeFloat>::quiet_NaN();
  if (x > 0.0f) return 1.0f;
  if (x < 0.0f) return -1.0f;
  return 0.0f;
}

inline FeFloat tanh_op(FeFloat x) {
  if (fe_is_nan(x)) return std::numeric_limits<FeFloat>::quiet_NaN();
  return static_cast<FeFloat>(std::tanh(static_cast<double>(x)));
}

inline FeFloat slog1p(FeFloat x) {
  if (fe_is_nan(x)) return std::numeric_limits<FeFloat>::quiet_NaN();
  double d = static_cast<double>(x);
  return static_cast<FeFloat>(std::copysign(std::log1p(std::abs(d)), d));
}

// ── Layer 2: array-level functions ──────────────────────────

using UnaryFn = FeFloat (*)(FeFloat);

inline void apply_unary(UnaryFn fn, const FeFloat* x, FeFloat* out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = fn(x[i]);
  }
}

inline void neg_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(neg, x, out, n); }
inline void abs_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(abs_op, x, out, n); }
inline void log_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(log_op, x, out, n); }
inline void sqr_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(sqr, x, out, n); }
inline void inv_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(inv, x, out, n); }
inline void sign_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(sign, x, out, n); }
inline void tanh_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(tanh_op, x, out, n); }
inline void slog1p_array(const FeFloat* x, FeFloat* out, int n) { apply_unary(slog1p, x, out, n); }

}  // namespace fe::ops
