# gameguard

Client-side neutralization of nProtect **GameGuard**. `npgamedll64_stub.c` is a
drop-in replacement for the SDK shim `NPGameDLL64.dll` that lets the retail client
boot **with no GameGuard running** — no `GameMon.des`, no kernel driver, no
`ROMGoldenAge.ini` signature check. Consistent with the server side: the emulator
never sends `S2C_GameGuardCheck`, so GameGuard gates nothing on the wire, and this
makes the client stop requiring it for its own boot.

This is the counterpart to [`../patchers/gameguard_config.py`](../patchers/gameguard_config.py):
that tool reads/rewrites GameGuard's signed `.ini`; **the stub removes GameGuard
entirely, so the `.ini` is never opened or verified.** Whichever you use, the
`.ini` signature (RSA-2048, private key never ships) is a non-issue with the stub.

## The gate

`NPGameDLL64.dll` is the Unity native plugin the game P/Invokes (names resolved at
runtime from `global-metadata.dat`, 47 exports). `GameGuardUnityManager` (IL2CPP)
treats GameGuard as healthy **iff two calls both return `NPGAMEMON_SUCCESS = 1877`
(0x755)**:

| Export | Must return | Wrong value ⇒ |
|---|---|---|
| `InitNPGameMon()` | **1877** | `Awake` → popup *"GameGuard Init Error N"* → `CloseNPGameMon` (boot aborts) |
| `CheckNPGameMon()` | **1877** | bootstrap → popup *"Initialization failed. Please restart the game. (N)"* (the `(0)` seen = Check returning 0) |
| `PreInitNPGameMonW()` | 1877 | set for consistency |

`CheckNPGameMon` is polled every frame; keep returning 1877. All other exports are
inert no-ops returning 0 — the exact configuration verified to reach the title
screen, byte-for-byte the same UI as a retail (real-GG) boot.

> **Note on the return value.** `125` is GameGuard's *tamper* code — it is what a
> real, unmodified plugin returns when it detects interference (e.g. a forwarding
> proxy) and then refuses to start GameMon. A clean success is `1877`, and the
> **return value is the gate** (not a callback). Earlier work mis-read the proxy's
> `125` as the success value and dead-ended chasing the callback.

## Build (mingw-w64 — cross-compiles on Linux, or native on Windows)

The output is a Windows PE DLL; a Windows-targeting toolchain is required. A
native Linux `gcc` will not work (it emits an ELF `.so` and lacks `windows.h`).

```bash
# Linux (Debian/Ubuntu):  sudo apt install gcc-mingw-w64-x86-64
# Arch: mingw-w64-gcc   Fedora: mingw64-gcc
x86_64-w64-mingw32-gcc -O2 -shared npgamedll64_stub.c -o NPGameDLL64.dll \
    -Wl,--kill-at -static -static-libgcc
```

On Windows the same command works with mingw-w64 on PATH (e.g. WinLibs).
Compiles clean under `-Wall -Wextra`; all 47 GameGuard exports are present.

## Install / restore

```bash
CLIENT=/path/to/ROMGoldenAge/client
PLUG="$CLIENT/ROMGoldenAge_Data/Plugins/x86_64"

cp "$PLUG/NPGameDLL64.dll" "$PLUG/NPGameDLL64.dll.retail"   # back up retail once
cp NPGameDLL64.dll "$PLUG/NPGameDLL64.dll"                  # install the stub
# restore:  cp "$PLUG/NPGameDLL64.dll.retail" "$PLUG/NPGameDLL64.dll"
```

Launch as usual: `ROMGoldenAge.exe -env=Real`.

## Status

- ✅ **Boots to title / server-select with no GameGuard** — pure DLL swap.
- ⬜ **Login → world through the stub** — unverified. The title screen calls
  `SendUserIDToGameMonW` (stub returns 0/false) and login may run a
  `SendCSAuth3ToGameMon` challenge; the emulator never triggers
  `S2C_GameGuardCheck`, so this path should stay dormant, but confirm it against
  the running redirect + emulator. If login blocks, the first suspects are the
  `Send*ToGameMon` bool returns (try 1/TRUE) — change only what a repro shows,
  since all-0 is what cleanly reached the title.

How it was found: with the stub installed (GameGuard absent, so IL2CPP hooks are
safe), a frida agent hooked the managed `GameGuardUnityManager` methods and caught
`Awake` calling `GetInitErrorMsg(<Init return>)`; returning 1877 flipped it to the
success path, and a second run pinned the later popup on `CheckNPGameMon`. The
frida agents live in the `rom-frida` repo (`scripts/watch_gg_manager.js`,
`scripts/diag_bootstrap.js`).
