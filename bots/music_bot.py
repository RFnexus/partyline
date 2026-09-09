#!/usr/bin/env python3
"""
    
    An example music bot for Partyline
    Needs ffmpeg

    python3 bots/music_bot.py SERVER_HASH --folder ~/music --room Concert
    or pass --stream (link to .m3u or live mp3)
    !play [N]  !pause  !resume  !skip  !stop  !list  !np  !loop  !stream URL  !help
    options: --ops-only to allow only operators to interact with the bot 
"""
import argparse
import glob
import os
import queue
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

STREAM_BUFFER_SECONDS = 6.0
STREAM_PREBUFFER_SECONDS = 2.0
STREAM_RETRY_SECONDS = 5
STREAM_RETRY_MAX_SECONDS = 60
STREAM_OPEN_ATTEMPTS = 6
STREAM_SCHEMES = ("http://", "https://")


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def find_tracks(folder):
    if not folder:
        return []
    found = []
    for pattern in ("*.mp3", "*.MP3", "*.wav", "*.WAV"):
        found.extend(glob.glob(os.path.join(folder, pattern)))
    return sorted(set(found))


def decode_wav(path, samplerate):
    from partyline.sounds import load_wav

    samples, rate = load_wav(path)
    if rate != samplerate:
        positions = np.arange(0, len(samples), rate / samplerate)
        samples = np.interp(positions, np.arange(len(samples)), samples)
    return samples.astype("float32")


def decode_mp3(path, samplerate):
    command = ["ffmpeg", "-v", "quiet", "-i", path, "-ac", "1", "-ar", str(samplerate), "-f", "f32le", "-"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("ffmpeg could not decode it")
    return np.frombuffer(result.stdout, dtype="<f4").astype("float32")


def decode_track(path, samplerate):
    if path.lower().endswith(".wav"):
        return decode_wav(path, samplerate)
    if not have_ffmpeg():
        raise RuntimeError("needs ffmpeg")
    return decode_mp3(path, samplerate)


def stream_command(url, samplerate):
    command = ["ffmpeg", "-v", "quiet", "-nostdin"]
    if url.startswith(STREAM_SCHEMES):
        command += ["-protocol_whitelist", "http,https,tcp,tls,crypto,data"]
        command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        command += ["-rw_timeout", "15000000"]
    command += ["-i", url, "-vn", "-ac", "1", "-ar", str(samplerate), "-f", "f32le", "-"]
    return command


class Stream:
    def __init__(self, url, samplerate, frame_samples):
        self.url = url
        self.frame_samples = frame_samples
        self.frames = queue.Queue(maxsize=max(2, int(STREAM_BUFFER_SECONDS * samplerate / frame_samples)))
        self.prebuffer = max(1, int(STREAM_PREBUFFER_SECONDS * samplerate / frame_samples))
        self.process = subprocess.Popen(
            stream_command(url, samplerate), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self.received = 0
        self.started = False
        self.announced = False
        self.ended = False
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        chunk_bytes = self.frame_samples * 4
        try:
            while not self.closed:
                data = self.process.stdout.read(chunk_bytes)
                if len(data) < chunk_bytes:
                    break
                frame = np.frombuffer(data, dtype="<f4").astype("float32")
                while not self.closed:
                    try:
                        self.frames.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        pass
                self.received += 1
        except Exception:
            pass
        self.ended = True

    def ready(self):
        return self.frames.qsize() >= self.prebuffer or self.ended

    def empty(self):
        return self.frames.empty()

    def next_frame(self):
        try:
            return self.frames.get_nowait()
        except queue.Empty:
            return None

    def drain(self):
        while self.next_frame() is not None:
            pass

    def close(self):
        self.closed = True
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=2)
        except Exception:
            pass


class MusicPlayer:
    def __init__(self, client, folder, gain=1.0, samplerate=SAMPLE_RATE, always_transmit=False):
        self.client = client
        self.folder = folder
        self.gain = gain
        self.samplerate = samplerate
        self.always_transmit = always_transmit
        self.tracks = find_tracks(folder)

        self.index = 0
        self.samples = np.zeros(0, dtype="float32")
        self.position = 0
        self.playing = False
        self.loop = False

        self.stream = None
        self.stream_url = None
        self.stream_retry_at = 0.0
        self.stream_backoff = STREAM_RETRY_SECONDS
        self.stream_down = False
        self.stream_had_audio = False
        self.stream_failures = 0

        self.codec = None
        self.frame_samples = None
        self.running = False
        self.lock = threading.Lock()

    def bind(self):
        if self.client.audio_profile is None:
            return
        self.codec = make_codec(self.client.audio_profile)
        self.codec.source = self
        frame_samples = max(1, int(self.samplerate * self.client.frame_ms / 1000))
        with self.lock:
            self.frame_samples = frame_samples
            if self.stream is not None and self.stream.frame_samples != frame_samples:
                self.stream.close()
                self.stream = None
                self.stream_retry_at = 0.0
        if not self.running:
            self.running = True
            threading.Thread(target=self._job, daemon=True).start()

    def start_stream(self, url):
        if not url.startswith(STREAM_SCHEMES):
            return "give me an http:// or https:// stream URL"
        if not have_ffmpeg():
            return "streaming needs ffmpeg installed where the bot runs"
        with self.lock:
            if self.frame_samples is None:
                return "not in a room yet"
            self._close_stream()
            self.samples = np.zeros(0, dtype="float32")
            self.position = 0
            self.stream_url = url
            self.stream_retry_at = 0.0
            self.stream_backoff = STREAM_RETRY_SECONDS
            self.stream_down = False
            self.stream_had_audio = False
            self.stream_failures = 0
            self.playing = True
        return None

    def _close_stream(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self.stream_url = None

    def play(self, index):
        with self.lock:
            if self.stream_url is not None and index is None:
                if self.playing:
                    return "already streaming"
                self.playing = True
                return "resumed"
            if not self.tracks:
                return "no tracks in the folder"
            if self.stream_url is not None:
                self._close_stream()
                self.samples = np.zeros(0, dtype="float32")
                self.position = 0
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
            if self.stream_url is not None:
                return "streaming, say stop first"
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
            self._close_stream()
            self.samples = np.zeros(0, dtype="float32")
            self.position = 0
            self.index = 0

    def toggle_loop(self):
        self.loop = not self.loop
        return self.loop

    def now_playing(self):
        if self.stream_url is not None:
            state = "streaming" if self.playing else "stream paused"
            if self.stream is None or (self.stream_down and not self.stream.announced):
                state += " (reconnecting)"
            return f"{state}: {self.stream_url}"
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
            samples = decode_track(path, self.samplerate) * self.gain
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

    def audience(self):
        client = self.client
        return any(
            user.room == client.my_room and user.sid != client.my_sid and not user.deaf
            for user in list(client.users.values())
        )

    def _stream_frame(self):
        with self.lock:
            url = self.stream_url
            if url is None:
                return None
            now = time.monotonic()
            stream = self.stream
            if stream is None:
                if now < self.stream_retry_at:
                    return None
                try:
                    stream = self.stream = Stream(url, self.samplerate, self.frame_samples)
                except Exception as error:
                    self._close_stream()
                    self.playing = False
                    self.client.send_text(f"could not start ffmpeg: {error}")
                    return None
            if stream.ended and stream.empty():
                stream.close()
                self.stream = None
                if stream.received:
                    self.stream_had_audio = True
                self.stream_failures += 1
                if not self.stream_had_audio and self.stream_failures >= STREAM_OPEN_ATTEMPTS:
                    self._close_stream()
                    self.playing = False
                    self.client.send_text(f"giving up on stream: {url}")
                    return None
                self.stream_retry_at = now + self.stream_backoff
                self.stream_backoff = min(STREAM_RETRY_MAX_SECONDS, self.stream_backoff * 2)
                if not self.stream_down:
                    self.stream_down = True
                    if self.stream_had_audio:
                        self.client.send_text("stream dropped, reconnecting")
                    else:
                        self.client.send_text(f"could not open stream, retrying: {url}")
                return None
            if not stream.started:
                if not stream.ready():
                    return None
                stream.started = True
                if not stream.announced:
                    stream.announced = True
                    self.stream_had_audio = True
                    self.stream_backoff = STREAM_RETRY_SECONDS
                    self.stream_failures = 0
                    if self.stream_down:
                        self.stream_down = False
                        self.client.send_text("stream is back")
                    else:
                        self.client.send_text(f"streaming: {url}")
            frame = stream.next_frame()
            if frame is None:
                stream.started = False
                return None
            frame = frame * self.gain
            peak = float(np.max(np.abs(frame))) if frame.size else 0.0
            if peak > HEADROOM:
                frame = frame * (HEADROOM / peak)
            return frame.astype("float32").reshape(-1, 1)

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

            ready = self.codec is not None and self.frame_samples is not None and packetizer is not None
            if not ready or not self.playing:
                if packetizer is not None and not packetizer.squelched:
                    packetizer.squelch()
                if self.stream is not None:
                    self.stream.drain()
                time.sleep(0.05)
                next_frame_at = time.monotonic()
                continue

            if self.stream_url is not None:
                frame = self._stream_frame()
                if frame is None:
                    time.sleep(0.05)
                    next_frame_at = time.monotonic()
                    continue
            else:
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
                if not self.always_transmit and not self.audience():
                    if not packetizer.squelched:
                        packetizer.squelch()
                else:
                    if packetizer.squelched:
                        packetizer.unsquelch()
                    packetizer.handle_frame(self.codec.encode(frame), self)

            frame_seconds = self.frame_samples / self.samplerate
            next_frame_at += frame_seconds
            now = time.monotonic()
            if next_frame_at < now - 0.5:
                next_frame_at = now
            time.sleep(max(0.0, next_frame_at - now))


def handle_command(client, player, prefix, text, sender=None, ops_only=False):
    if not text.startswith(prefix):
        return
    body = text[len(prefix) :].strip()
    if not body:
        return
    if ops_only and not getattr(sender, "operator", False):
        client.send_text("operators only")
        return
    name, _, rest = body.partition(" ")
    name = name.lower()
    rest = rest.strip()

    if name == "help":
        client.send_text(
            f"commands: {prefix}play [N], {prefix}pause, {prefix}resume, {prefix}skip, "
            f"{prefix}stop, {prefix}list, {prefix}np, {prefix}loop, {prefix}stream URL"
        )
    elif name in ("stream", "radio"):
        if not rest:
            client.send_text(player.now_playing() if player.stream_url else f"usage: {prefix}stream URL")
            return
        message = player.start_stream(rest.split()[0])
        if message:
            client.send_text(message)
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
    parser.add_argument("--folder", default=None, help="folder of MP3 files to play")
    parser.add_argument("--stream", default=None, metavar="URL", help="stream this URL as soon as the bot joins")
    parser.add_argument("--room", default=None, help="room to join")
    parser.add_argument("--name", default="MusicBot", help="display name")
    parser.add_argument("--identity", default=os.path.join(CONFIG_DIR, "bot_identity"), help="identity file")
    parser.add_argument("--password", default=None, help="password for --room")
    parser.add_argument("--server-password", default=None, help="server password if the server asks for one")
    parser.add_argument("--configdir", default=None, help="Reticulum config directory (default ~/.reticulum)")
    parser.add_argument("--prefix", default="!", help="chat command prefix")
    parser.add_argument("--gain", type=float, default=1.0, help="volume multiplier applied before encoding")
    parser.add_argument("--autoplay", action="store_true", help="start playing as soon as the bot joins")
    parser.add_argument("--ops-only", action="store_true", help="take commands from server operators only")
    parser.add_argument(
        "--always-transmit", action="store_true", help="keep sending audio when nobody in the room can hear it"
    )
    parser.add_argument(
        "--frames-per-packet", type=int, default=3, help="codec frames per packet, more saves bandwidth (default 3)"
    )
    args = parser.parse_args()

    

    try:
        server_hash = parse_hash(args.server)
    except ValueError as error:
        parser.error(str(error))

    if not args.folder and not args.stream:
        parser.error("--folder or --stream is required")
    if args.stream and not args.stream.startswith(STREAM_SCHEMES):
        parser.error("--stream needs an http:// or https:// URL")
    folder = os.path.expanduser(args.folder) if args.folder else None
    if folder and not os.path.isdir(folder):
        raise SystemExit(f"no such folder: {folder}")
    if not have_ffmpeg():
        if args.stream:
            raise SystemExit("--stream needs ffmpeg on PATH")
        print("warning: ffmpeg was not found on PATH: only WAV files will play and streaming is unavailable", flush=True)

    RNS.Reticulum(configdir=args.configdir)
    identity = load_identity(args.identity)
    print(f"Bot identity hash: {identity.hash.hex()}", flush=True)

    client = Client(
        Config(listen=True, null_audio=True, frames_per_packet=max(1, args.frames_per_packet)), identity, args.name
    )
    player = MusicPlayer(client, folder, gain=args.gain, always_transmit=args.always_transmit)
    if folder:
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
                            if args.stream and client.can_speak_here:
                                message = player.start_stream(args.stream)
                                if message:
                                    print(f"stream: {message}", flush=True)
                            elif args.autoplay and client.can_speak_here:
                                player.play(None)
                    elif kind == "text":
                        sender = event[1]
                        if getattr(sender, "sid", None) != client.my_sid:
                            handle_command(client, player, args.prefix, event[2], sender, args.ops_only)
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
