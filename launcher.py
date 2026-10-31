"""One-click setup; no administrator privileges or account keys required."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if sys.version_info < (3, 10):
    raise SystemExit('Please install Python 3.11 or newer from https://www.python.org/downloads/')
runtime = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
if not runtime.exists():
    print('First setup: creating local Python environment...', flush=True)
    subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
installed = subprocess.run([str(runtime), '-c', "import faster_whisper, yt_dlp, av; assert av.__version__ == '16.1.0'"], capture_output=True)
if installed.returncode:
    print('Installing local transcription and link components. This may take several minutes...', flush=True)
    result = subprocess.run([str(runtime), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')])
    if result.returncode:
        print('Dependency installation failed. Library and subtitle import remain available. Restart later to retry.', flush=True)
port = os.environ.get('LISTEN_LAB_PORT', '8765')
url = 'http://127.0.0.1:' + port
env = os.environ.copy()
env.setdefault('HF_HOME', str(ROOT / 'models'))
try:
    response = urllib.request.urlopen(url + '/api/health', timeout=1)
    if response.status == 200:
        print('A local service already uses this port. Opening it; close the previous server to switch libraries.', flush=True)
        webbrowser.open(url)
        raise SystemExit(0)
except OSError:
    pass

def show_when_ready():
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + '/api/health', timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.5)

threading.Thread(target=show_when_ready, daemon=True).start()
print('Starting Listen Lab. Keep this window open while learning.', flush=True)
try:
    subprocess.run([str(runtime), str(ROOT / 'server.py')], env=env, check=True)
except KeyboardInterrupt:
    pass
