#pragma once
/**
 * FactorGraph — a compiled, push-based DAG for a single factor.
 *
 * Usage:
 *   FactorGraph g;
 *   int close = g.add_input("close");
 *   int ma    = g.add_rolling(Op::MA, close, 120);
 *   int dev   = g.add_binary(Op::SUB, close, ma);
 *   int vol   = g.add_rolling(Op::TS_STD, close, 60);
 *   int sig   = g.add_binary(Op::DIV, dev, vol);
 *   g.compile();
 *
 *   // per tick:
 *   g.push_bar(close_val, volume_val, open_val, high_val, low_val, ret_val);
 *   if (g.ready()) { float signal = g.output(); }
 */

#include <cassert>
#include <cmath>
#include <memory>
#include <string>
#include <variant>
#include <vector>

#include "fe/runtime/kernels.hpp"

namespace fe::runtime {

// ── Op enum ─────────────────────────────────────────────────────

enum class Op {
    // Inputs
    INPUT_CLOSE, INPUT_VOLUME, INPUT_OPEN, INPUT_HIGH, INPUT_LOW, INPUT_RET,

    // P0 unary (stateless)
    NEG, ABS, LOG, SQR, INV, SIGN, TANH, SLOG1P,

    // P0 binary (stateless)
    ADD, SUB, MUL, DIV,

    // P0 scalar ops: op(node, scalar) or op(scalar, node)
    ADD_SCALAR, SUB_SCALAR, MUL_SCALAR, DIV_SCALAR,
    SCALAR_SUB, SCALAR_DIV,

    // P1 rolling (single-input, stateful)
    MA, TS_SUM, TS_STD, TS_VARI, EMA,
    TS_MIN, TS_MAX, TS_RANK, TS_ZSCORE,
    DELAY, TS_DIFF, TS_PCT,

    // P2 bivariate / special
    CORR, AUTOCORR, TS_MINMAX_DIFF, TS_SKEW,

    // P3 complex
    TS_MED, TS_MAD, TS_WMA, TS_MAX_DIFF, TS_MIN_DIFF,

    // Optimized variants
    TREAP_TS_RANK,  // O(log t) TsRank via Treap order-statistic tree

    // Derived
    PCT_CHANGE,  // (x - delay(x,1)) / (delay(x,1) + eps)
};

// ── Kernel variant ──────────────────────────────────────────────
// All stateful push-level kernels packed into a variant.
// P0 stateless ops use std::monostate — their logic is inline in push_bar()
// via direct calls to fe::ops:: scalar functions.

using KernelVar = std::variant<
    std::monostate,                         // P0 stateless ops
    fe::ops::RollingMeanKernel,             // directly from ops/
    fe::ops::RollingSumKernel,              // directly from ops/
    fe::ops::RollingStdKernel,              // directly from ops/
    RollingVarComposite,                    // composite: Std → square
    fe::ops::EmaKernel,                     // directly from ops/
    fe::ops::RollingMinKernel,              // directly from ops/
    fe::ops::RollingMaxKernel,              // directly from ops/
    TsRankPush,                             // new: brute-force O(t)
    TsZscoreComposite,                      // composite: Mean + Std
    fe::ops::DelayKernel,                   // directly from ops/
    TsDiffComposite,                        // composite: Delay
    TsPctComposite,                         // composite: Delay
    CorrPush,                               // new: push-level Pearson
    AutocorrPush,                           // composite: Delay + Corr
    TsMinMaxDiffPush,                       // new: dual monotonic deque
    TsSkewPush,                             // new: brute-force O(t)
    TsMedPush,                              // new: sorted buffer
    TsMadPush,                              // new: two-pass median
    TsWmaPush,                              // new: weighted average
    TsMaxDiffPush,                          // new: monotonic deque
    TsMinDiffPush,                          // new: monotonic deque
    TreapTsRankPush                         // O(log t) TsRank via Treap
>;

// ── FactorNode ──────────────────────────────────────────────────

struct FactorNode {
    Op op;
    int input_a = -1;     // index into values_[]
    int input_b = -1;     // for binary / bivariate ops
    int window  = 0;
    float scalar = 0.0f;  // for scalar ops
    KernelVar kernel;     // holds the stateful kernel (or monostate)
};

// ── FactorGraph ─────────────────────────────────────────────────

class FactorGraph {
public:
    FactorGraph() = default;

    // ── Build API (call before compile) ──────────────────────────

    int add_input(const std::string& feature) {
        FactorNode node;
        if      (feature == "close")  node.op = Op::INPUT_CLOSE;
        else if (feature == "volume") node.op = Op::INPUT_VOLUME;
        else if (feature == "open")   node.op = Op::INPUT_OPEN;
        else if (feature == "high")   node.op = Op::INPUT_HIGH;
        else if (feature == "low")    node.op = Op::INPUT_LOW;
        else if (feature == "ret")    node.op = Op::INPUT_RET;
        else                          node.op = Op::INPUT_CLOSE;
        nodes_.push_back(std::move(node));
        return static_cast<int>(nodes_.size()) - 1;
    }

    int add_unary(Op op, int src) {
        FactorNode node;
        node.op = op;
        node.input_a = src;
        nodes_.push_back(std::move(node));
        return static_cast<int>(nodes_.size()) - 1;
    }

    int add_binary(Op op, int src_a, int src_b) {
        FactorNode node;
        node.op = op;
        node.input_a = src_a;
        node.input_b = src_b;
        nodes_.push_back(std::move(node));
        return static_cast<int>(nodes_.size()) - 1;
    }

    int add_rolling(Op op, int src, int window) {
        FactorNode node;
        node.op = op;
        node.input_a = src;
        node.window = window;
        nodes_.push_back(std::move(node));
        return static_cast<int>(nodes_.size()) - 1;
    }

    int add_bivariate(Op op, int src_a, int src_b, int window) {
        FactorNode node;
        node.op = op;
        node.input_a = src_a;
        node.input_b = src_b;
        node.window = window;
        nodes_.push_back(std::move(node));
        return static_cast<int>(nodes_.size()) - 1;
    }

    int add_scalar_op(Op op, int src, float scalar) {
        FactorNode node;
        node.op = op;
        node.input_a = src;
        node.scalar = scalar;
        nodes_.push_back(std::move(node));
        return static_cast<int>(nodes_.size()) - 1;
    }

    int add_autocorr(int src, int window, int lag) {
        FactorNode node;
        node.op = Op::AUTOCORR;
        node.input_a = src;
        node.window = window;
        node.scalar = static_cast<float>(lag);
        nodes_.push_back(std::move(node));
        return static_cast<int>(nodes_.size()) - 1;
    }

    // ── Compile ──────────────────────────────────────────────────

    void compile() {
        int n = static_cast<int>(nodes_.size());
        values_.assign(n, kNaN);
        output_node_ = n - 1;

        for (auto& nd : nodes_) {
            nd.kernel = allocate_kernel(nd);
        }

        std::vector<int> warmup(n, 0);
        for (int i = 0; i < n; ++i) {
            int node_w = node_window(nodes_[i]);
            int max_dep = 0;
            if (nodes_[i].input_a >= 0) max_dep = std::max(max_dep, warmup[nodes_[i].input_a]);
            if (nodes_[i].input_b >= 0) max_dep = std::max(max_dep, warmup[nodes_[i].input_b]);
            warmup[i] = max_dep + node_w;
        }
        warmup_bars_ = warmup[output_node_];
        bars_seen_ = 0;
        compiled_ = true;
    }

    // ── Runtime API ──────────────────────────────────────────────

    void push_bar(FeFloat close, FeFloat volume = kNaN,
                  FeFloat open = kNaN, FeFloat high = kNaN,
                  FeFloat low = kNaN, FeFloat ret = kNaN) {
        assert(compiled_ && "Must call compile() before push_bar()");
        int n = static_cast<int>(nodes_.size());

        for (int i = 0; i < n; ++i) {
            auto& nd = nodes_[i];
            FeFloat a = (nd.input_a >= 0) ? values_[nd.input_a] : 0.0f;
            FeFloat b = (nd.input_b >= 0) ? values_[nd.input_b] : 0.0f;

            switch (nd.op) {
            // ── Inputs ──
            case Op::INPUT_CLOSE:  values_[i] = close;  break;
            case Op::INPUT_VOLUME: values_[i] = volume;  break;
            case Op::INPUT_OPEN:   values_[i] = open;    break;
            case Op::INPUT_HIGH:   values_[i] = high;    break;
            case Op::INPUT_LOW:    values_[i] = low;     break;
            case Op::INPUT_RET:    values_[i] = ret;     break;

            // ── P0 unary — direct calls to fe::ops scalar functions ──
            case Op::NEG:    values_[i] = fe::ops::neg(a);     break;
            case Op::ABS:    values_[i] = fe::ops::abs_op(a);  break;
            case Op::LOG:    values_[i] = fe::ops::log_op(a);  break;
            case Op::SQR:    values_[i] = fe::ops::sqr(a);     break;
            case Op::INV:    values_[i] = fe::ops::inv(a);     break;
            case Op::SIGN:   values_[i] = fe::ops::sign(a);    break;
            case Op::TANH:   values_[i] = fe::ops::tanh_op(a); break;
            case Op::SLOG1P: values_[i] = fe::ops::slog1p(a);  break;

            // ── P0 binary — direct calls to fe::ops scalar functions ──
            case Op::ADD: values_[i] = fe::ops::add(a, b);    break;
            case Op::SUB: values_[i] = fe::ops::sub(a, b);    break;
            case Op::MUL: values_[i] = fe::ops::mul(a, b);    break;
            case Op::DIV: values_[i] = fe::ops::div_op(a, b); break;

            // ── P0 scalar ──
            case Op::ADD_SCALAR: values_[i] = fe::ops::add(a, nd.scalar); break;
            case Op::SUB_SCALAR: values_[i] = fe::ops::sub(a, nd.scalar); break;
            case Op::MUL_SCALAR: values_[i] = fe::ops::mul(a, nd.scalar); break;
            case Op::DIV_SCALAR: values_[i] = fe::ops::div_op(a, nd.scalar); break;
            case Op::SCALAR_SUB: values_[i] = fe::ops::sub(nd.scalar, a); break;
            case Op::SCALAR_DIV: values_[i] = fe::ops::div_op(nd.scalar, a); break;

            // ── P1 rolling — directly using ops/ kernel classes ──
            case Op::MA:       { auto& k = std::get<fe::ops::RollingMeanKernel>(nd.kernel); k.push(a); values_[i] = k.output(); break; }
            case Op::TS_SUM:   { auto& k = std::get<fe::ops::RollingSumKernel>(nd.kernel);  k.push(a); values_[i] = k.output(); break; }
            case Op::TS_STD:   { auto& k = std::get<fe::ops::RollingStdKernel>(nd.kernel);  k.push(a); values_[i] = k.output(); break; }
            case Op::TS_VARI:  { auto& k = std::get<RollingVarComposite>(nd.kernel);        k.push(a); values_[i] = k.output(); break; }
            case Op::EMA:      { auto& k = std::get<fe::ops::EmaKernel>(nd.kernel);         k.push(a); values_[i] = k.output(); break; }
            case Op::TS_MIN:   { auto& k = std::get<fe::ops::RollingMinKernel>(nd.kernel);  k.push(a); values_[i] = k.output(); break; }
            case Op::TS_MAX:   { auto& k = std::get<fe::ops::RollingMaxKernel>(nd.kernel);  k.push(a); values_[i] = k.output(); break; }
            case Op::TS_RANK:       { auto& k = std::get<TsRankPush>(nd.kernel);            k.push(a); values_[i] = k.output(); break; }
            case Op::TREAP_TS_RANK: { auto& k = std::get<TreapTsRankPush>(nd.kernel);      k.push(a); values_[i] = k.output(); break; }
            case Op::TS_ZSCORE:{ auto& k = std::get<TsZscoreComposite>(nd.kernel);          k.push(a); values_[i] = k.output(); break; }
            case Op::DELAY:    { auto& k = std::get<fe::ops::DelayKernel>(nd.kernel);       k.push(a); values_[i] = k.output(); break; }
            case Op::TS_DIFF:  { auto& k = std::get<TsDiffComposite>(nd.kernel);            k.push(a); values_[i] = k.output(); break; }
            case Op::TS_PCT:   { auto& k = std::get<TsPctComposite>(nd.kernel);             k.push(a); values_[i] = k.output(); break; }
            case Op::PCT_CHANGE: { auto& k = std::get<TsPctComposite>(nd.kernel);           k.push(a); values_[i] = k.output(); break; }

            // ── P2 bivariate / special ──
            case Op::CORR:     { auto& k = std::get<CorrPush>(nd.kernel);            k.push(a, b); values_[i] = k.output(); break; }
            case Op::AUTOCORR: { auto& k = std::get<AutocorrPush>(nd.kernel);        k.push(a);    values_[i] = k.output(); break; }
            case Op::TS_MINMAX_DIFF: { auto& k = std::get<TsMinMaxDiffPush>(nd.kernel); k.push(a); values_[i] = k.output(); break; }
            case Op::TS_SKEW:  { auto& k = std::get<TsSkewPush>(nd.kernel);          k.push(a); values_[i] = k.output(); break; }

            // ── P3 complex ──
            case Op::TS_MED:      { auto& k = std::get<TsMedPush>(nd.kernel);        k.push(a); values_[i] = k.output(); break; }
            case Op::TS_MAD:      { auto& k = std::get<TsMadPush>(nd.kernel);        k.push(a); values_[i] = k.output(); break; }
            case Op::TS_WMA:      { auto& k = std::get<TsWmaPush>(nd.kernel);        k.push(a); values_[i] = k.output(); break; }
            case Op::TS_MAX_DIFF: { auto& k = std::get<TsMaxDiffPush>(nd.kernel);    k.push(a); values_[i] = k.output(); break; }
            case Op::TS_MIN_DIFF: { auto& k = std::get<TsMinDiffPush>(nd.kernel);    k.push(a); values_[i] = k.output(); break; }
            }
        }

        ++bars_seen_;
    }

    [[nodiscard]] bool ready() const { return bars_seen_ >= warmup_bars_; }

    [[nodiscard]] FeFloat output() const {
        FeFloat v = values_[output_node_];
        if (std::isinf(v) || std::isnan(v)) return 0.0f;
        return v;
    }

    [[nodiscard]] FeFloat raw_output() const { return values_[output_node_]; }

    [[nodiscard]] int warmup_bars() const { return warmup_bars_; }
    [[nodiscard]] int bars_seen() const { return bars_seen_; }
    [[nodiscard]] int num_nodes() const { return static_cast<int>(nodes_.size()); }

    void reset() {
        for (auto& nd : nodes_) {
            nd.kernel = allocate_kernel(nd);
        }
        std::fill(values_.begin(), values_.end(), kNaN);
        bars_seen_ = 0;
    }

    struct NodeInfo {
        int id;
        std::string op_name;
        int input_a;
        int input_b;
        int window;
        float scalar;
        bool is_output;
    };

    [[nodiscard]] std::vector<NodeInfo> describe() const {
        std::vector<NodeInfo> info;
        int n = static_cast<int>(nodes_.size());
        for (int i = 0; i < n; ++i) {
            const auto& nd = nodes_[i];
            info.push_back({i, op_name(nd.op),
                            nd.input_a, nd.input_b,
                            nd.window, nd.scalar,
                            i == output_node_});
        }
        return info;
    }

private:
    static std::string op_name(Op op) {
        switch (op) {
        case Op::INPUT_CLOSE:  return "INPUT_CLOSE";
        case Op::INPUT_VOLUME: return "INPUT_VOLUME";
        case Op::INPUT_OPEN:   return "INPUT_OPEN";
        case Op::INPUT_HIGH:   return "INPUT_HIGH";
        case Op::INPUT_LOW:    return "INPUT_LOW";
        case Op::INPUT_RET:    return "INPUT_RET";
        case Op::NEG:     return "NEG";
        case Op::ABS:     return "ABS";
        case Op::LOG:     return "LOG";
        case Op::SQR:     return "SQR";
        case Op::INV:     return "INV";
        case Op::SIGN:    return "SIGN";
        case Op::TANH:    return "TANH";
        case Op::SLOG1P:  return "SLOG1P";
        case Op::ADD:     return "ADD";
        case Op::SUB:     return "SUB";
        case Op::MUL:     return "MUL";
        case Op::DIV:     return "DIV";
        case Op::ADD_SCALAR: return "ADD_SCALAR";
        case Op::SUB_SCALAR: return "SUB_SCALAR";
        case Op::MUL_SCALAR: return "MUL_SCALAR";
        case Op::DIV_SCALAR: return "DIV_SCALAR";
        case Op::SCALAR_SUB: return "SCALAR_SUB";
        case Op::SCALAR_DIV: return "SCALAR_DIV";
        case Op::MA:       return "MA";
        case Op::TS_SUM:   return "TS_SUM";
        case Op::TS_STD:   return "TS_STD";
        case Op::TS_VARI:  return "TS_VARI";
        case Op::EMA:      return "EMA";
        case Op::TS_MIN:   return "TS_MIN";
        case Op::TS_MAX:   return "TS_MAX";
        case Op::TS_RANK:       return "TS_RANK";
        case Op::TREAP_TS_RANK: return "TREAP_TS_RANK";
        case Op::TS_ZSCORE:return "TS_ZSCORE";
        case Op::DELAY:    return "DELAY";
        case Op::TS_DIFF:  return "TS_DIFF";
        case Op::TS_PCT:   return "TS_PCT";
        case Op::PCT_CHANGE: return "PCT_CHANGE";
        case Op::CORR:     return "CORR";
        case Op::AUTOCORR: return "AUTOCORR";
        case Op::TS_MINMAX_DIFF: return "TS_MINMAX_DIFF";
        case Op::TS_SKEW:  return "TS_SKEW";
        case Op::TS_MED:   return "TS_MED";
        case Op::TS_MAD:   return "TS_MAD";
        case Op::TS_WMA:   return "TS_WMA";
        case Op::TS_MAX_DIFF: return "TS_MAX_DIFF";
        case Op::TS_MIN_DIFF: return "TS_MIN_DIFF";
        default:           return "UNKNOWN";
        }
    }

    std::vector<FactorNode> nodes_;
    std::vector<FeFloat> values_;
    int output_node_ = 0;
    int warmup_bars_ = 0;
    int bars_seen_ = 0;
    bool compiled_ = false;

    static int node_window(const FactorNode& nd) {
        switch (nd.op) {
        case Op::MA: case Op::TS_SUM: case Op::TS_STD: case Op::TS_VARI:
        case Op::EMA: case Op::TS_MIN: case Op::TS_MAX:
        case Op::TS_RANK: case Op::TREAP_TS_RANK: case Op::TS_ZSCORE:
        case Op::CORR: case Op::TS_MINMAX_DIFF: case Op::TS_SKEW:
        case Op::TS_MED: case Op::TS_MAD: case Op::TS_WMA:
        case Op::TS_MAX_DIFF: case Op::TS_MIN_DIFF:
            return nd.window;
        case Op::DELAY: case Op::TS_DIFF: case Op::TS_PCT: case Op::PCT_CHANGE:
            return nd.window;
        case Op::AUTOCORR:
            return nd.window + static_cast<int>(nd.scalar);
        default:
            return 0;
        }
    }

    static KernelVar allocate_kernel(const FactorNode& nd) {
        auto w = static_cast<std::uint32_t>(nd.window);
        switch (nd.op) {
        case Op::MA:       return fe::ops::RollingMeanKernel(w);
        case Op::TS_SUM:   return fe::ops::RollingSumKernel(w);
        case Op::TS_STD:   return fe::ops::RollingStdKernel(w);
        case Op::TS_VARI:  return RollingVarComposite(nd.window);
        case Op::EMA:      return fe::ops::EmaKernel(w);
        case Op::TS_MIN:   return fe::ops::RollingMinKernel(w);
        case Op::TS_MAX:   return fe::ops::RollingMaxKernel(w);
        case Op::TS_RANK:       return TsRankPush(nd.window);
        case Op::TREAP_TS_RANK: return TreapTsRankPush(nd.window);
        case Op::TS_ZSCORE: return TsZscoreComposite(nd.window);
        case Op::DELAY:    return fe::ops::DelayKernel(w);
        case Op::TS_DIFF:  return TsDiffComposite(nd.window);
        case Op::TS_PCT:   return TsPctComposite(nd.window);
        case Op::PCT_CHANGE: return TsPctComposite(nd.window);
        case Op::CORR:     return CorrPush(nd.window);
        case Op::AUTOCORR: return AutocorrPush(nd.window, static_cast<int>(nd.scalar));
        case Op::TS_MINMAX_DIFF: return TsMinMaxDiffPush(nd.window);
        case Op::TS_SKEW:  return TsSkewPush(nd.window);
        case Op::TS_MED:   return TsMedPush(nd.window);
        case Op::TS_MAD:   return TsMadPush(nd.window);
        case Op::TS_WMA:   return TsWmaPush(nd.window);
        case Op::TS_MAX_DIFF: return TsMaxDiffPush(nd.window);
        case Op::TS_MIN_DIFF: return TsMinDiffPush(nd.window);
        default:           return std::monostate{};
        }
    }
};

}  // namespace fe::runtime
