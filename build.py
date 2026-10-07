"""
Build ROBOO.exe for sharing:   python build.py

Produces dist/ROBOO/ROBOO.exe (plus its support files) and dist/ROBOO-Windows.zip.
The person you share it with unzips it and double-clicks ROBOO.exe - no Python
needed. Each user enters their own API key on first launch.
"""
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__
import speech_recognition

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
SR = Path(speech_recognition.__file__).parent

# Big libraries that happen to be installed but ROBOO never uses. PyInstaller
# follows optional imports into them (pyautogui -> cv2, etc.) and the bundle
# balloons from ~100 MB to 850 MB, so they are cut out explicitly.
EXCLUDE = ["torch", "torchvision", "torchaudio", "cv2", "scipy", "transformers",
           "matplotlib", "huggingface_hub", "hf_xet", "tokenizers", "sklearn", "pandas",
           "IPython", "jupyter", "notebook", "sympy", "onnxruntime", "tensorflow",
           "keras", "pocketsphinx", "PyQt5", "PyQt6", "PySide2", "PySide6", "tkinter",
           "test"]

PyInstaller.__main__.run([
    str(HERE / "main.py"),
    "--name=ROBOO",
    "--windowed",                                   # no console window
    "--noconfirm", "--clean",
    f"--icon={HERE / 'assets' / 'roboo.ico'}",
    f"--add-data={HERE / 'web'};web",               # the HUD
    # Google recognition needs only the FLAC encoder, not the offline models.
    f"--add-binary={SR / 'flac-win32.exe'};speech_recognition",
    *[f"--exclude-module={m}" for m in EXCLUDE],
    "--collect-submodules=pycaw",
    "--collect-submodules=comtypes",
    "--hidden-import=webview.platforms.edgechromium",
    "--hidden-import=webview.platforms.winforms",
    f"--distpath={DIST}",
    f"--workpath={HERE / 'build'}",
    f"--specpath={HERE / 'build'}",
])

app = DIST / "ROBOO"
shutil.copy2(HERE / "assets" / "roboo.ico", app / "roboo.ico")
(app / "README.txt").write_text(
    "ROBOO - holographic AI assistant\r\n\r\n"
    "1. Double-click ROBOO.exe\r\n"
    "2. Give it a nickname, paste any AI API key (Gemini keys are free at aistudio.google.com)\r\n"
    "3. Press the mic (or Ctrl+Space) and talk.\r\n\r\n"
    "If Windows shows 'Windows protected your PC', click More info -> Run anyway\r\n"
    "(the app is not code-signed).\r\n",
    encoding="utf-8")
zip_path = shutil.make_archive(str(DIST / "ROBOO-Windows"), "zip", DIST, "ROBOO")
print(f"\nBuilt {app / 'ROBOO.exe'}\nZip   {zip_path}  ({Path(zip_path).stat().st_size / 1e6:.0f} MB)")
sys.exit(0)
