"""Keep frozen PySide6 and Shiboken DLL directories available on Windows."""

import os
import sys


if sys.platform == "win32" and getattr(sys, "frozen", False):
    dll_handles = []
    for directory_name in ("PySide6", "shiboken6"):
        directory = os.path.join(sys._MEIPASS, directory_name)
        if os.path.isdir(directory):
            dll_handles.append(os.add_dll_directory(directory))
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
