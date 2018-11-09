#!/usr/bin/env python

from setuptools import setup

version = "0.1.0"

long_description = open('README.rst', 'r').read()

setup(
    name="vlcp-k8s",
    version=version,
    author="Hu Bo",
    author_email="hubo1016@126.com",
    long_description=long_description,
    description="VLCP kubernetes integration",
    license="Apache",
    classifiers=[
        'Intended Audience :: Developers',
        'Programming Language :: Python',
        'Topic :: Software Development',
    ],
    url="https://github.com/hubo1016/vlcp-k8s-cni",
    platforms=['linux'],
    packages=[
        'vlcp_k8s',
    ],
    install_requires = ["vlcp>=2.2.0",
                        "pyroute2>=0.5.3"],
)
