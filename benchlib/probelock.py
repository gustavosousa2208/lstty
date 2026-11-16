"""Machine-wide exclusive lock per debug probe, keyed by the probe's USB serial.

Why: two agents (or an agent and a person) must never talk to the same probe at once.
Every path that touches a probe takes this lock, so nobody has to remember to.

  with hold(serial, "flash app.hex"):   # waits, then runs; raises ProbeBusy on timeout
      ...
  hold(serial, "west flash", inherit=True).acquire()   # then os.exec*: the lock lives on in
                                                        # the exec'd program (fd + env token)

Design points:
  * flock(2): the kernel drops the lock when the holder dies, so a crash never leaves a stale one.
  * Keyed by serial, not nickname or /dev path, so every alias of one probe shares one lock.
  * Re-entrant across processes: an ancestor that holds a probe exports it in
    BENCH_LOCKS_HELD; children skip the lock instead of deadlocking on their parent.
  * Machine-wide (not per project): the hardware is shared by every project on the host.
  * Where flock is missing (Windows) the lock is a no-op, like lstty's config lock.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

HELD_ENV = "BENCH_LOCKS_HELD"
WAIT_ENV = "BENCH_LOCK_WAIT"          # seconds to wait for a busy probe (default 60; 0 = fail now)
DIR_ENV = "BENCH_LOCK_DIR"
DEFAULT_WAIT = 60.0
NOTICE_EVERY = 5.0


class ProbeBusy(Exception):
    def __init__(self, serial, owner, waited):
        self.serial, self.owner, self.waited = serial, owner, waited
        super().__init__(
            f"probe {serial} is in use by {owner} (waited {waited:.0f} s). "
            f"Retry later, or raise {WAIT_ENV}.")


def lock_dir():
    explicit = os.environ.get(DIR_ENV) or os.environ.get("BOARD_LOCK_DIR")
    if explicit:
        return Path(explicit)
    state = os.environ.get("AGENTS_STATE_DIR") or str(Path.home() / ".local/state/agents")
    return Path(state) / "locks"


def key_for(serial):
    """Canonical lock name. Case-folded; leading zeros dropped from all-digit serials
    (J-Link reports 000683123456 and 683123456 for the same probe)."""
    s = str(serial).strip().upper()
    if s.isdigit():
        s = s.lstrip("0") or "0"
    safe = re.sub(r"[^A-Z0-9._-]", "_", s)
    if not safe:
        raise ValueError("empty probe serial")
    return "probe-" + safe


def _held_tokens():
    return {t for t in os.environ.get(HELD_ENV, "").split(":") if t}


def _who():
    """Best-effort identity of the caller, for the 'busy' message."""
    actor = os.environ.get("BEADS_ACTOR", "")
    sid = os.environ.get("AGENT_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not actor and sid:
        state = os.environ.get("AGENTS_STATE_DIR") or str(Path.home() / ".local/state/agents")
        try:
            actor = json.loads((Path(state) / "sessions" / f"{sid}.json").read_text()).get("actor", "")
        except (OSError, ValueError):
            pass
    return actor or os.environ.get("USER", "unknown")


def _describe(owner_file):
    try:
        info = json.loads(owner_file.read_text())
    except (OSError, ValueError):
        return "an unknown holder"
    age = max(0, int(time.time() - info.get("since", time.time())))
    alive = ""
    pid = info.get("pid")
    if isinstance(pid, int) and not Path(f"/proc/{pid}").exists() and sys.platform.startswith("linux"):
        alive = ", no such process: stale note"
    return f"{info.get('who', '?')} ({info.get('what', '?')}, pid {info.get('pid', '?')}, {age} s ago{alive})"


class hold:
    """Context manager / explicit acquire. See the module docstring."""

    def __init__(self, serial, what="", wait=None, inherit=False, quiet=False):
        self.serial = str(serial)
        self.what = what or "probe access"
        self.inherit = inherit
        self.quiet = quiet
        env_wait = os.environ.get(WAIT_ENV)
        self.wait = float(wait if wait is not None else (env_wait if env_wait not in (None, "") else DEFAULT_WAIT))
        self.fd = None
        self.token = None
        self.noop = False

    # -- acquisition ---------------------------------------------------------------------
    def acquire(self):
        self.token = key_for(self.serial)
        if fcntl is None or self.token in _held_tokens():
            self.noop = True            # no flock here, or an ancestor already holds it
            return self
        directory = lock_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(directory / f"{self.token}.lock", os.O_RDWR | os.O_CREAT, 0o666)
        except OSError as exc:
            # Cannot create the lock (read-only home, odd FS): warn loudly, do not block work.
            print(f"warning: probe lock unavailable ({exc}); continuing WITHOUT mutual exclusion",
                  file=sys.stderr)
            self.noop = True
            return self
        owner_file = directory / f"{self.token}.owner"
        start = time.monotonic()
        last_notice = -NOTICE_EVERY
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.monotonic() - start
                if waited >= self.wait:
                    os.close(self.fd)
                    self.fd = None
                    raise ProbeBusy(self.serial, _describe(owner_file), waited) from None
                if not self.quiet and waited - last_notice >= NOTICE_EVERY:
                    print(f"waiting for probe {self.serial}: in use by {_describe(owner_file)}",
                          file=sys.stderr)
                    last_notice = waited
                time.sleep(0.2)
        try:
            owner_file.write_text(json.dumps({
                "who": _who(), "what": self.what, "pid": os.getpid(),
                "host": os.uname().nodename if hasattr(os, "uname") else "", "since": time.time(),
            }))
        except OSError:
            pass
        if self.inherit:
            os.set_inheritable(self.fd, True)   # survives os.exec*: the exec'd program keeps the lock
        os.environ[HELD_ENV] = ":".join(sorted(_held_tokens() | {self.token}))
        return self

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
        if self.token and not self.noop:
            left = _held_tokens() - {self.token}
            if left:
                os.environ[HELD_ENV] = ":".join(sorted(left))
            else:
                os.environ.pop(HELD_ENV, None)

    @property
    def pass_fds(self):
        """For subprocess.run(pass_fds=...): the child keeps the lock if we are killed, so an
        orphaned JLinkExe still talking to the probe cannot be joined by a second user."""
        return (self.fd,) if self.fd is not None else ()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def hold_many(serials, what="", **kw):
    """Lock several probes in canonical order (no lock-order deadlocks between callers)."""
    from contextlib import ExitStack
    stack = ExitStack()
    try:
        for serial in sorted(set(map(str, serials)), key=key_for):
            stack.enter_context(hold(serial, what, **kw))
    except BaseException:
        stack.close()
        raise
    return stack
