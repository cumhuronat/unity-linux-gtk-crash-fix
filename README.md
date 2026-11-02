# Unity Linux GTK focus fix

A binary patch for **compatible Unity Linux x86-64 Editors**, verified on 6000.6.4f1 and 6000.6.7f1. It fixes a reference-ownership defect in Unity's native `PropertyChangeFilter` callback that can cause crashes during nested window-focus handling.

The patcher updates the specified Editor's **`Unity` executable in place**. `--reverse` restores the exact original bytes. 

## Usage

Requires Python 3.8 or later, GNU binutils (`nm`, `objdump`, `readelf`), the matching `Unity_s.debug` beside the executable, and write access to the installation directory. Close all Editors using that installation before applying or reversing the patch.

```bash
python3 patch.py \
  "$HOME/Unity/Hub/Editor/6000.6.7f1/Editor"
```

Pass the installation directory for the version you want to patch; no version flag is needed. The script validates the callback and patch locations before replacing the installed file atomically. Executable permissions are preserved. Unrecognized code and incorrect patch states (such as applying twice) are rejected without changes.

Launch through Hub as usual, or explicitly with Vulkan:

```bash
"$HOME/Unity/Hub/Editor/6000.6.7f1/Editor/Unity" \
  -projectPath "/absolute/path/to/your/project" \
  -force-vulkan
```

To roll back, close Editors using that installation and run:

```bash
python3 patch.py --reverse \
  "$HOME/Unity/Hub/Editor/6000.6.7f1/Editor"
```

Reversal validates the patched instructions and helper, then reconstructs the original instructions with that build's addresses. No backup file is required. It also requires the matching debug symbols.

## Compatibility checks

The script locates `PropertyChangeFilter`, its globals, and helper padding through the Editor's native symbols. It checks that the executable and symbols have the same build ID, and translates virtual addresses through the ELF load segments.

It validates the entire 410-byte callback against an instruction fingerprint. Only address displacements are excluded from that fingerprint, and each excluded target is checked separately: named functions, imported GTK/GLib functions, named globals and their fields, and the `_NET_ACTIVE_WINDOW` string. Branches, opcodes, and stack accesses must match exactly. The helper must fit in executable alignment padding outside every symbol's occupied range, containing the expected unused bytes. All checks complete before any file is changed.

This supports other builds with the same implementation even when their addresses and overall executable hashes differ. It does **not** promise support for every intervening or future release. Compiler changes, missing symbols, or a Unity fix can cause a refusal; do not bypass the checks. A changed implementation needs a fresh review. The fingerprint verifies this callback's layout and targets, not every function elsewhere in the Editor.

## Root cause

The defect is in Unity's GTK window handling. Switching back to Unity during asset import was the tested reproduction case. Other workflows that trigger the same nested focus callbacks may expose it; those workflows have not been verified.

`gdk_screen_get_active_window()` returns an owned reference. Unity stores that pointer in the global `s_CurrentActiveWindow`, dispatches application focus callbacks, and afterward rereads the global to release the reference.

Those callbacks can pump GTK events and reenter `PropertyChangeFilter`. A nested invocation can replace the global with a different window. The outer invocation then releases the nested invocation's window instead of the reference it acquired:

```cpp
// Original behavior, simplified; null checks omitted.
s_CurrentActiveWindow = get_active_window(); // Acquires reference A.
dispatch_focus_callbacks();                 // Nested invocation can store B.
unref(s_CurrentActiveWindow);               // Releases B again; leaks A.
```

That extra release can free a destroyed `GdkWindow` while GTK's X11 window table still contains it. A later event lookup retrieves the freed pointer and crashes during `GDK_IS_WINDOW` in `gdk_event_source_get_filter_window`.

The patch preserves the acquired reference in the current invocation's stack frame:

```cpp
// Patched behavior, simplified; null checks omitted.
auto* local = get_active_window();
s_CurrentActiveWindow = local;
dispatch_focus_callbacks();
unref(local);
```

Each nested invocation has its own local reference. The global remains available to Unity's existing focus checks. The patch keeps event processing and reference releases intact; it does not suppress the crash handler or disable Vulkan.

GTK's ownership behavior can be checked in [`gdkscreen-x11.c`](https://github.com/GNOME/gtk/blob/3.24.52/gdk/x11/gdkscreen-x11.c) and [`gdkwindow-x11.c`](https://github.com/GNOME/gtk/blob/3.24.52/gdk/x11/gdkwindow-x11.c): an existing window returned by `gdk_x11_window_foreign_new_for_display` receives a reference, and native window creation keeps a reference until destroy notification removes the X11 table entry.

## Binary changes

On the two verified builds, **25 byte values change across three ranges totaling 27 bytes**. These are the discovered file offsets, not hardcoded patch locations or runtime addresses:

| 6000.6.4f1 offset | 6000.6.7f1 offset | Change |
| --- | --- | --- |
| `0x4022211` | `0x4061CC1` | Call the helper instead of only storing the global pointer. |
| `0x40222EF` | `0x4061D9F` | Load the reference to release from the stack instead of the global. |
| `0x4020C81` | `0x4060731` | Add a 13-byte helper in unused alignment padding. |

The callback already reserves an unused eight-byte stack slot. The helper stores the pointer there and also performs the original global store. Its call/return is balanced, the stack canary is preserved, and relative addressing remains compatible with ASLR. No sections or load addresses are changed.
