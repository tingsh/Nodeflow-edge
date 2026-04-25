from setuptools import setup, find_packages

from nodeflow_edge.__version__ import __version__

setup(
    name="nodeflow-edge",
    version=__version__,
    description="Nodeflow Edge IoT Gateway — Industrial protocol gateway for Nodeflow Cloud",
    author="Nodeflow",
    packages=find_packages(),
    python_requires=">=3.9",
    entry_points={
        "console_scripts": [
            "nodeflow-edge=nodeflow_edge.main:main",
        ],
    },
    install_requires=[
        "PyYAML",
        "simplejson",
        "orjson",
        "pybase64",
        "jsonpath-rw",
        "regex",
        "packaging>=23.1",
        "cachetools",
        "python-dateutil",
        "psutil",
        "cryptography",
        "PySocks",
        "paho-mqtt>=1.6.0",
        "pymodbus>=3.8.0",
        "pyserial",
        "pyserial-asyncio",
    ],
)
