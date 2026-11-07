#!/usr/bin/env python3
"""PostgreSQL launcher generation and raw ELF relocation."""
from pathlib import Path
import shutil

LOADER = "lib/musl/ld-musl-x86_64.so.1"
LIBRARY_PATH = "lib:lib/musl"


def commands(bundle_root: Path) -> list[str]:
    """Return sorted basenames of raw ELFs found directly under bundle_root/bin."""
    bin_dir = Path(bundle_root) / "bin"
    if not bin_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in bin_dir.iterdir()
        if p.is_file() and not p.is_symlink() and p.read_bytes()[:4] == b"\x7fELF"
    )


def render(command: str) -> str:
    """Render the launcher shell script for the given command name."""
    template_path = Path(__file__).resolve().parents[1] / "packaging/launcher.in"
    return template_path.read_text().replace("@COMMAND@", command)


def install(bundle_root: Path) -> list[str]:
    """Move raw ELFs from bin/ to libexec/bin/ and replace them with shell launchers.

    Refuses to run twice by raising ValueError if libexec/bin already exists.
    Returns the sorted list of command names installed.
    """
    bundle_root = Path(bundle_root)
    libexec_bin = bundle_root / "libexec/bin"
    if libexec_bin.exists():
        raise ValueError(f"libexec/bin already exists in {bundle_root}")

    cmds = commands(bundle_root)
    libexec_bin.mkdir(parents=True, exist_ok=False)

    bin_dir = bundle_root / "bin"
    for cmd in cmds:
        src = bin_dir / cmd
        dst = libexec_bin / cmd
        shutil.move(src, dst)
        src.write_text(render(cmd))
        src.chmod(0o755)

    return cmds
