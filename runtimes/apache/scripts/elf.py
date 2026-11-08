#!/usr/bin/env python3
"""Remove RPATH/RUNPATH from an ELF64 little-endian file without external tools.

The repository ships no absolute RPATH or RUNPATH: resolution is the launcher's
job, and a build prefix baked into a vendored library would break relocation.
patchelf/chrpath are not build-host requirements, so the one thing we need is
done here.

Removing the string is not enough — an empty path entry makes the loader search
the current directory — so the whole dynamic entry is dropped and the remaining
entries are shifted down and re-terminated with DT_NULL. Unknown or absent
rpath tags are ignored by both musl and glibc, so this leaves no search path at
all.

`set_interpreter` rewrites PT_INTERP in place for the same reason: the compiler
has to name a loader that exists while the build runs, which is the toolchain in
the build workspace, but the bundle must name the portable `/lib/...` path.
"""
import struct

EHDR = struct.Struct("<16sHHIQQQIHHHHHH")
PHDR = struct.Struct("<IIQQQQQQ")
DYN = struct.Struct("<qQ")
PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
DT_NULL, DT_RPATH, DT_RUNPATH, DT_STRTAB, DT_NEEDED = 0, 15, 29, 5, 1
ELFCLASS64, ELFDATA2LSB = 2, 1


def parse(header):
    fields = EHDR.unpack(header)
    if fields[0][:4] != b"\x7fELF" or fields[0][4] != ELFCLASS64 or fields[0][5] != ELFDATA2LSB:
        raise ValueError("Not an ELF64 little-endian file")
    return {
        "phoff": fields[5], "phentsize": fields[9], "phnum": fields[10],
    }


def dynamic_range(blob):
    """Return (offset, count) of the dynamic array, or None when there is none."""
    header = parse(blob[:EHDR.size])
    if header["phentsize"] != PHDR.size:
        raise ValueError("Unexpected program header size")
    for index in range(header["phnum"]):
        start = header["phoff"] + index * PHDR.size
        kind, _, offset, _, _, filesize, _, _ = PHDR.unpack_from(blob, start)
        if kind == PT_DYNAMIC:
            if filesize % DYN.size or offset + filesize > len(blob):
                raise ValueError("Malformed dynamic section")
            return offset, filesize // DYN.size
    return None


def entry_count(blob, offset, count):
    for index in range(count):
        tag, _ = DYN.unpack_from(blob, offset + index * DYN.size)
        if tag == DT_NULL:
            return index
    raise ValueError("Unterminated dynamic section")


def strip_rpath(path):
    """Drop RPATH/RUNPATH entries. Returns True when the file was changed."""
    blob = bytearray(path.read_bytes())
    if blob[:4] != b"\x7fELF":
        return False
    found = dynamic_range(blob)
    if found is None:
        return False
    offset, capacity = found
    count = entry_count(blob, offset, capacity)
    entries = [DYN.unpack_from(blob, offset + index * DYN.size) for index in range(count)]
    kept = [entry for entry in entries if entry[0] not in (DT_RPATH, DT_RUNPATH)]
    if len(kept) == len(entries):
        return False
    # Rewrite the array in place, shifted down, and keep the terminator last.
    keep = kept + [(DT_NULL, 0)]
    for index, entry in enumerate(keep):
        DYN.pack_into(blob, offset + index * DYN.size, *entry)
    for index in range(len(keep), count + 1):
        struct.pack_into("<qQ", blob, offset + index * DYN.size, 0, 0)
    path.write_bytes(blob)
    path.chmod(path.stat().st_mode)
    return True


def interpreter(path):
    """Return PT_INTERP as a string, or None when the file does not ask for one."""
    blob = path.read_bytes()
    header = parse(blob[:EHDR.size])
    for index in range(header["phnum"]):
        start = header["phoff"] + index * PHDR.size
        kind, _, offset, _, _, filesize, _, _ = PHDR.unpack_from(blob, start)
        if kind == PT_INTERP:
            if offset + filesize > len(blob):
                raise ValueError("Malformed PT_INTERP")
            return blob[offset:offset + filesize].rstrip(b"\x00").decode("utf-8", "replace")
    return None


def set_interpreter(path, wanted):
    """Rewrite PT_INTERP in place. Returns True when the file was changed.

    The rewrite is length-preserving: the new path is written into the existing
    PT_INTERP bytes and NUL-padded, so no segment moved and no offset shifted. It
    is refused when the new path cannot fit, which means the layout must change —
    a case to fix in the toolchain, not to patch around here.
    """
    blob = bytearray(path.read_bytes())
    if blob[:4] != b"\x7fELF":
        return False
    header = parse(blob[:EHDR.size])
    for index in range(header["phnum"]):
        start = header["phoff"] + index * PHDR.size
        kind, _, offset, _, _, filesize, _, _ = PHDR.unpack_from(blob, start)
        if kind != PT_INTERP:
            continue
        encoded = wanted.encode() + b"\x00"
        if len(encoded) > filesize:
            raise ValueError("%s cannot fit %s" % (path.name, wanted))
        if blob[offset:offset + filesize] == encoded.ljust(filesize, b"\x00"):
            return False
        blob[offset:offset + filesize] = encoded.ljust(filesize, b"\x00")
        path.write_bytes(blob)
        path.chmod(path.stat().st_mode)
        return True
    raise ValueError("No PT_INTERP to rewrite in " + path.name)


def _dynamic_entries(blob):
    found = dynamic_range(blob)
    if found is None:
        return []
    offset, capacity = found
    count = entry_count(blob, offset, capacity)
    return [DYN.unpack_from(blob, offset + index * DYN.size) for index in range(count)]


def _string_table(blob, entries):
    """Map DT_STRTAB (a virtual address) into a file offset."""
    strings = [value for tag, value in entries if tag == DT_STRTAB]
    if not strings:
        raise ValueError("Dynamic section has no string table")
    for kind, _, offset, vaddr, _, filesize, _, _ in _segments(blob):
        if kind == PT_LOAD and vaddr <= strings[0] < vaddr + filesize:
            return offset + (strings[0] - vaddr)
    raise ValueError("Cannot map DT_STRTAB into the file")


def _string(blob, table, offset):
    start = table + offset
    return blob[start:blob.index(b"\x00", start)].decode("utf-8", "replace")


def needed(path):
    """Return the DT_NEEDED sonames an ELF declares, in file order."""
    blob = path.read_bytes()
    entries = _dynamic_entries(blob)
    if not entries:
        return []
    table = _string_table(blob, entries)
    return [_string(blob, table, value) for tag, value in entries if tag == DT_NEEDED]


def rpaths(path):
    """Return the RPATH/RUNPATH strings an ELF actually carries.

    DT_RPATH/DT_RUNPATH hold a string-table offset, so the strings are resolved
    through DT_STRTAB; a file that merely has the numeric offset reported would
    look like a path and hide the real one.
    """
    blob = path.read_bytes()
    entries = _dynamic_entries(blob)
    wanted = [value for tag, value in entries if tag in (DT_RPATH, DT_RUNPATH)]
    if not wanted:
        return []
    table = _string_table(blob, entries)
    return [_string(blob, table, value) for value in wanted]


def _segments(blob):
    header = parse(blob[:EHDR.size])
    return [
        PHDR.unpack_from(blob, header["phoff"] + index * PHDR.size)
        for index in range(header["phnum"])
    ]
