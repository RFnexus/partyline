#!/usr/bin/env python3
import argparse
import collections
import math
import queue
import random
import sys
import threading
import time

import numpy as np
import RNS
from RNS.vendor import umsgpack as msgpack
from LXST import APP_NAME
from LXST.Network import Packetizer
from LXST.Pipeline import Pipeline
from LXST.Sinks import LocalSink, LineSink
from LXST.Sources import LocalSource, LineSource

from .common import *
from .audio import *
from .prefs import IDENTITY_FILE

MODES = ("ptt", "vox", "open")

WELCOME_TIMEOUT = 10.0  # seconds after link establishment to hear back from the server

PATH_WAIT = 20.0  # path request wait
PATH_WAIT_MAX = 120.0

SAMPLE_RATE = 48000


class CountingPacketizer(Packetizer):
    def __init__(self, link, profile_name):
        super().__init__(link)
        self.header = codec_byte(profile_name)
        self.squelched = True
        self.sequence = 0
        self.packets = 0
        self.bytes = 0
        self.payload_bytes = 0

    def squelch(self):
        if not self.squelched and self.destination.status == RNS.Link.ACTIVE:
            # tell the room the spurt is over so we don't conceal a pause from members
            RNS.Packet(self.destination, msgpack.packb({FIELD_TALK_END: True}), create_receipt=False).send()
        self.squelched = True

    def unsquelch(self):
        self.squelched = False

    def start(self):
        if hasattr(Packetizer, "start"):
            super().start()

    def stop(self):
        if hasattr(Packetizer, "stop"):
            super().stop()

    def handle_frame(self, frame, source=None):
        if self.squelched or self.destination.status != RNS.Link.ACTIVE:
            return
        self.sequence = (self.sequence + 1) & 0xFFFF
        data = msgpack.packb({FIELD_FRAMES: self.header + frame, FIELD_SEQ: self.sequence})
        packet = RNS.Packet(self.destination, data, create_receipt=False)
        if packet.send() is not False:
            self.packets += 1
            self.bytes += len(packet.raw)
            self.payload_bytes += len(frame)


### TESTING ###
class PacedTone(LocalSource):
    def __init__(self, frequency, target_frame_ms, gain=0.3):
        self.frequency = frequency
        self.target_frame_ms = target_frame_ms
        self.gain = gain
        self.samplerate = SAMPLE_RATE
        self.channels = 1
        self.bitdepth = 32
        self.codec = None
        self.sink = None
        self.pipeline = None
        self.should_run = False
        self.phase = 0.0

    def start(self):
        self.should_run = True
        threading.Thread(target=self._job, daemon=True).start()

    def stop(self):
        self.should_run = False

    def _job(self):
        samples_per_frame = int(self.samplerate * self.target_frame_ms / 1000)
        frame_seconds = samples_per_frame / self.samplerate
        sample_index = np.arange(samples_per_frame)
        next_frame_at = time.monotonic()
        while self.should_run:
            angle = self.phase + 2 * np.pi * self.frequency * sample_index / self.samplerate
            frame = (self.gain * np.sin(angle)).astype("float32").reshape(-1, 1)
            self.phase = (self.phase + 2 * np.pi * self.frequency * samples_per_frame / self.samplerate) % (2 * np.pi)
            if self.codec and self.sink and self.sink.can_receive(from_source=self):
                self.sink.handle_frame(self.codec.encode(frame), self)
            next_frame_at += frame_seconds
            time.sleep(max(0.0, next_frame_at - time.monotonic()))


class WavSource(LocalSource):
    def __init__(self, path, target_frame_ms, loop=False, gain=1.0):
        from .sounds import load_wav

        samples, samplerate = load_wav(path)
        if samplerate != SAMPLE_RATE:
            positions = np.arange(0, len(samples), samplerate / SAMPLE_RATE)
            samples = np.interp(positions, np.arange(len(samples)), samples)
        self.samples = (samples * gain).astype("float32")
        self.loop = loop
        self.target_frame_ms = target_frame_ms
        self.samplerate = SAMPLE_RATE
        self.channels = 1
        self.bitdepth = 32
        self.codec = None
        self.sink = None
        self.pipeline = None
        self.should_run = False
        self.position = 0
        self.finished = False

    def start(self):
        self.should_run = True
        threading.Thread(target=self._job, daemon=True).start()

    def stop(self):
        self.should_run = False

    def _job(self):
        samples_per_frame = int(self.samplerate * self.target_frame_ms / 1000)
        frame_seconds = samples_per_frame / self.samplerate
        next_frame_at = time.monotonic()
        while self.should_run:
            end = self.position + samples_per_frame
            if end > len(self.samples):
                if not self.loop:
                    self.finished = True
                    return
                self.position = 0
                end = samples_per_frame
            frame = self.samples[self.position : end].reshape(-1, 1)
            self.position = end
            if self.codec and self.sink and self.sink.can_receive(from_source=self):
                self.sink.handle_frame(self.codec.encode(frame), self)
            next_frame_at += frame_seconds
            time.sleep(max(0.0, next_frame_at - time.monotonic()))


class AnalyzingSink(LocalSink):
    MAX_QUEUED = 3

    def __init__(self, frame_ms, samplerate=SAMPLE_RATE):
        self.samplerate = samplerate
        self.channels = None
        self.frame_seconds = frame_ms / 1000
        self.queued = []
        self.recent = []
        self.frames = 0
        self.lock = threading.Lock()
        self.run = True
        threading.Thread(target=self._drain, daemon=True).start()

    def can_receive(self, from_source=None):
        with self.lock:
            return len(self.queued) < self.MAX_QUEUED

    def handle_frame(self, frame, source=None):
        with self.lock:
            self.queued.append(frame)

    def _drain(self):
        next_frame_at = time.monotonic()
        while self.run:
            with self.lock:
                frame = self.queued.pop(0) if self.queued else None
            if frame is None:
                time.sleep(self.frame_seconds / 4)
                next_frame_at = time.monotonic()
                continue
            self.frames += 1
            with self.lock:
                self.recent.append(frame[:, 0].astype("float32"))
                self.recent = self.recent[-200:]
            next_frame_at += frame.shape[0] / self.samplerate
            time.sleep(max(0.0, next_frame_at - time.monotonic()))

    def peaks(self):
        if not self.recent:
            return []
        signal = np.concatenate(self.recent)[-self.samplerate :]
        spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
        frequencies = np.fft.rfftfreq(len(signal), 1 / self.samplerate)
        top = []
        for index in np.argsort(spectrum)[::-1]:
            if spectrum[index] < 0.1 * spectrum.max() or len(top) >= 4:
                break
            if all(abs(frequencies[index] - found) > 30 for found in top):
                top.append(float(frequencies[index]))
        return sorted(round(frequency) for frequency in top)


class Channel:
    def __init__(self, room_id, name, profile, frame_ms, access, description, dialin_number=None):
        self.id = room_id
        self.name = name
        self.profile = profile
        self.frame_ms = frame_ms
        self.access = access
        self.description = description
        self.dialin_number = dialin_number  # rnphone / lxst clients can call this to land in the room

    @property
    def locked(self):
        return self.access != 0

    def requirements(self):
        parts = []
        if self.access & ACCESS_ALLOWLIST:
            parts.append("allow list")
        elif self.access & ACCESS_IDENTITY:
            parts.append("identified users")
        if self.access & ACCESS_PASSWORD:
            parts.append("password")
        return ", ".join(parts) or "open"


class User:
    def __init__(
        self,
        member_id,
        name,
        identity,
        room,
        muted,
        deaf,
        hops=None,
        rtt=None,
        operator=False,
        server_muted=False,
        text_only=False,
        speaker=True,
    ):
        self.sid = member_id
        self.name = name
        self.identity = identity
        self.room = room
        self.muted = muted
        self.deaf = deaf
        self.hops = hops
        self.rtt = rtt
        self.operator = operator
        self.server_muted = server_muted
        self.text_only = text_only
        self.speaker = speaker

    def path_info(self):
        parts = []
        if self.hops is not None:
            plural = "s" if self.hops != 1 else ""
            parts.append(f"{self.hops} hop{plural} to server")
        if self.rtt is not None:
            parts.append(f"RTT {self.rtt} ms")
        return ", ".join(parts) or "path unknown"


class Config:

    def __init__(self, **overrides):
        self.mode = "ptt"
        self.vad_db = -45.0
        self.vad_hang = 0.4
        self.jitter_ms = 200
        self.input = None
        self.output = None
        self.low_latency = False
        self.tone = None
        self.wav = None  # play this file instead of the microphone
        self.wav_loop = False
        self.null_audio = False
        self.listen = False
        self.text_only = False  # chat only mode
        self.force_profile = None  # Testing only do not use this for any client/server
        self.force_frame_ms = None
        self.rx_jitter_ms = 0  # Testing only
        self.rx_loss = 0.0  # testing only
        self.__dict__.update(overrides)


class Client:
    REORDER_HOLD = 0.06  # seconds to hold a frame that arrived ahead of a missing one

    def __init__(self, config, identity, name):
        self.cfg = config
        self.identity = identity
        self.name = name

        self.link = None
        self.state = "idle"
        self.error = None
        self.kicked = False
        self.server_hash = None
        self.server_name = None
        self.server_version = None
        self.motd = ""
        self.my_sid = None
        self.my_room = None
        self.can_speak_here = True
        self.synced = False

        self.channels = {}
        self.users = {}
        self.last_heard = {}
        self.speakers = {}
        self.lock = threading.Lock()
        self.events = collections.deque()

        self.packetizer = None
        self.gate = None
        self.playout = None
        self.out_sink = None
        self.tx_pipe = None
        self.audio_profile = None
        self.frame_ms = None
        self.expected_samples = None
        self._burst = []
        self._burst_lock = threading.Lock()
        # RNS starts a new thread for every packet it delivers. Decoding on those directly corrupts decoder
        # state and shuffles frames, so all packet handling goes through one worker, in arrival order.
        self.inbox = queue.Queue()
        self._reorder = {}
        threading.Thread(target=self._rx_worker, daemon=True).start()

        self.ptt_down = False
        self.muted = False
        self.deaf = False
        self.gains = {}
        self.local_muted = set()
        self.hops = None

        self.started_at = None
        self.rx_packets = 0
        self.rx_bytes = 0
        self.bad_frames = 0
        self.dropped = 0
        self.tx_packets = 0  # folded in from each packetizer when audio stops
        self.tx_bytes = 0
        self.tx_payload = 0
        self.heard = set()
        self._last_profile = None
        self._last_sink = None
        self._last_playout = None

        self._stat_time = time.time()
        self._stat_tx = 0
        self._stat_rx = 0
        self._want_room = None
        self._want_password = None
        self._server_password = None

    def event(self, *event):
        self.events.append(event)

    @property
    def me(self):
        return self.users.get(self.my_sid)

    @property
    def is_operator(self):
        me = self.me
        return bool(me and me.operator)

    ### CONNECTION ###
    def connect(self, destination_hash, room=None, password=None, server_password=None, timeout=20):
        self.state = "connecting"
        self.error = None
        self.server_hash = destination_hash
        self._want_room = room
        self._want_password = password
        self._server_password = server_password
        if self.cfg.text_only:
            self.muted = True
            self.deaf = True
        threading.Thread(target=self._connect, args=(destination_hash, timeout), daemon=True).start()

    def path_wait_seconds(self):
        try:
            medium = RNS.Reticulum.get_instance().get_medium_path_timeout() or 0
        except Exception:
            medium = 0
        return min(PATH_WAIT_MAX, max(PATH_WAIT, 6 * medium))

    def _connect(self, destination_hash, timeout):
        if not RNS.Transport.has_path(destination_hash):
            RNS.Transport.request_path(destination_hash)
            wait = max(timeout, self.path_wait_seconds())
            started = time.time()
            reminded = False
            while not RNS.Transport.has_path(destination_hash) and time.time() - started < wait:
                time.sleep(0.2)
                if not reminded and time.time() - started > wait / 3:
                    reminded = True
                    self.event("info", "still looking for a path to the server")
        identity = RNS.Identity.recall(destination_hash)
        if identity is None:
            self.fail("no path to server (is it running and announced?)")
            return
        try:
            self.hops = RNS.Transport.hops_to(destination_hash)
        except Exception:
            self.hops = None
        destination = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        self.link = RNS.Link(destination, established_callback=self.established, closed_callback=self.closed)
        # set before establishment so the server's first packet is not missed
        self.link.set_packet_callback(self.packet)

    def established(self, link):
        link.identify(self.identity)
        self.state = "waiting for server"
        rtt = None
        if getattr(link, "rtt", None):
            rtt = int(link.rtt * 1000)
        try:
            first_hop = RNS.Reticulum.get_instance().get_first_hop_timeout(self.server_hash) or 0
        except Exception:
            first_hop = 0
        welcome_wait = max(WELCOME_TIMEOUT, 15 * (link.rtt or 0), 4 * first_hop)

        hello = {
            "name": self.name,
            "room": self._want_room,
            "password": self._want_password,
            "server_password": self._server_password,
            "text_only": self.cfg.text_only,
            "muted": self.muted,
            "deaf": self.deaf,
            "hops": self.hops,
            "rtt": rtt,
            "ver": PROTOCOL_VERSION,
            "app": APP_VERSION,
        }

        self.send({FIELD_HELLO: hello})
        threading.Timer(welcome_wait, self.welcome_timeout).start()

    def welcome_timeout(self):
        if self.state == "waiting for server":
            self.fail("server did not answer (not a room server, or an older version)")
            self.disconnect()

    def fail(self, message):
        self.error = message
        self.state = "idle"
        self.event("error", message)

    def closed(self, link):
        if self.state == "closed":
            return
        self.state = "closed"
        self.stop_audio()
        self.event("closed", self.error or "link closed by server")
        RNS.log("Server link closed", RNS.LOG_NOTICE)

    def disconnect(self):
        self.stop_audio()
        if self.link and self.link.status == RNS.Link.ACTIVE:
            self.link.teardown()
        self.state = "idle"
        self.link = None

    @property
    def connected(self):
        return self.state == "connected"

    def packet(self, data, packet):
        self.inbox.put((data, packet))

    def _rx_worker(self):
        while True:
            try:
                item = self.inbox.get(timeout=0.02)
            except queue.Empty:
                item = None
            if item is not None:
                try:
                    self._handle_packet(*item)
                except Exception as error:
                    RNS.log(f"Receive error: {error}", RNS.LOG_ERROR)
            self._release_pending()

    def _audio(self, member_id, frame, sequence):
        if sequence is None:
            self.handle_frame(member_id, frame, None)
            return
        reorder = self._reorder.setdefault(member_id, {"next": None, "pending": {}})
        expected = reorder["next"]
        is_late = expected is not None and ((sequence - expected) & 0xFFFF) > 0x8000
        if expected is None or sequence == expected or is_late:
            # in order, or late (the playout fills its slot)
            self.handle_frame(member_id, frame, sequence)
            if expected is None or sequence == expected:
                reorder["next"] = (sequence + 1) & 0xFFFF
            self._drain_pending(member_id, reorder)
        else:
            reorder["pending"][sequence] = (frame, time.time())
            if len(reorder["pending"]) > 8:
                self._release_pending(force=True)

    def _drain_pending(self, member_id, reorder):
        while reorder["next"] in reorder["pending"]:
            frame, _ = reorder["pending"].pop(reorder["next"])
            self.handle_frame(member_id, frame, reorder["next"])
            reorder["next"] = (reorder["next"] + 1) & 0xFFFF

    def _release_pending(self, force=False):
        now = time.time()
        for member_id, reorder in list(self._reorder.items()):
            while reorder["pending"]:
                oldest = min(reorder["pending"], key=lambda held: (held - reorder["next"]) & 0xFFFF)
                frame, arrived_at = reorder["pending"][oldest]
                if not force and now - arrived_at < self.REORDER_HOLD:
                    break
                reorder["pending"].pop(oldest)
                self.handle_frame(member_id, frame, oldest)
                reorder["next"] = (oldest + 1) & 0xFFFF
                self._drain_pending(member_id, reorder)

    def _handle_packet(self, data, packet):
        fields = msgpack.unpackb(data)
        if type(fields) is not dict:
            return
        if FIELD_WELCOME in fields:
            self.welcome(fields[FIELD_WELCOME])
        if FIELD_CHANNEL in fields:
            self.channel(fields[FIELD_CHANNEL])
        if FIELD_USER in fields:
            self.user(fields[FIELD_USER])
        if FIELD_USER_LEFT in fields:
            self.user_left(fields[FIELD_USER_LEFT])
        if FIELD_SYNCED in fields:
            self.synced = True
            self.event("synced")
        if FIELD_ROOM in fields:
            self.room(fields[FIELD_ROOM])

        if FIELD_DENIED in fields:
            room_id, reason = fields[FIELD_DENIED]
            reason = clean_text(reason, MAX_TEXT)
            if room_id is None:
                # refused outright (version, ban, allow list, server password, kick): the server is closing
                # the link and trying again will not help, so front ends should not reconnect
                self.kicked = True
                self.error = reason
            self.event("denied", room_id, reason)
        if FIELD_NOTICE in fields:
            self.event("notice", clean_text(fields[FIELD_NOTICE], MAX_TEXT))
        if FIELD_TEXT in fields:
            member_id, text = fields[FIELD_TEXT]
            self.event("text", self.users.get(member_id), clean_text(text, MAX_TEXT))
        if FIELD_POKE in fields:
            member_id, text = fields[FIELD_POKE]
            self.event("poke", self.users.get(member_id), clean_text(text, MAX_TEXT))
        if FIELD_TALK_END in fields and self.playout:
            member_id = fields[FIELD_TALK_END]
            if isinstance(member_id, int):
                self.playout.end_spurt(member_id)

        if FIELD_FRAMES in fields and self.playout:
            self.rx_packets += 1
            self.rx_bytes += len(packet.raw) if packet.raw else len(data)
            member_id = int(fields.get(FIELD_SPEAKER, 0))
            frame = fields[FIELD_FRAMES]
            sequence = fields.get(FIELD_SEQ)
            if not isinstance(sequence, int) or not 0 <= sequence <= 0xFFFF:
                sequence = None
            if self.cfg.rx_loss and random.random() < self.cfg.rx_loss:
                return
            if self.cfg.rx_jitter_ms:
                with self._burst_lock:
                    self._burst.append((member_id, frame, sequence))
            else:
                self._audio(member_id, frame, sequence)

    def welcome(self, welcome):
        server_protocol = welcome.get("ver")
        if server_protocol != PROTOCOL_VERSION:
            self.kicked = True
            if isinstance(server_protocol, int) and server_protocol < PROTOCOL_VERSION:
                advice = "the server needs updating"
            else:
                advice = "please update"
            self.fail(
                f"this server speaks Partyline protocol {server_protocol}, this client speaks {PROTOCOL_VERSION}: {advice}"
            )
            self.disconnect()
            return

        self.server_name = clean_name(welcome.get("name"), "server")
        self.my_sid = int(welcome.get("sid"))
        self.motd = clean_text(welcome.get("motd", ""), MAX_TEXT)
        self.server_version = clean_text(str(welcome.get("app", "")), 32) or "unknown"
        self.state = "connected"
        self.started_at = time.time()
        self.event("connected")
        if self.server_version != APP_VERSION:
            self.event("version", self.server_version)

    def channel(self, record):
        room_id, name, profile, frame_ms_value, access, description = record[:6]
        dialin_number = record[6] if len(record) > 6 else None
        if profile not in PROFILES:
            self.event("error", f"room {name!r} uses unknown profile {profile!r}")
            return
        if not (isinstance(dialin_number, str) and len(dialin_number) == 32):
            dialin_number = None
        channel = Channel(
            int(room_id),
            clean_name(name, f"Room {room_id}"),
            profile,
            int(frame_ms_value),
            int(access),
            clean_text(description, MAX_DESCRIPTION),
            dialin_number,
        )
        self.channels[channel.id] = channel
        self.event("channel", channel)

    def user(self, record):
        member_id, name, identity, room, muted, deaf = record[:6]
        extras = list(record[6:]) + [None] * 6
        hops, rtt, operator, server_muted, text_only, speaker = extras[:6]
        if not (isinstance(identity, str) and len(identity) == 32):
            identity = None
        if not isinstance(room, int):
            room = None
        if not isinstance(hops, int):
            hops = None
        if isinstance(rtt, (int, float)):
            rtt = int(rtt)
        else:
            rtt = None
        previous = self.users.get(member_id)
        user = User(
            int(member_id),
            clean_name(name, f"guest-{member_id}"),
            identity,
            room,
            bool(muted),
            bool(deaf),
            hops,
            rtt,
            bool(operator),
            bool(server_muted),
            bool(text_only),
            True if speaker is None else bool(speaker),
        )
        self.users[user.sid] = user
        if previous is None:
            self.event("user_joined", user)
        elif previous.room != user.room:
            self.event("user_moved", user, previous.room)
            if previous.room == self.my_room and user.sid != self.my_sid:
                self.drop_speaker(user.sid)
        else:
            self.event("user_state", user, previous)

    def user_left(self, member_id):
        user = self.users.pop(member_id, None)
        if user:
            self.drop_speaker(member_id)
            self.event("user_left", user)

    def room(self, record):
        room_id, profile, frame_ms_value = record[:3]
        self.can_speak_here = bool(record[3]) if len(record) > 3 else True
        if isinstance(room_id, int):
            self.my_room = room_id
        else:
            self.my_room = None
        if self.my_room is None:
            self.stop_audio()
            self.event("room", None)
            return
        if profile not in PROFILES:
            self.fail(f"room uses unknown profile {profile!r}")
            return
        self.configure(profile, int(frame_ms_value))
        self.apply_mode()
        self.event("room", self.my_room)

    def handle_frame(self, member_id, frame, sequence=None):
        if type(frame) is not bytes or len(frame) < 2:
            self.bad_frames += 1
            return
        codec_class = codec_type(frame[0])
        playout = self.playout
        if codec_class is None or playout is None:
            self.bad_frames += 1
            return

        with self.lock:
            speaker = self.speakers.get(member_id)
            if speaker is None:
                speaker = Speaker(playout, codec_class)
                self.speakers[member_id] = speaker
                RNS.log(f"Hearing member {member_id}", RNS.LOG_DEBUG)
            elif type(speaker.codec) is not codec_class:
                speaker.set_codec(codec_class)

        try:
            samples = speaker.codec.decode(frame[1:])
        except Exception:
            self.bad_frames += 1  # decoder torn down under us, or a corrupt frame
            return

        sample_count = samples.shape[0]
        wanted = self.expected_samples
        if abs(sample_count - wanted) > wanted // 50:
            self.bad_frames += 1  # wrong frame length for this room
            return
        if sample_count < wanted:
            # Codec2 resampling comes back a few samples short
            padding = np.zeros((wanted - sample_count, samples.shape[1]), samples.dtype)
            samples = np.vstack([samples, padding])
        elif sample_count > wanted:
            samples = samples[:wanted]

        self.last_heard[member_id] = time.time()
        self.heard.add(member_id)
        if self.deaf or member_id in self.local_muted:
            return  # still shows who is talking, plays nothing

        gain = self.gains.get(member_id)
        if gain:
            samples = (samples * 10 ** (gain / 20)).astype("float32")
        playout.push(member_id, samples, sequence)

    def drop_speaker(self, member_id):
        with self.lock:
            self.speakers.pop(member_id, None)
        self._reorder.pop(member_id, None)
        if self.playout:
            self.playout.remove(member_id)
        self.last_heard.pop(member_id, None)

    def _burst_job(self):
        while self.playout and self.cfg.rx_jitter_ms:
            time.sleep(self.cfg.rx_jitter_ms / 1000)
            with self._burst_lock:
                pending = self._burst
                self._burst = []
            for member_id, frame, sequence in pending:
                self._audio(member_id, frame, sequence)

    ### OUTGOING QUEUE ###
    def send(self, fields):
        if self.link and self.link.status == RNS.Link.ACTIVE:
            RNS.Packet(self.link, msgpack.packb(fields), create_receipt=False).send()

    def move(self, room_id, password=None):
        if self.connected:
            self.send({FIELD_MOVE: [int(room_id), password]})

    def find_channel(self, name):
        for channel in self.channels.values():
            if channel.name.lower() == name.strip().lower():
                return channel
        return None

    def find_user(self, name):
        for user in self.users.values():
            if user.name.lower() == name.strip().lower():
                return user
        return None

    def set_gain(self, member_id, decibels):
        if decibels:
            self.gains[member_id] = float(decibels)
        else:
            self.gains.pop(member_id, None)

    def set_local_mute(self, member_id, muted):
        if muted:
            self.local_muted.add(member_id)
        else:
            self.local_muted.discard(member_id)

    def poke(self, member_id, text):
        text = clean_text(text, MAX_TEXT) or "poke"
        if self.connected:
            self.send({FIELD_POKE: [int(member_id), text]})
            self.event("poked", self.users.get(member_id), text)

    def admin(self, action, member_id, argument=None):
        if action not in ADMIN_ACTIONS:
            raise ValueError(action)
        if self.connected:
            self.send({FIELD_ADMIN: [action, int(member_id), argument]})

    def send_text(self, text):
        text = clean_text(text, MAX_TEXT)
        if text and self.connected and self.my_room is not None:
            self.send({FIELD_TEXT: text})
            self.event("text", self.me or self, text)

    def set_muted(self, muted):
        self.muted = bool(muted)
        if not self.muted:
            self.deaf = False
        self.push_state()

    def set_deaf(self, deaf):
        self.deaf = bool(deaf)
        if self.deaf:
            self.muted = True
        self.push_state()

    def push_state(self):
        self.apply_mode()
        if self.connected:
            self.send({FIELD_STATE: [self.muted, self.deaf]})
        self.event("self_state")

    ### AUDIO PIPELINE ###
    def configure(self, profile, frame_ms_value):
        if self.cfg.text_only:
            RNS.log(f"Chat only: not opening audio for {describe(profile)}", RNS.LOG_DEBUG)
            return
        if self.cfg.force_profile and profile != self.cfg.force_profile:
            RNS.log(f"Room is {profile}, ignoring it and using {self.cfg.force_profile} as forced", RNS.LOG_WARNING)
            profile = self.cfg.force_profile
            frame_ms_value = frame_ms(self.cfg.force_profile)
        if self.cfg.force_frame_ms:
            frame_ms_value = self.cfg.force_frame_ms
        if self.playout and (profile, frame_ms_value) == (self.audio_profile, self.frame_ms):
            self.clear_speakers()  # forget the old rooms voices
            return
        self.stop_audio()
        try:
            self._build_audio(profile, frame_ms_value)
        except Exception as error:
            RNS.log(f"Could not set up audio {error}", RNS.LOG_ERROR)
            self.stop_audio()
            self.event("error", f"audio setup failed: {error}")

    def _build_audio(self, profile, frame_ms_value):
        self.audio_profile = profile
        self.frame_ms = frame_ms_value
        self.expected_samples = int(SAMPLE_RATE * frame_ms_value / 1000)
        RNS.log(f"Audio configured for {describe(profile)}", RNS.LOG_NOTICE)

        # receive: members -> decoders -> Playout (jitter buffer + mix) -> soundcard or analyzer
        if self.cfg.null_audio:
            self.out_sink = AnalyzingSink(frame_ms_value)
        else:
            self.out_sink = LineSink(preferred_device=self.cfg.output, low_latency=self.cfg.low_latency)
        sink_rate = getattr(self.out_sink, "samplerate", None) or SAMPLE_RATE
        self.playout = Playout(frame_ms_value, self.jitter_frames(frame_ms_value), self.out_sink, sink_rate)
        self.playout.start()
        if self.cfg.rx_jitter_ms:
            threading.Thread(target=self._burst_job, daemon=True).start()

        # transmit: microphone or tone -> gate -> codec -> packets
        self.packetizer = CountingPacketizer(self.link, profile)
        if self.cfg.listen:
            return
        self.gate = VoiceGate(self.packetizer, self.cfg.vad_db, self.cfg.vad_hang)
        if self.cfg.tone:
            source = PacedTone(self.cfg.tone, frame_ms_value)
        elif self.cfg.wav:
            source = WavSource(self.cfg.wav, frame_ms_value, loop=self.cfg.wav_loop)
        else:
            source = LineSource(preferred_device=self.cfg.input, target_frame_ms=frame_ms_value)
        self.tx_pipe = Pipeline(source=source, codec=gated_codec(profile, self.gate), sink=self.packetizer)
        self.packetizer.start()
        self.tx_pipe.start()
        self.apply_mode()

    def jitter_frames(self, frame_ms_value):
        return max(1, math.ceil(self.cfg.jitter_ms / frame_ms_value))

    def set_jitter(self, milliseconds):
        self.cfg.jitter_ms = max(0, int(milliseconds))
        if self.playout:
            self.playout.set_floor(self.jitter_frames(self.frame_ms))

    def clear_speakers(self):
        with self.lock:
            self.speakers.clear()
            self.last_heard.clear()
        self._reorder.clear()
        if self.playout:
            self.playout.clear()

    def stop_audio(self):
        if self.tx_pipe:
            try:
                self.tx_pipe.stop()
            except Exception:
                pass

        if self.playout:
            self.playout.stop()
            self.dropped += self.playout.dropped
            self._last_playout = self.playout

        if self.packetizer:
            self.packetizer.stop()
            self.tx_packets += self.packetizer.packets
            self.tx_bytes += self.packetizer.bytes
            self.tx_payload += self.packetizer.payload_bytes

        if isinstance(self.out_sink, AnalyzingSink):
            self.out_sink.run = False
            self._last_sink = self.out_sink
        if self.audio_profile:
            self._last_profile = (self.audio_profile, self.frame_ms)

        self.clear_speakers()
        self.packetizer = None
        self.gate = None
        self.playout = None
        self.out_sink = None
        self.tx_pipe = None
        self.audio_profile = None

    def set_devices(self, input_name, output_name, low_latency=None):

        self.cfg.input = input_name or None

        self.cfg.output = output_name or None

        if low_latency is not None:
            self.cfg.low_latency = bool(low_latency)

        if self.playout:
            profile = self.audio_profile
            frame_ms_value = self.frame_ms
            self.stop_audio()
            self.configure(profile, frame_ms_value)

    def set_vad(self, threshold_db, hang_seconds):
        self.cfg.vad_db = float(threshold_db)
        self.cfg.vad_hang = float(hang_seconds)
        if self.gate:
            self.gate.threshold = self.cfg.vad_db
            self.gate.hang = self.cfg.vad_hang

    ### TX CONTROL ###
    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(mode)
        self.cfg.mode = mode
        self.apply_mode()

    def set_transmit(self, down):
        self.ptt_down = bool(down)
        self.apply_mode()

    def apply_mode(self):
        if not self.packetizer or self.cfg.listen:
            return
        mode = self.cfg.mode
        if self.gate:
            self.gate.enabled = (mode == "vox") and not self.muted
        if self.cfg.tone:
            mode = "open"  # tone has no gate to drive
        elif self.cfg.wav and mode == "ptt":
            mode = "open"  # a file has nobody to press the key; vox still gates its silences
        if self.muted or not self.can_speak_here:
            self.packetizer.squelch()
        elif mode == "open" or (mode == "ptt" and self.ptt_down):
            self.packetizer.unsquelch()
        elif mode == "ptt":
            self.packetizer.squelch()

    @property
    def transmitting(self):
        return bool(self.packetizer) and not self.packetizer.squelched

    @property
    def level(self):
        if self.gate:
            return self.gate.level
        return -120.0

    ### STATS REPORTING ###

    def tx_totals(self):
        packetizer = self.packetizer
        packets = self.tx_packets
        wire_bytes = self.tx_bytes
        payload_bytes = self.tx_payload
        if packetizer:
            packets += packetizer.packets
            wire_bytes += packetizer.bytes
            payload_bytes += packetizer.payload_bytes
        return packets, wire_bytes, payload_bytes

    def stats(self):
        now = time.time()
        elapsed = max(now - self._stat_time, 1e-3)
        tx_bytes = self.tx_totals()[1]
        result = {
            "tx_kbps": (tx_bytes - self._stat_tx) * 8 / elapsed / 1000,
            "rx_kbps": (self.rx_bytes - self._stat_rx) * 8 / elapsed / 1000,
        }
        self._stat_time = now
        self._stat_tx = tx_bytes
        self._stat_rx = self.rx_bytes
        return result

    def summary(self):
        if not self.started_at:
            return f"never connected ({self.error or self.state})"
        elapsed = time.time() - self.started_at
        packets, wire_bytes, payload_bytes = self.tx_totals()
        playout = self.playout or self._last_playout
        room = self.channels.get(self.my_room)
        if self.audio_profile:
            profile, frame_ms_value = self.audio_profile, self.frame_ms
        else:
            profile, frame_ms_value = self._last_profile or (None, None)
        room_name = room.name if room else None

        lines = ["SUMMARY"]
        lines.append(
            f"  server {self.server_name!r}, my id {self.my_sid}, in room {room_name} "
            f"({describe(profile)} at {frame_ms_value} ms), {len(self.users)} users on server"
        )

        if self.cfg.listen:
            lines.append("  TX none (listen only)")
        else:
            lines.append(
                f"  TX {packets} pkts, {wire_bytes} B on wire = {wire_bytes * 8 / elapsed / 1000:.1f} kbps "
                f"(codec payload {payload_bytes * 8 / elapsed / 1000:.1f} kbps, {wire_bytes / max(packets, 1):.0f} B/pkt)"
            )

        dropped = self.dropped
        if self.playout:
            dropped += self.playout.dropped
        if playout:
            playout_summary = (
                f"{playout.lost} lost ({playout.recovered} late but used), {playout.concealed} concealed, "
                f"{playout.frames_out} frames out in {playout.blocks_out} blocks, "
                f"buffer ended at {playout.depth_ms} ms after {playout.grew} growths"
            )
        else:
            playout_summary = "no playout"
        lines.append(
            f"  RX {self.rx_packets} pkts, {self.rx_bytes} B on wire = {self.rx_bytes * 8 / elapsed / 1000:.1f} kbps "
            f"from {len(self.heard)} members, {self.bad_frames} bad frames, {dropped} dropped, {playout_summary}"
        )

        if isinstance(self.out_sink, AnalyzingSink):
            sink = self.out_sink
        else:
            sink = self._last_sink
        if sink:
            lines.append(f"  heard {sink.frames} mixed frames, peaks {sink.peaks()} Hz")
        return "\n".join(lines)


### TUI ###
def add_common_args(parser):
    parser.add_argument("--configdir", default=None, help="Reticulum config directory (default ~/.reticulum)")
    parser.add_argument("--identity", default=IDENTITY_FILE, help="our identity file (created if missing)")
    parser.add_argument("--name", default=None, help="display name (default: guest-<identity prefix>)")
    parser.add_argument("--input", default=None, help="microphone name , default: system default")
    parser.add_argument("--output", default=None, help="speaker name , default: system default")
    parser.add_argument(
        "--mode", choices=MODES, default="ptt", help="ptt: push to talk, vox: voice gate, open: always send"
    )
    parser.add_argument("--vad-db", type=float, default=-45.0, help="voice gate threshold db")
    parser.add_argument("--vad-hang", type=float, default=0.4, help="seconds to keep sending after speech")
    parser.add_argument("--low-latency", action="store_true")
    parser.add_argument("--jitter-ms", type=int, default=200, help="receive jitter in ms more = smoother, later")


def config_from_args(args):
    return Config(
        mode=args.mode,
        vad_db=args.vad_db,
        vad_hang=args.vad_hang,
        input=args.input,
        output=args.output,
        low_latency=args.low_latency,
        jitter_ms=args.jitter_ms,
        tone=getattr(args, "tone", None),
        wav=getattr(args, "wav", None),
        wav_loop=getattr(args, "wav_loop", False),
        null_audio=getattr(args, "null_audio", False),
        listen=getattr(args, "listen", False),
        text_only=getattr(args, "text_only", False),
        force_profile=getattr(args, "force_profile", None),
        force_frame_ms=getattr(args, "force_frame_ms", None),
        rx_jitter_ms=getattr(args, "rx_jitter_ms", 0),
        rx_loss=getattr(args, "rx_loss", 0.0),
    )


### GC AND EVENT MISC ###
def tune_gc():
    import gc

    gc.collect()
    gc.freeze()
    gc.set_threshold(50000, 50, 50)


def default_name(identity):
    return f"guest-{identity.hash.hex()[:6]}"


def describe_event(client, event):
    kind = event[0]
    if kind == "connected":
        motd = f": {client.motd}" if client.motd else ""
        return f"connected to {client.server_name!r}{motd}"
    if kind == "room":
        channel = client.channels.get(event[1])
        if channel:
            return f"now in room {channel.name!r} ({describe(channel.profile)})"
        return "not in any room"
    if kind == "denied":
        channel = client.channels.get(event[1])
        where = f" for {channel.name!r}" if channel else ""
        return f"denied{where}: {event[2]}"
    if kind == "notice":
        return f"*** {event[1]}"
    if kind == "info":
        return f"... {event[1]}"
    if kind == "version":
        return f"*** the server runs Partyline {event[1]}, you run {APP_VERSION}: please update so both match"
    if kind == "user_joined":
        if event[1].sid == client.my_sid:
            return None
        return f"{event[1].name} connected"
    if kind == "user_left":
        return f"{event[1].name} disconnected"
    if kind == "user_moved":
        channel = client.channels.get(event[1].room)
        room_name = channel.name if channel else "no room"
        return f"{event[1].name} moved to {room_name}"
    if kind == "text":
        return f"<{getattr(event[1], 'name', '?')}> {event[2]}"
    if kind == "poke":
        return f"*** {getattr(event[1], 'name', '?')} poked you: {event[2]}"
    if kind == "poked":
        return f"you poked {getattr(event[1], 'name', '?')}: {event[2]}"
    if kind in ("error", "closed"):
        return f"{kind}: {event[1]}"
    return None


COMMAND_HELP = (
    "commands: /join ROOM [PASSWORD], /say TEXT, /poke USER TEXT, /who, /mute, /unmute, /deaf, /undeaf, /ptt, "
    "/kick USER, /ban USER, /smute USER, /sunmute USER, /op USER, /deop USER, /move USER ROOM, /quit"
)
ADMIN_COMMANDS = {
    "/kick": ADMIN_KICK,
    "/ban": ADMIN_BAN,
    "/smute": ADMIN_MUTE,
    "/sunmute": ADMIN_UNMUTE,
    "/op": ADMIN_OP,
    "/deop": ADMIN_DEOP,
}


def stdin_commands(client, stop):

    while True:
        line = sys.stdin.readline()
        if line == "":
            return
        line = line.strip()
        if not line:
            continue
        command, _, rest = line.partition(" ")
        if command == "/join":
            room_name, _, password = rest.partition(" ")
            channel = client.find_channel(room_name)
            if channel:
                client.move(channel.id, password or None)
            else:
                room_names = ", ".join(channel.name for channel in client.channels.values())
                print(f"no such room {room_name!r}; rooms: {room_names}", flush=True)
        elif command == "/say":
            client.send_text(rest)
        elif command == "/poke":
            user_name, _, text = rest.partition(" ")
            user = client.find_user(user_name)
            if user:
                client.poke(user.sid, text)
            else:
                print(f"no such user {user_name!r}", flush=True)
        elif command == "/who":
            for user in client.users.values():
                flags = []
                if user.operator:
                    flags.append("operator")
                if user.text_only:
                    flags.append("text only")
                flag_text = f" ({', '.join(flags)})" if flags else ""
                print(f"  {user.name}{flag_text}: {user.path_info()}", flush=True)
        elif command in ADMIN_COMMANDS:
            user = client.find_user(rest)
            if user:
                client.admin(ADMIN_COMMANDS[command], user.sid)
            else:
                print(f"no such user {rest!r}", flush=True)
        elif command == "/move":
            user_name, _, room_name = rest.partition(" ")
            user = client.find_user(user_name)
            channel = client.find_channel(room_name)
            if user and channel:
                client.admin(ADMIN_MOVE, user.sid, channel.id)
            else:
                print("usage: /move USER ROOM", flush=True)
        elif command == "/mute":
            client.set_muted(True)
        elif command == "/unmute":
            client.set_muted(False)
        elif command == "/deaf":
            client.set_deaf(True)
        elif command == "/undeaf":
            client.set_deaf(False)
        elif command == "/ptt":
            client.set_transmit(not client.ptt_down)
            print("TX ON" if client.ptt_down else "TX off", flush=True)
        elif command == "/quit":
            stop.set()
            return
        elif command.startswith("/"):
            print(COMMAND_HELP, flush=True)
        else:
            client.send_text(line)


def main():
    parser = argparse.ArgumentParser(description="Partyline voice room client")
    parser.add_argument("--version", action="version", version=f"Partyline {APP_VERSION} (protocol {PROTOCOL_VERSION})")
    parser.add_argument("server", nargs="?", help="destination hash printed by partyline-server")
    add_common_args(parser)
    parser.add_argument("--room", default=None, help="room to enter on arrival (default: the server's default room)")
    parser.add_argument("--password", default=None, help="password for --room")
    parser.add_argument("--server-password", default=None, help="password the server asks for before anything else")
    parser.add_argument("--list-devices", action="store_true", help="show microphones and speakers, then exit")
    parser.add_argument("--listen", action="store_true", help="receive only, never transmit")
    parser.add_argument("--text-only", action="store_true", help="chat only: no audio in or out, for very slow links")

    # parser.add_argument(
    #     "--tone", type=float, default=None, help="send a test tone at this Hz instead of the microphone"
    # )

    parser.add_argument("--wav", default=None, metavar="FILE")
    parser.add_argument("--wav-loop", action="store_true")
    parser.add_argument("--null-audio", action="store_true")

    parser.add_argument("--force-profile", choices=PROFILES.keys(), default=None)
    parser.add_argument("--force-frame-ms", type=int, default=None)
    # parser.add_argument(
    #     "--rx-jitter-ms", type=int, default=0, help="testing: deliver received audio in bursts this far apart"
    # )
    # parser.add_argument(
    #     "--rx-loss", type=float, default=0.0, help="testing: drop this fraction of received audio packets (0.02 = 2%%)"
    # )
    parser.add_argument("--duration", type=float, default=0, help="seconds to run (0 = until Ctrl-C or /quit)")
    args = parser.parse_args()

    if args.list_devices:
        microphones, speakers = audio_devices()
        print("microphones:")
        for name in microphones:
            print("  ", name)
        print("speakers:")
        for name in speakers:
            print("  ", name)
        return

    if not args.server:
        parser.error("server hash required")
    try:
        server_hash = parse_hash(args.server)
    except ValueError as error:
        parser.error(str(error))

    ### INIT ###
    RNS.Reticulum(configdir=args.configdir)
    identity = load_identity(args.identity)

    tune_gc()

    print(f"Our identity hash: {identity.hash.hex()}", flush=True)

    client = Client(config_from_args(args), identity, args.name or default_name(identity))
    client.connect(server_hash, room=args.room, password=args.password, server_password=args.server_password)

    stop = threading.Event()
    threading.Thread(target=stdin_commands, args=(client, stop), daemon=True).start()
    started = time.time()
    try:
        while client.state != "closed" and not stop.is_set():
            if args.duration and time.time() - started >= args.duration:
                break
            while client.events:
                line = describe_event(client, client.events.popleft())
                if line:
                    print(line, flush=True)
            if client.error and client.state == "idle":
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    client.disconnect()
    print(client.summary(), flush=True)


if __name__ == "__main__":
    main()
