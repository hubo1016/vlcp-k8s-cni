FROM python:3.6

AUTHOR hubo

ADD /vlcp_k8s /opt/workspace/

RUN (pip3 install -U pip wheel setuptools && cd /opt/workspace/vlcp_k8s && python3 setup.py bdist_wheel bdist_egg sdist && cp -r dist/ /vlcp_k8s)

