"""
FactorEngine setup — builds C++ pybind11 extensions via CMake.

Usage:
    pip install -e .          # editable install (dev)
    pip install -e ".[dev]"   # with test dependencies
    pip install .             # production install
"""

import os
import subprocess
import sys
from pathlib import Path

from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext


class CMakeExtension(Extension):
    """Marker extension — CMake handles actual compilation."""

    def __init__(self, name: str):
        super().__init__(name, sources=[])


class CMakeBuild(build_ext):
    """Custom build_ext that drives CMake to compile all native extensions.

    Produces fe_ops.*.so and fe_runtime.*.so as top-level modules so that
    `import fe_ops` and `import fe_runtime` work after installation.
    """

    def run(self):
        import pybind11

        ext_fullpath = Path(self.get_ext_fullpath("fe_ops")).resolve()
        output_dir = ext_fullpath.parent
        output_dir.mkdir(parents=True, exist_ok=True)

        source_dir = str(Path(__file__).resolve().parent / "native")
        build_dir = os.path.join(source_dir, "build")
        os.makedirs(build_dir, exist_ok=True)

        cfg = "Release"
        cmake_args = [
            f"-DCMAKE_BUILD_TYPE={cfg}",
            f"-Dpybind11_DIR={pybind11.get_cmake_dir()}",
            f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={output_dir}",
            f"-DPYTHON_EXECUTABLE={sys.executable}",
        ]
        build_args = ["--config", cfg, "-j"]

        subprocess.check_call(
            ["cmake", source_dir] + cmake_args,
            cwd=build_dir,
        )
        subprocess.check_call(
            ["cmake", "--build", "."] + build_args,
            cwd=build_dir,
        )

    def build_extension(self, ext):
        pass


setup(
    name="factorengine",
    version="0.1.0",
    description="Real-time factor computation engine with C++ kernels",
    author="tiandiao123",
    url="https://github.com/tiandiao123/FactorEngine",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "aiohttp>=3.9",
        "numpy>=1.24",
        "pybind11>=2.11",
    ],
    extras_require={
        "dev": [
            "pytest",
            "pandas",
            "graphviz",
        ],
    },
    ext_modules=[
        CMakeExtension("fe_ops"),
        CMakeExtension("fe_runtime"),
    ],
    cmdclass={"build_ext": CMakeBuild},
    zip_safe=False,
)
