#!/bin/bash -xe

# Using docker

mkdir -p dist

# Build
build(){
	rm -rf build
	mkdir -p build
	cp -r Dockerfile/$1/* build/
	cp -r $1 build/
	(cd build && docker build -t vlcp-k8s-tmp/$1 .)
	docker run --rm vlcp-k8s-tmp/$1 tar c /$2 | tar x -C dist/
}

build vlcp-k8s-cni vlcp-k8s-cni
build vlcp_k8s vlcp_k8s