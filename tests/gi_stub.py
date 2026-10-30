"""A minimal ``gi`` namespace for adapter tests on hosts without PyGObject.

The adapter tests never create widgets or run a main loop, so every GLib
source they schedule only stays pending. The stub preserves that contract:
scheduling functions return source IDs and never invoke callbacks, matching
real GLib when nothing iterates the default context. It also provides every
attribute the tests patch, so they run identically with and without PyGObject.
"""

from __future__ import annotations

import itertools
import sys
import types
from types import SimpleNamespace


class _GLibError(Exception):
    """Stands in for GLib.Error without catching unrelated exceptions."""


def _glib() -> SimpleNamespace:
    source_ids = itertools.count(1)

    def schedule(*_args, **_kwargs) -> int:
        return next(source_ids)

    return SimpleNamespace(
        Error=_GLibError,
        SOURCE_REMOVE=False,
        SOURCE_CONTINUE=True,
        idle_add=schedule,
        timeout_add=schedule,
        timeout_add_seconds=schedule,
        source_remove=lambda _source_id: True,
    )


class _Notification:
    @classmethod
    def new(cls, *_args):
        return cls()

    def show(self) -> bool:
        return True


def install() -> None:
    """Install the stub as ``gi``/``gi.repository`` unless it already is."""
    if getattr(sys.modules.get("gi"), "_airloom_test_stub", False):
        return
    gi = types.ModuleType("gi")
    gi._airloom_test_stub = True
    gi.require_version = lambda *_args: None
    repository = types.ModuleType("gi.repository")
    repository.Adw = SimpleNamespace(Application=object)
    repository.Gio = SimpleNamespace()
    repository.GLib = _glib()
    repository.Gtk = SimpleNamespace()
    repository.Notify = SimpleNamespace(
        init=lambda *_args: True, Notification=_Notification
    )
    repository.WebKit = SimpleNamespace()
    gi.repository = repository
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository
