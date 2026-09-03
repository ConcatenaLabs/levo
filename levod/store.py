"""Persistence: a single JSON file, written atomically.

Levo's durable state is small. Most of it is public anyway: project metadata,
sale terms, and which staking keys an account has proved -- all of it derivable
from the chain or already shown on the site. One part is not. The allocation
ledger maps an account key to the purchases it recorded and the amount it has
committed to each sale, and the API shows that only to the account it belongs
to; the public listing carries a count of buyers and nothing else.

So the file and its backups are read-restricted, which is why `save` writes
through `mkstemp` and leaves the mode it gives. There are still no keys and no
balances here, because levod holds neither: the tokens live in the covenant and
the stake lives on chain.

It is still worth backing up. A funded sale's leaves are rebuilt from the
terms in this file, and a project that did not keep its own copy of the
listing response has no other way to derive the witness that spends its
covenant. Losing the file costs nobody tokens, but it can cost a project the
easy path to its reclaim, and it costs every buyer their allocation record.
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Exit status for "the state on disk is not something levod may start from".
# It is a configuration failure, not a crash: restarting cannot fix it, and a
# supervisor that retried would bury the one message that says what to do.
# contrib/levod.service holds the matching RestartPreventExitStatus.
BAD_STATE_EXIT = 78

# How old a leftover temp file must be before a starting levod removes it. A
# save takes milliseconds; anything older than this was left by a process that
# is gone.
STALE_TEMP_SECONDS = 300


class Store:
    def __init__(self, path=None, exclusive=False):
        self.path = Path(path or os.environ.get("LEVOD_STATE", "levo-state.json"))
        self.data = {"projects": {}, "stake_links": {}, "version": 1}
        # Set when a write fails: the state in memory is then ahead of the
        # state on disk, which is a fact the operator has to be told.
        self.write_error = None
        self.dirty = False
        # Writes happen outside the caller's lock, so they are ordered here:
        # `_version` numbers the snapshots as they are built, `_written` is the
        # newest that reached the disk, and `_writing` keeps two writers from
        # interleaving their bytes.
        self._version = 0
        self._written = 0
        self._writing = threading.Lock()
        self._lock_fd = None
        if exclusive:
            self._take_the_file()
        if self.path.is_file():
            self.load()
        self._sweep_temp()

    def _take_the_file(self):
        """One levod per state file, enforced by the filesystem.

        Taken only by a running SERVICE (`exclusive=True`), which is what
        server.py and demo.py open. A tool or a test that reads or writes a
        state file is not a second levod, and locking those out would stop a
        round trip in one process from being written at all.

        Two processes on one file is not a rare mistake: the unit is running
        and somebody starts a second by hand to look at something, or a second
        unit is enabled by accident. Each keeps its own copy of the platform in
        memory and writes the whole file, so every purchase either of them
        records is erased by the other's next save -- silently, since both
        believe they wrote what they hold.

        The lock is advisory and held on an open descriptor, so a levod that
        crashes releases it at once and the next start is unimpeded.
        """
        try:
            import fcntl
        except ImportError:
            return                      # not a platform this ships on
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = open(str(self.path) + ".lock", "a+")
        except OSError:
            return                      # a read-only directory says its own thing
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.seek(0)
            other = (fd.read(40) or "").strip() or "another process"
            fd.close()
            self._refuse(
                "%s is already open by %s. Two levods on one state file "
                "overwrite each other's ledgers, so this one is not starting. "
                "Stop the other, or point this one at a state file of its own "
                "with LEVOD_STATE." % (self.path, other))
        fd.seek(0)
        fd.truncate()
        fd.write("pid %d" % os.getpid())
        fd.flush()
        self._lock_fd = fd              # held for the life of the process

    def _refuse(self, why):
        """Stop, with the reason where an operator will read it.

        The exit status says "do not restart me": a state file that cannot be
        read will not become readable in five seconds, and a restart loop turns
        one legible message into thousands of illegible ones.
        """
        sys.stderr.write("levod: %s\n" % why)
        sys.stderr.write("levod: not starting. Fix or restore the file, then "
                         "start levod again.\n")
        raise SystemExit(BAD_STATE_EXIT)

    def _sweep_temp(self):
        """Drop temp files a killed process left beside the state file. Each
        was an unfinished write; the state file itself was never touched.

        Only files old enough to be certain of. A second levod started against
        the same file -- a hand-run `python3 levod/server.py`, which builds its
        Store before it ever tries to bind a port -- would otherwise delete the
        temp file the running one is writing through, and the save it was
        halfway into fails.
        """
        cutoff = time.time() - STALE_TEMP_SECONDS
        try:
            for stale in self.path.parent.glob(".levo-*.tmp"):
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except ValueError as e:
            self._refuse("the state file %s is not valid JSON (%s). Restore it "
                         "from a backup rather than starting with an empty "
                         "ledger." % (self.path, e))
        except OSError as e:
            self._refuse("the state file %s cannot be read (%s)." % (self.path, e))
        if not isinstance(data, dict):
            self._refuse("the state file %s does not hold an object." % self.path)
        self.data = data
        self.data.setdefault("projects", {})
        self.data.setdefault("stake_links", {})

    def snapshot(self, data=None):
        """The bytes to write.

        Compact, not pretty. Indenting a state file with a large allocation
        ledger costs about five times the CPU and two thirds again the bytes,
        on every purchase, for a file whose readers are this program and
        `json.load`. `sort_keys` stays: it makes two saves of the same state
        the same bytes, which is what makes a backup worth diffing.
        """
        return json.dumps(self.data if data is None else data,
                          separators=(",", ":"), sort_keys=True).encode()

    def write(self, payload, version=None):
        """Put an already-serialised state on disk, atomically and in order.

        Two savers can build their snapshots in one order and reach the disk in
        the other, and the older one would then overwrite the newer with no
        sign at all. Each snapshot carries the version it was built at, and a
        write older than the one already on disk is dropped: the newer write
        contains everything the older one did, because both were built from the
        same state under the same lock.
        """
        with self._writing:
            if version is not None and version <= self._written:
                return False
            self._write(payload)
            if version is not None:
                self._written = version
            return True

    def next_version(self):
        """The version a snapshot taken now belongs to. Called under the
        caller's own lock, so two snapshots never share one."""
        self._version += 1
        return self._version

    def save(self):
        """Write via a temp file and rename, so an interrupted save cannot
        truncate the state that was already good.

        A failure here is remembered. The change it was writing has already
        been made in memory, so a levod that goes on serving after a failed
        save is running on a ledger that exists nowhere else: the next restart
        silently reverts to the last write that worked. `write_error` is what
        the health endpoint reads, and `dirty` is what makes the watcher try
        the write again on its next poll.
        """
        # Versioned like every other write, so a save taken here can never
        # overwrite a newer one that is still on its way to the disk. Without
        # the version this path is the one hole in the ordering guard.
        with self._writing:
            self._version += 1
            version = self._version
            if version <= self._written:
                return False
            ok = self._write(self.snapshot())
            self._written = version
            return ok

    def _write(self, payload):
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".levo-", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception as e:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
            self.write_error = str(e)
            self.dirty = True
            raise
        self.write_error = None
        self.dirty = False
