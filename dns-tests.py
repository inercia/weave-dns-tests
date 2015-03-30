#!/usr/bin/env python
#
# Usage:
# Just run the script with root privileges and pointing to the WeaveDNS executable.
#
# $ sudo ./dns-tests.py -w $GOPATH/src/github.com/zettio/weave/weavedns/weavedns
#
# Requirements:
#
# - mininet     (apt-get install mininet)
# - dnspython   (apt-get install python-dnspython)
# - requests    (apt-get install python-requests)
#

import getopt
import os
import signal
import sys
import shlex
import subprocess
import time
from contextlib import contextmanager

import requests
import dns.name
import dns.message
import dns.query
import dns.flags
from dns.exception import Timeout
from dns import reversename

from mininet.net import Mininet
from mininet.cli import CLI
from mininet.log import lg
from mininet.node import Node, OVSKernelSwitch
from mininet.topolib import TreeTopo
from mininet.topo import SingleSwitchTopo
from mininet.link import Link
from mininet.node import OVSController


# WeaveDNS HTTP API port
WEAVEDNS_HTTP_PORT = 6785

# negative cache  TTL
CACHE_NEG_TTL = 30

#
ADDITIONAL_RDCLASS = 65535

########################################################################################################################

class SetupError(Exception):
    pass


class TestError(Exception):
    pass


def log(msg):
    print("### %s" % msg)


def error(msg):
    log("ERROR: %s" % msg)

def success(msg):
    log("SUCCESS: ***** %s *****" % msg)

#######################
# Topology & Processes
#######################

def startTopology(num):
    """
    Start the topology
    """
    try:
        topo = SingleSwitchTopo(num)
        net = Mininet(topo, controller=OVSController)
    except Exception, e:
        print("ERROR: %s" % e)
        sys.exit(2)

    switch = net.switches[0]  # switch to use
    ip = '10.123.123.1'  # our IP address on host network
    routes = ['10.0.0.0/8']  # host networks to route to

    # Create a node in root namespace and link to switch 0
    log("Creating root node...")
    root = Node('root', inNamespace=False)
    intf = Link(root, switch).intf1
    log("... link: %s" % intf)
    root.setIP(ip, 8, intf)
    log("... ip: %s" % ip)

    time.sleep(1)

    # Start network that now includes link to root namespace
    log("Starting network")
    net.start()

    # Add routes from root ns to hosts
    log("Adding routes from root...")
    for route in routes:
        root.cmd('route add -net ' + route + ' dev ' + str(intf))

    log("Fixing mDNS routing")
    for h in net.hosts:
        h.cmd('route add 224.0.0.251 dev %s' % h.intf())

    return net


def stopTopology(net):
    if net:
        net.stop()


def connCheckBetween(h1, h2):
    """
    Perform a connectivity check between two hosts
    """
    log("Performing connectivity checks...")
    log("... pinging from %s to %s" % (h1.IP(), h2.IP()))
    print(h1.cmd('ping -c1 %s' % h2.IP()))
    log("... pinging from %s to %s" % (h2.IP(), h1.IP()))
    print(h2.cmd('ping -c1 %s' % h1.IP()))


def connMulticastCheckBetween(h1, h2):
    """
    Perform a multicast connectivity check between two hosts
    """
    log("Performing multicast connectivity checks...")
    p1 = h1.popen(shlex.split("timeout 3s iperf -s -u -B 224.0.0.251 -i 1 -t 3"),
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = h2.popen(shlex.split("timeout 3s iperf -c 224.0.0.251 -u -T 32 -i 1 -t 3"),
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    p1.wait()
    p2.wait()
    dumpProcOut(p1, "[h1/receiver]")
    dumpProcOut(p2, "[h2/sender]")


def dumpDefaultDevice(h):
    for l in h.cmd("ifconfig %s" % h.intf()).splitlines():
        log(" [%s] %s" % (h.name, l))


def runWeaveDNS(h, exe, debug=False):
    log("Running WeaveDNS at %s [%s, dev:%s]" % (h.name, h.IP(), h.intf()))
    cmdLine = '%s -watch=false -debug -iface="%s" -wait=0' % (exe, h.intf())
    if debug:
        log(" ... running: %s" % cmdLine)
    p = h.popen(shlex.split(cmdLine), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    log("Waiting for WaveDNS HTTP API")
    serverUrl = "http://%s:%d/status" % (h.IP(), WEAVEDNS_HTTP_PORT)
    connected = False
    tries = 10
    while tries > 0 and not connected:
        try:
            r = requests.get(serverUrl, timeout=10)
            connected = True
        except requests.exceptions.Timeout, e:
            tries = 0
        except requests.exceptions.ConnectionError, e:
            time.sleep(1)
            tries -= 1

    if not connected:
        killProc(h, p)
        dumpProcOut(p, "[%s/%s]" % (h.name, h.IP()))
        raise SetupError("Could not get /status from server %s" % h.IP())

    return p


def wait(secs, reason):
    log("Waiting %d secs for %s..." % (secs, reason))
    time.sleep(secs)


#######################
# WeaveDNS HTTP API
#######################

def publishName(server, container, name, ip):
    log("Publishing name '%s' at %s..." % (name, server))
    serverUrl = "http://%s:%d/name/%s/%s" % (server.IP(), WEAVEDNS_HTTP_PORT, container, ip)
    params = {'fqdn': name}
    try:
        r = requests.put(serverUrl, params=params)
    except requests.exceptions.ConnectionError, e:
        error("Could not PUT to %s: %s" % (serverUrl, e))
        raise TestError(e)


def deleteName(server, container, name, ip):
    log("Deleting name '%s' at %s..." % (name, server))
    serverUrl = "http://%s:%d/name/%s/%s" % (server.IP(), WEAVEDNS_HTTP_PORT, container, ip)
    params = {'fqdn': name}
    try:
        r = requests.delete(serverUrl, params=params)
    except requests.exceptions.ConnectionError, e:
        error("Could not DELETE to %s: %s" % (serverUrl, e))
        raise TestError(e)


#######################
# DNS resolutions
#######################

def resolveNameAt(name, server):
    """
    Resolve name in a server
    """
    addresses = set()
    response = None
    name = dns.name.from_text(name)
    if not name.is_absolute():
        name = name.concatenate(dns.name.root)

    try:
        request = dns.message.make_query(name, dns.rdatatype.A)
        request.flags |= dns.flags.AD
        request.find_rrset(request.additional, dns.name.root, ADDITIONAL_RDCLASS, dns.rdatatype.OPT,
                           create=True, force_unique=True)

        log("Sending A-query (UDP) about '%s' to '%s'..." % (name, server.IP()))
        response = dns.query.udp(request, server.IP(), timeout=3.0)

    except Timeout, e:
        log("ERROR: Timeout while waiting for response")
    else:
        log("Received answer from '%s'" % server.IP())
        log("... answer:     %s" % response.answer)
        for rr in response.answer:
            for rdata in rr.items:
                log("...... address: %s" % rdata.address)
                addresses.add(rdata.address)
        log("... additional: %s" % response.additional)
        log("... authority:  %s" % response.authority)

    return addresses


def resolveIPAt(ip, server):
    """
    Resolvean IP in a server
    """
    names = set()
    response = None    
    name = reversename.from_address(ip)
    try:
        request = dns.message.make_query(name, dns.rdatatype.PTR)
        request.flags |= dns.flags.AD
        request.find_rrset(request.additional, dns.name.root, ADDITIONAL_RDCLASS, dns.rdatatype.OPT,
                           create=True, force_unique=True)

        log("Sending PTR-query (UDP) about '%s' to '%s'..." % (name, server.IP()))
        response = dns.query.udp(request, server.IP(), timeout=3.0)

    except Timeout, e:
        log("ERROR: Timeout while waiting for response")
    else:
        log("Received answer from '%s'" % server.IP())
        log("... answer:     %s" % response.answer)
        for rr in response.answer:
            for rdata in rr.items:
                log("...... name: %s" % rdata.target)
                names.add(str(rdata.target))
        log("... additional: %s" % response.additional)
        log("... authority:  %s" % response.authority)

    return names


#######################
# Processes
#######################

def dumpProcOut(p, ident=""):
    out, err = p.communicate()
    for l in out.split('\n'):
        if len(l) > 0:
            log(" %s %s" % (ident, l))
    for l in err.split('\n'):
        if len(l) > 0:
            log(" %s ERROR: %s" % (ident, l))


def killProc(h, p):
    try:
        log("[%s] Killing process" % h.name)
        p.send_signal(signal.SIGINT)
        time.sleep(1)
        p.kill()
    except OSError:
        pass


@contextmanager
def weaveDnsNetwork(num, weavedns, connCheck=False, debug=False):
    net = None
    hw = []
    rc = 0

    try:
        net = startTopology(num)

        if connCheck:
            connCheckBetween(net.hosts[0], net.hosts[1])
            connMulticastCheckBetween(net.hosts[0], net.hosts[1])

        for h in net.hosts:
            time.sleep(1)
            w = runWeaveDNS(h, weavedns, debug=debug)
            hw.append((h, w))

        yield net

    except SetupError, e:
        error("Could not setup test: %s" % e)
        rc = 1
    except TestError, e:
        error("--------------------------")
        error("Test failed: %s" % str(e))
        error("--------------------------")
    except KeyboardInterrupt:
        log("Interrupted... exiting")
        rc = 1
    except Exception, e:
    	rc = 1
    finally:
        log("Stopping everything...")
        for (h, w) in hw:
            if w:
                killProc(h, w)
                if debug:
                    dumpProcOut(w, "[%s/%s]" % (h.name, h.IP()))

        stopTopology(net)
        log("Done... bye")
        if rc:
        	sys.exit(rc)

#######################
# Assertions
#######################

def assertEmptySet(addrs):
    if len(addrs) != 0:
        raise TestError("Did expect an expty set")
    log("Good! No results obtained...")

def assertIPInSet(ip, addrs):
    if not ip in addrs:
        raise TestError("Did not find %s in the list of IPs" % (ip))
    log("Good! IP found...")

def assertNameInSet(name, names):
    if not name in names:
        raise TestError("Did not find %s in set %s" % (name, names))
    log("Good! Name found...")


##########################################################################################################
# Main tests
##########################################################################################################

ARGS_HELP = 'ARGS: --exe=<weavedns-exe> --weavedns= --time= --debug", "conn-check"'

if __name__ == '__main__':

    weavedns = ''
    timeout = 10
    debug = False
    connCheck = False

    try:
        opts, args = getopt.getopt(sys.argv[1:], "hn:w:t:dc",
                                   ["exe=", "weavedns=", "time=", "debug", "conn-check"])
    except getopt.GetoptError:
        print ARGS_HELP
        sys.exit(2)

    for opt, arg in opts:
        if opt == '-h':
            print ARGS_HELP
            sys.exit()
        elif opt in ("-d", "--debug"):
            debug = True
        elif opt in ("-c", "--conn-check"):
            connCheck = True
        elif opt in ("-w", "--weavedns", "--exe"):
            log("Using WeaveDNS from %s" % arg)
            weavedns = arg
        elif opt in ("-t", "--time"):
            timeout = int(arg)
        else:
            log("Unknown option %s" % opt)
            sys.exit(2)

    if not weavedns:
        here = os.path.dirname(os.path.realpath(__file__))
        weavedns = os.path.join(here, 'weavedns', 'weavedns')

    if not os.path.exists(weavedns):
        log("Could not find WeaveDNS executable at %s" % weavedns)
        sys.exit(1)

    ###################################################

    lg.setLogLevel('info')

    log("Testing distributed A-query with negative cache")
    with weaveDnsNetwork(2, weavedns, debug=debug) as net:
        NAME = 'something.weave.local.'
        IP = '10.0.0.9'

        h1, h2 = net.hosts[0], net.hosts[1]

        publishName(h2, 'container', NAME, IP)
        addresses = resolveNameAt(NAME, h1)
        assertIPInSet(IP, addresses)

        deleteName(h2, 'container', NAME, IP)
        wait(CACHE_NEG_TTL + 1, "cache expiration")

        addresses = resolveNameAt(NAME, h1)
        assertEmptySet(addresses)

        publishName(h2, 'container', NAME, IP)
        wait(CACHE_NEG_TTL + 1, "cache expiration")
        addresses = resolveNameAt(NAME, h1)
        assertIPInSet(IP, addresses)
        success("Distributed A-query was OK!")

    log("Testing distributed PTR-query with negative cache")
    with weaveDnsNetwork(2, weavedns, debug=debug) as net:
        NAME = 'something.weave.local.'
        IP = '10.0.0.9'

        h1, h2 = net.hosts[0], net.hosts[1]

        publishName(h2, 'container', NAME, IP)
        names = resolveIPAt(IP, h1)
        assertNameInSet(NAME, names)

        deleteName(h2, 'container', NAME, IP)
        wait(CACHE_NEG_TTL + 1, "cache expiration")

        names = resolveIPAt(IP, h1)
        assertEmptySet(names)

        publishName(h2, 'container', NAME, IP)
        wait(CACHE_NEG_TTL + 1, "cache expiration")
        names = resolveIPAt(IP, h1)
        assertNameInSet(NAME, names)
        success("Distributed PTR-query was OK!")
