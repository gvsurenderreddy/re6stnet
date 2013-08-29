import logging, platform, random, socket, subprocess, sys, time
from collections import deque
from itertools import chain
from . import plib, utils

PORT = 326
RTF_CACHE = 0x01000000  # cache entry

# Be careful the refresh interval should let the routes be established


class MultiGatewayManager(dict):

    def __init__(self, gateway):
        self._gw = gateway

    def _route(self, cmd, dest, gw):
        if gw:
            cmd = 'ip', '-4', 'route', cmd, '%s/32' % dest, 'via', gw
            logging.trace('%r', cmd)
            subprocess.call(cmd)

    def add(self, dest, route):
        try:
            self[dest][1] += 1
        except KeyError:
            gw = self._gw(dest) if route else None
            self[dest] = [gw, 0]
            self._route('add', dest, gw)

    def remove(self, dest):
        gw, count = self[dest]
        if count:
            self[dest][1] = count - 1
        else:
            del self[dest]
            try:
                self._route('del', dest, gw)
            except:
                pass

class Connection(object):

    def __init__(self, address_list, iface, prefix):
        self.address_list = address_list
        self.iface = iface
        self.routes = 0
        self._prefix = prefix

    def __iter__(self):
        if not hasattr(self, '_remote_ip_set'):
            self._remote_ip_set = set()
            for ip, port, proto in self.address_list:
                try:
                    socket.inet_pton(socket.AF_INET, ip)
                except socket.error:
                    continue
                self._remote_ip_set.add(ip)
        return iter(self._remote_ip_set)

    def open(self, write_pipe, timeout, encrypt, ovpn_args, _retry=0):
        self.process = plib.client(
            self.iface, (self.address_list[_retry],), encrypt,
            '--tls-remote', '%u/%u' % (int(self._prefix, 2), len(self._prefix)),
            '--resolv-retry', '0',
            '--connect-retry-max', '3', '--tls-exit',
            '--ping-exit', str(timeout),
            '--route-up', '%s %u' % (plib.ovpn_client, write_pipe),
            *ovpn_args)
        _retry += 1
        self._retry = _retry < len(self.address_list) and (
            write_pipe, timeout, encrypt, ovpn_args, _retry)

    def connected(self, db):
        try:
            i = self._retry[-1] - 1
            self._retry = None
        except TypeError:
            i = len(self.address_list) - 1
        if i:
            db.addPeer(self._prefix, ','.join(self.address_list[i]), True)
        else:
            db.connecting(self._prefix, 0)

    def close(self):
        try:
            self.process.stop()
        except (AttributeError, OSError):
            pass # we already polled an exited process

    def refresh(self):
        # Check that the connection is alive
        if self.process.poll() != None:
            logging.info('Connection with %s has failed with return code %s',
                         self._prefix, self.process.returncode)
            if not self._retry:
                return False
            logging.info('Retrying with alternate address')
            self.close()
            self.open(*self._retry)
        return True


class TunnelManager(object):

    def __init__(self, write_pipe, peer_db, openvpn_args, timeout,
                refresh, client_count, iface_list, network, prefix,
                address, ip_changed, encrypt, remote_gateway, disable_proto):
        self._write_pipe = write_pipe
        self._peer_db = peer_db
        self._connecting = set()
        self._connection_dict = {}
        self._disconnected = None
        self._distant_peers = []
        self._iface_to_prefix = {}
        self._ovpn_args = openvpn_args
        self._timeout = timeout
        self._refresh_time = refresh
        self._network = network
        self._iface_list = iface_list
        self._prefix = prefix
        self._address = utils.dump_address(address)
        self._ip_changed = ip_changed
        self._encrypt = encrypt
        self._gateway_manager = MultiGatewayManager(remote_gateway) \
                                if remote_gateway else None
        self._disable_proto = disable_proto
        self._served = set()

        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        # See also http://stackoverflow.com/questions/597225/
        # about binding and anycast.
        self.sock.bind(('::', PORT))

        self.next_refresh = time.time()
        self._next_tunnel_refresh = time.time()

        self._client_count = client_count
        self._refresh_count = 1
        self.new_iface_list = deque('re6stnet' + str(i)
            for i in xrange(1, self._client_count + 1))
        self._free_iface_list = []

    def _tuntap(self, iface=None):
        if iface:
            self.new_iface_list.appendleft(iface)
            action = 'del'
        else:
            iface = self.new_iface_list.popleft()
            action = 'add'
        args = 'ip', 'tuntap', action, 'dev', iface, 'mode', 'tap'
        logging.debug('%r', args)
        subprocess.call(args)
        return iface

    def delInterfaces(self):
        iface_list = self._free_iface_list
        iface_list += self._iface_to_prefix
        self._iface_to_prefix.clear()
        while iface_list:
          self._tuntap(iface_list.pop())

    def getFreeInterface(self, prefix):
        try:
          iface = self._free_iface_list.pop()
        except IndexError:
          iface = self._tuntap()
        self._iface_to_prefix[iface] = prefix
        return iface

    def freeInterface(self, iface):
        self._free_iface_list.append(iface)
        del self._iface_to_prefix[iface]

    def refresh(self):
        logging.debug('Checking tunnels...')
        self._cleanDeads()
        remove = self._next_tunnel_refresh < time.time()
        if remove:
            self._countRoutes()
            self._removeSomeTunnels()
            self._next_tunnel_refresh = time.time() + self._refresh_time
            self._peer_db.log()
        self._makeNewTunnels(remove)
        if remove and self._free_iface_list:
            self._tuntap(self._free_iface_list.pop())
        self.next_refresh = time.time() + 5

    def _cleanDeads(self):
        for prefix in self._connection_dict.keys():
            if not self._connection_dict[prefix].refresh():
                self._kill(prefix)

    def _removeSomeTunnels(self):
        # Get the candidates to killing
        candidates = sorted(self._connection_dict, key=lambda p:
                self._connection_dict[p].routes)
        for prefix in candidates[0: max(0, len(self._connection_dict) -
                self._client_count + self._refresh_count)]:
            self._kill(prefix)

    def _kill(self, prefix):
        logging.info('Killing the connection with %u/%u...',
                     int(prefix, 2), len(prefix))
        connection = self._connection_dict.pop(prefix)
        self.freeInterface(connection.iface)
        connection.close()
        if self._gateway_manager is not None:
            for ip in connection:
                self._gateway_manager.remove(ip)
        logging.trace('Connection with %u/%u killed',
                      int(prefix, 2), len(prefix))

    def _makeTunnel(self, prefix, address):
        assert len(self._connection_dict) < self._client_count, (prefix, self.__dict__)
        if prefix in self._served or prefix in self._connection_dict:
            return False
        assert prefix != self._prefix, self.__dict__
        address = [x for x in utils.parse_address(address)
                     if x[2] not in self._disable_proto]
        self._peer_db.connecting(prefix, 1)
        if not address:
            return False
        logging.info('Establishing a connection with %u/%u',
                     int(prefix, 2), len(prefix))
        iface = self.getFreeInterface(prefix)
        self._connection_dict[prefix] = c = Connection(address, iface, prefix)
        if self._gateway_manager is not None:
            for ip in c:
                self._gateway_manager.add(ip, True)
        c.open(self._write_pipe, self._timeout, self._encrypt, self._ovpn_args)
        return True

    def _makeNewTunnels(self, route_counted):
        count = self._client_count - len(self._connection_dict)
        if not count:
            return
        assert count >= 0
        # CAVEAT: Forget any peer that didn't reply to our previous address
        #         request, either because latency is too high or some packet
        #         was lost. However, this means that some time should pass
        #         before calling _makeNewTunnels again.
        self._connecting.clear()
        distant_peers = self._distant_peers
        if len(distant_peers) < count and not route_counted:
            self._countRoutes()
        disconnected = self._disconnected
        if disconnected is not None:
            logging.info("No route to registry (%u neighbours, %u distant"
                         " peers)", len(disconnected), len(distant_peers))
            # We aren't the registry node and we have no tunnel to or from it,
            # so it looks like we are not connected to the network, and our
            # neighbours are in the same situation.
            self._disconnected = None
            disconnected = set(disconnected).union(distant_peers)
            if disconnected:
                # We do have neighbours that are probably also disconnected,
                # so force rebootstrapping.
                peer = self._peer_db.getBootstrapPeer()
                if not peer:
                    # Registry dead ? Assume we're connected after all.
                    disconnected = None
                elif peer[0] not in disconnected:
                    # Got a node that will probably help us rejoining the
                    # network, so connect to it.
                    count -= self._makeTunnel(*peer)
        if disconnected is None:
            # Normal operation. Choose peers to connect to by looking at the
            # routing table.
            while count and distant_peers:
                i = random.randrange(0, len(distant_peers))
                peer = distant_peers[i]
                distant_peers[i] = distant_peers[-1]
                del distant_peers[-1]
                address = self._peer_db.getAddress(peer)
                if address:
                    count -= self._makeTunnel(peer, address)
                else:
                    ip = utils.ipFromBin(self._network + peer)
                    # TODO: Send at least 1 address. This helps the registry
                    #       node filling its cache when building a new network.
                    try:
                        self.sock.sendto('\2', (ip, PORT))
                    except socket.error, e:
                        logging.info('Failed to query %s (%s)', ip, e)
                    self._connecting.add(peer)
                    count -= 1
        elif count:
            # No route/tunnel to registry, which usually happens when starting
            # up. Select peers from cache for which we have no route.
            new = 0
            bootstrap = True
            for peer, address in self._peer_db.getPeerList():
                if peer not in disconnected:
                    logging.info("Try to bootstrap using peer %u/%u",
                                 int(peer, 2), len(peer))
                    bootstrap = False
                    if self._makeTunnel(peer, address):
                        new += 1
                        if new == count:
                            return
            if not (new or disconnected):
                if bootstrap:
                    # Startup without any good address in the cache.
                    peer = self._peer_db.getBootstrapPeer()
                    if peer and self._makeTunnel(*peer):
                        return
                # Failed to bootstrap ! Last change to connect is to
                # retry an address that already failed :(
                for peer in self._peer_db.getPeerList(1):
                    if self._makeTunnel(*peer):
                        break

    if sys.platform == 'cygwin':
        def _iterRoutes(self):
            # Before Vista
            if platform.system()[10:11] == '5':
                args = ('netsh', 'interface', 'ipv6', 'show', 'route', 'verbose')
            else:
                args = ('ipwin', 'ipv6', 'show', 'route', 'verbose')
            routing_table = subprocess.check_output(args, stderr=subprocess.STDOUT)
            for line in routing_table.splitlines():
                fs = line.split(':', 1)
                test = fs[0].startswith
                if test('Prefix'):
                    prefix, prefix_len = fs[1].split('/', 1)
                elif test('Interface'):
                    yield (fs[1].strip(),
                           utils.binFromIp(prefix.strip()),
                           int(prefix_len))
    else:
        def _iterRoutes(self):
            with open('/proc/net/ipv6_route') as f:
                routing_table = f.read()
            for line in routing_table.splitlines():
                line = line.split()
                iface = line[-1]
                if iface != 'lo' and not (int(line[-2], 16) & RTF_CACHE):
                    yield (iface, bin(int(line[0], 16))[2:].rjust(128, '0'),
                                  int(line[1], 16))

    def _countRoutes(self):
        logging.debug('Starting to count the routes on each interface...')
        del self._distant_peers[:]
        for conn in self._connection_dict.itervalues():
            conn.routes = 0
        a = len(self._network)
        b = a + len(self._prefix)
        other = []
        for iface, ip, prefix_len in self._iterRoutes():
            if ip[:a] == self._network and ip[a:b] != self._prefix:
                prefix = ip[a:prefix_len]
                logging.trace('Route on iface %s detected to %s/%u',
                              iface, utils.ipFromBin(ip), prefix_len)
                nexthop = self._iface_to_prefix.get(iface)
                if nexthop:
                    self._connection_dict[nexthop].routes += 1
                if prefix in self._served or prefix in self._connection_dict:
                    continue
                if iface in self._iface_list:
                    other.append(prefix)
                else:
                    self._distant_peers.append(prefix)
        is_registry = self._peer_db.registry_ip[a:].startswith
        if is_registry(self._prefix) or any(is_registry(peer)
              for peer in chain(self._distant_peers, other,
                                self._served, self._connection_dict)):
            self._disconnected = None
            # XXX: When there is no new peer to connect when looking at routes
            #      coming from tunnels, we'd like to consider those discovered
            #      from the LAN. However, we don't want to create tunnels to
            #      nodes of the LAN so do nothing until we find a way to get
            #      some information from Babel.
            #if not self._distant_peers:
            #    self._distant_peers = other
        else:
            self._disconnected = other
        logging.debug("Routes counted: %u distant peers",
                      len(self._distant_peers))
        for c in self._connection_dict.itervalues():
            logging.trace('- %s: %s', c.iface, c.routes)

    def killAll(self):
        for prefix in self._connection_dict.keys():
            self._kill(prefix)

    def handleTunnelEvent(self, msg):
        try:
            msg = msg.rstrip()
            args = msg.split()
            m = getattr(self, '_ovpn_' + args.pop(0).replace('-', '_'))
        except (AttributeError, ValueError):
            logging.warning("Unknown message received from OpenVPN: %s", msg)
        else:
            logging.debug(msg)
            m(*args)

    def _ovpn_client_connect(self, common_name, trusted_ip):
        prefix = utils.binFromSubnet(common_name)
        self._served.add(prefix)
        if self._gateway_manager is not None:
            self._gateway_manager.add(trusted_ip, False)
        if prefix in self._connection_dict and self._prefix < prefix:
            self._kill(prefix)
            self._peer_db.connecting(prefix, 0)

    def _ovpn_client_disconnect(self, common_name, trusted_ip):
        prefix = utils.binFromSubnet(common_name)
        try:
            self._served.remove(prefix)
        except KeyError:
            return
        if self._gateway_manager is not None:
            self._gateway_manager.remove(trusted_ip)

    def _ovpn_route_up(self, common_name, ip):
        prefix = utils.binFromSubnet(common_name)
        try:
            self._connection_dict[prefix].connected(self._peer_db)
        except KeyError:
            pass
        if self._ip_changed:
            self._address = utils.dump_address(self._ip_changed(ip))

    def handlePeerEvent(self):
        msg, address = self.sock.recvfrom(1<<16)
        if not (msg or utils.binFromIp(address[0]).startswith(self._network)):
            return
        code = ord(msg[0])
        if code == 1: # answer
            # We parse the message in a way to discard a truncated line.
            for peer in msg[1:].split('\n')[:-1]:
                try:
                    prefix, address = peer.split()
                    int(prefix, 2)
                except ValueError:
                    break
                if prefix != self._prefix:
                    self._peer_db.addPeer(prefix, address)
                    try:
                        self._connecting.remove(prefix)
                    except KeyError:
                        continue
                    self._makeTunnel(prefix, address)
        elif code == 2: # request
            encode = '%s %s\n'.__mod__
            if self._address:
                msg = [encode((self._prefix, self._address))]
            else: # I don't know my IP yet!
                msg = []
            # Add an extra random peer, mainly for the registry.
            if random.randint(0, self._peer_db.getPeerCount()):
                msg.append(encode(self._peer_db.getPeerList().next()))
            if msg:
                try:
                    self.sock.sendto('\1' + ''.join(msg), address[:2])
                except socket.error, e:
                    logging.info('Failed to reply to %s (%s)', address, e)
        elif code == 255:
            # the registry wants to know the topology for debugging purpose
            if utils.binFromIp(address[0]) == self._peer_db.registry_ip:
                msg = ['\xfe%s%u/%u\n%u\n' % (msg[1:],
                    int(self._prefix, 2), len(self._prefix),
                    len(self._connection_dict))]
                msg.extend('%u/%u\n' % (int(x, 2), len(x))
                           for x in (self._connection_dict, self._served)
                           for x in x)
                try:
                    self.sock.sendto(''.join(msg), address[:2])
                except socket.error, e:
                    pass
