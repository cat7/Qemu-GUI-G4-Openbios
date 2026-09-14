# HANDOFF: mac99 openbios GUI (paused mid-session)

Paused on request at 2026-09-14, resume after 11:10 local. Branch `mac99`,
last commit `bade6a1` in this tree. Read this before doing anything else on
the mac99 GUI task; do not trust a compacted summary over this file.

## Task in one line

Add a second machine family ("Qemu-system-ppc Mac99 openbios GUI") to this
same Qemu-GUI tree, modelled on the existing g3beige launcher, and produce a
universal-binary distribution in
`/Users/hsp/src/_ppc_g4_mac99_openbios_for_emaculation`. A SEPARATE Claude
session (not this one) owns building qemu-system-ppc itself (both arches),
Libs_arm64/, Libs_x86_64/, pc-bios/, the ATI Rage 128 Pro ROM copy, and the
final zip in that target folder. This session's job is the GUI code, its
tests, the .app bundle (PyInstaller), the Machines/ records, and
Readme-ppc-mac99.txt -- written with distinct filenames so nothing collides
with that other session's output.

## Git state -- READ CAREFULLY, there is an open discrepancy

- Branch `mac99` off `main` at `fdecf2f` (the tip of the g3beige-era GUI at
  session start).
- At pause time, `git status` showed FOUR tracked files modified
  (`qemugui/command.py`, `qemugui/model.py`, `qemugui/paths.py`,
  `qemugui/systems.py`) plus SIX new untracked files
  (`qemugui/mac99_command.py`, `qemugui/mac99_model.py`,
  `qemugui/mac99_systems.py`, `tests/test_mac99_command.py`,
  `tests/fixtures/mac99-os9.json`, `tests/fixtures/mac99-osx.json`).
- I did not author any of this -- this session's own work never got past
  research (see below). It was already sitting in the working tree when I
  went to check it, mid-pause.
- I ran `git add` on all ten paths, intending to commit a wip snapshot so
  nothing was lost. The commit (`bade6a1`, "wip: mac99 GUI module snapshot
  at session pause") only picked up the SIX new files (1395 lines). By the
  time `git add`/`git commit` ran, the four modified tracked files
  (command.py/model.py/paths.py/systems.py) no longer differed from HEAD --
  their edits had disappeared from the working tree in the few seconds
  between my first `git status` and the commit.
- I did not revert them myself (no checkout/reset/stash was run by me
  before they vanished). `git reflog` and `git fsck --dangling` show no
  trace of those edits anywhere reachable or dangling in this repo -- the
  only dangling objects found are unrelated stash artifacts dated
  2026-09-06. The most likely explanation is a concurrent process (the
  other Claude session working the same overall task, or one of its own
  subagents) was also editing this exact shared working tree and reverted
  its own in-progress edits to those four files at that moment.
- **Before writing a single line in this tree on resume: confirm with the
  other session (or the user) whether command.py/model.py/paths.py/
  systems.py were meant to change for mac99 integration, and if so, get
  that content again -- it is not recoverable from this repo's history.**
  Do not assume the six committed mac99_*.py/json files are correct or
  complete without reviewing them fresh; their provenance (whether this
  session's own research fork wrote them despite being told read-only-only,
  or another session did) is unconfirmed.
- Working tree is otherwise clean as of `bade6a1`.

## Research completed and CONFIRMED (evidence-backed, trust these)

Source tree probed: `/Users/hsp/src/claude-code/qemu-ppc-smp`, branch
`smp-audio-usb`. **Actual HEAD at probe time was `4987ce252f`, not
`76f2383475`** as given in the original task brief -- flag this to the user,
branch may have moved.

1. **`via=` has three values, not two**: `cuda` (default), `pmu`,
   `pmu-adb` -- `hw/ppc/mac_newworld.c:708-719` (`core99_set_via_config`).
   Default is `cuda`, NOT `pmu`. Hard gate at `mac_newworld.c:416-421`:
   `-smp >1` requires `via=pmu` or `via=pmu-adb`, else QEMU exits with an
   error. **The GUI must force/validate via=pmu whenever CPU count > 1**,
   and the Mac OS 9 profile (locked to -smp 1 per the task brief) may use
   any via, but OS X profiles needing multi-CPU must not offer via=cuda.
2. **Memory cap**: hard error above 2 GiB RAM (`mac_newworld.c:240-243`).
   1024 MiB (OS X) and 512 MiB (OS 9) are both fine.
3. **Max CPUs = 4** (`KEYLARGO_MAX_CPU`, `include/hw/ppc/openpic.h:35`,
   enforced again in `hw/intc/openpic.c:1612-1614`).
4. **Storage**: `MAX_IDE_BUS=2` x `MAX_IDE_DEVS=2` = 4 slots
   (`mac_newworld.c:81`, `include/hw/ide/ide-dev.h:33`). Index 0/1 =
   controller-0 master/slave, index 2/3 = controller-1 master/slave
   (`mac_newworld.c:493-500`, comment notes only 2 of 3 IDE controllers are
   emulated). **No SCSI/mesh device anywhere in mac_newworld.c** (full-file
   grep, zero hits) -- remove the SCSI tab entirely for mac99.
5. **Floppy: NOT supported, confirmed.** `hw/ppc/mac_newworld.c:300-302`:
   > /* We consider that NewWorld PowerMac never have any floppy drive
   >  * For now, OHW cannot boot from the network. */
   No swim3/floppy/fdc instantiation anywhere in the file. The generic
   `floppy`/`isa-fdc` QEMU devices exist but need an ISA bus mac99 doesn't
   have. **Remove the floppy tab/option entirely for mac99.**
6. **NVRAM: NOT persisted to a file for mac99 -- volatile only.**
   `mac_newworld.c:549-561` creates the nvram device via plain
   `qdev_new(TYPE_MACIO_NVRAM)` with only `size`/`it_shift` set, no
   `drive_get()` call. Contrast g3beige/OldWorld
   (`hw/ppc/mac_oldworld.c:238-242`), which explicitly wires a drive via
   `drive_get(IF_MTD, 0, 0)` + `qdev_prop_set_drive`. The `macio-nvram` QOM
   type itself supports a `drive=` property
   (`hw/nvram/mac_nvram.c:140`, `DEFINE_PROP_DRIVE`), mac_newworld.c just
   never sets it. **Do not offer a "zap NVRAM/PRAM" button for mac99** --
   there is nothing on disk to zap in this tree as it stands. (A QEMU
   source patch could add persistence like oldworld has, but that's a QEMU
   change, out of scope for a GUI-only launcher.)
7. **Default NIC is `sungem`** (`mac_newworld.c:679`,
   `mc->default_nic = "sungem"`). `rtl8139` is also available generically.
   Do NOT offer `bmac` (g3beige/OldWorld only, not in mac99's device set).
8. **Graphics**: no default/automatic VGA device instantiated by
   mac_newworld.c when `-vga none` is passed; graphics is 100% opt-in via
   `-device ati-rage128-pro` or `-device ati-vga`. No `mach64` device
   exists for this machine at all. Keep the launch line's `-vga none` +
   explicit PCI GPU device model; do not add a "second GPU" option (that
   was g3beige-only).
9. **`ati-rage128-pro,help` properties**: `acpi-index`, `addr`,
   `async-engine=<OnOffAuto>` (default `auto`), `busnr`, `failover_pair_id`,
   `fillwatch`/`fillwatch-size`, `monitor-connected` (default on),
   `multifunction`, `refresh_rate`, `rombar`, `romfile`, `romsize`,
   `silent-regs`, `sriov-pf`, several `x-pcie-*` tuning bools,
   `xmax`/`xres`/`ymax`/`yres`. `romfile` is how the AGP ROM is attached.
10. **`-global` properties confirmed real**:
    `screamer.audiodev` (generic `DEFINE_AUDIO_PROPERTIES` macro,
    `hw/audio/screamer.c:681`) and `adb-mouse.extended-protocol`
    (`hw/input/adb-mouse.c:539`, `DEFINE_PROP_BOOL`) -- but the latter is
    **only meaningful when `adb-mouse` is actually instantiated**, i.e.
    `via=cuda` or `via=pmu-adb`. Under `via=pmu` (USB HID mouse, the
    reference launch line's choice) this global is a silent no-op --
    **only emit it when via != pmu**.
11. **Audio backends on this host** (`-audiodev help`): `none, coreaudio,
    dbus, sdl, wav` -- no pulseaudio/alsa (macOS). `usb-audio` device is
    also present in `-device help`.
12. **`-device help` confirmed present**: `ati-rage128-pro`, `ati-vga`,
    `usb-storage`, `usb-tablet`, `usb-audio`, `sungem`, `rtl8139`,
    `ide-cd`/`ide-hd`/`ide-cf`. No `mach64`. SCSI HBA devices exist
    generically in QEMU but mac99 wires none of them up itself.
13. **OpenBIOS prom-env keys confirmed real** in this tree's
    `roms/openbios/`: `boot-device`
    (`arch/ppc/qemu/init.c:1239-1284`), `auto-boot?` and `boot-command`
    (`forth/system/main.fs:55-56`), `vga-ndrv?` (`drivers/vga.fs:268`).
    `boot-args` also exists (`forth/admin/nvram.fs:373`) but only as an
    NVRAM config default, not read directly in the qemu arch-init path --
    treat as supported but lower-confidence.
14. **`-M mac99,help`** only lists generic machine props (memory, smp,
    boot, usb, dtb); `via` is a custom string property not enumerated by
    `,help` -- confirmed only via source (#1 above).

## Existing distribution folder inventory (BEFORE the user emptied it)

`/Users/hsp/src/_ppc_g4_mac99_openbios_for_emaculation` originally held a
copy of the G3 distribution (Libs_arm64/, Libs_x86_64/, Machines/ with 6 G3
machine folders + settings.json, "Qemu-system-ppc GUI.app", RunMeFirst.app,
a G3 zip, Readme-ppc-g3.txt, top-level qemu-img/qemu-system-ppc). **The user
has since EMPTIED this folder entirely** -- nothing to preserve, write
straight into it.

Example `machine.json` schema (from the old `Machines/OSX 10.1/`, for
reference on shape only -- the mac99 module needs its own schema variant per
the differences above, especially: no `scsi`, no `floppy`, no
`onboard_romfile`/`second_gpu` (replace with a single `gpu_romfile` for
ati-rage128-pro), `via` field, and no nvram-zap capability):
```json
{
  "schema": 1, "name": "OSX 10.1", "system": "macosx_10_0_to_10_2",
  "machine": "g3beige", "ram_mb": 1024,
  "rom": "/Applications/.../PowerMacG3v3.ROM",
  "display": "cocoa", "audio": "default",
  "onboard_romfile": "/Applications/.../ati_mach_gt.rom",
  "second_gpu": null,
  "network": {"mode": "vmnet-bridged", "mac": "00:05:02:12:34:56", "ifname": "en0"},
  "governor": {"mode": "off"},
  "ata": [{"kind": "disk", "file": "...", "format": "raw"}, null, null, null],
  "scsi": [], "floppy": null, "extra_args": "", "notes": ""
}
```
Each machine folder also holds a GENERATED `.command` launcher, `nvram.img`
(N/A for mac99, see finding #6), `pram.img` (N/A), `last-run.log`.
`Machines/settings.json` = `{"last_machine": "<name>"}`.

## Build-agent coordination (the OTHER session's lane, not mine)

The other session's planned output filenames in the target folder, to
reference (not create) from the Readme/machine records:
`qemu-system-ppc` (universal, at folder root), `Libs_x86_64/`,
`Libs_arm64/`, `pc-bios/` (OpenBIOS -- launchers use `-L ./pc-bios` relative
to the distribution folder), `ati_rage128pro_136_agp.rom` (copied from
`/Applications/qemu-system-ppc-smp-usb-rage-openbios-based/`), and a zip
named after the folder. Generate all launcher paths relative to the
distribution folder, exactly like the G3 GUI does.

## Incident: a rogue qemu boot, resolved

One of this session's own research forks (tasked with read-only `-M
mac99,help`/`-device help`-style probing, explicitly told not to boot any
guest image) launched a real boot instead: pid 62814,
`-M mac99,via=pmu -smp 4 -display sdl`, real disk
`/Volumes/Macdata/qemu/hd/10.4-dev.qcow2`, a Chessmaster ISO, PM4/3D
traces, netdev hostfwd -- a command pattern that matches the unrelated,
already-shelved GPU-offload/Quartz-Extreme investigation, apparently pulled
from inherited memory context rather than anything in its actual brief. It
was orphaned (parent fork already exited) but still running. Stopped
cleanly via its QMP socket (`quit`, clean SHUTDOWN event) before this pause;
`ps` confirms it is gone. Not inspected further: whether it wrote anything
to `/Volumes/Macdata/qemu/hd/10.4-dev.qcow2` beyond its own run -- worth the
user checking that disk if it matters. Lesson for resume: do not give any
future fork inherited-memory context plus qemu-binary access without an
explicit, repeated "no boot" guard, and verify after each fork completes
that no process it spawned is still alive.

## What this session had NOT yet done at pause

- Never got a completed report back from either of its own two research
  forks: (a) full Qemu-GUI codebase architecture (command.py's exact
  command-line-building logic, the machine.json-to-.command generation
  path, PyInstaller spec structure, test harness/venv details) or (b) the
  g3beige-docs branch's universal build recipe (doc/build/, for how the
  .app itself -- not the qemu binary -- gets bundled with PyInstaller,
  which this session still owns). **Both need to be redone or resumed on
  restart** -- do not assume they finished; TaskStop was issued against
  both at pause time but neither could be confirmed stopped (returned
  "owned by <agent>, cannot stop" -- ownership model quirk, not proof they
  kept running).
- Had not written or reviewed any GUI code of its own.
- Had not run any tests.
- Had not touched PyInstaller/the .app bundle, Machines/ records, or
  Readme-ppc-mac99.txt.

## Exact next action on resume

1. Ask the other session (or the user) to reconcile the vanished
   command.py/model.py/paths.py/systems.py edits described above --
   don't start new integration work in those files until that's settled,
   to avoid clobbering whatever the other session intended.
2. Review the six files in commit `bade6a1`
   (`qemugui/mac99_command.py`, `qemugui/mac99_model.py`,
   `qemugui/mac99_systems.py`, `tests/test_mac99_command.py`,
   `tests/fixtures/mac99-os9.json`, `tests/fixtures/mac99-osx.json`) against
   the confirmed findings above (especially: via=pmu forced when smp>1, no
   floppy/scsi/nvram-zap, sungem default NIC, adb-mouse global gated on
   via!=pmu) before trusting or building on them.
3. Re-run the architecture-study and build-recipe research passes that
   never confirmed completion (see above), or do that reading directly
   rather than forking again, since forking already caused one incident
   this session.
4. Continue with: PyInstaller app bundle, Machines/ records (empty drive
   paths, two profiles: Mac OS 9.2 at -smp 1/-m 512, Mac OS X 10.4 at up to
   -smp 4/-m 1024), Readme-ppc-mac99.txt, tests all passing headless, one
   disk-less `-display none` + QMP smoke start using
   `/Users/hsp/src/claude-code/qemu-ppc-smp/build-smp/qemu-system-ppc`
   directly (not the not-yet-built distribution binary), coordinate with
   the other session before finalizing the Readme/zip contents in case its
   output filenames changed.
