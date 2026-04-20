#pragma once
/**
 * SymbolRunner — manages N FactorGraphs for a single trading symbol.
 *
 * Usage:
 *   SymbolRunner runner("BTC-USDT");
 *   runner.add_factor("0001", std::move(graph1));
 *   runner.add_factor("0010", std::move(graph2));
 *
 *   runner.push_bar(close, volume, open, high, low, ret);
 *   auto& outputs = runner.outputs();  // vector of floats
 */

#include <string>
#include <vector>

#include "fe/runtime/factor_graph.hpp"

namespace fe::runtime {

class SymbolRunner {
public:
    explicit SymbolRunner(std::string symbol)
        : symbol_(std::move(symbol)) {}

    const std::string& symbol() const { return symbol_; }

    void add_factor(std::string factor_id, FactorGraph graph) {
        factor_ids_.push_back(std::move(factor_id));
        graphs_.push_back(std::move(graph));
        outputs_.push_back(0.0f);
    }

    void push_bar(FeFloat close, FeFloat volume = kNaN,
                  FeFloat open = kNaN, FeFloat high = kNaN,
                  FeFloat low = kNaN, FeFloat ret = kNaN) {
        for (std::size_t i = 0; i < graphs_.size(); ++i) {
            graphs_[i].push_bar(close, volume, open, high, low, ret);
            outputs_[i] = graphs_[i].output();
        }
        ++bars_pushed_;
    }

    [[nodiscard]] std::size_t num_factors() const { return graphs_.size(); }
    [[nodiscard]] int bars_pushed() const { return bars_pushed_; }
    [[nodiscard]] const std::vector<FeFloat>& outputs() const { return outputs_; }
    [[nodiscard]] const std::vector<std::string>& factor_ids() const { return factor_ids_; }

    [[nodiscard]] FeFloat output(std::size_t idx) const {
        return (idx < outputs_.size()) ? outputs_[idx] : kNaN;
    }

    [[nodiscard]] FeFloat output_by_id(const std::string& fid) const {
        for (std::size_t i = 0; i < factor_ids_.size(); ++i) {
            if (factor_ids_[i] == fid) return outputs_[i];
        }
        return kNaN;
    }

    void reset() {
        for (auto& g : graphs_) g.reset();
        std::fill(outputs_.begin(), outputs_.end(), 0.0f);
        bars_pushed_ = 0;
    }

private:
    std::string symbol_;
    std::vector<std::string> factor_ids_;
    std::vector<FactorGraph> graphs_;
    std::vector<FeFloat> outputs_;
    int bars_pushed_ = 0;

    static constexpr FeFloat kNaN = std::numeric_limits<FeFloat>::quiet_NaN();
};

}  // namespace fe::runtime
