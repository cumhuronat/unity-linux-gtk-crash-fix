#!/usr/bin/env python3
"""Apply or reverse the GTK focus fix in a compatible Unity Linux x86-64 Editor."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

# Fingerprint of the entire 410-byte callback, with address displacements zeroed.
# All other bytes (including branches, stack accesses and opcodes) must match.
FINGERPRINT = "a1c0ab5f59ab4167daba1379499bf61f103e90a1512bd9ef31fac303ecbd4ddb"
CALLS = {
    0x2D: "X11GetAtomName(unsigned long)",
    0x3F: "strcmp@plt",
    0x4C: "gdk_display_get_default@plt",
    0x54: "gdk_display_get_default_screen@plt",
    0x5C: "gdk_screen_get_active_window@plt",
    0x74: "gdk_x11_window_get_xid@plt",
    0x106: "IsApplicationInitialized()",
    0x135: "ApplicationHandleEditorFocusChange(bool)",
    0x13A: "ApplicationUpdateInteractionModeSettings()",
    0x150: "g_type_check_instance_cast@plt",
    0x158: "g_object_unref@plt",
    0x160: "XFree@plt",
    0x195: "__stack_chk_fail@plt",
}
# Displacement offset: (next instruction offset, target symbol, field offset).
REFERENCES = {
    0x64: (0x68, "s_CurrentActiveWindow", 0),
    0x7F: (0x83, "gApplicationWindows", 8),
    0xA5: (0xA9, "gApplicationWindows", 0),
    0xFF: (0x104, "SetApplicationFocus(bool)::applicationInitialized", 0),
    0x111: (0x116, "SetApplicationFocus(bool)::applicationInitialized", 0),
    0x11B: (0x11F, "gApplicationFocused", 0),
    0x131: (0x135, "gApplicationFocused", 0),
    0x142: (0x146, "s_CurrentActiveWindow", 0),
    0x182: (0x187, "SetApplicationFocus(bool)::applicationInitialized", 0),
    0x18D: (0x191, "gApplicationFocused", 0),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("editor", type=Path, help="Editor directory containing Unity and Unity_s.debug")
    parser.add_argument("--reverse", action="store_true", help="Restore the original instructions")
    args = parser.parse_args()
    editor = (args.editor.expanduser() / "Unity").resolve()
    debug = editor.with_name("Unity_s.debug")
    data = bytearray(editor.read_bytes())
    require(data[:7] == b"\x7fELF\x02\x01\x01" and struct.unpack_from("<H", data, 18)[0] == 62,
            "Expected a little-endian Linux x86-64 ELF executable.")
    for tool in ("nm", "objdump", "readelf"):
        require(shutil.which(tool), f"Missing {tool}; install binutils.")
    ids = [re.findall(r"Build ID: ([0-9a-f]+)", command("readelf", "-n", str(p)))
           for p in (editor, debug)]
    require(len(ids[0]) == 1 and ids[0] == ids[1], "Unity_s.debug must match the executable's build ID.")

    # ELF load segments translate virtual addresses to file offsets without assumptions.
    phoff = struct.unpack_from("<Q", data, 32)[0]
    entry_size, count = struct.unpack_from("<HH", data, 54)
    segments = [struct.unpack_from("<IIQQQQQQ", data, phoff + i * entry_size) for i in range(count)]

    def offset(address, size, executable=False):
        for kind, flags, start, virtual, _, length, _, _ in segments:
            if kind == 1 and virtual <= address and address + size <= virtual + length:
                result = start + address - virtual
                require(result + size <= len(data) and (not executable or flags & 1), "Invalid code range.")
                return result
        raise ValueError("Address is outside the executable's file-backed load segments.")

    symbols = [(int(a, 16), int(s, 16), name) for a, s, name in re.findall(
        r"^([0-9a-f]+) ([0-9a-f]+) \w (.+)$",
        command("nm", "-S", "-C", "--defined-only", str(debug)), re.M)]

    def symbol(name):
        matches = [(a, s) for a, s, n in symbols if n == name]
        require(len(matches) == 1, f"Missing or ambiguous symbol: {name}")
        return matches[0]

    imports = {name: int(a, 16) for a, name in re.findall(
        r"^([0-9a-f]+) <([^>]+@plt)>:", command("objdump", "-d", "-j", ".plt", str(editor)), re.M)}
    function, size = symbol("PropertyChangeFilter(void*, _GdkEvent*, void*)")
    require(size == 0x19A, "Unrecognized callback size; refusing to patch.")
    start = offset(function, size, True)
    window, _ = symbol("s_CurrentActiveWindow")
    helper, helper_size = symbol("GetGdkWindowsInReverseOrder(core::vector<_GdkWindow*, core::allocator<_GdkWindow*, 0ul> >&)")
    helper += helper_size
    cave = offset(helper, 13, True)
    require(not any(helper <= a < helper + 13 or a < helper < a + s for a, s, _ in symbols),
            "Helper padding overlaps a symbol.")

    def relative(target, next_address):
        return struct.pack("<i", target - next_address)

    # Each edit: file offset, original bytes, patched bytes.
    edits = [
        (start + 0x61, b"\x48\x89\x05" + relative(window, function + 0x68),
         b"\xe8" + relative(helper, function + 0x66) + b"\x90\x90"),
        (start + 0x13F, b"\x48\x8b\x3d" + relative(window, function + 0x146), b"\x48\x8b\x3c\x24\x90\x90\x90"),
        (cave, b"\xcc" * 13, b"\x48\x89\x05" + relative(window, helper + 7) + b"\x48\x89\x44\x24\x08\xc3"),
    ]
    body = data[start:start + size]
    for position, original, patched in edits:
        expected = patched if args.reverse else original
        require(data[position:position + len(expected)] == expected,
                "Unexpected instructions or patch state; refusing to change the executable.")
        if start <= position < start + size:
            body[position - start:position - start + len(original)] = original

    # Validate each relocated target before excluding its displacement from the fingerprint.
    def target(displacement, next_instruction):
        return function + next_instruction + struct.unpack_from("<i", body, displacement)[0]

    for position, name in CALLS.items():
        expected = imports.get(name) if name.endswith("@plt") else symbol(name)[0]
        require(target(position + 1, position + 5) == expected, f"Unexpected call: {name}")
    for position, (next_instruction, name, field) in REFERENCES.items():
        require(target(position, next_instruction) == symbol(name)[0] + field, f"Unexpected reference: {name}")
    atom = offset(target(0x38, 0x3C), 19)
    require(data[atom:atom + 19] == b"_NET_ACTIVE_WINDOW\0", "Unexpected focus event atom.")
    for position in [p + 1 for p in CALLS] + list(REFERENCES) + [0x38]:
        body[position:position + 4] = b"\0" * 4
    require(hashlib.sha256(body).hexdigest() == FINGERPRINT, "Unrecognized callback instructions; refusing to patch.")
    for position, original, patched in edits:
        replacement = original if args.reverse else patched
        data[position:position + len(replacement)] = replacement

    # Atomic replacement preserves permissions and avoids partial executable writes.
    with tempfile.NamedTemporaryFile(dir=editor.parent, prefix=".unity-patch-", delete=False) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            shutil.copystat(editor, temporary)
            os.replace(temporary, editor)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"{'Restored' if args.reverse else 'Patched'} Editor: {editor}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, struct.error, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Patch failed: {error}")
