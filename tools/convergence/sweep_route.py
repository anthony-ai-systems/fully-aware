"""Content-free observation of the existing IRIS automation configuration.

Configuration is not scheduler liveness or permission to run. This module never
changes an automation, reads its prompt into an output, or retries any work.
"""
import datetime as dt
import os
from pathlib import Path
import stat
import tomllib

AUTOMATION = "iris-proactive-work-sweep"
OWNER = "01a0cf00-be7a-7263-ac37-4d175f09b546"
SCHEDULE = "FREQ=DAILY;BYHOUR=9,13,17;BYMINUTE=0;BYSECOND=0"
MAX_BYTES = 256 * 1024


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_route(path, *, now=None):
    """Read a single explicitly selected local config; expose allowlisted facts."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result = {
        "availability": "unavailable", "configured_status": "unknown",
        "reason": "not_configured", "observed_at": now.isoformat(),
        "execution_verified": False, "authority": "none",
    }
    if path is None:
        return result
    try:
        target = Path(path)
        if not target.is_absolute() or target.resolve() != target:
            raise ValueError("invalid path")
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        with os.fdopen(os.open(target, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise ValueError("invalid file")
            raw = stream.read(MAX_BYTES + 1)
            if (len(raw) > MAX_BYTES or identity(before) != identity(os.fstat(stream.fileno()))
                    or identity(before) != identity(target.lstat())):
                raise ValueError("configuration changed")
        data = tomllib.loads(raw.decode("utf-8"))
        if (type(data.get("version")) is not int or data["version"] != 1
                or data.get("id") != AUTOMATION or data.get("kind") != "heartbeat"
                or data.get("status") not in ("ACTIVE", "PAUSED")
                or not isinstance(data.get("target_thread_id"), str)
                or not isinstance(data.get("rrule"), str)):
            raise ValueError("invalid identity")
        # No prompt, name, arbitrary owner ID, recurrence text or path is emitted.
        result.update(availability="available", configured_status=data["status"].lower(),
                      reason="configuration_observed",
                      expected_owner=data["target_thread_id"] == OWNER,
                      expected_schedule=data["rrule"] == SCHEDULE)
    except FileNotFoundError:
        # Missing local storage is not proof that the app deleted the automation.
        result["reason"] = "local_configuration_missing"
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        result["reason"] = "local_configuration_unreadable_or_invalid"
    return result
