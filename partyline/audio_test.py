import math
import sys
import threading
from types import SimpleNamespace

import numpy as np

from .audio import MicConditioner
from .common import PROFILES, frame_ms


class AudioTest:
    SAMPLE_RATE = 48000
    RECORD_SECONDS = 5
    BLOCK_SAMPLES = 1920

    def __init__(self):
        self.thread = None
        self.stopped = threading.Event()
        self.samples = None
        self.state = "idle"
        self.error = None
        self.level = -120.0
        self.clipped = False
        self.progress = 0.0

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def _start(self, state, job, *args):
        if self.busy:
            return False
        self.stopped.clear()
        self.state = state
        self.error = None
        self.progress = 0.0
        self.thread = threading.Thread(target=self._run, args=(job, args), daemon=True)
        self.thread.start()
        return True

    def _run(self, job, args):
        try:
            job(*args)
        except Exception as error:
            self.error = str(error)
        finally:
            self.state = "error" if self.error else "idle"

    def stop(self):
        self.stopped.set()

    def record(self, input_name=None, gain_db=0.0, agc=False):
        return self._start("recording", self._record, input_name, gain_db, agc)

    def _record(self, input_name, gain_db, agc):
        from LXST.Sources import Backend

        self.samples = None
        self.clipped = False
        self.level = -120.0
        conditioner = MicConditioner(gain_db, agc)
        backend = Backend(preferred_device=input_name, samplerate=self.SAMPLE_RATE)
        blocks = []
        total = 0
        limit = self.SAMPLE_RATE * self.RECORD_SECONDS
        blocksize = None if sys.platform == "darwin" else self.BLOCK_SAMPLES
        try:
            with backend.get_recorder(samples_per_frame=blocksize) as recorder:
                while not self.stopped.is_set() and total < limit:
                    block = np.asarray(
                        recorder.record(numframes=min(self.BLOCK_SAMPLES, limit - total)), dtype="float32"
                    )
                    if self.stopped.is_set():
                        break
                    if block.ndim == 1:
                        block = block.reshape(-1, 1)
                    if not block.size:
                        raise RuntimeError("The microphone returned no samples")
                    block = block[: limit - total]
                    boosted = conditioner.boost(block)
                    self.clipped = self.clipped or bool(np.max(np.abs(boosted)) >= 0.99)
                    rms = float(np.sqrt(np.mean(np.square(boosted))))
                    self.level = 20 * math.log10(rms) if rms > 0 else -120.0
                    blocks.append(conditioner.level(boosted).copy())
                    total += len(block)
                    self.progress = total / limit
        finally:
            backend.release_recorder()
        if blocks:
            self.samples = np.concatenate(blocks)

    def play(self, output_name=None, profile="opus-high", low_latency=False, tone=False):
        if not tone and self.samples is None:
            return False
        return self._start("processing", self._play, output_name, profile, low_latency, tone)

    def _preview(self, samples, profile):
        codec_class, argument, _, _ = PROFILES[profile]
        encoder = codec_class(argument)
        decoder = codec_class()
        encoder.source = SimpleNamespace(samplerate=self.SAMPLE_RATE)
        decoder.sink = SimpleNamespace(samplerate=self.SAMPLE_RATE, channels=None)
        count = round(self.SAMPLE_RATE * frame_ms(profile) / 1000)
        blocks = []
        for offset in range(0, len(samples), count):
            if self.stopped.is_set():
                return None
            block = samples[offset : offset + count]
            if len(block) < count:
                block = np.pad(block, ((0, count - len(block)), (0, 0)))
            decoded = decoder.decode(encoder.encode(block))
            if len(decoded) < count:
                decoded = np.pad(decoded, ((0, count - len(decoded)), (0, 0)))
            blocks.append(decoded[:count])
        return np.concatenate(blocks)[: len(samples)]

    def _play(self, output_name, profile, low_latency, tone):
        from LXST.Sinks import Backend

        if tone:
            positions = np.arange(self.SAMPLE_RATE) / self.SAMPLE_RATE
            samples = (0.1 * np.sin(2 * np.pi * 440 * positions) * np.sin(np.pi * positions) ** 2).astype("float32")[
                :, None
            ]
        else:
            samples = self._preview(self.samples, profile)
        if samples is None or self.stopped.is_set():
            return
        self.state = "playing"
        backend = Backend(preferred_device=output_name, samplerate=self.SAMPLE_RATE)
        blocksize = None if sys.platform == "darwin" else self.BLOCK_SAMPLES
        try:
            with backend.get_player(samples_per_frame=blocksize, low_latency=low_latency) as player:
                for offset in range(0, len(samples), self.BLOCK_SAMPLES):
                    if self.stopped.is_set():
                        break
                    block = samples[offset : offset + self.BLOCK_SAMPLES]
                    channels = getattr(backend.device, "channels", block.shape[1])
                    player.play(np.clip(block[:, :channels], -1, 1))
                    self.progress = min(1, (offset + len(block)) / len(samples))
        finally:
            backend.release_player()

    def discard(self):
        if not self.busy:
            self.samples = None
