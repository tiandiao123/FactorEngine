#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "fe/runtime/factor_graph.hpp"
#include "fe/runtime/symbol_runner.hpp"
#include "fe/runtime/inference_engine.hpp"

namespace py = pybind11;
using namespace fe::runtime;

PYBIND11_MODULE(fe_runtime, m) {
    m.doc() = "FactorEngine DAG runtime (pybind11)";

    py::enum_<Op>(m, "Op")
        // Inputs
        .value("INPUT_CLOSE",  Op::INPUT_CLOSE)
        .value("INPUT_VOLUME", Op::INPUT_VOLUME)
        .value("INPUT_OPEN",   Op::INPUT_OPEN)
        .value("INPUT_HIGH",   Op::INPUT_HIGH)
        .value("INPUT_LOW",    Op::INPUT_LOW)
        .value("INPUT_RET",    Op::INPUT_RET)
        // P0 unary
        .value("NEG",    Op::NEG)
        .value("ABS",    Op::ABS)
        .value("LOG",    Op::LOG)
        .value("SQR",    Op::SQR)
        .value("INV",    Op::INV)
        .value("SIGN",   Op::SIGN)
        .value("TANH",   Op::TANH)
        .value("SLOG1P", Op::SLOG1P)
        // P0 binary
        .value("ADD", Op::ADD)
        .value("SUB", Op::SUB)
        .value("MUL", Op::MUL)
        .value("DIV", Op::DIV)
        // P0 scalar
        .value("ADD_SCALAR", Op::ADD_SCALAR)
        .value("SUB_SCALAR", Op::SUB_SCALAR)
        .value("MUL_SCALAR", Op::MUL_SCALAR)
        .value("DIV_SCALAR", Op::DIV_SCALAR)
        .value("SCALAR_SUB", Op::SCALAR_SUB)
        .value("SCALAR_DIV", Op::SCALAR_DIV)
        // P1 rolling
        .value("MA",        Op::MA)
        .value("TS_SUM",    Op::TS_SUM)
        .value("TS_STD",    Op::TS_STD)
        .value("TS_VARI",   Op::TS_VARI)
        .value("EMA",       Op::EMA)
        .value("TS_MIN",    Op::TS_MIN)
        .value("TS_MAX",    Op::TS_MAX)
        .value("TS_RANK",   Op::TS_RANK)
        .value("TS_ZSCORE", Op::TS_ZSCORE)
        .value("DELAY",     Op::DELAY)
        .value("TS_DIFF",   Op::TS_DIFF)
        .value("TS_PCT",    Op::TS_PCT)
        // P2
        .value("CORR",           Op::CORR)
        .value("AUTOCORR",       Op::AUTOCORR)
        .value("TS_MINMAX_DIFF", Op::TS_MINMAX_DIFF)
        .value("TS_SKEW",        Op::TS_SKEW)
        // P3
        .value("TS_MED",      Op::TS_MED)
        .value("TS_MAD",      Op::TS_MAD)
        .value("TS_WMA",      Op::TS_WMA)
        .value("TS_MAX_DIFF", Op::TS_MAX_DIFF)
        .value("TS_MIN_DIFF", Op::TS_MIN_DIFF)
        // Optimized
        .value("TREAP_TS_RANK", Op::TREAP_TS_RANK)
        // Derived
        .value("PCT_CHANGE", Op::PCT_CHANGE)
        .export_values();

    py::class_<FactorGraph::NodeInfo>(m, "NodeInfo")
        .def_readonly("id",        &FactorGraph::NodeInfo::id)
        .def_readonly("op_name",   &FactorGraph::NodeInfo::op_name)
        .def_readonly("input_a",   &FactorGraph::NodeInfo::input_a)
        .def_readonly("input_b",   &FactorGraph::NodeInfo::input_b)
        .def_readonly("window",    &FactorGraph::NodeInfo::window)
        .def_readonly("scalar",    &FactorGraph::NodeInfo::scalar)
        .def_readonly("is_output", &FactorGraph::NodeInfo::is_output);

    py::class_<FactorGraph>(m, "FactorGraph")
        .def(py::init<>())
        .def("add_input",     &FactorGraph::add_input,     py::arg("feature"))
        .def("add_unary",     &FactorGraph::add_unary,     py::arg("op"), py::arg("src"))
        .def("add_binary",    &FactorGraph::add_binary,    py::arg("op"), py::arg("src_a"), py::arg("src_b"))
        .def("add_rolling",   &FactorGraph::add_rolling,   py::arg("op"), py::arg("src"), py::arg("window"))
        .def("add_bivariate", &FactorGraph::add_bivariate, py::arg("op"), py::arg("src_a"), py::arg("src_b"), py::arg("window"))
        .def("add_scalar_op", &FactorGraph::add_scalar_op, py::arg("op"), py::arg("src"), py::arg("scalar"))
        .def("add_autocorr",  &FactorGraph::add_autocorr,  py::arg("src"), py::arg("window"), py::arg("lag"))
        .def("compile",       &FactorGraph::compile)
        .def("push_bar",      &FactorGraph::push_bar,
             py::arg("close"),
             py::arg("volume") = std::numeric_limits<float>::quiet_NaN(),
             py::arg("open")   = std::numeric_limits<float>::quiet_NaN(),
             py::arg("high")   = std::numeric_limits<float>::quiet_NaN(),
             py::arg("low")    = std::numeric_limits<float>::quiet_NaN(),
             py::arg("ret")    = std::numeric_limits<float>::quiet_NaN())
        .def("ready",         &FactorGraph::ready)
        .def("output",        &FactorGraph::output)
        .def("raw_output",    &FactorGraph::raw_output)
        .def("warmup_bars",   &FactorGraph::warmup_bars)
        .def("bars_seen",     &FactorGraph::bars_seen)
        .def("num_nodes",     &FactorGraph::num_nodes)
        .def("describe",      &FactorGraph::describe)
        .def("reset",         &FactorGraph::reset);

    py::class_<SymbolRunner>(m, "SymbolRunner")
        .def(py::init<std::string>(), py::arg("symbol"))
        .def("add_factor",   &SymbolRunner::add_factor,
             py::arg("factor_id"), py::arg("graph"))
        .def("push_bar",     &SymbolRunner::push_bar,
             py::arg("close"),
             py::arg("volume") = std::numeric_limits<float>::quiet_NaN(),
             py::arg("open")   = std::numeric_limits<float>::quiet_NaN(),
             py::arg("high")   = std::numeric_limits<float>::quiet_NaN(),
             py::arg("low")    = std::numeric_limits<float>::quiet_NaN(),
             py::arg("ret")    = std::numeric_limits<float>::quiet_NaN())
        .def("symbol",       &SymbolRunner::symbol)
        .def("num_factors",  &SymbolRunner::num_factors)
        .def("bars_pushed",  &SymbolRunner::bars_pushed)
        .def("outputs",      &SymbolRunner::outputs)
        .def("output",       &SymbolRunner::output,       py::arg("idx"))
        .def("output_by_id", &SymbolRunner::output_by_id, py::arg("factor_id"))
        .def("factor_ids",   &SymbolRunner::factor_ids)
        .def("reset",        &SymbolRunner::reset);

    py::class_<BarData>(m, "BarData")
        .def(py::init<>())
        .def(py::init([](float c, float v, float o, float h, float l, float r) {
            return BarData{c, v, o, h, l, r};
        }), py::arg("close"),
            py::arg("volume") = std::numeric_limits<float>::quiet_NaN(),
            py::arg("open")   = std::numeric_limits<float>::quiet_NaN(),
            py::arg("high")   = std::numeric_limits<float>::quiet_NaN(),
            py::arg("low")    = std::numeric_limits<float>::quiet_NaN(),
            py::arg("ret")    = std::numeric_limits<float>::quiet_NaN())
        .def_readwrite("close",  &BarData::close)
        .def_readwrite("volume", &BarData::volume)
        .def_readwrite("open",   &BarData::open)
        .def_readwrite("high",   &BarData::high)
        .def_readwrite("low",    &BarData::low)
        .def_readwrite("ret",    &BarData::ret);

    py::class_<InferenceEngine>(m, "InferenceEngine")
        .def(py::init<int>(), py::arg("num_threads") = 0)
        .def("add_symbol",     &InferenceEngine::add_symbol,     py::arg("symbol"))
        .def("add_factor",     &InferenceEngine::add_factor,
             py::arg("symbol"), py::arg("factor_id"), py::arg("graph"))
        .def("push_bar",       &InferenceEngine::push_bar,
             py::arg("symbol"), py::arg("close"),
             py::arg("volume") = std::numeric_limits<float>::quiet_NaN(),
             py::arg("open")   = std::numeric_limits<float>::quiet_NaN(),
             py::arg("high")   = std::numeric_limits<float>::quiet_NaN(),
             py::arg("low")    = std::numeric_limits<float>::quiet_NaN(),
             py::arg("ret")    = std::numeric_limits<float>::quiet_NaN())
        .def("push_bars", [](InferenceEngine& self,
                             const std::unordered_map<std::string, BarData>& bars) {
            py::gil_scoped_release release;
            self.push_bars(bars);
        }, py::arg("bars"),
           "Batch push bars for multiple symbols in parallel (releases GIL).")
        .def("get_outputs",     &InferenceEngine::get_outputs,     py::arg("symbol"))
        .def("get_factor_ids", &InferenceEngine::get_factor_ids, py::arg("symbol"))
        .def("get_all_outputs", &InferenceEngine::get_all_outputs,
             "Return all symbols' outputs: {symbol: {factor_id: float}}")
        .def("symbols",        &InferenceEngine::symbols)
        .def("num_symbols",    &InferenceEngine::num_symbols)
        .def("num_threads",    &InferenceEngine::num_threads)
        .def("reset",          &InferenceEngine::reset);
}
