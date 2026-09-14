"""The systems the mac99 System list offers, and sensible starting points for
each.

A system only seeds a new record and can be changed afterwards; nothing in
``mac99_command.py`` branches on it -- except CPU count, which OpenBIOS mac99
genuinely restricts (see ``System.smp_max``): more than one CPU needs
``via=pmu`` or ``via=pmu-adb`` (hw/ppc/mac_newworld.c, "-smp %u needs
via=pmu or via=pmu-adb"), and Mac OS 9 loses keyboard control under load
with more than one CPU in this tree, a limit the machine itself does not
enforce, so the System is what enforces it here.

**A system never chooses a file.** No field that names a file is ever filled
in by the program, whatever happens to be lying next to the emulator.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class System:
    id: str
    label: str
    ram_mb: int
    smp: int
    smp_max: int          # Mac OS 9 is held at 1: it loses keyboard control
                           # under load with more than one CPU in this tree.
    via: str = "pmu"


# mac OS 9 first because it is the more restricted of the two, and to match
# the order things are usually tried in: classic, then X.
SYSTEMS: dict[str, System] = {
    "macos9": System("macos9", "Mac OS 9.2", 512, smp=1, smp_max=1, via="pmu"),
    "macosx": System("macosx", "Mac OS X 10.4", 1024, smp=4, smp_max=4, via="pmu"),
    "other": System("other", "Other", 512, smp=1, smp_max=4, via="pmu"),
}

DEFAULT_MAC = "00:05:02:12:34:56"


def system_ids() -> list[str]:
    return list(SYSTEMS)


def system_labels() -> list[str]:
    return [s.label for s in SYSTEMS.values()]


def system_by_label(label: str) -> System:
    for s in SYSTEMS.values():
        if s.label == label:
            return s
    return SYSTEMS["other"]


def normalise_system_id(system_id: str | None) -> str:
    return system_id if system_id in SYSTEMS else "other"
