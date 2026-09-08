"""Drop root privileges, then exec the real command.

The container starts as root only so the entrypoint can create /data and match the ownership of
the folder the NAS user copied over. Nothing after this point needs root, so the app runs as the
uid/gid that owns its own data.

Written in Python instead of pulling in gosu/su-exec: the interpreter is already the base image,
so there is nothing extra to install and nothing to go missing on a rebuild.
"""

from __future__ import annotations

import os
import sys

if len(sys.argv) < 4:
    sys.exit("usage: switch-user.py UID GID COMMAND [ARGS...]")

uid = int(sys.argv[1])
gid = int(sys.argv[2])
command = sys.argv[3:]

# Order matters: supplementary groups and the gid have to go before the uid, because dropping the
# uid first would take away the privilege needed to set them.
try:
    os.setgroups([gid])
except OSError:
    pass
os.setgid(gid)
os.setuid(uid)

# HOME still points at root's home, which the new uid cannot write to. Nothing in the app needs a
# home directory, but a stray library that assumes one should not be able to fail the boot.
os.environ["HOME"] = "/tmp"

os.execvp(command[0], command)
