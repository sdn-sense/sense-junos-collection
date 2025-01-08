#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cisco NX9 Command execute
Copyright: Contributors to the SENSE Project
GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

Title                   : sdn-sense/sense-junos-collection
Author                  : Justas Balcas
Email                   : juztas (at) gmail.com
@Copyright              : General Public License v3.0+
Date                    : 2024/07/15
"""
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import time

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six import string_types
from ansible.utils.display import Display
from ansible_collections.ansible.netcommon.plugins.module_utils.network.common.parsing import \
    Conditional
from ansible_collections.ansible.netcommon.plugins.module_utils.network.common.utils import \
    ComplexList
from ansible_collections.sense.junos.plugins.module_utils.network.junos import (
    check_args, junos_argument_spec, run_commands)
from ansible_collections.sense.junos.plugins.module_utils.runwrapper import \
    functionwrapper

display = Display()


@functionwrapper
def toLines(stdout):
    """stdout to list lines, split by \n character"""
    for item in stdout:
        if isinstance(item, string_types):
            item = str(item).split("\n")
        yield item


@functionwrapper
def parse_commands(module, _warnings):
    """Parse commands"""
    command = ComplexList(
        {"command": {"key": True}, "prompt": {}, "answer": {}}, module
    )
    if module.params.get("src", ""):
        # Load src file
        with open(module.params["src"], encoding="utf-8") as fd:
            cmds = fd.readlines()
            # if cmd starts with comment, ignore:
            cmds = [cmd for cmd in cmds if not cmd.startswith("#")]
            commands = command(cmds)
    elif module.params["commands"]:
        commands = command(module.params["commands"])

    for _index, item in enumerate(commands):
        if item["command"].startswith("conf"):
            module.fail_json(
                msg="junos_command does not support running config mode commands.  Please use junos_config instead"
            )
    return commands


@functionwrapper
def main():
    """main entry point for module execution"""
    argument_spec = {
        "lines": {"aliases": ["commands"], "type": "list"},
        "src": {"type": "path"},
        "wait_for": {"type": "list", "elements": "str"},
        "match": {"default": "all", "choices": ["all", "any"]},
        "retries": {"default": 10, "type": "int"},
        "interval": {"default": 1, "type": "int"},
    }

    argument_spec.update(junos_argument_spec)

    mutually_exclusive = [("lines", "src")]

    module = AnsibleModule(
        argument_spec=argument_spec,
        mutually_exclusive=mutually_exclusive,
        supports_check_mode=True,
    )

    result = {"changed": False}

    warnings = []
    check_args(module, warnings)
    commands = parse_commands(module, warnings)
    result["warnings"] = warnings

    wait_for = module.params["wait_for"] or []
    conditionals = [Conditional(c) for c in wait_for]

    retries = module.params["retries"]
    interval = module.params["interval"]
    match = module.params["match"]
    responses = None
    while retries > 0:
        responses = run_commands(module, commands)

        for item in conditionals:
            if item(responses):
                if match == "any":
                    conditionals = []
                    break
                conditionals.remove(item)

        if not conditionals:
            break

        time.sleep(interval)
        retries -= 1

    if conditionals:
        failed_conditions = [item.raw for item in conditionals]
        msg = "One or more conditional statements have not been satisfied"
        module.fail_json(msg=msg, failed_conditions=failed_conditions)

    result.update(
        {
            "changed": False,
            "stdout": responses,
            "stdout_lines": list(toLines(responses)),
        }
    )

    module.exit_json(**result)


if __name__ == "__main__":
    main()
