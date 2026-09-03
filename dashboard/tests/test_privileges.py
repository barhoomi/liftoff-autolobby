"""dashboard.control.privileges -- the root -> owner drop, decided purely.

`plan_privilege_drop` takes its euid, its stat result and its `getpwuid` as parameters
exactly so this suite never has to be root (and never has to fake `os.setuid`).
`drop_privileges_to_owner` itself is deliberately untested: it is a branch-free wrapper
around syscalls a test process must not make.
"""

from dashboard.control.privileges import PrivilegeDrop, plan_privilege_drop


class FakeStat:
    def __init__(self, uid, gid=None):
        self.st_uid = uid
        self.st_gid = uid if gid is None else gid


class FakePasswd:
    def __init__(self, uid, gid, name, home):
        self.pw_uid = uid
        self.pw_gid = gid
        self.pw_name = name
        self.pw_dir = home


def passwd_db(**entries):
    def getpwuid(uid):
        if uid not in entries:
            raise KeyError(uid)
        return entries[uid]
    return getpwuid


BOTUSER = FakePasswd(1000, 1000, "botuser", "/home/botuser")
DB = passwd_db(**{"1000": BOTUSER})


def getpwuid(uid):
    if uid == 1000:
        return BOTUSER
    raise KeyError(uid)


class TestPlanPrivilegeDrop:
    def test_a_non_root_process_never_drops(self):
        """The ordinary case -- an operator running the CLI as themselves. There is
        nothing to drop to, and trying would just fail."""
        assert plan_privilege_drop(1000, FakeStat(1000), getpwuid) is None

    def test_a_root_owned_directory_gives_nothing_to_drop_to(self):
        assert plan_privilege_drop(0, FakeStat(0), getpwuid) is None

    def test_root_over_a_botuser_owned_directory_plans_the_drop(self):
        plan = plan_privilege_drop(0, FakeStat(1000), getpwuid)
        assert plan == PrivilegeDrop(uid=1000, gid=1000, user="botuser",
                                     home="/home/botuser")

    def test_the_gid_comes_from_the_passwd_entry_not_the_directory(self):
        """setgid must land on the user's own primary group; a directory can carry any
        group at all (a shared 'steam' group, say) without that being the identity we
        want to become."""
        plan = plan_privilege_drop(0, FakeStat(1000, gid=999), getpwuid)
        assert plan.gid == 1000

    def test_an_owner_with_no_passwd_entry_leaves_us_where_we_are(self):
        """No group and no home to switch to -- keep today's behaviour (stay root) and
        let ProtocolDir's chowns be the backstop, rather than inventing an identity."""
        assert plan_privilege_drop(0, FakeStat(4242), getpwuid) is None
