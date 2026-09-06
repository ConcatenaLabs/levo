"""Which Levo this is: the commit of the checkout it runs from.

Levo has no version number. It is deployed from a checkout, so the one fact
that names a running levod or a `levo` command for a report is the commit the
checkout is at. Read from `.git` directly rather than by running git: the
service runs in a sandbox that need not have git, and a checkout's HEAD is a
plain file either way.
"""
import re
from pathlib import Path

_HEX = re.compile(r"^[0-9a-f]{40}$")


def commit(root):
    """The full commit id the checkout at `root` is at, or None when `root` is
    not a checkout, or is one whose HEAD cannot be resolved without git."""
    git = Path(root) / ".git"
    try:
        if git.is_file():                       # a worktree: `gitdir: <path>`
            line = git.read_text().strip()
            if not line.startswith("gitdir:"):
                return None
            git = Path(line[len("gitdir:"):].strip())
            if not git.is_absolute():
                git = (Path(root) / git).resolve()
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return None
    if _HEX.match(head):
        return head                             # detached
    if not head.startswith("ref: "):
        return None
    ref = head[len("ref: "):].strip()
    # A worktree's HEAD lives in its own directory; its refs in the common one.
    common = git
    try:
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text().strip()).resolve()
    except OSError:
        pass
    try:
        loose = common / ref
        if loose.is_file():
            value = loose.read_text().strip()
            return value if _HEX.match(value) else None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    value = line.split(" ", 1)[0]
                    return value if _HEX.match(value) else None
    except OSError:
        return None
    return None


def short(root):
    """The first twelve characters of the commit, or None."""
    c = commit(root)
    return c[:12] if c else None
