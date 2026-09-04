import collections
import queue
import threading
import time

import numpy as np
import RNS
from RNS.vendor import umsgpack as msgpack
from LXST import APP_NAME
from LXST.Network import FIELD_SIGNALLING
from LXST.Primitives.Telephony import Profiles, Signalling, PRIMITIVE_NAME

from .common import *
from .audio import Playout, Speaker

ANSWER_DELAY = 0.6   # seconds of ringing before the bridge picks up

RING_TIMEOUT = 30.0  # seconds a link may sit unidentified before it is dropped
SAMPLE_RATE = 48000


class _Endpoint:


    def __init__(self, samplerate=SAMPLE_RATE, channels=1):
        self.samplerate = samplerate
        self.channels = channels


class CallerFeed:

    def __init__(self, call):
        self.call = call
        self.samplerate = SAMPLE_RATE
        self.channels = 1
        self.queued = collections.deque()
        self.pending_samples = np.zeros((0, 1), dtype="float32")
        self.lock = threading.Lock()
        self.should_run = False
        self.packets = 0
        self.set_profile(call.profile)

    def set_profile(self, profile):
        codec = Profiles.get_codec(profile)
        codec.source = _Endpoint()
        codec.sink = None
        frame_ms = Profiles.get_frame_time(profile)
        with self.lock:
            self.codec = codec
            self.header = codec_header_byte(type(codec))
            self.frame_ms = frame_ms
            self.samples_per_frame = SAMPLE_RATE * frame_ms // 1000

    def can_receive(self, source=None):
        return len(self.queued) < 3

    def handle_frame(self, block, source=None):
        self.queued.append(np.asarray(block, dtype="float32")[:, :1])

    def start(self):
        if not self.should_run:
            self.should_run = True
            threading.Thread(target=self._job, daemon=True).start()

    def stop(self):
        self.should_run = False

    def _job(self):
        next_send = time.monotonic()
        while self.should_run:
            with self.lock:
                codec = self.codec
                header = self.header
                samples_per_frame = self.samples_per_frame
                frame_seconds = self.frame_ms / 1000

            while self.pending_samples.shape[0] < samples_per_frame and self.queued:
                self.pending_samples = np.concatenate([self.pending_samples, self.queued.popleft()])
            if self.pending_samples.shape[0] >= samples_per_frame:
                frame = self.pending_samples[:samples_per_frame]
                self.pending_samples = self.pending_samples[samples_per_frame:]
            else:
                frame = np.zeros((samples_per_frame, 1), dtype="float32")

            link = self.call.link
            if link and link.status == RNS.Link.ACTIVE:
                try:
                    data = msgpack.packb({FIELD_FRAMES: header + codec.encode(frame)})
                    RNS.Packet(link, data, create_receipt=False).send()
                    self.packets += 1
                except Exception as error:
                    RNS.log(f"Dial-in send failed: {error}", RNS.LOG_DEBUG)

            next_send += frame_seconds
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -2 * frame_seconds:
                next_send = time.monotonic()


class Call:


    def __init__(self, dialin, link):
        self.dialin = dialin
        self.server = dialin.server
        self.link = link
        self.identity = None
        self.member = None
        self.state = "new"
        self.profile = Profiles.DEFAULT_PROFILE
        self.mode = Profiles.DEFAULT_MODE
        self.decoder = None
        self.speakers = {}
        self.playout = None
        self.feed = None
        self.started_at = None

        self.room = dialin.room
        self.room_codec = make_codec(self.room.profile)
        self.room_codec.source = _Endpoint()
        self.room_codec.sink = None
        self.room_header = codec_byte(self.room.profile)
        self.room_samples_per_frame = SAMPLE_RATE * self.room.frame_ms // 1000

        self.caller_samples = np.zeros((0, 1), dtype="float32")
        self.sequence = 0
        self.gate_open_until = 0.0
        self.gate_was_open = False
        self.injected_frames = 0

        self.inbox = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

        link.set_packet_callback(lambda data, packet: self.inbox.put((data, packet)))
        link.set_remote_identified_callback(self.identified)
        link.set_link_closed_callback(self.closed)

        threading.Timer(RING_TIMEOUT, self.ring_timeout).start()
        self.signal(Signalling.STATUS_AVAILABLE)

    def label(self):
        if self.identity:
            return f"caller {RNS.prettyhexrep(self.identity.hash)}"
        return "caller (unidentified)"



### DIALIN SIGNALLING ###
    def signal(self, signals):
        if not isinstance(signals, list):
            signals = [signals]
        if self.link and self.link.status == RNS.Link.ACTIVE:
            RNS.Packet(self.link, msgpack.packb({FIELD_SIGNALLING: signals}), create_receipt=False).send()

    def identified(self, link, identity):
        self.identity = identity
        if self.dialin.allowed is not None and identity.hash not in self.dialin.allowed:
            RNS.log(f"Dial-in: {self.label()} is not allowed, rejecting", RNS.LOG_NOTICE)
            self.signal(Signalling.STATUS_REJECTED)
            threading.Timer(0.5, self.hangup).start()
            return
        self.state = "ringing"
        self.signal(Signalling.STATUS_RINGING)
        RNS.log(f"Dial-in: {self.label()} ringing", RNS.LOG_NOTICE)
        threading.Timer(ANSWER_DELAY, self.answer).start()

    def ring_timeout(self):
        if self.state == "new":
            RNS.log("Dialin: caller never identified, dropping", RNS.LOG_NOTICE)
            self.hangup()


    def answer(self):
        if self.state != "ringing" or self.link.status != RNS.Link.ACTIVE:
            return
        self.signal(Signalling.STATUS_CONNECTING)

        name = clean_name(f"{self.dialin.name_prefix}-{self.identity.hash.hex()[:6]}", "phone")
        rtt = None
        if getattr(self.link, "rtt", None):
            rtt = int(self.link.rtt * 1000)

        member, reason = self.server.add_bridge_member(self, self.identity, name, self.room, rtt)
        if member is None:
            RNS.log(f"Dial-in: {self.label()} refused: {reason}", RNS.LOG_NOTICE)
            self.signal(Signalling.STATUS_REJECTED)
            threading.Timer(0.5, self.hangup).start()
            return

        self.member = member
        self.feed = CallerFeed(self)
        depth_frames = max(1, -(-self.dialin.jitter_ms // self.room.frame_ms))
        self.playout = Playout(self.room.frame_ms, depth_frames, self.feed)
        self.playout.start()
        self.feed.start()

        self.state = "established"
        self.started_at = time.time()
        self.signal(Signalling.STATUS_ESTABLISHED)
        RNS.log(f"Dial-in: {self.label()} connected to {self.room.name} as {name}", RNS.LOG_NOTICE)

    def handle_signals(self, signals):
        for signal in signals:
            if not isinstance(signal, int):
                continue
            if signal >= Signalling.PREFERRED_PROFILE:
                profile = signal - Signalling.PREFERRED_PROFILE
                if profile in Profiles.available_profiles() and profile != self.profile:
                    self.profile = profile
                    if self.feed:
                        self.feed.set_profile(profile)
                    RNS.log(f"Dial-in: {self.label()} prefers {Profiles.profile_name(profile)}", RNS.LOG_DEBUG)
            elif signal >= Signalling.PREFERRED_MODE:
                self.mode = signal - Signalling.PREFERRED_MODE
            elif signal in (Signalling.STATUS_BUSY, Signalling.STATUS_REJECTED):
                self.hangup()







    def _worker(self):

        while self.state != "ended":
            try:
                data, packet = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                fields = msgpack.unpackb(data)
                if type(fields) is not dict:
                    continue
                if FIELD_SIGNALLING in fields:
                    signals = fields[FIELD_SIGNALLING]
                    if not isinstance(signals, list):
                        signals = [signals]
                    self.handle_signals(signals)
                if FIELD_FRAMES in fields and self.state == "established":
                    frames = fields[FIELD_FRAMES]
                    if not isinstance(frames, list):
                        frames = [frames]
                    for frame in frames:
                        self.caller_frame(frame)
            except Exception as error:
                RNS.log(f"Dial-in receive error: {error}", RNS.LOG_ERROR)


    def caller_frame(self, frame):
        if type(frame) is not bytes or len(frame) < 2:
            return
        codec_class = codec_type(frame[0])
        if codec_class is None:
            return
        if self.decoder is None or type(self.decoder) is not codec_class:
            self.decoder = codec_class()
            self.decoder.sink = _Endpoint(SAMPLE_RATE, None)
            self.decoder.source = None
        samples = np.asarray(self.decoder.decode(frame[1:]), dtype="float32")
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        self.caller_samples = np.concatenate([self.caller_samples, samples[:, :1]])
        while self.caller_samples.shape[0] >= self.room_samples_per_frame:
            chunk = self.caller_samples[: self.room_samples_per_frame]
            self.caller_samples = self.caller_samples[self.room_samples_per_frame :]
            self.inject(chunk)

    def inject(self, chunk):
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        now = time.time()
        if rms > 10 ** (self.dialin.gate_db / 20):
            self.gate_open_until = now + 0.4
        if now > self.gate_open_until:
            if self.gate_was_open:
                self.gate_was_open = False
                self.server.relay_end(self.member)
            return
        self.gate_was_open = True
        encoded = self.room_codec.encode(chunk)
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.injected_frames += 1
        self.server.relay(self.member, self.room_header + encoded, len(encoded) + 1, self.sequence)

### ROOM AUDIO ###
    def deliver_rx_loopback(self, frame, member_id, sequence):
        if self.state != "established" or not self.playout:
            return
        codec_class = codec_type(frame[0])
        if codec_class is None:
            return
        speaker = self.speakers.get(member_id)
        if speaker is None:
            speaker = Speaker(self.playout, codec_class)
            self.speakers[member_id] = speaker
        elif type(speaker.codec) is not codec_class:
            speaker.set_codec(codec_class)
        try:
            samples = speaker.codec.decode(frame[1:])
        except Exception:
            return
        sample_count = samples.shape[0]
        wanted = self.room_samples_per_frame
        if abs(sample_count - wanted) > wanted // 50:
            return
        if sample_count < wanted:
            padding = np.zeros((wanted - sample_count, samples.shape[1]), samples.dtype)
            samples = np.vstack([samples, padding])
        elif sample_count > wanted:
            samples = samples[:wanted]
        self.playout.push(member_id, samples, sequence)


    def deliver(self, frame, member_id, sequence):
        if self.state != "established" or not self.playout:
            return
        codec_class = codec_type(frame[0])
        if codec_class is None:
            return
        speaker = self.speakers.get(member_id)
        if speaker is None:
            speaker = Speaker(self.playout, codec_class)
            self.speakers[member_id] = speaker
        elif type(speaker.codec) is not codec_class:
            speaker.set_codec(codec_class)
        try:
            samples = speaker.codec.decode(frame[1:])
        except Exception:
            return
        sample_count = samples.shape[0]
        wanted = self.room_samples_per_frame
        if abs(sample_count - wanted) > wanted // 50:
            return
        if sample_count < wanted:
            padding = np.zeros((wanted - sample_count, samples.shape[1]), samples.dtype)
            samples = np.vstack([samples, padding])
        elif sample_count > wanted:
            samples = samples[:wanted]
        self.playout.push(member_id, samples, sequence)

    def deliver_end(self, member_id):
        if self.playout:
            self.playout.end_spurt(member_id)

### CLEANUP ###
    def closed(self, link):
        self.hangup()

    def hangup(self):
        if self.state == "ended":
            return
        self.state = "ended"
        if self.playout:
            self.playout.stop()
        if self.feed:
            self.feed.stop()
        if self.member:
            self.server.remove_member(self)
        if self.link and self.link.status == RNS.Link.ACTIVE:
            self.link.teardown()
        self.dialin.calls.pop(self.link, None)
        if self.started_at:
            duration = int(time.time() - self.started_at)
            packets_out = self.feed.packets if self.feed else 0
            RNS.log(
                f"Dial-in: {self.label()} hung up after {duration} s, {self.injected_frames} frames in, {packets_out} out",
                RNS.LOG_NOTICE,
            )


class DialIn:
    def __init__(self, server, identity, spec):
        self.server = server
        self.identity = identity
        self.calls = {}
        room = None
        if spec.get("room"):
            room = server.find_room(spec.get("room"))
        self.room = room or server.default_room
        self.name_prefix = clean_name(spec.get("name", "phone"), "phone")[:12]
        self.gate_db = float(spec.get("gate_db", -50))
        self.jitter_ms = int(spec.get("jitter_ms", 200))
        if spec.get("allow") or spec.get("allowed_file"):
            self.allowed = load_hash_list(spec.get("allow"), spec.get("allowed_file"))
        else:
            self.allowed = None
        self.destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, PRIMITIVE_NAME
        )
        self.destination.set_proof_strategy(RNS.Destination.PROVE_NONE)
        self.destination.set_link_established_callback(self.incoming)

    @property
    def number(self):
        return self.identity.hash.hex()

    def incoming(self, link):
        self.calls[link] = Call(self, link)
        RNS.log(f"Dialin: incoming call, {len(self.calls)} on the line", RNS.LOG_NOTICE)

    def announce(self):
        self.destination.announce()
