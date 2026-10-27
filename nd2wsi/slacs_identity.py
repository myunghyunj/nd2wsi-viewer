"""Decide whether a Windows process command line is the SLACS GUI, read-only.

This only parses process metadata that the caller already holds; it never starts,
imports, or signals SLACS and never touches controller hardware. Identity comes
from the Python script operand, not from a window title or the mere presence of a
file. ``nd2wsi/slacs_discovery.py`` on the discovery branch carries the identical
command-line splitting and script-operand rules; keep the two in step.
"""

from __future__ import annotations

import ntpath

SCRIPT_NAME = "SniperCellSorterGUIv3.1.py"


def _split_windows_command_line(command_line: str) -> list[str]:
    """Split Windows C-runtime quoting, without invoking a shell or interpreter.

    Backslashes are special only before quotes. Reject an unclosed quote instead
    of guessing a script identity from incomplete metadata.
    """
    if not isinstance(command_line, str) or "\0" in command_line:
        raise ValueError("Invalid process command line")
    result = []
    index, length = 0, len(command_line)
    while index < length:
        while index < length and command_line[index] in " \t":
            index += 1
        if index == length:
            break
        value, quoted = [], False
        while index < length and (quoted or command_line[index] not in " \t"):
            slashes = 0
            while index < length and command_line[index] == "\\":
                slashes += 1
                index += 1
            if index < length and command_line[index] == '"':
                value.append("\\" * (slashes // 2))
                if slashes % 2:
                    value.append('"')
                    index += 1
                elif quoted and index + 1 < length and command_line[index + 1] == '"':
                    value.append('"')
                    index += 2
                else:
                    quoted = not quoted
                    index += 1
            else:
                value.append("\\" * slashes)
                if index < length and (quoted or command_line[index] not in " \t"):
                    value.append(command_line[index])
                    index += 1
        if quoted:
            raise ValueError("Incomplete process command line")
        result.append("".join(value))
    return result


def _script_argument(command_line: str) -> str | None:
    """Return only Python's script operand, never a later application argument."""
    arguments = _split_windows_command_line(command_line)
    if not arguments or ntpath.basename(arguments[0]).lower() not in (
            "python", "pythonw", "python.exe", "pythonw.exe"):
        return None
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            return arguments[index] if index < len(arguments) and arguments[index] != "-" else None
        if not argument or argument == "-":
            return None
        if not argument.startswith("-"):
            return argument
        if argument == "--check-hash-based-pycs":
            if index >= len(arguments):
                return None
            index += 1
            continue
        if argument.startswith("--check-hash-based-pycs="):
            continue
        if argument.startswith("--"):
            return None
        # Python 2.7 and Python 3 interpreter flags. Options taking a value
        # consume that value even if it happens to be the target script's name.
        options = argument[1:]
        position = 0
        while position < len(options):
            option = options[position]
            if option in "cmhV?":
                return None
            if option in "WXQ":
                if position + 1 == len(options):
                    if index >= len(arguments):
                        return None
                    index += 1
                break
            if option not in "bBdEiIOPqRsStuvx3":
                return None
            position += 1
    return None


def command_line_is_slacs(command_line: str) -> bool:
    """True when the command line runs the SLACS GUI script, never raising."""
    try:
        script = _script_argument(command_line)
    except ValueError:
        return False
    return script is not None and ntpath.basename(script).lower() == SCRIPT_NAME.lower()
