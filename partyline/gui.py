#!/usr/bin/env python3
import argparse
import os
import queue
import sys
import threading
import time

import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from tkinter import font as tkfont

import RNS
from LXST import APP_NAME

from .client import Client, Config, MODES, default_name, audio_devices, tune_gc
from .common import *
from .prefs import Settings, ServerList, IDENTITY_FILE
from .sounds import SoundPlayer

### ICONS / MISC ###
HERE = os.path.dirname(os.path.abspath(__file__))
ART_FILE = os.path.join(HERE, "partyline.png")
ICON_DIR = os.path.join(HERE, "icons") 

try:
    from pynput import keyboard as pynput_keyboard
    from pynput import mouse as pynput_mouse
except Exception:
    pynput_keyboard = None
    pynput_mouse = None

REFRESH_MS = 100
HOTKEY_MS = 25
TALKING_SECONDS = 0.35  # a user is "talking" if a frame arrived this recently
RELEASE_MS = 60  # X11 auto-repeat sends release/press pairs; a release counts only if no press follows at once
DEFAULT_DEVICE = "(system default)"
USER_ICON_KINDS = ("user_idle", "user_talking", "user_muted", "user_deaf", "user_localmute", "user_keyboard")
CHAT_ONLY_WARNING = (
    "Connect chat-only?\n\nYou will not be able to talk or hear anyone. Only the chat window works. "
    "Use this on very slow links where voice cannot get through."
)

PALETTES = {
    "light": {
        "bg": "#f0f0f0",
        "fg": "#000000",
        "field": "#ffffff",
        "btn": "#e4e4e4",
        "btn_active": "#d4d4d4",
        "border": "#c4c4c4",
        "select": "#4c8bd6",
        "selfg": "#ffffff",
        "muted": "#777777",
        "sys": "#3b6ea5",
        "err": "#c0392b",
        "me": "#2e7d32",
        "tx_off": "#dddddd",
    },
    "dark": {
        "bg": "#2b2b2b",
        "fg": "#e6e6e6",
        "field": "#1e1e1e",
        "btn": "#3c3c3c",
        "btn_active": "#4a4a4a",
        "border": "#4a4a4a",
        "select": "#3d6fb4",
        "selfg": "#ffffff",
        "muted": "#9a9a9a",
        "sys": "#7fb0ea",
        "err": "#ef7b73",
        "me": "#7bc67e",
        "tx_off": "#444444",
    },
}


### ICONS / MISC ###
def load_icon(kind, operator=False):
    file_name = kind + ("+op" if operator else "") + ".png"
    try:
        return tk.PhotoImage(file=os.path.join(ICON_DIR, file_name))
    except tk.TclError:
        return make_icon(kind, operator=operator)

# unused
def make_icon(kind, size=14, operator=False):
    image = tk.PhotoImage(width=size, height=size)
    centre = (size - 1) / 2
    radius = size / 2 - 1

    def disc(color):
        for row in range(size):
            for column in range(size):
                if (column - centre) ** 2 + (row - centre) ** 2 <= radius * radius:
                    image.put(color, (column, row))

    def box(color, left, top, right, bottom):
        for row in range(top, bottom + 1):
            for column in range(left, right + 1):
                image.put(color, (column, row))

    if kind in ("server", "server_locked"):
        box("#455a64", 1, 2, size - 2, size - 3)
        box("#78909c", 1, 2, size - 2, 3)
        box("#cfd8dc", 3, size - 5, size - 4, size - 5)
        if kind == "server_locked":
            box("#f0b429", size - 7, size - 7, size - 1, size - 1)
            box("#5d4300", size - 5, size - 4, size - 3, size - 3)
    elif kind in ("channel", "channel_locked"):
        box("#4c8bd6", 2, 2, size - 3, size - 3)
        for column, row in ((2, 2), (size - 3, 2), (2, size - 3), (size - 3, size - 3)):
            image.put("", (column, row))  # rounded corners
        box("#dbe9f8", 4, 5, size - 5, 5)
        box("#dbe9f8", 4, 8, size - 7, 8)
        if kind == "channel_locked":
            box("#f0b429", size - 7, size - 7, size - 1, size - 1)
            box("#5d4300", size - 5, size - 4, size - 3, size - 3)
    elif kind == "user_idle":
        disc("#8a8f98")
    elif kind == "user_talking":
        disc("#3fb950")
    elif kind == "user_muted":
        disc("#d9534f")
        box("#ffffff", 3, size // 2 - 1, size - 4, size // 2)
    elif kind == "user_deaf":
        disc("#9b59b6")
        box("#ffffff", 3, size // 2 - 1, size - 4, size // 2)
        box("#ffffff", size // 2 - 1, 3, size // 2, size - 4)
    elif kind == "user_self":
        disc("#5b9bd5")
    elif kind == "user_localmute":
        disc("#8a8f98")
        for offset in range(2, size - 2):
            image.put("#d9534f", (offset, offset))
            if offset > 2:
                image.put("#d9534f", (offset, offset - 1))

    if operator:
        box("#f0b429", size - 6, 0, size - 1, 5)
        box("#7a5c00", size - 4, 2, size - 3, 3)

    return image


def set_window_icon(root):
    images = []
    for size in (16, 32, 48, 64, 128, 256):
        path = os.path.join(ICON_DIR, f"app_{size}.png")
        try:
            images.append(tk.PhotoImage(file=path))
        except tk.TclError:
            pass
    if images:
        root.iconphoto(True, *images)
        root.app_icon_images = images  # keep references or Tk drops them




### SERVER DISCOVERY ###
class Discovery:



    aspect_filter = f"{APP_NAME}.{ASPECT}"

    def __init__(self):
        self.servers = {}
        self.lock = threading.Lock()
        RNS.Transport.register_announce_handler(self)
        threading.Thread(target=self.scan_cache, daemon=True).start()


    def received_announce(self, destination_hash, announced_identity, app_data):
        self.add(destination_hash, unpack_announce(app_data), time.time())




    def add(self, destination_hash, announced, seen_at):
        announced = announced or {}
        flags = announced.get("flags", 0)
        with self.lock:
            if flags & ANNOUNCE_HIDDEN:
                self.servers.pop(destination_hash.hex(), None) 
                return
            self.servers[destination_hash.hex()] = {
                "name": announced.get("name") or "(unnamed server)",
                "seen": seen_at,
                "hash": destination_hash,
                "flags": flags,
                "version": announced.get("version"),
                "description": announced.get("description") or "",
                "language": announced.get("language") or "",
                "country": announced.get("country") or "",
            }

    def flags_for(self, hash_hex):
        with self.lock:
            server = self.servers.get(hash_hex)
        if server:
            return server["flags"]
        return 0

    def scan_cache(self):
        try:
            for destination_hash, entry in list(RNS.Identity.known_destinations.items()):
                try:
                    identity = RNS.Identity.recall(destination_hash, _no_use=True)
                except TypeError:
                    identity = RNS.Identity.recall(destination_hash)  # older RNS
                if identity and is_room_destination(destination_hash, identity):
                    self.add(destination_hash, unpack_announce(entry[3]), float(entry[0]))
        except Exception as error:
            RNS.log(f"Announce cache scan failed: {error}", RNS.LOG_DEBUG)

    def snapshot(self):
        with self.lock:
            return sorted(self.servers.values(), key=lambda server: -server["seen"])


### HOTKEYS & PYNPUT ###

TK_TO_NAME = {
    "control_l": "ctrl",
    "control_r": "ctrl_r",
    "alt_l": "alt",
    "alt_r": "alt_r",
    "shift_l": "shift",
    "shift_r": "shift_r",
    "super_l": "cmd",
    "super_r": "cmd_r",
    "return": "enter",
    "escape": "esc",
    "prior": "page_up",
    "next": "page_down",
    "iso_level3_shift": "alt_gr",
    "kp_enter": "enter",
}


def tk_key_name(keysym):
    lowered = keysym.lower()
    return TK_TO_NAME.get(lowered, lowered)


def pynput_key_name(key):
    if hasattr(key, "name"):
        return key.name.lower()
    if getattr(key, "char", None):
        return key.char.lower()
    virtual_key = getattr(key, "vk", None)
    if virtual_key:
        return f"vk{virtual_key}"
    return None


def pretty_key(spec):
    if not spec:
        return "(none)"
    if spec.startswith("mouse:"):
        return "Mouse button " + spec[6:]
    return spec.replace("_", " ").title()


class Hotkeys:
    def __init__(self):
        self.ptt_events = queue.Queue()  # ("down" | "up", key name) for the main window
        self.captured_keys = queue.Queue()  # key names for the Settings dialog while capturing
        self.spec = None
        self.capture = False
        self.keyboard_listener = None
        self.mouse_listener = None
        self.active = False

    def start(self):
        if self.active or not pynput_keyboard:
            return False
        try:
            self.keyboard_listener = pynput_keyboard.Listener(
                on_press=lambda key: self._key(key, True), on_release=lambda key: self._key(key, False)
            )
            self.keyboard_listener.start()
            self.mouse_listener = pynput_mouse.Listener(on_click=self._click)
            self.mouse_listener.start()
            self.active = True
        except Exception as error:
            RNS.log(f"Global hotkeys unavailable: {error}", RNS.LOG_WARNING)
            self.active = False
        return self.active

    def stop(self):
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener:
                try:
                    listener.stop()
                except Exception:
                    pass
        self.keyboard_listener = None
        self.mouse_listener = None
        self.active = False

    def _key(self, key, down):
        name = pynput_key_name(key)
        if name is None:
            return
        if self.capture:
            if down:
                self.captured_keys.put(name)
            return
        if self.spec == name:
            self.ptt_events.put(("down" if down else "up", name))

    def _click(self, x, y, button, pressed):
        if button in (pynput_mouse.Button.left, pynput_mouse.Button.right):
            return  # never steal ordinary clicks
        name = "mouse:" + str(getattr(button, "value", getattr(button, "name", "?")))
        if self.capture:
            if pressed:
                self.captured_keys.put(name)
            return
        if self.spec == name:
            self.ptt_events.put(("down" if pressed else "up", name))


### DIALOGS ###

class Dialog(tk.Toplevel):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda event: self.cancel())

    def show(self):
        self.update_idletasks()
        parent_x = self.master.winfo_rootx()
        parent_y = self.master.winfo_rooty()
        self.geometry(f"+{parent_x + 60}+{parent_y + 60}")
        self.grab_set()
        self.wait_window()
        return self.result

    def cancel(self):
        self.result = None
        self.destroy()


class ServerEditDialog(Dialog):
    def __init__(self, parent, entry=None, default_user_name=""):
        super().__init__(parent, "Edit Server" if entry else "Add Server")
        entry = entry or {}
        form = ttk.Frame(self, padding=10)
        form.pack(fill="both", expand=True)
        self.variables = {}

        rows = (
            ("Label", "label", ""),
            ("Address (hash)", "hash", ""),
            ("Username", "name", default_user_name),
            ("Server password (Optional)", "server_password", ""),
            ("Room (Optional)", "room", ""),
            ("Room password (Optional)", "password", ""),
        )

        for row, (text, key, placeholder) in enumerate(rows):
            ttk.Label(form, text=text).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            variable = tk.StringVar(value=entry.get(key, "") or "")
            self.variables[key] = variable
            show = "*" if key in ("password", "server_password") else ""
            ttk.Entry(form, textvariable=variable, width=40, show=show).grid(row=row, column=1, sticky="ew", pady=3)

        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="OK", command=self.ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right", padx=6)
        self.bind("<Return>", lambda event: self.ok())

    def ok(self):
        values = {key: variable.get().strip() for key, variable in self.variables.items()}
        try:
            parse_hash(values["hash"])
        except ValueError as error:
            messagebox.showerror("Address", f"Address {error}", parent=self)
            return
        if not values["label"]:
            values["label"] = values["hash"][:12]
        self.result = values
        self.destroy()


class ConnectDialog(Dialog):
    def __init__(self, app):
        super().__init__(app.root, "Connect to Server")
        self.app = app
        self.minsize(560, 340)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        favourites_tab = ttk.Frame(self.notebook)
        discovered_tab = ttk.Frame(self.notebook)
        self.notebook.add(favourites_tab, text="Favorites")
        self.notebook.add(discovered_tab, text="Discovered")

        self.favourites = ttk.Treeview(
            favourites_tab, columns=("server", "room", "user"), show="tree headings", selectmode="browse"
        )
        self.favourites.heading("#0", text="")
        self.favourites.column("#0", width=28, stretch=False)
        for column, heading, width in (("server", "Server", 150), ("room", "Room", 100), ("user", "Username", 100)):
            self.favourites.heading(column, text=heading)
            self.favourites.column(column, width=width, anchor="w")
        self.favourites.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(favourites_tab, command=self.favourites.yview).pack(side="right", fill="y")
        self.favourites.bind("<Double-1>", lambda event: self.connect())

        filter_frame = ttk.Frame(discovered_tab)
        filter_frame.pack(side="top", fill="x", pady=(0, 4))
        ttk.Label(filter_frame, text="Filter").pack(side="left")
        self.discovered_filter = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.discovered_filter).pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.discovered_filter.trace_add("write", lambda *event: self.refresh_discovered())
        self.discovered_descriptions = {}
        self.discovered_tip = None
        self.discovered_tip_row = None

        self.discovered = ttk.Treeview(
            discovered_tab, columns=("name", "hash", "locale", "seen"), show="tree headings", selectmode="browse"
        )
        self.discovered.heading("#0", text="")
        self.discovered.column("#0", width=28, stretch=False)
        for column, heading, width in (
            ("name", "Server", 160),
            ("hash", "Address", 210),
            ("locale", "Locale", 70),
            ("seen", "Last seen", 80),
        ):
            self.discovered.heading(column, text=heading)
            self.discovered.column(column, width=width, anchor="w")
        self.discovered.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(discovered_tab, command=self.discovered.yview).pack(side="right", fill="y")
        self.discovered.bind("<Double-1>", lambda event: self.connect())
        self.discovered.bind("<Motion>", self.discovered_hover)
        self.discovered.bind("<Leave>", lambda event: self.hide_discovered_tip())

        buttons = ttk.Frame(self, padding=(10, 4, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Add New...", command=self.add).pack(side="left")
        self.edit_button = ttk.Button(buttons, text="Edit...", command=self.edit)
        self.edit_button.pack(side="left", padx=4)
        self.remove_button = ttk.Button(buttons, text="Remove", command=self.remove)
        self.remove_button.pack(side="left")
        self.favourite_button = ttk.Button(buttons, text="Add to Favorites", command=self.favourite)
        self.favourite_button.pack(side="left", padx=4)
        # packed right to left: Cancel at the edge, then Chat only, then Connect
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Chat only", command=self.connect_chat_only).pack(side="right", padx=6)
        ttk.Button(buttons, text="Connect", command=self.connect, default="active").pack(side="right")

        self.notebook.bind("<<NotebookTabChanged>>", lambda event: self.tab_changed())
        self.fill_favourites()
        self.tab_changed()
        self.tick()

    def on_favourites_tab(self):
        return self.notebook.index(self.notebook.select()) == 0

    def tab_changed(self):
        on_favourites = self.on_favourites_tab()
        for button in (self.edit_button, self.remove_button):
            button.state(["!disabled"] if on_favourites else ["disabled"])
        self.favourite_button.state(["disabled"] if on_favourites else ["!disabled"])

    def fill_favourites(self):
        self.favourites.delete(*self.favourites.get_children())
        ordered = sorted(self.app.servers.entries, key=lambda entry: -entry["last_used"])
        for entry in ordered:
            values = (entry["label"], entry["room"] or "(default)", entry["name"] or self.app.display_name())
            icon = self.app.server_icon(self.app.discovery.flags_for(entry["hash"]), bool(entry.get("server_password")))
            self.favourites.insert("", "end", iid=str(id(entry)), values=values, image=icon)
        children = self.favourites.get_children()
        if children:
            self.favourites.selection_set(children[0])
            self.favourites.focus(children[0])

    def tick(self):
        if not self.winfo_exists():
            return
        self.refresh_discovered()
        self.after(1000, self.tick)

    def refresh_discovered(self):
        if not self.winfo_exists():
            return
        selected = self.discovered.selection()
        now = time.time()
        query = self.discovered_filter.get().strip().lower()
        self.discovered.delete(*self.discovered.get_children())
        self.discovered_descriptions = {}
        for server in self.app.discovery.snapshot():
            language = server.get("language") or ""
            country = server.get("country") or ""
            description = server.get("description") or ""
            name = server["name"]
            haystack = " ".join([name, language, country, description]).lower()
            if query and query not in haystack:
                continue
            age = now - server["seen"]
            if age < 90:
                ago = f"{int(age)} s"
            elif age < 5400:
                ago = f"{int(age / 60)} min"
            else:
                ago = f"{int(age / 3600)} h"
            hash_hex = server["hash"].hex()
            icon = self.app.server_icon(server["flags"], False)
            if server.get("version") != PROTOCOL_VERSION:
                name += "  (different version)"
            locale = " / ".join(part for part in (language, country) if part)
            self.discovered.insert("", "end", iid=hash_hex, values=(name, hash_hex, locale, ago), image=icon)
            if description:
                self.discovered_descriptions[hash_hex] = description
        if selected and self.discovered.exists(selected[0]):
            self.discovered.selection_set(selected[0])

    def discovered_hover(self, event):
        row = self.discovered.identify_row(event.y)
        if row == self.discovered_tip_row:
            return
        self.hide_discovered_tip()
        self.discovered_tip_row = row
        description = self.discovered_descriptions.get(row)
        if not description:
            return
        self.discovered_tip = tk.Toplevel(self.discovered)
        self.discovered_tip.wm_overrideredirect(True)
        tk.Label(
            self.discovered_tip,
            text=description,
            justify="left",
            background=self.app.palette["field"],
            foreground=self.app.palette["fg"],
            relief="solid",
            borderwidth=1,
            padx=5,
            pady=3,
            wraplength=320,
        ).pack()
        self.discovered_tip.wm_geometry(
            f"+{self.discovered.winfo_rootx() + event.x + 14}+{self.discovered.winfo_rooty() + event.y + 16}"
        )

    def hide_discovered_tip(self):
        if self.discovered_tip is not None:
            self.discovered_tip.destroy()
            self.discovered_tip = None
        self.discovered_tip_row = None

    def selected_favourite(self):
        selected = self.favourites.selection()
        if not selected:
            return None
        for entry in self.app.servers.entries:
            if str(id(entry)) == selected[0]:
                return entry
        return None

    def add(self):
        result = ServerEditDialog(self, default_user_name=self.app.display_name()).show()
        if result:
            self.app.servers.add(**result)
            self.fill_favourites()

    def edit(self):
        entry = self.selected_favourite()
        if entry is None:
            return
        result = ServerEditDialog(self, entry).show()
        if result:
            self.app.servers.update(entry, **result)
            self.fill_favourites()

    def remove(self):
        entry = self.selected_favourite()
        if entry and messagebox.askyesno("Remove", f"Remove {entry['label']!r} from favorites?", parent=self):
            self.app.servers.remove(entry)
            self.fill_favourites()

    def favourite(self):
        selected = self.discovered.selection()
        if not selected:
            return
        server = self.app.discovery.servers.get(selected[0])
        if server is None:
            return
        prefilled = {"label": server["name"], "hash": selected[0]}
        result = ServerEditDialog(self, prefilled, default_user_name=self.app.display_name()).show()
        if result:
            self.app.servers.add(**result)
            self.fill_favourites()
            self.notebook.select(0)

    def connect_chat_only(self):
        if messagebox.askyesno("Chat-only", CHAT_ONLY_WARNING, parent=self):
            self.connect(chat_only=True)

    def connect(self, chat_only=False):
        if self.on_favourites_tab():
            entry = self.selected_favourite()
            if entry is None:
                return
            self.result = (entry, chat_only)
        else:
            selected = self.discovered.selection()
            if not selected:
                return
            entry = self.app.servers.find(selected[0])
            if entry is None:
                entry = {
                    "label": self.app.discovery.servers[selected[0]]["name"],
                    "hash": selected[0],
                    "name": "",
                    "room": "",
                    "password": "",
                    "server_password": "",
                    "last_used": 0,
                }
            self.result = (entry, chat_only)
        self.destroy()


class SettingsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app.root, "Settings")
        self.app = app
        settings = app.settings
        self.capturing = False
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        microphone_names, speaker_names = audio_devices()

        ### AUDIO INPUT ###
        input_tab = ttk.Frame(notebook, padding=10)
        notebook.add(input_tab, text="Audio Input")
        self.input_var = tk.StringVar(value=settings["input"] or DEFAULT_DEVICE)
        ttk.Label(input_tab, text="Device").grid(row=0, column=0, sticky="w", pady=3)
        input_box = ttk.Combobox(
            input_tab,
            textvariable=self.input_var,
            values=[DEFAULT_DEVICE] + microphone_names,
            state="readonly",
            width=44,
        )
        input_box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)

        transmit_frame = ttk.LabelFrame(input_tab, text="Transmission", padding=8)
        transmit_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.mode_var = tk.StringVar(value=settings["mode"])
        for text, mode in (("Push To Talk", "ptt"), ("Voice Activity", "vox"), ("Continuous", "open")):
            ttk.Radiobutton(transmit_frame, text=text, value=mode, variable=self.mode_var).pack(
                side="left", padx=(0, 12)
            )

        ptt_frame = ttk.LabelFrame(input_tab, text="Push To Talk", padding=8)
        ptt_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=4)
        self.key_spec = settings["ptt_key"]
        self.key_var = tk.StringVar(value=pretty_key(self.key_spec))
        ttk.Label(ptt_frame, text="Shortcut").grid(row=0, column=0, sticky="w")
        ttk.Entry(ptt_frame, textvariable=self.key_var, state="readonly", width=22).grid(
            row=0, column=1, sticky="w", padx=6
        )
        self.set_button = ttk.Button(ptt_frame, text="Set...", command=self.capture_key)
        self.set_button.grid(row=0, column=2, padx=2)
        ttk.Button(ptt_frame, text="Clear", command=lambda: self.set_key("")).grid(row=0, column=3, padx=2)
        self.toggle_var = tk.BooleanVar(value=settings["ptt_toggle"])
        ttk.Checkbutton(ptt_frame, text="Toggle: press once to talk, again to stop", variable=self.toggle_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        self.global_var = tk.BooleanVar(value=settings["ptt_global"] and pynput_keyboard is not None)
        global_box = ttk.Checkbutton(
            ptt_frame, text="System-wide shortcut (works while another window has focus)", variable=self.global_var
        )
        global_box.grid(row=2, column=0, columnspan=4, sticky="w")
        if pynput_keyboard is None:
            global_box.state(["disabled"])
            ttk.Label(ptt_frame, text="install python3-pynput for system-wide shortcuts", foreground="#666").grid(
                row=3, column=0, columnspan=4, sticky="w"
            )

        vad_frame = ttk.LabelFrame(input_tab, text="Voice Activity", padding=8)
        vad_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=4)
        self.vad_var = tk.DoubleVar(value=settings["vad_db"])
        self.hang_var = tk.DoubleVar(value=settings["vad_hang"])
        ttk.Label(vad_frame, text="Threshold").grid(row=0, column=0, sticky="w")
        ttk.Scale(vad_frame, from_=-80, to=0, variable=self.vad_var, orient="horizontal", length=220).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        self.vad_label = ttk.Label(vad_frame, width=8)
        self.vad_label.grid(row=0, column=2)
        ttk.Label(vad_frame, text="Hang time").grid(row=1, column=0, sticky="w")
        ttk.Scale(vad_frame, from_=0.1, to=2.0, variable=self.hang_var, orient="horizontal", length=220).grid(
            row=1, column=1, sticky="ew", padx=6
        )
        self.hang_label = ttk.Label(vad_frame, width=8)
        self.hang_label.grid(row=1, column=2)
        ttk.Label(vad_frame, text="Input level").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.meter = ttk.Progressbar(vad_frame, maximum=80, length=220)
        self.meter.grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))
        self.meter_label = ttk.Label(vad_frame, width=8)
        self.meter_label.grid(row=2, column=2, pady=(6, 0))
        vad_frame.columnconfigure(1, weight=1)
        input_tab.columnconfigure(1, weight=1)

        ### AUDIO OUTPUT ###
        output_tab = ttk.Frame(notebook, padding=10)
        notebook.add(output_tab, text="Audio Output")
        self.output_var = tk.StringVar(value=settings["output"] or DEFAULT_DEVICE)
        ttk.Label(output_tab, text="Device").grid(row=0, column=0, sticky="w", pady=3)
        output_box = ttk.Combobox(
            output_tab,
            textvariable=self.output_var,
            values=[DEFAULT_DEVICE] + speaker_names,
            state="readonly",
            width=44,
        )
        output_box.grid(row=0, column=1, sticky="ew", pady=3)

        self.low_latency_var = tk.BooleanVar(value=settings["low_latency"])
        ttk.Checkbutton(
            output_tab, text="Check for Android only (not applicable yet)", variable=self.low_latency_var
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)

        ttk.Label(output_tab, text="Jitter buffer").grid(row=2, column=0, sticky="w", pady=3)
        jitter_row = ttk.Frame(output_tab)
        jitter_row.grid(row=2, column=1, sticky="ew", pady=3)
        self.jitter_var = tk.IntVar(value=int(settings["jitter_ms"]))
        ttk.Scale(
            jitter_row,
            from_=0,
            to=5000,
            variable=self.jitter_var,
            orient="horizontal",
            length=220,
            command=lambda value: self.jitter_var.set(int(float(value))),
        ).pack(side="left")
        self.jitter_label = ttk.Label(jitter_row, width=8)
        self.jitter_label.pack(side="left", padx=6)
        ttk.Label(
            output_tab, text="The codec and frame size are set by the server for each room.", foreground="#666"
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.debug_stats_var = tk.BooleanVar(value=settings["debug_stats"])
        ttk.Checkbutton(
            output_tab,
            text="Display debug stats (bitrates and loss counters in the status bar)",
            variable=self.debug_stats_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
        output_tab.columnconfigure(1, weight=1)

        ### APPEARANCE ###
        appearance_tab = ttk.Frame(notebook, padding=10)
        notebook.add(appearance_tab, text="Appearance")
        ttk.Label(appearance_tab, text="Text size").grid(row=0, column=0, sticky="w", pady=3)

        self.font_size_var = tk.IntVar(value=int(settings["font_size"]))
        ttk.Spinbox(appearance_tab, from_=8, to=24, width=5, textvariable=self.font_size_var).grid(
            row=0, column=1, sticky="w", pady=3, padx=6
        )

        ttk.Label(appearance_tab, text="points").grid(
            row=0, column=2, sticky="w"
        )

        ttk.Checkbutton(appearance_tab, text="Dark mode", variable=app.dark_var, command=app.toggle_theme).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(12, 0)
        )

        ### SOUNDS ###

        sounds_tab = ttk.Frame(notebook, padding=10)
        notebook.add(sounds_tab, text="Sounds")
        self.sfx_var = tk.BooleanVar(value=settings["sfx"])
        ttk.Checkbutton(sounds_tab, text="Play sound effects", variable=self.sfx_var, command=self.sfx_toggled).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        self.sfx_vars = {}
        self.sfx_rows = []
        sound_rows = (
            ("sfx_join", "Someone joins the server", "join"),
            ("sfx_leave", "Someone leaves the server", "leave"),
            ("sfx_room", "Someone enters or leaves your room", "room_join"),
            ("sfx_disconnect", "Disconnected from the server", "disconnect"),
            ("sfx_ptt", "Push to talk on and off", "ptt_on"),
            ("sfx_mute", "Muting and deafening", "mute_on"),
        )
        for row, (key, text, preview_event) in enumerate(sound_rows, 1):
            variable = tk.BooleanVar(value=settings[key])
            self.sfx_vars[key] = variable
            checkbox = ttk.Checkbutton(sounds_tab, text=text, variable=variable)
            checkbox.grid(row=row, column=0, sticky="w", padx=(20, 12), pady=2)
            play_button = ttk.Button(
                sounds_tab, text="Play", width=6, command=lambda event=preview_event: app.sounds.play(event, force=True)
            )
            play_button.grid(row=row, column=1, pady=2)
            self.sfx_rows.append((checkbox, play_button))
        ttk.Label(
            sounds_tab,
            text="test placeholder",
            foreground="#666",
        ).grid(row=len(sound_rows) + 1, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.sfx_toggled()

        ### USER ###
        user_tab = ttk.Frame(notebook, padding=10)
        notebook.add(user_tab, text="User")
        self.name_var = tk.StringVar(value=settings["name"] or app.display_name())
        ttk.Label(user_tab, text="Display name").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(user_tab, textvariable=self.name_var, width=32).grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(user_tab, text="Identity hash").grid(row=1, column=0, sticky="w", pady=3)
        identity_var = tk.StringVar(value=app.identity.hash.hex())
        ttk.Entry(user_tab, textvariable=identity_var, state="readonly", width=36).grid(
            row=1, column=1, sticky="w", pady=3
        )
        ttk.Button(user_tab, text="Copy", command=lambda: self.copy_text(identity_var.get())).grid(
            row=1, column=2, padx=4
        )
        ttk.Label(
            user_tab,
            text="Give this hash to a server operator to be put on an allow list.\n"
            "A name change takes effect on the next connection.",
            foreground="#666",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))



        ### BUTTONS ###
        buttons = ttk.Frame(self, padding=(10, 4, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="OK", command=self.ok).pack(side="right", padx=6)
        ttk.Button(buttons, text="Apply", command=self.apply).pack(side="right")

        self.bind("<KeyPress>", self.tk_key, add="+")
        self.tick()

    def copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def sfx_toggled(self):
        state = ["!disabled"] if self.sfx_var.get() else ["disabled"]
        for checkbox, play_button in self.sfx_rows:
            checkbox.state(state)
            play_button.state(state)

    def set_key(self, spec):
        self.key_spec = spec
        self.key_var.set(pretty_key(spec))
        self.capturing = False
        self.set_button.config(text="Set...")
        self.app.hotkeys.capture = False

    def capture_key(self):
        self.capturing = True
        self.key_var.set("press a key or mouse button...")
        self.set_button.config(text="waiting")
        hotkeys = self.app.hotkeys
        while not hotkeys.captured_keys.empty():
            hotkeys.captured_keys.get_nowait()  # nothing stale from an earlier attempt
        if hotkeys.active:
            hotkeys.capture = True
        self.focus_set()

    def tk_key(self, event):
        if self.capturing and not self.app.hotkeys.active:
            self.set_key(tk_key_name(event.keysym))
            return "break"

    def tick(self):
        if not self.winfo_exists():
            return
        self.vad_label.config(text=f"{self.vad_var.get():.0f} dB")
        self.hang_label.config(text=f"{self.hang_var.get():.1f} s")
        self.jitter_label.config(text=f"{self.jitter_var.get()} ms")
        if self.app.client:
            level = self.app.client.level
        else:
            level = -120.0
        self.meter["value"] = max(0, level + 80)
        self.meter_label.config(text=f"{level:.0f} dB" if level > -119 else "no input")
        if self.capturing and self.app.hotkeys.active:
            try:
                self.set_key(self.app.hotkeys.captured_keys.get_nowait())
            except queue.Empty:
                pass
        self.after(100, self.tick)

    def apply(self):
        settings = self.app.settings
        input_name = self.input_var.get()
        output_name = self.output_var.get()
        settings.update(
            input=None if input_name == DEFAULT_DEVICE else input_name,
            output=None if output_name == DEFAULT_DEVICE else output_name,
            low_latency=self.low_latency_var.get(),
            mode=self.mode_var.get(),
            ptt_key=self.key_spec,
            ptt_toggle=self.toggle_var.get(),
            ptt_global=self.global_var.get(),
            vad_db=round(self.vad_var.get(), 1),
            vad_hang=round(self.hang_var.get(), 2),
            name=self.name_var.get().strip(),
            jitter_ms=int(self.jitter_var.get()),
            debug_stats=self.debug_stats_var.get(),
            font_size=max(8, min(24, int(self.font_size_var.get() or 10))),
            sfx=self.sfx_var.get(),
            **{key: variable.get() for key, variable in self.sfx_vars.items()},
        )
        settings.save()
        self.app.apply_settings()

    def ok(self):
        self.apply()
        self.destroy()

    def cancel(self):
        self.app.hotkeys.capture = False
        super().cancel()


class VolumeDialog(tk.Toplevel):
    def __init__(self, app, user):
        super().__init__(app.root)
        self.app = app
        self.user = user
        self.title(f"Local Volume: {user.name}")
        self.transient(app.root)
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        current_gain = app.client.gains.get(user.sid, 0.0) if app.client else 0.0
        self.gain_var = tk.DoubleVar(value=current_gain)
        ttk.Scale(
            frame,
            from_=-30,
            to=20,
            variable=self.gain_var,
            orient="horizontal",
            length=260,
            command=lambda value: self.apply(),
        ).grid(row=0, column=0, columnspan=2, sticky="ew")
        self.gain_label = ttk.Label(frame, width=8)
        self.gain_label.grid(row=0, column=2, padx=6)

        ttk.Button(frame, text="Reset", command=self.reset).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Button(frame, text="Close", command=self.destroy).grid(row=1, column=2, sticky="e", pady=(10, 0))
        self.bind("<Escape>", lambda event: self.destroy())
        self.apply()
        self.geometry(f"+{app.root.winfo_rootx() + 80}+{app.root.winfo_rooty() + 80}")

    def reset(self):
        self.gain_var.set(0.0)
        self.apply()

    def apply(self):
        decibels = round(self.gain_var.get())
        self.gain_label.config(text=f"{decibels:+d} dB")
        self.app.set_user_gain(self.user, decibels)


class PokeWindow(tk.Toplevel):
    def __init__(self, app, user, text):
        super().__init__(app.root)
        self.title("Poke")
        self.attributes("-topmost", True)
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        sender = user.name if user else "Someone"
        ttk.Label(frame, text=f"{sender} poked you", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text=text, wraplength=320).pack(anchor="w", pady=(6, 10))
        ttk.Button(frame, text="OK", command=self.destroy).pack(anchor="e")
        self.bind("<Escape>", lambda event: self.destroy())
        self.bind("<Return>", lambda event: self.destroy())
        self.geometry(f"+{app.root.winfo_rootx() + 120}+{app.root.winfo_rooty() + 120}")

        app.root.bell()
        self.lift()
        self.focus_force()
        self.after(10000, self.close)

    def close(self):
        if self.winfo_exists():
            self.destroy()


class ServerInfoDialog(tk.Toplevel):
    def __init__(self, app, client):
        super().__init__(app.root)
        self.app = app
        self.title("Server Information")
        self.transient(app.root)
        self.minsize(640, 420)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        heading_font = ("TkDefaultFont", 11, "bold")
        ttk.Label(frame, text=client.server_name, font=heading_font).pack(anchor="w")
        if client.hops is not None:
            plural = "s" if client.hops != 1 else ""
            path = f"{client.hops} hop{plural} away"
        else:
            path = "path unknown"
        ttk.Label(frame, text=f"Address {client.server_hash.hex()}, {path}, {len(client.users)} users").pack(anchor="w")
        if client.motd:
            ttk.Label(frame, text=client.motd, foreground=app.palette["muted"]).pack(anchor="w", pady=(2, 0))

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True, pady=(10, 6))
        columns = ("room", "codec", "access", "users", "dialin", "description")
        table = ttk.Treeview(table_frame, columns=columns, show="headings", height=8)
        for column, heading, width in (
            ("room", "Room", 110),
            ("codec", "Codec", 190),
            ("access", "Access", 120),
            ("users", "Users", 50),
            ("dialin", "Dial-in number", 230),
            ("description", "Description", 220),
        ):
            table.heading(column, text=heading)
            anchor = "center" if column in ("users", "dialin") else "w"
            table.column(column, width=width, anchor=anchor, stretch=(column == "description"))
        self.dialin_numbers = {}
        for channel in sorted(client.channels.values(), key=lambda entry: entry.id):
            user_count = sum(1 for user in client.users.values() if user.room == channel.id)
            dialin = channel.dialin_number or ""
            values = (
                channel.name,
                describe(channel.profile),
                channel.requirements(),
                user_count,
                dialin,
                channel.description,
            )
            table.insert("", "end", iid=str(channel.id), values=values)
            if channel.dialin_number:
                self.dialin_numbers[str(channel.id)] = channel.dialin_number
        self.table = table
        table.bind("<Button-3>", self.room_menu)
        table.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(table_frame, command=table.yview).pack(side="right", fill="y")

        packets, wire_bytes, _ = client.tx_totals()
        ttk.Label(
            frame,
            text=f"Sent {packets} packets, {wire_bytes:,} bytes. Received {client.rx_packets} packets, {client.rx_bytes:,} bytes.",
        ).pack(anchor="w")
        playout = client.playout
        if playout:
            audio_text = (
                f"Audio: {playout.frames_out} frames played, {playout.lost} lost on the way, {playout.concealed} concealed, "
                f"{playout.dropped + client.dropped} dropped (buffer overrun), {client.bad_frames} bad. "
                f"Jitter buffer now {playout.depth_ms} ms (floor {client.cfg.jitter_ms} ms, grown {playout.grew} times)."
            )
        else:
            audio_text = "Audio: not running."
        ttk.Label(frame, text=audio_text, wraplength=600).pack(anchor="w", pady=(2, 0))
        ttk.Label(frame, text=f"Your identity: {app.identity.hash.hex()}", font=("TkFixedFont", 9)).pack(
            anchor="w", pady=(6, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Copy address", command=lambda: app.copy_to_clipboard(client.server_hash.hex())).pack(
            side="left"
        )
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda event: self.destroy())
        self.geometry(f"+{app.root.winfo_rootx() + 40}+{app.root.winfo_rooty() + 40}")

    def room_menu(self, event):
        number = self.dialin_numbers.get(self.table.identify_row(event.y))
        if not number:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"Dial-in number: {number}", state="disabled")
        menu.add_command(label="Copy dial-in number", command=lambda: self.app.copy_to_clipboard(number))
        menu.tk_popup(event.x_root, event.y_root)


class AboutDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.title("About")
        self.transient(app.root)
        self.resizable(False, False)
        palette = app.palette
        self.configure(bg=palette["bg"])

        mark = tk.Frame(self, bg="black", padx=24, pady=16)  # the word mark: white on black in either theme
        mark.pack(fill="x")
        try:
            self.image = tk.PhotoImage(file=ART_FILE)
            tk.Label(mark, image=self.image, bg="black").pack()
        except tk.TclError:
            self.image = None
        tk.Label(mark, text="partyline", font=("TkFixedFont", 30), fg="white", bg="black").pack(pady=(8, 0))
        tk.Label(mark, text=f"version {APP_VERSION}, protocol {PROTOCOL_VERSION}", fg="#9a9a9a", bg="black").pack()

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, text="A group voice application for Reticulum, built on LXST"
        ).pack()
        ttk.Label(frame, text=f"Your identity: {app.identity.hash.hex()}", font=("TkFixedFont", 9)).pack(pady=(10, 0))
        
        ttk.Label(
            frame, text="", foreground=palette["muted"]
        ).pack(pady=(10, 0))

        ttk.Button(frame, text="Close", command=self.destroy).pack(pady=(12, 0))
        self.bind("<Escape>", lambda event: self.destroy())
        self.geometry(f"+{app.root.winfo_rootx() + 60}+{app.root.winfo_rooty() + 40}")




### MAIN ###
class App:
    RECONNECT_DELAYS = (3, 5, 10, 20, 30)

    def __init__(self, root, identity, settings, servers, discovery):
        self.root = root
        self.identity = identity
        self.settings = settings
        self.servers = servers
        self.discovery = discovery
        self.client = None
        self.server_entry = None
        self.passwords = {}  

        self.hotkeys = Hotkeys()
        self.sounds = SoundPlayer(settings)
        self.key_down = False
        self.release_job = None
        self.tree_dirty = True
        self.icon_state = {}

        self.wanted = False  
        self.chat_only = False
        self.reconnect_job = None
        self.reconnect_attempt = 0
        self.reconnect_at = 0.0
        self.last_join = (None, None)  # (room name, password)

        self.palette = PALETTES.get(settings["theme"], PALETTES["light"])
        self.menus = []

        root.title(APP_TITLE)
        root.minsize(640, 420)
        if settings["window"]:
            try:
                root.geometry(settings["window"])
            except tk.TclError:
                pass

        self.icons = {}
        for kind in ("server", "server_locked", "channel", "channel_locked", "phone"):
            self.icons[kind] = load_icon(kind)
        for kind in USER_ICON_KINDS:
            self.icons[kind] = load_icon(kind)
            self.icons[kind + "+op"] = load_icon(kind, operator=True)

        self.build_menu()
        self.build_toolbar()
        self.build_body()
        self.build_statusbar()
        self.apply_theme()
        self.apply_settings()
        self.log("Welcome to Partyline. Server > Connect... to pick a server.", "sys")

        root.after(REFRESH_MS, self.refresh)
        root.after(HOTKEY_MS, self.poll_hotkeys)

    def display_name(self):
        return self.settings["name"] or default_name(self.identity)

    def server_icon(self, announce_flags, has_saved_password):
        protected = announce_flags & (ANNOUNCE_PASSWORD | ANNOUNCE_ALLOWLIST) or has_saved_password
        return self.icons["server_locked" if protected else "server"]

    def copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)


### UI CONSTRUCTION ###
    def build_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        server_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Server", menu=server_menu)
        server_menu.add_command(label="Connect...", accelerator="Ctrl+O", command=self.connect_dialog)
        server_menu.add_command(label="Disconnect", accelerator="Ctrl+D", command=self.disconnect)
        server_menu.add_command(label="Server Information...", command=self.server_info)
        server_menu.add_separator()
        server_menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.quit)

        self.mute_var = tk.BooleanVar(value=False)
        self.deaf_var = tk.BooleanVar(value=False)
        self_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Self", menu=self_menu)
        self_menu.add_checkbutton(label="Mute Self", variable=self.mute_var, command=self.toggle_mute)
        self_menu.add_checkbutton(label="Deafen Self", variable=self.deaf_var, command=self.toggle_deaf)

        configure_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Configure", menu=configure_menu)
        configure_menu.add_command(label="Settings...", command=lambda: SettingsDialog(self).show())
        self.dark_var = tk.BooleanVar(value=self.settings["theme"] == "dark")
        configure_menu.add_checkbutton(label="Dark Mode", variable=self.dark_var, command=self.toggle_theme)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=lambda: AboutDialog(self))

        self.menus = [menubar, server_menu, self_menu, configure_menu, help_menu]
        self.root.bind("<Control-o>", lambda event: self.connect_dialog())
        self.root.bind("<Control-d>", lambda event: self.disconnect())
        self.root.bind("<Control-q>", lambda event: self.quit())

    def build_toolbar(self):
        toolbar = ttk.Frame(self.root, padding=(4, 3))
        toolbar.pack(fill="x")
        self.connect_button = ttk.Button(toolbar, text="Connect", command=self.connect_dialog)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(toolbar, text="Disconnect", command=self.disconnect)
        self.disconnect_button.pack(side="left", padx=(2, 0))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        for kind in ("toolbar_mute", "toolbar_deafen", "toolbar_mute_on", "toolbar_deafen_on"):
            self.icons[kind] = load_icon(kind)
        # the grey icon normally, the white one on the red pressed button
        self.mute_button = ttk.Checkbutton(
            toolbar,
            text="Mute",
            image=(self.icons["toolbar_mute"], "selected", self.icons["toolbar_mute_on"]),
            compound="left",
            variable=self.mute_var,
            command=self.toggle_mute,
            style="Danger.Toolbutton",
        )
        self.mute_button.pack(side="left")
        self.deafen_button = ttk.Checkbutton(
            toolbar,
            text="Deafen",
            image=(self.icons["toolbar_deafen"], "selected", self.icons["toolbar_deafen_on"]),
            compound="left",
            variable=self.deaf_var,
            command=self.toggle_deaf,
            style="Danger.Toolbutton",
        )
        self.deafen_button.pack(side="left", padx=(2, 0))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Settings", command=lambda: SettingsDialog(self).show()).pack(side="left")

        self.ptt_button = tk.Button(toolbar, text="Push to talk", width=18, relief="raised", state="disabled")
        self.ptt_button.pack(side="right")
        self.ptt_button.bind("<ButtonPress-1>", lambda event: self.ptt_press())
        self.ptt_button.bind("<ButtonRelease-1>", lambda event: self.ptt_release())

    def build_body(self):
        pane = ttk.PanedWindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=4, pady=2)
        self.pane = pane

        chat_pane = ttk.Frame(pane)
        tree_pane = ttk.Frame(pane)

        pane.add(chat_pane, weight=1)
        pane.add(tree_pane, weight=2)
        self.root.after(50, self.place_sash)

        self.tree = ttk.Treeview(tree_pane, show="tree", selectmode="browse")
        self.tree.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(tree_pane, command=self.tree.yview).pack(side="right", fill="y")
        tree_font = ttk.Style().lookup("Treeview", "font") or "TkDefaultFont"
        if isinstance(tree_font, str):
            self.tree.tag_configure("self", font=(tree_font, 10, "bold"))
        else:
            self.tree.tag_configure("self", font=tree_font)
        self.tree.bind("<Double-1>", self.tree_double)
        self.tree.bind("<Return>", self.tree_double)
        self.tree.bind("<Button-3>", self.tree_menu)
        self.tree.bind("<<TreeviewSelect>>", lambda event: self.tree_selected())

        self.chat = tk.Text(chat_pane, wrap="word", state="disabled", height=10, font=("TkDefaultFont", 10))
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("name", font=("TkDefaultFont", 10, "bold"))
        self.chat.tag_configure("me", font=("TkDefaultFont", 10, "bold"))

        entry_row = ttk.Frame(chat_pane)
        entry_row.pack(fill="x", pady=(3, 0))
        self.entry = ttk.Entry(entry_row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda event: self.send_text())
        ttk.Button(entry_row, text="Send", command=self.send_text).pack(side="left", padx=(3, 0))

    def place_sash(self):
        width = self.pane.winfo_width()
        if width < 50:
            self.root.after(50, self.place_sash)
            return
        position = self.settings.get("sash") or int(width * 0.3)
        self.pane.sashpos(0, max(150, min(position, width - 200)))

    def build_statusbar(self):
        status_bar = ttk.Frame(self.root, padding=(6, 2))
        status_bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side="left")
        self.tx_label = tk.Label(status_bar, text=" TX ", width=5, relief="sunken")
        self.tx_label.pack(side="right", padx=(6, 0))
        self.meter = ttk.Progressbar(status_bar, maximum=80, length=90)
        self.meter.pack(side="right")
        self.rate_var = tk.StringVar(value="")
        ttk.Label(status_bar, textvariable=self.rate_var).pack(side="right", padx=8)






### THEME ###
    def toggle_theme(self):
        self.settings["theme"] = "dark" if self.dark_var.get() else "light"
        self.settings.save()
        self.apply_theme()

    def apply_theme(self):
        palette = PALETTES.get(self.settings["theme"], PALETTES["light"])
        self.palette = palette
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            ".",
            background=palette["bg"],
            foreground=palette["fg"],
            fieldbackground=palette["field"],
            bordercolor=palette["border"],
            lightcolor=palette["bg"],
            darkcolor=palette["bg"],
            troughcolor=palette["field"],
            selectbackground=palette["select"],
            selectforeground=palette["selfg"],
            insertcolor=palette["fg"],
            arrowcolor=palette["fg"],
        )

        style.configure("TButton", background=palette["btn"])
        style.map(
            "TButton",
            background=[("active", palette["btn_active"]), ("disabled", palette["bg"])],
            foreground=[("disabled", palette["muted"])],
        )
        style.configure("Toolbutton", background=palette["bg"])
        style.map(
            "Toolbutton",
            background=[("selected", palette["select"]), ("active", palette["btn_active"])],
            foreground=[("selected", palette["selfg"])],
        )
        style.configure("Danger.Toolbutton", background=palette["bg"])  # mute and deafen: red when on
        style.map(
            "Danger.Toolbutton",
            background=[("selected", "#d9534f"), ("active", palette["btn_active"])],
            foreground=[("selected", "#ffffff"), ("disabled", palette["muted"])],
        )
        style.configure(
            "Treeview", background=palette["field"], fieldbackground=palette["field"], foreground=palette["fg"]
        )
        style.map("Treeview", background=[("selected", palette["select"])], foreground=[("selected", palette["selfg"])])
        style.configure("Treeview.Heading", background=palette["btn"], foreground=palette["fg"])
        style.map("Treeview.Heading", background=[("active", palette["btn_active"])])

        style.configure("TNotebook.Tab", background=palette["btn"], foreground=palette["fg"])
        style.map("TNotebook.Tab", background=[("selected", palette["bg"])])
        style.configure("TEntry", fieldbackground=palette["field"], foreground=palette["fg"])
        style.configure(
            "TCombobox", fieldbackground=palette["field"], foreground=palette["fg"], background=palette["btn"]
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["field"])],
            foreground=[("readonly", palette["fg"])],
            selectbackground=[("readonly", palette["field"])],
            selectforeground=[("readonly", palette["fg"])],
        )

        for widget_class in ("TCheckbutton", "TRadiobutton"):
            style.configure(
                widget_class,
                indicatorsize=16,
                indicatormargin=(2, 2, 6, 2),
                indicatorbackground=palette["field"],
                indicatorforeground=palette["fg"],
                upperbordercolor=palette["border"],
                lowerbordercolor=palette["border"],
            )
            style.map(
                widget_class,
                background=[("active", palette["bg"])],
                indicatorbackground=[("selected", palette["select"]), ("pressed", palette["btn_active"])],
                indicatorforeground=[("selected", palette["selfg"])],
                upperbordercolor=[("selected", palette["select"])],
                lowerbordercolor=[("selected", palette["select"])],
            )

        style.configure("TProgressbar", background=palette["select"], troughcolor=palette["field"])
        style.configure("TScale", troughcolor=palette["field"], background=palette["btn"])
        style.configure("TLabelframe.Label", background=palette["bg"], foreground=palette["fg"])
        style.configure("TScrollbar", background=palette["btn"], troughcolor=palette["bg"], arrowcolor=palette["fg"])

        self.root.configure(bg=palette["bg"])
        option_defaults = (
            ("*background", palette["bg"]),
            ("*foreground", palette["fg"]),
            ("*Menu.background", palette["btn"]),
            ("*Menu.foreground", palette["fg"]),
            ("*Menu.activeBackground", palette["select"]),
            ("*Menu.activeForeground", palette["selfg"]),
            ("*Entry.background", palette["field"]),
            ("*Entry.foreground", palette["fg"]),
            ("*Entry.insertBackground", palette["fg"]),
            ("*Text.background", palette["field"]),
            ("*Listbox.background", palette["field"]),
            ("*Listbox.foreground", palette["fg"]),
            ("*TCombobox*Listbox.background", palette["field"]),
            ("*TCombobox*Listbox.foreground", palette["fg"]),
            ("*TCombobox*Listbox.selectBackground", palette["select"]),
        )
        for option, value in option_defaults:
            self.root.option_add(option, value)

        for menu in self.menus:
            menu.configure(
                bg=palette["btn"],
                fg=palette["fg"],
                activebackground=palette["select"],
                activeforeground=palette["selfg"],
                borderwidth=0,
            )

        self.chat.configure(
            bg=palette["field"],
            fg=palette["fg"],
            insertbackground=palette["fg"],
            selectbackground=palette["select"],
            selectforeground=palette["selfg"],
            highlightthickness=0,
        )
        for tag, color in (
            ("time", palette["muted"]),
            ("sys", palette["sys"]),
            ("err", palette["err"]),
            ("me", palette["me"]),
        ):
            self.chat.tag_configure(tag, foreground=color)

        self.ptt_button.configure(
            bg=palette["btn"],
            fg=palette["fg"],
            activebackground=palette["btn_active"],
            activeforeground=palette["fg"],
            highlightbackground=palette["bg"],
        )
        self.tx_label.configure(bg=palette["tx_off"], fg=palette["fg"])




### SETTINGS ####
    def apply_fonts(self):
        size = int(self.settings.get("font_size") or 10)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkFixedFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        self.chat.configure(font=("TkDefaultFont", size))
        self.chat.tag_configure("name", font=("TkDefaultFont", size, "bold"))
        self.chat.tag_configure("me", font=("TkDefaultFont", size, "bold"))
        style = ttk.Style(self.root)
        style.configure("Treeview", font=("TkDefaultFont", size), rowheight=int(size * 2.2) + 2)
        self.tree.tag_configure("self", font=("TkDefaultFont", size, "bold"))

    def apply_settings(self):
        settings = self.settings
        self.apply_fonts()
        self.hotkeys.spec = settings["ptt_key"] or None
        if settings["ptt_global"] and settings["ptt_key"]:
            self.hotkeys.start()
        else:
            self.hotkeys.stop()

        self.root.unbind("<KeyPress>")
        self.root.unbind("<KeyRelease>")
        if settings["ptt_key"] and not settings["ptt_key"].startswith("mouse:"):
            self.root.bind("<KeyPress>", self.tk_key_press)
            self.root.bind("<KeyRelease>", self.tk_key_release)

        push_to_talk = settings["mode"] == "ptt"
        button_text = "Push to talk"
        if settings["ptt_key"]:
            button_text += f"  [{pretty_key(settings['ptt_key'])}]"

        client = self.client
        if client and not client.can_speak_here:
            self.ptt_button.config(state="disabled", text="Listen only")
        else:
            self.ptt_button.config(state="normal" if push_to_talk else "disabled", text=button_text)

        if client:
            current_devices = (client.cfg.input, client.cfg.output, client.cfg.low_latency)
            wanted_devices = (settings["input"], settings["output"], settings["low_latency"])
            if current_devices != wanted_devices:
                client.set_devices(settings["input"], settings["output"], settings["low_latency"])
            client.set_vad(settings["vad_db"], settings["vad_hang"])
            client.set_mode(settings["mode"])
            client.set_jitter(settings["jitter_ms"])
            if not push_to_talk:
                client.set_transmit(False)

    def set_chat_only_widgets(self, chat_only):
        state = ["disabled"] if chat_only else ["!disabled"]
        self.mute_button.state(state)
        self.deafen_button.state(state)
        if chat_only:
            self.ptt_button.config(state="disabled", text="Chat-only")
        else:
            self.apply_settings()

    def client_config(self):
        settings = self.settings
        return Config(
            mode=settings["mode"],
            vad_db=settings["vad_db"],
            vad_hang=settings["vad_hang"],
            input=settings["input"],
            output=settings["output"],
            low_latency=settings["low_latency"],
            jitter_ms=settings["jitter_ms"],
        )



    def connect_dialog(self):
        result = ConnectDialog(self).show()
        if result:
            entry, chat_only = result
            self.connect_to(entry, chat_only=chat_only)

    def connect_to(self, entry, retry=False, chat_only=False):
        if self.client and not retry:
            self.disconnect()
        if not retry:
            self.chat_only = chat_only
        try:
            destination_hash = parse_hash(entry["hash"])
        except ValueError as error:
            self.log(f"bad server address: {error}", "err")
            return

        self.server_entry = entry
        self.wanted = True
        if not retry:
            self.reconnect_attempt = 0
            self.last_join = (entry.get("room") or None, entry.get("password") or None)
            if entry.get("last_used") is not None and entry in self.servers.entries:
                self.servers.touch(entry)

        room, password = self.last_join
        server_password = entry.get("server_password") or None
        needs_password = self.discovery.flags_for(entry["hash"]) & ANNOUNCE_PASSWORD
        if needs_password and not server_password and not retry:
            server_password = simpledialog.askstring(
                "Server password", f"{entry['label']} asks for a password:", show="*", parent=self.root
            )
            if server_password is None:
                self.wanted = False
                return
            entry["server_password"] = server_password  # kept for reconnects; saved only if the entry is a favourite
            if entry in self.servers.entries:
                self.servers.save()

        name = entry.get("name") or self.display_name()
        config = self.client_config()
        config.text_only = self.chat_only
        self.client = Client(config, self.identity, name)
        self.client.muted = self.mute_var.get() or self.chat_only
        self.client.deaf = self.deaf_var.get() or self.chat_only
        if self.chat_only:
            self.mute_var.set(True)
            self.deaf_var.set(True)
        self.set_chat_only_widgets(self.chat_only)

        verb = "Reconnecting" if retry else "Connecting"
        if self.chat_only:
            verb += " chat-only"
        self.log(f"{verb} to {entry['label']} ({entry['hash'][:12]}...) as {name}", "sys")
        self.client.connect(destination_hash, room=room, password=password, server_password=server_password)
        self.tree_dirty = True

    def disconnect(self):
        self.wanted = False
        self.cancel_reconnect()
        if self.client:
            self.client.disconnect()
            self.log("Disconnected.", "sys")
        self.set_chat_only_widgets(False)
        self.client = None
        self.server_entry = None
        self.key_down = False
        self.tree_dirty = True
        self.icon_state.clear()

    def cancel_reconnect(self):
        if self.reconnect_job:
            self.root.after_cancel(self.reconnect_job)
            self.reconnect_job = None

    def schedule_reconnect(self):
        if not self.wanted or not self.server_entry or self.reconnect_job:
            return
        delay = self.RECONNECT_DELAYS[min(self.reconnect_attempt, len(self.RECONNECT_DELAYS) - 1)]
        self.reconnect_at = time.time() + delay
        self.log(
            f"Attempting to reconnect in {delay} s (attempt {self.reconnect_attempt + 1}). Disconnect to stop.", "err"
        )
        self.reconnect_job = self.root.after(int(delay * 1000), self.do_reconnect)


    def do_reconnect(self):
        self.reconnect_job = None
        if not self.wanted or not self.server_entry:
            return
        self.reconnect_attempt += 1
        self.connect_to(self.server_entry, retry=True)

    def quit(self):
        try:
            self.settings["window"] = self.root.geometry()
            self.settings["sash"] = self.pane.sashpos(0)
            self.settings.save()
        except Exception:
            pass
        self.disconnect()
        self.hotkeys.stop()
        self.root.destroy()

    def server_info(self):
        client = self.client
        if not client or not client.connected:
            messagebox.showinfo("Server Information", "Not connected.", parent=self.root)
            return
        ServerInfoDialog(self, client)


    def toggle_mute(self):
        if self.deaf_var.get() and not self.mute_var.get():
            self.deaf_var.set(False)
        if self.client:
            self.client.set_deaf(self.deaf_var.get())
            self.client.set_muted(self.mute_var.get())
        self.log("Muted." if self.mute_var.get() else "Unmuted.", "sys")
        self.sounds.play("mute_on" if self.mute_var.get() else "mute_off")

    def toggle_deaf(self):
        if self.deaf_var.get():
            self.mute_var.set(True)
        if self.client:
            self.client.set_deaf(self.deaf_var.get())
        self.log("Deafened (and muted)." if self.deaf_var.get() else "Undeafened.", "sys")
        self.sounds.play("deafen_on" if self.deaf_var.get() else "deafen_off")



    def ptt_press(self):
        if self.release_job:
            self.root.after_cancel(self.release_job)
            self.release_job = None
        if self.key_down:
            return
        self.key_down = True
        if self.settings["ptt_toggle"]:
            currently_down = bool(self.client and self.client.ptt_down)
            self.set_transmit(not currently_down)
        else:
            self.set_transmit(True)

    def ptt_release(self):
        if self.release_job:
            self.root.after_cancel(self.release_job)
        self.release_job = self.root.after(RELEASE_MS, self._released)

    def _released(self):
        self.release_job = None
        self.key_down = False
        if not self.settings["ptt_toggle"]:
            self.set_transmit(False)

    def set_transmit(self, down):
        if self.settings["mode"] != "ptt":
            return
        was_down = bool(self.client and self.client.ptt_down)
        if self.client:
            self.client.set_transmit(down)
        self.ptt_button.config(relief="sunken" if down else "raised")
        if self.client and down != was_down:
            self.sounds.play("ptt_on" if down else "ptt_off")

    def typing_in_chat(self):
        return self.root.focus_get() is self.entry

    def tk_key_press(self, event):
        if self.hotkeys.active or self.typing_in_chat():
            return  # the global listener handles it, or the user is typing a message
        if tk_key_name(event.keysym) == self.settings["ptt_key"]:
            self.ptt_press()
            return "break"

    def tk_key_release(self, event):
        if self.hotkeys.active or self.typing_in_chat():
            return
        if tk_key_name(event.keysym) == self.settings["ptt_key"]:
            self.ptt_release()
            return "break"

    def poll_hotkeys(self):
        try:
            while True:
                kind, name = self.hotkeys.ptt_events.get_nowait()
                if kind == "down":
                    self.ptt_press()
                elif kind == "up":
                    self.ptt_release()
        except queue.Empty:
            pass
        self.root.after(HOTKEY_MS, self.poll_hotkeys)





    def tree_double(self, event=None):
        item = self.tree.focus()
        if item.startswith("ch"):
            self.join_room(int(item[2:]))

    def tree_selected(self):
        item = self.tree.focus()
        client = self.client
        if not client:
            return
        if item.startswith("ch"):
            channel = client.channels.get(int(item[2:]))
            if channel:
                text = f"{channel.name}: {describe(channel.profile)}, {channel.requirements()}"
                if channel.dialin_number:
                    text += f", dial-in {channel.dialin_number}"
                if channel.description:
                    text += f". {channel.description}"
                self.status_var.set(text)
        elif item.startswith("u"):
            user = client.users.get(int(item[1:]))
            if user:
                self.status_var.set(self.describe_user(user))

    def describe_user(self, user):
        client = self.client
        if client.hops is not None:
            plural = "s" if client.hops != 1 else ""
            mine = f"you: {client.hops} hop{plural}"
        else:
            mine = "your path unknown"
        flags = []
        if user.operator:
            flags.append("operator")
        if user.text_only:
            flags.append("text only")
        if user.server_muted:
            flags.append("muted by an operator")
        flag_text = f" [{', '.join(flags)}]" if flags else ""
        identity_text = f"identity {user.identity}" if user.identity else "anonymous"
        return f"{user.name}{flag_text}: {user.path_info()} ({mine}); {identity_text}"

    def tree_menu(self, event):
        item = self.tree.identify_row(event.y)
        client = self.client
        if not item or not client:
            return
        self.tree.selection_set(item)
        self.tree.focus(item)
        menu = tk.Menu(self.root, tearoff=0)
        if item.startswith("ch"):
            self.fill_room_menu(menu, client.channels.get(int(item[2:])))
        elif item.startswith("u"):
            self.fill_user_menu(menu, client.users.get(int(item[1:])))
        else:
            return
        menu.tk_popup(event.x_root, event.y_root)

    def fill_room_menu(self, menu, channel):
        if channel is None:
            return
        menu.add_command(
            label=f"{channel.name}: {describe(channel.profile)}, {channel.requirements()}", state="disabled"
        )
        menu.add_separator()
        menu.add_command(label="Join Room", command=lambda: self.join_room(channel.id))
        if channel.dialin_number:
            menu.add_separator()
            menu.add_command(label=f"Dial-in number: {channel.dialin_number}", state="disabled")
            menu.add_command(label="Copy dial-in number", command=lambda: self.copy_to_clipboard(channel.dialin_number))

    def fill_user_menu(self, menu, user):
        client = self.client
        if user is None:
            return
        if user.sid == client.my_sid:
            menu.add_checkbutton(label="Mute Self", variable=self.mute_var, command=self.toggle_mute)
            menu.add_checkbutton(label="Deafen Self", variable=self.deaf_var, command=self.toggle_deaf)
            return

        header = f"{user.name}: {user.path_info()}"
        if user.operator:
            header += " (operator)"
        menu.add_command(label=header, state="disabled")
        menu.add_separator()

        gain = client.gains.get(user.sid, 0)
        muted_here = user.sid in client.local_muted
        menu.add_command(
            label="Unmute Locally" if muted_here else "Mute Locally",
            command=lambda: self.set_local_mute(user, not muted_here),
        )
        volume_label = "Local Volume..."
        if gain:
            volume_label += f"  ({gain:+.0f} dB)"
        menu.add_command(label=volume_label, command=lambda: VolumeDialog(self, user))
        menu.add_command(label="Poke...", command=lambda: self.poke(user))
        menu.add_separator()
        menu.add_command(
            label="Copy identity hash",
            state="normal" if user.identity else "disabled",
            command=lambda: self.copy_to_clipboard(user.identity),
        )

        if client.is_operator:
            menu.add_separator()
            menu.add_command(label="Operator actions", state="disabled")
            menu.add_command(label="Kick", command=lambda: self.admin_action(user, ADMIN_KICK))
            menu.add_command(label="Ban", command=lambda: self.admin_action(user, ADMIN_BAN))
            if user.server_muted:
                menu.add_command(label="Server Unmute", command=lambda: self.admin_action(user, ADMIN_UNMUTE))
            else:
                menu.add_command(label="Server Mute", command=lambda: self.admin_action(user, ADMIN_MUTE))
            if user.operator:
                menu.add_command(label="Remove Operator", command=lambda: self.admin_action(user, ADMIN_DEOP))
            else:
                menu.add_command(label="Make Operator", command=lambda: self.admin_action(user, ADMIN_OP))

            move_menu = tk.Menu(menu, tearoff=0)
            for channel in sorted(client.channels.values(), key=lambda entry: entry.id):
                if channel.id != user.room:
                    move_menu.add_command(
                        label=channel.name,
                        command=lambda room_id=channel.id: self.admin_action(user, ADMIN_MOVE, room_id),
                    )
            menu.add_cascade(label="Move to", menu=move_menu)

    def admin_action(self, user, action, argument=None):
        if action in (ADMIN_KICK, ADMIN_BAN):
            verb = "Kick" if action == ADMIN_KICK else "Ban"
            if not messagebox.askyesno(verb, f"{verb} {user.name}?", parent=self.root):
                return
        self.client.admin(action, user.sid, argument)
        self.log(f"{action} requested on {user.name}.", "sys")

    def poke(self, user):
        text = simpledialog.askstring("Poke", f"Message to {user.name}:", parent=self.root, initialvalue="Hey!")
        if text is not None and self.client:
            self.client.poke(user.sid, text)

    def set_local_mute(self, user, muted):
        if self.client:
            self.client.set_local_mute(user.sid, muted)
        self.log(f"{user.name} {'muted' if muted else 'unmuted'} locally.", "sys")
        self.icon_state.pop(f"u{user.sid}", None)
        if user.identity:
            muted_identities = set(self.settings.get("user_muted") or [])
            if muted:
                muted_identities.add(user.identity)
            else:
                muted_identities.discard(user.identity)
            self.settings["user_muted"] = sorted(muted_identities)
            self.settings.save()

    def set_user_gain(self, user, decibels):
        if self.client:
            self.client.set_gain(user.sid, decibels)
        if user.identity:
            gains = dict(self.settings.get("user_gains") or {})
            if decibels:
                gains[user.identity] = decibels
            else:
                gains.pop(user.identity, None)
            self.settings["user_gains"] = gains
            self.settings.save()

    def join_room(self, room_id):
        client = self.client
        if not client or not client.connected:
            return
        channel = client.channels.get(room_id)
        if channel is None or room_id == client.my_room:
            return

        password = None
        if channel.access & ACCESS_PASSWORD:
            cache_key = (client.server_hash, room_id)
            password = self.passwords.get(cache_key)
            favourite_room = (self.server_entry or {}).get("room") or ""
            if password is None and favourite_room.lower() == channel.name.lower():
                password = self.server_entry.get("password") or None
            if password is None:
                password = simpledialog.askstring(
                    "Join Room", f"Password for {channel.name}:", show="*", parent=self.root
                )
                if password is None:
                    return
            self.passwords[cache_key] = password

        self.last_join = (channel.name, password)
        client.move(room_id, password)

    def rebuild_tree(self):
        client = self.client
        tree = self.tree
        selected = tree.focus()
        tree.delete(*tree.get_children())
        self.icon_state.clear()
        if not client:
            return

        if client.server_name:
            label = client.server_name
        elif self.server_entry:
            label = self.server_entry["label"]
        else:
            label = "server"
        if not client.connected:
            label += f"  ({client.state})"

        tree.insert("", "end", iid="server", text=f" {label}", image=self.icons["server"], open=True)
        for channel in sorted(client.channels.values(), key=lambda entry: entry.id):
            if channel.locked:
                icon = self.icons["channel_locked"]
            elif channel.dialin_number:
                icon = self.icons["phone"]
            else:
                icon = self.icons["channel"]
            tree.insert("server", "end", iid=f"ch{channel.id}", text=f" {channel.name}", image=icon, open=True)

        users = sorted(client.users.values(), key=lambda entry: (entry.sid != client.my_sid, entry.name.lower()))
        for user in users:
            parent = f"ch{user.room}" if user.room in client.channels else "server"
            tags = ("self",) if user.sid == client.my_sid else ()
            tree.insert(
                parent, "end", iid=f"u{user.sid}", text=f" {user.name}", image=self.icons["user_idle"], tags=tags
            )

        if selected and tree.exists(selected):
            tree.focus(selected)
            tree.selection_set(selected)

    def user_icon_kind(self, user):
        client = self.client
        now = time.time()
        if user.text_only:
            kind = "user_keyboard"
        elif user.deaf:
            kind = "user_deaf"
        elif user.muted or user.server_muted:
            kind = "user_muted"
        elif user.sid in client.local_muted:
            kind = "user_localmute"
        elif user.sid == client.my_sid and client.transmitting:
            kind = "user_talking"
        elif now - client.last_heard.get(user.sid, 0) < TALKING_SECONDS:
            kind = "user_talking"
        else:
            kind = "user_idle"
        if user.operator:
            kind += "+op"
        return kind

    def update_icons(self):
        client = self.client
        if not client:
            return
        for user in client.users.values():
            kind = self.user_icon_kind(user)
            item = f"u{user.sid}"
            if self.icon_state.get(item) != kind and self.tree.exists(item):
                self.tree.item(item, image=self.icons[kind])
                self.icon_state[item] = kind







    def log(self, text, tag="sys", who=None):
        self.chat.config(state="normal")
        self.chat.insert("end", time.strftime("[%H:%M:%S] "), "time")
        if who is not None:
            self.chat.insert("end", f"{who}: ", "me" if tag == "me" else "name")
            self.chat.insert("end", text + "\n")
        else:
            self.chat.insert("end", text + "\n", tag)
        self.chat.see("end")
        self.chat.config(state="disabled")

    def send_text(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        client = self.client
        if client and client.connected and client.my_room is not None:
            client.send_text(text)
        else:
            self.log("Join a room to chat.", "err")


    def room_name(self, room_id):
        channel = self.client.channels.get(room_id)
        if channel:
            return channel.name
        return "no room"

    def handle_event(self, event):
        client = self.client
        kind = event[0]
        if kind == "connected":
            if client.hops is not None:
                plural = "s" if client.hops != 1 else ""
                path = f" ({client.hops} hop{plural} away)"
            else:
                path = ""
            motd = f" {client.motd}" if client.motd else ""
            self.log(f"Connected to {client.server_name}{path}.{motd}", "sys")
            self.tree_dirty = True
            self.reconnect_attempt = 0
        elif kind == "synced":
            for channel in client.channels.values():
                if channel.dialin_number:
                    self.log(f"{channel.name} can be called from rnphone: {channel.dialin_number}", "sys")
            self.tree_dirty = True
        elif kind == "channel":
            self.tree_dirty = True
        elif kind == "room":
            self.apply_settings()
            channel = client.channels.get(event[1])
            if channel:
                self.log(f"Joined {channel.name} ({describe(channel.profile)}).", "sys")
            else:
                self.log("Not in any room: double-click a room to join it.", "sys")
            self.tree_dirty = True
        elif kind == "denied":
            room_id, reason = event[1], event[2]
            if room_id is not None:
                self.log(f"Cannot join {self.room_name(room_id)}: {reason}", "err")
            else:
                self.log(f"Refused: {reason}", "err")
            if "password" in reason:
                self.passwords.pop((client.server_hash, room_id), None)
        elif kind == "info":
            self.log(event[1], "sys")
        elif kind == "notice":
            self.log(event[1], "err")
        elif kind == "version":
            self.log(
                f"The server runs Partyline {event[1]} and you run {APP_VERSION}. Please update so both match.", "err"
            )
        elif kind == "user_joined":
            user = event[1]
            if user.sid != client.my_sid and client.synced:
                self.log(f"{user.name} connected", "sys")
                if user.room == client.my_room and client.my_room is not None:
                    self.sounds.play("room_join")
                else:
                    self.sounds.play("join")
            self.apply_remembered_user_settings(user)
            self.tree_dirty = True
        elif kind == "user_left":
            self.log(f"{event[1].name} disconnected", "sys")
            if event[1].room == client.my_room and client.my_room is not None:
                self.sounds.play("room_leave")
            else:
                self.sounds.play("leave")
            self.tree_dirty = True
        elif kind == "user_moved":
            user = event[1]
            previous_room = event[2]
            if user.sid != client.my_sid:
                self.log(f"{user.name} moved to {self.room_name(user.room)}", "sys")
                if user.room == client.my_room and client.my_room is not None:
                    self.sounds.play("room_join")
                elif previous_room == client.my_room and client.my_room is not None:
                    self.sounds.play("room_leave")
            self.tree_dirty = True
        elif kind == "user_state":
            user, previous = event[1], event[2]
            if user.operator != previous.operator and user.sid != client.my_sid:
                self.log(f"{user.name} is {'now' if user.operator else 'no longer'} an operator.", "sys")
            self.tree_dirty = True
        elif kind == "text":
            sender = event[1]
            is_me = getattr(sender, "sid", None) == client.my_sid
            self.log(event[2], "me" if is_me else "name", who=getattr(sender, "name", "?"))
        elif kind == "poke":
            self.log(f"{getattr(event[1], 'name', 'Someone')} poked you: {event[2]}", "err")
            PokeWindow(self, event[1], event[2])
        elif kind == "poked":
            self.log(f"You poked {getattr(event[1], 'name', '?')}: {event[2]}", "sys")
        elif kind == "error":
            self.log(event[1], "err")
            self.tree_dirty = True
        elif kind == "closed":
            self.log(f"Connection lost: {event[1]}", "err")
            self.sounds.play("disconnect")
            if client.kicked:
                self.wanted = False  # kicked or banned: do not come straight back
            self.tree_dirty = True

    def apply_remembered_user_settings(self, user):
        if not user.identity:
            return
        remembered_gain = (self.settings.get("user_gains") or {}).get(user.identity)
        if remembered_gain:
            self.client.set_gain(user.sid, remembered_gain)
        if user.identity in (self.settings.get("user_muted") or []):
            self.client.set_local_mute(user.sid, True)

    def refresh(self):
        client = self.client
        if client:
            while client.events:
                self.handle_event(client.events.popleft())
            failed = client.state == "idle" and not client.connected and client.error
            if failed or client.state == "closed":
                self.client = None
                client = None
                self.tree_dirty = True
                self.icon_state.clear()
                if self.wanted:
                    self.schedule_reconnect()

        if self.tree_dirty:
            self.rebuild_tree()
            self.tree_dirty = False

        if client:
            self.update_icons()
            stats = client.stats()
            channel = client.channels.get(client.my_room)
            focused = self.tree.focus()
            if client.connected and not focused.startswith(("ch", "u")):
                if channel:
                    where = f", in {channel.name}: {describe(channel.profile)}"
                    if not client.can_speak_here:
                        where += " (listen only)"
                else:
                    where = ", not in a room"
                self.status_var.set(f"Connected to {client.server_name}{where}")
            elif not client.connected:
                self.status_var.set(client.state.capitalize() + "...")
            if self.settings["debug_stats"]:
                playout = client.playout
                audio = ""
                if playout:
                    audio = (
                        f"   lost {playout.lost}  late {playout.recovered}  concealed {playout.concealed}  "
                        f"dropped {playout.dropped + client.dropped}  buffer {playout.depth_ms} ms"
                    )
                self.rate_var.set(f"TX {stats['tx_kbps']:4.1f} kbps   RX {stats['rx_kbps']:4.1f} kbps{audio}")
            else:
                self.rate_var.set("")
            self.meter["value"] = max(0, client.level + 80)
            self.tx_label.config(bg="#3fb950" if client.transmitting else self.palette["tx_off"])
            self.connect_button.state(["disabled"])
            self.disconnect_button.state(["!disabled"])
        else:
            if self.wanted and self.reconnect_job:
                seconds_left = max(0, int(self.reconnect_at - time.time()))
                self.status_var.set(
                    f"Connection lost, attempting to reconnect in {seconds_left} s (attempt {self.reconnect_attempt + 1})"
                )
            else:
                self.status_var.set("Not connected")
            self.rate_var.set("")
            self.meter["value"] = 0
            self.tx_label.config(bg=self.palette["tx_off"])
            self.connect_button.state(["!disabled"])
            self.disconnect_button.state(["!disabled"] if self.wanted else ["disabled"])

        self.root.after(REFRESH_MS, self.refresh)


def main():
    parser = argparse.ArgumentParser(description="Partyline: group voice over Reticulum")
    parser.add_argument("--version", action="version", version=f"Partyline {APP_VERSION} protocol {PROTOCOL_VERSION}")
    parser.add_argument("--configdir", default=None, help="Reticulum config directory (default ~/.reticulum)")
    parser.add_argument("--identity", default=IDENTITY_FILE, help="our identity file")
    parser.add_argument("--connect", default=None, metavar="HASH", help="connect to this server at startup")
    args = parser.parse_args()

    RNS.Reticulum(configdir=args.configdir)
    identity = load_identity(args.identity)
    tune_gc()
    print(f"Our identity hash: {identity.hash.hex()}", flush=True)

    settings = Settings()
    servers = ServerList()
    discovery = Discovery()

    root = tk.Tk(className="Partyline")  #  launcher entry
    set_window_icon(root)
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass

    app = App(root, identity, settings, servers, discovery)
    root.protocol("WM_DELETE_WINDOW", app.quit)

    if args.connect:
        entry = servers.find(args.connect)
        if entry is None:
            entry = {
                "label": args.connect[:12],
                "hash": args.connect,
                "name": "",
                "room": "",
                "password": "",
                "server_password": "",
                "last_used": None,
            }
        root.after(200, lambda: app.connect_to(entry))

    root.mainloop()


if __name__ == "__main__":
    main()
