# HANDOFF: mac99 openbios GUI

Read this before doing anything else on the mac99 GUI task; do not trust a
compacted summary over this file. The pause/resume narrative below (from
the first work session, 2026-09-14 morning) is kept as historical record;
this section is the current state as of the SECOND session (2026-09-14
afternoon), which finished the deliverables.

## STATE OF PLAY (read this, not the log below)

Branch **`mac99-openbios`** (renamed from `mac99` mid-task -- there is a
DIFFERENT, unrelated mac99 tree, `qemu-mac99`, booting a real Apple ROM;
`mac99-openbios` disambiguates which one this GUI targets). Every finding
below came from `/Users/hsp/src/claude-code/qemu-ppc-smp`, branch
`smp-audio-usb`, and its already-built binary
`build-smp/qemu-system-ppc` -- NOT from `qemu-mac99` or
`/Applications/qemu-system-ppc-mac99`.

Done: GUI code (record/command/systems/UI layers, 9 files), 150 headless
tests all passing (venv:
`/Users/hsp/PycharmProjects/QemuGUI-PPC/.venv/bin/python`), two bugs found
and fixed on review (a dead function name in reset_nvram; a missing
nvram.img causing every fresh machine's first Start to fail -- both now
covered by regression tests), a VNC display option added (confirmed
functional by the build session, wired here as `-display none -vnc <spec>`
and unit-tested), the universal (arm64+x86_64) `.app` built with
PyInstaller and copied into the distribution folder, two ready Machines/
records (Mac OS 9.2, Mac OS X 10.4 -- both with the ATI ROM attached, no
disk), Readme-ppc-mac99-openbios.txt written, and the one authorized smoke start
(disk-less, `-display none`, QMP `query-status`/`quit`) run twice against
`build-smp/qemu-system-ppc` (once pre-fix reproducing the nvram bug, once
post-fix confirming it).

Not done by this session, owned by the separate build session: the
universal `qemu-system-ppc`/`qemu-img` binaries, `Libs_arm64/`,
`Libs_x86_64/`, `pc-bios/`, the ATI ROM copy, and the final zip -- as of
last check those were all present in the distribution folder except the
zip. Re-check before telling the user the folder is finished.

Open questions for the user, not resolved by either session: whether Mac OS
9.2 should ship with the GPU attached by default (this session gave it one,
on the reasoning that it is the only graphics option this GUI offers, but
9.x's use of it is untested here); whether VNC-on-mac99 specifically (as
opposed to `-machine none`, which the build session verified) has been
end-to-end verified -- the build session's own resume notes flag this as
still open on their side.

## Addendum: user review round (2026-09-14, same afternoon)

The user reviewed the shipped app and asked for a batch of changes, all
applied on this branch (commits `10de8c7`, `6b7b4d7`, `54cff4d`):

- Empty slate: no pre-made Machines records ship any more; New machine asks
  for a name only (no system-type chooser -- there is no governor on this
  machine). `mac99_systems.py` is gone; `qemugui/mac99_model.py` no longer
  has a `system` field at all.
- Firmware override removed: it only ever emitted `-bios <path>`, an
  advanced escape hatch to run different OpenBIOS/firmware than
  `-L ./pc-bios` supplies -- nothing on this machine needs it, and the
  distribution's own pc-bios/ is fixed, so it was removed rather than kept
  unused.
- USB storage removed from the editor (kept, tested, at the record/command
  layer only) -- candidate, not verified against any guest.
- Two Advanced-tab checkboxes renamed with INVERTED polarity from the
  field they write ("Boot into Open Firmware" == NOT auto-boot?; "Do not
  load vga driver" == NOT vga-ndrv?), and the second now follows the GPU
  checkbox as a starting point.
- Found and documented a real, load-bearing QEMU behavior while answering
  the Reset-NVRAM question: `hw/ppc/mac_newworld.c:561` calls
  `pmac_format_nvram_partition()` UNCONDITIONALLY on every machine start,
  rebuilding the entire NVRAM (both the OpenBIOS variables and the OS X
  half) from that invocation's `-prom-env` flags alone
  (`hw/nvram/chrp_nvram.c:48-86`, itself an unconditional rebuild, no check
  against existing content). So this GUI's own Advanced-tab fields always
  win at Start, and NVRAM only carries anything across a warm Restart
  performed *inside* one continuous QEMU process -- never across quitting
  and starting again. `qemugui/mac99_model.py`'s module docstring and the
  Reset NVRAM dialog text were corrected to say this accurately; see also
  `doc/screenshot-mac99-main.png` for the finished look.
- A non-white `clam` ttk theme (`qemugui/mac99_theme.py`) applied to the
  main window and the editor/dialogs.
- PyInstaller SDK-stamp mismatch (x86_64 slice `LC_VERSION_MIN_MACOSX`
  sdk 15.5, arm64 slice `LC_BUILD_VERSION` sdk 26.2, confirmed via
  `otool -arch <arch> -l`) -- not present in the G3 app built earlier on
  this same machine; traced to this machine's current Xcode SDK (26.2)
  being newer than when that build ran, and PyInstaller's own SDK-rewrite
  step (visible in its build log) evidently only patching one of the two
  load-command styles. Left as-is: `minos`/`LSMinimumSystemVersion` (the
  values macOS actually gates launch on) are correct and consistent on
  both slices; the `sdk` field is informational only.
- Discovered mid-task: a real machine folder named "Tiger" exists in the
  distribution's Machines/ folder, pointed at the user's actual
  `/Volumes/Macdata/qemu/hd/10.4.img` and a real CD image, with a
  `last-run.log` -- the user has already used this app for real. Left
  completely untouched throughout; flagging so nobody mistakes it for
  test debris and deletes it.

---
## LOG (first session, pre-resume -- historical, corrected by the STATE OF PLAY above)
---

## Task in one line

Add a second machine family ("Qemu-system-ppc Mac99 openbios GUI") to this
same Qemu-GUI tree, modelled on the existing g3beige launcher, and produce a
universal-binary distribution in
`/Users/hsp/src/_ppc_g4_mac99_openbios_for_emaculation`. A SEPARATE Claude
session (not this one) owns building qemu-system-ppc itself (both arches),
Libs_arm64/, Libs_x86_64/, pc-bios/, the ATI Rage 128 Pro ROM copy, and the
final zip in that target folder. This session's job is the GUI code, its
tests, the .app bundle (PyInstaller), the Machines/ records, and
Readme-ppc-mac99-openbios.txt -- written with distinct filenames so nothing collides
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

## Addendum, written seconds after the above

While staging the resume-notes commit, a NEW commit appeared on this same
`mac99` branch that I did not make: `8f69f7a "mac99: the pure record and
command layer, headless-tested"`, author `cat7`, touching only
`tests/test_mac99_command.py` (25 insertions/8 deletions), timestamped
2026-09-14 07:51:54, landed on top of my `bade6a1`. This confirms the other
Claude session (or a teammate of it) is actively and concurrently committing
to this exact branch/tree right now, under the same git identity as the
user. That most likely explains the vanished command.py/model.py/paths.py/
systems.py edits above: that session probably reset its own in-progress
edit to those files deliberately, planning to re-land it after this
commit, rather than anything being lost. Treat the "reconcile with the
other session" step below as lower-urgency than it reads above -- it is
plausibly already in hand -- but still confirm before editing those four
files yourself, since two sessions writing the same files in the same
working tree can still collide.

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
  Readme-ppc-mac99-openbios.txt.

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
   -smp 4/-m 1024), Readme-ppc-mac99-openbios.txt, tests all passing headless, one
   disk-less `-display none` + QMP smoke start using
   `/Users/hsp/src/claude-code/qemu-ppc-smp/build-smp/qemu-system-ppc`
   directly (not the not-yet-built distribution binary), coordinate with
   the other session before finalizing the Readme/zip contents in case its
   output filenames changed.
