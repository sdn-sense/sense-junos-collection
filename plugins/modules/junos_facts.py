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
    IgnoreInterface, check_args, junos_argument_spec, run_commands)
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

    fd, path = tempfile.mkstemp(prefix="ansible_facts_", suffix=".json", dir="/tmp")
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

    COMMANDS = [
        "show version | display json",
        "show ethernet-switching table detail | display json",
    ]
    # Takes ~12 seconds # TODO

    def populate(self):
        super(Default, self).populate()
        self.facts["default"] = self.responses[0]
        try:
            self.facts["mactable"] = self.parse_mac_table(self.responses[1])
        except Exception:
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
        "mx": "show bridge-domain instance {ri} detail | display json",
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
        self.parse_interfaces(self.responses[0])
        if vlanmode == "mx":
            self.parse_bridge_domains_mx(self.responses[1])
        elif vlanmode == "ptx":
            self.parse_routing_instance_vlans(self.responses[1])
        else:
            self.parse_vlans(self.responses[1])
        self.parse_lldp(self.responses[2])
        self.parse_port_channels(self.responses[3])

    def parse_interfaces(self, cmdoutput):
        """Parse Junos Output Interfaces"""
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
        if "Gbps" in speed:
            speed = int(speed.split("Gbps")[0]) * 1000
        elif "Mbps" in speed:
            speed = int(speed.split("Mbps")[0])
        elif "Kbps" in speed:
            speed = int(speed.split("Kbps")[0]) / 1000
        elif speed == "Unlimited":
            raise IgnoreInterface("Unlimited speed")
        elif speed == "Unspecified":
            speed = 10000  # Default to 10Gbps
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

        Output of `show bridge-domain instance <ri> detail | display json`:
          {
            "bridge-domain-information": [{
              "bridge-domain": [{
                "bridge-domain-name": [{"data": "VLAN-1323"}],
                "bridge-vlan-id":     [{"data": "1323"}],
                "bridge-interface":   [{"data": "ae14.1323"}, {"data": "ae4.1323"}]
              }, ...]
            }]
          }
        Member interfaces appear as `<port>.<vlan>` sub-units; we strip the
        sub-unit suffix and record the parent port as a tagged member, matching
        the shape used by parse_vlans() for consistent downstream comparison.
        """
        for bd in (
            cmdoutput.get("bridge-domain-information", [{"": ""}])[0]
            .get("bridge-domain", [])
        ):
            bd_name = bd.get("bridge-domain-name", [{"": ""}])[0].get("data", "")
            vlanid = bd.get("bridge-vlan-id", [{"": ""}])[0].get("data", "")
            if not bd_name and vlanid:
                bd_name = f"VLAN-{vlanid}"
            if not bd_name:
                continue
            newEntry = self.facts["interfaces"].setdefault(bd_name, {})
            newEntry["mtu"] = 1500
            # bridge-interface may be a list of {"data": "ae14.1323"} entries,
            # or a single comma-separated string under "data". Handle both.
            raw_members = []
            for entry in bd.get("bridge-interface", []):
                val = entry.get("data", "") if isinstance(entry, dict) else ""
                if not val:
                    continue
                raw_members.extend(part.strip() for part in val.split(",") if part.strip())
            for member in raw_members:
                parent = member.replace("*", "").split(".")[0]
                if not parent:
                    continue
                newEntry.setdefault("tagged", [])
                if parent not in newEntry["tagged"]:
                    newEntry["tagged"].append(parent)

    def parse_lldp(self, cmdoutput):
        """Parse LLDP"""
        for lldpdata in cmdoutput["lldp-neighbors-information"]:
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
                self.facts["lldp"][intf] = lldpdata


@classwrapper
class Routing(FactsBase):
    """Routing Information Class"""

    COMMANDS = ["show route all | display xml"]

    def populate(self):
        super(Routing, self).populate()
        self.facts["ipv6"] = []
        self.facts["ipv4"] = []
        self.getRouting(self.responses[0])

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
