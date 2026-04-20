"""
Demo: FactorGraph visualization.

Three output modes:
  1. ASCII text   — printed to terminal
  2. DOT source   — saved as .dot file (can be rendered externally)
  3. PNG image    — rendered via graphviz (requires: pip install graphviz)

Run:
    python tests/visualization/demo_visualize.py
"""
import os

import fe_runtime as rt
from factorengine.factors import FactorRegistry
from factorengine.factors.visualize import print_graph, to_dot, render_graph

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


def demo_ascii():
    """Print all registered factors as ASCII text."""
    reg = FactorRegistry()
    reg.load_all()

    print("=" * 60)
    print("  ASCII Visualization Demo")
    print("=" * 60)

    for group in reg.groups:
        print(f"\n{'─'*40}")
        print(f"  Group: {group}")
        print(f"{'─'*40}")
        for fid in reg.factor_ids_by_group(group):
            print(f"\n--- Factor {group}/{fid} ---")
            g = reg.build(fid, group=group)
            print_graph(g)


def demo_dot():
    """Save DOT source files for all registered factors."""
    reg = FactorRegistry()
    reg.load_all()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("\n" + "=" * 60)
    print("  DOT Source Demo")
    print("=" * 60)

    for group in reg.groups:
        for fid in reg.factor_ids_by_group(group):
            g = reg.build(fid, group=group)
            dot_src = to_dot(g, title=f"Factor {group}/{fid}")
            dot_path = os.path.join(OUTPUT_DIR, f"factor_{group}_{fid}.dot")
            with open(dot_path, "w") as f:
                f.write(dot_src)
            print(f"  Saved: {dot_path}")


def demo_png():
    """Render PNG images for all registered factors."""
    reg = FactorRegistry()
    reg.load_all()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("\n" + "=" * 60)
    print("  PNG Render Demo")
    print("=" * 60)

    try:
        import graphviz  # noqa: F401
    except ImportError:
        print("  [SKIP] graphviz not installed. Run: pip install graphviz")
        return

    for group in reg.groups:
        for fid in reg.factor_ids_by_group(group):
            g = reg.build(fid, group=group)
            png_path = os.path.join(OUTPUT_DIR, f"factor_{group}_{fid}.png")
            render_graph(g, png_path, title=f"Factor {group}/{fid}")
            print(f"  Saved: {png_path}")


def demo_custom_factor():
    """Build and visualize a custom factor from scratch."""
    Op = rt.Op

    print("\n" + "=" * 60)
    print("  Custom Factor Demo: TsRank(Div(Ema(close,20), Ma(close,60)), 120)")
    print("=" * 60)

    g = rt.FactorGraph()
    c = g.add_input("close")
    ema20 = g.add_rolling(Op.EMA, c, 20)
    ma60 = g.add_rolling(Op.MA, c, 60)
    ratio = g.add_binary(Op.DIV, ema20, ma60)
    ranked = g.add_rolling(Op.TS_RANK, ratio, 120)
    g.compile()

    print("\nASCII:")
    print_graph(g)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    png_path = os.path.join(OUTPUT_DIR, "custom_factor.png")
    try:
        render_graph(g, png_path, title="Custom: TsRank(Div(Ema,Ma),120)")
        print(f"\nPNG: {png_path}")
    except ImportError:
        print("\n[SKIP] PNG rendering (graphviz not installed)")

    dot_path = os.path.join(OUTPUT_DIR, "custom_factor.dot")
    with open(dot_path, "w") as f:
        f.write(to_dot(g, title="Custom: TsRank(Div(Ema,Ma),120)"))
    print(f"DOT: {dot_path}")


if __name__ == "__main__":
    demo_ascii()
    demo_dot()
    demo_png()
    demo_custom_factor()
    print(f"\nAll output files in: {os.path.abspath(OUTPUT_DIR)}")
