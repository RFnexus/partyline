import collections
import math
import time
import threading

import numpy as np
import RNS

from .common import *


class VoiceGate:
    def __init__(self, packetizer, threshold_db=-45.0, hang_seconds=0.4):
        self.packetizer = packetizer
        self.threshold = threshold_db
        self.hang = hang_seconds
        self.enabled = False
        self.open_until = 0.0
        self.level = -120.0

    def handle_frame(self, samples, samplerate=None):
        if samples.size:
            rms = float(np.sqrt(np.mean(np.square(samples))))
        else:
            rms = 0.0
        if rms > 0:
            self.level = 20 * math.log10(rms)
        else:
            self.level = -120.0
        if self.enabled:
            now = time.time()
            if self.level > self.threshold:
                self.open_until = now + self.hang
            if now < self.open_until:
                if self.packetizer.squelched:
                    self.packetizer.unsquelch()
            elif not self.packetizer.squelched:
                self.packetizer.squelch()
        return samples




class MicConditioner:
    TARGET_RMS = 0.12     # -18 dBFS

    CEILING = 0.89        # -1 dBFS peak limit

    GATE_RMS = 0.0025     # -52 dBFS

    MAX_AGC_GAIN = 8.0    # +18 dB ceiling
    MIN_AGC_GAIN = 0.25   # -12 dB floor
    ATTACK = 0.4        
    RELEASE = 0.04

    def __init__(self, gain_db=0.0, agc=False):
        self.set_gain(gain_db)
        self.agc = bool(agc)
        self.agc_gain = 1.0

    def set_gain(self, gain_db):
        self.gain_db = float(gain_db)
        self.gain = 10.0 ** (self.gain_db / 20.0)

    def set_agc(self, enabled):
        self.agc = bool(enabled)
        if not self.agc:
            self.agc_gain = 1.0

    def boost(self, samples):
        # manual gain aplied before the voice gate
        if self.gain == 1.0 or samples is None or not samples.size:
            return samples
        return (samples * self.gain).astype("float32", copy=False)

    def level(self, samples):
        # AGC and peak limiting
        if samples is None or not samples.size:
            return samples
        if self.agc:
            rms = float(np.sqrt(np.mean(np.square(samples))))
            if rms > self.GATE_RMS:
                desired = self.TARGET_RMS / rms
                desired = min(self.MAX_AGC_GAIN, max(self.MIN_AGC_GAIN, desired))
                rate = self.ATTACK if desired < self.agc_gain else self.RELEASE
                self.agc_gain += (desired - self.agc_gain) * rate
            return self._limit(samples * self.agc_gain) 
        if self.gain != 1.0:
            return self._limit(samples)
        return samples

    def _limit(self, samples):
        peak = float(np.max(np.abs(samples)))
        if peak > self.CEILING:
            samples = samples * (self.CEILING / peak)
        return samples.astype("float32", copy=False)


def gated_codec(profile_name, gate, conditioner=None):
    codec_class, codec_argument, _, _ = PROFILES[profile_name]

    class Gated(codec_class):
        def encode(self, samples):
            if conditioner is not None:
                samples = conditioner.boost(samples)
            gate.handle_frame(samples)
            if conditioner is not None:
                samples = conditioner.level(samples)
            return super().encode(samples)

    Gated.__name__ = codec_class.__name__
    return Gated(codec_argument)


def audio_devices():

    microphones = None
    speakers = None

    try:
        import LXST.Sources
        import LXST.Sinks

        microphones = [device.name for device in LXST.Sources.Backend().all_microphones()]
        speakers = [device.name for device in LXST.Sinks.Backend().all_speakers()]
    except Exception:
        try:
            import soundcard

            microphones = [device.name for device in soundcard.all_microphones()]
            speakers = [device.name for device in soundcard.all_speakers()]
        except Exception as error:
            RNS.log(f"Could not list audio devices: {error}", RNS.LOG_WARNING)
    return microphones or [], speakers or []


class Playout:

    BLOCK_MS = 60  # minimum block handed to the sound card
    
    MAX_DEPTH_MS = 3000  # Buffer limit

    SHRINK_EVERY = 15.0  # seconds between looks at whether the buffer can come down again

    LEAD_MS = 180        # audio kept queued at the sound card itself, on top of the jitter buffer

    HOLD_SECONDS = 1.0   # a member is still talking this long after its last frame

    FADE = 0.5           # gain applied per consecutive concealed frame
    REPEAT_MAX_FRAME_MS = 100  
    
    MAX_GAP = 64         # missing frames we will will reserve slots for

    def __init__(self, frame_ms, depth_frames, sink, samplerate=48000, max_depth_ms=None):
        self.frame_ms = frame_ms
        self.frame_seconds = frame_ms / 1000
        self.max_depth_ms = max_depth_ms or self.MAX_DEPTH_MS
        self.depth = max(1, int(depth_frames))
        self.depth_min = self.depth
        self.depth_max = max(self.depth, math.ceil(self.max_depth_ms / frame_ms))
        self.low_water = None  
        self.last_shrink_check = time.time()
        self.grew = 0
        self.block_frames = max(1, round(self.BLOCK_MS / frame_ms))
        self.lead_blocks = max(1, round(self.LEAD_MS / (self.block_frames * frame_ms)))
        self.lead_frames = self.lead_blocks * self.block_frames
        self.out_samples = max(1, round(samplerate * self.BLOCK_MS / 1000))
        self.out_lead_blocks = max(1, round(self.LEAD_MS / self.BLOCK_MS))
        self.out_buffer = None

        self.sink = sink
        self.samplerate = samplerate
        self.channels = None
        self.codec = None
        self.pipeline = None

        self.members = {}
        self.lock = threading.Lock()
        self.should_run = False
        self.shape = None

        self.frames_out = 0
        self.concealed = 0
        self.dropped = 0
        self.blocks_out = 0
        self.lost = 0
        self.recovered = 0

    @property
    def slack(self):
        return max(4, self.depth // 2)

    def push(self, key, samples, sequence=None):
        with self.lock:
            member = self.members.get(key)
            if member is None:
                member = {
                    "queue": collections.deque(),
                    "active": False,
                    "last": None,
                    "misses": 0,
                    "pending_misses": 0,
                    "ending": False,
                    "seen": 0.0,
                    "expect": None,
                }
                self.members[key] = member
            frames = member["queue"]
            member["seen"] = time.time()
            member["ending"] = False
            if member["pending_misses"]:
                self.concealed += member["pending_misses"]
                self.grow(member["pending_misses"])
                member["pending_misses"] = 0

            if sequence is None or member["expect"] is None:
                frames.append(samples)
            else:
                gap = (sequence - member["expect"]) & 0xFFFF
                if gap == 0:
                    frames.append(samples)
                elif gap <= self.MAX_GAP:
                    frames.extend([None] * gap)
                    frames.append(samples)
                    self.lost += gap
                elif gap >= 0x10000 - self.MAX_GAP:
                    # late or reordered: fill its slot if that slot is still queued
                    slots_back = 0x10000 - gap
                    if slots_back <= len(frames) and frames[len(frames) - slots_back] is None:
                        frames[len(frames) - slots_back] = samples
                        self.lost -= 1
                        self.recovered += 1
                    return
                else:
                    frames.append(samples)  # sender restarted or wrapped strangely

            if sequence is not None:
                member["expect"] = (sequence + 1) & 0xFFFF
            else:
                member["expect"] = None

            if not member["active"] and len(frames) >= self.depth + self.lead_frames:
                member["active"] = True
            if len(frames) > self.depth + self.lead_frames + self.slack:
                # a burst bigger than the buffer
                if not self.grow(len(frames) - self.depth - self.lead_frames):
                    frames.popleft()  #
                    self.dropped += 1
            if self.shape is None:
                self.shape = samples.shape


    def grow(self, frames_more):
        if self.depth >= self.depth_max:
            return False
        self.depth = min(self.depth_max, self.depth + max(1, int(frames_more)))
        self.grew += 1
        return True

    def set_floor(self, depth_frames):
        self.depth_min = max(1, int(depth_frames))
        self.depth = self.depth_min
        self.low_water = None

    def set_max_depth(self, max_depth_ms):
        self.max_depth_ms = max(self.frame_ms, int(max_depth_ms))
        self.depth_max = max(self.depth, math.ceil(self.max_depth_ms / self.frame_ms))

    @property
    def depth_ms(self):
        return self.depth * self.frame_ms

    def maybe_shrink(self):
        now = time.time()
        if now - self.last_shrink_check < self.SHRINK_EVERY:
            return
        self.last_shrink_check = now
        if self.low_water is not None and self.depth > self.depth_min and self.low_water > self.depth // 2:
            self.depth = max(self.depth_min, self.depth - self.low_water // 2)
        self.low_water = None

    def end_spurt(self, key):
        with self.lock:
            member = self.members.get(key)
            if member:
                member["ending"] = True
                if member["queue"]:
                    member["active"] = True
                elif not member["active"]:
                    member["expect"] = None

    def remove(self, key):
        with self.lock:
            self.members.pop(key, None)

    def clear(self):
        with self.lock:
            self.members.clear()

    def start(self):
        if not self.should_run:
            self.should_run = True
            threading.Thread(target=self._job, daemon=True).start()

    def stop(self):
        self.should_run = False

    def _tick(self):
        # A tick is one frame of timed audio
        mixed = None
        now = time.time()
        for member in self.members.values():
            if not member["active"]:
                continue
            frames = member["queue"]
            if self.low_water is None or len(frames) < self.low_water:
                self.low_water = len(frames)
            frame = frames.popleft() if frames else None
            if frame is not None:
                member["last"] = frame
                member["misses"] = 0
            elif not frames and member["ending"]:


                # the talker told us it stopped and everything it sent has played
                member["active"] = False
                member["last"] = None
                member["expect"] = None
                member["pending_misses"] = 0
                member["ending"] = False
                continue
            elif frames or (now - member["seen"] <= self.HOLD_SECONDS and member["last"] is not None):
                pass  # a lost or late frame
            else:
                # talk spurt over
                member["active"] = False
                member["last"] = None
                member["expect"] = None
                member["pending_misses"] = 0 # pauses in speech /audio
                continue
            if frame is None:
                member["misses"] += 1
                if frames:
                    self.concealed += 1  # a known-lost frame 
                else:
                    member["pending_misses"] += 1  


                repeatable = member["last"] is not None and member["misses"] <= 4 and self.frame_ms <= self.REPEAT_MAX_FRAME_MS
                if repeatable:
                    frame = member["last"] * (self.FADE ** member["misses"])
                else:
                    frame = np.zeros(self.shape, dtype="float32")

            if mixed is None:
                mixed = frame.astype("float32", copy=True)
            elif frame.shape == mixed.shape:
                mixed += frame
        return mixed

    def sink_backlog(self):
        backlog = getattr(self.sink, "frame_deque", None)
        if backlog is None:
            backlog = getattr(self.sink, "queued", ())
        return len(backlog)

    def _job(self):
        while self.should_run:
            if not self.sink.can_receive(self) or self.sink_backlog() >= self.out_lead_blocks:
                time.sleep(min(0.005, self.frame_seconds / 8))
                continue

            with self.lock:
                while self.out_buffer is None or self.out_buffer.shape[0] < self.out_samples:
                    mixed = self._tick()
                    if mixed is None:
                        break
                    self.frames_out += 1
                    if self.out_buffer is None:
                        self.out_buffer = mixed.astype("float32", copy=True)
                    else:
                        self.out_buffer = np.concatenate([self.out_buffer, mixed])
                self.maybe_shrink()

            if self.out_buffer is None or self.out_buffer.shape[0] == 0:
                time.sleep(self.frame_seconds / 2)
                continue

            take = min(self.out_samples, self.out_buffer.shape[0])
            block = np.clip(self.out_buffer[:take], -1.0, 1.0)
            self.out_buffer = self.out_buffer[take:] if take < self.out_buffer.shape[0] else None
            self.sink.handle_frame(block, self)
            self.blocks_out += 1


class Speaker:
    def __init__(self, playout, codec_class):
        self.playout = playout
        self.set_codec(codec_class)

    def set_codec(self, codec_class):
        self.codec = codec_class()
        self.codec.sink = self.playout
        self.codec.source = None
