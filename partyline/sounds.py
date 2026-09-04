import os
import threading
import wave

import numpy as np
import RNS

SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")



EVENTS = {
    "join": "sfx_join",
    "leave": "sfx_leave",
    "room_join": "sfx_room",
    "room_leave": "sfx_room",
    "disconnect": "sfx_disconnect",
    "ptt_on": "sfx_ptt",
    "ptt_off": "sfx_ptt",
    "mute_on": "sfx_mute",
    "mute_off": "sfx_mute",
    "deafen_on": "sfx_mute",
    "deafen_off": "sfx_mute",
}


def load_wav(path):
    with wave.open(path, "rb") as wav_file:
        channels = wav_file.getnchannels()
        samplerate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels)[:, 0]
    return samples, samplerate


class SoundPlayer:
    def __init__(self, settings):
        self.settings = settings
        self.cache = {}
        self.lock = threading.Lock()

    def enabled(self, event):
        if not self.settings.get("sfx"):
            return False
        return bool(self.settings.get(EVENTS.get(event, ""), False))

    def play(self, event, force=False):
        if not force and not self.enabled(event):
            return
        threading.Thread(target=self._play, args=(event,), daemon=True).start()

    def _clip(self, event):
        with self.lock:
            if event not in self.cache:
                path = os.path.join(SOUND_DIR, event + ".wav")
                try:
                    self.cache[event] = load_wav(path)
                except Exception as error:
                    RNS.log(f"No sound for {event!r}: {error}", RNS.LOG_DEBUG)
                    self.cache[event] = None
            return self.cache[event]

    def _play(self, event):
        clip = self._clip(event)
        if clip is None:
            return
        samples, samplerate = clip
        try:
            import LXST.Sinks

            # LXST's backend finds the named speaker with the sound card library it is using
            backend = LXST.Sinks.Backend(preferred_device=self.settings.get("output") or None)
            backend.device.play(samples, samplerate=samplerate)
        except Exception as error:
            RNS.log(f"Could not play sound {event!r}: {error}", RNS.LOG_DEBUG)
