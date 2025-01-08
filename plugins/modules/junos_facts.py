#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
import traceback

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six import iteritems
from ansible.utils.display import Display
from ansible_collections.sense.junos.plugins.module_utils.network.junos import (
    check_args, junos_argument_spec, run_commands)
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
        # TODO Parsing (see fixtures)
        self.facts["default"] = self.responses[0]


@classwrapper
class Config(FactsBase):
    """Default Class to get basic info"""

    COMMANDS = [
        "show running-config | json",
    ]

    def populate(self):
        super(Config, self).populate()
        self.facts["config"] = self.responses[0]


@classwrapper
class Interfaces(FactsBase):
    """All Interfaces Class"""

    COMMANDS = [
        "show interfaces brief | display json",
        "show vlans | display json",
        "show interfaces terse | display json",
        "show lldp neighbors | display json",
    ]

    def populate(self):
        super(Interfaces, self).populate()
        self.facts["interfaces"] = self.responses[0]
        self.facts["vlans"] = self.responses[1]
        self.facts["ipaddresses"] = self.responses[2]
        self.facts["lldp"] = self.responses[3]


@classwrapper
class Routing(FactsBase):
    """Routing Information Class"""

    COMMANDS = ["show route all | display json"]

    def populate(self):
        super(Routing, self).populate()
        self.facts["routes"] = self.responses[0]


FACT_SUBSETS = {
    "default": Default,
    "interfaces": Interfaces,
    "routing": Routing,
    "config": Config,
}

VALID_SUBSETS = frozenset(FACT_SUBSETS.keys())


@functionwrapper
def main():
    """main entry point for module execution"""
    argument_spec = {"gather_subset": {"default": ["!config"], "type": "list"}}
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
