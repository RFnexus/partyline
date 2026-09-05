#!/usr/bin/env python3
"""
    
    An example music bot for Partyline
    Needs ffmpeg

    python3 bots/music_bot.py SERVER_HASH --folder ~/music --room Concert
    !play [N]  !pause  !resume  !skip  !stop  !list  !np  !loop  !help
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import RNS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from partyline.client import Client, Config
from partyline.common import CONFIG_DIR, load_identity, make_codec, parse_hash



SAMPLE_RATE = 48000


LIST_LIMIT = 280


HEADROOM = 0.8


RECONNECT_SECONDS = 5


def find_tracks(folder):
    found = []
    for pattern in ("*.mp3", "*.MP3"):
        found.extend(glob.glob(os.path.join(folder, pattern)))
    return sorted(set(found))


def decode_mp3(path, samplerate):
    command = ["ffmpeg", "-v", "quiet", "-i", path, "-ac", "1", "-ar", str(samplerate), "-f", "f32le", "-"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("ffmpeg could not decode it")
    return np.frombuffer(result.stdout, dtype="<f4").astype("float32")


class MusicPlayer:
    def __init__(self, client, folder, gain=1.0, samplerate=SAMPLE_RATE):
        self.client = client
        self.folder = folder
        self.gain = gain
        self.samplerate = samplerate
        self.tracks = find_tracks(folder)

        self.index = 0
        self.samples = np.zeros(0, dtype="float32")
        self.position = 0
        self.playing = False
        self.loop = False

        self.codec = None
        self.frame_samples = None
        self.running = False
        self.lock = threading.Lock()

    def bind(self):
        if self.client.audio_profile is None:
            return
        self.codec = make_codec(self.client.audio_profile)
        self.codec.source = self
        self.frame_samples = max(1, int(self.samplerate * self.client.frame_ms / 1000))
        if not self.running:
            self.running = True
            threading.Thread(target=self._job, daemon=True).start()

    def play(self, index):
        with self.lock:
            if not self.tracks:
                return "no tracks in the folder"
            if index is None and self.playing:
                return "already playing"
            if index is None and len(self.samples) > 0:
                self.playing = True
                return "resumed"
            if index is not None:
                self.index = index
            elif len(self.samples) == 0:
                self.index = 0
            self._load_current()
            self.playing = True
            return None

    def pause(self):
        with self.lock:
            if self.playing:
                self.playing = False
                return True
            return False

    def skip(self):
        with self.lock:
            if not self.tracks:
                return "no tracks in the folder"
            if not self._advance():
                self.playing = False
                self.samples = np.zeros(0, dtype="float32")
                self.position = 0
                return "end of playlist"
            self.playing = True
            return None

    def stop(self):
        with self.lock:
            self.playing = False
            self.samples = np.zeros(0, dtype="float32")
            self.position = 0
            self.index = 0

    def toggle_loop(self):
        self.loop = not self.loop
        return self.loop

    def now_playing(self):
        if not self.tracks:
            return "no tracks in the folder"
        if len(self.samples) == 0 and not self.playing:
            return "nothing playing"
        name = os.path.basename(self.tracks[self.index])
        state = "playing" if self.playing else "paused"
        return f"{state} ({self.index + 1}/{len(self.tracks)}): {name}"

    def list_tracks(self):
        if not self.tracks:
            return "no tracks in the folder"
        parts = [f"{number}) {os.path.basename(path)}" for number, path in enumerate(self.tracks, start=1)]
        listing = "  ".join(parts)
        if len(listing) > LIST_LIMIT:
            listing = listing[: LIST_LIMIT - 1] + "…"
        return listing

    def _advance(self):
        if self.index + 1 < len(self.tracks):
            self.index += 1
        elif self.loop:
            self.index = 0
        else:
            return False
        self._load_current()
        return True

    def _load_current(self):

        path = self.tracks[self.index]
        try:
            samples = decode_mp3(path, self.samplerate) * self.gain
        except Exception as error:
            self.samples = np.zeros(0, dtype="float32")
            self.position = 0
            self.client.send_text(f"could not play {os.path.basename(path)}: {error}")
            return
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        if peak > HEADROOM:
            samples = samples * (HEADROOM / peak)
        self.samples = samples.astype("float32")
        self.position = 0
        self.client.send_text(f"now playing ({self.index + 1}/{len(self.tracks)}): {os.path.basename(path)}")

    def _next_frame(self):

        with self.lock:
            attempts = 0
            while self.position + self.frame_samples > len(self.samples):
                if not self._advance():
                    return None
                attempts += 1
                if attempts > len(self.tracks):
                    return None
            start = self.position
            self.position += self.frame_samples
            return self.samples[start : self.position].reshape(-1, 1)

    def _job(self):

        next_frame_at = time.monotonic()
        started_packetizer = None
        while self.running:
            packetizer = self.client.packetizer
            if packetizer is not None and packetizer is not started_packetizer:
                packetizer.start()
                started_packetizer = packetizer

            if not self.playing or self.codec is None or self.frame_samples is None or packetizer is None:
                if packetizer is not None and not packetizer.squelched:
                    packetizer.squelch()
                time.sleep(0.05)
                next_frame_at = time.monotonic()
                continue

            frame = self._next_frame()
            if frame is None:
                with self.lock:
                    self.playing = False
                    self.samples = np.zeros(0, dtype="float32")
                    self.position = 0
                    self.index = 0
                self.client.send_text("playlist finished")
                continue

            if packetizer is not None:
                if packetizer.squelched:
                    packetizer.unsquelch()
                packetizer.handle_frame(self.codec.encode(frame), self)

            frame_seconds = self.frame_samples / self.samplerate
            next_frame_at += frame_seconds
            now = time.monotonic()
            if next_frame_at < now - 0.5:
                next_frame_at = now
            time.sleep(max(0.0, next_frame_at - now))


def handle_command(client, player, prefix, text):
    if not text.startswith(prefix):
        return
    body = text[len(prefix) :].strip()
    if not body:
        return
    name, _, rest = body.partition(" ")
    name = name.lower()
    rest = rest.strip()

    if name == "help":
        client.send_text(
            f"commands: {prefix}play [N], {prefix}pause, {prefix}resume, {prefix}skip, "
            f"{prefix}stop, {prefix}list, {prefix}np, {prefix}loop"
        )
    elif name == "play":
        index = None
        if rest:
            if not rest.isdigit() or not 1 <= int(rest) <= len(player.tracks):
                client.send_text(f"pick a track 1-{len(player.tracks)}")
                return
            index = int(rest) - 1
        message = player.play(index)
        if message:
            client.send_text(message)
    elif name in ("skip", "next"):
        message = player.skip()
        if message:
            client.send_text(message)
    elif name == "stop":
        player.stop()
        client.send_text("stopped")
    elif name == "pause":
        client.send_text("paused" if player.pause() else "nothing is playing")
    elif name == "resume":
        message = player.play(None)
        if message:
            client.send_text(message)
    elif name in ("list", "songs"):
        client.send_text(player.list_tracks())
    elif name == "np":
        client.send_text(player.now_playing())
    elif name == "loop":
        client.send_text(f"loop {'on' if player.toggle_loop() else 'off'}")
    else:
        client.send_text(f"unknown command; say {prefix}help")


def main():

    parser = argparse.ArgumentParser(description="Partyline music bot")
    parser.add_argument("server", help="destination hash printed by partyline-server")
    parser.add_argument("--folder", required=True, help="folder of MP3 files to play")
    parser.add_argument("--room", default=None, help="room to join")
    parser.add_argument("--name", default="MusicBot", help="display name")
    parser.add_argument("--identity", default=os.path.join(CONFIG_DIR, "bot_identity"), help="identity file")
    parser.add_argument("--password", default=None, help="password for --room")
    parser.add_argument("--server-password", default=None, help="server password if the server asks for one")
    parser.add_argument("--configdir", default=None, help="Reticulum config directory (default ~/.reticulum)")
    parser.add_argument("--prefix", default="!", help="chat command prefix")
    parser.add_argument("--gain", type=float, default=1.0, help="volume multiplier applied before encoding")
    parser.add_argument("--autoplay", action="store_true", help="start playing as soon as the bot joins")
    args = parser.parse_args()

    

    try:
        server_hash = parse_hash(args.server)
    except ValueError as error:
        parser.error(str(error))

    folder = os.path.expanduser(args.folder)
    if not os.path.isdir(folder):
        raise SystemExit(f"no such folder: {folder}")
    if shutil.which("ffmpeg") is None:
        print("warning: ffmpeg was not found on PATH, the bot cannot decode MP3s", flush=True)

    RNS.Reticulum(configdir=args.configdir)
    identity = load_identity(args.identity)
    print(f"Bot identity hash: {identity.hash.hex()}", flush=True)

    client = Client(Config(listen=True, null_audio=True), identity, args.name)
    player = MusicPlayer(client, folder, gain=args.gain)
    print(f"{len(player.tracks)} track(s) in {folder}", flush=True)

    try:
        while True:
            client.connect(server_hash, room=args.room, password=args.password, server_password=args.server_password)

            greeted = False
            while client.state != "closed":
                while client.events:
                    event = client.events.popleft()
                    kind = event[0]
                    if kind == "connected":
                        print(f"connected to {client.server_name!r}", flush=True)
                    elif kind == "room" and event[1] is not None:
                        player.bind()
                        channel = client.channels.get(event[1])
                        print(f"in room {channel.name if channel else event[1]}", flush=True)
                        if not greeted:
                            greeted = True
                            if not client.can_speak_here:
                                print("warning: not a speaker in this room", flush=True)
                                client.send_text("I'm not allowed to talk in this room. If you are the op add my identity hash to music:[] in your server config")
                            client.send_text(f"Music Bot ready, {len(player.tracks)} track(s). say {args.prefix}help")
                            if args.autoplay and client.can_speak_here:
                                player.play(None)
                    elif kind == "text":
                        sender = event[1]
                        if getattr(sender, "sid", None) != client.my_sid:
                            handle_command(client, player, args.prefix, event[2])
                    elif kind == "denied":
                        print(f"denied: {event[2]}", flush=True)
                    elif kind in ("error", "closed"):
                        print(f"{kind}: {event[1]}", flush=True)
                if client.error and client.state == "idle":
                    break
                time.sleep(0.1)

            client.disconnect()
            if client.kicked:
                break
            print(f"disconnected, reconnecting in {RECONNECT_SECONDS}s", flush=True)
            time.sleep(RECONNECT_SECONDS)
    except KeyboardInterrupt:
        pass

    player.stop()
    client.disconnect()


if __name__ == "__main__":
    main()
