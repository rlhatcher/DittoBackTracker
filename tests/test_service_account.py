"""Hold the service account's four files to each other.

The account the service runs as is named in four places that have never checked
one another: `User=`/`Group=` in the unit, the principal in each of the two
sudoers rules, and the `chown` target plus the fstab `uid=`/`gid=` in
install.sh. They agree today by hand.

Every way they can disagree is silent here and quiet on the device:

  * Change `User=` and forget a sudoers file, and the rule stops matching. The
    Done button unmounts the pedal, answers 200, and leaves the Pi running —
    the response is already sent by the time sudo is refused. Update reports
    "the restart was refused" and keeps serving the old code.
  * Change the `chown` target and not the fstab options, and the service owns
    its data partition but cannot read the pedal it just mounted.

The argv checks are the same idea one step further in. test_web_session.py
already pins `sudo -n /sbin/poweroff` against a literal, which catches core.py
drifting — but not the rule drifting away from core.py, because nothing in the
suite reads etc/99-ditto-poweroff. Nothing reads etc/99-ditto-restart at all;
test_update.py stubs anything containing "systemctl" and asserts the refusal
message, so the path and arguments in that rule are unchecked in both
directions.

Read out of the source with ast rather than run, for the reason
conftest._block_sudo exists: the commands under test power off or restart the
machine running the suite. Same approach as test_frontend.py, which parses
app.js to pin what it reaches for.
"""

import ast
import re
from pathlib import Path

from ditto import config

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "systemd" / "ditto-web.service"
POWEROFF_RULE = ROOT / "etc" / "99-ditto-poweroff"
RESTART_RULE = ROOT / "etc" / "99-ditto-restart"
INSTALL_SH = ROOT / "install.sh"


def _unit_value(key: str) -> str:
    """One `Key=value` from the unit file. Unit files allow a key to repeat,
    with the last winning, so take the last rather than the first."""
    found = [line.split("=", 1)[1].strip()
             for line in UNIT.read_text().splitlines()
             if line.strip().startswith(f"{key}=")]
    assert found, f"{UNIT.name} has no {key}="
    return found[-1]


def _rule(path: Path) -> tuple:
    """(principal, command, args) from a sudoers.d file.

    args is None when the rule names no arguments — which in sudoers means the
    command may be run with *any* arguments, not none. `""` is how sudoers
    spells "no arguments", and parses here as an empty list.
    """
    lines = [ln.strip() for ln in path.read_text().splitlines()]
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    assert len(rules) == 1, f"{path.name} has {len(rules)} rules, expected 1"
    who, _, what = rules[0].partition("ALL=(root) NOPASSWD:")
    assert what, f"{path.name} is not a NOPASSWD root rule: {rules[0]!r}"
    argv = what.split()
    args = None if len(argv) == 1 else ([] if argv[1:] == ['""'] else argv[1:])
    return who.strip(), argv[0], args


def _permits(rule: tuple, argv: list) -> bool:
    """Would this sudoers rule let this argv run?

    argv is what the code passes subprocess.run, so it starts with `sudo` and
    whatever options it wants. Options are sudo's, not the command's, so strip
    them before comparing against the rule.
    """
    _, command, args = rule
    rest = argv[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[1:]
    if not rest or rest[0] != command:
        return False
    return args is None or rest[1:] == args


def _sudo_argvs(module: str) -> list:
    """Every `subprocess.run(["sudo", ...])` argv in a module, read statically.

    Resolves `config.NAME` elements, since the restart call names the unit
    through config.RESTART_SERVICE rather than spelling it out.
    """
    tree = ast.parse((ROOT / "ditto" / module).read_text())
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and node.args
                and isinstance(node.args[0], ast.List)):
            continue
        argv = []
        for el in node.args[0].elts:
            if isinstance(el, ast.Constant):
                argv.append(el.value)
            elif (isinstance(el, ast.Attribute)
                  and isinstance(el.value, ast.Name)
                  and el.value.id == "config"):
                argv.append(getattr(config, el.attr))
            else:
                argv.append(None)   # something this parser can't resolve
        if argv and argv[0] == "sudo":
            out.append(argv)
    return out


def test_the_unit_and_both_sudoers_rules_name_one_account():
    user, group = _unit_value("User"), _unit_value("Group")
    assert user == group, f"unit runs as {user} but group {group}"
    for path in (POWEROFF_RULE, RESTART_RULE):
        assert _rule(path)[0] == user, \
            f"{path.name} grants {_rule(path)[0]}, unit runs as {user}"


def test_the_installer_gives_the_data_partition_to_that_account():
    """install.sh names the account once, in SVC=, and spends it through a
    variable everywhere else. Both spends are checked, because they fail
    differently: the wrong chown target leaves the service unable to write its
    own data partition, the wrong fstab uid= leaves it unable to read the pedal
    it just mounted."""
    user = _unit_value("User")
    sh = INSTALL_SH.read_text()
    declared = re.search(r"^SVC=(\S+)$", sh, re.M)
    assert declared, "install.sh has no SVC= line naming the service account"
    assert declared.group(1) == user, \
        f"install.sh installs for {declared.group(1)}, unit runs as {user}"
    assert 'chown -R "$SVC:$SVC" /var/lib/ditto' in sh, \
        "install.sh does not chown the data partition to $SVC"
    assert "uid=$SVC,gid=$SVC" in sh, \
        "install.sh does not mount the pedal as $SVC"


def test_the_poweroff_rule_permits_the_command_core_runs():
    argvs = [a for a in _sudo_argvs("core.py")
             if any("poweroff" in str(x) for x in a)]
    assert len(argvs) == 1, f"expected one poweroff in core.py, found {argvs}"
    rule = _rule(POWEROFF_RULE)
    assert _permits(rule, argvs[0]), \
        f"{POWEROFF_RULE.name} allows {rule[1:]}, core.py runs {argvs[0]}"


def test_the_restart_rule_permits_the_command_the_updater_runs():
    argvs = [a for a in _sudo_argvs("update.py")
             if any("systemctl" in str(x) for x in a)]
    assert len(argvs) == 1, f"expected one systemctl in update.py, got {argvs}"
    rule = _rule(RESTART_RULE)
    assert _permits(rule, argvs[0]), \
        f"{RESTART_RULE.name} allows {rule[1:]}, update.py runs {argvs[0]}"
