#pragma once

#include "fe/ops/spec.hpp"

namespace fe::ops {

// ── Layer 1: scalar kernels ─────────────────────────────────

inline FeFloat add(FeFloat a, FeFloat b) { return a + b; }

inline FeFloat sub(FeFloat a, FeFloat b) { return a - b; }

inline FeFloat mul(FeFloat a, FeFloat b) { return a * b; }

inline FeFloat div_op(FeFloat a, FeFloat b) {
  return a / (b + kEps);
}

// ── Layer 2: array-level functions ──────────────────────────

using BinaryFn = FeFloat (*)(FeFloat, FeFloat);

// array + array
inline void apply_binary_aa(BinaryFn fn, const FeFloat* x, const FeFloat* y,
                            FeFloat* out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = fn(x[i], y[i]);
  }
}

// array + scalar (broadcast)
inline void apply_binary_as(BinaryFn fn, const FeFloat* x, FeFloat scalar,
                            FeFloat* out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = fn(x[i], scalar);
  }
}

// scalar + array (broadcast)
inline void apply_binary_sa(BinaryFn fn, FeFloat scalar, const FeFloat* y,
                            FeFloat* out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = fn(scalar, y[i]);
  }
}

// ── named array functions ───────────────────────────────────

inline void add_aa(const FeFloat* x, const FeFloat* y, FeFloat* out, int n) { apply_binary_aa(add, x, y, out, n); }
inline void add_as(const FeFloat* x, FeFloat s, FeFloat* out, int n) { apply_binary_as(add, x, s, out, n); }
inline void add_sa(FeFloat s, const FeFloat* y, FeFloat* out, int n) { apply_binary_sa(add, s, y, out, n); }

inline void sub_aa(const FeFloat* x, const FeFloat* y, FeFloat* out, int n) { apply_binary_aa(sub, x, y, out, n); }
inline void sub_as(const FeFloat* x, FeFloat s, FeFloat* out, int n) { apply_binary_as(sub, x, s, out, n); }
inline void sub_sa(FeFloat s, const FeFloat* y, FeFloat* out, int n) { apply_binary_sa(sub, s, y, out, n); }

inline void mul_aa(const FeFloat* x, const FeFloat* y, FeFloat* out, int n) { apply_binary_aa(mul, x, y, out, n); }
inline void mul_as(const FeFloat* x, FeFloat s, FeFloat* out, int n) { apply_binary_as(mul, x, s, out, n); }
inline void mul_sa(FeFloat s, const FeFloat* y, FeFloat* out, int n) { apply_binary_sa(mul, s, y, out, n); }

inline void div_aa(const FeFloat* x, const FeFloat* y, FeFloat* out, int n) { apply_binary_aa(div_op, x, y, out, n); }
inline void div_as(const FeFloat* x, FeFloat s, FeFloat* out, int n) { apply_binary_as(div_op, x, s, out, n); }
inline void div_sa(FeFloat s, const FeFloat* y, FeFloat* out, int n) { apply_binary_sa(div_op, s, y, out, n); }

}  // namespace fe::ops
