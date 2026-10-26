"""Dependency-free inspection of deployable Linux ELF images."""

import re
import struct
from dataclasses import dataclass


def normalize_architecture(value: str) -> str:
    value = value.lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(value, value)


@dataclass(frozen=True)
class ElfInfo:
    architecture: str
    bits: int
    endianness: str
    linkage: str
    interpreter: str | None
    needed: tuple[str, ...]
    minimum_glibc: str | None


def inspect_elf(image: bytes) -> ElfInfo:
    """Read headers and dynamic dependencies without executing the image."""
    try:
        if image[:7] != b"\x7fELF\x02\x01\x01" or len(image) < 64:
            raise ValueError("expected a 64-bit little-endian ELF image")
        if image[7] not in (0, 3):
            raise ValueError("ELF OS ABI is not compatible with Linux")
        kind, machine = struct.unpack_from("<HH", image, 16)
        architecture = {62: "x86_64", 183: "aarch64"}.get(machine)
        if architecture is None or kind not in (2, 3):
            raise ValueError("unsupported ELF architecture or executable type")
        entry = struct.unpack_from("<Q", image, 24)[0]
        if not entry:
            raise ValueError("ELF has no executable entry point")
        offset = struct.unpack_from("<Q", image, 32)[0]
        size, count = struct.unpack_from("<HH", image, 54)
        if size < 56 or not count or offset + size * count > len(image):
            raise ValueError("invalid ELF program headers")
        loads = []
        dynamic = None
        interpreter = None
        executable_entry = False
        for index in range(count):
            ptype, flags, start, address, _, length, memory_length, _ = struct.unpack_from(
                "<IIQQQQQQ", image, offset + index * size
            )
            if start + length > len(image):
                raise ValueError("ELF segment outside image")
            if ptype == 1:
                loads.append((address, start, length))
                if flags & 1 and address <= entry < address + memory_length:
                    executable_entry = True
            elif ptype == 2:
                dynamic = (start, length)
            elif ptype == 3:
                value = image[start : start + length]
                if not value.endswith(b"\0") or b"\0" in value[:-1]:
                    raise ValueError("invalid ELF interpreter")
                interpreter = value[:-1].decode("utf-8")
                if not interpreter:
                    raise ValueError("empty ELF interpreter")
        if not executable_entry:
            raise ValueError("ELF entry point is not in an executable load segment")
        needed_offsets = []
        string_address = None
        string_size = None
        if dynamic:
            start, length = dynamic
            if length % 16:
                raise ValueError("invalid ELF dynamic segment")
            for position in range(start, start + length, 16):
                tag, value = struct.unpack_from("<qQ", image, position)
                if tag == 0:
                    break
                if tag == 1:
                    needed_offsets.append(value)
                elif tag == 5:
                    string_address = value
                elif tag == 10:
                    string_size = value
        needed = []
        table = b""
        if needed_offsets:
            if string_address is None or string_size is None:
                raise ValueError("missing ELF dynamic string table")
            table = None
            for address, start, length in loads:
                if address <= string_address and string_address + string_size <= address + length:
                    begin = start + string_address - address
                    table = image[begin : begin + string_size]
                    break
            if table is None:
                raise ValueError("invalid ELF dynamic string table")
            for position in needed_offsets:
                end = table.find(b"\0", position)
                if position >= len(table) or end < 0:
                    raise ValueError("invalid ELF dependency name")
                needed.append(table[position:end].decode("utf-8"))
        versions = [
            tuple(map(int, match.split(b".")))
            for match in re.findall(rb"\bGLIBC_([0-9]+(?:\.[0-9]+)+)\x00", table)
        ]
        return ElfInfo(
            architecture=architecture,
            bits=64,
            endianness="little",
            linkage="dynamic" if interpreter or needed else "static",
            interpreter=interpreter,
            needed=tuple(needed),
            minimum_glibc=".".join(map(str, max(versions))) if versions else None,
        )
    except (struct.error, UnicodeError, OverflowError) as error:
        raise ValueError(f"invalid ELF image: {error}") from error
