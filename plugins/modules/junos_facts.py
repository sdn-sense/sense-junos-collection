#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
import traceback

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six import iteritems
from ansible.utils.display import Display
from ansible_collections.sense.junos.plugins.module_utils.network.junos import (
    check_args, junos_argument_spec, run_commands, IgnoreInterface)
from ansible_collections.sense.junos.plugins.module_utils.runwrapper import (
    classwrapper, functionwrapper)

display = Display()


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
    ]

    def populate(self):
        super(Default, self).populate()
        self.facts["default"] = self.responses[0]

@classwrapper
class Interfaces(FactsBase):
    """All Interfaces Class"""

    COMMANDS = [
        'show interfaces | display json',
        "show vlans detail | display json",
        "show lldp neighbors | display json",
        "show interfaces ae* | display json"
    ]
    def populate(self):
        super(Interfaces, self).populate()
        self.facts.setdefault("info", {"macs": []})
        self.facts.setdefault("interfaces", {})
        self.parse_interfaces(self.responses[0])
        self.parse_vlans(self.responses[1])
        self.parse_lldp(self.responses[2])
        self.parse_port_channels(self.responses[3])

    def parse_interfaces(self, cmdoutput):
        """Parse Junos Output Interfaces"""
        for physdata in cmdoutput.get("interface-information", [{"": ""}])[0].get("physical-interface", []):
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
        if "switch-options" in physdata:
            newEntry["switchport"] = True
        else:
            newEntry["switchport"] = True # For now we report that all are switchports (TODO - Review in future)

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
        if mtu == 'Unlimited':
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
            speed = 10000 # Default to 10Gbps
        newEntry["speed"] = speed

    def _getMacAddress(self, newEntry, physdata):
        """Get Mac Address"""
        # current-physical-address and hardware-physical-address
        for key in ["current-physical-address", "hardware-physical-address"]:
            mac = physdata.get(key, [{"": ""}])[0].get("data", "")
            if mac:
                self._addMac(mac)
                newEntry["mac"] = mac

    def parse_port_channels(self, cmdoutput):
        """Parse Port Channels"""
        # show interfaces ae* | display json
        for physdata in cmdoutput.get("interface-information", [{"": ""}])[0].get("physical-interface", []):
            intf = physdata.get("name", [{"": ""}])[0].get("data", "")
            if intf.startswith("ae"):
                try:
                    newEntry = self.facts["interfaces"].setdefault(intf, {})
                    self._getOperStatus(newEntry, physdata)
                    self._getlineprotocol(newEntry, physdata)
                    self._getMTU(newEntry, physdata)
                    self._getSpeed(newEntry, physdata)
                    self._getMacAddress(newEntry, physdata)
                except IgnoreInterface:
                    del self.facts["interfaces"][intf]


    def parse_taggness(self, inputval):
        """Parse if it is tagged or not"""
        taginft = inputval.get("l2ng-l2rtb-vlan-member-interface", [{"": ""}])[0].get("data", "")
        taginft = taginft.replace("*", "").split(".")[0]
        tagtype = inputval.get("l2ng-l2rtb-vlan-member-tagness", [{"": ""}])[0].get("data", "")
        return tagtype, taginft

    def parse_vlans(self, cmdoutput):
        """Parse Vlans"""
        for vlan in cmdoutput.get("l2ng-l2ald-vlan-instance-information", [{"": ""}])[0].get("l2ng-l2ald-vlan-instance-group", []):
            vlanid = vlan.get("l2ng-l2rtb-vlan-tag", [{"": ""}])[0].get("data", "")
            if vlanid:
                newEntry = self.facts["interfaces"].setdefault(f"Vlan{vlanid}", {})
                newEntry["mtu"] = 1500 # Need a way to loop all interfaces self.facts["interfaces"][intf].get("mtu", 1500)
                # Get tagged vlan members l2ng-l2rtb-vlan-member
                for vlanmember in vlan.get("l2ng-l2rtb-vlan-member", []):
                    tagtype, taginft = self.parse_taggness(vlanmember)
                    newEntry.setdefault(tagtype, [])
                    if taginft not in newEntry[tagtype]:
                        newEntry[tagtype].append(taginft)

    def parse_lldp(self, cmdoutput):
        """Parse LLDP"""
        for lldpdata in cmdoutput["lldp-neighbors-information"]:
            intf = lldpdata.get("lldp-local-port-id", [{"": ""}])[0].get("data", "")
            if intf:
                entryOut = {'local_port_id': intf}
                for key, mapping in {'lldp-remote-system-name': 'remote_system_name',
                                     'lldp-remote-chassis-id': 'remote_chassis_id',
                                     'lldp-remote-port-id': 'remote_port_id'}.items():
                    tmpVal = lldpdata.get(key, [{"": ""}])[0].get("data", "")
                    if tmpVal:
                        entryOut[mapping] = tmpVal
                self.facts["lldp"][intf] = lldpdata


@classwrapper
class Routing(FactsBase):
    """Routing Information Class"""

    COMMANDS = ["show route all | display json"]

    def populate(self):
        super(Routing, self).populate()
        self.facts["ipv6"] = []
        self.facts["ipv4"] = []
        self.getRouting(self.responses[0])

    def getRouting(self, cmdoutput):
        """Get Routing Information"""
        for route in cmdoutput.get("route-information", [{"": ""}])[0].get("route-table", []):
            for routeEntry in route.get("rt", []):
                rval = {}
                rval["from"] = routeEntry.get("rt-destination", [{"": ""}])[0].get("data", "")
                if not rval["from"]:
                    continue
                rval["to"] = routeEntry.get("rt-entry", [{"": ""}])[0].get("nh", [{"": ""}])[0].get("to", [{"": ""}])[0].get("data", "")
                rval["via"] = routeEntry.get("rt-entry", [{"": ""}])[0].get("nh", [{"": ""}])[0].get("via", [{"": ""}])[0].get("data", "")
                if rval["to"] or rval["via"]:
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
    argument_spec = {"gather_subset": {"default": ["!default"], "type": "list"}}
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
    module.exit_json(ansible_facts=ansible_facts, warnings=warnings)


if __name__ == "__main__":
    main()
