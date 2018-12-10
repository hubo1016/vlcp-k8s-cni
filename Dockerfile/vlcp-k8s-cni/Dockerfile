FROM golang:1.10

MAINTAINER hubo

ADD /vlcp-k8s-cni /go/src/github.com/hubo1016/vlcp-k8s-cni/

RUN cd /go/src/github.com/hubo1016/vlcp-k8s-cni/vlcp-k8s-cni/ && go build -v && mv ./vlcp-k8s-cni /
