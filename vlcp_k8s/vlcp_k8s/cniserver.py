'''
Created on 2018/11/19

:author: hubo
'''

from vlcp.server.module import Module, depend, call_api
from vlcp.config.config import defaultconfig
from vlcp.service.connection import httpserver
from vlcp.service.sdn import viperflow, vrouterapi, ovsdbmanager
from vlcp.utils.http import HttpHandler
import traceback
import json
import functools
from pychecktype import check_type
from vlcp.utils.ethernet import mac_addr_bytes
from namedstruct import create_binary, uint64
from random import randint
from vlcp_k8s.netns import create_veth, add_routes, set_ip_address, link_up, delete_interface
from vlcp.utils.connector import TaskPool
import vlcp.utils.ovsdb as ovsdb
from vlcp.protocol.jsonrpc import JsonRPCErrorResultException
import socket
from vlcp.event import Future
from contextlib import contextmanager


network_config_schema = {"cniVersion": str,
                         "name": str,
                         "?args": {},
                         "dns":
                            {
                                "nameservers": [str],
                                "domain": str,
                                "search": [str],
                                "options": [str]
                            }
                         }


class CNIException(Exception):
    def __init__(self, errorcode, errormsg):
        Exception.__init__(self, errormsg)
        self.errorcode = errorcode


def _check_transact_result(result, operations):
    if any('error' in r for r in result if r is not None):
        err_info = next((r['error'],
                     operations[i] if i < len(operations) else None)
                    for i,r in enumerate(result)
                    if r is not None and 'error' in r)
        raise JsonRPCErrorResultException('Error in OVSDB transact operation: %r. Corresponding operation: %r' % err_info)


async def _ovsdb_transact(c, *operations):
    method, params = ovsdb.transact(
                        "Open_vSwitch",
                        *operations
                    )
    result, _ = await c.protocol.querywithreply(method, params, c)
    _check_transact_result(result, operations)
    return result


class _CancellingException(BaseException):
    pass


class CancelledException(Exception):
    pass


class _CancelContext(object):
    def __init__(self, container, future = None):
        if future is None:
            self._future = Future(container.scheduler)
        else:
            self._future = future
        self._container = container
        self._suppressing = False
    
    def cancel(self):
        if not self._future.done():
            self._future.set_result(True)
    
    async def with_cancel(self, func, *args, **kwargs):
        matcher = self._future.get_matcher()
        if matcher is None:
            raise CancelledException
        def _callback(event, matcher):
            if not self._suppressing:
                raise _CancellingException(self)
        try:
            return await self._container.with_callback(func(*args, **kwargs),
                                                        _callback,
                                                        matcher)
        except _CancellingException as exc:
            if exc.args[0] is self:
                raise CancelledException
            else:
                raise
    
    @contextmanager
    def suppress(self):
        try:
            self._suppressing = True
            yield self
        finally:
            self._suppressing = False
            if self._future.done():
                raise _CancellingException(self)


def _cniapi(path):
    def _decoractor(f):
        @HttpHandler.route(path, method=[b'POST'])
        @functools.wraps(f)
        async def _handler(self, env):
            try:
                content_type = env.headerdict.get(b'content-type', b'')
                content_type, _, _ = content_type.partition(b';')
                if content_type.strip().lower() != b'application/json':
                    raise CNIException(101, "Content type not acceptable - must be application/json")
                
                data = await env.inputstream.read()
                config = json.loads(data)
                
                try:
                    config = check_type(config, network_config_schema)
    
                    cid = env.headerdict.get(b"x-cni-containerid")
                    if not cid:
                        raise CNIException(103, "Container ID is required")
                    else:
                        cid = cid.decode('ascii')
                    
                    ns = env.headerdict.get(b"x-cni-netns")
                    if not ns:
                        raise CNIException(103, "Netns is required")
                    else:
                        ns = ns.decode('ascii')
    
                    ifname = env.headerdict.get(b"x-cni-ifname")
                    if not ifname:
                        raise CNIException(103, "IfName is required")
                    else:
                        ifname = ifname.decode('ascii')
                    
                    args_s = env.headerdict.get(b"x-cni-args", b"")
                    args = config.get("args", {})
                    
                    for a in args_s.split(b';'):
                        k, p, v = a.partition(b'=')
                        if not p:
                            raise CNIException(103, "Invalid argument format: %r" % (a,))
                        args[k.strip().decode('ascii')] = v.strip().decode('ascii')
                except CNIException:
                    raise
                except Exception as exc:
                    raise CNIException(102, str(exc))
                else:
                    cancelcontext = _CancelContext(self)
                    try:
                        r = await self.delegate(f(self, env, config, cid, ns, ifname, args, cancelcontext))
                    finally:
                        cancelcontext.cancel()
            except CNIException as exc:
                env.start_response(400)
                env.outputjson({
                                  "cniVersion": "0.3.1",
                                  "code": exc.errorcode,
                                  "msg": str(exc),
                                  "details": traceback.format_exc(10)
                                })
            except Exception as exc:
                self._logger.warning("Unexpected exception on request handling", exc_info=True)
                env.start_response(500)
                env.outputjson({
                                  "cniVersion": "0.3.1",
                                  "code": 100,
                                  "msg": str(exc),
                                  "details": traceback.format_exc(10)
                                })
            else:
                env.start_response(200)
                env.outputjson(r)


class CNIHandler(HttpHandler):
    def __init__(self, parent):
        HttpHandler.__init__(self, parent.scheduler, False, vhost=parent.vhostbind)
        self._parent = parent
        self._logger = parent._logger
        self._macbase = uint64.create(create_binary(mac_addr_bytes(self._parent.mactemplate), 8))
        self._bridgename = parent.bridgename
        self._ovsdbhost = parent.ovsdbhost
        self._logicalnetwork = parent.logicalnetwork
        self._subnet = parent.subnet
        self._mtu = parent.mtu
        self._checkerror = parent.checkerror
        self._hostname = socket.gethostname()
    
    @_cniapi(b'/add')
    async def add(self, env, config, container_id, netns, ifname, args, cancelcontext):
        # If connections are not ready, do not start further actions
        await call_api(self, 'ovsdbmanager', 'waitanyconnection',
                                {"vhost": self._ovsdbhost})
        # 1. Create logical port
        # 2. Create interface in the namespace
        # 3. Attach the interface to OVSDB
        # 4. Write interface information back to logical port
        logicalport_id = 'k8s-' + container_id
        
        # For logical network ID:
        # 1. If specified in args, use args['network']
        # 2. Use module.cniserver.logicalnetwork if specified in config file
        # 3. Use CNI network config parameter "name"

        # For subnet ID:
        # 1. If specified in args, use args['subnet']
        # 2. If args['network'] is specified but not args['subnet'], use args['network']
        # 3. Use module.cniserver.subnet if specified in config file
        # 4. Use same ID as logical network (from module.cniserver.logicalnetwork or CNI config)
        lognet = args.get('network')
        subnet = args.get('subnet')
        if not subnet:
            subnet = lognet
        if not lognet:
            lognet = self._logicalnetwork
        if not subnet:
            subnet = self._subnet
        if not lognet:
            lognet = config['name']
        if not subnet:
            subnet = lognet
        
        # Generate a MAC address
        mac_num = self._macbase
        mac_num ^= randint(0, 0xffffffff)
        mac_address = mac_addr_bytes.formatter(create_binary(mac_num, 6))
        
        params = {"id": logicalport_id,
                  "logicalnetwork": lognet,
                  "subnet": subnet,
                  "mac_address": mac_address
                  }
        if "ip" in args:
            params['ip_address'] = args['ip']
        if 'hostname' in args:
            params['hostname'] = args['hostname']
        with cancelcontext.suppress():
            result = await call_api(self, 'viperflow', 'createlogicalport', params)
            async def _cleanup_logicalport(logicalport_id=logicalport_id):
                await call_api(self, 'viperflow', 'deletelogicalport', {"id": logicalport_id})
            cleanups = [_cleanup_logicalport]
        try:
            logport = result[0]
            ip = logport['ip_address']
            mac_address = logport['mac_address']
            mtu = logport['subnet'].get('mtu')
            if mtu is None:
                mtu = logport['network'].get('mtu')
            if mtu is None:
                mtu = self._mtu
            cidr = logport['subnet']['cidr']
            gateway = logport['subnet'].get('gateway')
            host_routes = logport['subnet'].get('host_routes', [])
            dns_nameservers = logport['subnet'].get('dns_nameservers')
            if dns_nameservers is None:
                dns_nameservers = logport['network'].get('dns_nameservers')
            domain_name = logport['subnet'].get('domain_name')
            if domain_name is None:
                domain_name = logport['network'].get('domain_name')
            settings = [link_up, set_ip_address(cidr, ip), add_routes(gateway, host_routes)]
            with cancelcontext.suppress():
                interface_name = await self._parent._taskpool.run_task(
                                            self,
                                            functools.partial(create_veth,
                                                              netns,
                                                              mtu,
                                                              mac_address,
                                                              *settings))
                async def _cleanup_veth(interface_name=interface_name):
                    await self._parent._taskpool.run_task(
                            self,
                            functools.partial(delete_interface,
                                              interface_name)
                        )
                cleanups.append(_cleanup_veth)
            conns = await call_api(self, 'ovsdbmanager', 'waitanyconnection',
                                        {"vhost": self._ovsdbhost})
            c = conns[0]
            # Add interface to OVSDB
            with cancelcontext.suppress():
                result = await _ovsdb_transact(
                                    c,
                                    ovsdb.insert(
                                        "Interface",
                                        {
                                            "name": interface_name,
                                            "type": "system",
                                            "external_ids" : ovsdb.omap(("iface-id", logicalport_id))
                                        },
                                        "interface_id"
                                    ),
                                    ovsdb.insert(
                                        "Port",
                                        {
                                            "name": interface_name,
                                            "interfaces": ovsdb.oset(ovsdb.named_uuid("interface_id"))
                                        },
                                        "port_id"
                                    ),
                                    ovsdb.mutate(
                                        "Bridge",
                                        [ovsdb.condition("name", "==", self._bridgename)],
                                        [ovsdb.mutation("ports",
                                                        "insert",
                                                        ovsdb.oset(ovsdb.named_uuid("port_id")))]
                                    )
                                )
                interface_uuid = result[0]['uuid']
                port_uuid = result[1]['uuid']
                async def _cleanup_ovsdb():
                    await _ovsdb_transact(
                                c,
                                ovsdb.mutate(
                                    "Bridge",
                                    [ovsdb.condition("name", "==", self._bridgename)],
                                    [ovsdb.mutation("ports",
                                                    "delete",
                                                    ovsdb.oset(port_uuid))]
                                  )
                            )
                cleanups.append(_cleanup_ovsdb)
            if self._checkerror:
                # Check OVSDB
                await _ovsdb_transact(
                            c,
                            ovsdb.wait('Interface', [["_uuid", "==", interface_uuid]],
                                       ["ofport"], [{"ofport":ovsdb.oset()}], False, 5000),
                            ovsdb.wait('Interface', [["_uuid", "==", interface_uuid]],
                                       ["ofport"], [{"ofport":-1}], False, 0),
                            ovsdb.wait('Interface', [["_uuid", "==", interface_uuid]],
                                       ["ifindex"], [{"ifindex":ovsdb.oset()}], False, 1000)
                        )
        except:
            # Always Cleanup
            async def _cleanup(cleanups=cleanups):
                self._logger.warning("Cleanup resources")
                for t in reversed(cleanups):
                    try:
                        await t()
                    except Exception:
                        self._logger.warning("Cleanup exception with %r", t, exc_info=True)
            self.subroutine(_cleanup())
            raise
        else:
            # Transaction succeeded, write back asynchronously
            system_id = getattr(c, 'ovsdb_systemid', None)
            async def _writeback():
                try:
                    await call_api(self, 'viperflow', 'updatelogicalport', {'id': logicalport_id,
                                                                            'k8s_systemid': system_id,
                                                                            'k8s_hostname': self._hostname,
                                                                            'k8s_netns': netns,
                                                                            'k8s_ifname': interface_name,
                                                                            'k8s_ns_ifname': ifname})
                except Exception:
                    self._logger.warning("Write K8S information back failed for port %r. Request: %r",
                                         logicalport_id,
                                        {'id': logicalport_id,
                                        'k8s_systemid': system_id,
                                        'k8s_hostname': self._hostname,
                                        'k8s_netns': netns,
                                        'k8s_ifname': interface_name,
                                        'k8s_ns_ifname': ifname},
                                        exc_info=True)
            self.subroutine(_writeback(), False)
            # Return
            _, _, mask = cidr.partition('/')
            ip_mask = ip + '/' + mask
            result = \
                {
                  "cniVersion": "0.3.1",
                  "interfaces": [
                      {
                          "name": ifname,
                          "mac": mac_address,
                          "sandbox": netns
                      }
                  ],
                  "ips": [
                      {
                          "version": "4",
                          "address": ip_mask,
                          "interface": 0
                      }
                  ],
                  "routes": [{'dst': cidr, 'gw': via}
                             for cidr, via in host_routes],
                  "dns": config['dns']
                }
            if gateway:
                result['ips'][0]['gateway'] = gateway
            return result

    @_cniapi('/del')
    async def delete(self, env, config, container_id, netns, ifname, args, cancelcontext):
        # Delete logical port, OVSDB port config and interface in namespace
        logicalport_id = 'k8s-' + container_id
        async def _delete_logport():
            try:
                await call_api(self, 'viperflow', 'deletelogicalport', {"id": logicalport_id})
            except ValueError:
                # Already removed
                pass
            except Exception:
                self._logger.warning("Failed to remove logical port %r", logicalport_id, exc_info=True)
        async def _delete_veth():
            try:
                conns = await call_api(self, 'ovsdbmanager', 'waitanyconnection',
                                            {"vhost": self._ovsdbhost})
                c = conns[0]
                result = await _ovsdb_transact(
                                    c,
                                    ovsdb.select(
                                        "Interface",
                                        [ovsdb.condition(
                                            "external_ids",
                                            "includes",
                                            ovsdb.omap(("iface-id", logicalport_id))
                                        )],
                                        ["_uuid", "name"]
                                    )
                                )
                if result[0]['rows']:
                    interface_uuid = result[0]['rows'][0]['_uuid']
                    result = await _ovsdb_transact(
                                        c,
                                        ovsdb.select(
                                            "Port",
                                            [ovsdb.condition(
                                                "interfaces",
                                                "includes",
                                                ovsdb.oset(interface_uuid)
                                            )],
                                            ["_uuid"]
                                        )
                                    )
                    if result[0]['rows']:
                        port_uuid = result[0]['rows'][0]['_uuid']
                        await _ovsdb_transact(
                                        c,
                                        ovsdb.mutate(
                                            "Bridge",
                                            [ovsdb.condition("name", "==", self._bridgename)],
                                            [ovsdb.mutation("ports",
                                                            "delete",
                                                            ovsdb.oset(port_uuid))]
                                        )
                                    )
            except Exception:
                self._logger.warning("Failed to remove port %r from OVSDB", logicalport_id, exc_info=True)
            # Remove interface
            try:
                await self._parent._taskpool.run_task(
                                            self,
                                            functools.partial(delete_interface,
                                                              ifname,
                                                              netns))
            except Exception:
                self._logger.warning("Failed to remove interface %r in namespace %r",
                                     ifname, netns, exc_info=True)
        with cancelcontext.suppress():
            await self.execute_all([_delete_logport(), _delete_veth()])
        return {}


@defaultconfig
@depend(httpserver.HttpServer, viperflow.ViperFlow, vrouterapi.VRouterApi, ovsdbmanager.OVSDBManager)
class CNIServer(Module):
    # Bind Docker API EndPoint (a HTTP service) to specified vHost
    _default_vhostbind = 'cniserver'
    # OVSDB connection vHost binding
    _default_ovsdbvhost = ''
    # Bridge name
    _default_bridgename = 'br0'
    # Default MTU used for networks
    _default_mtu = 1500
    # Override logical network ID to create logical ports
    _default_logicalnetwork = None
    # Override subnet ID to create logical ports
    _default_subnet = None
    # A template MAC address used on generating MAC addresses
    _default_mactemplate = '02:77:00:00:00:00'
    # Check OVSDB error
    _default_checkerror = True
    def __init__(self, server):
        Module.__init__(self, server)
        self.routines.append(CNIHandler(self))
        self._taskpool = TaskPool(self.scheduler)
        self.routines.append(self._taskpool)
    
    