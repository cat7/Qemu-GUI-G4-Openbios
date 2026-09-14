# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Qemu-system-ppc Mac99 openbios GUI.
#
# Build with the python.org universal2 framework Python, which has a
# working tkinter on both arches --
#
#   /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
#       -m PyInstaller --noconfirm Mac99GUI.spec
#
# Put the result ("dist/Qemu-system-ppc Mac99 openbios GUI.app") into the
# distribution folder that holds the universal qemu-system-ppc binary and
# pc-bios/. paths.resolve_install_dir walks up out of the .app to find that
# folder; Machines/ stays beside the application, never inside the
# (read-only) bundle.

import sys

TARGET_ARCH = 'universal2' if sys.platform == 'darwin' else None

a = Analysis(
    ['mac99_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.filedialog',
                   'tkinter.messagebox', 'tkinter.simpledialog'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Qemu-system-ppc Mac99 openbios GUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Qemu-system-ppc Mac99 openbios GUI',
)
app = BUNDLE(
    coll,
    name='Qemu-system-ppc Mac99 openbios GUI.app',
    icon=None,
    bundle_identifier='org.cat7.qemu-gui-mac99',
    info_plist={
        'CFBundleName': 'Qemu-system-ppc Mac99 openbios GUI',
        'CFBundleDisplayName': 'Qemu-system-ppc Mac99 openbios GUI',
        'CFBundleShortVersionString': '1.0',
        'CFBundleVersion': '1.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.13',
        'LSApplicationCategoryType': 'public.app-category.utilities',
    },
)
