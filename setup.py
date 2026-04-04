from setuptools import setup, find_packages

setup(
    name="factorengine",
    version="0.1.0",
    description="Real-time factor computation engine for OKX perpetual contracts",
    author="tiandiao123",
    url="https://github.com/tiandiao123/FactorEngine",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "aiohttp>=3.9",
        "numpy>=1.24",
    ],
    extras_require={
        "dev": [
            "pytest",
        ],
    },
)
