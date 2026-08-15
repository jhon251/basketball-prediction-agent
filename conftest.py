"""Root-level test environment bootstrap (sandbox adaptation only).

The DSH file sandbox on Windows translates ``os.mkdir`` mode bits into real
ACLs.  Libraries that create temp directories with mode 0o700
(``tempfile.mkdtemp``, pytest's numbered basetemp directories, pip build
dirs) therefore produce directories the sandbox later refuses to enumerate,
which breaks ``tmp_path`` with ``PermissionError``.

This conftest:
* forces directory creation to mode 0o777 for the duration of the test
  session (standard full-access mode; identical on normal systems);
* points pytest's basetemp at a fresh per-run directory so leftovers locked
  by earlier runs can never be touched.

It adapts the environment only — it changes no assertions and no behavior of
the code under test.
"""
from __future__ import annotations

import os
from pathlib import Path

_real_mkdir = os.mkdir


def _sandbox_safe_mkdir(path, mode=0o777, *, dir_fd=None):
    """Create directories with full permissions so the sandbox does not
    attach owner-only ACLs to them."""
    return _real_mkdir(path, 0o777, dir_fd=dir_fd)


os.mkdir = _sandbox_safe_mkdir


def pytest_configure(config):
    """Use a fresh, per-run basetemp directory (never a leftover from
    previous runs, which may be ACL-locked by the sandbox)."""
    base = Path(__file__).resolve().parent / ".tmp" / f"pytest-basetemp-{os.getpid()}"
    os.makedirs(base, exist_ok=True)
    config.option.basetemp = str(base)
