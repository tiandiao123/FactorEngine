"""
FactorGraph visualization utilities.

Provides two output modes:
  1. ASCII art (terminal-friendly, no dependencies)
  2. Graphviz DOT (renders to PNG/SVG/PDF if graphviz is installed)

Usage:
    from factorengine.factors.visualize import print_graph, to_dot, render_graph

    g = build_factor_0001()
    print_graph(g)                         # ASCII to stdout
    print(to_dot(g, title="Factor 0001"))  # DOT source string
    render_graph(g, "factor_0001.png")     # save image (requires graphviz)
"""

from __future__ import annotations

import os
import sys

import fe_runtime as rt


def _node_label(info: rt.NodeInfo) -> str:
    """Build a human-readable label for a node."""
    name = info.op_name
    parts = [f"[{info.id}] {name}"]
    if info.window > 0:
        parts.append(f"w={info.window}")
    if info.scalar != 0.0 and "SCALAR" in name:
        parts.append(f"s={info.scalar}")
    if name == "AUTOCORR":
        parts.append(f"lag={int(info.scalar)}")
    return " ".join(parts)


def _node_shape(info: rt.NodeInfo) -> str:
    """Graphviz shape based on node type."""
    if info.op_name.startswith("INPUT"):
        return "ellipse"
    if info.window > 0:
        return "box"
    return "diamond"


def _node_color(info: rt.NodeInfo) -> str:
    if info.is_output:
        return "#ff6b6b"
    if info.op_name.startswith("INPUT"):
        return "#74b9ff"
    if info.window > 0:
        return "#55efc4"
    return "#ffeaa7"


# ── ASCII visualization ──────────────────────────────────────

def print_graph(g: rt.FactorGraph, file=None):
    """Print a text-based representation of the FactorGraph."""
    if file is None:
        file = sys.stdout

    nodes = g.describe()
    print(f"FactorGraph: {len(nodes)} nodes, warmup={g.warmup_bars()} bars", file=file)
    print("=" * 60, file=file)

    for nd in nodes:
        label = _node_label(nd)
        marker = " ◀ OUTPUT" if nd.is_output else ""
        inputs = []
        if nd.input_a >= 0:
            inputs.append(f"a←[{nd.input_a}]")
        if nd.input_b >= 0:
            inputs.append(f"b←[{nd.input_b}]")
        inp_str = f"  ({', '.join(inputs)})" if inputs else ""
        print(f"  {label}{inp_str}{marker}", file=file)

    print("=" * 60, file=file)

    print("\nEdges:", file=file)
    for nd in nodes:
        if nd.input_a >= 0:
            src = nodes[nd.input_a]
            print(f"  [{src.id}] {src.op_name} ──→ [{nd.id}] {nd.op_name}", file=file)
        if nd.input_b >= 0:
            src = nodes[nd.input_b]
            print(f"  [{src.id}] {src.op_name} ──→ [{nd.id}] {nd.op_name}", file=file)


# ── Graphviz DOT ─────────────────────────────────────────────

def to_dot(g: rt.FactorGraph, title: str = "FactorGraph") -> str:
    """Generate Graphviz DOT source for the FactorGraph."""
    nodes = g.describe()
    lines = [
        f'digraph "{title}" {{',
        '  rankdir=TB;',
        f'  label="{title}  ({len(nodes)} nodes, warmup={g.warmup_bars()})";',
        '  labelloc=t;',
        '  fontsize=14;',
        '  node [fontsize=11, style=filled];',
        '  edge [fontsize=9];',
    ]

    for nd in nodes:
        label = _node_label(nd).replace('"', '\\"')
        shape = _node_shape(nd)
        color = _node_color(nd)
        penwidth = "2.5" if nd.is_output else "1.0"
        lines.append(
            f'  n{nd.id} [label="{label}", shape={shape}, '
            f'fillcolor="{color}", penwidth={penwidth}];'
        )

    for nd in nodes:
        if nd.input_a >= 0:
            lines.append(f'  n{nd.input_a} -> n{nd.id};')
        if nd.input_b >= 0:
            lines.append(f'  n{nd.input_b} -> n{nd.id} [style=dashed];')

    lines.append("}")
    return "\n".join(lines)


def render_graph(g: rt.FactorGraph, output_path: str,
                 title: str = "FactorGraph", fmt: str | None = None):
    """Render FactorGraph to an image file using graphviz.

    Args:
        g: Compiled FactorGraph
        output_path: Output file path (e.g. "factor_0001.png")
        title: Graph title
        fmt: Output format ("png", "svg", "pdf"). Auto-detected from extension.
    """
    try:
        import graphviz
    except ImportError:
        raise ImportError(
            "graphviz Python package required: pip install graphviz\n"
            "Also need graphviz system package: apt install graphviz"
        )

    if fmt is None:
        fmt = os.path.splitext(output_path)[1].lstrip(".")
        if not fmt:
            fmt = "png"

    dot_src = to_dot(g, title=title)
    base = os.path.splitext(output_path)[0]
    gv = graphviz.Source(dot_src)
    gv.render(filename=base, format=fmt, cleanup=True)
    print(f"Rendered: {output_path}")
