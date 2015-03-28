#!/usr/bin/env python

# Requirements:
# - mininet (apt-get install mininet)
# - dnspython (apt-get install python-dnspython)
# - requests (apt-get install python-requests)
#

import getopt
import os
import signal
import sys
import shlex
import subprocess
import time

import requests
import dns.name
import dns.message
import dns.query
import dns.flags
from dns.exception import Timeout

from mininet.net import Mininet
from mininet.cli import CLI
from mininet.log import lg
from mininet.node import Node, OVSKernelSwitch
from mininet.topolib import TreeTopo
from mininet.topo import SingleSwitchTopo
from mininet.link import Link
from mininet.node import OVSController


########################################################################################################################

def log(msg):
    print("### %s" % msg)

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

    switch = net.switches[0] # switch to use
    ip     = '10.123.123.1'  # our IP address on host network
    routes = ['10.0.0.0/8']  # host networks to route to

    # Create a node in root namespace and link to switch 0
    log("Creating root node...")
    root = Node('root', inNamespace=False)
    intf = Link(root, switch).intf1
    log("... link: %s" % intf)
    root.setIP(ip, 8, intf)
    log("... ip: %s" % ip)

    time.sleep(1)

    # Add routes from root ns to hosts
    log("Adding routes from root...")
    [root.cmd('route add -net ' + route + ' dev ' + str(intf)) for route in routes]

    log("Fixing mDNS routing")
    [h.cmd('route add 224.0.0.251 dev %s' % h.intf()) for h in net.hosts]

    # Start network that now includes link to root namespace
    log("Starting network")
    net.start()

    return net


def stopTopology(net):
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


def publishName(server, port, container, name, ip):
    log("Publishing name '%s' at %s..." % (name, server))
    serverUrl = "http://%s:%d/name/%s/%s" % (server, port, container, ip)
    params = {'fqdn': name}
    r = requests.put(serverUrl, params=params)


def runWeaveDNS(h, exe, timeout, debug=False):
    log("Running WeaveDNS at %s [%s, dev:%s]" % (h.name, h.IP(), h.intf()))
    cmdLine = '%s -watch=false -debug -iface="%s" -wait=0' % (exe, h.intf())
    if debug:
        log(" ... running: %s" % cmdLine)
    p = h.popen(shlex.split(cmdLine), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p


def deleteName(server, port, container, name, ip):
    log("Deleting name '%s' at %s..." % (name, server))
    serverUrl = "http://%s:%d/name/%s/%s" % (server, port, container, ip)
    params = {'fqdn': name}
    r = requests.delete(serverUrl, params=params)


def resolveNameAt(name, server):
    """
    Resolve name in a server
    """
    ADDITIONAL_RDCLASS = 65535

    response = None
    name = dns.name.from_text(name)
    if not name.is_absolute():
        name = name.concatenate(dns.name.root)

    try:
        request = dns.message.make_query(name, dns.rdatatype.A)
        request.flags |= dns.flags.AD
        request.find_rrset(request.additional, dns.name.root, ADDITIONAL_RDCLASS, dns.rdatatype.OPT,
                           create=True, force_unique=True)

        log("Sending DNS (UDP) query about '%s' to '%s'..." % (name, server.IP()))
        response = dns.query.udp(request, server.IP(), timeout=3.0)

    except Timeout, e:
        log("ERROR: Timeout while waiting for response")
    else:
        log("Received answer from '%s'" % server.IP())
        log("... answer:     %s" % response.answer)
        log("... additional: %s" % response.additional)
        log("... authority:  %s" % response.authority)

    return response


def dumpProcOut(p, ident=""):
    out, err = p.communicate()
    for l in out.split('\n'):
        if len(l) > 0:
            log(" %s %s" % (ident, l))
    for l in err.split('\n'):
        if len(l) > 0:
            log(" %s ERROR: %s" % (ident, l))


def killProc(p):
    try:
        log("Killing process at %s" % h.name)
        p.send_signal(signal.SIGINT)
        time.sleep(1)
        p.kill()
    except OSError:
        pass


########################################################################################################################

ARGS_HELP='ARGS: -w <weavedns>'

if __name__ == '__main__':

    num = 2
    weavedns = ''
    timeout = 10
    debug = False
    connCheck = False

    try:
        opts, args = getopt.getopt(sys.argv[1:], "hn:w:t:dc", ["num=", "exe=", "weavedns=", "time=", "debug", "conn-check"])
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
        elif opt in ("-n", "--num"):
            num = int(arg)
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

    net = None    
    hw = []

    try:
        net = startTopology(num)

        if connCheck:
            connCheckBetween(net.hosts[0], net.hosts[1])
            connMulticastCheckBetween(net.hosts[0], net.hosts[1])

        for h in net.hosts:
            time.sleep(1)
            w = runWeaveDNS(h, weavedns, timeout=timeout, debug=debug)
            hw.append((h, w))

        # time.sleep(3)
        # publishName(h2.IP(), 6785, 'container', 'something.weave.local.', '10.0.0.9')
        #time.sleep(1)
        #response = resolveNameAt('something.weave.local.', h1.IP())

        time.sleep(timeout)

        for (h, w) in hw:
            dumpDefaultDevice(h)

    except KeyboardInterrupt:
        print "Interrupted... exiting"
    finally:
        print ">>> Done... bye"
        for (h, w) in hw:
            if w:
                killProc(w)
                dumpProcOut(w, "[%s/%s]" % (h.name, h.IP()))

        stopTopology(net)

