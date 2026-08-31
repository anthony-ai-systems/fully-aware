#!/usr/bin/env python3
"""notify.py -- the push edge: one-way phone notification over ntfy.

The audit that built this repo found notification channels that were dead
while reporting success, so this module fails in exactly one direction: a
lane must never break, hang, or change its exit code because its messenger
did. Every failure to send is a silent ``False``.

Three rules:

  1. OPT-IN. Nothing sends unless ``FULLY_AWARE_PUSH=1`` is in the
     environment. The live lane wrappers (nightly-fix.sh, morning-pack.sh)
     set it; tests and hand runs never do, so the whole lane can execute
     against fixtures without reaching the network.
  2. The topic never appears in this repository. It is read at send time
     from ``~/.claude/ntfy-topic`` (or the file ``FULLY_AWARE_NTFY_TOPIC_FILE``
     names). No file, or a file that does not hold one bare token: no push.
     Never print or log the topic.
  3. Push only what needs a human. Callers decide that; this module just
     carries the message. A channel that pings on routine outcomes gets
     muted, and then the one push that mattered is never seen.

Python 3.9, standard library only.
"""

import os
import urllib.request

DEFAULT_TOPIC_FILE = os.path.expanduser("~/.claude/ntfy-topic")
NTFY_BASE = "https://ntfy.sh"
TIMEOUT_S = 10
MESSAGE_LIMIT = 600
PRIORITIES = ("min", "low", "default", "high", "urgent")


def topic_file():
    return os.environ.get("FULLY_AWARE_NTFY_TOPIC_FILE") or DEFAULT_TOPIC_FILE


def read_topic(path=None):
    """The topic string, or ``None``. Never raises.

    A topic is one bare token. Whitespace inside the file body or a path
    separator means a corrupt or mis-pointed file, not a topic -- refuse it
    rather than POST to a mangled URL.
    """
    try:
        with open(path or topic_file(), "r", encoding="utf-8") as fh:
            topic = fh.read().strip()
    except OSError:
        return None
    if not topic or any(ch.isspace() for ch in topic) or "/" in topic:
        return None
    return topic


def _header_safe(text):
    """HTTP header values are latin-1 and single-line; flatten and replace."""
    one_line = " ".join(str(text).split())
    return one_line.encode("latin-1", "replace").decode("latin-1")


def _http_post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S):
        pass


def push(title, message, priority="default", transport=None):
    """Send one push. ``True`` only when the message was handed to the
    network; ``False`` for every other outcome -- disabled, no topic, network
    failure. Never raises.

    ``transport`` is injectable for tests: ``transport(url, data, headers)``.
    """
    if os.environ.get("FULLY_AWARE_PUSH") != "1":
        return False
    topic = read_topic()
    if topic is None:
        return False
    if priority not in PRIORITIES:
        priority = "default"
    body = " ".join(str(message).split())
    if len(body) > MESSAGE_LIMIT:
        body = body[: MESSAGE_LIMIT - 3] + "..."
    headers = {"Title": _header_safe(title), "Priority": priority}
    try:
        (transport or _http_post)("%s/%s" % (NTFY_BASE, topic),
                                  body.encode("utf-8"), headers)
    except Exception:
        return False
    return True
