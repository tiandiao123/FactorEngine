#pragma once
/**
 * InferenceEngine — manages multiple SymbolRunners with a thread pool.
 *
 * Each symbol's SymbolRunner has fully independent state, so push_bar
 * for different symbols can execute in parallel without any locking.
 *
 * Two push modes:
 *   1. push_bar(symbol, ...)    — single-symbol push (no threading)
 *   2. push_bars(bars_map)      — batch push all symbols in parallel
 *
 * Usage:
 *   InferenceEngine engine(8);  // 8 worker threads
 *   engine.add_symbol("BTC-USDT");
 *   engine.add_factor("BTC-USDT", "0001", std::move(graph));
 *
 *   // Batch push (parallel)
 *   std::unordered_map<std::string, BarData> bars;
 *   bars["BTC-USDT"] = {close, volume, open, high, low, ret};
 *   engine.push_bars(bars);
 *
 *   auto outputs = engine.get_outputs("BTC-USDT");
 */

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <queue>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "fe/runtime/symbol_runner.hpp"

namespace fe::runtime {

// ── BarData — OHLCV tuple for batch push ──────────────────────

struct BarData {
    FeFloat close  = 0.0f;
    FeFloat volume = std::numeric_limits<FeFloat>::quiet_NaN();
    FeFloat open   = std::numeric_limits<FeFloat>::quiet_NaN();
    FeFloat high   = std::numeric_limits<FeFloat>::quiet_NaN();
    FeFloat low    = std::numeric_limits<FeFloat>::quiet_NaN();
    FeFloat ret    = std::numeric_limits<FeFloat>::quiet_NaN();
};

// ── ThreadPool — lightweight fixed-size pool ──────────────────

class ThreadPool {
public:
    explicit ThreadPool(std::size_t num_threads) : stop_(false) {
        workers_.reserve(num_threads);
        for (std::size_t i = 0; i < num_threads; ++i) {
            workers_.emplace_back([this] { worker_loop(); });
        }
    }

    ~ThreadPool() {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            stop_ = true;
        }
        cv_.notify_all();
        for (auto& t : workers_) {
            if (t.joinable()) t.join();
        }
    }

    ThreadPool(const ThreadPool&) = delete;
    ThreadPool& operator=(const ThreadPool&) = delete;

    void submit(std::function<void()> task) {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            tasks_.push(std::move(task));
        }
        cv_.notify_one();
    }

    std::size_t num_threads() const { return workers_.size(); }

private:
    void worker_loop() {
        while (true) {
            std::function<void()> task;
            {
                std::unique_lock<std::mutex> lk(mtx_);
                cv_.wait(lk, [this] { return stop_ || !tasks_.empty(); });
                if (stop_ && tasks_.empty()) return;
                task = std::move(tasks_.front());
                tasks_.pop();
            }
            task();
        }
    }

    std::vector<std::thread> workers_;
    std::queue<std::function<void()>> tasks_;
    std::mutex mtx_;
    std::condition_variable cv_;
    bool stop_;
};

// ── InferenceEngine ───────────────────────────────────────────

class InferenceEngine {
public:
    explicit InferenceEngine(int num_threads = 0)
        : num_threads_(num_threads > 0
              ? static_cast<std::size_t>(num_threads)
              : std::max<std::size_t>(1, std::thread::hardware_concurrency()))
    {
        pool_ = std::make_unique<ThreadPool>(num_threads_);
    }

    // ── Setup API ────────────────────────────────────────────

    void add_symbol(const std::string& symbol) {
        if (runners_.count(symbol))
            throw std::runtime_error("Symbol already registered: " + symbol);
        runners_.emplace(symbol, SymbolRunner(symbol));
        symbol_order_.push_back(symbol);
    }

    void add_factor(const std::string& symbol,
                    const std::string& factor_id,
                    FactorGraph graph) {
        auto it = runners_.find(symbol);
        if (it == runners_.end())
            throw std::runtime_error("Symbol not registered: " + symbol);
        it->second.add_factor(factor_id, std::move(graph));
    }

    // ── Single-symbol push (no threading) ────────────────────

    void push_bar(const std::string& symbol,
                  FeFloat close, FeFloat volume = kNaN,
                  FeFloat open = kNaN, FeFloat high = kNaN,
                  FeFloat low = kNaN, FeFloat ret = kNaN) {
        auto it = runners_.find(symbol);
        if (it == runners_.end())
            throw std::runtime_error("Symbol not registered: " + symbol);
        it->second.push_bar(close, volume, open, high, low, ret);
    }

    // ── Batch push (parallel via thread pool) ────────────────

    void push_bars(const std::unordered_map<std::string, BarData>& bars) {
        if (bars.empty()) return;

        // Shared state must outlive all workers — use shared_ptr so the
        // last user (main thread or last worker) handles destruction.
        struct Barrier {
            std::atomic<int> remaining{0};
            std::mutex mtx;
            std::condition_variable cv;
        };
        auto barrier = std::make_shared<Barrier>();

        for (auto& [sym, bar] : bars) {
            auto it = runners_.find(sym);
            if (it == runners_.end()) continue;

            barrier->remaining.fetch_add(1, std::memory_order_relaxed);
            auto* runner = &(it->second);
            BarData b = bar;

            pool_->submit([runner, b, barrier] {
                runner->push_bar(b.close, b.volume, b.open, b.high, b.low, b.ret);
                if (barrier->remaining.fetch_sub(1, std::memory_order_acq_rel) == 1) {
                    std::lock_guard<std::mutex> lk(barrier->mtx);
                    barrier->cv.notify_one();
                }
            });
        }

        std::unique_lock<std::mutex> lk(barrier->mtx);
        barrier->cv.wait(lk, [&barrier] {
            return barrier->remaining.load(std::memory_order_acquire) == 0;
        });
    }

    // ── Query API ────────────────────────────────────────────

    [[nodiscard]] std::vector<FeFloat> get_outputs(const std::string& symbol) const {
        auto it = runners_.find(symbol);
        if (it == runners_.end())
            throw std::runtime_error("Symbol not registered: " + symbol);
        return it->second.outputs();
    }

    [[nodiscard]] std::vector<std::string> get_factor_ids(const std::string& symbol) const {
        auto it = runners_.find(symbol);
        if (it == runners_.end())
            throw std::runtime_error("Symbol not registered: " + symbol);
        return it->second.factor_ids();
    }

    /// Return all symbols' outputs in one call: {symbol: {factor_id: value}}.
    /// Avoids N round-trips across the Python↔C++ boundary.
    [[nodiscard]]
    std::unordered_map<std::string, std::unordered_map<std::string, FeFloat>>
    get_all_outputs() const {
        std::unordered_map<std::string, std::unordered_map<std::string, FeFloat>> result;
        result.reserve(runners_.size());
        for (const auto& [sym, runner] : runners_) {
            const auto& fids = runner.factor_ids();
            const auto& outs = runner.outputs();
            std::unordered_map<std::string, FeFloat> fmap;
            fmap.reserve(fids.size());
            for (std::size_t i = 0; i < fids.size(); ++i) {
                fmap[fids[i]] = outs[i];
            }
            result[sym] = std::move(fmap);
        }
        return result;
    }

    [[nodiscard]] const SymbolRunner& get_runner(const std::string& symbol) const {
        auto it = runners_.find(symbol);
        if (it == runners_.end())
            throw std::runtime_error("Symbol not registered: " + symbol);
        return it->second;
    }

    [[nodiscard]] std::vector<std::string> symbols() const { return symbol_order_; }
    [[nodiscard]] std::size_t num_symbols() const { return runners_.size(); }
    [[nodiscard]] std::size_t num_threads() const { return num_threads_; }

    void reset() {
        for (auto& [sym, runner] : runners_) runner.reset();
    }

private:
    std::unordered_map<std::string, SymbolRunner> runners_;
    std::vector<std::string> symbol_order_;
    std::size_t num_threads_;
    std::unique_ptr<ThreadPool> pool_;

    static constexpr FeFloat kNaN = std::numeric_limits<FeFloat>::quiet_NaN();
};

}  // namespace fe::runtime
