#pragma once

#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling Pearson correlation via online sum accumulators.
 * Internal accumulation in double for numerical stability.
 *
 * Aligned with: pandas Series.rolling(t, min_periods=t).corr(other)
 *   corr = (n*Sxy - Sx*Sy) / sqrt((n*Sxx - Sx^2) * (n*Syy - Sy^2))
 *
 * This formulation is algebraically identical to sample Pearson r
 * (the n and (n-1) factors cancel in numerator and denominator).
 */
inline void rolling_corr(const FeFloat* x, const FeFloat* y,
                          FeFloat* out, int n, int window) {
  const auto t = static_cast<std::uint32_t>(window);
  if (t < 2 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }

  std::vector<FeFloat> rx(t), ry(t);
  double sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
  std::uint32_t pos = 0, count = 0, nan_count = 0;

  for (int i = 0; i < n; ++i) {
    FeFloat xv = x[i], yv = y[i];
    bool cur_nan = fe_is_nan(xv) || fe_is_nan(yv);

    if (count >= t) {
      FeFloat ox = rx[pos % t], oy = ry[pos % t];
      bool old_nan = fe_is_nan(ox) || fe_is_nan(oy);
      if (old_nan) {
        --nan_count;
      } else {
        double dx = static_cast<double>(ox), dy = static_cast<double>(oy);
        sx -= dx; sy -= dy;
        sxx -= dx * dx; syy -= dy * dy; sxy -= dx * dy;
      }
    }

    rx[pos % t] = xv;
    ry[pos % t] = yv;

    if (cur_nan) {
      ++nan_count;
    } else {
      double dx = static_cast<double>(xv), dy = static_cast<double>(yv);
      sx += dx; sy += dy;
      sxx += dx * dx; syy += dy * dy; sxy += dx * dy;
    }

    ++pos;
    if (count < t) ++count;

    if (count < t || nan_count > 0) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }

    double dn = static_cast<double>(t);
    double var_x = dn * sxx - sx * sx;
    double var_y = dn * syy - sy * sy;
    double cov = dn * sxy - sx * sy;

    if (var_x < 0.0) var_x = 0.0;
    if (var_y < 0.0) var_y = 0.0;
    double denom = std::sqrt(var_x * var_y);

    double r = cov / denom;
    out[i] = static_cast<FeFloat>(r);
  }
}

/**
 * Rolling autocorrelation: corr(x, x.shift(lag), window).
 *
 * Exact replication of Autocorr(x, t, n) in ts_ops.py:
 *   x0 = x
 *   x1 = x.shift(n)
 *   mean0 = x0.rolling(t).mean()
 *   mean1 = x1.rolling(t).mean()
 *   cov   = ((x0 - mean0) * (x1 - mean1)).rolling(t).mean()
 *   std0  = x0.rolling(t).std()       # ddof=1
 *   std1  = x1.rolling(t).std()       # ddof=1
 *   out   = cov / (std0 * std1), NaN where std0<=0 or std1<=0
 *
 * This is a double-rolling computation (rolling on products of rolling results).
 */
inline void autocorr(const FeFloat* x, FeFloat* out, int n,
                     int window, int lag) {
  if (window < 2 || lag < 1 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }

  const int t = window;
  const auto nan = std::numeric_limits<FeFloat>::quiet_NaN();

  // Step 1: build x1 = x.shift(lag)
  std::vector<double> x0d(n), x1d(n);
  for (int i = 0; i < n; ++i)
    x0d[i] = static_cast<double>(x[i]);
  for (int i = 0; i < lag && i < n; ++i)
    x1d[i] = std::numeric_limits<double>::quiet_NaN();
  for (int i = lag; i < n; ++i)
    x1d[i] = x0d[i - lag];

  // Step 2: rolling mean of x0 and x1
  std::vector<double> mean0(n), mean1(n);
  {
    double s = 0;
    for (int i = 0; i < n; ++i) {
      s += x0d[i];
      if (i >= t) s -= x0d[i - t];
      mean0[i] = (i >= t - 1) ? s / t : std::numeric_limits<double>::quiet_NaN();
    }
  }
  {
    double s = 0;
    int valid = 0;
    for (int i = 0; i < n; ++i) {
      if (!std::isnan(x1d[i])) { s += x1d[i]; ++valid; }
      if (i >= t) {
        if (!std::isnan(x1d[i - t])) { s -= x1d[i - t]; --valid; }
      }
      int wsize = (i < t) ? (i + 1) : t;
      mean1[i] = (valid >= t) ? s / valid : std::numeric_limits<double>::quiet_NaN();
    }
  }

  // Step 3: product = (x0 - mean0) * (x1 - mean1)
  std::vector<double> prod(n);
  for (int i = 0; i < n; ++i) {
    if (std::isnan(mean0[i]) || std::isnan(mean1[i]) || std::isnan(x1d[i]))
      prod[i] = std::numeric_limits<double>::quiet_NaN();
    else
      prod[i] = (x0d[i] - mean0[i]) * (x1d[i] - mean1[i]);
  }

  // Step 4: cov = rolling mean of product (min_periods=t)
  std::vector<double> cov(n);
  {
    double s = 0;
    int valid = 0;
    for (int i = 0; i < n; ++i) {
      if (!std::isnan(prod[i])) { s += prod[i]; ++valid; }
      if (i >= t) {
        if (!std::isnan(prod[i - t])) { s -= prod[i - t]; --valid; }
      }
      cov[i] = (valid >= t) ? s / valid : std::numeric_limits<double>::quiet_NaN();
    }
  }

  // Step 5: std0 and std1 (ddof=1)
  std::vector<double> std0(n), std1(n);
  {
    double s = 0, s2 = 0;
    for (int i = 0; i < n; ++i) {
      s += x0d[i]; s2 += x0d[i] * x0d[i];
      if (i >= t) { s -= x0d[i - t]; s2 -= x0d[i - t] * x0d[i - t]; }
      if (i >= t - 1) {
        double m = s / t;
        double var = s2 / t - m * m;
        if (var < 0) var = 0;
        std0[i] = std::sqrt(var * t / (t - 1.0));
      } else {
        std0[i] = std::numeric_limits<double>::quiet_NaN();
      }
    }
  }
  {
    double s = 0, s2 = 0;
    int valid = 0;
    for (int i = 0; i < n; ++i) {
      if (!std::isnan(x1d[i])) { s += x1d[i]; s2 += x1d[i] * x1d[i]; ++valid; }
      if (i >= t) {
        if (!std::isnan(x1d[i - t])) { s -= x1d[i - t]; s2 -= x1d[i - t] * x1d[i - t]; --valid; }
      }
      if (valid >= t) {
        double m = s / valid;
        double var = s2 / valid - m * m;
        if (var < 0) var = 0;
        std1[i] = std::sqrt(var * valid / (valid - 1.0));
      } else {
        std1[i] = std::numeric_limits<double>::quiet_NaN();
      }
    }
  }

  // Step 6: out = cov / (std0 * std1), NaN where std<=0
  for (int i = 0; i < n; ++i) {
    if (std::isnan(cov[i]) || std::isnan(std0[i]) || std::isnan(std1[i])
        || std0[i] <= 0.0 || std1[i] <= 0.0) {
      out[i] = nan;
    } else {
      out[i] = static_cast<FeFloat>(cov[i] / (std0[i] * std1[i]));
    }
  }
}

}  // namespace fe::ops
