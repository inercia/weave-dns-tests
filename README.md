# WeaveDNS tests

WeaveDNS distributed tests

These tests create multiple instances of a WeaveDNS server, modifying the ZoneDB
in one of them and asking other instances about some names, IPs, etc...

## Usage:

Just run the script with root privileges and pointing to the WeaveDNS executable.

```
$ sudo ./dns-tests.py -w $GOPATH/go/src/github.com/zettio/weave/weavedns/weavedns
```

Requirements:

- mininet     (apt-get install mininet)
- dnspython   (apt-get install python-dnspython)
- requests    (apt-get install python-requests)



