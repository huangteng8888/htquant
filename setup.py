from setuptools import setup, find_packages

setup(
    name="htquant",
    version="0.1.0",
    description="Multi-Agent Debate Aggregation for Quantitative Research",
    author="",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "numpy>=1.21.0",
        "pandas>=1.3.0",
    ],
    entry_points={
        "console_scripts": [
            "htquant=htquant.main:main",
        ],
    },
)
