#pragma once
/**
 * Push-level kernel classes for DAG factor inference.
 *
 * DESIGN: Reuse existing kernel classes from fe/ops/ directly.
 * Only implement new push-level classes where ops/ only has array-level
 * functions (TsRank, Corr, Autocorr, TsSkew, TsMinMaxDiff, P3 ops).
 */

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <vector>

#include "fe/ops/treap_rank.hpp"

// Reuse existing ops — both scalar functions and kernel classes
#include "fe/ops/spec.hpp"
#include "fe/ops/unary.hpp"
#include "fe/ops/binary.hpp"
#include "fe/ops/rolling_mean.hpp"
#include "fe/ops/rolling_sum.hpp"
#include "fe/ops/rolling_std.hpp"
#include "fe/ops/rolling_ema.hpp"
#include "fe/ops/rolling_minmax.hpp"
#include "fe/ops/shift.hpp"

namespace fe::runtime {

using fe::ops::FeFloat;
using fe::ops::fe_is_nan;
using fe::ops::kEps;

static constexpr FeFloat kNaN = std::numeric_limits<FeFloat>::quiet_NaN();

// ═══════════════════════════════════════════════════════════════
//  Composite kernels that build on reused ops/ classes
// ═══════════════════════════════════════════════════════════════

class RollingVarComposite {
public:
    explicit RollingVarComposite(int w) : k_(static_cast<std::uint32_t>(w)) {}
    void push(FeFloat x) { k_.push(x); }
    [[nodiscard]] bool ready()    const { return k_.ready(); }
    [[nodiscard]] FeFloat output() const {
        FeFloat s = k_.output();
        return fe_is_nan(s) ? kNaN : s * s;
    }
    void reset() { k_.reset(); }
private:
    fe::ops::RollingStdKernel k_;
};

class TsDiffComposite {
public:
    explicit TsDiffComposite(int lag) : k_(static_cast<std::uint32_t>(lag)) {}
    void push(FeFloat x) { k_.push(x); }
    [[nodiscard]] bool ready() const { return k_.ready(); }
    [[nodiscard]] FeFloat output() const {
        FeFloat cur = k_.current();
        FeFloat old = k_.output();
        if (fe_is_nan(cur) || fe_is_nan(old)) return kNaN;
        return cur - old;
    }
    void reset() { k_.reset(); }
private:
    fe::ops::DelayKernel k_;
};

class TsPctComposite {
public:
    explicit TsPctComposite(int lag) : k_(static_cast<std::uint32_t>(lag)) {}
    void push(FeFloat x) { k_.push(x); }
    [[nodiscard]] bool ready() const { return k_.ready(); }
    [[nodiscard]] FeFloat output() const {
        FeFloat cur = k_.current();
        FeFloat old = k_.output();
        if (fe_is_nan(cur) || fe_is_nan(old)) return kNaN;
        return cur / (old + kEps) - 1.0f;
    }
    void reset() { k_.reset(); }
private:
    fe::ops::DelayKernel k_;
};

class TsZscoreComposite {
public:
    explicit TsZscoreComposite(int window)
        : mean_k_(static_cast<std::uint32_t>(window)),
          std_k_(static_cast<std::uint32_t>(window)) {}
    void push(FeFloat x) {
        cur_ = x;
        mean_k_.push(x);
        std_k_.push(x);
    }
    [[nodiscard]] bool ready() const { return mean_k_.ready(); }
    [[nodiscard]] FeFloat output() const {
        FeFloat m = mean_k_.output();
        FeFloat s = std_k_.output();
        if (fe_is_nan(m) || fe_is_nan(s) || fe_is_nan(cur_) || s < kEps) return kNaN;
        return (cur_ - m) / (s + kEps);
    }
    void reset() { mean_k_.reset(); std_k_.reset(); cur_ = kNaN; }
private:
    fe::ops::RollingMeanKernel mean_k_;
    fe::ops::RollingStdKernel std_k_;
    FeFloat cur_ = kNaN;
};

// ═══════════════════════════════════════════════════════════════
//  New push-level kernels (no class equivalent in ops/)
// ═══════════════════════════════════════════════════════════════

// --------------- TsRank (push-level, per-step O(t)) ---------------
class TsRankPush {
public:
    explicit TsRankPush(int window)
        : t_(window > 0 ? window : 1), ring_(t_), pos_(0), count_(0), nan_count_(0) {}

    void push(FeFloat x) {
        if (count_ >= t_) {
            FeFloat old = ring_[pos_ % t_];
            if (fe_is_nan(old)) --nan_count_;
        }
        ring_[pos_ % t_] = x;
        if (fe_is_nan(x)) ++nan_count_;
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const { return count_ >= t_; }

    [[nodiscard]] FeFloat output() const {
        if (!ready() || nan_count_ > 0) return kNaN;
        if (t_ == 1) return 0.0f;
        FeFloat cur = ring_[(pos_ - 1) % t_];
        if (fe_is_nan(cur)) return kNaN;
        int less = 0, equal = 0;
        for (int j = 0; j < t_; ++j) {
            FeFloat v = ring_[j];
            if (v < cur) ++less;
            else if (v == cur) ++equal;
        }
        double rank = (static_cast<double>(less) + 1.0 +
                        static_cast<double>(less + equal)) * 0.5;
        return static_cast<FeFloat>(rank / static_cast<double>(t_));
    }
    void reset() { pos_ = 0; count_ = 0; nan_count_ = 0; }

private:
    int t_;
    std::vector<FeFloat> ring_;
    int pos_, count_, nan_count_;
};

// --------------- TreapTsRank (push-level, per-step O(log t)) ------
class TreapTsRankPush {
public:
    explicit TreapTsRankPush(int window)
        : t_(window > 0 ? window : 1), ring_(t_), seq_ring_(t_),
          treap_(t_), pos_(0), count_(0), nan_count_(0), seq_id_(0) {}

    void push(FeFloat x) {
        if (count_ >= t_) {
            int evict_pos = pos_ % t_;
            FeFloat old = ring_[evict_pos];
            if (fe_is_nan(old)) {
                --nan_count_;
            } else {
                treap_.erase(old, seq_ring_[evict_pos]);
            }
        }

        int cur_pos = pos_ % t_;
        ring_[cur_pos] = x;
        seq_ring_[cur_pos] = seq_id_;

        if (fe_is_nan(x)) {
            ++nan_count_;
        } else {
            treap_.insert(x, seq_id_);
        }

        ++seq_id_;
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const { return count_ >= t_; }

    [[nodiscard]] FeFloat output() const {
        if (!ready() || nan_count_ > 0) return kNaN;
        if (t_ == 1) return 0.0f;
        FeFloat cur = ring_[(pos_ - 1) % t_];
        if (fe_is_nan(cur)) return kNaN;

        int less = treap_.count_less(cur);
        int equal = treap_.count_equal(cur);
        double rank = (static_cast<double>(less) + 1.0 +
                       static_cast<double>(less + equal)) * 0.5;
        return static_cast<FeFloat>(rank / static_cast<double>(t_));
    }

    void reset() {
        treap_.clear();
        pos_ = 0; count_ = 0; nan_count_ = 0; seq_id_ = 0;
    }

private:
    int t_;
    std::vector<FeFloat> ring_;
    std::vector<std::uint64_t> seq_ring_;
    mutable fe::ops::TreapOrderStatistic treap_;
    int pos_, count_, nan_count_;
    std::uint64_t seq_id_;
};

// --------------- Corr (rolling Pearson, push-level) ---------------
class CorrPush {
public:
    explicit CorrPush(int window)
        : t_(window > 1 ? window : 2), rx_(t_), ry_(t_),
          pos_(0), count_(0), nan_count_(0),
          sx_(0), sy_(0), sxx_(0), syy_(0), sxy_(0) {}

    void push(FeFloat xv, FeFloat yv) {
        bool cur_nan = fe_is_nan(xv) || fe_is_nan(yv);
        if (count_ >= t_) {
            FeFloat ox = rx_[pos_ % t_], oy = ry_[pos_ % t_];
            bool old_nan = fe_is_nan(ox) || fe_is_nan(oy);
            if (old_nan) { --nan_count_; }
            else {
                double dx = static_cast<double>(ox), dy = static_cast<double>(oy);
                sx_ -= dx; sy_ -= dy; sxx_ -= dx*dx; syy_ -= dy*dy; sxy_ -= dx*dy;
            }
        }
        rx_[pos_ % t_] = xv;
        ry_[pos_ % t_] = yv;
        if (cur_nan) { ++nan_count_; }
        else {
            double dx = static_cast<double>(xv), dy = static_cast<double>(yv);
            sx_ += dx; sy_ += dy; sxx_ += dx*dx; syy_ += dy*dy; sxy_ += dx*dy;
        }
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const { return count_ >= t_ && nan_count_ == 0; }
    [[nodiscard]] FeFloat output() const {
        if (!ready()) return kNaN;
        double dn = static_cast<double>(t_);
        double var_x = dn * sxx_ - sx_ * sx_;
        double var_y = dn * syy_ - sy_ * sy_;
        if (var_x < 0.0) var_x = 0.0;
        if (var_y < 0.0) var_y = 0.0;
        double denom = std::sqrt(var_x * var_y);
        if (denom <= 0.0) return kNaN;
        return static_cast<FeFloat>((dn * sxy_ - sx_ * sy_) / denom);
    }
    void reset() {
        pos_ = 0; count_ = 0; nan_count_ = 0;
        sx_ = sy_ = sxx_ = syy_ = sxy_ = 0.0;
    }

private:
    int t_;
    std::vector<FeFloat> rx_, ry_;
    int pos_, count_, nan_count_;
    double sx_, sy_, sxx_, syy_, sxy_;
};

// --------------- Autocorr (composite: Delay + Corr) ---------------
class AutocorrPush {
public:
    AutocorrPush(int window, int lag)
        : delay_(static_cast<std::uint32_t>(lag)), corr_(window),
          window_(window), lag_(lag) {}

    void push(FeFloat x) {
        delay_.push(x);
        FeFloat lagged = delay_.output();
        corr_.push(x, lagged);
    }

    [[nodiscard]] bool ready() const { return corr_.ready(); }
    [[nodiscard]] FeFloat output() const { return corr_.output(); }
    void reset() { delay_.reset(); corr_.reset(); }

private:
    fe::ops::DelayKernel delay_;
    CorrPush corr_;
    int window_, lag_;
};

// --------------- TsMinMaxDiff (min_periods=1) ---------------
class TsMinMaxDiffPush {
public:
    explicit TsMinMaxDiffPush(int window)
        : t_(window > 0 ? window : 1), ring_(t_), pos_(0), count_(0), nan_count_(0) {}

    void push(FeFloat v) {
        if (count_ >= t_) {
            FeFloat old = ring_[pos_ % t_];
            if (fe_is_nan(old)) { --nan_count_; }
            else {
                if (!dq_max_.empty() && dq_max_.front() == pos_ - t_) dq_max_.pop_front();
                if (!dq_min_.empty() && dq_min_.front() == pos_ - t_) dq_min_.pop_front();
            }
        }
        ring_[pos_ % t_] = v;
        if (fe_is_nan(v)) { ++nan_count_; }
        else {
            while (!dq_max_.empty() && ring_[dq_max_.back() % t_] <= v) dq_max_.pop_back();
            dq_max_.push_back(pos_);
            while (!dq_min_.empty() && ring_[dq_min_.back() % t_] >= v) dq_min_.pop_back();
            dq_min_.push_back(pos_);
        }
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const { return (count_ - nan_count_) > 0; }
    [[nodiscard]] FeFloat output() const {
        if (!ready() || dq_max_.empty() || dq_min_.empty()) return kNaN;
        return ring_[dq_max_.front() % t_] - ring_[dq_min_.front() % t_];
    }
    void reset() { pos_ = 0; count_ = 0; nan_count_ = 0; dq_max_.clear(); dq_min_.clear(); }

private:
    int t_;
    std::vector<FeFloat> ring_;
    int pos_, count_, nan_count_;
    std::deque<int> dq_max_, dq_min_;
};

// --------------- TsSkew (brute-force O(t) per step) ---------------
class TsSkewPush {
public:
    explicit TsSkewPush(int window)
        : t_(window >= 3 ? window : 3), ring_(t_), pos_(0), count_(0),
          sum_(0.0), nan_count_(0) {}

    void push(FeFloat v) {
        if (count_ >= t_) {
            FeFloat old = ring_[pos_ % t_];
            if (fe_is_nan(old)) --nan_count_; else sum_ -= static_cast<double>(old);
        }
        ring_[pos_ % t_] = v;
        if (fe_is_nan(v)) ++nan_count_; else sum_ += static_cast<double>(v);
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const { return count_ >= t_ && nan_count_ == 0; }
    [[nodiscard]] FeFloat output() const {
        if (!ready()) return kNaN;
        double dn = static_cast<double>(t_);
        double mean = sum_ / dn;
        double m2 = 0.0, m3 = 0.0;
        for (int j = 0; j < t_; ++j) {
            double d = static_cast<double>(ring_[j]) - mean;
            double d2 = d * d;
            m2 += d2;
            m3 += d2 * d;
        }
        if (m2 <= 0.0) return 0.0f;
        double m2_32 = m2 * std::sqrt(m2);
        return static_cast<FeFloat>(
            (m3 * dn * std::sqrt(dn - 1.0)) / ((dn - 2.0) * m2_32));
    }
    void reset() { pos_ = 0; count_ = 0; sum_ = 0.0; nan_count_ = 0; }

private:
    int t_;
    std::vector<FeFloat> ring_;
    int pos_, count_;
    double sum_;
    int nan_count_;
};

// --------------- TsMed (rolling median, min_periods=t) ---------------
class TsMedPush {
public:
    explicit TsMedPush(int window)
        : t_(window > 0 ? window : 1), ring_(t_), pos_(0), count_(0) {
        sorted_buf_.reserve(t_);
    }

    void push(FeFloat v) {
        if (count_ >= t_) {
            FeFloat old = ring_[pos_ % t_];
            if (!fe_is_nan(old)) {
                auto it = std::lower_bound(sorted_buf_.begin(), sorted_buf_.end(), old);
                sorted_buf_.erase(it);
            }
        }
        ring_[pos_ % t_] = v;
        if (!fe_is_nan(v)) {
            auto ins = std::lower_bound(sorted_buf_.begin(), sorted_buf_.end(), v);
            sorted_buf_.insert(ins, v);
        }
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const {
        return static_cast<int>(sorted_buf_.size()) >= t_;
    }
    [[nodiscard]] FeFloat output() const {
        if (!ready()) return kNaN;
        return sorted_median(sorted_buf_);
    }
    void reset() { pos_ = 0; count_ = 0; sorted_buf_.clear(); }

private:
    static FeFloat sorted_median(const std::vector<FeFloat>& sb) {
        int sz = static_cast<int>(sb.size());
        if (sz == 0) return kNaN;
        int mid = sz / 2;
        if (sz % 2 == 1) return sb[mid];
        return static_cast<FeFloat>(
            (static_cast<double>(sb[mid - 1]) + static_cast<double>(sb[mid])) * 0.5);
    }

    int t_;
    std::vector<FeFloat> ring_;
    std::vector<FeFloat> sorted_buf_;
    int pos_, count_;
};

// --------------- TsMad (rolling MAD = median(|x - median|)) ----------
class TsMadPush {
public:
    explicit TsMadPush(int window)
        : t_(window > 0 ? window : 1), mp_(std::max(2, t_ / 2)),
          ring_(t_), pos_(0), count_(0) {
        sorted_vals_.reserve(t_);
    }

    void push(FeFloat v) {
        if (count_ >= t_) {
            FeFloat old = ring_[pos_ % t_];
            if (!fe_is_nan(old)) {
                auto it = std::lower_bound(sorted_vals_.begin(), sorted_vals_.end(), old);
                sorted_vals_.erase(it);
            }
        }
        ring_[pos_ % t_] = v;
        if (!fe_is_nan(v)) {
            auto ins = std::lower_bound(sorted_vals_.begin(), sorted_vals_.end(), v);
            sorted_vals_.insert(ins, v);
        }
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const {
        return static_cast<int>(sorted_vals_.size()) >= mp_;
    }
    [[nodiscard]] FeFloat output() const {
        int valid = static_cast<int>(sorted_vals_.size());
        if (valid < mp_) return kNaN;
        FeFloat med = sorted_median(sorted_vals_);
        std::vector<FeFloat> devs;
        devs.reserve(valid);
        for (int j = 0; j < valid; ++j)
            devs.push_back(std::abs(sorted_vals_[j] - med));
        std::sort(devs.begin(), devs.end());
        return sorted_median(devs);
    }
    void reset() { pos_ = 0; count_ = 0; sorted_vals_.clear(); }

private:
    static FeFloat sorted_median(const std::vector<FeFloat>& sb) {
        int sz = static_cast<int>(sb.size());
        if (sz == 0) return kNaN;
        int mid = sz / 2;
        if (sz % 2 == 1) return sb[mid];
        return static_cast<FeFloat>(
            (static_cast<double>(sb[mid - 1]) + static_cast<double>(sb[mid])) * 0.5);
    }

    int t_, mp_;
    std::vector<FeFloat> ring_;
    std::vector<FeFloat> sorted_vals_;
    int pos_, count_;
};

// --------------- TsWMA (linear weighted moving average) ---------------
class TsWmaPush {
public:
    explicit TsWmaPush(int window)
        : t_(window > 0 ? window : 1), ring_(t_), weights_(t_),
          pos_(0), count_(0) {
        double wsum = static_cast<double>(t_) * (t_ + 1) / 2.0;
        for (int k = 0; k < t_; ++k)
            weights_[k] = static_cast<double>(k + 1) / wsum;
    }

    void push(FeFloat x) {
        ring_[pos_ % t_] = x;
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const { return count_ >= t_; }
    [[nodiscard]] FeFloat output() const {
        if (!ready()) return kNaN;
        double acc = 0.0;
        for (int k = 0; k < t_; ++k) {
            FeFloat val = ring_[(pos_ - t_ + k) % t_];
            if (fe_is_nan(val)) return kNaN;
            acc += weights_[k] * static_cast<double>(val);
        }
        return static_cast<FeFloat>(acc);
    }
    void reset() { pos_ = 0; count_ = 0; }

private:
    int t_;
    std::vector<FeFloat> ring_;
    std::vector<double> weights_;
    int pos_, count_;
};

// --------------- TsMaxDiff / TsMinDiff (min_periods=1) ---------------
template <bool IsMax>
class TsExtremalDiffPush {
public:
    explicit TsExtremalDiffPush(int window)
        : t_(window > 0 ? window : 1), ring_(t_), pos_(0), count_(0), nan_count_(0) {}

    void push(FeFloat v) {
        if (count_ >= t_) {
            FeFloat old = ring_[pos_ % t_];
            if (fe_is_nan(old)) { --nan_count_; }
            else if (!dq_.empty() && dq_.front() == pos_ - t_) { dq_.pop_front(); }
        }
        ring_[pos_ % t_] = v;
        if (fe_is_nan(v)) { ++nan_count_; }
        else {
            while (!dq_.empty()) {
                FeFloat bv = ring_[dq_.back() % t_];
                bool pop = IsMax ? (bv <= v) : (bv >= v);
                if (pop) dq_.pop_back(); else break;
            }
            dq_.push_back(pos_);
        }
        ++pos_;
        if (count_ < t_) ++count_;
    }

    [[nodiscard]] bool ready() const { return true; }
    [[nodiscard]] FeFloat output() const {
        FeFloat cur = (pos_ > 0) ? ring_[(pos_ - 1) % t_] : kNaN;
        if (fe_is_nan(cur) || dq_.empty()) return kNaN;
        FeFloat ext = ring_[dq_.front() % t_];
        return cur - ext;
    }
    void reset() { pos_ = 0; count_ = 0; nan_count_ = 0; dq_.clear(); }

private:
    int t_;
    std::vector<FeFloat> ring_;
    int pos_, count_, nan_count_;
    std::deque<int> dq_;
};

using TsMaxDiffPush = TsExtremalDiffPush<true>;
using TsMinDiffPush = TsExtremalDiffPush<false>;

}  // namespace fe::runtime
