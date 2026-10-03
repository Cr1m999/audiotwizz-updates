"""AudioTwizz launcher - the real app logic lives in _audiotwizz_core (a compiled file sitting
next to this one). Generated automatically by publish.py - edit app/local_presence_app.py in the
project and run Publish.bat instead of editing this file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _audiotwizz_core as _core

if __name__ == "__main__":
    _core.run_app(os.path.abspath(__file__))
