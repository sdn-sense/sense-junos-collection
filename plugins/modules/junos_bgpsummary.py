#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Junos BGP Summary
Copyright: Contributors to the SENSE Project
GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

Runs 'show bgp summary | display json' and normalizes the result into the
shared SiteRM BGP schema: {"vrf": ..., "afi_checked": [...], "peers": [...]}.

Junos's summary view has no advertised/sent-prefix counter at all (confirmed
live: the header lists only Active/Received/Accepted/Damped), and it also
has no device-wide local-AS field. When 'detail' is requested (default
True), one extra call per established peer -- 'show bgp neighbor <peer-ip>
| display xml', a single well-formed document, unlike Dell OS10's
concatenated multi-document XML -- fills in both prefixes_advertised and
local_asn from the <bgp-rib>/<local-as> elements.

Junos's summary command has no AFI filter and returns all peers in one
call regardless of family, and does not otherwise mark family membership
for peers that are currently down, so ipv4 vs ipv6 is derived from whether
the peer address itself contains ':' -- reliable since it's the peer's own
IP, independent of routing-table membership.

Title                   : sdn-sense/sense-junos-collection
Author                  : Justas Balcas
Email                   : juztas (at) gmail.com
@Copyright              : General Public License v3.0+
Date                    : 2026/09/15
"""
from __future__ import (absolute_import, division, print_function)

__metaclass__ = type

import json
import xml.etree.ElementTree as ET

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.sense.junos.plugins.module_utils.network.junos import (
    check_args, junos_argument_spec, run_commands)
from ansible_collections.sense.junos.plugins.module_utils.runwrapper import \
    functionwrapper

_KNOWN_STATES = ("established", "idle", "active", "connect", "opensent", "openconfirm")


@functionwrapper
def normalizeBgpState(rawstate):
    """Normalize a vendor BGP FSM state string to the shared enum."""
    state = str(rawstate).lower()
    return state if state in _KNOWN_STATES else "unknown"


@functionwrapper
def firstData(node, key):
    """Junos's 'display json' wraps every leaf as [{"data": value, ...}].
    Return the first entry's 'data', or None if the key/list is absent."""
    values = (node or {}).get(key)
    if not values:
        return None
    return values[0].get("data")


@functionwrapper
def buildSummaryCommand(vrf):
    """Build the Junos 'show bgp summary' command.
    ASSUMPTION (unverified): 'instance <vrf>' was not tested live -- only
    the default-instance form (no vrf) was confirmed."""
    if vrf:
        return f"show bgp summary instance {vrf} | display json"
    return "show bgp summary | display json"


@functionwrapper
def buildNeighborDetailCommand(vrf, peer):
    """Build the Junos single-peer detail command used for advertised
    counts and local ASN.
    ASSUMPTION (unverified): 'instance <vrf>' placement was not tested."""
    bareaddr = peer.split("+")[0]
    if vrf:
        return f"show bgp neighbor {bareaddr} instance {vrf} | display xml"
    return f"show bgp neighbor {bareaddr} | display xml"


@functionwrapper
def parseBgpSummaryJson(rawjson, peers):
    """Parse 'show bgp summary | display json' output, appending one
    normalized peer per entry, regardless of address family."""
    try:
        data = json.loads(rawjson)
    except ValueError:
        return
    bgpinfo = data.get("bgp-information")
    if not bgpinfo:
        return
    for peernode in bgpinfo[0].get("bgp-peer", []):
        peeraddr = (firstData(peernode, "peer-address") or "").split("+")[0]
        if not peeraddr:
            continue
        iptype = "ipv6" if ":" in peeraddr else "ipv4"
        uptimeseconds = None
        elapsed = (peernode.get("elapsed-time") or [{}])[0]
        secondstext = elapsed.get("attributes", {}).get("junos:seconds")
        if secondstext is not None:
            try:
                uptimeseconds = int(secondstext)
            except ValueError:
                uptimeseconds = None
        received = accepted = None
        for ribnode in peernode.get("bgp-rib", []):
            received = _toint(firstData(ribnode, "received-prefix-count"))
            accepted = _toint(firstData(ribnode, "accepted-prefix-count"))
            break
        peers.append({
            "peer": peeraddr,
            "iptype": iptype,
            "local_asn": None,
            "remote_asn": _toint(firstData(peernode, "peer-as")),
            "state": normalizeBgpState(firstData(peernode, "peer-state") or ""),
            "uptime_seconds": uptimeseconds,
            "prefixes_received": received if received is not None else accepted,
            "prefixes_advertised": None,
            "advertised_known": False,
        })


def _toint(value):
    """Best-effort int conversion, None on failure/absence."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@functionwrapper
def fillAdvertisedAndLocalAsn(module, vrf, peer):
    """Fetch and parse the advertised-prefix count (and local ASN, not
    available from the summary view) for one established peer via the
    single-peer XML detail command."""
    command = buildNeighborDetailCommand(vrf, peer["peer"])
    try:
        responses = run_commands(module, [command])
    except Exception:  # pylint: disable=broad-except
        return
    rawxml = responses[0] if responses else ""
    try:
        root = ET.fromstring(rawxml)
    except ET.ParseError:
        return
    # Junos XML tags are namespaced (e.g. "{http://xml.juniper.net/...}bgp-peer"),
    # so match on the local tag name rather than a namespace-qualified find().
    peerel = None
    for el in root.iter():
        if el.tag == "bgp-peer" or el.tag.endswith("}bgp-peer"):
            peerel = el
            break
    if peerel is None:
        return

    def findtext(tag):
        for el in peerel.iter():
            if el.tag == tag or el.tag.endswith("}" + tag):
                return el.text
        return None

    localasn = _toint(findtext("local-as"))
    if localasn is not None:
        peer["local_asn"] = localasn
    ribel = None
    for el in peerel.iter():
        if el.tag == "bgp-rib" or el.tag.endswith("}bgp-rib"):
            ribel = el
            break
    if ribel is None:
        return

    def ribtext(tag):
        for el in ribel.iter():
            if el.tag == tag or el.tag.endswith("}" + tag):
                return el.text
        return None

    advertised = _toint(ribtext("advertised-prefix-count"))
    if advertised is not None:
        peer["prefixes_advertised"] = advertised
        peer["advertised_known"] = True
    received = _toint(ribtext("received-prefix-count"))
    if received is not None:
        peer["prefixes_received"] = received


@functionwrapper
def main():
    """main entry point for module execution"""
    argument_spec = {
        "vrf": {"type": "str", "default": ""},
        "type": {"type": "str", "default": "both", "choices": ["ipv4", "ipv6", "both"]},
        "detail": {"type": "bool", "default": True},
    }
    argument_spec.update(junos_argument_spec)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    warnings = []
    check_args(module, warnings)

    vrf = module.params["vrf"]
    wanttype = module.params["type"]
    detail = module.params["detail"]
    wantafis = ["ipv4", "ipv6"] if wanttype == "both" else [wanttype]

    allpeers = []
    responses = run_commands(module, [buildSummaryCommand(vrf)])
    parseBgpSummaryJson(responses[0] if responses else "", allpeers)
    peers = [p for p in allpeers if p["iptype"] in wantafis]

    if detail:
        for peer in peers:
            if peer["state"] == "established":
                fillAdvertisedAndLocalAsn(module, vrf, peer)

    bgp_summary = {"vrf": vrf or None, "afi_checked": wantafis, "peers": peers}

    module.exit_json(changed=False, warnings=warnings, bgp_summary=bgp_summary)


if __name__ == "__main__":
    main()
