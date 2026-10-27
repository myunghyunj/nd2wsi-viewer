"""SLACS identity comes from the Python command line, never from running code.

The command-line splitter and script-operand rules are ported unchanged from
``nd2wsi/slacs_discovery.py`` on the discovery branch; these cases mirror that
branch's ``tests/test_slacs_discovery.py``.
"""

import subprocess

import pytest

from nd2wsi import slacs_identity as identity

SCRIPT = r"C:\Python27\Scripts\SniperCellSorterGUIv3.1.py"


@pytest.mark.parametrize("command, expected", [
    (f'python.exe "{SCRIPT}"', SCRIPT),
    (f'"C:\\Program Files\\Python\\pythonw.exe" -u -OO "{SCRIPT}" user', SCRIPT),
    (f'python.exe -Buv -W ignore -Qwarnall "{SCRIPT}"', SCRIPT),
    (f'python.exe -X utf8 --check-hash-based-pycs always -- "{SCRIPT}"', SCRIPT),
    (f'pythonw --check-hash-based-pycs=never -Wignore "{SCRIPT}"', SCRIPT),
    (f'python.exe -c "print(1)" "{SCRIPT}"', None),
    (f'python.exe -cprint(1) "{SCRIPT}"', None),
    (f'python.exe -m other "{SCRIPT}"', None),
    (f'python.exe -Imother "{SCRIPT}"', None),
    (f'python.exe other.py "{SCRIPT}"', "other.py"),
    (f'python.exe -W "{SCRIPT}"', None),
    (f'python.exe -X "{SCRIPT}"', None),
    (f'python.exe --help "{SCRIPT}"', None),
    (f'python.exe -V "{SCRIPT}"', None),
    (f'python.exe -z "{SCRIPT}"', None),
    (f'python.exe - "{SCRIPT}"', None),
    (f'cmd.exe /c python.exe "{SCRIPT}"', None),
    ("python.exe", None),
])
def test_only_the_python_script_operand_counts(command, expected):
    assert identity._script_argument(command) == expected


@pytest.mark.parametrize("arguments", [
    [r"C:\Program Files\Python\python.exe", r"C:\Some Folder\SniperCellSorterGUIv3.1.py"],
    ["python.exe", "-W", 'ignore:a "quoted" warning', SCRIPT, ""],
    ["pythonw.exe", SCRIPT, "backslashes\\\\", 'ends in slash \\'],
])
def test_windows_quoting_round_trips_without_a_shell(arguments):
    assert identity._split_windows_command_line(subprocess.list2cmdline(arguments)) == arguments


@pytest.mark.parametrize("command_line", [None, 42, "python.exe \0 bad"])
def test_invalid_command_lines_raise(command_line):
    with pytest.raises(ValueError):
        identity._split_windows_command_line(command_line)


def test_unterminated_quote_is_rejected():
    with pytest.raises(ValueError):
        identity._split_windows_command_line('python.exe "unterminated')


@pytest.mark.parametrize("command, expected", [
    (f'python.exe "{SCRIPT}"', True),
    (f'PYTHONW.EXE "{SCRIPT.upper()}"', True),
    (f'python.exe "{SCRIPT}."', False),
    (f'python.exe "{SCRIPT}.bak"', False),
    (f'python.exe other.py "{SCRIPT}"', False),
    ('python.exe "unterminated', False),
    ("cmd.exe /c dir", False),
])
def test_command_line_is_slacs(command, expected):
    assert identity.command_line_is_slacs(command) is expected


def test_script_name_constant():
    assert identity.SCRIPT_NAME == "SniperCellSorterGUIv3.1.py"
