import queue
import tkinter as tk
from tkinter import ttk

from . import diagnostics
from .audio import audio_devices
from .audio_test import AudioTest
from .common import PROFILES
from .i18n import _


class DiagnosticsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title(_("Audio diagnostics"))
        self.transient(app.root)
        self.geometry("900x640")
        self.minsize(620, 400)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.sample_label = ttk.Label(frame)
        self.sample_label.pack(anchor="w", pady=(0, 8))
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("metric", "value"), show="headings", selectmode="browse")
        self.table.heading("metric", text=_("Measurement"))
        self.table.heading("value", text=_("Value"))
        self.table.column("metric", width=360, minwidth=360)
        self.table.column("value", width=490, minwidth=490)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text=_("Copy diagnostic report"), command=self.copy).pack(side="left")
        self.copy_label = ttk.Label(buttons, wraplength=220)
        self.copy_label.pack(side="left", padx=8)
        ttk.Button(buttons, text=_("Close"), command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.job = None
        self.tick()

    def tick(self):
        if not self.app.settings["debug_stats"]:
            self.close()
            return
        report = self.app.debug_report or diagnostics.snapshot()
        prefix = _("Live sample") if self.app.client else _("Last sample")
        self.sample_label.configure(text=f"{prefix}: {report['sampled_at']}")
        for index, row in enumerate(diagnostics.rows(report)):
            iid = str(index)
            if self.table.exists(iid):
                self.table.item(iid, values=row)
            else:
                self.table.insert("", "end", iid=iid, values=row)
        self.job = self.after(500, self.tick)

    def copy(self):
        self.app.copy_diagnostics()
        self.copy_label.configure(text=_("Copied"))

    def close(self):
        if self.job is not None:
            self.after_cancel(self.job)
        self.app.diagnostics_window = None
        self.destroy()


class AudioSetupDialog(tk.Toplevel):
    def __init__(self, app):
        from .gui import pretty_key

        super().__init__(app.root)
        self.app = app
        self.test = AudioTest()
        self.closing = False
        self.capturing = False
        self.key_down = False
        self.ptt_active = False
        self.title(_("Audio setup"))
        self.transient(app.root)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.bind("<KeyPress>", self.key_press)
        self.bind("<KeyRelease>", self.key_release)
        self.default_device = _("(system default)")
        settings = app.settings
        self.original_hotkey = app.hotkeys.spec
        self.key_spec = settings["ptt_key"]
        self.input_var = tk.StringVar(value=settings["input"] or self.default_device)
        self.output_var = tk.StringVar(value=settings["output"] or self.default_device)
        self.gain_var = tk.DoubleVar(value=settings["tx_gain_db"])
        self.agc_var = tk.BooleanVar(value=settings["mic_agc"])
        self.profile_var = tk.StringVar(value="opus-high")
        self.mode_var = tk.StringVar(value=settings["mode"])
        self.toggle_var = tk.BooleanVar(value=settings["ptt_toggle"])
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)
        audio_tab = ttk.Frame(notebook, padding=8)
        ptt_tab = ttk.Frame(notebook, padding=8)
        notebook.add(audio_tab, text=_("Audio test"))
        notebook.add(ptt_tab, text=_("Push To Talk"))
        devices = ttk.LabelFrame(audio_tab, text=_("1. Choose devices"), padding=10)
        devices.pack(fill="x")
        microphones, speakers = audio_devices()
        self.controls = []
        for row, (label, variable, names) in enumerate(
            (
                (_("Microphone"), self.input_var, microphones),
                (_("Speakers"), self.output_var, speakers),
            )
        ):
            ttk.Label(devices, text=label).grid(row=row, column=0, sticky="w", pady=4)
            box = ttk.Combobox(
                devices, textvariable=variable, values=[self.default_device] + names, state="readonly", width=42
            )
            box.grid(row=row, column=1, sticky="ew", padx=8, pady=4)
            self.controls.append(box)
        devices.columnconfigure(1, weight=1)
        self.speaker_button = ttk.Button(devices, text=_("Test speakers"), command=lambda: self.play(tone=True))
        self.speaker_button.grid(row=2, column=1, sticky="w", padx=8, pady=4)
        mic = ttk.LabelFrame(audio_tab, text=_("2. Record and listen"), padding=10)
        mic.pack(fill="x", pady=10)
        ttk.Label(mic, text=_("TX gain")).grid(row=0, column=0, sticky="w")
        scale = ttk.Scale(mic, from_=-20, to=20, variable=self.gain_var)
        scale.grid(row=0, column=1, sticky="ew", padx=8)
        self.controls.append(scale)
        self.gain_label = ttk.Label(mic, width=9)
        self.gain_label.grid(row=0, column=2)
        agc = ttk.Checkbutton(mic, text=_("Automatic gain control (AGC)"), variable=self.agc_var)
        agc.grid(row=1, column=0, columnspan=3, sticky="w", pady=6)
        self.controls.append(agc)
        self.meter = ttk.Progressbar(mic, maximum=80)
        self.meter.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
        self.level_label = ttk.Label(mic, width=14)
        self.level_label.grid(row=2, column=2)
        ttk.Label(mic, text=_("Preview codec")).grid(row=3, column=0, sticky="w", pady=6)
        codec_box = ttk.Combobox(mic, textvariable=self.profile_var, values=tuple(PROFILES), state="readonly", width=18)
        codec_box.grid(row=3, column=1, sticky="w", padx=8)
        self.controls.append(codec_box)
        actions = ttk.Frame(mic)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=6)
        self.record_button = ttk.Button(actions, text=_("Record 5 seconds"), command=self.record)
        self.record_button.pack(side="left")
        self.play_button = ttk.Button(actions, text=_("Play recording"), command=self.play)
        self.play_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(actions, text=_("Stop"), command=self.test.stop)
        self.stop_button.pack(side="left")
        self.progress = ttk.Progressbar(mic, maximum=1)
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
        self.result_label = ttk.Label(mic, wraplength=530)
        self.result_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=4)
        mic.columnconfigure(1, weight=1)
        ptt = ttk.LabelFrame(ptt_tab, text=_("Test push to talk"), padding=10)
        ptt.pack(fill="x")
        self.key_label = ttk.Label(ptt, text=pretty_key(self.key_spec), width=22)
        self.key_label.grid(row=0, column=0, sticky="w")
        ttk.Button(ptt, text=_("Set shortcut..."), command=self.capture_key).grid(row=0, column=1, padx=8)
        ttk.Checkbutton(ptt, text=_("Toggle: press once to talk, again to stop"), variable=self.toggle_var).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=4
        )
        ttk.Label(ptt, text=_("Transmit mode")).grid(row=2, column=0, sticky="w", pady=4)
        modes = ttk.Frame(ptt)
        modes.grid(row=2, column=1, columnspan=2, sticky="w", padx=8)
        for mode, label in (("ptt", _("Push To Talk")), ("vox", _("Voice Activity")), ("open", _("Continuous"))):
            ttk.Radiobutton(modes, text=label, variable=self.mode_var, value=mode).pack(side="left", padx=(0, 8))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        self.disconnect_button = ttk.Button(buttons, text=_("Disconnect to test"), command=app.disconnect)
        self.disconnect_button.pack(side="left")
        ttk.Button(buttons, text=_("Cancel"), command=self.close).pack(side="right")
        self.save_button = ttk.Button(buttons, text=_("Save"), command=self.save)
        self.save_button.pack(side="right", padx=8)
        self.job = None
        self.tick()

    def online(self):
        return self.app.client is not None or self.app.wanted

    def record(self):
        if not self.online():
            name = self.input_var.get()
            self.test.record(None if name == self.default_device else name, self.gain_var.get(), self.agc_var.get())

    def play(self, tone=False):
        if not self.online():
            name = self.output_var.get()
            self.test.play(
                None if name == self.default_device else name,
                self.profile_var.get(),
                self.app.settings["low_latency"],
                tone,
            )

    def capture_key(self):
        self.capturing = True
        self.key_label.configure(text=_("press a key or mouse button..."))
        while not self.app.hotkeys.captured_keys.empty():
            self.app.hotkeys.captured_keys.get_nowait()
        self.app.hotkeys.capture = self.app.hotkeys.active
        self.focus_set()

    def set_key(self, key):
        from .gui import pretty_key

        self.capturing = False
        self.key_spec = key
        self.key_label.configure(text=pretty_key(key))
        self.app.hotkeys.capture = False
        self.app.hotkeys.spec = key
        self.key_down = False
        self.ptt_active = False

    def key_press(self, event):
        from .gui import tk_key_name

        if self.app.hotkeys.active:
            return
        key = tk_key_name(event.keysym)
        if self.capturing:
            self.set_key(key)
            return "break"
        if key == self.key_spec:
            self.hotkey("down")
            return "break"

    def key_release(self, event):
        from .gui import tk_key_name

        if not self.app.hotkeys.active and tk_key_name(event.keysym) == self.key_spec:
            self.hotkey("up")
            return "break"

    def hotkey(self, kind):
        from .gui import pretty_key

        if kind == "down" and not self.key_down:
            self.ptt_active = not self.ptt_active if self.toggle_var.get() else True
        elif kind == "up" and not self.toggle_var.get():
            self.ptt_active = False
        self.key_down = kind == "down"
        label = pretty_key(self.key_spec)
        self.key_label.configure(text=f"{label} [PTT]" if self.ptt_active else label)

    def tick(self):
        busy = self.test.busy
        if self.closing and not busy:
            self.finish_close()
            return
        online = self.online()
        if online and busy:
            self.test.stop()
        self.disconnect_button.state(["!disabled"] if online else ["disabled"])
        for button in (self.record_button, self.speaker_button):
            button.state(["disabled"] if online or busy or self.closing else ["!disabled"])
        self.play_button.state(
            ["!disabled"]
            if not online and not busy and self.test.samples is not None and not self.closing
            else ["disabled"]
        )
        self.stop_button.state(["!disabled"] if busy else ["disabled"])
        self.save_button.state(["disabled"] if busy or self.closing else ["!disabled"])
        for control in self.controls:
            control.state(["disabled"] if busy or self.closing else ["!disabled"])
        self.gain_label.configure(text=_("{0:+.0f} dB").format(self.gain_var.get()))
        self.meter["value"] = max(0, self.test.level + 80)
        self.level_label.configure(
            text=_("{0:.0f} dB").format(self.test.level) if self.test.level > -119 else _("no input")
        )
        self.progress["value"] = self.test.progress
        if self.closing:
            text = _("Stopping audio...")
        elif self.test.error:
            text = _("Audio test failed: {0}").format(self.test.error)
        elif busy:
            text = {
                "recording": _("Recording locally..."),
                "processing": _("Preparing codec preview..."),
                "playing": _("Playing locally..."),
            }.get(self.test.state, "")
        elif self.test.clipped:
            text = _("Clipping detected. Lower the microphone level or TX gain and record again.")
        elif self.test.samples is not None:
            text = _("Recording ready")
        else:
            text = ""
        self.result_label.configure(text=text)
        if self.capturing and self.app.hotkeys.active:
            try:
                self.set_key(self.app.hotkeys.captured_keys.get_nowait())
            except queue.Empty:
                pass
        self.job = self.after(100, self.tick)

    def save(self):
        if self.test.busy:
            return
        self.app.settings.update(
            input=None if self.input_var.get() == self.default_device else self.input_var.get(),
            output=None if self.output_var.get() == self.default_device else self.output_var.get(),
            tx_gain_db=round(self.gain_var.get(), 1),
            mic_agc=self.agc_var.get(),
            ptt_key=self.key_spec,
            ptt_toggle=self.toggle_var.get(),
            mode=self.mode_var.get(),
        )
        self.app.settings.save()
        self.original_hotkey = self.key_spec
        self.app.apply_settings()
        self.close()

    def close(self):
        self.closing = True
        self.test.stop()
        if not self.test.busy:
            self.finish_close()

    def finish_close(self):
        if self.job is not None:
            self.after_cancel(self.job)
        self.test.discard()
        self.app.hotkeys.capture = False
        self.app.hotkeys.spec = self.original_hotkey
        while not self.app.hotkeys.ptt_events.empty():
            self.app.hotkeys.ptt_events.get_nowait()
        self.app.audio_setup_window = None
        self.destroy()
