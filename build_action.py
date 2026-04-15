import PyInstaller.__main__
import customtkinter
import os
import sys

custom_tkinter_path = os.path.dirname(customtkinter.__file__)

# Fix separator depending on OS (Windows is ;, Unix/Mac is :)
separator = ';' if sys.platform.startswith('win') else ':'

PyInstaller.__main__.run([
    'desktop_app.py',
    '--onefile',
    '--windowed',
    '--name=MediaGrab',
    f'--add-data={custom_tkinter_path}{separator}customtkinter'
])
