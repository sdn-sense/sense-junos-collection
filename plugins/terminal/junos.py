#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
import re

from ansible.errors import AnsibleConnectionFailure
from ansible.plugins.terminal import TerminalBase
from ansible_collections.sense.junos.plugins.module_utils.runwrapper import \
    classwrapper


@classwrapper
class TerminalModule(TerminalBase):
    """Base terminal plugin for Junos devices"""

    terminal_stdout_re = [
        re.compile(rb"[\r\n]?[\w+\-\.:\/\[\]]+(?:\([^\)]+\)){,3}(?:>|#) ?$"),
        re.compile(rb"\[\w+\@[\w\-\.]+(?: [^\]])\] ?[>#\$] ?$"),
    ]

    terminal_stderr_re = [
        re.compile(
            rb"% ?Error: (?:(?!\bdoes not exist\b)(?!\balready exists\b)(?!\bHost not found\b)(?!\bnot active\b).)*\n"
        ),
        re.compile(rb"% ?Bad secret"),
        re.compile(rb"invalid input", re.I),
        re.compile(rb"(?:incomplete|ambiguous) command", re.I),
        re.compile(rb"connection timed out", re.I),
        re.compile(rb"'[^']' +returned error code: ?\d+"),
    ]

    terminal_initial_prompt = rb"\[y/n\]:"

    terminal_initial_answer = b"y"

    def on_open_shell(self):
        """Execute cmd on open shell"""
        try:
            self._exec_cli_command(b"set cli screen-length 0")
        except AnsibleConnectionFailure:
            raise AnsibleConnectionFailure(
                "unable to set terminal parameters"
            ) from AnsibleConnectionFailure

    def on_become(self, _passwd=None):
        """Execute cmd on become (Junos has no enable mode)"""
        if self._get_prompt().endswith(b">"):
            return

    def on_unbecome(self):
        """Execute cmd on unbecome (Junos has no disable mode)"""
        prompt = self._get_prompt()
        if prompt is None:
            # if prompt is None most likely the terminal is hung up at a prompt
            return
