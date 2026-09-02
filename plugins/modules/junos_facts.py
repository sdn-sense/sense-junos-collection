#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import tempfile
import re
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
import traceback
import xml.etree.ElementTree as ET

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six import iteritems
from ansible.utils.display import Display
from ansible_collections.sense.junos.plugins.module_utils.network.junos import (
    IgnoreInterface, check_args, junos_argument_spec, preview_output, run_commands)
from ansible_collections.sense.junos.plugins.module_utils.runwrapper import (
    classwrapper, functionwrapper)

display = Display()

@functionwrapper
def dumpFactsToTmp(ansible_facts):
    """
    Dump ansible_facts to a temp JSON file
    """
    def default_serializer(obj):
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, bytes):
            return obj.decode('utf-8', errors='replace')
        return str(obj)

    # No dir= : honour $TMPDIR so a full /tmp can be worked around by ops;
    # SiteRM removes these files after reading them.
    fd, path = tempfile.mkstemp(prefix="ansible_facts_", suffix=".json")
    os.close(fd)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(ansible_facts, f, indent=2, ensure_ascii=False, default=default_serializer)
    return path


def strip_ns(tag):
    """Remove namespace from XML tag"""
    return re.sub(r"\{.*\}", "", tag)


@classwrapper
class FactsBase:
    """Base class for Facts"""

    COMMANDS = []

    def __init__(self, module):
        self.module = module
        self.facts = {}
        self.responses = None

    def populate(self):
        """Populate responses"""
        self.responses = run_commands(self.module, self.COMMANDS, check_rc=False)

    def run(self, cmd):
        """Run commands"""
        return run_commands(self.module, cmd, check_rc=False)


@classwrapper
class Default(FactsBase):
    """Default Class to get basic info"""

    # COMMANDS is rebuilt in populate() from vlanmode. `show ethernet-switching
    # table detail` is a syntax error on PTX (pure router, no ethernet-switching
    # table) and errors on every poll, so it is dropped for vlanmode=ptx and the
    # MAC table is simply left empty for that mode.
    COMMANDS = [
        "show version | display json",
        "show ethernet-switching table detail | display json",
    ]
    # Takes ~12 seconds # TODO

    def populate(self):
        vlanmode = (self.module.params.get("vlanmode") or "standard").lower()
        want_mactable = vlanmode != "ptx"
        self.COMMANDS = ["show version | display json"]
        if want_mactable:
            self.COMMANDS.append("show ethernet-switching table detail | display json")
        super(Default, self).populate()
        self.facts["default"] = self.responses[0]
        if not isinstance(self.responses[0], dict):
            display.warning(
                "junos_facts: 'show version | display json' did not return parseable JSON "
                f"(got {type(self.responses[0]).__name__}). Raw device response: "
                f"{preview_output(self.responses[0])}"
            )
        self.facts["mactable"] = {}
        if not want_mactable:
            return
        try:
            self.facts["mactable"] = self.parse_mac_table(self.responses[1])
        except Exception as ex:
            display.warning(
                f"junos_facts: failed to parse_mac_table output: {ex}. Raw device response "
                f"({type(self.responses[1]).__name__}): {preview_output(self.responses[1])}"
            )
            self.facts["mactable"] = {}

    def parse_mac_table(self, cmdoutput):
        """Parse Mac Table"""
        out = {}
        for macdata in cmdoutput.get("l2ng-l2ald-rtb-macdb", [{"": ""}])[0].get(
            "l2ng-l2ald-mac-entry-vlan", []
        ):
            mac = macdata.get("l2ng-l2-mac-address", [{"": ""}])[0].get("data", "")
            vlanid = macdata.get("l2ng-l2-vlan-id", [{"": ""}])[0].get("data", "")
            if mac and vlanid:
                out.setdefault(vlanid, [])
                if mac not in out[vlanid]:
                    out[vlanid].append(mac)
        return out


@classwrapper
class Interfaces(FactsBase):
    """All Interfaces Class"""

    # Note: COMMANDS is rebuilt in populate() based on vlanmode/routing_instance
    # module params, so the VLAN-listing command points at either the global
    # `show vlans` (standard) or a routing-instance scope (MX bridge-domains /
    # PTX vlans). The slot order is fixed so downstream parsing indices stay valid.
    COMMANDS = [
        "show interfaces | display json",
        "show vlans detail | display json",
        "show lldp neighbors | display json",
        "show interfaces ae* | display json",
    ]

    VLAN_COMMANDS = {
        "standard": "show vlans detail | display json",
        # "bridge domain" (two words) is the real command on MX -- the
        # hyphenated "bridge-domain" is a syntax error on Junos 22.2R1-S2.4
        # (confirmed live against an MX304). Verify against your own MX
        # release if it differs.
        "mx": "show bridge domain instance {ri} detail | display json",
        # NOT verified against real PTX hardware -- confirm this command
        # exists and returns this shape before relying on ptx mode.
        "ptx": "show vlans instance {ri} detail | display json",
    }

    def populate(self):
        vlanmode = (self.module.params.get("vlanmode") or "standard").lower()
        ri_name = self.module.params.get("routing_instance") or "SENSE-Vlans"
        if vlanmode not in self.VLAN_COMMANDS:
            vlanmode = "standard"
        # Substitute routing-instance into the VLAN-listing slot (index 1).
        vlan_cmd = self.VLAN_COMMANDS[vlanmode].format(ri=ri_name)
        self.COMMANDS = [
            "show interfaces | display json",
            vlan_cmd,
            "show lldp neighbors | display json",
            "show interfaces ae* | display json",
        ]
        super(Interfaces, self).populate()
        self.facts.setdefault("info", {"macs": []})
        self.facts.setdefault("interfaces", {})
        self.facts.setdefault("lldp", {})
        if vlanmode == "mx":
            vlan_parser = self.parse_bridge_domains_mx
        elif vlanmode == "ptx":
            vlan_parser = self.parse_routing_instance_vlans
        else:
            vlan_parser = self.parse_vlans
        # Each parser runs independently: a malformed/non-JSON response for
        # one command (CLI error, empty table, device quirk) must not blank
        # out fact subsets that parsed successfully, nor crash the whole
        # facts-gathering run.
        parse_errors = {}
        for name, cmd, parsefunc, response in (
            ("parse_interfaces", self.COMMANDS[0], self.parse_interfaces, self.responses[0]),
            ("parse_vlans", self.COMMANDS[1], vlan_parser, self.responses[1]),
            ("parse_lldp", self.COMMANDS[2], self.parse_lldp, self.responses[2]),
            ("parse_port_channels", self.COMMANDS[3], self.parse_port_channels, self.responses[3]),
        ):
            try:
                parsefunc(response)
            except Exception as ex:
                parse_errors[name] = str(ex)
                display.warning(
                    f"junos_facts: failed to {name} ({cmd!r}): {ex}. Raw device response "
                    f"({type(response).__name__}): {preview_output(response)}"
                )

        # `show interfaces | display json` is the critical payload
        if "parse_interfaces" in parse_errors:
            raise RuntimeError(
                "'show interfaces | display json' did not return parseable JSON "
                f"({parse_errors['parse_interfaces']}); refusing to publish a "
                "degraded interface fact set. Raw device response "
                f"({type(self.responses[0]).__name__}): {preview_output(self.responses[0])}"
            )

    def parse_interfaces(self, cmdoutput):
        """Parse Junos Output Interfaces"""
        if not isinstance(cmdoutput, dict):
            raise ValueError(
                f"device did not return parseable JSON (got {type(cmdoutput).__name__})"
            )
        for physdata in cmdoutput.get("interface-information", [{"": ""}])[0].get(
            "physical-interface", []
        ):
            intf = physdata.get("name", [{"": ""}])[0].get("data", "")
            if intf:
                try:
                    newEntry = self.facts["interfaces"].setdefault(intf, {})
                    self._getOperStatus(newEntry, physdata)
                    self._getlineprotocol(newEntry, physdata)
                    self._getMTU(newEntry, physdata)
                    self._getSpeed(newEntry, physdata)
                    self._getMacAddress(newEntry, physdata)
                    self._getSwitchport(newEntry, physdata)
                except IgnoreInterface:
                    del self.facts["interfaces"][intf]

    def _addMac(self, macaddr):
        """Add Mac Address"""
        if macaddr not in self.facts["info"]["macs"]:
            self.facts["info"]["macs"].append(macaddr)

    def _getSwitchport(self, newEntry, physdata):
        """Get Switchport"""
        # logical-interface
        switchPort = False
        for item in physdata.get("logical-interface", [{"": ""}]):
            if "address-family" in item:
                for addritem in item["address-family"]:
                    if "address-family-name" in addritem:
                        for addritemname in addritem["address-family-name"]:
                            if addritemname.get("data") == "ethernet-switching":
                                switchPort = True
        # On MX/PTX, a port destined for a virtual-switch VLAN service is never
        # configured with an "ethernet-switching" family (that's EX/QFX-only) --
        # it's added via `routing-instances ... bridge-domains|vlans` instead.
        # "Link-level type: Flexible-Ethernet" (confirmed against a real MX/PTX
        # device) is what actually indicates it's safe to attach logical L2
        # units to the port, so treat it the same as an existing switchport.
        link_level_type = physdata.get("link-level-type", [{"": ""}])[0].get("data", "")
        if link_level_type == "Flexible-Ethernet":
            switchPort = True
        port = physdata.get("name", [{"": ""}])[0].get("data", "")
        newEntry["switchport"] = switchPort

    def _getOperStatus(self, newEntry, physdata):
        """Get Operational Status"""
        operstatus = physdata.get("oper-status", [{"": ""}])[0].get("data", "unknown")
        newEntry["operstatus"] = operstatus

    def _getlineprotocol(self, newEntry, physdata):
        """Get Line Protocol"""
        adminstatus = physdata.get("admin-status", [{"": ""}])[0].get("data", "unknown")
        newEntry["lineprotocol"] = adminstatus

    def _getMTU(self, newEntry, physdata):
        """Get MTU"""
        mtu = physdata.get("mtu", [{"": ""}])[0].get("data", 1500)
        if mtu == "Unlimited":
            raise IgnoreInterface("Unlimited MTU")
        newEntry["mtu"] = mtu

    def _getSpeed(self, newEntry, physdata):
        """Get Speed"""
        speed = physdata.get("speed", [{"": ""}])[0].get("data", "")
        if not speed:
            newEntry["speed"] = 0
            return
        if speed == "Unlimited":
            raise IgnoreInterface("Unlimited speed")
        if speed == "Unspecified":
            newEntry["speed"] = 10000  # Default to 10Gbps
            return
        # Unit casing is not consistent across platforms/interfaces (e.g. MX
        # reports some ports as "800mbps" lowercase instead of "800Mbps"),
        # so match case-insensitively instead of exact substrings.
        match = re.match(r"^([\d.]+)\s*(gbps|mbps|kbps)$", speed, re.IGNORECASE)
        if not match:
            newEntry["speed"] = 0
            return
        value, unit = float(match.group(1)), match.group(2).lower()
        if unit == "gbps":
            speed = int(value * 1000)
        elif unit == "mbps":
            speed = int(value)
        else:
            speed = value / 1000
        newEntry["speed"] = speed

    def _getMacAddress(self, newEntry, physdata):
        """Get Mac Address"""
        # current-physical-address and hardware-physical-address
        for key in ["current-physical-address", "hardware-physical-address"]:
            mac = physdata.get(key, [{"": ""}])[0].get("data", "")
            if mac:
                self._addMac(mac)
                newEntry["mac"] = mac

    def _getLagMembers(self, newEntry, physdata):
        """Get LAG Members"""
        for lagmember in physdata.get("ifd-lag-traffic-statistics", [{"": ""}])[0].get(
            "ifd-lag-members-list", []
        ):
            intf = lagmember.get("name", [{"": ""}])[0].get("data", "")
            if intf:
                newEntry.setdefault("channel-member", [])
                newEntry["channel-member"].append(intf)

    def parse_port_channels(self, cmdoutput):
        """Parse Port Channels"""
        # show interfaces ae* | display json
        if not isinstance(cmdoutput, dict):
            raise ValueError(
                f"device did not return parseable JSON (got {type(cmdoutput).__name__})"
            )
        for physdata in cmdoutput.get("interface-information", [{"": ""}])[0].get(
            "physical-interface", []
        ):
            intf = physdata.get("name", [{"": ""}])[0].get("data", "")
            if intf.startswith("ae"):
                try:
                    newEntry = self.facts["interfaces"].setdefault(intf, {})
                    self._getOperStatus(newEntry, physdata)
                    self._getlineprotocol(newEntry, physdata)
                    self._getMTU(newEntry, physdata)
                    self._getSpeed(newEntry, physdata)
                    self._getMacAddress(newEntry, physdata)
                    self._getLagMembers(newEntry, physdata)
                except IgnoreInterface:
                    del self.facts["interfaces"][intf]

    def parse_taggness(self, inputval):
        """Parse if it is tagged or not"""
        taginft = inputval.get("l2ng-l2rtb-vlan-member-interface", [{"": ""}])[0].get(
            "data", ""
        )
        taginft = taginft.replace("*", "").split(".")[0]
        tagtype = inputval.get("l2ng-l2rtb-vlan-member-tagness", [{"": ""}])[0].get(
            "data", ""
        )
        return tagtype, taginft

    def parse_vlans(self, cmdoutput):
        """Parse Vlans"""
        for vlan in cmdoutput.get("l2ng-l2ald-vlan-instance-information", [{"": ""}])[
            0
        ].get("l2ng-l2ald-vlan-instance-group", []):
            vlanid = vlan.get("l2ng-l2rtb-vlan-tag", [{"": ""}])[0].get("data", "")
            if vlanid:
                newEntry = self.facts["interfaces"].setdefault(f"Vlan{vlanid}", {})
                newEntry["mtu"] = (
                    1500  # Need a way to loop all interfaces self.facts["interfaces"][intf].get("mtu", 1500)
                )
                # Get tagged vlan members l2ng-l2rtb-vlan-member
                for vlanmember in vlan.get("l2ng-l2rtb-vlan-member", []):
                    tagtype, taginft = self.parse_taggness(vlanmember)
                    newEntry.setdefault(tagtype, [])
                    if taginft not in newEntry[tagtype]:
                        newEntry[tagtype].append(taginft)

    def parse_routing_instance_vlans(self, cmdoutput):
        """Parse VLANs scoped to a routing-instance virtual-switch (PTX).

        `show vlans instance <ri> detail | display json` mirrors the global
        `show vlans detail` JSON shape, just filtered to the instance. The
        configured VLAN name (e.g. VLAN-1323) is exposed as
        l2ng-l2rtb-vlan-name; we fall back to f"VLAN-{vlanid}" if absent.
        """
        for vlan in cmdoutput.get("l2ng-l2ald-vlan-instance-information", [{"": ""}])[
            0
        ].get("l2ng-l2ald-vlan-instance-group", []):
            vlanid = vlan.get("l2ng-l2rtb-vlan-tag", [{"": ""}])[0].get("data", "")
            if not vlanid:
                continue
            vlan_name = vlan.get("l2ng-l2rtb-vlan-name", [{"": ""}])[0].get(
                "data", ""
            ) or f"VLAN-{vlanid}"
            newEntry = self.facts["interfaces"].setdefault(vlan_name, {})
            newEntry["mtu"] = 1500
            for vlanmember in vlan.get("l2ng-l2rtb-vlan-member", []):
                tagtype, taginft = self.parse_taggness(vlanmember)
                newEntry.setdefault(tagtype, [])
                if taginft not in newEntry[tagtype]:
                    newEntry[tagtype].append(taginft)

    def parse_bridge_domains_mx(self, cmdoutput):
        """Parse Bridge Domains for MX routing-instance virtual-switch.

        Output of `show bridge domain instance <ri> detail | display json`.
        Confirmed live against an MX304 (Junos 22.2R1-S2.4, network-services
        enhanced-ip) by committing a throwaway bridge-domain
        (domain-type bridge, an explicit vlan-id) and capturing the real
        response, then reverting:
          {
            "l2ald-bridge-instance-information": [{
              "l2ald-bridge-instance-group": [{
                "l2rtb-name":            [{"data": "default-switch"}],
                "l2rtb-bridging-domain": [{"data": "VLAN-1323"}],
                "l2rtb-bridge-vlan":     [{"data": "1323"}],
                "l2rtb-instance-state":  [{"data": "Active"}],
                ... (mac-limit/mac-learned/sequence-number counters)
              }, ...]
            }]
          }
        `show bridge domain instance <fake-ri> detail` returns this same
        empty-but-valid skeleton for a nonexistent instance name (no syntax
        error), confirming the "instance <ri>" filter itself is accepted --
        so this command form should work whether or not <ri> is a real
        virtual-switch, but the instance-scoped case wasn't observed with an
        actual virtual-switch routing-instance (device had none configured).

        NOT available here: member interfaces. Even with a real interface
        attached to the test bridge-domain, this command's output never
        included an interface list, in either "detail" or "extensive" style
        -- Junos exposes interface<->bridge-domain binding via a *separate*
        command, `show bridge domain interface <name> extensive | display
        json` (top-level key "l2ald-bd-ifbd-information" /
        "l2ald-bd-ifbd-entry"), which requires the interface to already be
        known and returned no populated fields for the down/unlinked test
        port used here. Getting real tagged/untagged membership for MX would
        mean issuing that command per candidate interface (or finding
        another way to enumerate members) -- not implemented yet, so "mtu"
        is set but "tagged"/"untagged" are left absent for vlanmode=mx until
        this is designed and verified against a device with a live member
        interface.
        """
        for bd in cmdoutput.get("l2ald-bridge-instance-information", [{"": ""}])[
            0
        ].get("l2ald-bridge-instance-group", []):
            bd_name = bd.get("l2rtb-bridging-domain", [{"": ""}])[0].get("data", "")
            vlanid = bd.get("l2rtb-bridge-vlan", [{"": ""}])[0].get("data", "")
            if not bd_name and vlanid:
                bd_name = f"VLAN-{vlanid}"
            if not bd_name:
                continue
            newEntry = self.facts["interfaces"].setdefault(bd_name, {})
            newEntry["mtu"] = 1500

    def parse_lldp(self, cmdoutput):
        """Parse LLDP"""
        for lldpdata in cmdoutput.get("lldp-neighbors-information", [{"": ""}])[0].get(
            "lldp-neighbor-information", []
        ):
            intf = lldpdata.get("lldp-local-port-id", [{"": ""}])[0].get("data", "")
            if intf:
                entryOut = {"local_port_id": intf}
                for key, mapping in {
                    "lldp-remote-system-name": "remote_system_name",
                    "lldp-remote-chassis-id": "remote_chassis_id",
                    "lldp-remote-port-id": "remote_port_id",
                }.items():
                    tmpVal = lldpdata.get(key, [{"": ""}])[0].get("data", "")
                    if tmpVal:
                        entryOut[mapping] = tmpVal
                self.facts["lldp"][intf] = entryOut


@classwrapper
class Routing(FactsBase):
    """Routing Information Class"""

    COMMANDS = ["show route all | display xml"]

    def populate(self):
        super(Routing, self).populate()
        self.facts["ipv6"] = []
        self.facts["ipv4"] = []
        try:
            self.getRouting(self.responses[0])
        except Exception as ex:
            display.warning(
                f"junos_facts: failed to parse routing output ({self.COMMANDS[0]!r}): {ex}. "
                f"Raw device response ({type(self.responses[0]).__name__}): "
                f"{preview_output(self.responses[0])}"
            )

    def getRouting(self, cmdoutput):
        """Parse Routing Information from XML ignoring namespaces"""
        root = ET.fromstring(cmdoutput)
        for route_table in root.iter():
            if strip_ns(route_table.tag) != "route-table":
                continue
            for rt in route_table:
                if strip_ns(rt.tag) != "rt":
                    continue
                rval_base = {}
                # Get destination
                for child in rt:
                    if strip_ns(child.tag) == "rt-destination":
                        rval_base["from"] = child.text or ""
                        break
                if not rval_base.get("from"):
                    continue
                for rt_entry in rt:
                    if strip_ns(rt_entry.tag) != "rt-entry":
                        continue
                    nh = None
                    for entry_child in rt_entry:
                        if strip_ns(entry_child.tag) == "nh":
                            nh = entry_child
                            break
                    rval = rval_base.copy()
                    if nh is not None:
                        for nh_child in nh:
                            tag = strip_ns(nh_child.tag)
                            if tag == "to":
                                rval["to"] = nh_child.text or ""
                            elif tag == "via":
                                rval["via"] = nh_child.text or ""
                    else:
                        rval["to"] = ""
                        rval["via"] = ""
                    if rval.get("to") or rval.get("via"):
                        if ":" in rval["from"]:
                            self.facts["ipv6"].append(rval)
                        else:
                            self.facts["ipv4"].append(rval)


FACT_SUBSETS = {
    "default": Default,
    "interfaces": Interfaces,
    "routing": Routing,
}

VALID_SUBSETS = frozenset(FACT_SUBSETS.keys())


@functionwrapper
def main():
    """main entry point for module execution"""
    argument_spec = {
        "gather_subset": {"default": ["!default"], "type": "list"},
        # vlanmode selects how VLANs are discovered on the device:
        #   standard -> `show vlans detail` (default; EX/QFX/SRX/ELS, standard MX)
        #   mx       -> `show bridge-domain instance <ri> detail` (MX virtual-switch)
        #   ptx      -> `show vlans instance <ri> detail`         (PTX virtual-switch)
        # routing_instance is only consulted when vlanmode is mx or ptx.
        # vlanmode=ptx also drops `show ethernet-switching table detail` from the
        # default subset (invalid command on PTX); MAC table is empty in that mode.
        "vlanmode": {"type": "str", "default": "standard", "choices": ["standard", "mx", "ptx"]},
        "routing_instance": {"type": "str", "default": "SENSE-Vlans"},
    }
    argument_spec.update(junos_argument_spec)
    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)
    gather_subset = module.params["gather_subset"]
    runable_subsets = set()
    exclude_subsets = set()

    for subset in gather_subset:
        if subset == "all":
            runable_subsets.update(VALID_SUBSETS)
            continue
        if subset.startswith("!"):
            subset = subset[1:]
            if subset == "all":
                exclude_subsets.update(VALID_SUBSETS)
                continue
            exclude = True
        else:
            exclude = False
        if subset not in VALID_SUBSETS:
            module.fail_json(msg="Bad subset")
        if exclude:
            exclude_subsets.add(subset)
        else:
            runable_subsets.add(subset)
    if not runable_subsets:
        runable_subsets.update(VALID_SUBSETS)

    runable_subsets.difference_update(exclude_subsets)
    runable_subsets.add("default")

    facts = {"gather_subset": [runable_subsets]}

    instances = []
    for key in runable_subsets:
        instances.append(FACT_SUBSETS[key](module))

    for inst in instances:
        if inst:
            try:
                inst.populate()
                facts.update(inst.facts)
            except Exception as ex:
                display.vvv(traceback.format_exc())
                raise Exception(traceback.format_exc()) from ex

    ansible_facts = {}
    for key, value in iteritems(facts):
        key = f"ansible_net_{key}"
        ansible_facts[key] = value

    warnings = []
    check_args(module, warnings)
    if len(str(ansible_facts)) > 100000:
        facts_path = dumpFactsToTmp(ansible_facts)
        display.vvv(facts_path)
        module.exit_json(ansible_facts_file={"file": facts_path}, warnings=warnings)
    else:
        module.exit_json(ansible_facts=ansible_facts, warnings=warnings)


if __name__ == "__main__":
    main()
