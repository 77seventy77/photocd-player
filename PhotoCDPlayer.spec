# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['photocd_decoder.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('assets', 'assets'),
        ('photocd_fs_reader.py', '.'),
        ('photocd_disc_map.py', '.'),
        ('photocd_hires.py', '.'),
        ('photocd_playlist_parse.py', '.'),
    ],
    hiddenimports=[
        'PIL', 'PIL.Image', 'PIL.ImageTk',
        'numpy',
        'pygame',
        'cairosvg',
        'cssselect2',
        'tinycss2',
        'cairocffi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Photo CD Player',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Photo CD Player',
)

app = BUNDLE(
    coll,
    name='Photo CD Player.app',
    icon='App Icon/PhotoCDLogo.icns',
    bundle_identifier='com.photocd.player',
    version='1.01',
    info_plist={
        'NSHighResolutionCapable': True,
        'CFBundleShortVersionString': '1.01',
        'CFBundleVersion': '1.01',
        'NSHumanReadableCopyright': 'Photo CD Player v1.01',
        'LSMinimumSystemVersion': '12.0',
    },
)
