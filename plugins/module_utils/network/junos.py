#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
import json

from ansible.module_utils._text import to_text
from ansible.module_utils.basic import env_fallback
from ansible.module_utils.connection import exec_command
from ansible_collections.ansible.netcommon.plugins.module_utils.network.common.config import (
    ConfigLine, NetworkConfig)
from ansible_collections.ansible.netcommon.plugins.module_utils.network.common.utils import (
    ComplexList, to_list)
from ansible_collections.sense.junos.plugins.module_utils.runwrapper import \
    functionwrapper

_DEVICE_CONFIGS = {}

WARNING_PROMPTS_RE = [
    r"[\r\n]?\[yes/no\]:\s?$",
    r"[\r\n]?\[confirm yes/no\]:\s?$",
    r"[\r\n]?\[y/n\]:\s?$",
]

junos_provider_spec = {
    "host": {},
    "port": {"type": int},
    "username": {"fallback": (env_fallback, ["ANSIBLE_NET_USERNAME"])},
    "password": {"fallback": (env_fallback, ["ANSIBLE_NET_PASSWORD"]), "no_log": True},
    "ssh_keyfile": {
        "fallback": (env_fallback, ["ANSIBLE_NET_SSH_KEYFILE"]),
        "type": "path",
    },
    "authorize": {
        "fallback": (env_fallback, ["ANSIBLE_NET_AUTHORIZE"]),
        "type": "bool",
    },
    "auth_pass": {
        "fallback": (env_fallback, ["ANSIBLE_NET_AUTH_PASS"]),
        "no_log": True,
    },
    "timeout": {"type": "int"},
}
junos_argument_spec = {"provider": {"type": "dict", "options": junos_provider_spec}}


@functionwrapper
def to_json(out):
    """Check and change output to dict if possible"""
    try:
        return json.loads(out)
    except ValueError:
        return out


@functionwrapper
def check_args(module, warnings):
    """Check args pass"""
    pass


@functionwrapper
def get_config(module, flags=None):
    """Get running config"""
    flags = [] if flags is None else flags

    cmd = "show running-config " + " ".join(flags)
    cmd = cmd.strip()

    try:
        return _DEVICE_CONFIGS[cmd]
    except KeyError:
        ret, out, err = exec_command(module, cmd)
        if ret != 0:
            module.fail_json(
                msg="unable to retrieve current config",
                stderr=to_text(err, errors="surrogate_or_strict"),
            )
        cfg = to_text(out, errors="surrogate_or_strict").strip()
        _DEVICE_CONFIGS[cmd] = cfg
        return cfg


@functionwrapper
def to_commands(module, commands):
    """Transform commands"""
    spec = {"command": {"key": True}, "prompt": {}, "answer": {}}
    transform = ComplexList(spec, module)
    return transform(commands)


@functionwrapper
def run_commands(module, commands, check_rc=True):
    """Run Commands"""
    responses = []
    commands = to_commands(module, to_list(commands))
    for cmd in commands:
        cmd = module.jsonify(cmd)
        ret, out, err = exec_command(module, cmd)
        if check_rc and ret != 0:
            module.fail_json(msg=to_text(err, errors="surrogate_or_strict"), rc=ret)
        responses.append(to_json(to_text(out, errors="surrogate_or_strict")))
    return responses


@functionwrapper
def load_config(module, commands):
    """Load config"""
    ret, _out, err = exec_command(module, "configure terminal")
    if ret != 0:
        module.fail_json(
            msg="unable to enter configuration mode",
            err=to_text(err, errors="surrogate_or_strict"),
        )

    for command in to_list(commands):
        if command == "end":
            continue
        ret, _out, err = exec_command(module, command)
        if ret != 0:
            module.fail_json(
                msg=to_text(err, errors="surrogate_or_strict"), command=command, rc=ret
            )

    exec_command(module, "end")


@functionwrapper
def get_sublevel_config(running_config, module):
    """Get sublevel config"""
    contents = []
    current_config_contents = []
    running_config = NetworkConfig(contents=running_config, indent=1)
    obj = running_config.get_object(module.params["parents"])
    if obj:
        contents = obj.children
    contents[:0] = module.params["parents"]

    indent = 0
    for cts in contents:
        if isinstance(cts, str):
            current_config_contents.append(cts.rjust(len(cts) + indent, " "))
        if isinstance(cts, ConfigLine):
            current_config_contents.append(cts.raw)
        indent = 1
    sublevel_config = "\n".join(current_config_contents)

    return sublevel_config
