

import json
import os
import time

from .common import CONFIG_DIR

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
SERVERS_FILE = os.path.join(CONFIG_DIR, "servers.json")
IDENTITY_FILE = os.path.join(CONFIG_DIR, "identity")


DEFAULTS = {
    "name": "",           # display name default identity hash if none

    "input": None,        # microphone name, None = system default
    "output": None,       # speaker name, None = system default
    
    "low_latency": False,
    "jitter_ms": 200,     # receive jitter buffer depth

    "user_gains": {},     # identity hash local volume adjustment in dB
    "user_muted": [],     # identity hashes muted locally

    "mode": "ptt",        # ptt | vox | open
    "ptt_key": "v",       # key name as captured in Settings
    "ptt_toggle": False,  # press once to start, again to stop
    "ptt_global": True,   # system-wide hotkey via pynput when available
    "vad_db": -45.0,
    "vad_hang": 0.4,

    "window": None,       # window dimensions
    "sash": None,         # divider between chat and tree, in pixels from the left
    "theme": "light",     # light | dark
    "font_size": 10,      # text size in pt
    "debug_stats": False, # show bitrates and loss counters in the status bar

    "sfx": False,         # master switch for sound effects
    "sfx_join": True,     # per event switches
    "sfx_leave": True,
    "sfx_room": True,
    "sfx_disconnect": True,
    "sfx_ptt": True,
    "sfx_mute": True,
}



def _read_json(path, default):
    try:
        with open(path) as json_file:
            return json.load(json_file)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    temporary_path = path + ".tmp"
    with open(temporary_path, "w") as json_file:
        json.dump(data, json_file, indent=2)
    os.replace(temporary_path, path)


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        stored = _read_json(SETTINGS_FILE, {})
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in DEFAULTS:
                    self[key] = value

    def save(self):
        _write_json(SETTINGS_FILE, dict(self))


class ServerList:

    FIELDS = ("label", "hash", "name", "room", "password", "server_password")

    def __init__(self):
        self.entries = []
        stored = _read_json(SERVERS_FILE, [])
        if isinstance(stored, list):
            for stored_entry in stored:
                if isinstance(stored_entry, dict) and isinstance(stored_entry.get("hash"), str):
                    self.entries.append(self._normalise(stored_entry))

    def _normalise(self, raw_entry):
        entry = {}
        for field in self.FIELDS:
            entry[field] = raw_entry.get(field) or ""
        entry["last_used"] = float(raw_entry.get("last_used") or 0)
        return entry

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _write_json(SERVERS_FILE, self.entries)
        os.chmod(SERVERS_FILE, 0o600) 

    def add(self, **fields):
        entry = self._normalise(fields)
        self.entries.append(entry)
        self.save()
        return entry

    def update(self, entry, **fields):
        for field in self.FIELDS:
            if field in fields:
                entry[field] = fields.get(field) or ""
        self.save()

    def remove(self, entry):
        self.entries = [existing for existing in self.entries if existing is not entry]
        self.save()

    def touch(self, entry):
        entry["last_used"] = time.time()
        self.save()

    def find(self, destination_hash):
        for entry in self.entries:
            if entry["hash"].lower() == destination_hash.lower():
                return entry
        return None
