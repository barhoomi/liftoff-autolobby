"""Drop root down to the user that owns the protocol directory.

`docker compose exec` defaults to **root**. A root-invoked CLI run therefore leaves
root-owned files under ``BepInEx/plugins/`` — and the plugin runs as ``botuser``, so the
next time it tried to write ``rotation_state.txt`` it could not, and ``/track`` answered
"Access denied" until a human ran ``chown`` inside the container. That is evidence 6 of
``docs/features/doing/workshop-ingame-download.md``, and this module plus the root-only
chowns in ``protocol.py`` are the two halves of the fix (§8): drop the privileges we
should not have, and correct the ownership of anything we write while we still have them.

The split into a **pure planner** and a **thin applier** is the same "inject the seam,
test the decision" shape ``workshop_download.py`` uses for ``clock``/``sleep``/``gather``:
``plan_privilege_drop`` takes the euid, the directory stat and a ``getpwuid`` as plain
arguments, so every branch of the decision is unit-testable without the suite ever being
root. ``drop_privileges_to_owner`` is deliberately branch-free around the syscalls it
cannot be tested through.
"""

import os
import pwd
from dataclasses import dataclass
from typing import Optional


@dataclass
class PrivilegeDrop:
    """The identity to become. Returned by the planner, applied by the applier."""

    uid: int
    gid: int
    user: str
    home: str


def plan_privilege_drop(euid, dir_stat, getpwuid) -> Optional[PrivilegeDrop]:
    """Who should this process become to match ``dir_stat``'s owner? None = stay put.

    None in three cases, each of them "there is nothing to drop to":

    - we are not root (``euid != 0``) — an unprivileged process cannot change identity,
      and the common case (an operator running the CLI as themselves) is already right;
    - the directory is owned by uid 0 — dropping would make things worse, not better;
    - the owning uid has no passwd entry — we would have no group and no home to switch
      to, so we keep today's behaviour (stay root) and let ``protocol.py``'s chowns be the
      backstop, rather than inventing an identity.
    """
    if euid != 0:
        return None
    uid = dir_stat.st_uid
    if uid == 0:
        return None
    try:
        entry = getpwuid(uid)
    except KeyError:
        return None
    return PrivilegeDrop(uid=uid, gid=entry.pw_gid, user=entry.pw_name, home=entry.pw_dir)


def drop_privileges_to_owner(path) -> Optional[PrivilegeDrop]:
    """Become the owner of ``path`` when running as root. Returns the applied plan.

    Order is not stylistic: ``setgroups`` and ``setgid`` are impossible once ``setuid``
    has run, so they must come first. ``HOME``/``USER``/``LOGNAME`` are updated because
    ``getpass.getuser()`` reads those environment variables *before* consulting the
    password database, and ``orchestrator/workshop_items.py`` builds its
    ``/home/<user>/.steam/...`` candidates from it — a process that changed uid but kept
    root's ``HOME`` would look for workshop content in ``/root``.
    """
    plan = plan_privilege_drop(os.geteuid(), os.stat(path), pwd.getpwuid)
    if plan is None:
        return None

    os.setgroups([])
    os.setgid(plan.gid)
    os.setuid(plan.uid)

    os.environ["HOME"] = plan.home
    os.environ["USER"] = plan.user
    os.environ["LOGNAME"] = plan.user

    print("[Privileges] dropped to {} (uid={} gid={}) to match the owner of {}.".format(
        plan.user, plan.uid, plan.gid, path))
    return plan
