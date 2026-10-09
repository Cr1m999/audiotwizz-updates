"""AudioTwizz launcher - the real app logic lives in _audiotwizz_core (a compiled file sitting
next to this one). Generated automatically by publish.py - edit app/local_presence_app.py in the
project and run Publish.bat instead of editing this file.
"""
import glob
import hashlib
import os
import shutil
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_CORE_FILE = '_audiotwizz_core.cp312-win_amd64.pyd'
_CORE_SHA = '146429175f4f9faa512a7361e11360658fb85080c031713894756784cc1cc467'
_CORE_URL = 'https://cr1m999.github.io/audiotwizz-updates/releases/9.2/_audiotwizz_core.cp312-win_amd64.pyd'


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _heal():
    target = os.path.join(_HERE, _CORE_FILE)
    try:                                   # sweep files renamed aside by earlier updates
        for n in os.listdir(_HERE):
            if n.startswith(_CORE_FILE) and n.endswith(".old"):
                try:
                    os.remove(os.path.join(_HERE, n))
                except OSError:
                    pass
    except OSError:
        pass
    if os.path.isfile(target) and _sha(target) == _CORE_SHA:
        return
    data = None
    for staged in glob.glob(os.path.join(_HERE, "*.runtime.new", _CORE_FILE)):
        try:
            if _sha(staged) == _CORE_SHA:      # a failed update already downloaded + verified it
                with open(staged, "rb") as f:
                    data = f.read()
                break
        except OSError:
            pass
    if data is None:
        for attempt in range(3):               # cache-busting query: GitHub Pages can serve a stale copy
            try:
                req = urllib.request.Request(_CORE_URL + "?t=" + str(int(time.time())) + str(attempt),
                                             headers={"User-Agent": "AudioTwizz"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    blob = r.read()
                if hashlib.sha256(blob).hexdigest() == _CORE_SHA:
                    data = blob
                    break
            except Exception:
                pass
            time.sleep(2)
    if data is None:
        return                                  # offline / not deployed yet: run what's there
    tmp = target + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.replace(tmp, target)
    except OSError:                             # still locked by a stray process: rename aside, then swap
        aside = target + "." + str(os.getpid()) + ".old"
        os.replace(target, aside)
        os.replace(tmp, target)
    for d in glob.glob(os.path.join(_HERE, "*.runtime.new")):
        shutil.rmtree(d, ignore_errors=True)


try:
    _heal()
except Exception:
    pass

import _audiotwizz_core as _core

if __name__ == "__main__":
    _core.run_app(os.path.abspath(__file__))
