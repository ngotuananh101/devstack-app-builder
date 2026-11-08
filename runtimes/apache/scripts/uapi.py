#!/usr/bin/env python3
"""Locate the host's kernel UAPI headers, which musl deliberately does not ship.

musl provides the C library, not the kernel interface: <linux/*.h> and the
<asm/*.h> they include are the distribution's, and they must match the kernel the
bundle runs on. Where a distribution puts them is not portable — Fedora has both
under /usr/include, Ubuntu splits them between /usr/include (linux/) and
/usr/include/x86_64-linux-gnu (asm/) — so the compiler is asked for its search
list and every directory that supplies one of the two is used. Assuming a single
/usr/include is what failed the CI job: <linux/mman.h> was found, and the
<asm/mman.h> it includes was not beside it.

The roots are announced by loader.py as a colon-separated list, the way PATH
works, and the flag they render to is -idirafter — never -I — so a musl header of
the same name still wins.
"""
import os
from pathlib import Path
import re
import subprocess

MARKERS = ("linux", "asm")
SEPARATOR = ":"


def roots(value):
    """Validate a root list and return it, refusing one that does not cover both.

    <linux/mman.h> includes <asm/mman.h>, so a list that supplies only one of the
    two fails on the other, deep inside a dependency build. It is checked here so
    the failure names the flag instead.
    """
    items = [item for item in str(value).split(SEPARATOR) if item]
    if not items:
        raise ValueError("No kernel include root was given")
    covered = set()
    for item in items:
        path = Path(item)
        if not path.is_absolute():
            raise ValueError("The kernel include root must be absolute: " + item)
        if not path.is_dir():
            raise ValueError("No such kernel include root: " + item)
        for name in MARKERS:
            if (path / name).is_dir():
                covered.add(name)
    missing = set(MARKERS) - covered
    if missing:
        raise ValueError(
            "No kernel include root holds %s/; musl ships no kernel UAPI headers, "
            "so the host must provide them: %s"
            % ("/ and ".join(sorted(missing)), SEPARATOR.join(items)))
    return items


def flags(value):
    """Render the root list as compiler flags, one directory per word.

    Returned as separate words rather than one string: a caller that puts the whole
    string into argv would pass "-idirafter /usr/include" as a single unusable
    argument.
    """
    words = []
    for item in roots(value):
        words += ["-idirafter", item]
    return words


def env_flags(value):
    """The same flags as a single string, for CFLAGS-style variables."""
    return " ".join(flags(value))


def discover(compiler):
    """Return the include-roots that supply linux/ and asm/ for this compiler.

    Only directories the compiler itself searches are considered, so a root it
    does not use cannot mask a broken toolchain. Both markers are collected even
    when they live in different directories; the result is in the compiler's own
    order, which is what makes a musl header of the same name win.
    """
    result = subprocess.run([compiler, "-E", "-v", "-xc", os.devnull], text=True,
                            capture_output=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if result.returncode != 0:
        raise ValueError("The host compiler failed to report its include search list:\n"
                         + result.stderr[-2000:])
    match = re.search(r"#include <\.\.\.> search starts here:\n(.*?)End of search list\.",
                      result.stderr, re.DOTALL)
    if not match:
        raise ValueError("The host compiler does not report its include search list")
    found = []
    for line in match[1].splitlines():
        path = Path(line.strip())
        if path.is_absolute() and any((path / name).is_dir() for name in MARKERS):
            found.append(str(path))
    return roots(SEPARATOR.join(found))
