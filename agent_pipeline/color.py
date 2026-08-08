"""Targeted ANSI color helpers, stdlib-only.

Respects NO_COLOR / FORCE_COLOR env var conventions and checks the
destination stream's isatty() so piped/redirected output stays plain.
"""

from __future__ import print_function

import os
import sys

_CODES = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "dim": "2", "bold": "1"}


def enabled(stream=None):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream if stream is not None else sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def wrap(name, text, stream=None):
    if not enabled(stream):
        return text
    return "\x1b[%sm%s\x1b[0m" % (_CODES[name], text)


def red(text, stream=None):
    return wrap("red", text, stream)


def green(text, stream=None):
    return wrap("green", text, stream)


def yellow(text, stream=None):
    return wrap("yellow", text, stream)


def cyan(text, stream=None):
    return wrap("cyan", text, stream)


def dim(text, stream=None):
    return wrap("dim", text, stream)


def bold(text, stream=None):
    return wrap("bold", text, stream)


STATE_COLOR = {
    "ready": "dim",
    "running": "yellow",
    "awaiting_retry_approval": "yellow",
    "awaiting_human_test": "yellow",
    "awaiting_final_decision": "yellow",
    "blocked": "red",
    "failed": "red",
    "complete": "green",
    "passed": "green",
    "CORRUPT": "red",
}


def colorize_state(label, stream=None):
    color_name = STATE_COLOR.get(label)
    if color_name is None:
        return label
    return wrap(color_name, label, stream)
