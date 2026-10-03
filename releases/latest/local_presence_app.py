"""
Local Presence - show what your local music player is playing on Discord,
with the title, artist, album, cover art and progress bar read straight from your own files.

Windows app. Reads the system media controls (SMTC), so any player that
publishes there works (VLC, MusicBee, Windows Media Player, Spotify, browsers,
foobar2000 with the SMTC plugin, ...). Cover art / album / length come from the
tags inside your music files (mp3, flac, m4a, ogg, opus, wma), from the player's
own thumbnail, or - as a last resort - from an online lookup.
Everything is configured inside the app.
"""

import asyncio
import base64
import difflib
import hashlib
import inspect
import io
import json
import math
import os
import platform
import queue
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import tkinter as tk

# ------------------------------------------------------ media helper mode --
# Windows' media API misbehaves on background threads of a GUI program, so the
# reading is done by a second copy of this program (no window) that runs it on
# its own main thread and writes the result to a small file.
HELPER_FLAG = "--smtc-helper"
ONCE_FLAG = "--smtc-once"


def _cfg_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LocalPresence")


def _write_atomic(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _parent_alive(pid):
    if not pid:
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            return False
        alive = k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT
        k32.CloseHandle(h)
        return alive
    except Exception:
        return True


def _thumb_path(app, title, artist, album):
    key = "|".join((app, title, artist, album)).encode("utf-8", "ignore")
    return os.path.join(_cfg_dir(), "thumbs", hashlib.sha1(key).hexdigest()[:20] + ".img")


async def _read_thumbnail(ref):
    """Reads the cover picture a player publishes to Windows' media controls."""
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

    stream = await ref.open_read_async()
    size = int(getattr(stream, "size", 0) or 0)
    cap = size if 0 < size <= 8_000_000 else 4_000_000
    buf = Buffer(cap)
    await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
    reader = DataReader.from_buffer(buf)
    data = bytearray(reader.unconsumed_buffer_length)
    reader.read_bytes(data)
    return bytes(data)


async def _smtc_snapshot(mgr, tcache=None):
    from datetime import datetime, timezone

    if tcache is None:
        tcache = {}
    out = []
    for sess in mgr.get_sessions():
        app = sess.source_app_user_model_id or ""
        try:
            props = await sess.try_get_media_properties_async()
            tl = sess.get_timeline_properties()
            playing = sess.get_playback_info().playback_status == 4  # PLAYING
            pos = tl.position.total_seconds()
            dur = (tl.end_time - tl.start_time).total_seconds()
            if playing:
                try:
                    delta = (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
                    if 0 <= delta <= 3600:
                        pos += delta
                except Exception:
                    pass
            if dur > 0:
                pos = min(pos, dur)
            title, artist, album = props.title or "", props.artist or "", props.album_title or ""

            thumb = ""
            if title and getattr(props, "thumbnail", None) is not None:
                tp = _thumb_path(app, title, artist, album)
                ok, tries, last = tcache.get(tp, (None, 0, 0.0))
                if ok is None or (not ok and tries < 5 and time.time() - last > 4):
                    try:
                        if not os.path.exists(tp):
                            data = await _read_thumbnail(props.thumbnail)
                            if data:
                                os.makedirs(os.path.dirname(tp), exist_ok=True)
                                with open(tp, "wb") as f:
                                    f.write(data)
                    except Exception:
                        pass
                    tcache[tp] = (os.path.exists(tp), tries + 1, time.time())
                if tcache.get(tp, (False,))[0]:
                    thumb = tp

            out.append({
                "app": app,
                "title": title,
                "artist": artist,
                "album": album,
                "pos": pos,
                "dur": dur,
                "playing": playing,
                "thumb": thumb,
            })
        except Exception as e:
            out.append({"app": app, "error": f"{type(e).__name__}: {e}"})
    return out


def _clean_thumbs(max_age=86400):
    d = os.path.join(_cfg_dir(), "thumbs")
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if time.time() - os.path.getmtime(p) > max_age:
                os.remove(p)
    except OSError:
        pass


async def _smtc_helper_loop(parent_pid, path):
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
    )

    _clean_thumbs()
    mgr = await Manager.request_async()
    tcache = {}
    while _parent_alive(parent_pid):
        try:
            payload = {"ts": time.time(), "sessions": await _smtc_snapshot(mgr, tcache), "error": None}
        except Exception as e:
            payload = {"ts": time.time(), "sessions": [], "error": f"{type(e).__name__}: {e}"}
        _write_atomic(path, payload)
        await asyncio.sleep(1)


if __name__ == "__main__" and (HELPER_FLAG in sys.argv or ONCE_FLAG in sys.argv):
    if ONCE_FLAG in sys.argv:
        # Diagnostic: py local_presence_app.py --smtc-once
        async def _once():
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
            )

            mgr = await Manager.request_async()
            print(json.dumps(await _smtc_snapshot(mgr), indent=2))

        asyncio.run(_once())
    else:
        try:
            _parent = int(sys.argv[sys.argv.index(HELPER_FLAG) + 1])
        except Exception:
            _parent = 0
        os.makedirs(_cfg_dir(), exist_ok=True)
        _path = os.path.join(_cfg_dir(), "now.json")
        try:
            asyncio.run(_smtc_helper_loop(_parent, _path))
        except Exception as e:
            _write_atomic(_path, {"ts": time.time(), "sessions": [], "error": f"{type(e).__name__}: {e}"})
    sys.exit(0)


import customtkinter as ctk
from PIL import Image, ImageDraw, ImageOps
from pypresence import ActivityType, Presence

try:
    from pypresence import StatusDisplayType
except Exception:  # older pypresence: no such enum
    StatusDisplayType = None

try:
    import pystray
except Exception:  # tray is optional
    pystray = None

try:
    import mutagen
except Exception:  # tag reading is optional (falls back to file names)
    mutagen = None

APP_NAME = "LocalPresence"
APP_TITLE = "AudioTwizz"   # display name only; APP_NAME stays so existing settings keep working
APP_VERSION = "6.9"
# Address of your own update server (the one in the update-server/ folder), e.g.
# "https://updates.example.com" or "http://1.2.3.4:8765". No trailing slash. Leave empty to
# disable update checking entirely. The app asks "{UPDATE_SERVER}/api/latest" for the newest
# version and, if the person agrees, downloads and installs it automatically in the background.
UPDATE_SERVER = "https://cr1m999.github.io/audiotwizz-updates"

# ---- one copy at a time -----------------------------------------------------------------------------
# The first copy listens on a private local port. Starting a second copy pings that port, which makes the
# first one show its window, and the second one quits. The port is freed by the OS if the app ever crashes.
RESTART_FLAG = "--restarted"        # added when the app relaunches itself after an update
_SI_PORT = 47613
_SI_MAGIC = b"AUDIOTWIZZ-SHOW\n"
_si_sock = None


def acquire_single_instance(wait=0.0):
    """True: we are the only copy (or the lock can't be used) - carry on.
    False: another copy is running; it has been asked to show its window - quit.
    wait > 0: a freshly updated copy gives the old process a few seconds to let go instead of giving up."""
    global _si_sock
    deadline = time.time() + wait
    while True:
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sk.bind(("127.0.0.1", _SI_PORT))
            sk.listen(5)
            _si_sock = sk
            return True
        except OSError:
            sk.close()
        if wait > 0:
            if time.time() >= deadline:
                return True            # the old copy never let go; start anyway rather than not at all
            time.sleep(0.25)
            continue
        try:
            with socket.create_connection(("127.0.0.1", _SI_PORT), timeout=1.5) as c:
                c.sendall(_SI_MAGIC)
                c.settimeout(1.5)
                if c.recv(16).startswith(b"OK"):
                    return False       # really AudioTwizz, and it's already open
        except OSError:
            pass
        return True                    # port taken by something else: don't lock ourselves out


def release_single_instance():
    global _si_sock
    sk, _si_sock = _si_sock, None
    if sk is not None:
        try:
            sk.shutdown(socket.SHUT_RDWR)   # wakes the listener thread so the port is freed right away
        except OSError:
            pass
        try:
            sk.close()
        except OSError:
            pass


def _single_instance_listener(on_show):
    sk = _si_sock
    while sk is not None and sk is _si_sock:
        try:
            conn, _addr = sk.accept()
        except OSError:
            return                     # socket closed (quitting or restarting)
        try:
            conn.settimeout(1.0)
            if conn.recv(32).startswith(_SI_MAGIC.strip()):
                conn.sendall(b"OK\n")
                on_show()
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


AUTOSTART = "--autostart" in sys.argv    # passed by the Windows startup entry
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
NOW_PATH = os.path.join(CONFIG_DIR, "now.json")
NOW_PS_PATH = os.path.join(CONFIG_DIR, "now_ps.json")
LIBRARY_PATH = os.path.join(CONFIG_DIR, "library.json")
COVER_DB_PATH = os.path.join(CONFIG_DIR, "cover_urls.json")
LOG_PATH = os.path.join(CONFIG_DIR, "error.log")

# Fallback media reader (Windows PowerShell), used if the Python one fails.
PS_SCRIPT = r'''param([int]$ParentPid = 0, [string]$Out = "", [switch]$Once)
$ErrorActionPreference = 'Stop'

function Now-Epoch { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }

function Save($obj) {
    if ($Once) { $obj | ConvertTo-Json -Depth 6; return }
    try {
        $tmp = "$Out.tmp"
        $obj | ConvertTo-Json -Depth 6 -Compress | Set-Content -Path $tmp -Encoding UTF8
        Move-Item -Path $tmp -Destination $Out -Force
    } catch {}
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Await($op, $type) {
        $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
        $task.Wait(-1) | Out-Null
        $task.Result
    }
    [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime] | Out-Null
    [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties,Windows.Media.Control,ContentType=WindowsRuntime] | Out-Null
    $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
    $propsType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]
    $mgr = Await ($mgrType::RequestAsync()) $mgrType

    while ($true) {
        if (-not $Once -and $ParentPid -ne 0 -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
        $list = @()
        foreach ($s in $mgr.GetSessions()) {
            try {
                $p = Await ($s.TryGetMediaPropertiesAsync()) $propsType
                $t = $s.GetTimelineProperties()
                $pos = $t.Position.TotalSeconds
                $dur = ($t.EndTime - $t.StartTime).TotalSeconds
                $playing = ($s.GetPlaybackInfo().PlaybackStatus.ToString() -eq 'Playing')
                if ($playing) {
                    $delta = ([DateTimeOffset]::UtcNow - $t.LastUpdatedTime).TotalSeconds
                    if ($delta -ge 0 -and $delta -le 3600) { $pos += $delta }
                }
                if ($dur -gt 0 -and $pos -gt $dur) { $pos = $dur }
                $list += @{ app = "$($s.SourceAppUserModelId)"; title = "$($p.Title)"; artist = "$($p.Artist)"; album = "$($p.AlbumTitle)"; pos = $pos; dur = $dur; playing = $playing }
            } catch {
                $list += @{ app = "$($s.SourceAppUserModelId)"; error = $_.Exception.Message }
            }
        }
        Save @{ ts = (Now-Epoch); sessions = $list; error = $null }
        if ($Once) { break }
        Start-Sleep -Seconds 1
    }
} catch {
    Save @{ ts = (Now-Epoch); sessions = @(); error = $_.Exception.Message }
}
'''

SETTINGS_VERSION = 3

DEFAULTS = {
    "version": SETTINGS_VERSION,
    "client_id": "",
    "poll_seconds": 2,
    # what is sent
    "show_cover": True,
    "show_progress": True,
    "show_title": True,
    "show_artist": True,
    "show_album": True,
    "tpl_details": "{title}",      # line 1 (bold)
    "tpl_state": "{artist}",       # line 2
    "tpl_album": "{album}",        # line 3 (Discord's large-image text)
    "album_fallback": True,        # no cover -> Discord can't show line 3, so fold it into line 2
    "status_display": "Name",
    "prefer_file_tags": True,
    "folder_art": True,
    "upload_covers": True,
    "online_lookup": True,
    # clickable buttons under the status (Discord allows at most two; other people see them, you don't)
    "btn1_on": False,
    "btn1_label": "",
    "btn1_url": "",
    "btn2_on": False,
    "btn2_label": "",
    "btn2_url": "",
    # players
    "allow": "",
    "block": "",
    "only_library": False,
    "use_library": False,          # cover comes from the playing song itself; the file library is opt-in only
    "title_order": "Artist - Title",
    # pause behaviour
    "pause_mode": "Clear status",
    "pause_delay": 6,
    "paused_badge": True,          # kept status while paused shows a pause badge on the cover
    "player_badge": True,          # small player logo (Spotify) on the cover corner while playing
    "spotify_bg": "#1ED760",       # Spotify badge: disc colour
    "spotify_fg": "#000000",       # Spotify badge: three-bars colour
    "spotify_dynamic": False,      # Spotify badge: colours follow the cover art's dominant colour
    "song_link": True,             # streaming songs get a "Listen on ..." button that opens the song
    "song_link_title": True,       # ...and the song title itself becomes clickable
    # app
    "autostart_presence": True,
    "start_with_windows": False,
    "close_to_tray": True,
    "start_minimized": False,      # always open straight into the tray
    "notify_song_change": False,   # small toast on song change (only while the window is hidden)
    "confirm_disable": True,       # ask before turning presence fully off
    "window_geometry": "",         # remembered size/position  (WxH+X+Y, unscaled)
    "window_zoomed": False,
    "last_hidden": False,          # app was in the tray when it last closed
    "music_folders": [],
    # appearance
    "accent": "Purple",
    "tint_card": True,
    "ui_scale": 100,
    "show_preview": True,
    "mini_mode": False,            # the small always-on-top widget is open (restored on next start)
    "mini_geometry": "",           # remembered widget position  (X,Y)
    "mini_opacity": 100,
    "privacy_mode": False,         # manual "hide my status" switch (tray / sidebar / hotkey)
    "auto_hide": False,            # hide the status while one of the apps below is running
    "auto_hide_apps": "",
    "hide_fullscreen": False,      # ...or while any app is fullscreen
    "quiet_on": False,             # ...or between two times of day
    "quiet_from": "23:00",
    "quiet_to": "07:00",
    "hotkeys": True,               # Ctrl+Alt+H privacy mode, Ctrl+Alt+M mini mode
    "compact_ui": False,           # set once the window has been reset to the compact size
    # presets
    "presets": {},
}
TITLE_ORDERS = ["Artist - Title", "Title - Artist"]
STATUS_DISPLAY = ["Name", "Artist (line 2)", "Title (line 1)"]
PAUSE_MODES = ["Clear status", "Keep status (no progress)"]
TEMPLATE_VARS = ("title", "artist", "album", "albumartist", "year", "duration", "player", "filename")

ACCENTS = {
    "Purple": ("#9d5cff", "#8540ee"),
    "Violet": ("#7c4dff", "#6638e6"),
    "Lavender": ("#b794f6", "#9d77e6"),
    "Magenta": ("#c94bf0", "#a736cc"),
    "Green": ("#2bb673", "#1f9460"),
    "Pink": ("#f0529c", "#c93f82"),
    "Orange": ("#f59e42", "#cf7f2c"),
    "Teal": ("#22c1b8", "#199b94"),
    "Red": ("#f2555a", "#cc4146"),
}

ACCENT_AUTO = "Auto (cover art)"

BOOL_KEYS = (
    "show_cover", "show_progress", "show_title", "show_artist", "show_album", "album_fallback",
    "prefer_file_tags", "folder_art", "upload_covers", "online_lookup", "only_library",
    "autostart_presence", "start_with_windows", "close_to_tray", "tint_card", "show_preview",
    "paused_badge", "player_badge", "spotify_dynamic", "song_link", "song_link_title", "start_minimized", "notify_song_change", "confirm_disable",
    "btn1_on", "btn2_on", "auto_hide", "hide_fullscreen", "quiet_on", "hotkeys",
)
STR_KEYS = (
    "tpl_details", "tpl_state", "tpl_album", "status_display", "pause_mode",
    "allow", "block", "title_order", "accent",
    "btn1_label", "btn1_url", "btn2_label", "btn2_url", "spotify_bg", "spotify_fg",
    "auto_hide_apps", "quiet_from", "quiet_to",
)
INT_KEYS = ("pause_delay", "poll_seconds", "ui_scale", "mini_opacity")
CHOICES = {
    "status_display": STATUS_DISPLAY, "pause_mode": PAUSE_MODES,
    "title_order": TITLE_ORDERS, "accent": [ACCENT_AUTO] + list(ACCENTS),
}

# what a preset stores (everything about how the status looks / which players count)
PRESET_KEYS = (
    "show_cover", "show_progress", "show_title", "show_artist", "show_album",
    "tpl_details", "tpl_state", "tpl_album", "album_fallback", "status_display",
    "pause_mode", "pause_delay", "paused_badge", "player_badge", "spotify_bg", "spotify_fg", "spotify_dynamic", "song_link", "song_link_title", "prefer_file_tags", "folder_art", "only_library",
    "allow", "block", "accent", "tint_card",
)
BUILTIN_PRESETS = {
    "Default": {},
    "Minimal (title + artist)": {"show_album": False},
    "Artist - Title on one line": {"tpl_details": "{artist} - {title}", "show_artist": False},
    "Album in line 2": {"tpl_state": "{artist} \u00b7 {album}", "show_album": False},
    "Spotify-style (merge look)": {"status_display": "Artist (line 2)", "tpl_details": "{title}",
                                   "tpl_state": "{artist}", "show_album": True},
    "No cover art": {"show_cover": False},
}

# Palette: near-black with a violet cast, purple accent
BG = "#08060d"
SIDEBAR = "#0c0912"
PANEL = "#120e1b"
PANEL2 = "#1b1528"
CARD = "#0e0a16"
TEXT = "#f1ecfa"
SUB = "#b6aacb"
MUTED = "#7f7396"
HOVER = "#2a2040"       # neutral hover for secondary buttons
LINE = "#261d3a"        # hairline borders
TRACK = "#2c2342"       # progress track
ACC, ACC_H = ACCENTS["Purple"]
GREEN, YELLOW, RED, GREY = "#23a55a", "#f0b232", "#f23f43", "#80848e"
DEFAULT_THEME = {"BG": BG, "SIDEBAR": SIDEBAR, "PANEL": PANEL, "PANEL2": PANEL2, "CARD": CARD, "HOVER": HOVER,
                 "LINE": LINE, "TRACK": TRACK, "ACC": ACC, "ACC_H": ACC_H}


def apply_premium_theme():
    """Sets the default look of every customtkinter widget once, so the whole app shares radii, hairline
    borders and slim scrollbars. Plain strings only: the live re-theming code matches colours by value."""
    try:
        T = ctk.ThemeManager.theme

        def put(widget, **kw):
            T.setdefault(widget, {}).update(kw)

        put("CTkFrame", border_color=LINE)
        put("CTkButton", corner_radius=10, border_width=0, text_color=TEXT, text_color_disabled=MUTED)
        put("CTkEntry", corner_radius=10, border_width=1, fg_color=BG, border_color=LINE, text_color=TEXT,
            placeholder_text_color=MUTED)
        put("CTkOptionMenu", corner_radius=10, fg_color=PANEL2, button_color=PANEL2, button_hover_color=HOVER,
            text_color=TEXT)
        put("DropdownMenu", fg_color=PANEL2, hover_color=HOVER, text_color=TEXT)
        put("CTkSwitch", fg_color=TRACK, progress_color=ACC, button_color="#f3ecff",
            button_hover_color="#e0d3f7", text_color=TEXT)
        put("CTkSlider", fg_color=TRACK, progress_color=ACC, button_color=ACC, button_hover_color=ACC_H)
        put("CTkProgressBar", fg_color=TRACK, progress_color=ACC)
        put("CTkScrollbar", fg_color="transparent", button_color=TRACK, button_hover_color="#43355f",
            corner_radius=8, border_spacing=3)
        put("CTkSegmentedButton", corner_radius=10, unselected_hover_color=HOVER)
        put("CTkCheckBox", fg_color=ACC, hover_color=ACC_H, border_color=LINE, checkmark_color="#ffffff",
            text_color=TEXT)
        put("CTkRadioButton", fg_color=ACC, hover_color=ACC_H, border_color=LINE, text_color=TEXT)
        put("CTkComboBox", corner_radius=10, fg_color=BG, border_color=LINE, button_color=PANEL2,
            button_hover_color=HOVER, text_color=TEXT)
        put("CTkTextbox", corner_radius=10, fg_color=BG, border_color=LINE, text_color=TEXT)
    except Exception as e:
        log_error(f"theme defaults: {type(e).__name__}: {e}")


_FONTS = None


def _load_fonts():
    """Picks the best installed families once: body text, a real Semibold (instead of Tk's heavy 'bold'),
    and the Display cut for big headings (Windows 11). Falls back gracefully on older systems."""
    global _FONTS
    if _FONTS is not None:
        return _FONTS
    _FONTS = {"body": "", "semi": "", "display": "", "display_semi": ""}
    try:
        import tkinter.font as tkfont
        have = set(tkfont.families())

        def pick(*names):
            for n in names:
                if n in have:
                    return n
            return ""
        _FONTS["body"] = pick("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Inter", "SF Pro Text")
        _FONTS["semi"] = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "Inter SemiBold")
        _FONTS["display"] = pick("Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI")
        _FONTS["display_semi"] = pick("Segoe UI Variable Display Semibold", "Segoe UI Semibold",
                                      "Segoe UI Variable Text Semibold")
    except Exception:
        pass
    return _FONTS


def F(size=13, weight="normal"):
    """One type system for the whole app: Segoe UI Variable (Text for body, Display for large headings),
    with Semibold in place of heavy bold."""
    f = _load_fonts()
    big = size >= 20
    if weight == "bold":
        fam = (f["display_semi"] if big else f["semi"])
        if fam:
            return ctk.CTkFont(family=fam, size=size, weight="normal")
        fam = f["body"]
        return ctk.CTkFont(family=fam, size=size, weight="bold") if fam else ctk.CTkFont(size=size, weight="bold")
    fam = f["display"] if big and f["display"] else f["body"]
    return ctk.CTkFont(family=fam, size=size, weight="normal") if fam else ctk.CTkFont(size=size, weight=weight)


def log_error(msg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.ctime()}  {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- settings --

def load_settings():
    data = dict(DEFAULTS)
    saved = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            saved = {}
    except Exception:
        saved = {}
    data.update(saved)
    if saved and saved.get("version", 1) < SETTINGS_VERSION:
        # settings from an older build: keep the look the user had
        if saved.get("state_mode") == "Artist - Album" and "tpl_state" not in saved:
            data["tpl_state"] = "{artist} - {album}"
            data["show_album"] = False if not saved.get("show_album", True) else data["show_album"]
        if int(saved.get("poll_seconds", 5) or 5) >= 5:
            data["poll_seconds"] = 2  # a 5 s poll made pause / seek / song changes feel laggy
        data["version"] = SETTINGS_VERSION
    # sanitise
    for key, opts in CHOICES.items():
        if data.get(key) not in opts:
            data[key] = DEFAULTS[key]
    if not isinstance(data.get("presets"), dict):
        data["presets"] = {}
    try:
        data["pause_delay"] = max(0, min(60, int(data["pause_delay"])))
        data["poll_seconds"] = max(1, min(15, int(data["poll_seconds"])))
        data["ui_scale"] = max(80, min(140, int(data["ui_scale"])))
        data["mini_opacity"] = max(40, min(100, int(data["mini_opacity"])))
    except Exception:
        data["pause_delay"], data["poll_seconds"], data["ui_scale"], data["mini_opacity"] = 6, 2, 100, 100
    return data


def save_settings(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def startup_command(minimized=False):
    """The command Windows runs at sign-in. Always the windowless interpreter + absolute paths."""
    if getattr(sys, "frozen", False):
        parts = [sys.executable]
    else:
        exe = sys.executable
        d, f = os.path.split(exe)
        if f.lower() == "python.exe":
            pw = os.path.join(d, "pythonw.exe")
            if os.path.exists(pw):
                exe = pw
        parts = [exe, os.path.abspath(__file__)]
    cmd = " ".join('"%s"' % p for p in parts) + " --autostart"
    if minimized:
        cmd += " --minimized"
    return cmd


def set_startup(enabled, minimized=False):
    """Add / remove the 'launch with Windows' entry. Raises if Windows refuses, so the UI can say so."""
    if sys.platform != "win32":
        return
    import winreg

    run_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    appr_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
    try:
        if enabled:
            cmd = startup_command(minimized)
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            back, _t = winreg.QueryValueEx(key, APP_NAME)
            if back != cmd:
                raise OSError("Windows did not keep the startup entry")
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)
    # If the entry was ever switched off in Task Manager > Startup apps, Windows keeps ignoring it
    # until this "approved" flag is cleared - removing the flag puts it back to enabled.
    try:
        ak = winreg.OpenKey(winreg.HKEY_CURRENT_USER, appr_path, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(ak, APP_NAME)
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(ak)
    except OSError:
        pass


# ------------------------------------------- window-title fallback reader --
# Used when Windows' media-control service refuses to talk to us. Reads the
# title bar of known music players. No duration available, so Discord shows
# an elapsed timer instead of a progress bar.
PLAYER_SUFFIX = {
    "spotify.exe": None,
    "vlc.exe": r"\s+-\s+VLC media player$",
    "musicbee.exe": r"\s+-\s+MusicBee$",
    "foobar2000.exe": r"\s*\[foobar2000[^\]]*\]\s*$",
    "aimp.exe": r"\s+-\s+AIMP$",
    "potplayermini64.exe": r"\s+-\s+PotPlayer$",
    "potplayermini.exe": r"\s+-\s+PotPlayer$",
    "mpc-hc64.exe": r"\s+-\s+MPC-HC.*$",
    "mpc-hc.exe": r"\s+-\s+MPC-HC.*$",
}
IDLE_TITLES = {
    "spotify", "spotify free", "spotify premium", "advertisement",
    "vlc media player", "musicbee", "foobar2000", "aimp", "potplayer",
}


def list_windows():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    out = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if h:
            size = wintypes.DWORD(520)
            pbuf = ctypes.create_unicode_buffer(520)
            if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                exe = os.path.basename(pbuf.value).lower()
            kernel32.CloseHandle(h)
        out.append((exe, buf.value))
        return True

    user32.EnumWindows(proc_type(cb), 0)
    return out


def parse_title(exe, title, order):
    """Return (song, artist) from a player's window title, or None if idle."""
    text = (title or "").strip()
    if text.lower() in IDLE_TITLES:
        return None
    pat = PLAYER_SUFFIX.get(exe)
    if pat:
        text = re.sub(pat, "", text, flags=re.I).strip()
    if not text or text.lower() in IDLE_TITLES:
        return None
    if " - " in text:
        first, rest = text.split(" - ", 1)
        artist, song = (first, rest) if order == "Artist - Title" else (rest, first)
    else:
        artist, song = "", text
    return song.strip(), artist.strip()



# ----------------------------------------------------------------- helpers --

def fit(text):
    """Discord needs 2-128 characters for details/state."""
    text = (text or "").strip()[:128]
    return text if len(text) >= 2 else text + "\u00a0" * (2 - len(text))


def fmt_time(sec):
    sec = max(0, int(sec))
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def clip(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


CUSTOM_ICON = os.path.join(CONFIG_DIR, "custom_icon.png")     # picked in Settings > General > App icon
_ICON_CACHE = {}


def make_icon(size=64):
    """The app icon: your picked image if there is one, otherwise the built-in note."""
    try:
        if os.path.isfile(CUSTOM_ICON):
            key = (size, os.path.getmtime(CUSTOM_ICON))
            if key not in _ICON_CACHE:
                im = Image.open(CUSTOM_ICON).convert("RGBA")
                _ICON_CACHE[key] = im.resize((size, size), Image.LANCZOS)
            return _ICON_CACHE[key].copy()
    except Exception as e:
        log_error(f"custom icon: {type(e).__name__}: {e}")
    return _default_icon(size)


def _default_icon(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    grad = Image.new("RGBA", (size, size))
    gp = grad.load()
    for yy in range(size):
        for xx in range(size):
            t = (xx + yy) / (2.0 * size)
            gp[xx, yy] = (int(176 - 70 * t), int(110 - 55 * t), int(255 - 25 * t), 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 4, fill=255)
    img.paste(grad, (0, 0), mask)
    s = size / 64
    d.ellipse([18 * s, 38 * s, 32 * s, 50 * s], fill="white")
    d.rectangle([29 * s, 16 * s, 33 * s, 44 * s], fill="white")
    d.polygon([(33 * s, 16 * s), (46 * s, 22 * s), (46 * s, 28 * s), (33 * s, 22 * s)], fill="white")
    return img


def _norm(x):
    """Lower-case, no accents, no punctuation: "Don't Stop (Me Now)!" -> "dont stop me now"."""
    x = unicodedata.normalize("NFKD", x or "")
    x = "".join(c for c in x if not unicodedata.combining(c))
    x = x.lower().replace("&", " and ")
    x = re.sub(r"[\u2018\u2019'`\u00b4]", "", x)  # don't == dont
    return re.sub(r"[\W_]+", " ", x).strip()


def _match(title, artist, found_title, found_artist):
    """Loose check used for the online fallback lookups."""
    nt, na = _norm(title), _norm(artist)
    ft, fa = _norm(found_title), _norm(found_artist)
    title_ok = bool(nt and ft) and (nt in ft or ft in nt)
    artist_ok = (not na) or (bool(fa) and (na in fa or fa in na))
    return title_ok and artist_ok


# ------------------------------------------------ tolerant title / artist keys --
_FEAT_BRACKET = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with|w/)\s+([^\)\]]+)[\)\]]", re.I)
_FEAT_TAIL = re.compile(r"\s+(?:-\s+)?(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.I)
_VERSION_TAIL = re.compile(
    r"\s+[-\u2013\u2014]\s+[^-\u2013\u2014]*\b(?:remaster(?:ed)?|version|edit|mono|stereo|explicit|"
    r"deluxe|bonus|single|radio|live|mix)\b.*$", re.I)
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|;|/|\+|&|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\bvs\.?|\band\b|\bx\b)\s*", re.I)


def split_feat(title):
    """"Song (feat. A & B)" -> ("Song", ["A & B"])"""
    feats = []

    def grab(m):
        feats.append(m.group(1))
        return ""

    t = _FEAT_BRACKET.sub(grab, title or "")
    t = _FEAT_TAIL.sub(grab, t)
    return t.strip(), feats


def full_title_key(title):
    return _norm(split_feat(title)[0])


def loose_title_key(title):
    """Also ignores (Remastered), [Explicit], '- Single Version' ... for a looser second try."""
    t = split_feat(title)[0]
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)
    t = _VERSION_TAIL.sub("", t)
    return _norm(t)


def artist_set(artist, extra=()):
    out = set()
    for chunk in [artist or ""] + list(extra):
        for part in _ARTIST_SPLIT.split(chunk):
            n = _norm(part)
            if n.startswith("the ") and len(n) > 4:  # "The Beatles" == "Beatles"
                n = n[4:]
            if n:
                out.add(n)
    return out


def artist_cmp(a, b):
    """0..30 for how well two artist sets agree, or None when both are known and clearly different."""
    if not a or not b:
        return 15  # unknown on one side: neutral
    if a == b:
        return 30
    inter = a & b
    if inter:
        return int(18 + 12 * len(inter) / max(len(a), len(b)))
    ja, jb = " ".join(sorted(a)), " ".join(sorted(b))
    if ja in jb or jb in ja:
        return 20
    return None


# ------------------------------------------- tags + cover art from the files --
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wma", ".wav", ".aac"}
FOLDER_ART_NAMES = ("cover", "folder", "front", "album", "albumart", "art", "artwork", "thumb")


def _join(values):
    if values is None:
        return ""
    if not isinstance(values, (list, tuple)):
        values = [values]
    return ", ".join(str(v) for v in values if str(v).strip())


def read_tags(path):
    """Returns dict(title, artist, album, albumartist, year, dur, cover, tagged) from a file, or None."""
    if mutagen is None:
        return None
    f = mutagen.File(path)
    if f is None:
        return None
    out = {"title": "", "artist": "", "album": "", "albumartist": "", "year": "",
           "dur": 0.0, "cover": False, "tagged": False}
    out["dur"] = float(getattr(getattr(f, "info", None), "length", 0) or 0)
    tags = f.tags
    if tags is not None:
        if hasattr(tags, "getall"):  # ID3 (mp3, wav, aac)
            def t(frame):
                fr = tags.getall(frame)
                return _join(list(fr[0].text)) if fr and getattr(fr[0], "text", None) else ""

            out["title"], out["artist"], out["album"] = t("TIT2"), t("TPE1"), t("TALB")
            out["albumartist"] = t("TPE2")
            out["year"] = (t("TDRC") or t("TYER") or t("TDOR"))[:4]
            out["cover"] = bool(tags.getall("APIC"))
        elif hasattr(tags, "get"):  # Vorbis comments, MP4, WMA

            def g(*keys):
                for k in keys:
                    try:
                        v = tags.get(k)
                    except Exception:
                        v = None
                    if v:
                        return _join(v)
                return ""

            out["title"] = g("title", "\xa9nam", "Title", "WM/Title")
            out["artist"] = g("artist", "\xa9ART", "Author", "WM/Author")
            out["album"] = g("album", "\xa9alb", "WM/AlbumTitle")
            out["albumartist"] = g("albumartist", "album artist", "aART", "WM/AlbumArtist")
            out["year"] = g("date", "year", "\xa9day", "WM/Year")[:4]
            try:
                out["cover"] = bool(
                    getattr(f, "pictures", None) or tags.get("covr")
                    or tags.get("metadata_block_picture") or tags.get("WM/Picture")
                )
            except Exception:
                pass
    if getattr(f, "pictures", None):
        out["cover"] = True
    out["tagged"] = bool(out["title"].strip())
    return out


def _asf_picture(raw):
    """WMA 'WM/Picture' blob: type(1) size(4) mime\\0 desc\\0 data."""
    n = int.from_bytes(raw[1:5], "little")
    i = 5
    for _ in range(2):
        while raw[i:i + 2] != b"\x00\x00":
            i += 2
        i += 2
    return bytes(raw[i:i + n])


def read_cover(path):
    """Returns the embedded cover picture bytes of an audio file, or None."""
    if mutagen is None:
        return None
    f = mutagen.File(path)
    if f is None:
        return None
    tags = f.tags
    if tags is not None and hasattr(tags, "getall"):
        pics = tags.getall("APIC")
        if pics:
            best = next((p for p in pics if getattr(p, "type", None) == 3), pics[0])  # 3 = front cover
            return bytes(best.data)
    pics = getattr(f, "pictures", None)
    if pics:  # FLAC
        best = next((p for p in pics if getattr(p, "type", None) == 3), pics[0])
        return bytes(best.data)
    if tags is not None and hasattr(tags, "get"):
        try:
            covr = tags.get("covr")  # MP4 / M4A
            if covr:
                return bytes(covr[0])
            mbp = tags.get("metadata_block_picture")  # Ogg / Opus
            if mbp:
                from mutagen.flac import Picture

                return bytes(Picture(base64.b64decode(mbp[0])).data)
            wm = tags.get("WM/Picture")  # WMA
            if wm:
                v = wm[0]
                return _asf_picture(bytes(getattr(v, "value", v)))
        except Exception:
            pass
    return None


def folder_cover(path):
    """cover.jpg / folder.png / front.jpg ... sitting next to the audio file."""
    d = os.path.dirname(path)
    try:
        names = {n.lower(): n for n in os.listdir(d)}
    except OSError:
        return None
    for base in FOLDER_ART_NAMES:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            n = names.get(base + ext)
            if not n:
                continue
            p = os.path.join(d, n)
            try:
                if os.path.getsize(p) <= 15_000_000:
                    with open(p, "rb") as fh:
                        return fh.read()
            except OSError:
                pass
    return None


MIN_MATCH_SCORE = 50


class Library:
    """Index of the tags in the user's music folders, used to find the file that is playing."""

    def __init__(self, ui_queue):
        self.ui = ui_queue
        self.tracks = {}
        self.idx = None
        self.gen = 0
        self.scanning = False

    def load_cache(self):
        try:
            with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("v") == 2:  # v1 caches lack album artist / year, so they are re-read
                self.tracks = data["tracks"]
                self._build()
        except Exception:
            pass
        self._report(False)

    def _save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
                json.dump({"v": 2, "tracks": self.tracks}, f)
        except Exception:
            pass

    def _build(self):
        keys, by_full, by_loose, by_file = {}, {}, {}, {}
        for p, r in self.tracks.items():
            title = r.get("title", "")
            clean, feats = split_feat(title)
            nf, nl = _norm(clean), loose_title_key(title)
            keys[p] = (nf, nl, artist_set(r.get("artist", ""), feats), _norm(r.get("album", "")))
            by_full.setdefault(nf, []).append(p)
            by_loose.setdefault(nl, []).append(p)
            stem = os.path.splitext(os.path.basename(p))[0]
            for k in {full_title_key(stem), loose_title_key(stem), loose_title_key(stem.split(" - ", 1)[-1])}:
                if k:
                    by_file.setdefault(k, []).append(p)
        toks = {k: {w for w in k.split() if len(w) > 2} for k in by_loose}
        self.idx = (keys, by_full, by_loose, by_file, toks)
        self.gen += 1

    def _report(self, scanning, seen=None):
        n = len(self.tracks) if seen is None else seen
        covers = sum(1 for r in self.tracks.values() if r.get("cover")) if seen is None else None
        self.ui.put(("lib", n, covers, scanning, mutagen is not None))

    def scan(self, folders):
        if self.scanning:
            return
        self.scanning = True
        threading.Thread(target=self._scan, args=(list(folders),), daemon=True).start()

    def _read(self, path, st):
        rec = {"path": path, "mtime": int(st.st_mtime), "size": st.st_size,
               "title": "", "artist": "", "album": "", "albumartist": "", "year": "",
               "dur": 0.0, "cover": False, "tagged": False}
        try:
            tags = read_tags(path)
        except Exception as e:
            tags = None
            log_error(f"tags {os.path.basename(path)}: {type(e).__name__}: {e}")
        if tags:
            rec.update(tags)
        if not rec["title"]:  # no tag: use the file name ("Artist - Title.mp3")
            stem = os.path.splitext(os.path.basename(path))[0]
            if " - " in stem:
                a, t = stem.split(" - ", 1)
                rec["title"] = t.strip()
                rec["artist"] = rec["artist"] or a.strip()
            else:
                rec["title"] = stem.strip()
        return rec

    def _scan(self, folders):
        try:
            old, new, seen = self.tracks, {}, 0
            for root in folders:
                for dp, _dn, files in os.walk(root):
                    for name in files:
                        if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                            continue
                        p = os.path.join(dp, name)
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        rec = old.get(p)
                        if not rec or rec.get("mtime") != int(st.st_mtime) or rec.get("size") != st.st_size \
                                or "tagged" not in rec:
                            rec = self._read(p, st)
                        new[p] = rec
                        seen += 1
                        if seen % 25 == 0:
                            self._report(True, seen)
            self.tracks = new
            self._build()
            self._save()
        except Exception as e:
            log_error(f"library scan: {type(e).__name__}: {e}")
        finally:
            self.scanning = False
            self._report(False)

    def find_ex(self, title, artist="", album="", dur=0.0):
        """Best matching file record and its score (0 / None when nothing is convincing)."""
        if not self.idx or not (title or "").strip():
            return None, 0
        keys, by_full, by_loose, by_file, toks = self.idx
        clean, feats = split_feat(title)
        nf, nl = _norm(clean), loose_title_key(title)
        aset = artist_set(artist, feats)
        nal = _norm(album)

        cands = {}  # path -> how well the title matched
        for p in by_full.get(nf, ()):
            cands[p] = 45
        for p in by_loose.get(nl, ()):
            cands.setdefault(p, 35)
        for k in (nf, nl):
            for p in by_file.get(k, ()):
                cands.setdefault(p, 30)
        if not cands and nl:  # typos, odd spacing ...
            mine = {w for w in nl.split() if len(w) > 2}
            short = [k for k, tk in toks.items()
                     if abs(len(k) - len(nl)) <= max(4, len(nl) // 4) and (tk & mine)]
            for k in difflib.get_close_matches(nl, short, n=5, cutoff=0.86):
                ratio = difflib.SequenceMatcher(None, nl, k).ratio()
                for p in by_loose[k]:
                    cands.setdefault(p, int(ratio * 30))

        best, best_score = None, 0
        for p, ts in cands.items():
            r = self.tracks.get(p)
            if not r or p not in keys:
                continue
            _nf, _nl, aset_r, nal_r = keys[p]
            ac = artist_cmp(aset, aset_r)
            album_eq = bool(nal and nal_r and nal == nal_r)
            if ac is None:  # artists disagree: only accept a rock-solid title + album match
                if not (ts >= 45 and album_eq):
                    continue
                ac = 0
            sc = ts + ac
            if nal and nal_r:
                if album_eq:
                    sc += 12
                elif nal in nal_r or nal_r in nal:
                    sc += 6
            rd = r.get("dur", 0) or 0
            if dur > 0 and rd > 0:
                d = abs(dur - rd)
                sc += 15 if d <= 2.5 else 6 if d <= 6 else (-25 if d > 12 else 0)
            if sc > best_score:
                best, best_score = r, sc
        if best is not None and best_score < MIN_MATCH_SCORE:
            return None, best_score
        return best, best_score

    def find(self, title, artist="", album="", dur=0.0):
        return self.find_ex(title, artist, album, dur)[0]


# ------------------------------------------------------ what Discord receives --
_TPL_RE = re.compile(r"\{(\w+)\}")
_SEPS = "-\u2013\u2014\u00b7|\u2022/,:;"


PAUSE_MARK = "\u23f8"

_BADGE_CACHE = {}
SPOTIFY_BG, SPOTIFY_FG = "#1ED760", "#000000"


def norm_hex(v, default):
    """'#1ed760' / '1ED760' -> '#1ED760'; anything else -> default."""
    v = str(v or "").strip().lstrip("#")
    return "#" + v.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", v) else default


def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


_DYN_CACHE = {}


def _lum(rgb):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2])


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def cover_colors(data):
    """Picks the pair of cover colours that looks best as a logo: (disc, bars).
    Colours are grouped into families first: every shade of purple counts as one purple, so a shaded
    object isn't lost just because its pixels are spread over many slightly different shades. Each family
    is shown by one real pixel colour from the cover (never an average), so nothing is invented.
    Disc = the colourful family that best represents the cover (a colourful accent beats black / white / grey,
    even when it covers a small area); bars = the cover colour that stands out most against it, else black / white."""
    import colorsys
    from collections import Counter
    img = Image.open(io.BytesIO(data)).convert("RGB").resize((128, 128), Image.NEAREST)   # NEAREST: no blended edge pixels
    exact = Counter(img.getdata())
    total = float(sum(exact.values()))

    def hls(c):
        return colorsys.rgb_to_hls(c[0] / 255, c[1] / 255, c[2] / 255)

    def tone(l):
        return max(0.05, 1.0 - abs(l - 0.50) * 2.0)           # best around mid lightness

    chroma, neutral = [], []        # chroma: [hue, [(rgb, n, s, l)...], count]   neutral: [rep rgb, count]
    for rgb, n in exact.most_common(1500):
        h, l, s = hls(rgb)
        if s >= 0.22 and 0.10 <= l <= 0.93:
            for fam in chroma:
                dh = abs(h - fam[0]); dh = min(dh, 1 - dh)
                if dh <= 0.055:               # within ~20 degrees: same colour family
                    fam[1].append((rgb, n, s, l)); fam[2] += n
                    break
            else:
                chroma.append([h, [(rgb, n, s, l)], n])
        else:
            for g in neutral:
                r0 = g[0]
                if (rgb[0] - r0[0]) ** 2 + (rgb[1] - r0[1]) ** 2 + (rgb[2] - r0[2]) ** 2 < 42 ** 2:
                    g[1] += n
                    break
            else:
                neutral.append([rgb, n])

    fams = []       # (rep rgb, share, saturation) for each colourful family worth considering
    for h, members, cnt in chroma:
        share = cnt / total
        if share < 0.006:
            continue
        # show the family by its best-looking real shade: common, colourful, mid lightness
        rep = max(members, key=lambda m: m[1] * (0.3 + m[2]) * tone(m[3]))
        fams.append((rep[0], share, rep[2]))
    neut = [(g[0], g[1] / total) for g in neutral if g[1] / total >= 0.03]

    if fams:
        disc = max(fams, key=lambda f: (f[1] ** 0.5) * (0.1 + f[2]) * tone(hls(f[0])[1]))[0]
    elif neut:
        disc = max(neut, key=lambda c: c[1])[0]
    else:
        top = exact.most_common(1)[0][0]
        return top, None

    best, best_score = None, 0.0
    for rgb, share in [(f[0], f[1]) for f in fams] + neut:
        if rgb == disc:
            continue
        ratio = _contrast(disc, rgb)
        if ratio < 2.5:
            continue
        sc = (max(share, 0.02) ** 0.3) * min(ratio, 8.0) / 8.0
        if sc > best_score:
            best, best_score = rgb, sc
    return disc, best


def dynamic_badge_colors(data):
    """(disc, bars) chosen from the cover. Same cover bytes -> same answer (cached), so the logo only
    changes when the cover does."""
    if not data:
        return None
    key = hashlib.md5(data).hexdigest()
    if key in _DYN_CACHE:
        return _DYN_CACHE[key]
    cols = None
    try:
        disc, bars = cover_colors(data)
        if bars is None:        # nothing on the cover contrasts enough: black or white
            bars = (0, 0, 0) if _contrast(disc, (0, 0, 0)) >= _contrast(disc, (255, 255, 255)) else (255, 255, 255)
        cols = ("#%02X%02X%02X" % tuple(disc), "#%02X%02X%02X" % tuple(bars))
    except Exception as e:
        log_error(f"dynamic badge colours: {type(e).__name__}: {e}")
    if len(_DYN_CACHE) > 200:
        _DYN_CACHE.clear()
    _DYN_CACHE[key] = cols
    return cols


def badge_colors(s, cover_data=None):
    """(disc, bars) colours for the Spotify badge: from the cover when 'Dynamic' is on, else from the settings."""
    if s.get("spotify_dynamic"):
        dyn = dynamic_badge_colors(cover_data)
        if dyn:
            return dyn
    return norm_hex(s.get("spotify_bg"), SPOTIFY_BG), norm_hex(s.get("spotify_fg"), SPOTIFY_FG)


def badge_img(kind, size=256, bg=None, fg=None):
    """Solid square badge icon (Discord crops the small image to a circle). kind: 'pause' | 'spotify'."""
    bg, fg = norm_hex(bg, SPOTIFY_BG), norm_hex(fg, SPOTIFY_FG)
    key = (kind, size, bg, fg) if kind == "spotify" else (kind, size)
    if key in _BADGE_CACHE:
        return _BADGE_CACHE[key].copy()
    # want the official artwork? drop your own square PNG at  %APPDATA%\\LocalPresence\\badges\\spotify.png  (or pause.png)
    try:
        custom = os.path.join(CONFIG_DIR, "badges", kind + ".png")
        if os.path.isfile(custom):
            img = ImageOps.fit(Image.open(custom).convert("RGB"), (size, size), Image.LANCZOS)
            _BADGE_CACHE[key] = img
            return img.copy()
    except Exception as e:
        log_error(f"custom badge {kind}: {type(e).__name__}: {e}")
    S = size * 4
    if kind == "pause":
        img = Image.new("RGB", (S, S), (24, 25, 29))
        d = ImageDraw.Draw(img)
        w, h, gap = S * 0.13, S * 0.38, S * 0.11
        for x0 in (S / 2 - gap / 2 - w, S / 2 + gap / 2):
            d.rounded_rectangle((x0, S / 2 - h / 2, x0 + w, S / 2 + h / 2), radius=w * 0.3, fill=(255, 255, 255))
    else:   # spotify: green disc, three black curved bands (long/thick on top, shorter/thinner below)
        img = Image.new("RGB", (S, S), _rgb(bg))
        d = ImageDraw.Draw(img)
        # (half length, y at the ends, rise in the middle, thickness) as fractions of the disc
        for hl, y_end, rise, th in ((0.300, 0.405, 0.085, 0.082), (0.262, 0.540, 0.072, 0.068),
                                    (0.222, 0.655, 0.060, 0.056)):
            n = 160
            pts = []
            for i in range(n + 1):
                t = i / n * 2 - 1
                pts.append(((0.5 + t * hl) * S, (y_end - rise * (1 - t * t) + t * 0.018) * S))
            half = th * S / 2
            up, dn = [], []
            for i, (x, y) in enumerate(pts):
                a, b = pts[max(i - 1, 0)], pts[min(i + 1, n)]
                dx, dy = b[0] - a[0], b[1] - a[1]
                ln = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / ln, dx / ln
                up.append((x + nx * half, y + ny * half))
                dn.append((x - nx * half, y - ny * half))
            d.polygon(up + dn[::-1], fill=_rgb(fg))
            for (x, y) in (pts[0], pts[-1]):
                d.ellipse((x - half, y - half, x + half, y + half), fill=_rgb(fg))
    img = img.resize((size, size), Image.LANCZOS)
    _BADGE_CACHE[key] = img
    return img.copy()


def badge_bytes(kind, bg=None, fg=None):
    buf = io.BytesIO()
    badge_img(kind, 256, bg, fg).save(buf, "PNG")
    return buf.getvalue()


def player_kind(app):
    """Which player logo we have for this app (None = no badge)."""
    return "spotify" if "spotify" in (app or "").lower() else None


def friendly_player(app):
    """"Spotify.exe" -> "Spotify";  "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic" -> "ZuneMusic"."""
    a = (app or "").replace(" (window title mode)", "")
    a = a.split("!")[-1]
    parts = [p for p in a.split(".") if p and p.lower() != "exe"]
    return (parts[-1] if parts else a).strip() or "player"


def render_template(tpl, values):
    """Fills {title} {artist} ... and tidies separators left dangling by empty values."""
    blank = [False]

    def sub(m):
        name = m.group(1)
        if name not in values:
            return m.group(0)
        v = str(values.get(name) or "")
        if not v.strip():
            blank[0] = True
        return v

    out = _TPL_RE.sub(sub, tpl or "")
    if blank[0]:
        out = re.sub(r"\(\s*\)|\[\s*\]", "", out)
        out = re.sub(r"(?:\s+[%s]\s+){2,}" % re.escape(_SEPS), " - ", out)
        out = re.sub(r"^[\s%s]+|[\s%s]+$" % (re.escape(_SEPS), re.escape(_SEPS)), "", out)
    return out.strip()


# ------------------------------------------------- auto-hide / hotkeys helpers --
_SHELL_EXES = {"explorer.exe", "applicationframehost.exe", "textinputhost.exe", "searchhost.exe",
               "startmenuexperiencehost.exe", "shellexperiencehost.exe", "systemsettings.exe",
               "lockapp.exe", "widgets.exe"}


def parse_app_list(text):
    """'Valorant-Win64-Shipping, eldenring.exe' -> {'valorant-win64-shipping.exe', 'eldenring.exe'}"""
    out = set()
    for part in re.split(r"[,;\n]+", text or ""):
        n = part.strip().lower().strip('"')
        if n:
            out.add(n if "." in n else n + ".exe")
    return out


def _parse_hm(t):
    m = re.match(r"^\s*(\d{1,2})[:.]?(\d{2})\s*$", t or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def in_quiet_hours(frm, to, now=None):
    """True while the local time is inside [frm, to) - the range may cross midnight (23:00 -> 07:00)."""
    a, b = _parse_hm(frm), _parse_hm(to)
    if a is None or b is None or a == b:
        return False
    t = time.localtime(now)
    cur = t.tm_hour * 60 + t.tm_min
    return (a <= cur < b) if a < b else (cur >= a or cur < b)


def running_app_names():
    """Sorted exe names of programs that currently have a visible window (what the 'add app' picker offers)."""
    if sys.platform != "win32":
        return []
    me = os.path.basename(sys.executable or "").lower()
    try:
        names = {e for e, _t in list_windows() if e}
    except Exception:
        return []
    return sorted(n for n in names if n not in _SHELL_EXES and n != me)


def fullscreen_exe():
    """Exe name of the foreground window when it covers its whole monitor (a game, a fullscreen video), else ''."""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u32.MonitorFromWindow.restype = wintypes.HANDLE
    u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return ""
    rc = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return ""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not u32.GetMonitorInfoW(u32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
        return ""
    r = mi.rcMonitor
    if not (rc.left <= r.left and rc.top <= r.top and rc.right >= r.right and rc.bottom >= r.bottom):
        return ""
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return ""
    exe = ""
    h = k32.OpenProcess(0x1000, False, pid.value)
    if h:
        size = wintypes.DWORD(520)
        buf = ctypes.create_unicode_buffer(520)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            exe = os.path.basename(buf.value).lower()
        k32.CloseHandle(h)
    return "" if (not exe or exe in _SHELL_EXES) else exe


class GameWatcher(threading.Thread):
    """Every few seconds decides whether the status should be hidden automatically (a listed app is running,
    something is fullscreen, or it's quiet hours) and tells the worker."""

    def __init__(self, get_settings, worker, ui_queue):
        super().__init__(daemon=True)
        self.get, self.worker, self.ui = get_settings, worker, ui_queue

    def reason(self, s):
        if s.get("quiet_on") and in_quiet_hours(s.get("quiet_from"), s.get("quiet_to")):
            return "quiet hours"
        if s.get("auto_hide"):
            wanted = parse_app_list(s.get("auto_hide_apps"))
            if wanted:
                try:
                    have = {e for e, _t in list_windows() if e} if sys.platform == "win32" else set()
                except Exception:
                    have = set()
                hit = sorted(wanted & have)
                if hit:
                    return hit[0]
        if s.get("hide_fullscreen"):
            try:
                exe = fullscreen_exe()
            except Exception:
                exe = ""
            if exe:
                return "fullscreen: " + exe
        return ""

    def run(self):
        last = None
        while not self.worker.stop_evt.is_set():
            try:
                why = self.reason(self.get())
            except Exception as e:
                log_error(f"auto-hide: {type(e).__name__}: {e}")
                why = ""
            if why != last:
                last = why
                self.worker.auto_reason = why
                if why:
                    self.worker.auto_hide.set()
                else:
                    self.worker.auto_hide.clear()
                self.worker.wake.set()
                self.ui.put(("hide", why))
            self.worker.stop_evt.wait(4)


class HotkeyThread(threading.Thread):
    """Global hotkeys (Windows): Ctrl+Alt+H = privacy mode, Ctrl+Alt+M = mini mode."""
    KEYS = ((1, 0x48, "privacy"), (2, 0x4D, "mini"))      # (id, virtual key, command)

    def __init__(self, ui_queue):
        super().__init__(daemon=True)
        self.ui, self.tid = ui_queue, None

    def run(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            self.tid = k32.GetCurrentThreadId()
            ok = []
            for hid, vk, name in self.KEYS:       # MOD_ALT | MOD_CONTROL | MOD_NOREPEAT
                if u32.RegisterHotKey(None, hid, 0x0001 | 0x0002 | 0x4000, vk):
                    ok.append(hid)
                else:
                    log_info(f"hotkey Ctrl+Alt+{chr(vk)} is taken by another program")
            msg = wintypes.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:         # WM_HOTKEY
                    for hid, _vk, name in self.KEYS:
                        if msg.wParam == hid:
                            self.ui.put(("cmd", name))
            for hid in ok:
                u32.UnregisterHotKey(None, hid)
        except Exception as e:
            log_error(f"hotkeys: {type(e).__name__}: {e}")

    def stop(self):
        try:
            if self.tid:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)   # WM_QUIT
        except Exception:
            pass


def track_values(info, m):
    dur = m.get("dur") or info.get("dur") or 0.0
    return {
        "title": m.get("title") or info.get("title") or "",
        "artist": m.get("artist") or info.get("artist") or "",
        "album": m.get("album") or "",
        "albumartist": m.get("albumartist") or "",
        "year": m.get("year") or "",
        "duration": fmt_time(dur) if dur > 0 else "",
        "player": friendly_player(info.get("app", "")),
        "filename": os.path.basename(m.get("file") or ""),
    }


def clean_url(url):
    """Turns what the user typed into a link Discord accepts (http/https, <= 512 chars), or '' if it can't be one."""
    u = (url or "").strip()
    if not u or " " in u:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        u = "https://" + u            # 'example.com' -> 'https://example.com'
    if not re.match(r"^https?://[^/\s]+\.[^/\s]+", u, re.I) and not re.match(r"^https?://localhost", u, re.I):
        return ""
    return u if len(u) <= 512 else ""


def active_buttons(s):
    """The (up to two) buttons that are switched on and have both a label and a usable link."""
    out = []
    for n in (1, 2):
        if not s.get(f"btn{n}_on"):
            continue
        label = (s.get(f"btn{n}_label") or "").strip()[:32]
        url = clean_url(s.get(f"btn{n}_url"))
        if label and url:
            out.append({"label": label, "url": url})
    return out


def build_activity(info, m, s, now=None, paused=False, badge=True):
    """Everything that gets sent to Discord for the current track (also drives the live preview)."""
    now = time.time() if now is None else now
    vals = track_values(info, m)
    cover_url = m.get("cover_url") if s.get("show_cover") else None
    if cover_url and len(cover_url) > 256:  # Discord rejects longer image URLs
        cover_url = None

    details = render_template(s.get("tpl_details", "{title}"), vals) if s.get("show_title") else ""
    state = render_template(s.get("tpl_state", "{artist}"), vals) if s.get("show_artist") else ""
    line3 = render_template(s.get("tpl_album", "{album}"), vals) if s.get("show_album") else ""
    if line3 and not cover_url and s.get("album_fallback"):
        # Discord only shows the large-image text when there is a large image
        state = f"{state} \u00b7 {line3}" if state else line3
        line3 = ""

    sd = {"Artist (line 2)": 1, "Title (line 1)": 2}.get(s.get("status_display"), 0)
    icons = m.get("icons")        # {kind: public URL}; None for sample/preview data
    badge_kind, small_url, small_text, badge_cols = None, None, None, None
    if paused and badge and s.get("paused_badge", True):
        badge_kind, small_text = "pause", "Paused"
        small_url = (icons or {}).get("pause")
        if icons is not None and not small_url:
            # the pause icon isn't hosted (yet): fall back to the text mark so a pause is still visible.
            # It goes on a line that is NOT the one Discord copies into the member-list status.
            if sd == 1:
                details = (PAUSE_MARK + " " + details) if details else PAUSE_MARK + " Paused"
            else:
                state = (f"{state} \u00b7 {PAUSE_MARK} Paused" if state else f"{PAUSE_MARK} Paused")
    elif not paused and s.get("player_badge", True):
        pk = player_kind(info.get("app"))
        if pk:
            badge_kind, small_text = pk, pk.capitalize()
            small_url = (icons or {}).get(pk)
            badge_cols = badge_colors(s, m.get("cover_bytes"))
    if not cover_url:
        small_url = None          # Discord only draws the small image on top of a large one

    dur = float(m.get("dur") or info.get("dur") or 0.0)
    pos = max(0.0, float(info.get("pos") or 0.0))
    if info.get("playing"):
        pos += max(0.0, now - info.get("t", now))
    if dur > 0:
        pos = min(pos, dur)
    show_time = bool(s.get("show_progress"))
    live = show_time and not paused and bool(info.get("playing"))
    start = int(round(now - pos)) if live else None
    end = int(round(now - pos + dur)) if (live and dur > 0) else None

    # streaming song: first button opens the song itself, your own buttons follow (Discord allows two in total)
    buttons = active_buttons(s)
    details_url = None
    sl = m.get("song_link") if s.get("song_link", True) else None
    if sl and sl.get("url"):
        buttons = ([{"label": sl["label"][:32], "url": sl["url"]}] + buttons)[:2]
        if s.get("song_link_title", True) and details and len(sl["url"]) <= 256:
            details_url = sl["url"]

    act = {
        "details": fit(details) if details else None,
        "state": fit(state) if state else None,
        "large_text": fit(line3) if (line3 and cover_url) else None,
        "large_image": cover_url,
        "start": start, "end": end,
        "pos": pos, "dur": dur, "live": live, "show_time": show_time,
        "status_display_type": sd, "paused": bool(paused),
        "buttons": buttons, "own_buttons": active_buttons(s), "details_url": details_url,
        "badge": badge_kind if cover_url or icons is None else None, "badge_colors": badge_cols,
        "small_image": small_url, "small_text": small_text if small_url else None,
    }
    # what must be re-sent when it changes (the timestamps are compared separately, with tolerance)
    act["sig"] = (act["details"], act["state"], act["large_text"], act["large_image"],
                  act["small_image"], act["small_text"], act["details_url"], sd, int(dur), start is not None, end is not None,
                  tuple((b["label"], b["url"]) for b in act["buttons"]))
    return act


_RPC_PARAMS = None
_warned_sd = False
_warned_btn = False


class _EnumLike:
    """Stand-in for pypresence's StatusDisplayType member (newer pypresence reads `.value` on it)."""

    def __init__(self, value):
        self.value = int(value)

    def __int__(self):
        return self.value

    __index__ = __int__

    def __bool__(self):
        return True


def _sd_candidates(sd):
    """Ways to hand 'status display type' to pypresence, best first.  pypresence calls `.value` on the argument,
    so a bare int raises AttributeError ('int' object has no attribute 'value') - the 'Lost connection to Discord
    (AttributeError)' you got after switching to a preset that changes this option."""
    out = []
    if StatusDisplayType is not None:
        try:
            out.append(StatusDisplayType(sd))
        except Exception:
            pass
    out.append(_EnumLike(sd))
    out.append(sd)
    return out


def send_activity(rpc, act):
    """Sends one activity. Optional fields are only used when the installed pypresence supports them."""
    global _RPC_PARAMS, _warned_sd, _warned_btn
    if _RPC_PARAMS is None:
        try:
            _RPC_PARAMS = set(inspect.signature(rpc.update).parameters)
        except Exception:
            _RPC_PARAMS = set()
    kw = {"activity_type": ActivityType.LISTENING}
    for k in ("details", "state", "large_image", "large_text", "small_image", "small_text", "start", "end"):
        if act.get(k) is not None:
            kw[k] = act[k]
    if act.get("details_url") and "details_url" in _RPC_PARAMS:
        kw["details_url"] = act["details_url"]
    if act.get("buttons"):
        if "buttons" in _RPC_PARAMS:
            kw["buttons"] = act["buttons"]
        elif not _warned_btn:
            _warned_btn = True
            log_error("buttons aren't supported by the installed pypresence - update it (pip install -U pypresence)")
    sd = act.get("status_display_type") or 0
    tries = [None]
    if sd:
        if "status_display_type" in _RPC_PARAMS:
            tries = _sd_candidates(sd) + [None]  # last resort: send without it rather than fail
        elif not _warned_sd:
            _warned_sd = True
            log_error("status_display_type isn't supported by the installed pypresence - update it to use that option")
    resp, last = None, None
    for cand in tries:
        kw2 = dict(kw)
        if cand is not None:
            kw2["status_display_type"] = cand
        try:
            resp = rpc.update(**kw2)
            last = None
            break
        except (TypeError, ValueError, AttributeError) as e:
            last = e  # wrong shape for this pypresence version: try the next form
    if last is not None:
        raise last
    _report_asset(act, resp)
    return resp


def _report_asset(act, resp):
    """Discord answers SET_ACTIVITY with the activity it stored; an accepted external image comes back as
    'mp:external/...'.  That reply is the only programmatic proof Discord took the cover."""
    url = act.get("large_image")
    if not url:
        log_info("DISCORD PRESENCE SET (no cover art for this track)")
        return
    try:
        data = (resp or {}).get("data") or {}
        err = (resp or {}).get("evt") == "ERROR" or data.get("code")
        got = (data.get("assets") or {}).get("large_image")
    except Exception:
        data, err, got = None, False, None
    if err:
        log_info(f"DISCORD REJECTED ACTIVITY \u2717 {str(resp)[:200]}")
    elif got:
        log_info(f"DISCORD ASSET SET \u2713 large_image={url} -> {got}")
    elif data is None or not isinstance(resp, dict):
        log_info(f"DISCORD ASSET SENT (no reply to inspect) large_image={url}")
    else:
        log_info(f"DISCORD ASSET NOT ACCEPTED \u2717 sent {url} but Discord stored no large_image: {str(resp)[:200]}")


# ------------------------------------------------------- online fallback --
def _is_ssl_error(e):
    import ssl
    return isinstance(e, ssl.SSLError) or isinstance(getattr(e, "reason", None), ssl.SSLError)


def _curl_get(url, timeout):
    """Windows' own curl.exe checks certificates the way a browser does (Windows certificate store).
    Safety net for PCs where Python rejects a certificate that is actually fine."""
    import subprocess
    exe = "curl.exe" if sys.platform == "win32" else "curl"
    p = subprocess.run([exe, "-sS", "-L", "--fail", "--max-time", str(timeout), "-A", "LocalPresence/3.0", url],
                       capture_output=True, timeout=timeout + 5,
                       creationflags=0x08000000 if sys.platform == "win32" else 0)
    if p.returncode != 0:
        raise OSError("curl: " + (p.stderr or b"").decode("utf-8", "replace").strip()[:200])
    return p.stdout


def _http_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "LocalPresence/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except Exception as e:
        if not _is_ssl_error(e):
            raise
        log_error(f"python TLS check failed for {url} ({e}); retrying with the system's curl")
        return _curl_get(url, 30)


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "LocalPresence/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.load(r)
    except Exception as e:
        if not _is_ssl_error(e):
            raise
        log_error(f"python TLS check failed for {url} ({e}); retrying with the system's curl")
        return json.loads(_curl_get(url, 15).decode("utf-8"))


def lookup_itunes(title, artist):
    q = urllib.parse.urlencode({"term": f"{artist} {title}".strip(), "entity": "song", "limit": 5})
    for r in _http_json(f"https://itunes.apple.com/search?{q}").get("results", []):
        if _match(title, artist, r.get("trackName"), r.get("artistName")):
            url = (r.get("artworkUrl100") or "").replace("100x100", "512x512") or None
            album = re.sub(r"\s+-\s+(Single|EP)$", "", r.get("collectionName") or "", flags=re.I)
            return {"url": url, "album": album or None, "dur": (r.get("trackTimeMillis") or 0) / 1000.0}
    return None


def lookup_deezer(title, artist):
    term = f'artist:"{artist}" track:"{title}"' if artist else title
    q = urllib.parse.urlencode({"q": term, "limit": 5})
    for r in _http_json(f"https://api.deezer.com/search?{q}").get("data", []):
        if _match(title, artist, r.get("title"), (r.get("artist") or {}).get("name")):
            alb = r.get("album") or {}
            return {"url": alb.get("cover_xl") or alb.get("cover_big") or None,
                    "album": alb.get("title") or None, "dur": float(r.get("duration") or 0)}
    return None


def lookup_online(title, artist):
    for fn in (lookup_itunes, lookup_deezer):
        try:
            r = fn(title, artist)
        except Exception:
            r = None
        if r and (r["url"] or r["album"]):
            return r
    return None


def fetch_app_name(cid):
    """The name Discord shows after 'Listening to' (your Discord application's name)."""
    try:
        return _http_json(f"https://discord.com/api/v10/applications/{cid}/rpc").get("name") or None
    except Exception:
        return None


# --------------------------------------------- getting local art onto Discord --
# Discord can only show an image it can download itself, so a cover read from
# your file is shrunk to a 512x512 JPEG, uploaded to an anonymous image host,
# the returned link is CHECKED (https, HTTP 200, really an image) and only then
# cached and handed to Rich Presence.  Several hosts are tried in turn.
CACHE_VERSION = 2          # bumping this discards link caches written by older versions
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalPresence/3.1"
MAX_COVER_BYTES = 450_000  # prepared JPEG is squeezed below this
MAX_SOURCE_PIXELS = 60_000_000
ART_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "MPO"}


def log_info(msg):
    """Diagnostics: always into the log file, and onto the console when there is one."""
    log_error(msg)
    try:
        if sys.stdout:
            print(msg, flush=True)
    except Exception:  # cp1252 consoles can't print the check mark
        try:
            print(msg.encode("ascii", "replace").decode(), flush=True)
        except Exception:
            pass


def describe_image(data):
    """'JPEG 1200x1200, 233 KB' for good artwork; raises ValueError with the reason for bad artwork."""
    if not data or len(data) < 64:
        raise ValueError("artwork is empty / truncated")
    try:
        im = Image.open(io.BytesIO(data))
        fmt, size = im.format, im.size
        im.verify()  # structural check (truncated / corrupt files)
    except Exception as e:
        raise ValueError(f"not a readable image ({type(e).__name__}: {e})")
    if fmt not in ART_FORMATS:
        raise ValueError(f"unsupported image format {fmt}")
    if size[0] * size[1] > MAX_SOURCE_PIXELS:
        raise ValueError(f"image is absurdly large ({size[0]}x{size[1]})")
    return f"{fmt} {size[0]}x{size[1]}, {len(data) // 1024} KB"


def prepare_cover(data, size=512):
    """Any PNG/JPEG/WebP/... -> square JPEG (<= `size` px, < MAX_COVER_BYTES). Raises ValueError on bad art."""
    describe_image(data)  # validates (raises ValueError)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img) or img
        if img.mode in ("RGBA", "LA", "P", "PA"):
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))  # flatten transparency onto white, not black
            img = Image.alpha_composite(bg, img)
        side = max(64, min(size, *img.size))
        img = ImageOps.fit(img.convert("RGB"), (side, side), Image.LANCZOS)
    except Exception as e:
        raise ValueError(f"image could not be converted ({type(e).__name__}: {e})")
    out = b""
    for q in (90, 82, 74, 66, 55):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        out = buf.getvalue()
        if len(out) <= MAX_COVER_BYTES:
            break
    return out


def _multipart(fields, field, fname, fdata, ctype="image/jpeg"):
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(
        f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n".encode()
    )
    parts.append(fdata)
    parts.append(f"\r\n--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


# Each host: how to upload, and how to turn its reply into a DIRECT image link.
def _first_url(text):
    m = re.search(r"https?://[^\s\"'<>\\]+", text)
    if not m:
        raise ValueError("no link in reply: " + text.strip()[:80])
    return m.group(0)


def _parse_text(text):
    return _first_url(text)


def _parse_uguu(text):
    j = json.loads(text)
    return j["files"][0]["url"]


def _parse_tmpfiles(text):
    url = json.loads(text)["data"]["url"]
    return re.sub(r"^https?://tmpfiles\.org/", "https://tmpfiles.org/dl/", url)  # page link -> direct link


UPLOADERS = [
    # name, url, extra form fields, file field, reply parser, hours the link stays valid (0 = permanent)
    ("litterbox", "https://litterbox.catbox.moe/resources/internals/api.php",
     {"reqtype": "fileupload", "time": "72h"}, "fileToUpload", _parse_text, 72),
    ("catbox", "https://catbox.moe/user/api.php", {"reqtype": "fileupload"}, "fileToUpload", _parse_text, 0),
    ("uguu", "https://uguu.se/upload", {}, "files[]", _parse_uguu, 3),
    ("tmpfiles", "https://tmpfiles.org/api/v1/upload", {}, "file", _parse_tmpfiles, 1),
    ("0x0", "https://0x0.st", {}, "file", _parse_text, 24 * 20),
]


def verify_public_url(url, expect=None, tries=2):
    """Fetches `url` the way Discord's image proxy will. Returns (ok, reason)."""
    if not url.lower().startswith("https://"):
        return False, "not https"
    if len(url) > 256:
        return False, f"URL is {len(url)} chars (Discord max 256)"
    last = "unknown"
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": WEB_UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                final, ctype, body = r.geturl(), (r.headers.get("Content-Type") or "").lower(), r.read(3_000_000)
            if not final.lower().startswith("https://"):
                return False, "redirects to non-https " + final[:60]
            if r.status != 200:
                last = f"HTTP {r.status}"
            elif not ctype.startswith("image/"):
                last = f"served as {ctype or 'unknown type'}, not an image"
            elif not body:
                last = "empty body"
            else:
                try:
                    describe_image(body)
                except ValueError as e:
                    last = "downloaded file is not an image: " + str(e)
                else:
                    return True, f"HTTP 200 {ctype}, {len(body) // 1024} KB"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403, 429, 503):
                return True, f"host refused my check (HTTP {e.code}) - assuming OK, Discord fetches it from its own servers"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if isinstance(e, (OSError,)) and not isinstance(e, urllib.error.HTTPError):
                return True, f"couldn't reach host to double-check ({type(e).__name__}) - assuming OK"
        time.sleep(1.0 + i)  # a fresh upload can take a moment to become visible
    return False, last


class CoverHost:
    def __init__(self):
        self.db = {}
        self.last_error = ""
        try:
            with open(COVER_DB_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # entries written by older versions were never verified -> start clean
            self.db = {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("v") == CACHE_VERSION}
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(COVER_DB_PATH, "w", encoding="utf-8") as f:
                json.dump(self.db, f)
        except Exception:
            pass

    @staticmethod
    def _alive(url):
        """Cached link still serves an image? Only a definite 'gone' answer counts as dead."""
        ok, why = verify_public_url(url, tries=1)
        if ok:
            return True
        return not why.startswith(("HTTP 403", "HTTP 404", "HTTP 410", "served as", "downloaded file is not", "empty body"))

    def _upload_one(self, name, url, fields, field, parse, img):
        body, ctype = _multipart(fields, field, "cover.jpg", img)
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype, "User-Agent": WEB_UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                text = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} {e.read(120).decode('utf-8', 'ignore').strip()[:80]}")
        return parse(text.strip()).strip()

    def url_for(self, data, label=""):
        key = hashlib.sha1(data).hexdigest()
        rec, now = self.db.get(key), time.time()
        if rec and (rec["ttl"] == 0 or now - rec["ts"] < rec["ttl"] * 3600 * 0.85):
            if now - rec.get("chk", rec["ts"]) > 6 * 3600:  # make sure the host still serves it
                if self._alive(rec["url"]):
                    rec["chk"] = now
                    self._save()
                    log_info(f"ARTWORK CACHED \u2713 (no re-upload)  PUBLIC URL: {rec['url']}")
                    return rec["url"]
                log_info("ARTWORK CACHE STALE - link is dead, uploading again")
                self.db.pop(key, None)
            else:
                log_info(f"ARTWORK CACHED \u2713 (no re-upload)  PUBLIC URL: {rec['url']}")
                return rec["url"]
        try:
            img = prepare_cover(data)
        except ValueError as e:
            self.last_error = str(e)
            log_info(f"ARTWORK INVALID \u2717 {self.last_error}")
            return None
        errors = []
        for name, url, fields, field, parse, ttl in UPLOADERS:
            try:
                link = self._upload_one(name, url, fields, field, parse, img)
                link = re.sub(r"^http://", "https://", link)
                log_info(f"ARTWORK UPLOADED \u2713 ({name}, {len(img) // 1024} KB)")
                ok, why = verify_public_url(link, expect=img)
                if not ok:
                    errors.append(f"{name}: uploaded but link unusable ({why})")
                    log_info(f"ARTWORK LINK REJECTED \u2717 {link} - {why}")
                    continue
                log_info(f"PUBLIC URL: {link}  [{why}]")
                self.db[key] = {"url": link, "ts": now, "ttl": ttl, "chk": now, "v": CACHE_VERSION, "host": name}
                if len(self.db) > 400:
                    for k in sorted(self.db, key=lambda k: self.db[k]["ts"])[:100]:
                        self.db.pop(k, None)
                self._save()
                self.last_error = ""
                return link
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
                log_info(f"ARTWORK UPLOAD FAILED \u2717 {errors[-1]}")
        self.last_error = "; ".join(errors)
        log_error("cover upload failed - " + self.last_error)
        return None


class CoverService(threading.Thread):
    """Background thread that turns a cover into a Discord-ready link (and fills in gaps online)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.res = {}
        self.pending = set()
        self.host = CoverHost()

    def request(self, key, title, artist, img, upload, lookup, need_meta):
        if key in self.pending:
            return
        r = self.res.get(key)
        if r is not None and (r.get("url") or time.time() - r["ts"] < 90):
            return
        self.pending.add(key)
        self.q.put((key, title, artist, img, upload, lookup, need_meta))

    def get(self, key):
        return self.res.get(key)

    def run(self):
        while True:
            job = self.q.get()
            key = job[0]
            try:
                r = self.work(*job)
            except Exception as e:
                log_error(f"cover service: {type(e).__name__}: {e}")
                r = {"url": None, "note": f"{type(e).__name__}: {e}"}
            r["ts"] = time.time()
            if len(self.res) > 80:
                self.res.clear()
            self.res[key] = r
            self.pending.discard(key)

    def work(self, key, title, artist, img, upload, lookup, need_meta):
        out = {"url": None, "album": None, "dur": 0.0, "bytes": None, "note": ""}
        if img and upload:
            url = self.host.url_for(img, title)
            if url:
                out["url"] = url
            else:
                out["note"] = "Couldn't upload the cover for Discord: " + (self.host.last_error or "unknown error")
        if lookup and (not out["url"] or need_meta or not img):
            r = lookup_online(title, artist)
            if r:
                out["album"], out["dur"] = r.get("album"), r.get("dur") or 0.0
                if not out["url"] and r.get("url"):
                    out["url"] = r["url"]
                    out["note"] = ""
                    if not img:
                        try:
                            req = urllib.request.Request(r["url"], headers={"User-Agent": "LocalPresence/3.0"})
                            with urllib.request.urlopen(req, timeout=8) as resp:
                                out["bytes"] = resp.read()
                        except Exception:
                            pass
        return out


# ------------------------------------------------------------- song links --
# (hint in the player's name, Odesli platform key, label)
_STREAM_PLAYERS = (
    ("spotify", "spotify", "Spotify"), ("tidal", "tidal", "TIDAL"), ("deezer", "deezer", "Deezer"),
    ("apple", "appleMusic", "Apple Music"), ("itunes", "appleMusic", "Apple Music"),
    ("amazon", "amazonMusic", "Amazon Music"),
)
_BROWSERS = ("chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi", "arc", "zen")


def stream_platform(app):
    """(odesli key, nice name) when this player streams songs; ('', '') for browsers; None for local players."""
    a = (app or "").lower()
    for hint, key, name in _STREAM_PLAYERS:
        if hint in a:
            return key, name
    if any(b in a for b in _BROWSERS):
        return "", ""
    return None


def _source_song_url(title, artist):
    """A catalogue link for the song (Apple Music, else Deezer) that the link resolver understands."""
    try:
        q = urllib.parse.urlencode({"term": f"{artist} {title}".strip(), "entity": "song", "limit": 5})
        for r in _http_json(f"https://itunes.apple.com/search?{q}").get("results", []):
            if r.get("trackViewUrl") and _match(title, artist, r.get("trackName"), r.get("artistName")):
                return r["trackViewUrl"]
    except Exception:
        pass
    try:
        term = f'artist:"{artist}" track:"{title}"' if artist else title
        q = urllib.parse.urlencode({"q": term, "limit": 5})
        for r in _http_json(f"https://api.deezer.com/search?{q}").get("data", []):
            if r.get("link") and _match(title, artist, r.get("title"), (r.get("artist") or {}).get("name")):
                return r["link"]
    except Exception:
        pass
    return None


def find_song_link(title, artist, plat):
    """{'url', 'label'} that opens this exact song on the player's own service, or None."""
    key, name = plat
    if not title:
        return None
    src = _source_song_url(title, artist)
    if src:
        try:
            q = urllib.parse.urlencode({"url": src, "userCountry": "US"})
            data = _http_json("https://api.song.link/v1-alpha.1/links?" + q)
            by = data.get("linksByPlatform") or {}
            own = (by.get(key) or {}).get("url") if key else None
            url = clean_url(own) if own else ""
            if url:
                return {"url": url, "label": f"Listen on {name}"}
            page = clean_url(data.get("pageUrl") or "")
            if page:
                return {"url": page, "label": "Listen on " + name if name else "Open this song"}
        except Exception as e:
            log_info(f"SONG LINK resolver failed ({type(e).__name__}: {e}) - using the catalogue link")
        url = clean_url(src)
        if url:
            return {"url": url, "label": "Open this song"}
    if key == "spotify":      # nothing found: at least land on the search results for it
        url = clean_url("https://open.spotify.com/search/" + urllib.parse.quote(f"{artist} {title}".strip()))
        if url:
            return {"url": url, "label": "Search on Spotify"}
    return None


class LinkService(threading.Thread):
    """Looks up 'open this song' links in the background and remembers them."""

    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.res = {}
        self.pending = set()

    def request(self, key, title, artist, plat):
        if key in self.pending:
            return
        r = self.res.get(key)
        if r is not None and (r.get("url") or time.time() - r["ts"] < 90):
            return
        self.pending.add(key)
        self.q.put((key, title, artist, plat))

    def get(self, key):
        return self.res.get(key)

    def run(self):
        while True:
            key, title, artist, plat = self.q.get()
            try:
                r = find_song_link(title, artist, plat) or {}
            except Exception as e:
                log_error(f"song link: {type(e).__name__}: {e}")
                r = {}
            r["ts"] = time.time()
            if len(self.res) > 200:
                self.res.clear()
            self.res[key] = r
            self.pending.discard(key)
            if r.get("url"):
                log_info(f"SONG LINK \u2713 {title} - {artist}: {r['url']}")
            time.sleep(1.0)       # the free resolver allows about 10 requests a minute


# ------------------------------------------------------------------ worker --
SEEK_TOLERANCE = 2.0   # seconds of drift before the progress bar is re-synced (seek / pause+resume)
MIN_SEND_GAP = 3.0     # Discord allows about 5 presence updates per 20 s
COVER_HOLD = 4.0       # seconds a new song waits for its cover upload before the first update

class Worker(threading.Thread):
    """Polls the OS media session and drives Discord Rich Presence."""

    def __init__(self, get_settings, ui_queue, library):
        super().__init__(daemon=True)
        self.get_settings = get_settings
        self.ui = ui_queue
        self.library = library
        self.covers = CoverService()
        self.covers.start()
        self.links = LinkService()
        self.links.start()
        self.enabled = threading.Event()
        self.stop_evt = threading.Event()
        self.meta_cache = {}
        self.helper = None
        self.helper_started = 0.0
        self.data = None
        self.ps = None
        self.ps_started = 0.0
        self.title_key = None
        self.title_since = 0.0
        self.seen_apps = []
        self.diag = ""
        self.hold = threading.Event()           # presence paused: stay connected, show nothing
        self.privacy = threading.Event()        # manual privacy mode: same effect, switched from tray / hotkey
        self.auto_hide = threading.Event()      # set by GameWatcher (game running / fullscreen / quiet hours)
        self.auto_reason = ""
        self.wake = threading.Event()           # cut the current wait short
        self.reconnect_req = threading.Event()
        self.conn = "off"                       # off | connecting | connected | waiting | invalid
        self.last_alive_check = 0.0

    def hide_reason(self):
        """Why the status is being hidden on purpose ('' = it isn't)."""
        if self.privacy.is_set():
            return "privacy mode"
        if self.auto_hide.is_set():
            return self.auto_reason or "auto-hide"
        return ""

    def set_conn(self, state):
        if state != self.conn:
            self.conn = state
            self.ui.put(("conn", state))

    def request_reconnect(self):
        self.reconnect_req.set()
        self.wake.set()

    def _nap(self, secs):
        """Sleep, but wake early for a reconnect request / toggle / shutdown."""
        if not isinstance(self.stop_evt, threading.Event):
            self.stop_evt.wait(secs)
            return
        end = time.time() + secs
        while not self.stop_evt.is_set() and not self.wake.is_set():
            left = end - time.time()
            if left <= 0:
                break
            self.stop_evt.wait(min(0.2, left))
        self.wake.clear()

    @staticmethod
    def _alive(rpc):
        """True unless Discord's pipe is known to be closed (Discord quit / restarted)."""
        try:
            loop = getattr(rpc, "loop", None)
            if isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_running():
                loop.run_until_complete(asyncio.sleep(0))   # let the loop notice a closed pipe
            w, r = getattr(rpc, "sock_writer", None), getattr(rpc, "sock_reader", None)
            if w is not None and getattr(w, "transport", None) is not None and w.transport.is_closing() is True:
                return False
            if r is not None and r.at_eof() is True:
                return False
        except Exception:
            pass
        return True

    # -- media (read from helper processes) --------------------------------
    def ensure_helper(self):
        if self.helper is not None and self.helper.poll() is None:
            return
        if self.helper is not None and self.data and self.data.get("error"):
            return  # it already failed and exited; the PowerShell reader takes over
        if time.time() - self.helper_started < 5:
            return
        self.helper_started = time.time()
        import subprocess

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, HELPER_FLAG, str(os.getpid())]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), HELPER_FLAG, str(os.getpid())]
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        self.helper = subprocess.Popen(
            cmd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )

    def ensure_ps(self):
        if self.ps is not None and self.ps.poll() is None:
            return
        if time.time() - self.ps_started < 10:
            return
        self.ps_started = time.time()
        import subprocess

        os.makedirs(CONFIG_DIR, exist_ok=True)
        script = os.path.join(CONFIG_DIR, "smtc.ps1")
        with open(script, "w", encoding="utf-8") as f:
            f.write(PS_SCRIPT)
        exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        self.ps = subprocess.Popen(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
             "-ParentPid", str(os.getpid()), "-Out", NOW_PS_PATH],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )

    def stop_helper(self):
        for proc in (self.helper, self.ps):
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    @staticmethod
    def load_json(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def usable(d):
        try:
            return bool(d) and not d.get("error") and time.time() - d["ts"] < 15
        except Exception:
            return False

    def read_titles(self, s):
        if sys.platform != "win32":
            return None
        try:
            wins = list_windows()
        except Exception:
            return None
        allow = [x.strip().lower() for x in s["allow"].split(",") if x.strip()]
        block = [x.strip().lower() for x in s["block"].split(",") if x.strip()]
        found = None
        for exe, title in wins:
            if exe not in PLAYER_SUFFIX:
                continue
            if allow and not any(x in exe for x in allow):
                continue
            if any(x in exe for x in block):
                continue
            parsed = parse_title(exe, title, s.get("title_order", TITLE_ORDERS[0]))
            if parsed:
                found = (exe, parsed)
                break
        if not found:
            self.title_key = None
            return None
        exe, (song, artist) = found
        key = (exe, song, artist)
        if key != self.title_key:
            self.title_key = key
            self.title_since = time.time()
        return {
            "app": exe + " (window title mode)",
            "title": song,
            "artist": artist,
            "album": "",
            "pos": time.time() - self.title_since,
            "dur": 0.0,
            "playing": True,
            "thumb": "",
            "t": time.time(),
        }

    def read(self, s):
        self.ensure_helper()
        a = self.load_json(NOW_PATH)
        if a is not None:
            self.data = a
        a = self.data
        if a and a.get("error"):
            self.ensure_ps()  # the Python reader failed; use PowerShell instead
        b = self.load_json(NOW_PS_PATH) if self.ps_started else None

        if self.usable(a):
            data, age = a, time.time() - a["ts"]
        elif self.usable(b):
            data, age = b, time.time() - b["ts"]
        else:
            msgs = []
            if a and a.get("error"):
                msgs.append("winsdk: " + str(a["error"]))
            if b and b.get("error"):
                msgs.append("PowerShell: " + str(b["error"]))
            now = time.time()
            if now - self.helper_started < 15 or (self.ps_started and now - self.ps_started < 12):
                if not (b and b.get("error")):
                    return None  # still starting up
            info = self.read_titles(s)
            if info:
                return info
            raise RuntimeError(
                ("; ".join(msgs) or "Media helper isn't responding")
                + " | No supported player window found either."
            )

        allow = [x.strip().lower() for x in s["allow"].split(",") if x.strip()]
        block = [x.strip().lower() for x in s["block"].split(",") if x.strip()]
        chosen = None
        self.seen_apps = [i.get("app") for i in (data.get("sessions") or []) if i.get("app")]
        sess = data.get("sessions") or []
        parts = [f"Windows reports {len(sess)} media session(s)"]
        for i in sess:
            low = (i.get("app") or "?")
            why = ""
            if i.get("error"):
                why = f" [error: {i['error']}]"
            elif allow and not any(x in low.lower() for x in allow):
                why = " [IGNORED: not in your 'Only these' list in Settings]"
            elif any(x in low.lower() for x in block):
                why = " [IGNORED: in your 'Ignore these' list in Settings]"
            elif not (i.get("title") or ""):
                why = " [no title published]"
            parts.append(f"{low}: '{i.get('title') or ''}' {'playing' if i.get('playing') else 'paused/stopped'}{why}")
        if not sess:
            parts.append("your player isn't publishing to Windows media controls (or the helper that reads them "
                         "isn't running) - press play in Spotify and check Windows' volume flyout shows the song")
        self.diag = " | ".join(parts)
        if self.diag != getattr(self, "_diag_logged", None):
            self._diag_logged = self.diag
            log_error("media: " + self.diag)
        for item in data.get("sessions") or []:
            if item.get("error"):
                continue
            low = (item.get("app") or "").lower()
            if allow and not any(x in low for x in allow):
                continue
            if any(x in low for x in block):
                continue
            playing = bool(item.get("playing"))
            if chosen is None or (playing and not chosen["playing"]):
                chosen = {
                    "app": item.get("app") or "",
                    "title": item.get("title") or "",
                    "artist": item.get("artist") or "",
                    "album": item.get("album") or "",
                    "pos": float(item.get("pos") or 0),
                    "dur": float(item.get("dur") or 0),
                    "playing": playing,
                    "thumb": item.get("thumb") or "",
                }
            if playing:
                break
        if not chosen:
            return None
        if chosen["playing"]:
            chosen["pos"] += age
            if chosen["dur"] > 0:
                chosen["pos"] = min(chosen["pos"], chosen["dur"])
        chosen["t"] = time.time()  # the position above is valid at this moment
        return chosen

    # -- find the local file for what the player reports ----------------------
    def find_file(self, info):
        t, a = info["title"], info["artist"]
        tries = [(t, a, 0)]
        if " - " in t:  # some players put "Artist - Title" into the title
            left, right = t.split(" - ", 1)
            tries.append((right, left, 10))
        if a:  # window-title mode may have the order flipped
            tries.append((a, t, 10))
        best, best_score = None, 0
        for tt, aa, penalty in tries:
            rec, sc = self.library.find_ex(tt, aa, info["album"], info["dur"])
            if rec and sc - penalty >= MIN_MATCH_SCORE and sc - penalty > best_score:
                best, best_score = rec, sc - penalty
        return best, best_score

    # -- cover art / album / length: your files first, then player, then online --
    def enrich(self, info, s):
        prefer = bool(s.get("prefer_file_tags", True))
        key = (info["title"], info["artist"], info["album"], prefer, bool(s.get("folder_art", True)))
        m = self.meta_cache.get(key)
        if m is not None and m["file"] is None and m["gen"] != self.library.gen:
            m = None  # the library finished (re)scanning since we last looked
        if m is None:
            m = {
                "title": info["title"], "artist": info["artist"], "album": info["album"],
                "albumartist": "", "year": "",
                "cover_bytes": None, "cover_src": "", "cover_url": None, "cover_note": "",
                "cover_pending": False,
                "album_src": "player" if info["album"] else "",
                "dur": info["dur"], "dur_src": "player" if info["dur"] > 0 else "",
                "file": None, "score": 0, "gen": self.library.gen, "thumb_tried": None,
            }
            rec, score = (self.find_file(info) if s.get("use_library", False) else (None, 0))
            if rec:
                m["file"], m["score"] = rec["path"], score
                # the tags inside your file are the authority; the player only fills what the file lacks
                def pick(field):
                    fv = (rec.get(field) or "").strip()
                    if fv and (prefer or not m[field]):
                        m[field] = fv
                        return True
                    return False

                if rec.get("tagged"):
                    pick("title")
                pick("artist")
                if pick("album"):
                    m["album_src"] = "your file"
                m["albumartist"] = rec.get("albumartist", "") or ""
                m["year"] = rec.get("year", "") or ""
                fd = float(rec.get("dur") or 0)
                if fd > 0 and (prefer or m["dur"] <= 0):
                    m["dur"], m["dur_src"] = fd, "your file"
                try:
                    data = read_cover(rec["path"])
                except Exception as e:
                    data = None
                    log_error(f"cover {os.path.basename(rec['path'])}: {type(e).__name__}: {e}")
                if data:
                    m["cover_bytes"], m["cover_src"] = data, "tags in " + os.path.basename(rec["path"])
                elif s.get("folder_art", True):
                    data = folder_cover(rec["path"])
                    if data:
                        m["cover_bytes"], m["cover_src"] = data, "folder image next to the file"
            if len(self.meta_cache) > 60:
                self.meta_cache.clear()
            self.meta_cache[key] = m

        # the picture the player itself publishes (the helper writes it a moment after a track change)
        if not m["cover_bytes"] and info.get("thumb") and m["thumb_tried"] != info["thumb"]:
            m["thumb_tried"] = info["thumb"]
            try:
                with open(info["thumb"], "rb") as f:
                    data = f.read()
                if data:
                    m["cover_bytes"], m["cover_src"] = data, "player thumbnail"
            except OSError:
                pass

        # slow part (upload / online lookup) runs in the background
        if s["show_cover"] or not m["album"] or m["dur"] <= 0:
            need_meta = (not m["album"]) or m["dur"] <= 0
            if m["cover_bytes"] and m.get("_hb") is not m["cover_bytes"]:
                m["_hb"], m["_h"] = m["cover_bytes"], hashlib.sha1(m["cover_bytes"]).hexdigest()[:16]
                try:
                    log_info(f"ARTWORK EXTRACTED \u2713 {m['title']} - {m['artist']}: "
                             f"{describe_image(m['cover_bytes'])} ({m['cover_src']})")
                except ValueError as e:
                    log_info(f"ARTWORK INVALID \u2717 {m['title']} - {m['artist']} ({m['cover_src']}): {e}")
            elif not m["cover_bytes"] and not m.get("_nolog"):
                m["_nolog"] = True
                log_info(f"ARTWORK MISSING - {m['title']} - {m['artist']}: no embedded/folder/player art"
                         + (" (will try online lookup)" if s["online_lookup"] else ""))
            # keyed by the picture itself so a different image can never reuse another song's link
            ckey = key + (m.get("_h") if m["cover_bytes"] else "",)
            self.covers.request(
                ckey, m["title"], m["artist"], m["cover_bytes"] if s["show_cover"] else None,
                bool(s["upload_covers"]) and bool(s["show_cover"]), bool(s["online_lookup"]),
                need_meta or not m["cover_bytes"],
            )
            res = self.covers.get(ckey)
            m["cover_pending"] = bool(s["show_cover"]) and res is None and ckey in self.covers.pending
            if res is None and m.get("cover_url_key") != ckey:
                m["cover_url"] = None  # never keep showing the previous image while the new one uploads
            if res:
                if s["show_cover"]:
                    m["cover_url"], m["cover_note"] = res.get("url"), res.get("note") or ""
                    m["cover_url_key"] = ckey
                if not m["album"] and res.get("album"):
                    m["album"], m["album_src"] = res["album"], "online"
                if m["dur"] <= 0 and res.get("dur"):
                    m["dur"], m["dur_src"] = res["dur"], "online"
                if not m["cover_bytes"] and res.get("bytes"):
                    m["cover_bytes"], m["cover_src"] = res["bytes"], "online lookup"
        else:
            m["cover_pending"] = False

        # tiny badges (pause / player logo): uploaded once through the same host as covers, then cached
        icons = {}
        try:
            if s["show_cover"] and (s.get("paused_badge", True) or s.get("player_badge", True)):
                kinds = ["pause"] if s.get("paused_badge", True) else []
                pk = player_kind(info.get("app"))
                if pk and s.get("player_badge", True):
                    kinds.append(pk)
                bgc, fgc = badge_colors(s, m.get("cover_bytes"))
                if s.get("spotify_dynamic") and not m.get("cover_bytes") and "spotify" in kinds:
                    kinds.remove("spotify")   # wait for the cover so the logo never flashes the wrong colours
                for k in kinds:
                    ikey = ("badge", k, bgc, fgc) if k == "spotify" else ("badge", k)
                    if ikey not in self.covers.pending and self.covers.get(ikey) is None:
                        self.covers.request(ikey, k, "", badge_bytes(k, bgc, fgc), bool(s["upload_covers"]), False, False)
                    res = self.covers.get(ikey)
                    if res and res.get("url"):
                        icons[k] = res["url"]
        except Exception as e:
            log_error(f"badges: {type(e).__name__}: {e}")
        m["icons"] = icons

        # "open this song" link for streaming players (local files have nothing to open)
        m["song_link"], m["link_pending"] = None, False
        try:
            plat = stream_platform(info.get("app"))
            if s.get("song_link", True) and plat is not None and not m.get("file") and m.get("title"):
                lkey = (m["title"], m.get("artist") or "", plat[0])
                self.links.request(lkey, m["title"], m.get("artist") or "", plat)
                res = self.links.get(lkey)
                if res and res.get("url"):
                    m["song_link"] = res
                m["link_pending"] = res is None and lkey in self.links.pending
        except Exception as e:
            log_error(f"song link: {type(e).__name__}: {e}")
        return m

    # -- main loop ---------------------------------------------------------
    def status(self, text, color):
        self.ui.put(("status", text, color))

    def run(self):
        self.last_err = None
        for _p in (NOW_PATH, NOW_PS_PATH):
            try:
                os.remove(_p)
            except OSError:
                pass
        rpc_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(rpc_loop)
        rpc = None
        rpc_id = None
        shown = None  # {"sig", "start"} of what Discord currently displays
        last_sent = 0.0
        idle_since = None
        track_id, track_since = None, 0.0

        def close_rpc():
            nonlocal rpc
            if rpc is not None:
                try:
                    rpc.clear()
                    rpc.close()
                except Exception:
                    pass
            rpc = None

        while not self.stop_evt.is_set():
            s = self.get_settings()
            err = None
            try:
                info = self.read(s)
            except Exception as e:
                info = None
                err = f"{type(e).__name__}: {e}"
                if err != self.last_err:
                    log_error(err)
            self.last_err = err

            has_track = bool(info and info["title"])
            try:
                meta = self.enrich(info, s) if has_track else {"diag": self.diag}
            except Exception as e:  # a cover problem must never stop the presence
                log_error(f"enrich failed: {type(e).__name__}: {e}")
                meta = {"title": info["title"], "artist": info["artist"], "album": info["album"],
                        "dur": info["dur"], "file": None, "cover_url": None, "cover_bytes": None} if has_track else {"diag": self.diag}
            skipped = False
            if False and has_track and s.get("only_library") and not meta.get("file"):
                # "local files only": ignore browsers / streaming apps that aren't in the library
                skipped, has_track, meta = True, False, {"skipped": True}
                info = None
            now = time.time()
            tid = (meta.get("title"), meta.get("artist"), meta.get("file")) if has_track else None
            if tid != track_id:
                track_id, track_since = tid, now
            self.ui.put(("np", info if has_track else None, meta.get("cover_bytes"), now, err, meta))
            playing = has_track and info["playing"]
            cid = s["client_id"].strip()

            if self.reconnect_req.is_set():
                self.reconnect_req.clear()
                close_rpc()
                rpc_id = None
                shown = None

            if not self.enabled.is_set():
                close_rpc()
                rpc_id = None
                shown = None
                self.set_conn("off")
                self.status("Presence is off", GREY)
            elif not cid:
                self.set_conn("off")
                self.status("Add your Application ID in Settings", RED)
            else:
                if rpc is None or rpc_id != cid:
                    close_rpc()
                    if self.conn != "waiting":
                        self.set_conn("connecting")
                        self.status("Connecting to Discord...", YELLOW)
                    try:
                        rpc = Presence(cid, loop=rpc_loop)
                        rpc.connect()
                        rpc_id = cid
                        shown = None
                        self.last_alive_check = now
                        self.set_conn("connected")
                    except Exception as e:
                        rpc = None
                        if type(e).__name__ == "InvalidID":
                            self.set_conn("invalid")
                            self.status("Invalid Application ID", RED)
                        else:
                            self.set_conn("waiting")
                            self.status("Waiting for Discord to open...", YELLOW)
                        self._nap(5)
                        continue
                try:
                    if now - self.last_alive_check >= 10:      # Discord quit while nothing was being sent
                        self.last_alive_check = now
                        if not self._alive(rpc):
                            raise ConnectionError("Discord closed")
                    keep_paused = has_track and not playing and s.get("pause_mode") == PAUSE_MODES[1]
                    hide_why = self.hide_reason()
                    if self.hold.is_set() or hide_why:
                        if shown is not None:
                            rpc.clear()
                            shown = None
                        idle_since = None
                        self.status("Presence paused" if self.hold.is_set() else "Hidden: " + hide_why, YELLOW)
                    elif playing or keep_paused:
                        idle_since = None
                        act = build_activity(info, meta, s, now, paused=not playing)
                        changed = shown is None or shown["sig"] != act["sig"]
                        drift = 0.0
                        if shown and shown["start"] and act["start"]:
                            drift = abs(act["start"] - shown["start"])  # seek / pause+resume / skipped back
                        seeked = drift > SEEK_TOLERANCE
                        # give a new track's cover a moment to upload so Discord updates once, not twice
                        hold = (meta.get("cover_pending") or meta.get("link_pending")) and now - track_since < COVER_HOLD
                        if (changed or seeked) and now - last_sent >= MIN_SEND_GAP and not hold:
                            send_activity(rpc, act)
                            shown = {"sig": act["sig"], "start": act["start"]}
                            last_sent = now
                        self.status("Live on Discord" if playing else "Paused (status kept)",
                                    GREEN if playing else YELLOW)
                    else:
                        if idle_since is None:
                            idle_since = now
                        delay = int(s.get("pause_delay", 6)) if has_track else 6
                        # wait a few seconds so the gap between two songs doesn't blink the status
                        if shown is not None and now - idle_since >= delay:
                            rpc.clear()
                            shown = None
                        if skipped:
                            self.status("Skipping a track that isn't in your library", YELLOW)
                        else:
                            self.status("Paused" if has_track else "Connected - nothing playing", YELLOW)
                except Exception as e:
                    log_error(f"discord update: {type(e).__name__}: {e}")
                    close_rpc()
                    rpc_id = None
                    shown = None
                    self.set_conn("waiting")
                    self.status("Discord closed - reconnecting automatically..." if isinstance(e, ConnectionError)
                                else f"Lost connection to Discord ({type(e).__name__}), retrying...", YELLOW)

            self._nap(max(1, int(s["poll_seconds"])))

        close_rpc()
        self.stop_helper()


# --------------------------------------------------------------------- app --
COVER = 124

SAMPLE_INFO = {
    "app": "Sample.exe", "title": "Simple (Living At The Top)", "artist": "Juice WRLD",
    "album": "JW3 (Sessions)", "pos": 8.0, "dur": 234.0, "playing": True, "thumb": "", "t": 0.0,
}
SAMPLE_META = {
    "title": "Simple (Living At The Top)", "artist": "Juice WRLD", "album": "JW3 (Sessions)",
    "albumartist": "Juice WRLD", "year": "2021", "dur": 234.0, "file": "Simple (Living At The Top).flac",
    "cover_url": "https://example.invalid/cover.jpg",
}


def hex_mix(rgb, base, t):
    return "#%02x%02x%02x" % tuple(int(base[i] * (1 - t) + rgb[i] * t) for i in range(3))


def _hls(rgb):
    import colorsys
    return colorsys.rgb_to_hls(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def _hex(h, l, sat):
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h % 1.0, min(max(l, 0), 1), min(max(sat, 0), 1))
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def dominant_color(data):
    """The cover's most characteristic colour: frequent AND colourful, ignoring near-black / near-white.
    Returns (r, g, b) or None for a colourless (grey) cover."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((64, 64), Image.BILINEAR)
        q = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette()[:24]
        counts = sorted(q.getcolors(), reverse=True)
        best, best_score = None, 0.0
        for n, idx in counts:
            rgb = tuple(pal[idx * 3: idx * 3 + 3])
            h, l, sa = _hls(rgb)
            if l < 0.12 or l > 0.92:
                continue
            score = n * (sa ** 1.5) * (1 - abs(l - 0.5))
            if score > best_score:
                best, best_score = rgb, score
        if best is None or _hls(best)[2] < 0.18:
            return None
        return best
    except Exception:
        return None


def make_theme(rgb):
    """Full app palette from one cover colour. Dark surfaces carry a faint tint of its hue."""
    if rgb is None:
        return None
    h, _l, sa = _hls(rgb)
    acc = _hex(h, 0.52, max(0.50, min(sa, 0.85)))
    acc_h = _hex(h, 0.43, max(0.50, min(sa, 0.85)))
    t = min(max(sa, 0.25), 0.45)
    return {
        "BG": _hex(h, 0.065, t), "SIDEBAR": _hex(h, 0.085, t), "PANEL": _hex(h, 0.105, t * 0.9),
        "PANEL2": _hex(h, 0.15, t * 0.8), "CARD": _hex(h, 0.092, t), "HOVER": _hex(h, 0.19, t * 0.7),
        "LINE": _hex(h, 0.20, t * 0.6), "TRACK": _hex(h, 0.20, t * 0.6),
        "ACC": acc, "ACC_H": acc_h,
    }


def _rounded(img, size, radius):
    img = img.convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.putalpha(mask)
    return img


def cover_pil(data, size=COVER):
    """Rounded square cover (drawn at 2x for sharpness) and its average colour."""
    big = size * 2
    avg = None
    try:
        if not data:
            raise ValueError("no cover")
        img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (big, big), Image.LANCZOS)
        avg = img.resize((1, 1), Image.BILINEAR).getpixel((0, 0))
    except Exception:
        img = Image.new("RGB", (big, big), (36, 28, 54))
        d = ImageDraw.Draw(img)
        s = big / 100
        c = (125, 110, 160)
        d.ellipse([30 * s, 60 * s, 48 * s, 76 * s], fill=c)
        d.rectangle([44 * s, 26 * s, 50 * s, 68 * s], fill=c)
        d.polygon([(50 * s, 26 * s), (72 * s, 34 * s), (72 * s, 44 * s), (50 * s, 36 * s)], fill=c)
    return _rounded(img, big, 28), avg


def _plain(text):
    return (text or "").replace("\u00a0", " ").strip()


class DiscordCard(ctk.CTkFrame):
    """The 'Listening to ...' card exactly as Discord lays it out:
    cover on the left; title, artist, album; then elapsed time, progress bar, total time."""

    def __init__(self, master, placeholder, px=COVER):
        super().__init__(master, corner_radius=18, fg_color=CARD, border_width=1, border_color=LINE)
        self.px = px
        self.placeholder = placeholder
        self.cover_ref = None
        self.cover_key = "init"
        self.act = None
        self.use_tint = True
        self.head = ctk.CTkLabel(
            self, text="Listening to your Discord app", font=F(size=12, weight="bold"),
            text_color=SUB, anchor="w", fg_color=CARD,
        )
        self.pause_pill = ctk.CTkLabel(
            self, text="", font=F(size=11, weight="bold"), text_color="#1b1c20", fg_color=YELLOW,
            corner_radius=9, height=20,
        )  # shown (top-right) only while paused
        self.head.pack(fill="x", padx=24, pady=(18, 12))
        self.row_f = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)
        self.row_f.pack(fill="x", padx=24, pady=(0, 22))
        self.cover_lbl = ctk.CTkLabel(self.row_f, text="", image=placeholder, fg_color=CARD)
        self.cover_lbl.pack(side="left", anchor="n")
        self.info_f = ctk.CTkFrame(self.row_f, fg_color=CARD, corner_radius=0)
        self.info_f.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self.info_f.grid_columnconfigure(0, weight=1)
        self.l1 = ctk.CTkLabel(self.info_f, text="", font=F(size=17, weight="bold"),
                               text_color=TEXT, anchor="w", fg_color=CARD)
        self.l2 = ctk.CTkLabel(self.info_f, text="", font=F(size=13),
                               text_color=TEXT, anchor="w", fg_color=CARD)
        self.l3 = ctk.CTkLabel(self.info_f, text="", font=F(size=13),
                               text_color=SUB, anchor="w", fg_color=CARD)
        self.trow = ctk.CTkFrame(self.info_f, fg_color=CARD, corner_radius=0)
        self.trow.grid_columnconfigure(1, weight=1)
        self.t_left = ctk.CTkLabel(self.trow, text="0:00", text_color=SUB, font=F(size=12),
                                   fg_color=CARD, width=44, anchor="w")
        self.t_left.grid(row=0, column=0)
        self.progress = ctk.CTkProgressBar(self.trow, height=5, corner_radius=3, fg_color=TRACK,
                                           progress_color=ACC, bg_color=CARD)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, sticky="ew", padx=8)
        self.t_right = ctk.CTkLabel(self.trow, text="0:00", text_color=SUB, font=F(size=12),
                                    fg_color=CARD, width=44, anchor="e")
        self.t_right.grid(row=0, column=2)
        self.btn_row = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)   # preview of the link buttons
        self.paused = False
        self._btn_sig = None
        self._arr = None
        self._pause_sig = None
        self._time_mode = None
        self.tint_w = [self, self.head, self.row_f, self.cover_lbl, self.info_f, self.l1, self.l2, self.l3,
                       self.trow, self.t_left, self.t_right]
        self.arrange(False, False, False, False)

    @staticmethod
    def _txt(w, text):
        try:
            if w.cget("text") == text:
                return
        except Exception:
            pass
        w.configure(text=text)

    def set_buttons(self, buttons):
        sig = tuple(clip(b["label"], 32) for b in (buttons or ()))
        if sig == self._btn_sig:
            return
        self._btn_sig = sig
        for w in self.btn_row.winfo_children():
            w.destroy()
        self.btn_row.pack_forget()
        if not buttons:
            return
        self.btn_row.pack(fill="x", padx=24, pady=(0, 20))
        for b in buttons:
            ctk.CTkButton(self.btn_row, text=clip(b["label"], 32), height=34, corner_radius=8, fg_color=PANEL2,
                          hover_color=HOVER, font=F(size=12, weight="bold"), state="disabled",
                          text_color_disabled=TEXT).pack(fill="x", pady=2)

    def arrange(self, a, b, c, d):
        """Show only the lines Discord would show (hidden lines leave no gap)."""
        if self._arr == (a, b, c, d):
            return
        self._arr = (a, b, c, d)
        self._time_mode = None
        for w, on, r, pad in ((self.l1, a, 0, (2, 0)), (self.l2, b, 1, (1, 0)),
                              (self.l3, c, 2, (0, 0)), (self.trow, d, 3, (10, 0))):
            w.grid_forget()
            if on:
                w.grid(row=r, column=0, sticky="ew", pady=pad)

    def set_cover(self, data, badge=None):
        key = (hash(data) if data else None, self.use_tint, badge)
        if key == self.cover_key:
            return
        self.cover_key = key
        pil, avg = cover_pil(data, self.px)
        if badge:
            try:
                pil = self._with_badge(pil, badge)
            except Exception as e:
                log_error(f"badge: {type(e).__name__}: {e}")
        self.cover_ref = ctk.CTkImage(light_image=pil, dark_image=pil, size=(self.px, self.px))
        self.cover_lbl.configure(image=self.cover_ref)
        color = CARD if (avg is None or not self.use_tint) else hex_mix(avg, (14, 10, 22), 0.24)
        try:
            for w in self.tint_w:
                w.configure(fg_color=color)
            self.progress.configure(bg_color=color)
        except Exception:
            pass

    @staticmethod
    def _with_badge(pil, badge):
        """Round badge in the bottom-right corner of the cover, like Discord draws the small image."""
        kind, cols = badge if isinstance(badge, tuple) else (badge, None)
        img = pil.convert("RGBA")
        W = img.size[0]
        d = int(W * 0.36)
        ring = max(3, int(W * 0.035))
        scale = 4
        icon = badge_img(kind, d * scale, *(cols or (None, None))).convert("RGBA")
        mask = Image.new("L", icon.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, icon.size[0] - 1, icon.size[1] - 1), fill=255)
        icon.putalpha(mask)
        disc = Image.new("RGBA", ((d + ring * 2) * scale,) * 2, (0, 0, 0, 0))
        ImageDraw.Draw(disc).ellipse((0, 0, disc.size[0] - 1, disc.size[1] - 1), fill=(14, 10, 22, 255))
        disc.alpha_composite(icon, (ring * scale, ring * scale))
        disc = disc.resize((d + ring * 2, d + ring * 2), Image.LANCZOS)
        m = int(W * 0.015)
        img.alpha_composite(disc, (W - disc.size[0] - m, W - disc.size[1] - m))
        return img

    def set_paused(self, paused, note=""):
        """Pause look: yellow 'Paused' pill, frozen + dimmed progress bar."""
        self.paused = bool(paused)
        if self._pause_sig == (self.paused, note):
            return
        self._pause_sig = (self.paused, note)
        try:
            if self.paused:
                self.pause_pill.configure(text=f"  \u23f8 Paused{note}  ")
                self.pause_pill.place(relx=1.0, x=-20, y=14, anchor="ne")
                self.progress.configure(progress_color=MUTED)
                self.l1.configure(text_color=SUB)
            else:
                self.pause_pill.place_forget()
                self.progress.configure(progress_color=ACC)
                self.l1.configure(text_color=TEXT)
        except Exception:
            pass

    def render(self, act, cover_data, header, paused=False, pause_note=""):
        self._txt(self.head, header)
        self.act = act
        self.set_buttons((act or {}).get("own_buttons"))
        _b = (act or {}).get("badge")
        self.set_cover(cover_data, (_b, (act or {}).get("badge_colors")) if _b else None)
        self.set_paused(paused and act is not None, pause_note)
        if act is None:
            self._txt(self.l1, "Nothing playing")
            self._txt(self.l2, "Start a song in your music player")
            self.arrange(True, True, False, False)
            return
        t1, t2, t3 = _plain(act["details"]), _plain(act["state"]), _plain(act["large_text"])
        self._txt(self.l1, clip(t1, 40))
        self._txt(self.l2, clip(t2, 50))
        self._txt(self.l3, clip(t3, 50))
        self.arrange(bool(t1), bool(t2), bool(t3), bool(act["show_time"]))
        self.tick(time.time())

    def tick(self, now):
        a = self.act
        if not a or not a["show_time"]:
            return
        pos = (now - a["start"]) if a["live"] and a["start"] else a["pos"]
        dur = a["dur"]
        if dur > 0:
            pos = min(max(pos, 0.0), dur)
            self.progress.set(pos / dur)
            self._txt(self.t_left, fmt_time(pos))
            self._txt(self.t_right, fmt_time(dur))
            if self._time_mode != "full":
                self._time_mode = "full"
                self.progress.grid(row=0, column=1, sticky="ew", padx=8)
                self.t_right.grid(row=0, column=2)
        else:
            # no length known: Discord shows only a counting-up timer
            self._txt(self.t_left, fmt_time(max(pos, 0.0)) + " elapsed")
            if self._time_mode != "elapsed":
                self._time_mode = "elapsed"
                self.progress.grid_forget()
                self.t_right.grid_forget()


class MiniPlayer(tk.Toplevel):
    """Small borderless always-on-top widget: cover, title, artist and progress. Drag to move, double-click or
    the expand button to go back to the full window, right-click for the menu."""
    W, H, CV = 376, 108, 78

    def __init__(self, app):
        # A plain Tk window (not CTkToplevel): CustomTkinter's own top-level re-decorates itself on Windows,
        # which put the title bar back and left the widget blank with a generic icon.
        super().__init__(app, bg=BG)
        self.app = app
        self.withdraw()
        self.title(APP_TITLE)
        try:
            ico = os.path.join(CONFIG_DIR, "icon.ico")
            if not os.path.isfile(ico):
                os.makedirs(CONFIG_DIR, exist_ok=True)
                make_icon(256).save(ico, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
            self.iconbitmap(ico)
        except Exception:
            pass
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self._act = None
        self._txt_cache = {}
        self._cover_key = "init"
        self._cover_ref = None
        self._drag = (0, 0)
        self.resizable(False, False)
        self._sc = app._scaling()
        tk.Wm.geometry(self, f"{int(self.W * self._sc)}x{int(self.H * self._sc)}")
        self.frame = ctk.CTkFrame(self, corner_radius=0, fg_color=CARD, border_width=1, border_color=ACC)
        self.frame.pack(fill="both", expand=True)
        pil, _ = cover_pil(None, self.CV)
        self._cover_ref = ctk.CTkImage(light_image=pil, dark_image=pil, size=(self.CV, self.CV))
        self.cover = ctk.CTkLabel(self.frame, text="", image=self._cover_ref, fg_color=CARD)
        self.cover.pack(side="left", padx=(14, 0), pady=14, anchor="n")
        self.col = ctk.CTkFrame(self.frame, fg_color=CARD, corner_radius=0)
        self.col.pack(side="left", fill="both", expand=True, padx=(12, 10), pady=(10, 12))
        self.col.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(self.col, text="", font=F(size=10, weight="bold"), text_color=GREY,
                                   anchor="w", fg_color=CARD)
        self.status.grid(row=0, column=0, sticky="ew")
        self.l1 = ctk.CTkLabel(self.col, text="Nothing playing", font=F(size=14, weight="bold"),
                               text_color=TEXT, anchor="w", fg_color=CARD)
        self.l1.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self.l2 = ctk.CTkLabel(self.col, text="", font=F(size=12), text_color=SUB, anchor="w", fg_color=CARD)
        self.l2.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.trow = ctk.CTkFrame(self.col, fg_color=CARD, corner_radius=0)
        self.trow.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.trow.grid_columnconfigure(1, weight=1)
        self.t_left = ctk.CTkLabel(self.trow, text="0:00", font=F(size=10), text_color=SUB, fg_color=CARD,
                                   width=34, anchor="w")
        self.t_left.grid(row=0, column=0)
        self.bar = ctk.CTkProgressBar(self.trow, height=4, corner_radius=2, fg_color=TRACK, progress_color=ACC,
                                      bg_color=CARD)
        self.bar.set(0)
        self.bar.grid(row=0, column=1, sticky="ew", padx=6)
        self.t_right = ctk.CTkLabel(self.trow, text="0:00", font=F(size=10), text_color=SUB, fg_color=CARD,
                                    width=34, anchor="e")
        self.t_right.grid(row=0, column=2)
        self.btns = []
        for txt, cmd, c in (("\u2922", lambda: app.show_window(), 1), ("\u2715", lambda: app.close_mini_to_tray(), 2)):
            btn = ctk.CTkButton(self.col, text=txt, width=24, height=20, corner_radius=6, fg_color="transparent",
                                hover_color=HOVER, text_color=SUB, font=F(size=12), command=cmd)
            btn.grid(row=0, column=c, sticky="e", padx=(2, 0))
            self.btns.append(btn)
        self.tint_w = [self.frame, self.cover, self.col, self.status, self.l1, self.l2, self.trow, self.t_left,
                       self.t_right]
        for w in (self.frame, self.cover, self.col, self.status, self.l1, self.l2, self.trow, self.t_left,
                  self.t_right, self.bar):
            self._bind(w)
        self.apply_look()
        self.place_window()
        self.update_idletasks()
        self.deiconify()
        self.after(50, self._round_corners)
        self.after(1500, self._keep_on_top)

    def _round_corners(self):
        """Windows 11: ask for rounded corners on the borderless window (ignored on older Windows)."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            pref = ctypes.c_int(2)      # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    def _keep_on_top(self):
        try:
            self.attributes("-topmost", True)
            self.after(5000, self._keep_on_top)
        except Exception:
            pass

    def apply_look(self):
        try:
            self.attributes("-alpha", max(0.4, min(1.0, int(self.app.settings.get("mini_opacity", 100)) / 100)))
        except Exception:
            pass

    def place_window(self):
        x = y = None
        m = re.match(r"^(-?\d+),(-?\d+)$", self.app.settings.get("mini_geometry") or "")
        try:
            l, t, r, b = self.app._virtual_screen()
        except Exception:
            l, t, r, b = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
        sc = self._sc
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            x = max(l - 20, min(x, r - int(120 * sc)))
            y = max(t, min(y, b - int(60 * sc)))
        else:
            x = r - int(self.W * sc) - 28
            y = b - int(self.H * sc) - 90
        tk.Wm.geometry(self, f"+{x}+{y}")

    def _bind(self, w):
        w.bind("<ButtonPress-1>", self._drag_start, add="+")
        w.bind("<B1-Motion>", self._drag_move, add="+")
        w.bind("<ButtonRelease-1>", self._drag_end, add="+")
        w.bind("<Double-Button-1>", lambda e: self.app.show_window(), add="+")
        w.bind("<Button-3>", self._menu, add="+")

    def _drag_start(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        tk.Wm.geometry(self, f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, _e):
        self.app.settings["mini_geometry"] = f"{self.winfo_x()},{self.winfo_y()}"
        self.app.persist()

    def _menu(self, e):
        a = self.app
        m = tk.Menu(self, tearoff=0, bg=PANEL2, fg=TEXT, activebackground=ACC, activeforeground="#ffffff",
                     bd=0, relief="flat")
        m.add_command(label="Open full window", command=a.show_window)
        m.add_command(label="Copy now playing", command=a.copy_now_playing)
        on = a.worker.privacy.is_set()
        m.add_command(label="Turn privacy mode " + ("off" if on else "on"), command=a.toggle_privacy)
        m.add_separator()
        m.add_command(label="Close mini mode (keep in tray)", command=a.close_mini_to_tray)
        m.add_command(label="Exit " + APP_TITLE, command=a.quit_app)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def render(self, title, artist, act, cover_data, status, status_color, paused):
        self.status.configure(text=status, text_color=status_color)
        self._act = act
        self.l1.configure(text=clip(title, 30) if title else "Nothing playing",
                          text_color=SUB if paused else TEXT)
        self.l2.configure(text=clip(artist, 40) if artist else ("Start a song in your music player" if not title else ""))
        key = (hash(cover_data) if cover_data else None, self.app.settings.get("tint_card", True))
        if key != self._cover_key:
            self._cover_key = key
            pil, avg = cover_pil(cover_data, self.CV)
            self._cover_ref = ctk.CTkImage(light_image=pil, dark_image=pil, size=(self.CV, self.CV))
            self.cover.configure(image=self._cover_ref)
            color = CARD if (avg is None or not key[1]) else hex_mix(avg, (14, 10, 22), 0.24)
            try:
                for w in self.tint_w:
                    w.configure(fg_color=color)
                self.bar.configure(bg_color=color)
            except Exception:
                pass
        self.bar.configure(progress_color=MUTED if paused else ACC)
        self.tick(time.time())

    def tick(self, now):
        def put(w, text):
            if self._txt_cache.get(id(w)) != text:
                self._txt_cache[id(w)] = text
                w.configure(text=text)

        a = self._act
        if not a:
            self.bar.set(0)
            put(self.t_left, "0:00")
            put(self.t_right, "0:00")
            return
        pos = (now - a["start"]) if a["live"] and a["start"] else a["pos"]
        dur = a["dur"]
        if dur > 0:
            pos = min(max(pos, 0.0), dur)
            self.bar.set(pos / dur)
            put(self.t_left, fmt_time(pos))
            put(self.t_right, fmt_time(dur))
        else:
            self.bar.set(0)
            put(self.t_left, fmt_time(max(pos, 0.0)))
            put(self.t_right, "")


class App(ctk.CTk):
    def __init__(self, start_hidden=False):
        super().__init__()
        global ACC, ACC_H
        self.settings = load_settings()
        ACC, ACC_H = ACCENTS.get(self.settings["accent"], ACCENTS["Purple"])
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        apply_premium_theme()
        try:
            ctk.set_widget_scaling(self.settings["ui_scale"] / 100)
        except Exception:
            pass
        self.title(APP_TITLE)
        go_hidden = bool(start_hidden or self.settings["start_minimized"] or self.settings.get("mini_mode")
                         or (AUTOSTART and self.settings.get("last_hidden")))
        self._starting_hidden = go_hidden
        self._user_opened = False    # becomes True once the person (or a 2nd launch / tray) shows the window
        if go_hidden:
            self.withdraw()          # no flash of the window before it goes to the tray
        self.minsize(860, 600)
        self.restore_geometry()
        self.configure(fg_color=BG)

        self.ui_q = queue.Queue()
        self.np = None
        self.np_time = 0.0
        self.meta = {}
        self.cover_data = None
        self.tray = None
        self.mini = None
        self.hotkeys = None
        self.appname = None
        self._pv_job = None
        self._last_pv = 0.0
        self._focus_entry = None
        self.shown_apps = None
        self.paused_since = None
        self._pause_view = None
        self._geo_job = None
        self._lib_scanning = False
        self._np_seen = False          # first track after launch never triggers a toast
        self._notified = None
        self._last_toast = 0.0
        self.tray_state = {}
        self._update_in_progress = False
        self._declined_update_version = None

        pil, _ = cover_pil(None)
        self.placeholder = ctk.CTkImage(light_image=pil, dark_image=pil, size=(COVER, COVER))

        self.library = Library(self.ui_q)
        self.worker = Worker(lambda: self.settings, self.ui_q, self.library)
        if self.settings.get("privacy_mode"):
            self.worker.privacy.set()
        self.watcher = GameWatcher(lambda: self.settings, self.worker, self.ui_q)

        self.make_vars()
        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Configure>", self._on_configure)
        self.worker.start()
        self.watcher.start()
        self.apply_hotkeys()
        if self.settings.get("mini_mode"):
            self.after(400, self.enter_mini)
        if self.settings["autostart_presence"] and self.settings["client_id"].strip():
            self.set_presence(True)
        self.show_page("now" if self.settings["client_id"].strip() else "set")
        self.load_appname()
        self.refresh_previews()
        self.after(250, self.drain)
        self.after(500, self.tick)
        self.after(500, self.set_icon)
        self.start_tray()
        if _si_sock is not None:
            threading.Thread(target=_single_instance_listener, args=(lambda: self.ui_q.put(("cmd", "open")),),
                             daemon=True).start()
        if go_hidden:
            # Re-assert the hidden state a few times: Windows / CustomTkinter can re-show a window that was
            # withdrawn before its first paint (title-bar redraw, zoom restore), which is why "Start minimized"
            # used to be ignored.
            for ms in (200, 700, 1500, 3000):
                self.after(ms, self._keep_hidden)
        self.after(1500, self.sync_startup)
        if UPDATE_SERVER:
            self.after(5000, lambda: self.check_updates(False))   # quiet startup check
            self.after(6 * 60 * 60 * 1000, self.periodic_update_check)

    def periodic_update_check(self):
        self.check_updates(False)
        self.after(6 * 60 * 60 * 1000, self.periodic_update_check)

    def set_icon(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            p = os.path.join(CONFIG_DIR, "icon.ico")
            make_icon(256).save(p, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            self.iconbitmap(p)
        except Exception:
            pass

    # -- settings variables ----------------------------------------------------
    def make_vars(self):
        s = self.settings
        self.vars = {}
        for k in BOOL_KEYS:
            self.vars[k] = ctk.BooleanVar(value=bool(s.get(k)))
        for k in STR_KEYS:
            self.vars[k] = ctk.StringVar(value=str(s.get(k, "")))
        for k in INT_KEYS:
            self.vars[k] = ctk.IntVar(value=int(s.get(k, DEFAULTS[k])))
        self.cid_var = ctk.StringVar(value=s["client_id"])
        self.preset_name = ctk.StringVar(value="")
        self.preset_pick = ctk.StringVar(value="Default")
        for v in self.vars.values():
            v.trace_add("write", lambda *_a: self.schedule_preview())
        for k in ("start_with_windows", "start_minimized"):
            self.vars[k].trace_add("write", lambda *_a: self.after_idle(self.apply_startup_now))

    def ui_settings(self):
        """The settings as currently shown in the window (saved or not)."""
        d = dict(self.settings)
        for k, v in self.vars.items():
            try:
                val = v.get()
            except Exception:
                continue
            if k in BOOL_KEYS:
                val = bool(val)
            elif k in INT_KEYS:
                val = int(val)
            else:
                val = str(val)
                if k in CHOICES and val not in CHOICES[k]:
                    val = DEFAULTS[k]
            d[k] = val
        d["client_id"] = self.cid_var.get().strip()
        d["allow"], d["block"] = d["allow"].strip(), d["block"].strip()
        return d

    # -- layout ------------------------------------------------------------
    def report_callback_exception(self, exc, val, tb):
        """Errors inside button clicks / timers go to error.log instead of vanishing (the app runs without a console)."""
        import traceback
        log_error("UI callback error: " + "".join(traceback.format_exception(exc, val, tb))[-1500:])

    def build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.pages = {k: ctk.CTkFrame(self.content, fg_color=BG, corner_radius=0) for k in ("now", "set")}
        self.build_now_playing(self.pages["now"])
        self.build_settings(self.pages["set"])
        # Both pages live in the same grid cell for good; switching just raises one. Tearing a page down and
        # re-gridding it on every switch is what made the window flash and relayout.
        for p in self.pages.values():
            p.grid(row=0, column=0, sticky="nsew")
        self._page = None

    def build_sidebar(self):
        side = ctk.CTkFrame(self, width=236, corner_radius=0, fg_color=SIDEBAR)
        side.grid(row=0, column=0, sticky="nsew")
        side.pack_propagate(False)

        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.pack(fill="x", padx=20, pady=(26, 18))
        pil = make_icon(128)
        self.logo_ref = ctk.CTkImage(light_image=pil, dark_image=pil, size=(38, 38))
        ctk.CTkLabel(logo, text="", image=self.logo_ref).pack(side="left")
        box = ctk.CTkFrame(logo, fg_color="transparent")
        box.pack(side="left", padx=12)
        ctk.CTkLabel(box, text=APP_TITLE, font=F(size=16, weight="bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(box, text=f"Version {APP_VERSION}", font=F(size=11), text_color=MUTED).pack(anchor="w")
        ctk.CTkFrame(side, height=1, corner_radius=0, fg_color=LINE).pack(fill="x", padx=20, pady=(0, 18))

        ctk.CTkLabel(side, text="MENU", font=F(size=10, weight="bold"), text_color=MUTED, anchor="w").pack(
            fill="x", padx=26, pady=(0, 8))
        self.nav, self.nav_bar = {}, {}
        for key, glyph, label in (("now", "\u266b", "Now Playing"), ("set", "\u2699", "Settings")):
            row = ctk.CTkFrame(side, fg_color="transparent", height=42)
            row.pack(fill="x", padx=12, pady=2)
            row.pack_propagate(False)
            bar = ctk.CTkFrame(row, width=3, height=20, corner_radius=2, fg_color="transparent")   # accent marker
            bar.pack(side="left", fill="y", pady=11)
            b = ctk.CTkButton(
                row, text=f"   {glyph}    {label}", anchor="w", height=42, corner_radius=10,
                fg_color="transparent", hover_color=PANEL2, text_color=SUB,
                font=F(size=14, weight="bold"), command=lambda k=key: self.show_page(k),
            )
            b.pack(side="left", fill="both", expand=True, padx=(7, 0))
            self.nav[key], self.nav_bar[key] = b, bar

        bottom = ctk.CTkFrame(side, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=16, pady=18)
        ctk.CTkLabel(bottom, text="DISCORD", font=F(size=10, weight="bold"), text_color=MUTED, anchor="w").pack(
            fill="x", padx=4, pady=(0, 8))
        pill = ctk.CTkFrame(bottom, fg_color=CARD, corner_radius=12, border_width=1, border_color=LINE)
        pill.pack(fill="x")
        self.dot = ctk.CTkLabel(pill, text="\u25cf", text_color=GREY, font=F(size=14))
        self.dot.pack(side="left", padx=(14, 6), pady=13)
        self.status_lbl = ctk.CTkLabel(
            pill, text="Starting...", text_color=SUB, anchor="w", justify="left",
            wraplength=120, font=F(size=12),
        )
        self.status_lbl.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.toggle_btn = ctk.CTkButton(
            bottom, text="Go live on Discord", height=44, corner_radius=12,
            fg_color=ACC, hover_color=ACC_H, font=F(size=14, weight="bold"),
            command=self.toggle_presence,
        )
        self.toggle_btn.pack(fill="x", pady=(12, 0))
        sub = ctk.CTkFrame(bottom, fg_color="transparent")
        sub.pack(fill="x", pady=(8, 0))
        sub.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self.hold_btn = ctk.CTkButton(
            sub, text="Pause presence", height=32, corner_radius=8, fg_color=PANEL2, hover_color=HOVER,
            text_color=SUB, font=F(size=12), command=self.toggle_hold,
        )
        self.hold_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.reconnect_btn = ctk.CTkButton(
            sub, text="Reconnect", height=32, corner_radius=8, fg_color=PANEL2, hover_color=HOVER,
            text_color=SUB, font=F(size=12), command=self.reconnect_discord,
        )
        self.reconnect_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        sub2 = ctk.CTkFrame(bottom, fg_color="transparent")
        sub2.pack(fill="x", pady=(6, 0))
        sub2.grid_columnconfigure((0, 1), weight=1, uniform="c")
        self.privacy_btn = ctk.CTkButton(
            sub2, text="Privacy mode", height=32, corner_radius=8, fg_color=PANEL2, hover_color=HOVER,
            text_color=SUB, font=F(size=12), command=self.toggle_privacy,
        )
        self.privacy_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.mini_btn = ctk.CTkButton(
            sub2, text="Mini mode", height=32, corner_radius=8, fg_color=PANEL2, hover_color=HOVER,
            text_color=SUB, font=F(size=12), command=self.enter_mini,
        )
        self.mini_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))

    def show_page(self, key):
        if key == getattr(self, "_page", None):
            return
        self._page = key
        for k in self.pages:
            if k != key:
                self.nav[k].configure(fg_color="transparent", text_color=SUB)
                self.nav_bar[k].configure(fg_color="transparent")
        self.nav[key].configure(fg_color=PANEL2, text_color=TEXT)
        self.nav_bar[key].configure(fg_color=ACC)
        self.pages[key].tkraise()
        try:
            self.focus_set()      # don't leave the cursor in an entry on the page that's now underneath
        except Exception:
            pass
        try:
            self.refresh_previews()   # the page that was hidden skipped updates; catch it up in one go
        except Exception as e:
            log_error(f"show_page: {type(e).__name__}: {e}")

    def page_header(self, page, title, sub):
        ttl = ctk.CTkLabel(
            page, text=title, font=F(size=24, weight="bold"), text_color=TEXT, anchor="w",
        )
        ttl.pack(fill="x", padx=32, pady=(28, 0))
        lbl = ctk.CTkLabel(
            page, text=sub, text_color=MUTED, anchor="w", justify="left", wraplength=620, font=F(size=13),
        )
        lbl.pack(fill="x", padx=32, pady=(4, 0))
        ctk.CTkFrame(page, height=1, corner_radius=0, fg_color=LINE).pack(fill="x", padx=32, pady=(16, 0))
        lbl._title_widget = ttl
        return lbl

    # -- now playing -------------------------------------------------------
    def build_now_playing(self, page):
        self.page_header(page, "Now Playing", "A live preview of the status your friends see on Discord.")

        self.now_card = DiscordCard(page, self.placeholder)
        self.now_card.pack(fill="x", padx=32, pady=(20, 0))
        brow = ctk.CTkFrame(page, fg_color="transparent")
        brow.pack(fill="x", padx=32, pady=(12, 0))
        self.copy_btn = ctk.CTkButton(
            brow, text="Copy now playing", width=160, height=34, corner_radius=10, fg_color=PANEL2,
            hover_color=HOVER, text_color=TEXT, font=F(size=12, weight="bold"), command=self.copy_now_playing,
        )
        self.copy_btn.pack(side="left")
        ctk.CTkButton(
            brow, text="Mini mode", width=120, height=34, corner_radius=10, fg_color=PANEL2, hover_color=HOVER,
            text_color=TEXT, font=F(size=12, weight="bold"), command=self.enter_mini,
        ).pack(side="left", padx=8)

        ctk.CTkLabel(
            page, text="DETAILS", font=F(size=11, weight="bold"), text_color=MUTED, anchor="w",
        ).pack(fill="x", padx=34, pady=(22, 2))
        grid = ctk.CTkFrame(page, fg_color="transparent")
        grid.pack(fill="x", padx=26, pady=(0, 0))
        grid.grid_columnconfigure((0, 1), weight=1, uniform="tile")
        self.detail = {}
        for k, lab, r, c, span in (
            ("player", "Player", 0, 0, 1), ("cover", "Cover art", 0, 1, 1),
            ("album", "Album", 1, 0, 1), ("length", "Length", 1, 1, 1),
            ("sent", "Sent to Discord", 2, 0, 2),
        ):
            tile = ctk.CTkFrame(grid, corner_radius=16, fg_color=PANEL, border_width=1, border_color=LINE)
            tile.grid(row=r, column=c, columnspan=span, sticky="nsew", padx=6, pady=6)
            ctk.CTkLabel(tile, text=lab.upper(), font=F(size=10, weight="bold"), text_color=MUTED,
                         anchor="w").pack(fill="x", padx=16, pady=(12, 0))
            v = ctk.CTkLabel(tile, text="-", text_color=TEXT, anchor="w", justify="left",
                             wraplength=270 if span == 1 else 580, font=F(size=13))
            v.pack(fill="x", padx=16, pady=(3, 12))
            self.detail[k] = v
        self.err_lbl = ctk.CTkLabel(page, text="Looking for your music player...", text_color=SUB, anchor="w",
                                    justify="left", wraplength=700)
        self.err_lbl.pack(fill="x", padx=34, pady=(8, 0))

    def persist(self):
        try:
            save_settings(self.settings)
        except Exception as e:
            log_error(f"save settings: {e}")

    # -- settings ----------------------------------------------------------
    def build_settings(self, page):
        self.set_sub = self.page_header(
            page, "Settings",
            "Customise what Discord shows. The preview updates as you type - press Save to apply it to Discord.",
        )
        s = self.settings
        self._pv_user, self._pv_auto_off, self._fit_job = True, False, None
        page.bind("<Configure>", lambda e: self._schedule_fit())

        foot = ctk.CTkFrame(page, fg_color=BG, corner_radius=0)
        foot.pack(side="bottom", fill="x", padx=32, pady=(10, 18))
        ctk.CTkFrame(page, height=1, corner_radius=0, fg_color=LINE).pack(side="bottom", fill="x", padx=32)
        ctk.CTkButton(
            foot, text="Save settings", width=170, height=42, corner_radius=12, fg_color=ACC, hover_color=ACC_H,
            font=F(size=13, weight="bold"), command=self.save,
        ).pack(side="left")
        self.save_lbl = ctk.CTkLabel(foot, text="", text_color=SUB)
        self.save_lbl.pack(side="left", padx=14)

        self.pv_caption = ctk.CTkLabel(page, text="LIVE PREVIEW  \u00b7  WHAT DISCORD RECEIVES", text_color=MUTED,
                                       anchor="w", font=F(size=11, weight="bold"))
        self.pv_caption.pack(fill="x", padx=34, pady=(16, 6))
        self.set_card = DiscordCard(page, self.placeholder, px=88)
        self.set_card.pack(fill="x", padx=32)

        self.tabs = ctk.CTkTabview(
            page, fg_color=PANEL, corner_radius=18, segmented_button_selected_color=ACC,
            segmented_button_selected_hover_color=ACC_H, segmented_button_unselected_color=PANEL2,
            segmented_button_fg_color=PANEL2, border_width=1, border_color=LINE,
        )
        try:
            self.tabs._segmented_button.configure(font=F(size=13, weight="bold"), height=36)
        except Exception:
            pass
        self.tabs.pack(fill="both", expand=True, padx=32, pady=(12, 10))
        names = ("Display", "Players", "Presets", "Appearance", "General", "About")
        frames = {}
        for n in names:
            t = self.tabs.add(n)
            sf = ctk.CTkScrollableFrame(t, fg_color="transparent")
            sf.pack(fill="both", expand=True)
            frames[n] = sf
        try:   # tab strip spans the whole card (equal-width tabs) instead of a small centred pill
            self.tabs._segmented_button.grid_configure(sticky="ew")
        except Exception:
            pass

        def section(parent, title, desc=None):
            box = ctk.CTkFrame(parent, corner_radius=16, fg_color=CARD, border_width=1, border_color=LINE)
            box.pack(fill="x", padx=4, pady=7)
            ctk.CTkLabel(box, text=title, font=F(size=14, weight="bold"), text_color=TEXT, anchor="w").pack(
                fill="x", padx=18, pady=(14, 2))
            if desc:
                ctk.CTkLabel(box, text=desc, text_color=MUTED, anchor="w", justify="left", wraplength=620,
                             font=F(size=12)).pack(fill="x", padx=18, pady=(0, 6))
            return box

        def switch(box, key, text):
            sw = ctk.CTkSwitch(box, text=text, variable=self.vars[key], progress_color=ACC)
            sw.pack(anchor="w", padx=18, pady=6)
            return sw

        def row(box):
            r = ctk.CTkFrame(box, fg_color="transparent")
            r.pack(fill="x", padx=18, pady=6)
            return r

        def menu(r, key, values, width=210):
            m = ctk.CTkOptionMenu(r, values=list(values), variable=self.vars[key], width=width,
                                  fg_color=PANEL2, button_color=PANEL2, button_hover_color=HOVER)
            m.pack(side="left", padx=10)
            return m

        def pad(box):
            ctk.CTkLabel(box, text="", height=4).pack()

        # ---------------------------------------------------------- Display --
        d = frames["Display"]
        box = section(
            d, "Lines on Discord",
            "A music status has three text lines: line 1 (bold), line 2 and line 3 (shown beside the cover; "
            "needs a cover to be sent). Each line is a template - click a variable to insert it. "
            "Switch a line off to hide it. Empty variables are skipped cleanly.",
        )
        self.entries = {}
        for label, show_key, tpl_key, hint in (
            ("Line 1", "show_title", "tpl_details", "{title}"),
            ("Line 2", "show_artist", "tpl_state", "{artist}"),
            ("Line 3", "show_album", "tpl_album", "{album}"),
        ):
            r = row(box)
            ctk.CTkSwitch(r, text="", variable=self.vars[show_key], width=44, progress_color=ACC).pack(side="left")
            ctk.CTkLabel(r, text=label, width=56, anchor="w").pack(side="left")
            e = ctk.CTkEntry(r, textvariable=self.vars[tpl_key], width=340, placeholder_text=hint,
                             fg_color=BG, border_color=PANEL2)
            e.pack(side="left", padx=6)
            e.bind("<FocusIn>", lambda _ev, w=e: setattr(self, "_focus_entry", w), add="+")
            self.entries[tpl_key] = e
        self._focus_entry = self.entries["tpl_details"]
        chips = ctk.CTkFrame(box, fg_color="transparent")
        chips.pack(fill="x", padx=16, pady=(4, 2))
        for i, name in enumerate(TEMPLATE_VARS):
            ctk.CTkButton(
                chips, text="{" + name + "}", width=96, height=26, corner_radius=6, fg_color=PANEL2,
                hover_color=HOVER, font=F(size=11), command=lambda n=name: self.insert_var(n),
            ).grid(row=i // 4, column=i % 4, padx=3, pady=3)
        r = row(box)
        ctk.CTkButton(r, text="Reset lines to default", width=160, fg_color=PANEL2, hover_color=HOVER,
                      command=self.reset_lines).pack(side="left")
        pad(box)

        box = section(
            d, "Song link",
            "When you stream (Spotify, Apple Music, TIDAL, Deezer, a browser...), people on your profile get a "
            "button that opens that exact song. Local files never get one.",
        )
        switch(box, "song_link", "Add a \"Listen on ...\" button for streamed songs")
        switch(box, "song_link_title", "Make the song title itself clickable (needs a recent pypresence)")
        ctk.CTkLabel(box, text="Discord allows two buttons, so the song button takes the first slot and your "
                               "Button 1 below moves to the second.", text_color=MUTED, anchor="w",
                     justify="left", wraplength=620, font=F(size=12)).pack(fill="x", padx=18, pady=(0, 4))
        pad(box)

        box = section(
            d, "Link buttons",
            "Up to two buttons under your status that open any link you want (your server invite, a playlist, "
            "your site...). Discord shows them to other people who view your profile - you won't see them on "
            "your own - and they don't appear on Discord mobile.",
        )
        for n in (1, 2):
            r = row(box)
            ctk.CTkSwitch(r, text="", variable=self.vars[f"btn{n}_on"], width=44, progress_color=ACC).pack(side="left")
            ctk.CTkLabel(r, text=f"Button {n}", width=64, anchor="w").pack(side="left")
            ctk.CTkEntry(r, textvariable=self.vars[f"btn{n}_label"], width=170, placeholder_text="Button text",
                         fg_color=BG, border_color=PANEL2).pack(side="left", padx=(6, 4))
            ctk.CTkEntry(r, textvariable=self.vars[f"btn{n}_url"], width=300, placeholder_text="https://where-it-goes.com",
                         fg_color=BG, border_color=PANEL2).pack(side="left", padx=4)
        ctk.CTkLabel(box, text="Text can be up to 32 characters. Press Save to apply.", text_color=MUTED,
                     anchor="w").pack(fill="x", padx=16, pady=(0, 4))
        pad(box)

        box = section(d, "What to send")
        switch(box, "show_cover", "Album cover")
        switch(box, "show_progress", "Progress bar with elapsed / total time")
        switch(box, "album_fallback", "No cover available? Move line 3 onto line 2 so the album still shows")
        switch(box, "prefer_file_tags", "Use the tags inside my files instead of what the player reports")
        r = row(box)
        ctk.CTkLabel(r, text="Discord status text").pack(side="left")
        menu(r, "status_display", STATUS_DISPLAY, 170)
        ctk.CTkLabel(
            box, text="What shows next to your name in the member list: the app name (default), your line 2, "
            "or your line 1. Needs a recent pypresence; otherwise it's ignored.",
            text_color=MUTED, anchor="w", justify="left", wraplength=620,
        ).pack(fill="x", padx=16)
        ctk.CTkLabel(
            box, text="To show a single card instead of two: Discord can't merge into Spotify's own card, so in "
            "Discord go to Settings > Connections > Spotify and turn off \"Display Spotify as your status\". "
            "Then pick the \"Spotify-style (merge look)\" preset below and this app is the only music card.",
            text_color=SUB, anchor="w", justify="left", wraplength=620,
        ).pack(fill="x", padx=16, pady=(8, 0))
        pad(box)

        box = section(
            d, "When playback pauses",
            "Discord can't freeze a progress bar, so a paused song either clears your status or stays "
            "visible without the progress bar.",
        )
        r = row(box)
        ctk.CTkLabel(r, text="On pause").pack(side="left")
        menu(r, "pause_mode", PAUSE_MODES, 230)
        r = row(box)
        ctk.CTkLabel(r, text="Wait before clearing").pack(side="left")
        self.pause_lbl = ctk.CTkLabel(r, text=f"{self.vars['pause_delay'].get()} s", width=44)
        ctk.CTkSlider(
            r, from_=0, to=60, number_of_steps=60, variable=self.vars["pause_delay"], width=220,
            progress_color=ACC, button_color=ACC, button_hover_color=ACC_H,
            command=lambda v: self.pause_lbl.configure(text=f"{int(v)} s"),
        ).pack(side="left", padx=10)
        self.pause_lbl.pack(side="left")
        switch(box, "paused_badge", "Show a pause badge on the cover while the status is kept")
        switch(box, "player_badge", "Show the player logo (Spotify) on the cover while playing")
        r = row(box)
        ctk.CTkLabel(r, text="Spotify logo colors").pack(side="left")
        self.sw_btns = {}
        for key, label in (("spotify_bg", "Background"), ("spotify_fg", "Symbol")):
            b = ctk.CTkButton(r, text=label, width=110, command=lambda k=key: self.pick_badge_color(k))
            b.pack(side="left", padx=(10, 0))
            self.sw_btns[key] = b
        self.dyn_btn = ctk.CTkButton(r, text="Dynamic", width=110, command=self.toggle_dynamic_badge)
        self.dyn_btn.pack(side="left", padx=(10, 0))
        ctk.CTkButton(r, text="Reset", width=70, fg_color=PANEL2, hover_color=HOVER,
                      command=self.reset_badge_colors).pack(side="left", padx=10)
        self.paint_swatches()
        pad(box)

        box = section(
            d, "Cover art",
            "Covers come from your files' embedded art first, then a cover.jpg / folder.jpg next to the file, "
            "then the picture your player publishes, then an online lookup. Discord can only display images "
            "that have a web address, so a cover is shrunk to 512x512 and uploaded once to a free anonymous "
            "image host; the link is cached and reused for every song with that cover.",
        )
        switch(box, "upload_covers", "Upload covers from my files so Discord can show them")
        switch(box, "folder_art", "Use cover.jpg / folder.jpg from the song's folder")
        switch(box, "online_lookup", "Fill in missing covers, albums and lengths from iTunes / Deezer")
        r = row(box)
        self.cache_lbl = ctk.CTkLabel(r, text="", text_color=SUB)
        ctk.CTkButton(r, text="Clear artwork cache", width=160, fg_color=PANEL2, hover_color=HOVER,
                      command=self.clear_cache).pack(side="left")
        self.cache_lbl.pack(side="left", padx=12)
        pad(box)

        # ---------------------------------------------------------- Players --
        p = frames["Players"]
        box = section(
            p, "Detected players",
            "Everything currently publishing to Windows' media controls. Use the buttons to ignore a player "
            "(browsers, for example) or to listen to that one only.",
        )
        self.players_box = ctk.CTkFrame(box, fg_color="transparent")
        self.players_box.pack(fill="x", padx=16, pady=(2, 10))
        self.update_players([])

        box = section(
            p, "Filtering",
            "Comma-separated, matched against the player's app name (e.g. vlc, musicbee). "
            "Leave \"Only these\" empty to allow everything.",
        )
        for label, key, ph in (("Only these", "allow", "vlc, musicbee"),
                               ("Ignore these", "block", "chrome, firefox, msedge")):
            r = row(box)
            ctk.CTkLabel(r, text=label, width=100, anchor="w").pack(side="left")
            ctk.CTkEntry(r, textvariable=self.vars[key], width=340, placeholder_text=ph,
                         fg_color=BG, border_color=PANEL2).pack(side="left")
        pad(box)

        box = section(
            p, "Window-title fallback",
            "If Windows' media controls aren't available, the app reads the player's window title (Spotify, VLC, "
            "MusicBee, foobar2000, AIMP, PotPlayer). Pick how your titles are ordered.",
        )
        r = row(box)
        ctk.CTkLabel(r, text="Window title order").pack(side="left")
        menu(r, "title_order", TITLE_ORDERS, 170)
        pad(box)

        # ---------------------------------------------------------- Presets --
        pr = frames["Presets"]
        box = section(
            pr, "Presets",
            "A preset remembers the whole look: lines, templates, what's shown, pause behaviour, "
            "player filters and accent colour. Loading one applies and saves it.",
        )
        r = row(box)
        self.preset_menu = ctk.CTkOptionMenu(
            r, values=self.preset_names(), variable=self.preset_pick, width=260,
            fg_color=PANEL2, button_color=PANEL2, button_hover_color=HOVER,
        )
        self.preset_menu.pack(side="left")
        ctk.CTkButton(r, text="Load", width=80, fg_color=ACC, hover_color=ACC_H,
                      command=self.load_preset).pack(side="left", padx=(10, 6))
        ctk.CTkButton(r, text="Delete", width=80, fg_color=PANEL2, hover_color=HOVER,
                      command=self.delete_preset).pack(side="left")
        r = row(box)
        ctk.CTkEntry(r, textvariable=self.preset_name, width=260, placeholder_text="Name for a new preset",
                     fg_color=BG, border_color=PANEL2).pack(side="left")
        ctk.CTkButton(r, text="Save current settings as preset", width=230, fg_color=PANEL2, hover_color=HOVER,
                      command=self.save_preset).pack(side="left", padx=10)
        self.preset_lbl = ctk.CTkLabel(box, text="", text_color=SUB, anchor="w")
        self.preset_lbl.pack(fill="x", padx=16, pady=(2, 10))

        # -------------------------------------------------------- Appearance --
        a = frames["Appearance"]
        box = section(a, "Look of this app")
        r = row(box)
        ctk.CTkLabel(r, text="Accent colour").pack(side="left")
        menu(r, "accent", CHOICES["accent"], 170)
        ctk.CTkLabel(
            box, text="Auto (cover art) recolours the whole app from the album art of the song that's playing.",
            text_color=MUTED, anchor="w", justify="left", wraplength=620,
        ).pack(fill="x", padx=16)
        switch(box, "tint_card", "Tint the preview card with the cover's colour")
        switch(box, "show_preview", "Show the live preview at the top of Settings")
        r = row(box)
        ctk.CTkLabel(r, text="Interface size").pack(side="left")
        self.scale_lbl = ctk.CTkLabel(r, text=f"{self.vars['ui_scale'].get()}%", width=50)
        ctk.CTkSlider(
            r, from_=80, to=140, number_of_steps=12, variable=self.vars["ui_scale"], width=220,
            progress_color=ACC, button_color=ACC, button_hover_color=ACC_H,
            command=lambda v: self.scale_lbl.configure(text=f"{int(v)}%"),
        ).pack(side="left", padx=10)
        self.scale_lbl.pack(side="left")
        pad(box)

        box = section(a, "Mini mode",
                      "A small always-on-top widget of the card. Drag it anywhere, double-click it to open the "
                      "full window, right-click for options.")
        r = row(box)
        ctk.CTkLabel(r, text="Widget opacity").pack(side="left")
        self.mini_op_lbl = ctk.CTkLabel(r, text=f"{self.vars['mini_opacity'].get()}%", width=50)
        ctk.CTkSlider(
            r, from_=40, to=100, number_of_steps=12, variable=self.vars["mini_opacity"], width=220,
            progress_color=ACC, button_color=ACC, button_hover_color=ACC_H,
            command=lambda v: self.mini_op_lbl.configure(text=f"{int(v)}%"),
        ).pack(side="left", padx=10)
        self.mini_op_lbl.pack(side="left")
        r = row(box)
        ctk.CTkButton(r, text="Open mini mode", width=150, fg_color=ACC, hover_color=ACC_H,
                      command=self.enter_mini).pack(side="left")
        pad(box)

        # ----------------------------------------------------------- General --
        g = frames["General"]
        box = section(
            g, "Discord connection",
            "1. Open the Discord Developer Portal and create a New Application. Its name is what appears "
            "after \"Listening to\" on your profile.\n2. Copy the Application ID from General Information and paste it below.",
        )
        r = row(box)
        ctk.CTkEntry(r, textvariable=self.cid_var, width=270, placeholder_text="Application ID",
                     fg_color=BG, border_color=PANEL2).pack(side="left")
        ctk.CTkButton(
            r, text="Open Developer Portal", width=170, fg_color=PANEL2, hover_color=HOVER,
            command=lambda: webbrowser.open("https://discord.com/developers/applications"),
        ).pack(side="left", padx=10)
        pad(box)

        box = section(
            g, "App icon",
            "Used for the window, the taskbar, the tray and (if you installed it) the desktop / Start menu "
            "shortcuts. Any picture works - PNG, JPG, ICO... - it's cropped to a square.",
        )
        r = row(box)
        ctk.CTkButton(r, text="Choose image...", width=150, fg_color=ACC, hover_color=ACC_H,
                      command=self.choose_icon).pack(side="left")
        ctk.CTkButton(r, text="Reset to default", width=140, fg_color=PANEL2, hover_color=HOVER,
                      command=self.reset_icon).pack(side="left", padx=10)
        self.icon_lbl = ctk.CTkLabel(r, text="Custom icon in use" if os.path.isfile(CUSTOM_ICON) else "Default icon",
                                     text_color=SUB)
        self.icon_lbl.pack(side="left", padx=6)
        pad(box)

        box = section(
            g, "Privacy & auto-hide",
            "Hides your status on Discord without stopping the app (press Save after changing these). Quick "
            "switches: the Privacy button in the sidebar, the tray menu, the mini widget's right-click menu, "
            "or Ctrl+Alt+H.",
        )
        switch(box, "auto_hide", "Hide my status while one of these apps is running")
        r = row(box)
        ctk.CTkEntry(r, textvariable=self.vars["auto_hide_apps"], width=440,
                     placeholder_text="valorant-win64-shipping.exe, eldenring.exe",
                     fg_color=BG, border_color=PANEL2).pack(side="left")
        r = row(box)
        self.proc_var = ctk.StringVar(value="")
        self.proc_menu = ctk.CTkOptionMenu(r, values=["(click Refresh)"], variable=self.proc_var, width=250,
                                           fg_color=PANEL2, button_color=PANEL2, button_hover_color=HOVER)
        self.proc_menu.pack(side="left")
        ctk.CTkButton(r, text="Refresh", width=80, fg_color=PANEL2, hover_color=HOVER,
                      command=self.refresh_procs).pack(side="left", padx=8)
        ctk.CTkButton(r, text="Add app", width=90, fg_color=ACC, hover_color=ACC_H,
                      command=self.add_proc).pack(side="left")
        switch(box, "hide_fullscreen", "Also hide while any app is fullscreen (games, but also fullscreen video)")
        r = row(box)
        ctk.CTkSwitch(r, text="Quiet hours", variable=self.vars["quiet_on"], progress_color=ACC).pack(side="left")
        ctk.CTkEntry(r, textvariable=self.vars["quiet_from"], width=70, fg_color=BG,
                     border_color=PANEL2).pack(side="left", padx=(14, 4))
        ctk.CTkLabel(r, text="to").pack(side="left", padx=4)
        ctk.CTkEntry(r, textvariable=self.vars["quiet_to"], width=70, fg_color=BG,
                     border_color=PANEL2).pack(side="left", padx=(4, 0))
        switch(box, "hotkeys", "Global hotkeys: Ctrl+Alt+H privacy mode, Ctrl+Alt+M mini mode")
        pad(box)
        self.after(400, self.refresh_procs)

        box = section(g, "App")
        r = row(box)
        ctk.CTkLabel(r, text="Check player every").pack(side="left")
        self.poll_lbl = ctk.CTkLabel(r, text=f"{self.vars['poll_seconds'].get()} s", width=40)
        ctk.CTkSlider(
            r, from_=1, to=10, number_of_steps=9, variable=self.vars["poll_seconds"], width=200,
            progress_color=ACC, button_color=ACC, button_hover_color=ACC_H,
            command=lambda v: self.poll_lbl.configure(text=f"{int(v)} s"),
        ).pack(side="left", padx=10)
        self.poll_lbl.pack(side="left")
        switch(box, "autostart_presence", "Go live automatically when the app opens")
        switch(box, "start_with_windows", "Launch with Windows")
        switch(box, "start_minimized", "Start minimized (straight to the system tray)")
        tray_sw = switch(box, "close_to_tray", "Closing the window keeps it running in the system tray")
        if pystray is None:
            tray_sw.configure(state="disabled")
        switch(box, "notify_song_change", "Show a small notification when the song changes (only while minimized)")
        switch(box, "confirm_disable", "Ask before turning presence off")
        r = row(box)
        ctk.CTkButton(
            r, text="Reconnect Discord", width=150, fg_color=PANEL2, hover_color=HOVER,
            command=self.reconnect_discord,
        ).pack(side="left")
        ctk.CTkButton(
            r, text="Open log folder", width=140, fg_color=PANEL2, hover_color=HOVER,
            command=self.open_config_dir,
        ).pack(side="left", padx=10)
        pad(box)

        # ------------------------------------------------------------ About --
        ab = frames["About"]
        box = section(ab, f"{APP_TITLE}  v{APP_VERSION}",
                      "Shows what your local music player is playing on Discord, with cover art, "
                      "progress and details read from your own files.")
        ctk.CTkLabel(box, text=f"Settings & logs: {CONFIG_DIR}", text_color=MUTED, anchor="w",
                     justify="left", wraplength=620).pack(fill="x", padx=16, pady=(0, 6))
        r = row(box)
        self.update_btn = ctk.CTkButton(
            r, text="Check for updates", width=160, fg_color=ACC, hover_color=ACC_H,
            command=lambda: self.check_updates(True),
        )
        self.update_btn.pack(side="left")
        self.update_lbl = ctk.CTkLabel(r, text="", text_color=SUB, anchor="w", justify="left", wraplength=380)
        self.update_lbl.pack(side="left", padx=12)
        r = row(box)
        ctk.CTkButton(
            r, text="Copy diagnostic information", width=200, fg_color=PANEL2, hover_color=HOVER,
            command=self.copy_diagnostics,
        ).pack(side="left")
        ctk.CTkButton(
            r, text="Open log folder", width=140, fg_color=PANEL2, hover_color=HOVER,
            command=self.open_config_dir,
        ).pack(side="left", padx=10)
        self.about_lbl = ctk.CTkLabel(box, text="", text_color=SUB, anchor="w")
        self.about_lbl.pack(fill="x", padx=16, pady=(0, 6))
        pad(box)

        self.set_card.use_tint = bool(s.get("tint_card", True))
        self.now_card.use_tint = bool(s.get("tint_card", True))
        self.set_preview_visible(bool(s.get("show_preview", True)))

    # -- customisation helpers ---------------------------------------------
    def insert_var(self, name):
        w = self._focus_entry or self.entries["tpl_details"]
        try:
            w.insert("insert", "{" + name + "}")
            w.focus_set()
        except Exception as e:
            log_error(f"insert var: {e}")

    def reset_lines(self):
        for k in ("tpl_details", "tpl_state", "tpl_album"):
            self.vars[k].set(DEFAULTS[k])
        for k in ("show_title", "show_artist", "show_album"):
            self.vars[k].set(True)

    def set_preview_visible(self, on):
        """'on' is the person's choice; the preview is also hidden automatically while the window is too short."""
        self._pv_user = bool(on)
        self._apply_preview()

    def _apply_preview(self):
        show = self._pv_user and not self._pv_auto_off
        mapped = bool(self.set_card.winfo_manager())
        if show and not mapped:
            self.pv_caption.pack(fill="x", padx=34, pady=(16, 6), before=self.tabs)
            self.set_card.pack(fill="x", padx=32, before=self.tabs)
        elif not show and mapped:
            self.pv_caption.pack_forget()
            self.set_card.pack_forget()

    def _schedule_fit(self):
        if self._fit_job is None:
            self._fit_job = self.after(120, self._fit_settings)

    def _fit_settings(self):
        """Small window: collapse the live preview + subtitle so the tabs always get enough room
        (they used to be squashed to a thin strip). Comes back by itself when the window is tall enough."""
        self._fit_job = None
        try:
            sc = self._scaling()
            page_h = self.pages["set"].winfo_height() / sc
            need_tabs = 280                                   # comfortable height for the tab content
            fixed = 150 + 74                                  # header + footer (approx., unscaled px)
            card = 150 if self._pv_user else 0                # preview caption + card
            auto_off = self._pv_auto_off
            if not auto_off and self._pv_user and page_h < fixed + card + need_tabs:
                auto_off = True
            elif auto_off and page_h >= fixed + card + need_tabs + 40:   # hysteresis: no flicker at the edge
                auto_off = False
            if auto_off != self._pv_auto_off:
                self._pv_auto_off = auto_off
                self._apply_preview()
            tight = page_h < fixed + need_tabs - 40           # even without the preview it's tight
            if tight and self.set_sub.winfo_manager():
                self.set_sub.pack_forget()
            elif not tight and not self.set_sub.winfo_manager():
                self.set_sub.pack(fill="x", padx=32, pady=(4, 0), after=self.set_sub._title_widget)
        except Exception as e:
            log_error(f"fit settings: {type(e).__name__}: {e}")

    def clear_cache(self):
        try:
            host = self.worker.covers.host
            host.db.clear()
            host._save()
            shutil_dir = os.path.join(CONFIG_DIR, "thumbs")
            if os.path.isdir(shutil_dir):
                for n in os.listdir(shutil_dir):
                    try:
                        os.remove(os.path.join(shutil_dir, n))
                    except OSError:
                        pass
            self.worker.covers.res.clear()
            self.worker.meta_cache.clear()
            self.cache_lbl.configure(text="Cleared - covers will be re-sent next time.", text_color=GREEN)
        except Exception as e:
            self.cache_lbl.configure(text=f"Couldn't clear: {e}", text_color=RED)

    # presets
    def preset_names(self):
        return list(BUILTIN_PRESETS) + sorted((self.settings.get("presets") or {}).keys())

    def preset_data(self, name):
        base = {k: DEFAULTS[k] for k in PRESET_KEYS}
        if name in BUILTIN_PRESETS:
            base.update(BUILTIN_PRESETS[name])
            return base
        data = (self.settings.get("presets") or {}).get(name)
        if data is None:
            return None
        base.update({k: v for k, v in data.items() if k in PRESET_KEYS})
        return base

    def refresh_preset_menu(self, pick=None):
        names = self.preset_names()
        self.preset_menu.configure(values=names)
        self.preset_pick.set(pick if pick in names else names[0])

    def save_preset(self):
        name = self.preset_name.get().strip()[:40]
        if not name:
            self.preset_lbl.configure(text="Type a name first.", text_color=YELLOW)
            return
        if name in BUILTIN_PRESETS:
            self.preset_lbl.configure(text="That name is used by a built-in preset.", text_color=YELLOW)
            return
        ui = self.ui_settings()
        presets = dict(self.settings.get("presets") or {})
        presets[name] = {k: ui[k] for k in PRESET_KEYS}
        self.settings["presets"] = presets
        self.persist()
        self.refresh_preset_menu(name)
        self.preset_name.set("")
        self.preset_lbl.configure(text=f"Saved preset \"{name}\".", text_color=GREEN)

    def load_preset(self):
        name = self.preset_pick.get()
        data = self.preset_data(name)
        if data is None:
            self.preset_lbl.configure(text="Pick a preset first.", text_color=YELLOW)
            return
        for k, v in data.items():
            if k in self.vars:
                try:
                    self.vars[k].set(v)
                except Exception:
                    pass
        self.save()
        self.preset_lbl.configure(text=f"Loaded \"{name}\".", text_color=GREEN)

    def delete_preset(self):
        name = self.preset_pick.get()
        if name in BUILTIN_PRESETS:
            self.preset_lbl.configure(text="Built-in presets can't be deleted.", text_color=YELLOW)
            return
        presets = dict(self.settings.get("presets") or {})
        if presets.pop(name, None) is None:
            return
        self.settings["presets"] = presets
        self.persist()
        self.refresh_preset_menu()
        self.preset_lbl.configure(text=f"Deleted \"{name}\".", text_color=SUB)

    # detected players
    def update_players(self, apps):
        for w in self.players_box.winfo_children():
            w.destroy()
        if not apps:
            ctk.CTkLabel(self.players_box, text="Nothing is publishing to the media controls right now.",
                         text_color=MUTED, anchor="w").pack(fill="x", pady=4)
            return
        for app in apps:
            token = friendly_player(app).lower()
            r = ctk.CTkFrame(self.players_box, fg_color=PANEL2, corner_radius=8)
            r.pack(fill="x", pady=3)
            ctk.CTkLabel(r, text=friendly_player(app), anchor="w", text_color=TEXT,
                         font=F(size=13, weight="bold")).pack(side="left", padx=(12, 6), pady=8)
            ctk.CTkLabel(r, text=clip(app, 44), anchor="w", text_color=MUTED,
                         font=F(size=11)).pack(side="left")
            ctk.CTkButton(r, text="Only this", width=80, height=26, fg_color="transparent", hover_color=HOVER,
                          text_color=SUB, border_width=1, border_color=LINE,
                          command=lambda t=token: self.vars["allow"].set(t)).pack(side="right", padx=(4, 10))
            ctk.CTkButton(r, text="Ignore", width=70, height=26, fg_color="transparent", hover_color=HOVER,
                          text_color=SUB, border_width=1, border_color=LINE,
                          command=lambda t=token: self.add_block(t)).pack(side="right")

    def add_block(self, token):
        cur = [x.strip() for x in self.vars["block"].get().split(",") if x.strip()]
        if token not in [c.lower() for c in cur]:
            cur.append(token)
        self.vars["block"].set(", ".join(cur))

    # -- auto theme from cover art ---------------------------------------------
    def theme_from_cover(self, data):
        """Re-colour the whole window from the playing cover (only when accent is 'Auto')."""
        if self.settings.get("accent") != ACCENT_AUTO:
            return
        key = hash(data) if data else None
        if key == getattr(self, "_theme_key", "x"):
            return
        self._theme_key = key
        th = make_theme(dominant_color(data)) if data else None
        if th is None:
            th = dict(DEFAULT_THEME)
        self.retheme(th)

    def retheme(self, th, steps=8):
        global BG, SIDEBAR, PANEL, PANEL2, CARD, HOVER, LINE, TRACK, ACC, ACC_H
        old = {"BG": BG, "SIDEBAR": SIDEBAR, "PANEL": PANEL, "PANEL2": PANEL2, "CARD": CARD, "HOVER": HOVER,
               "LINE": LINE, "TRACK": TRACK, "ACC": ACC, "ACC_H": ACC_H}
        BG, SIDEBAR, PANEL, PANEL2, CARD = th["BG"], th["SIDEBAR"], th["PANEL"], th["PANEL2"], th["CARD"]
        HOVER, LINE, TRACK, ACC, ACC_H = th["HOVER"], th["LINE"], th["TRACK"], th["ACC"], th["ACC_H"]
        lookup = {}
        for k, v in old.items():
            lookup.setdefault(v.lower(), k)
        opts = ("fg_color", "bg_color", "hover_color", "border_color", "progress_color", "button_color",
                "button_hover_color", "segmented_button_selected_color", "segmented_button_selected_hover_color")
        jobs = []

        def walk(w):
            for c in w.winfo_children():
                for o in opts:
                    try:
                        v = c.cget(o)
                    except Exception:
                        continue
                    if isinstance(v, str) and v.lower() in lookup:
                        jobs.append((c, o, v, lookup[v.lower()]))
                walk(c)
        walk(self)
        self.configure(fg_color=th["BG"])

        def mix(a, b, t):
            a, b = a.lstrip("#"), b.lstrip("#")
            return "#" + "".join("%02x" % int(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (0, 2, 4))

        def step(i):
            t = i / steps
            for c, o, v, k in jobs:
                try:
                    c.configure(**{o: mix(v, th[k], t)})
                except Exception:
                    pass
            if i < steps:
                self.after(24, lambda: step(i + 1))
            else:
                self.update_toggle()
        step(1)

    # -- accent colour -------------------------------------------------------
    def apply_accent(self, name):
        global ACC, ACC_H
        if name == ACCENT_AUTO:
            self._theme_key = "x"
            self.theme_from_cover(self.cover_data)
            return
        if BG != DEFAULT_THEME["BG"]:   # leaving Auto: put the surfaces back, then apply the chosen accent
            a1, a2 = ACCENTS.get(name, ACCENTS["Purple"])
            self._theme_key = "x"
            self.retheme(dict(DEFAULT_THEME, ACC=a1, ACC_H=a2))
            return
        old1, old2 = ACC, ACC_H
        ACC, ACC_H = ACCENTS.get(name, ACCENTS["Purple"])
        new1, new2 = ACC, ACC_H

        def walk(w):
            for c in w.winfo_children():
                try:
                    if isinstance(c, ctk.CTkButton):
                        if c.cget("fg_color") == old1:
                            c.configure(fg_color=new1, hover_color=new2)
                    elif isinstance(c, ctk.CTkSwitch):
                        c.configure(progress_color=new1)
                    elif isinstance(c, ctk.CTkSlider):
                        c.configure(progress_color=new1, button_color=new1, button_hover_color=new2)
                    elif isinstance(c, ctk.CTkTabview):
                        c.configure(segmented_button_selected_color=new1,
                                    segmented_button_selected_hover_color=new2)
                    elif isinstance(c, ctk.CTkProgressBar):
                        if c.cget("progress_color") == old1:
                            c.configure(progress_color=new1)
                    elif isinstance(c, ctk.CTkFrame):
                        if c.cget("fg_color") == old1:   # header accent bars, the active-tab marker
                            c.configure(fg_color=new1)
                except Exception:
                    pass
                walk(c)

        walk(self)
        self.update_toggle()

    # -- actions -----------------------------------------------------------
    def save(self):
        new = self.ui_settings()
        cid = new["client_id"]
        if cid and not cid.isdigit():
            self.save_lbl.configure(text="Application ID should be a long number (digits only).", text_color=RED)
            self.tabs.set("General")
            return
        old = self.settings
        new["version"] = SETTINGS_VERSION
        try:
            save_settings(new)
            set_startup(new["start_with_windows"], new["start_minimized"])
        except Exception as e:
            self.save_lbl.configure(text=f"Could not save: {e}", text_color=RED)
            return
        self.settings = new
        self.save_lbl.configure(text="Saved.", text_color=GREEN)
        if cid != old["client_id"].strip():
            self.load_appname()
        if new["accent"] != old.get("accent"):
            self.apply_accent(new["accent"])
        if new["ui_scale"] != old.get("ui_scale"):
            try:
                ctk.set_widget_scaling(new["ui_scale"] / 100)
            except Exception:
                pass
        self.now_card.use_tint = self.set_card.use_tint = bool(new["tint_card"])
        self.set_preview_visible(bool(new["show_preview"]))
        if self.mini is not None:
            self.mini.apply_look()
        self.apply_hotkeys()
        self.worker.wake.set()
        self.refresh_previews()

    def load_appname(self):
        cid = self.settings["client_id"].strip()
        if not cid:
            self.set_appname(None)
            return
        threading.Thread(target=lambda: self.ui_q.put(("appname", fetch_app_name(cid))), daemon=True).start()

    def header_text(self):
        return "Listening to " + (self.appname or "your Discord app")

    def set_appname(self, name):
        self.appname = name
        self.refresh_previews()

    def update_toggle(self):
        on = self.worker.enabled.is_set()
        if on:
            self.toggle_btn.configure(text="Stop presence", fg_color=LINE, hover_color=HOVER)
        else:
            self.toggle_btn.configure(text="Go live on Discord", fg_color=ACC, hover_color=ACC_H)
        held = self.worker.hold.is_set()
        self.hold_btn.configure(text="Resume presence" if held else "Pause presence",
                                state="normal" if on else "disabled")
        priv = self.worker.privacy.is_set()
        self.privacy_btn.configure(text="Privacy: ON" if priv else "Privacy mode",
                                   fg_color=ACC if priv else PANEL2, hover_color=ACC_H if priv else HOVER,
                                   text_color="#FFFFFF" if priv else SUB)
        self.refresh_tray()

    def set_presence(self, on):
        if on:
            self.worker.enabled.set()
        else:
            self.worker.enabled.clear()
            self.worker.hold.clear()
        self.worker.wake.set()
        self.update_toggle()

    def toggle_hold(self):
        if not self.worker.enabled.is_set():
            return
        if self.worker.hold.is_set():
            self.worker.hold.clear()
        else:
            self.worker.hold.set()
        self.worker.wake.set()
        self.update_toggle()

    def reconnect_discord(self):
        if not self.worker.enabled.is_set():
            self.status_lbl.configure(text="Presence is off - go live first")
            return
        self.worker.request_reconnect()
        self.reconnect_btn.configure(state="disabled", text="Reconnecting...")
        self.after(3000, lambda: self.reconnect_btn.configure(state="normal", text="Reconnect"))

    def ask_yes_no(self, text):
        try:
            from tkinter import messagebox
            return bool(messagebox.askyesno(APP_TITLE, text))
        except Exception:
            return True

    # -- Spotify badge colours ---------------------------------------------
    def paint_swatches(self):
        dyn = bool(self.vars["spotify_dynamic"].get())
        for k, b in getattr(self, "sw_btns", {}).items():
            c = norm_hex(self.vars[k].get(), DEFAULTS[k])
            r, g, bl = _rgb(c)
            dark_text = (0.299 * r + 0.587 * g + 0.114 * bl) > 150
            try:
                b.configure(fg_color=c, hover_color=c, text_color="#000000" if dark_text else "#FFFFFF",
                            state="disabled" if dyn else "normal")
            except Exception:
                pass
        try:   # Dynamic: lit up while on (the two colours above are then ignored)
            self.dyn_btn.configure(fg_color=ACC if dyn else PANEL2,
                                   hover_color=ACC_H if dyn else HOVER, text_color="#FFFFFF")
        except Exception:
            pass

    def toggle_dynamic_badge(self):
        self.vars["spotify_dynamic"].set(not self.vars["spotify_dynamic"].get())
        self.paint_swatches()

    def pick_badge_color(self, key):
        from tkinter import colorchooser
        cur = norm_hex(self.vars[key].get(), DEFAULTS[key])
        try:
            _rgbv, hx = colorchooser.askcolor(color=cur, parent=self,
                                              title="Logo background" if key == "spotify_bg" else "Logo symbol")
        except Exception as e:
            log_error(f"colour picker: {type(e).__name__}: {e}")
            return
        if hx:
            self.vars[key].set(norm_hex(hx, cur))
            self.paint_swatches()

    def reset_badge_colors(self):
        for k in ("spotify_bg", "spotify_fg"):
            self.vars[k].set(DEFAULTS[k])
        self.vars["spotify_dynamic"].set(False)
        self.paint_swatches()

    # -- app icon ------------------------------------------------------------
    def choose_icon(self):
        from tkinter import filedialog, messagebox
        p = filedialog.askopenfilename(
            parent=self, title="Choose an app icon",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.ico"), ("All files", "*.*")])
        if not p:
            return
        try:
            im = Image.open(p)
            im.load()
            im = im.convert("RGBA")
            side = min(im.size)
            left, top = (im.width - side) // 2, (im.height - side) // 2
            im = im.crop((left, top, left + side, top + side)).resize((256, 256), Image.LANCZOS)
            os.makedirs(CONFIG_DIR, exist_ok=True)
            im.save(CUSTOM_ICON, "PNG")
        except Exception as e:
            log_error(f"choose icon: {type(e).__name__}: {e}")
            messagebox.showerror(APP_TITLE, "Couldn't use that image. Try a PNG or JPG.")
            return
        self.apply_icon()

    def reset_icon(self):
        try:
            if os.path.isfile(CUSTOM_ICON):
                os.remove(CUSTOM_ICON)
        except Exception as e:
            log_error(f"reset icon: {e}")
        self.apply_icon()

    def apply_icon(self):
        """Pushes the current icon everywhere it shows: window, taskbar, tray, installed shortcuts."""
        _ICON_CACHE.clear()
        self.set_icon()
        try:
            if self.tray is not None:
                self.tray.icon = make_icon()
        except Exception as e:
            log_error(f"tray icon: {e}")
        try:     # the installer's shortcuts point at <install folder>\AudioTwizz.ico - overwrite it in place
            inst = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(inst, "AudioTwizz.ico")
            if os.path.isfile(ico):
                make_icon(256).save(ico, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            import ctypes
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)     # make Explorer re-read icons
        except Exception as e:
            log_error(f"shortcut icon: {e}")
        try:
            self.icon_lbl.configure(text="Custom icon in use" if os.path.isfile(CUSTOM_ICON) else "Default icon")
        except Exception:
            pass

    def open_config_dir(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(CONFIG_DIR)
        except Exception as e:
            log_error(f"open folder: {e}")

    def toggle_presence(self):
        on = not self.worker.enabled.is_set()
        if on and not self.settings["client_id"].strip():
            self.show_page("set")
            self.tabs.set("General")
            self.save_lbl.configure(text="Paste your Application ID and press Save first.", text_color=YELLOW)
            return
        if not on and self.settings.get("confirm_disable", True) and not self.ask_yes_no(
                "Turn Discord presence off?\n\nYour status will be cleared until you turn it back on.\n"
                "(To hide it for a moment, use \"Pause presence\" instead.)"):
            return
        self.set_presence(on)

    # -- queue / ui refresh ------------------------------------------------
    def drain(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                if msg[0] == "status":
                    self.dot.configure(text_color=msg[2])
                    self.status_lbl.configure(text=msg[1])
                elif msg[0] == "np":
                    self.apply_np(*msg[1:])
                elif msg[0] == "lib":
                    pass   # the library page is gone; scan updates are ignored
                elif msg[0] == "appname":
                    self.set_appname(msg[1])
                elif msg[0] == "conn":
                    self.refresh_tray()
                elif msg[0] == "cmd":
                    self.run_cmd(msg[1])
                elif msg[0] == "hide":
                    self.update_toggle()
                    self.refresh_mini()
                elif msg[0] == "update":
                    self.on_update_result(msg[1])
                elif msg[0] == "update_progress":
                    try:
                        self.update_lbl.configure(text=msg[1], text_color=SUB)
                    except Exception:
                        pass
                elif msg[0] == "update_error":
                    self.on_update_error(msg[1])
                elif msg[0] == "update_done":
                    self.on_update_done()
        except queue.Empty:
            pass
        except Exception as e:
            log_error(f"ui: {type(e).__name__}: {e}")
        self.refresh_tray()
        self.after(250, self.drain)

    def schedule_preview(self):
        if self._pv_job is None:
            self._pv_job = self.after(120, self._run_preview)

    def _run_preview(self):
        self._pv_job = None
        try:
            self.refresh_previews()
        except Exception as e:
            log_error(f"preview: {type(e).__name__}: {e}")

    def pause_view(self, now=None, s=None):
        """(paused, note): note says whether Discord still shows the song or has cleared it."""
        info = self.np
        if not info or info["playing"]:
            return False, ""
        s = s or self.settings
        if s.get("pause_mode") == PAUSE_MODES[1]:
            return True, "  \u00b7  status kept"
        now = time.time() if now is None else now
        since = self.paused_since if self.paused_since is not None else now
        if now - since >= int(s.get("pause_delay", 6)):
            return True, "  \u00b7  cleared on Discord"
        return True, ""

    def refresh_previews(self):
        self._refresh_previews_inner()
        self.refresh_mini()

    def mini_status(self):
        w = self.worker
        if not w.enabled.is_set():
            return "PRESENCE OFF", GREY
        if w.hold.is_set():
            return "PAUSED", YELLOW
        why = w.hide_reason()
        if why:
            return ("PRIVATE" if w.privacy.is_set() else "HIDDEN \u00b7 " + clip(why.upper(), 22)), YELLOW
        if w.conn != "connected":
            return "WAITING FOR DISCORD", GREY
        if self.np and self.np.get("title"):
            return ("LIVE ON DISCORD", GREEN) if self.np["playing"] else ("PAUSED", YELLOW)
        return "CONNECTED", GREEN

    def refresh_mini(self):
        m = self.mini
        if m is None:
            return
        try:
            text, color = self.mini_status()
            info = self.np
            if info and info.get("title"):
                meta = self.meta or {}
                vals = track_values(info, meta)
                s2 = dict(self.settings, show_progress=True, show_cover=False)
                act = build_activity(info, meta, s2, time.time(), paused=not info["playing"], badge=False)
                m.render(vals["title"], vals["artist"], act, self.cover_data, text, color, not info["playing"])
            else:
                m.render("", "", None, None, text, color, False)
        except Exception as e:
            log_error(f"mini: {type(e).__name__}: {e}")

    # -- mini mode / privacy / clipboard ---------------------------------------
    def enter_mini(self):
        if self.mini is not None:
            return
        try:
            if self.state() not in ("withdrawn", "iconic"):
                self.save_window_state(persist=False)
            self.mini = MiniPlayer(self)
        except Exception as e:
            log_error(f"mini open: {type(e).__name__}: {e}")
            self.mini = None
            return
        self.withdraw()
        self.settings["mini_mode"] = True
        self.persist()
        self.refresh_mini()
        self.refresh_tray()

    def _close_mini(self):
        m, self.mini = self.mini, None
        if m is None:
            return
        try:
            m.withdraw()
            self.after(60, m.destroy)      # not from inside one of its own button callbacks
        except Exception:
            pass
        self.settings["mini_mode"] = False
        self.persist()
        self.refresh_tray()

    def close_mini_to_tray(self):
        if self.tray is None or pystray is None:
            self.quit_app()
            return
        self._close_mini()
        self.settings["last_hidden"] = True
        self.persist()

    def toggle_mini(self):
        if self.mini is not None:
            self.show_window()
        else:
            self.enter_mini()

    def toggle_privacy(self):
        w = self.worker
        if w.privacy.is_set():
            w.privacy.clear()
        else:
            w.privacy.set()
        self.settings["privacy_mode"] = w.privacy.is_set()
        self.persist()
        w.wake.set()
        self.update_toggle()
        self.refresh_mini()

    def copy_now_playing(self):
        info, m = self.np, self.meta or {}
        if not info or not info.get("title"):
            return
        vals = track_values(info, m)
        line = f"{vals['artist']} - {vals['title']}" if vals.get("artist") else vals["title"]
        link = ((m.get("song_link") or {}).get("url")) or ""
        try:
            self.clipboard_clear()
            self.clipboard_append(line + (("\n" + link) if link else ""))
            self.update()
        except Exception as e:
            log_error(f"clipboard: {type(e).__name__}: {e}")
            return
        try:
            self.copy_btn.configure(text="Copied!")
            self.after(1500, lambda: self.copy_btn.configure(text="Copy now playing"))
        except Exception:
            pass
        if self.mini is not None:
            self.mini.status.configure(text="COPIED TO CLIPBOARD", text_color=ACC)
            self.after(1500, self.refresh_mini)

    def apply_hotkeys(self):
        on = bool(self.settings.get("hotkeys")) and sys.platform == "win32"
        if on and self.hotkeys is None:
            self.hotkeys = HotkeyThread(self.ui_q)
            self.hotkeys.start()
        elif not on and self.hotkeys is not None:
            self.hotkeys.stop()
            self.hotkeys = None

    def refresh_procs(self):
        names = running_app_names() or ["(none found)"]
        self.proc_menu.configure(values=names)
        self.proc_var.set(names[0])

    def add_proc(self):
        n = self.proc_var.get().strip().lower()
        if not n or n.startswith("("):
            return
        cur = [x.strip() for x in self.vars["auto_hide_apps"].get().split(",") if x.strip()]
        if n not in [c.lower() for c in cur]:
            cur.append(n)
        self.vars["auto_hide_apps"].set(", ".join(cur))
        self.save_lbl.configure(text="Press Save to apply.", text_color=SUB)

    def _refresh_previews_inner(self):
        """Redraws both cards with the same function that builds what's sent to Discord."""
        now = time.time()
        self._last_pv = now
        head = self.header_text()
        info, meta = self.np, self.meta
        page = getattr(self, "_page", None)
        draw_now, draw_set = page == "now", page == "set"
        if info:
            paused = not info["playing"]
            ui = self.ui_settings()
            # "clear on pause" shows nothing on Discord, so only the kept-status mode carries the pause symbol
            act_now = build_activity(info, meta, self.settings, now, paused=paused,
                                     badge=self.settings.get("pause_mode") == PAUSE_MODES[1])
            _, note = self.pause_view(now)
            if draw_now:
                self.now_card.render(act_now, self.cover_data, head, paused, note)
            ui_act = build_activity(info, meta, ui, now, paused=paused,
                                    badge=ui.get("pause_mode") == PAUSE_MODES[1])
            _, note2 = self.pause_view(now, ui)
            if draw_set:
                self.set_card.render(ui_act, self.cover_data, head, paused, note2)
            self._pause_view = (paused, note)
            parts = [x for x in (_plain(act_now["details"]), _plain(act_now["state"]), _plain(act_now["large_text"])) if x]
            sent = "  |  ".join(parts) or "(no text)"
            if act_now["show_time"] and not paused:
                sent += f"  |  {fmt_time(act_now['pos'])} / " + (fmt_time(act_now["dur"]) if act_now["dur"] > 0 else "no length")
            if act_now["large_image"]:
                sent += "  |  cover"
            self.detail["sent"].configure(text=sent)
        else:
            if draw_now:
                self.now_card.render(None, None, head)
            sample = dict(SAMPLE_INFO, t=now)
            ui_act = build_activity(sample, SAMPLE_META, self.ui_settings(), now, paused=True, badge=False)
            if draw_set:
                self.set_card.render(ui_act, None, head + "  \u00b7  sample")
            self._pause_view = None
            self.detail["sent"].configure(text="-")

    def apply_np(self, info, cover_bytes, ts, err=None, meta=None):
        self.np, self.np_time, self.meta, self.cover_data = info, ts, meta or {}, cover_bytes
        if info and not info["playing"]:
            if self.paused_since is None:
                self.paused_since = ts
        else:
            self.paused_since = None
        try:
            self.theme_from_cover(cover_bytes)
        except Exception as e:
            log_error(f"theme: {type(e).__name__}: {e}")
        self.maybe_toast(info)
        apps = list(getattr(self.worker, "seen_apps", []) or [])
        if apps != self.shown_apps:
            self.shown_apps = apps
            self.update_players(apps)
        if not info:
            for k, v in self.detail.items():
                v.configure(text="-")
            if self.meta.get("skipped"):
                self.err_lbl.configure(text="Ignoring this track.", text_color=YELLOW)
            else:
                if err:
                    self.err_lbl.configure(text=f"Can't read media controls: {err}", text_color=RED)
                else:
                    self.err_lbl.configure(text=self.meta.get("diag") or "", text_color=YELLOW)
            self.refresh_previews()
            return
        m = self.meta
        album = m.get("album") or ""
        self.err_lbl.configure(text="")

        d = self.detail
        d["player"].configure(text=(info["app"] or "unknown player") + ("" if info["playing"] else "  (paused)"))
        if m.get("cover_src"):
            txt = "From " + m["cover_src"]
            if m.get("cover_url"):
                txt += "  \u00b7  sent to Discord"
            elif m.get("cover_note"):
                txt += "  \u00b7  " + m["cover_note"]
            elif self.settings["show_cover"]:
                txt += "  \u00b7  preparing for Discord..."
        elif m.get("cover_url"):
            txt = "Found online  \u00b7  sent to Discord"
        else:
            txt = "None found - add your music folder in Library, or embed the art in the file"
        d["cover"].configure(text=txt)
        d["album"].configure(text=(album + ("" if m.get("album_src") in ("", "player", None) else f"  (from {m['album_src']})")) if album else "-")
        dur = m.get("dur") or info["dur"]
        d["length"].configure(
            text=(fmt_time(dur) + ("" if m.get("dur_src") in ("", "player", None) else f"  (from {m['dur_src']})"))
            if dur > 0 else "Unknown - Discord will show elapsed time only"
        )
        self.refresh_previews()

    def tick(self):
        now = time.time()
        try:
            self.now_card.tick(now)
            self.set_card.tick(now)
            if self.mini is not None:
                self.mini.tick(now)
            # keep the preview's timestamps fresh while a song plays
            if self.np and self.np["playing"] and now - self._last_pv >= 5:
                self.refresh_previews()
            elif self.np and not self.np["playing"] and self._pause_view != self.pause_view(now):
                self.refresh_previews()
        except Exception as e:
            log_error(f"tick: {type(e).__name__}: {e}")
        self.after(250, self.tick)

    # -- tray / lifecycle --------------------------------------------------
    def tray_snapshot(self):
        """Everything the tray menu shows, as plain strings (compared to skip pointless redraws)."""
        w = self.worker
        enabled, held = w.enabled.is_set(), w.hold.is_set()
        conn = {
            "connected": "\u25cf Connected to Discord",
            "connecting": "\u25cf Connecting to Discord...",
            "waiting": "\u25cf Waiting for Discord...",
            "invalid": "\u25cf Invalid Application ID",
        }.get(w.conn)
        if conn is None:
            conn = "\u25cf Presence is off" if self.settings.get("client_id", "").strip() else "\u25cf Add your Application ID"
        info, m = self.np, self.meta or {}
        if info and info.get("title"):
            title = m.get("title") or info["title"]
            artist = m.get("artist") or info.get("artist") or ""
            album = m.get("album") or info.get("album") or ""
            state = "\u25cf Playing" if info.get("playing") else "\u25cf Paused"
        else:
            title = artist = album = ""
            state = "\u25cf Waiting for playback" if getattr(w, "seen_apps", None) else "\u25cf No player detected"
        snap = {
            "head": f"{APP_TITLE}  {conn}",
            "np": "Now Playing: " + (clip(title, 48) if title else "\u2014"),
            "artist": clip(artist, 52), "album": clip(album, 52),
            "status": "Status: " + state,
            "toggle": "Disable Presence" if enabled else "Enable Presence",
            "hold": "Resume Presence" if held else "Pause Presence",
            "can_hold": enabled,
        }
        why = w.hide_reason()
        if why:
            snap["status"] = "Status: Hidden (" + why + ")"
        snap["privacy"] = w.privacy.is_set()
        snap["mini"] = self.mini is not None
        snap["np_ok"] = bool(title)
        tip = f"{APP_TITLE} - {conn[2:]}"
        if title:
            tip += f"\n{clip(title, 40)}" + (f" - {clip(artist, 30)}" if artist else "")
        snap["tip"] = tip[:127]
        return snap

    def refresh_tray(self):
        try:
            snap = self.tray_snapshot()
        except Exception as e:
            log_error(f"tray snapshot: {type(e).__name__}: {e}")
            return
        if snap == self.tray_state:
            return
        self.tray_state = snap
        if self.tray is not None:
            try:
                self.tray.title = snap["tip"]
                self.tray.update_menu()
            except Exception as e:
                log_error(f"tray update: {type(e).__name__}: {e}")

    def build_tray_menu(self):
        M, I = pystray.Menu, pystray.MenuItem
        st = lambda: self.tray_state

        def cmd(name):
            return lambda icon=None, item=None: self.ui_q.put(("cmd", name))

        return M(
            I(lambda i: st().get("head", APP_TITLE), None, enabled=False),
            M.SEPARATOR,
            I(lambda i: st().get("np", "Now Playing: \u2014"), None, enabled=False),
            I(lambda i: "    " + st().get("artist", ""), None, enabled=False, visible=lambda i: bool(st().get("artist"))),
            I(lambda i: "    " + st().get("album", ""), None, enabled=False, visible=lambda i: bool(st().get("album"))),
            I(lambda i: st().get("status", ""), None, enabled=False),
            M.SEPARATOR,
            I(lambda i: st().get("toggle", "Enable Presence"), cmd("toggle")),
            I(lambda i: st().get("hold", "Pause Presence"), cmd("hold"), enabled=lambda i: bool(st().get("can_hold"))),
            I("Privacy mode (hide status)", cmd("privacy"), checked=lambda i: bool(st().get("privacy"))),
            I("Mini mode", cmd("mini"), checked=lambda i: bool(st().get("mini"))),
            I("Copy Now Playing", cmd("copy"), enabled=lambda i: bool(st().get("np_ok"))),
            M.SEPARATOR,
            I(f"Open {APP_TITLE}", cmd("open"), default=True),
            I("Open Presence Studio", cmd("studio")),
            I("Settings", cmd("settings")),
            M.SEPARATOR,
            I("Check for Updates", cmd("update")),
            I("Exit", cmd("exit")),
        )

    def start_tray(self):
        if pystray is None or self.tray is not None:
            return
        try:
            self.refresh_tray()
            self.tray = pystray.Icon(APP_NAME, make_icon(), self.tray_state.get("tip", APP_TITLE), self.build_tray_menu())
            threading.Thread(target=self._run_tray, daemon=True).start()
        except Exception as e:
            self.tray = None
            log_error(f"tray: {type(e).__name__}: {e}")

    def _run_tray(self):
        if sys.platform == "win32" and AUTOSTART:
            try:    # at sign-in Explorer's taskbar may not exist yet; an icon added too early never shows
                import ctypes
                for _ in range(120):
                    if ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None):
                        break
                    time.sleep(0.5)
            except Exception:
                pass
        try:
            self.tray.run()
        except Exception as e:
            log_error(f"tray run: {type(e).__name__}: {e}")

    def run_cmd(self, name):
        """Tray menu actions (queued from the tray thread, executed on the UI thread)."""
        if name == "toggle":
            self.toggle_presence()
        elif name == "hold":
            self.toggle_hold()
        elif name == "privacy":
            self.toggle_privacy()
        elif name == "mini":
            self.toggle_mini()
        elif name == "copy":
            self.copy_now_playing()
        elif name == "open":
            self.show_window()
        elif name == "studio":
            self.show_window()
            self.show_page("set")
            self.tabs.set("Display")
        elif name == "settings":
            self.show_window()
            self.show_page("set")
            self.tabs.set("General")
        elif name == "update":
            self.check_updates(False)
        elif name == "exit":
            self.quit_app()

    def hidden(self):
        try:
            return self.state() in ("withdrawn", "iconic")
        except Exception:
            return False

    def _keep_hidden(self):
        """Used while starting minimized: hide again unless the person has opened the window since."""
        if self._user_opened:
            return
        try:
            if self.state() in ("normal", "zoomed"):
                self.hide_window()
            elif self.state() == "withdrawn" and not self.settings.get("last_hidden"):
                self.hide_window()
        except Exception as e:
            log_error(f"keep hidden: {e}")

    def sync_startup(self):
        """Make the Windows startup entry match the saved setting (repairs it after a reinstall / move)."""
        if sys.platform != "win32":
            return
        try:
            set_startup(bool(self.settings.get("start_with_windows")), bool(self.settings.get("start_minimized")))
        except Exception as e:
            log_error(f"startup entry: {type(e).__name__}: {e}")

    def apply_startup_now(self):
        """The two startup switches take effect the moment they are flipped (no need to press Save)."""
        try:
            self.settings["start_with_windows"] = bool(self.vars["start_with_windows"].get())
            self.settings["start_minimized"] = bool(self.vars["start_minimized"].get())
            set_startup(self.settings["start_with_windows"], self.settings["start_minimized"])
            self.persist()
            lbl = getattr(self, "save_lbl", None)
            if lbl is not None:
                lbl.configure(text="Startup options applied.", text_color=GREEN)
        except Exception as e:
            log_error(f"startup switch: {type(e).__name__}: {e}")
            lbl = getattr(self, "save_lbl", None)
            if lbl is not None:
                lbl.configure(text=f"Couldn't change the Windows startup entry: {e}", text_color=RED)

    def hide_window(self):
        self.save_window_state()
        if pystray is None or self.tray is None:
            self.iconify()
            return
        self.withdraw()
        self.settings["last_hidden"] = True
        self.persist()

    def show_window(self):
        self._user_opened = True
        if self.mini is not None:
            self._close_mini()
        self.deiconify()
        self.state("zoomed" if self.settings.get("window_zoomed") else "normal")
        self.lift()
        self.focus_force()
        if self.settings.get("last_hidden"):
            self.settings["last_hidden"] = False
            self.persist()

    def on_close(self):
        if self.settings["close_to_tray"] and pystray is not None and self.tray is not None:
            self.hide_window()
        else:
            self.quit_app()

    def quit_app(self):
        try:
            self.settings["last_hidden"] = self.state() == "withdrawn"
            self.save_window_state(persist=False)
            self.persist()
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.hotkeys is not None:
            self.hotkeys.stop()
        self.worker.stop_evt.set()
        self.worker.wake.set()
        self.worker.join(timeout=3)
        self.destroy()

    # -- window size / position -----------------------------------------------
    def _virtual_screen(self):
        try:
            if sys.platform == "win32":
                import ctypes
                gm = ctypes.windll.user32.GetSystemMetrics
                l, t, w, h = gm(76), gm(77), gm(78), gm(79)
                if w > 0 and h > 0:
                    return l, t, l + w, t + h
        except Exception:
            pass
        return 0, 0, int(self.winfo_screenwidth()), int(self.winfo_screenheight())

    def _scaling(self):
        try:
            return float(self._get_window_scaling())
        except Exception:
            return 1.0

    def restore_geometry(self):
        default = "920x660"
        try:
            if not self.settings.get("compact_ui"):    # first launch of the compact layout: forget the old big size
                self.settings["compact_ui"] = True
                self.settings["window_geometry"] = ""
                self.settings["window_zoomed"] = False
            m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", self.settings.get("window_geometry") or "")
            if not m:
                self.geometry(default)
                return
            w, h, x, y = (int(v) for v in m.groups())
            l, t, r, b = self._virtual_screen()
            sc = self._scaling()
            w = max(860, min(w, int((r - l) / sc)))
            h = max(600, min(h, int((b - t) / sc)))
            x = max(l - int(w * sc) + 160, min(x, r - 160))     # keep a grabbable part on a connected screen
            y = max(t, min(y, b - 120))
            self.geometry(f"{w}x{h}+{x}+{y}")
            if self.settings.get("window_zoomed") and not getattr(self, "_starting_hidden", False):
                self.after(50, lambda: self.state("zoomed"))
        except Exception as e:
            log_error(f"restore geometry: {e}")
            self.geometry(default)

    def save_window_state(self, persist=True):
        try:
            st = self.state()
            if st == "zoomed":
                self.settings["window_zoomed"] = True
            elif st == "normal":
                sc = self._scaling()
                self.settings["window_zoomed"] = False
                self.settings["window_geometry"] = "%dx%d+%d+%d" % (
                    round(self.winfo_width() / sc), round(self.winfo_height() / sc), self.winfo_x(), self.winfo_y())
            else:
                return
            if persist:
                self.persist()
        except Exception as e:
            log_error(f"save geometry: {e}")

    def _on_configure(self, event):
        if event.widget is not self:
            return
        if self._geo_job is not None:
            try:
                self.after_cancel(self._geo_job)
            except Exception:
                pass
        self._geo_job = self.after(800, self.save_window_state)

    # -- notifications ---------------------------------------------------------
    def notify(self, title, text):
        if self.tray is None:
            return False
        try:
            if getattr(self.tray, "HAS_NOTIFICATION", True):
                self.tray.notify(text, title)
                return True
        except Exception as e:
            log_error(f"notify: {e}")
        return False

    def maybe_toast(self, info):
        """Subtle toast on a song change - only while hidden, never for the song already playing at launch,
        never when nothing is playing, and at most one every few seconds."""
        if not info or not info.get("title") or not info.get("playing"):
            return
        key = (info.get("title"), info.get("artist"))
        if not self._np_seen:
            self._np_seen, self._notified = True, key
            return
        if key == self._notified:
            return
        self._notified = key
        now = time.time()
        if not self.settings.get("notify_song_change") or not self.hidden() or now - self._last_toast < 3:
            return
        self._last_toast = now
        m = self.meta or {}
        artist, album = m.get("artist") or info.get("artist") or "", m.get("album") or info.get("album") or ""
        self.notify(clip(m.get("title") or info["title"], 60), clip(" \u2014 ".join(v for v in (artist, album) if v), 80) or " ")

    # -- diagnostics / updates -------------------------------------------------
    def diagnostics_text(self):
        w, s = self.worker, self.settings
        cid = s.get("client_id", "").strip()
        info = self.np or {}
        lines = [
            f"{APP_TITLE} {APP_VERSION}",
            f"Python {sys.version.split()[0]} ({'packaged exe' if getattr(sys, 'frozen', False) else 'source'})",
            f"OS: {platform.platform()}",
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"Discord connection: {w.conn}",
            f"Presence enabled: {w.enabled.is_set()}   paused: {w.hold.is_set()}",
            f"Application ID: {'set (...' + cid[-4:] + ')' if cid else 'NOT SET'}",
            f"Players seen: {', '.join(getattr(w, 'seen_apps', []) or []) or 'none'}",
            f"Now playing: {info.get('app', '-')} | playing={info.get('playing', '-')} | "
            f"{(info.get('artist') or '-')} - {(info.get('title') or '-')}" if info else "Now playing: nothing",
            f"Reader note: {(self.meta or {}).get('diag') or '-'}",
            f"Last read error: {getattr(w, 'last_err', None) or '-'}",
            f"Library: {len(self.library.tracks):,} tracks in {len(s.get('music_folders') or [])} folder(s); "
            f"scanning={self.library.scanning}",
            f"Tray available: {self.tray is not None}   tags module: {mutagen is not None}",
            "",
            "Settings: " + ", ".join(f"{k}={s.get(k)}" for k in (
                "poll_seconds", "pause_mode", "pause_delay", "show_cover", "upload_covers", "online_lookup",
                "use_library", "close_to_tray", "start_with_windows", "start_minimized", "notify_song_change")),
        ]
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read().splitlines()[-25:]
            lines += ["", "--- last log lines ---"] + tail
        except OSError:
            lines += ["", "(no error log - no errors recorded)"]
        return "\n".join(str(x) for x in lines)

    def copy_diagnostics(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self.diagnostics_text())
            self.update()
            self.about_lbl.configure(text="Copied - paste it into your bug report.", text_color=GREEN)
        except Exception as e:
            self.about_lbl.configure(text=f"Could not copy: {e}", text_color=RED)

    # -- self-hosted auto-update --------------------------------------------
    def check_updates(self, from_ui):
        """Ask our own update server (UPDATE_SERVER) whether a newer build exists.
        from_ui=True: triggered from the Settings > About button (updates that label).
        from_ui=False: triggered automatically on startup / periodically, or from the tray."""
        if not UPDATE_SERVER:
            self.on_update_result({"ok": False, "msg": "Update checking isn't set up in this build.", "ui": from_ui})
            return
        if getattr(self, "_update_in_progress", False):
            return
        try:
            self.update_btn.configure(state="disabled", text="Checking...")
            self.update_lbl.configure(text="Contacting the update server...", text_color=SUB)
        except Exception:
            pass

        def work():
            res = {"ok": False, "ui": from_ui}
            try:
                data = _http_json(UPDATE_SERVER.rstrip("/") + "/api/latest")
                ver = str(data.get("version") or "").strip()
                num = lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)
                res.update(
                    ok=True, version=ver, notes=data.get("notes") or "",
                    app_url=UPDATE_SERVER.rstrip("/") + "/releases/latest/local_presence_app.py",
                    app_sha256=(data.get("app_sha256") or "").lower(),
                    requirements_url=(UPDATE_SERVER.rstrip("/") + "/releases/latest/requirements.txt")
                    if data.get("requirements_sha256") else "",
                    requirements_sha256=(data.get("requirements_sha256") or "").lower(),
                    newer=bool(ver) and num(ver) > num(APP_VERSION),
                )
            except Exception as e:
                res["msg"] = ("Couldn't reach the update server right now. Check your internet connection, "
                              f"or try again in a bit. ({type(e).__name__})")
            self.ui_q.put(("update", res))

        threading.Thread(target=work, daemon=True).start()

    def on_update_result(self, res):
        try:
            self.update_btn.configure(state="normal", text="Check for updates")
        except Exception:
            pass
        if res.get("ok") and res.get("newer"):
            msg = f"Version {res['version']} is available (you have {APP_VERSION})."
            try:
                self.update_lbl.configure(text=msg, text_color=GREEN)
            except Exception:
                pass
            if self._declined_update_version == res.get("version"):
                return  # already said "not now" this session; don't nag again
            notes = (res.get("notes") or "").strip()
            prompt = f"{msg}\n\nUpdate now? {APP_TITLE} will close and reopen automatically."
            if notes:
                prompt += f"\n\nWhat's new:\n{notes[:400]}"
            if self.ask_yes_no(prompt):
                self.begin_update(res)
            else:
                self._declined_update_version = res.get("version")
            return
        if res.get("ok"):
            msg = f"You're up to date (v{APP_VERSION})."
        else:
            msg = res.get("msg") or "Couldn't check for updates."
        try:
            self.update_lbl.configure(text=msg, text_color=GREEN if res.get("ok") else YELLOW)
        except Exception:
            pass
        if not res.get("ui"):
            return  # silent background check with nothing to report: stay quiet
        if self.hidden():
            self.notify(APP_TITLE, msg)
        else:
            try:
                from tkinter import messagebox
                messagebox.showinfo(APP_TITLE, msg)
            except Exception:
                pass

    def begin_update(self, res):
        """Kick off a silent download-and-install of the version described by res."""
        self._update_in_progress = True
        try:
            self.update_btn.configure(state="disabled", text="Updating...")
            self.update_lbl.configure(text="Downloading update...", text_color=SUB)
        except Exception:
            pass
        if self.hidden():
            self.notify(APP_TITLE, "Downloading an update - AudioTwizz will restart shortly.")
        threading.Thread(target=self._apply_update, args=(res,), daemon=True).start()

    def _apply_update(self, res):
        import subprocess
        import tempfile

        def progress(text):
            self.ui_q.put(("update_progress", text))

        try:
            app_path = os.path.abspath(__file__)
            app_dir = os.path.dirname(app_path)

            # 1. Download the new app file into memory and check its hash before touching anything.
            progress("Downloading update...")
            app_bytes = _http_bytes(res["app_url"])
            if res.get("app_sha256") and hashlib.sha256(app_bytes).hexdigest() != res["app_sha256"]:
                raise RuntimeError("downloaded file didn't match the expected checksum")

            req_bytes = None
            req_path = os.path.join(app_dir, "requirements.txt")
            req_sig_path = os.path.join(app_dir, ".reqsig")
            req_changed = False
            if res.get("requirements_url"):
                req_bytes = _http_bytes(res["requirements_url"])
                if res.get("requirements_sha256") and hashlib.sha256(req_bytes).hexdigest() != res["requirements_sha256"]:
                    raise RuntimeError("requirements.txt didn't match the expected checksum")
                new_sig = hashlib.sha256(req_bytes).hexdigest()
                old_sig = ""
                try:
                    with open(req_sig_path, "r", encoding="utf-8") as f:
                        old_sig = f.read().strip()
                except OSError:
                    pass
                req_changed = new_sig != old_sig

            # 2. Write everything to temp files first, so a failure here never corrupts the install.
            fd, tmp_app = tempfile.mkstemp(dir=app_dir, suffix=".py.new")
            with os.fdopen(fd, "wb") as f:
                f.write(app_bytes)
            tmp_req = None
            if req_bytes is not None:
                fd, tmp_req = tempfile.mkstemp(dir=app_dir, suffix=".txt.new")
                with os.fdopen(fd, "wb") as f:
                    f.write(req_bytes)

            # 3. If the dependency list changed, install the new packages before swapping the
            #    app file in, using this same venv's python - no console window, nothing visible.
            if req_changed and tmp_req:
                progress("Installing updated dependencies...")
                py = sys.executable
                if py.lower().endswith("pythonw.exe"):
                    cand = py[:-len("pythonw.exe")] + "python.exe"
                    if os.path.exists(cand):
                        py = cand
                flags = 0
                if sys.platform == "win32":
                    flags = 0x08000000  # CREATE_NO_WINDOW
                cmd = [py, "-m", "pip", "install", "--disable-pip-version-check",
                       "--no-warn-script-location", "--prefer-binary", "-r", tmp_req]
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, creationflags=flags, timeout=600)
                try:
                    with open(LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(f"\n--- update pip install ---\n{proc.stdout.decode('utf-8', 'replace')}\n")
                except OSError:
                    pass
                if proc.returncode != 0:
                    raise RuntimeError("installing the updated dependencies failed; see error.log")

            # 4. Swap the files into place.
            progress("Installing update...")
            os.replace(tmp_app, app_path)
            if tmp_req:
                os.replace(tmp_req, req_path)
                with open(req_sig_path, "w", encoding="utf-8") as f:
                    f.write(hashlib.sha256(req_bytes).hexdigest())

            # 5. Relaunch a fresh process running the new file, then let this one exit.
            progress("Restarting...")
            release_single_instance()   # let the new copy take the lock immediately
            launch_args = [sys.executable, app_path] + [a for a in sys.argv[1:] if a not in (ONCE_FLAG, RESTART_FLAG)]
            launch_args.append(RESTART_FLAG)
            flags = 0
            if sys.platform == "win32":
                flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
            subprocess.Popen(launch_args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
            self.ui_q.put(("update_done", None))
        except Exception as e:
            if _si_sock is None:
                acquire_single_instance()
                if _si_sock is not None:
                    threading.Thread(target=_single_instance_listener,
                                     args=(lambda: self.ui_q.put(("cmd", "open")),), daemon=True).start()
            log_error(f"update: {type(e).__name__}: {e}")
            self.ui_q.put(("update_error", str(e)))

    def on_update_error(self, msg):
        self._update_in_progress = False
        try:
            self.update_btn.configure(state="normal", text="Check for updates")
            self.update_lbl.configure(text="Update failed - try again later.", text_color=RED)
        except Exception:
            pass
        if self.hidden():
            self.notify(APP_TITLE, "The update didn't go through. It'll try again next time.")
        else:
            try:
                from tkinter import messagebox
                messagebox.showerror(APP_TITLE, f"The update didn't go through:\n\n{msg}")
            except Exception:
                pass

    def on_update_done(self):
        # The new version is already starting up in a fresh process; just get out of its way.
        try:
            self.notify(APP_TITLE, "Update installed - reopening...")
        except Exception:
            pass
        self.after(300, lambda: os._exit(0))


def art_selftest(argv):
    """py local_presence_app.py --art-test <song-or-image> [client_id]
    Walks the whole pipeline on a real file and (with a client id and Discord open) shows it on your profile."""
    i = argv.index("--art-test")
    path = argv[i + 1] if len(argv) > i + 1 else ""
    cid = argv[i + 2] if len(argv) > i + 2 else load_settings().get("client_id", "")
    if not os.path.isfile(path):
        print("usage: --art-test <audio file or image> [discord application id]")
        return 2
    data = None
    if os.path.splitext(path)[1].lower() in AUDIO_EXT:
        data = read_cover(path) or folder_cover(path)
        src = "audio file"
    else:
        data, src = open(path, "rb").read(), "image file"
    if not data:
        print("ARTWORK MISSING \u2717 nothing embedded in that file and no folder image next to it")
        return 1
    try:
        print(f"ARTWORK EXTRACTED \u2713 {describe_image(data)} (from {src})")
    except ValueError as e:
        print(f"ARTWORK INVALID \u2717 {e}")
        return 1
    url = CoverHost().url_for(data, os.path.basename(path))
    if not url:
        print("UPLOAD FAILED \u2717 -", CoverHost().last_error or "see log")
        return 1
    print("PUBLIC URL:", url)
    if not cid:
        print("(no application id given - skipping the Discord step)")
        return 0
    rpc = Presence(cid)
    rpc.connect()
    act = {"details": "Art test", "state": os.path.basename(path)[:100], "large_image": url, "large_text": "cover test"}
    resp = send_activity(rpc, act)
    print("Look at your Discord profile now - the cover should be visible. Holding for 60 s...")
    time.sleep(60)
    rpc.clear()
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:  # own taskbar identity, so Windows shows AudioTwizz instead of "Python" / IDLE
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AudioTwizz.LocalPresence")
        except Exception:
            pass
    try:        # the Run entry starts programs in C:\\Windows\\System32; the shortcut starts in the app folder
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    except OSError:
        pass
    if "--art-test" in sys.argv:
        sys.exit(art_selftest(sys.argv))
    if not acquire_single_instance(wait=15 if RESTART_FLAG in sys.argv else 0):
        sys.exit(0)                    # already open - that copy was told to come to the front
    try:
        App(start_hidden="--minimized" in sys.argv).mainloop()
    except Exception:
        import traceback
        _tb = traceback.format_exc()
        log_error("STARTUP CRASH\n" + _tb)
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0, "AudioTwizz couldn't start.\n\n" + _tb[-1400:] + "\nSaved to:\n" + LOG_PATH,
                    "AudioTwizz", 0x10)
            except Exception:
                pass
        sys.exit(1)
