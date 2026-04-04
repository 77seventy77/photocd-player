"""
PyInstaller runtime hook — make cairocffi find the bundled libcairo.2.dylib.

cairocffi calls ctypes.util.find_library("cairo") at import time.
On a bundled macOS app the system libcairo is not available, so we patch
find_library to return the copy we bundled inside _MEIPASS.
"""
import sys
import os
import ctypes.util

if hasattr(sys, "_MEIPASS"):
    _orig_find_library = ctypes.util.find_library

    def _find_library_patched(name):
        if "cairo" in str(name):
            for candidate in ("libcairo.2.dylib", "libcairo-2.dll", "libcairo.so.2"):
                path = os.path.join(sys._MEIPASS, candidate)
                if os.path.exists(path):
                    return path
        return _orig_find_library(name)

    ctypes.util.find_library = _find_library_patched
