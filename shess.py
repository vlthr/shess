#!/usr/bin/env python
import dataclasses
import psutil
import datetime
import argparse
import json
from dataclasses import dataclass
import os
import sys
import re
from pathlib import Path

CACHE_DIR = Path("~/.cache/shess")
UNSET = object()

def might_be_interactive_shell(cmdline):
    # List of known shells
    known_shells = ['bash', 'sh', 'zsh', 'csh', 'ksh', 'fish', 'tcsh', 'dash']

    # Extract basename and check if it's a known shell (with optional leading dash)
    shell_name = os.path.basename(cmdline[0])
    if not re.match(r'-?({})'.format('|'.join(known_shells)), shell_name):
        return False

    # Combine all options into a single string for regex matching
    alias = {"login": "l", "interactive": "l"}
    options = set([opt.lstrip("-") for opt in cmdline[1:] if opt.startswith('-') and not opt.startswith("--")])
    long_options = set([opt.lstrip("-") for opt in cmdline[1:] if opt.startswith("--")])
    for o in long_options:
        if o in alias:
            options.add(alias[o])

    # Check for the interactive option '-i'
    if "i" in options:
        return True

    # Check if no non-option args were passed AND no '-c' option was passed
    non_option_args = [arg for arg in cmdline[1:] if not arg.startswith('-')]
    if not non_option_args and 'c' not in options:
        return True

    return False

def get_parent_processes(pid):
    try:
        process = psutil.Process(pid)
        while process:
            yield process
            process = process.parent()
    except psutil.Error as e:
        sys.stderr.write(f"Error: {e}\n")

@dataclass
class PidData:
    pid: int
    pid_create_time: str
    data: dict
    inherit: bool


@dataclass
class ProcessState:
    pid: int
    create_time: str
    cmdline: list
    is_interactive_shell: bool
    is_terminal: bool

def load_pid_data(pstate: ProcessState) -> PidData | None:
    file_path = CACHE_DIR / f"{pstate.pid}.pid"
    if file_path.is_file():
        contents = file_path.read_text()
        data = PidData(**json.loads(contents))
        recorded_create_time = datetime.datetime.fromisoformat(data.pid_create_time)
        actual_create_time = datetime.datetime.fromisoformat(pstate.create_time)
        if recorded_create_time < actual_create_time:
            # pid has been reused
            # TODO: delete file?
            return None
        return data
    else:
        return None

def save_pid_data(data: PidData):
    file_path = CACHE_DIR / f"{data.pid}.pid"
    text = json.dumps(dataclasses.asdict(data))
    file_path.write_text(text)

def get_parent_chain():
    parents = []
    tty = None
    for proc in get_parent_processes(os.getpid()):
        with proc.oneshot():
            try:
                if proc.pid in (0, 1):
                    break
                pgid = os.getpgid(proc.pid)
                if might_be_interactive_shell(proc.cmdline()):
                    # print(f"{proc.pid}")
                    # print(f"\tPID: {proc.pid}, Name: {proc.name()}, PGID: {pgid}", file=sys.stderr)
                    # print(f"\tmight_be_interactive_shell", file=sys.stderr)
                    create_time = datetime.datetime.fromtimestamp(proc.create_time(), tz=datetime.UTC).isoformat()
                    parents.append(ProcessState(pid=proc.pid, create_time=create_time, cmdline=proc.cmdline(), is_interactive_shell=True, is_terminal=False))
                proc_tty = proc.terminal()
                if proc_tty is not None:
                    tty = proc_tty
                if proc_tty is None and tty is not None:
                    # this is probably the terminal
                    terminal_pid = proc.pid
                    create_time = datetime.datetime.fromtimestamp(proc.create_time(), tz=datetime.UTC).isoformat()
                    parents.append(ProcessState(pid=proc.pid, create_time=create_time, cmdline=proc.cmdline(), is_interactive_shell=False, is_terminal=True))
                    break
            except psutil.NoSuchProcess:
                pass
            except Exception as e:
                sys.stderr.write(f"Error querying PID {proc.pid}: {e}\n")

    if not parents:
        raise Exception("Could not find nearest interactive shell")

    return parents

def get_command(args):
    key = args.key
    is_raw = args.raw
    parents = get_parent_chain()
    value = UNSET
    for p in parents:
        state = load_pid_data(p)
        if state is None:
            continue
        value = state.data.get(key, UNSET)
        if value is not UNSET:
            break
        if not state.inherit:
            break
    if value is UNSET:
        print(f"No value found for key `{key}`", file=sys.stderr)
        sys.exit(1)
    elif is_raw:
        assert isinstance(value, str), "`-r` or `--raw` can only be used with string values"
        print(value, end=None)
    else:
        print(json.dumps(value), end=None)

def set_command(args):
    key = args.key
    value = args.value
    is_raw = args.raw
    parents = get_parent_chain()
    parent = parents[0]
    state = load_pid_data(parent)
    if state is None:
        state = PidData(pid=parent.pid, pid_create_time=parent.create_time, data={}, inherit=True)

    if is_raw:
        state.data[key] = value
    else:
        state.data[key] = json.loads(value)
    save_pid_data(state)


def debug_parents_command(args):
    # Implementation for 'debug parents' command
    # Print parents data
    parents = get_parent_chain()
    for p in parents:
        print(json.dumps(p))

def main(cache_dir: Path):
    parser = argparse.ArgumentParser(description="Utility script.")
    subparsers = parser.add_subparsers(help='sub-command help')

    # Subparser for the 'get' command
    parser_get = subparsers.add_parser('get', help='Read the value of the given key')
    parser_get.add_argument('key', type=str, help='Key to get value for')
    parser_get.add_argument('-r', '--raw', action='store_true', help='If the value is a string, strip the JSON encoding')
    parser_get.set_defaults(func=get_command)

    # Subparser for the 'set' command
    parser_set = subparsers.add_parser('set', help='Set the value of the given key')
    parser_set.add_argument('key', type=str, help='Key to set value for')
    parser_set.add_argument('value', type=str, nargs='?', default=None, help='Value to set for the key')
    parser_set.add_argument('-r', '--raw', action='store_true', help='Interpret the value as a raw string, not JSON')
    parser_set.set_defaults(func=set_command)

    # Subparser group for 'debug' commands
    parser_debug = subparsers.add_parser('debug', help='Debugging utilities')
    debug_subparsers = parser_debug.add_subparsers(help='Debugging commands')

    # Subparser for the 'debug parents' command
    parser_debug_parents = debug_subparsers.add_parser('parents', help='Print the parent PIDs')
    parser_debug_parents.set_defaults(func=debug_parents_command)

    args = parser.parse_args()
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    main(cache_dir=CACHE_DIR)
