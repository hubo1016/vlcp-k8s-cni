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
from contextlib import contextmanager

VETH_NAME_TEMPLATE = 'vlcpk8s{:08d}'


_current_ns = None
_current_ns_lock = threading.Lock()


def get_current_ns():
    """
    Return network namespace fid of the main thread
    
    the file descriptor is cached on first call
    """
    global _current_ns
    if _current_ns is None:
        with _current_ns_lock:
            if _current_ns is None:
                _current_ns = os.open('/proc/self/ns/net', os.O_RDONLY)
                atexit.register(os.close, _current_ns)
    return _current_ns


@contextmanager
def nsenter(netns):
    # Open current network namespace
    current_ns = get_current_ns()
    # Avoid using NetNS to prevent using multiprocessing
    if netns is not None:
        ns.setns(netns, os.O_RDONLY)
    try:
        yield current_ns
    finally:
        # Swith back to the original namespace
        if netns is not None:
            ns.setns(current_ns)


def create_veth(netns, veth_name, mtu, address = None, *config, peer_up = True):
    """
    Create a veth pair in the target namespace, and move the peer side back
    to host space
    
    :param netns: network namespace path or fid
    
    :param veth_name: target veth device name
    
    :param mtu: target device mtu
    
    :param address: target device MAC address
    
    :param \*config: callback to setup target device in namespace
    
    :return: peer device name
    """
    # Create a random device name
    peer_name = VETH_NAME_TEMPLATE.format(random.randrange(1, 100000000))
    with nsenter(netns) as current_ns:
        with pyroute2.IPDB(mode='implicit') as ipdb:
            params = dict(kind='veth',
                          peer={"ifname": peer_name,
                                "mtu": mtu},
                          mtu=mtu)
            if address is not None:
                params['address'] = address
            with ipdb.create(**params) as veth_device:
                for c in config:
                    c(ipdb, veth_device)
            with ipdb.interfaces[peer_name] as peer_device:
                peer_device.mtu = mtu
                peer_device.net_ns_fd = current_ns
    if peer_up:
        with pyroute2.IPDB(mode='implicit') as ipdb:
            with ipdb.interfaces[peer_name] as peer_device:
                link_up(ipdb, peer_device)
    return peer_name


def delete_interface(interface_name, netns = None):
    with nsenter(netns):
        with pyroute2.IPDB(mode='implicit') as ipdb:
            ipdb.interfaces[interface_name].remove().commit()


def link_up(ipdb, veth_device):
    veth_device.up()


def set_ip_address(cidr, ip_address):
    # Create IP/Mask format
    _, _, mask = cidr.partition('/')
    ip_mask = ip_address + '/' + mask
    def setter(ipdb, veth_device):
        veth_device.add_ip(ip_mask)
    return setter


def add_routes(gateway, host_routes):
    def setter(ipdb, veth_device):
        if gateway is not None:
            ipdb.routes.add({'dst': 'default',
                             'oif': veth_device.index,
                             'gateway': gateway,
                             'priority': 100}).commit()
        for cidr, via in host_routes:
            if via == '0.0.0.0':
                ipdb.routes.add({'dst': cidr,
                                  'oif': veth_device.index,
                                  'scope': 253}).commit()
            else:
                ipdb.routes.add({'dst': cidr,
                                 'oif': veth_device.index,
                                 'gateway': via}).commit()
    return setter
