#!/usr/bin/env python3
"""Post one cast command to an FF1 configured in ff-cli: ff1-cast.py <device-name> <command> ['<request-json>'] [http://host:port]

Used to make a device capture a work for `dp1c.py import-capture`:
  ff1-cast.py kitchen downloadPlaylistItem '{"item": {...the DP-1 item...}}'
  ff1-cast.py kitchen getOfflineCacheStatus
The device then holds items/<sha256(item.source)>.json and its blobs under ~/.cache/offline-artworks (pull them over SSH).
Reads the device list and API key from ~/.config/ff-cli/config.json; nothing is stored here."""

import json, os, sys, urllib.request

cfg = json.load(open(os.path.expanduser("~/.config/ff-cli/config.json")))
name, command = sys.argv[1], sys.argv[2]
request = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
dev = next(d for d in cfg["ff1Devices"]["devices"] if d["name"] == name)
host = sys.argv[4] if len(sys.argv) > 4 else dev["host"]
hdr = {"Content-Type": "application/json"}
if dev.get("apiKey"):
    hdr["API-KEY"] = dev["apiKey"]
req = urllib.request.Request(
    host + "/api/cast", data=json.dumps({"command": command, "request": request}).encode(), headers=hdr, method="POST"
)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        print(r.status, r.read().decode()[:4000])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()[:2000])
    sys.exit(1)
