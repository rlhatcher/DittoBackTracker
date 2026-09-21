"""Hold the service account's two files to each other.

The account the service runs as is named in two places that have never checked
one another: `User=`/`Group=` in the unit, and the `chown` target plus the
fstab `uid=`/`gid=` in install.sh. They agree today by hand. Change the `chown`
target and not the fstab options, and the service owns its data partition but
cannot read the pedal it just mounted, and nothing says so.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "systemd" / "ditto-web.service"
INSTALL_SH = ROOT / "install.sh"


def _unit_value(key: str) -> str:
    """One `Key=value` from the unit file. Unit files allow a key to repeat,
    with the last winning, so take the last rather than the first."""
    found = [line.split("=", 1)[1].strip()
             for line in UNIT.read_text().splitlines()
             if line.strip().startswith(f"{key}=")]
    assert found, f"{UNIT.name} has no {key}="
    return found[-1]


def test_the_unit_runs_as_one_account():
    user, group = _unit_value("User"), _unit_value("Group")
    assert user == group, f"unit runs as {user} but group {group}"


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
