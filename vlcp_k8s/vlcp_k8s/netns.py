'''
Created on 2018/11/9

:author: hubo
'''
import pyroute2
import pyroute2.netns as ns
import random
import os
import atexit
import threading

VETH_NAME_TEMPLATE = 'vlcpk8s{:08d}'


_current_ns = None
_current_ns_lock = threading.Lock()

def get_current_ns():
    """
    Return network namespace fid of the main thread
    
    the file descriptor is cached on first call
    """
    if _current_ns is None:
        with _current_ns_lock:
            if _current_ns is None:
                _current_ns = os.open('/proc/self/ns/net', os.O_RDONLY)
                atexit.register(os.close, _current_ns)
    return _current_ns


def create_veth(netns, veth_name, mtu, *config):
    """
    Create a veth pair in the target namespace, and move the peer side back
    to host space
    
    :param netns: network namespace path or fid
    
    :param veth_name: target veth device name
    
    :param mtu: target device mtu
    
    :param \*config: callback to setup target device in namespace
    
    :return: peer device name
    """
    # Create a random device name
    peer_name = VETH_NAME_TEMPLATE.format(random.randrange(1, 100000000))
    # Open current network namespace
    current_ns = get_current_ns()
    # Avoid using NetNS to prevent using multiprocessing
    ns.setns(netns, os.O_RDONLY)
    try:
        with pyroute2.IPDB(mode='implicit') as ipdb:
            with ipdb.create(ifname=veth_name, kind='veth', peer=peer_name, mtu=mtu)\
                    as veth_device:
                for c in config:
                    c(ipdb, veth_device)
            with getattr(ipdb.interfaces, peer_name) as peer_device:
                peer_device.net_ns_fd = current_ns
    finally:
        # Swith back to the original namespace
        ns.setns(current_ns)
    return peer_name
