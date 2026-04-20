#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <numeric>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

/**
 * Rolling time-series rank via coordinate-compression + Fenwick tree.
 *
 * Aligned with TsRank(x, t) in ts_ops.py:
 *   - t <= 0 → all NaN
 *   - t == 1 → all 0
 *   - otherwise: rank(pct=True) = avg_rank / t, ties get average rank
 *
 * Complexity: O(n log t) total — O(log t) per push via BIT queries.
 *
 * Approach:
 *   1. Pre-sort all n input values to build a global value→index mapping.
 *   2. Maintain a Fenwick tree of size n over these compressed indices.
 *   3. On each push: add new element, evict oldest, query rank of current.
 *      rank = count_less + (count_equal + 1) / 2,  result = rank / t.
 */

namespace detail {

class FenwickTree {
 public:
  explicit FenwickTree(int n) : n_(n), tree_(n + 1, 0) {}

  void update(int i, int delta) {
    for (++i; i <= n_; i += i & (-i))
      tree_[i] += delta;
  }

  int prefix_sum(int i) const {
    int s = 0;
    for (++i; i > 0; i -= i & (-i))
      s += tree_[i];
    return s;
  }

  int range_sum(int lo, int hi) const {
    if (lo > hi) return 0;
    return prefix_sum(hi) - (lo > 0 ? prefix_sum(lo - 1) : 0);
  }

 private:
  int n_;
  std::vector<int> tree_;
};

}  // namespace detail

inline void rolling_rank(const FeFloat* x, FeFloat* out, int n, int window) {
  const auto t = static_cast<std::uint32_t>(window);

  if (t == 0 || n <= 0) {
    for (int i = 0; i < n; ++i)
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
    return;
  }
  if (t == 1) {
    for (int i = 0; i < n; ++i)
      out[i] = fe_is_nan(x[i]) ? std::numeric_limits<FeFloat>::quiet_NaN() : 0.0f;
    return;
  }

  // --- coordinate compression on non-NaN values ---
  std::vector<FeFloat> vals;
  vals.reserve(n);
  for (int i = 0; i < n; ++i) {
    if (!fe_is_nan(x[i])) vals.push_back(x[i]);
  }
  std::sort(vals.begin(), vals.end());
  vals.erase(std::unique(vals.begin(), vals.end()), vals.end());
  const int V = static_cast<int>(vals.size());

  auto compress = [&](FeFloat v) -> int {
    return static_cast<int>(std::lower_bound(vals.begin(), vals.end(), v) - vals.begin());
  };
  auto compress_upper = [&](FeFloat v) -> int {
    return static_cast<int>(std::upper_bound(vals.begin(), vals.end(), v) - vals.begin()) - 1;
  };

  detail::FenwickTree bit(V);
  std::vector<int> comp_idx(n, -1);  // compressed index per position, -1 if NaN
  for (int i = 0; i < n; ++i) {
    if (!fe_is_nan(x[i])) comp_idx[i] = compress(x[i]);
  }

  std::uint32_t nan_count = 0;

  for (int i = 0; i < n; ++i) {
    // add new element
    if (comp_idx[i] >= 0) {
      bit.update(comp_idx[i], 1);
    } else {
      ++nan_count;
    }

    // evict oldest if window full
    if (i >= static_cast<int>(t)) {
      int evict = i - static_cast<int>(t);
      if (comp_idx[evict] >= 0) {
        bit.update(comp_idx[evict], -1);
      } else {
        --nan_count;
      }
    }

    // not enough data or any NaN in window
    if (i + 1 < static_cast<int>(t) || nan_count > 0 || comp_idx[i] < 0) {
      out[i] = std::numeric_limits<FeFloat>::quiet_NaN();
      continue;
    }

    int ci = comp_idx[i];
    int count_less = (ci > 0) ? bit.prefix_sum(ci - 1) : 0;
    int count_equal = bit.range_sum(ci, ci);

    double rank_lo = static_cast<double>(count_less) + 1.0;
    double rank_hi = static_cast<double>(count_less + count_equal);
    double avg_rank = (rank_lo + rank_hi) / 2.0;

    out[i] = static_cast<FeFloat>(avg_rank / static_cast<double>(t));
  }
}

}  // namespace fe::ops
