VLCP Kubernetes Plugin
======================

This is the *server side* part of the kubernetes plugin. The CNI plugin
part is written in Go, and it simply forwards the request to a unix
socket `/var/run/vlcp-k8s-cni/vlcp-k8s-cni.sock` in HTTP protocol.

The *server side* part is written in Python. It is a VLCP module which
should be started in a VLCP daemon together with other necessary modules.
