#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>

#include "fe/ops/spec.hpp"
#include "fe/ops/unary.hpp"
#include "fe/ops/binary.hpp"
#include "fe/ops/rolling_mean.hpp"
#include "fe/ops/rolling_sum.hpp"
#include "fe/ops/rolling_std.hpp"
#include "fe/ops/rolling_ema.hpp"
#include "fe/ops/rolling_minmax.hpp"
#include "fe/ops/rolling_rank.hpp"
#include "fe/ops/rolling_zscore.hpp"
#include "fe/ops/shift.hpp"
#include "fe/ops/bivariate.hpp"
#include "fe/ops/rolling_extremal.hpp"
#include "fe/ops/rolling_skew.hpp"
#include "fe/ops/rolling_median.hpp"
#include "fe/ops/rolling_wma.hpp"

namespace py = pybind11;
using fe::ops::FeFloat;
using NpArray = py::array_t<float, py::array::c_style | py::array::forcecast>;

// ── pybind wrappers: thin shells over Layer 2 ───────────────

// unary: wrap Layer 2 xxx_array(const FeFloat*, FeFloat*, int)
using UnaryArrayFn = void (*)(const FeFloat*, FeFloat*, int);

py::array_t<float> py_unary(UnaryArrayFn fn, NpArray x) {
    auto buf = x.request();
    auto n = static_cast<int>(buf.size);
    py::array_t<float> result(n);
    fn(static_cast<float*>(buf.ptr), static_cast<float*>(result.request().ptr), n);
    return result;
}

// binary array+array: wrap Layer 2 xxx_aa(const FeFloat*, const FeFloat*, FeFloat*, int)
using BinAAFn = void (*)(const FeFloat*, const FeFloat*, FeFloat*, int);

py::array_t<float> py_binary_aa(BinAAFn fn, NpArray x, NpArray y) {
    auto bx = x.request();
    auto by = y.request();
    if (bx.size != by.size)
        throw std::runtime_error("x and y must have the same length");
    auto n = static_cast<int>(bx.size);
    py::array_t<float> result(n);
    fn(static_cast<float*>(bx.ptr), static_cast<float*>(by.ptr),
       static_cast<float*>(result.request().ptr), n);
    return result;
}

// binary array+scalar: wrap Layer 2 xxx_as(const FeFloat*, FeFloat, FeFloat*, int)
using BinASFn = void (*)(const FeFloat*, FeFloat, FeFloat*, int);

py::array_t<float> py_binary_as(BinASFn fn, NpArray x, float s) {
    auto buf = x.request();
    auto n = static_cast<int>(buf.size);
    py::array_t<float> result(n);
    fn(static_cast<float*>(buf.ptr), s, static_cast<float*>(result.request().ptr), n);
    return result;
}

// binary scalar+array: wrap Layer 2 xxx_sa(FeFloat, const FeFloat*, FeFloat*, int)
using BinSAFn = void (*)(FeFloat, const FeFloat*, FeFloat*, int);

py::array_t<float> py_binary_sa(BinSAFn fn, float s, NpArray y) {
    auto buf = y.request();
    auto n = static_cast<int>(buf.size);
    py::array_t<float> result(n);
    fn(s, static_cast<float*>(buf.ptr), static_cast<float*>(result.request().ptr), n);
    return result;
}

// rolling: generic wrapper for (const FeFloat*, FeFloat*, int, int) functions
using RollingFn = void (*)(const FeFloat*, FeFloat*, int, int);

py::array_t<float> py_rolling(RollingFn fn, NpArray x, int window) {
    auto buf = x.request();
    auto n = static_cast<int>(buf.size);
    py::array_t<float> result(n);
    fn(static_cast<float*>(buf.ptr), static_cast<float*>(result.request().ptr), n, window);
    return result;
}

// bivariate: (const FeFloat*, const FeFloat*, FeFloat*, int, int) functions
using BivariateFn = void (*)(const FeFloat*, const FeFloat*, FeFloat*, int, int);

py::array_t<float> py_bivariate(BivariateFn fn, NpArray x, NpArray y, int window) {
    auto bx = x.request();
    auto by = y.request();
    if (bx.size != by.size)
        throw std::runtime_error("x and y must have the same length");
    auto n = static_cast<int>(bx.size);
    py::array_t<float> result(n);
    fn(static_cast<float*>(bx.ptr), static_cast<float*>(by.ptr),
       static_cast<float*>(result.request().ptr), n, window);
    return result;
}

// ── module ───────────────────────────────────────────────────

PYBIND11_MODULE(fe_ops, m) {
    m.doc() = "FactorEngine C++ operator kernels (pybind11)";

    // P0 unary
    m.def("neg",     [](NpArray x) { return py_unary(fe::ops::neg_array, x); });
    m.def("abs_op",  [](NpArray x) { return py_unary(fe::ops::abs_array, x); });
    m.def("log_op",  [](NpArray x) { return py_unary(fe::ops::log_array, x); });
    m.def("sqr",     [](NpArray x) { return py_unary(fe::ops::sqr_array, x); });
    m.def("inv",     [](NpArray x) { return py_unary(fe::ops::inv_array, x); });
    m.def("sign",    [](NpArray x) { return py_unary(fe::ops::sign_array, x); });
    m.def("tanh_op", [](NpArray x) { return py_unary(fe::ops::tanh_array, x); });
    m.def("slog1p",  [](NpArray x) { return py_unary(fe::ops::slog1p_array, x); });

    // P0 binary: 3 overloads each (aa, as, sa)
    m.def("add", [](NpArray x, NpArray y) { return py_binary_aa(fe::ops::add_aa, x, y); });
    m.def("add", [](NpArray x, float s)   { return py_binary_as(fe::ops::add_as, x, s); });
    m.def("add", [](float s, NpArray y)   { return py_binary_sa(fe::ops::add_sa, s, y); });

    m.def("sub", [](NpArray x, NpArray y) { return py_binary_aa(fe::ops::sub_aa, x, y); });
    m.def("sub", [](NpArray x, float s)   { return py_binary_as(fe::ops::sub_as, x, s); });
    m.def("sub", [](float s, NpArray y)   { return py_binary_sa(fe::ops::sub_sa, s, y); });

    m.def("mul", [](NpArray x, NpArray y) { return py_binary_aa(fe::ops::mul_aa, x, y); });
    m.def("mul", [](NpArray x, float s)   { return py_binary_as(fe::ops::mul_as, x, s); });
    m.def("mul", [](float s, NpArray y)   { return py_binary_sa(fe::ops::mul_sa, s, y); });

    m.def("div_op", [](NpArray x, NpArray y) { return py_binary_aa(fe::ops::div_aa, x, y); });
    m.def("div_op", [](NpArray x, float s)   { return py_binary_as(fe::ops::div_as, x, s); });
    m.def("div_op", [](float s, NpArray y)   { return py_binary_sa(fe::ops::div_sa, s, y); });

    // P1 rolling
    m.def("rolling_mean",  [](NpArray x, int w) { return py_rolling(fe::ops::rolling_mean, x, w); },  py::arg("x"), py::arg("window"));
    m.def("rolling_sum",   [](NpArray x, int w) { return py_rolling(fe::ops::rolling_sum, x, w); },   py::arg("x"), py::arg("window"));
    m.def("rolling_std",   [](NpArray x, int w) { return py_rolling(fe::ops::rolling_std, x, w); },   py::arg("x"), py::arg("window"));
    m.def("rolling_var",   [](NpArray x, int w) { return py_rolling(fe::ops::rolling_var, x, w); },   py::arg("x"), py::arg("window"));
    m.def("ema",           [](NpArray x, int w) { return py_rolling(fe::ops::ema, x, w); },           py::arg("x"), py::arg("span"));
    m.def("rolling_min",   [](NpArray x, int w) { return py_rolling(fe::ops::rolling_min, x, w); },   py::arg("x"), py::arg("window"));
    m.def("rolling_max",   [](NpArray x, int w) { return py_rolling(fe::ops::rolling_max, x, w); },   py::arg("x"), py::arg("window"));
    m.def("rolling_rank",  [](NpArray x, int w) { return py_rolling(fe::ops::rolling_rank, x, w); },  py::arg("x"), py::arg("window"));
    m.def("rolling_zscore",[](NpArray x, int w) { return py_rolling(fe::ops::rolling_zscore, x, w); },py::arg("x"), py::arg("window"));
    m.def("delay",         [](NpArray x, int t) { return py_rolling(fe::ops::delay, x, t); },         py::arg("x"), py::arg("lag"));
    m.def("ts_diff",       [](NpArray x, int t) { return py_rolling(fe::ops::ts_diff, x, t); },       py::arg("x"), py::arg("lag"));
    m.def("ts_pct",        [](NpArray x, int t) { return py_rolling(fe::ops::ts_pct, x, t); },        py::arg("x"), py::arg("lag"));

    // P2 bivariate
    m.def("rolling_corr",  [](NpArray x, NpArray y, int w) { return py_bivariate(fe::ops::rolling_corr, x, y, w); }, py::arg("x"), py::arg("y"), py::arg("window"));
    m.def("autocorr", [](NpArray x, int w, int lag) {
        auto buf = x.request();
        auto n = static_cast<int>(buf.size);
        py::array_t<float> result(n);
        fe::ops::autocorr(static_cast<float*>(buf.ptr),
                          static_cast<float*>(result.request().ptr), n, w, lag);
        return result;
    }, py::arg("x"), py::arg("window"), py::arg("lag"));

    // P2 rolling extremal
    m.def("ts_minmax_diff", [](NpArray x, int w) { return py_rolling(fe::ops::ts_minmax_diff, x, w); }, py::arg("x"), py::arg("window"));

    // P2 rolling skew
    m.def("rolling_skew",  [](NpArray x, int w) { return py_rolling(fe::ops::rolling_skew, x, w); },  py::arg("x"), py::arg("window"));

    // P3 rolling median / MAD
    m.def("rolling_median", [](NpArray x, int w) { return py_rolling(fe::ops::rolling_median, x, w); }, py::arg("x"), py::arg("window"));
    m.def("rolling_mad",    [](NpArray x, int w) { return py_rolling(fe::ops::rolling_mad, x, w); },    py::arg("x"), py::arg("window"));

    // P3 rolling WMA
    m.def("rolling_wma",    [](NpArray x, int w) { return py_rolling(fe::ops::rolling_wma, x, w); },    py::arg("x"), py::arg("window"));

    // P3 rolling extremal diffs (min_periods=1)
    m.def("ts_max_diff",    [](NpArray x, int w) { return py_rolling(fe::ops::ts_max_diff, x, w); },    py::arg("x"), py::arg("window"));
    m.def("ts_min_diff",    [](NpArray x, int w) { return py_rolling(fe::ops::ts_min_diff, x, w); },    py::arg("x"), py::arg("window"));
}
