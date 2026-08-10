import subprocess
import sys
import platform
from pathlib import Path

if sys.version_info < (3, 10):
    sys.stderr.write(
        "ERROR: MARK L requires Python 3.10 or newer.\n"
        f"Current interpreter: {sys.version}\n"
        "Please install Python 3.10+ before running setup.py\n"
    )
    sys.exit(1)

print("Upgrading pip, setuptools, and wheel...")
subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], check=True)

print("Installing requirements...")
subprocess.run([sys.executable, "-m", "pip", "install", "--prefer-binary", "-r", "requirements.txt"], check=True)

print("Installing Playwright browsers...")
subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

if platform.system() == "Windows":
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        postinstall = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
        print(
            "\n⚠️  pywin32 did not install correctly — desktop shortcut creation "
            "will fall back to a slower method that may not work on this machine.\n"
            "    Try fixing it manually with:\n"
            f'    "{sys.executable}" -m pip install --force-reinstall pywin32\n'
            f'    "{sys.executable}" "{postinstall}" -install\n'
        )

print("\n✅ Setup complete! Run 'python main.py' to start MARK L.")

