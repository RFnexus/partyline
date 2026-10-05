### Vendored from
### rns://4cf8a0651c4d73cacd0f93ac1d95e80a/public/RNS_Config_Tools
# Author: rfnx <rfnx_dev@proton.me>

# This is free and unencumbered software released into the public domain.

# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.

# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
# For more information, please refer to <https://unlicense.org>

import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk, messagebox

import RNS

from . import rnsconfig
from .i18n import _, N_, ngettext

REFRESH_MS = 2000
CUSTOM_LABEL = N_("Other (custom type)")
RESTART_TEXT = N_("Saved. The change takes effect when Reticulum restarts: {target}.")
ONLINE = "online"
OFFLINE = "offline"
UNKNOWN = "unknown"
OFF = "off"
LOCAL_SERVER_TYPES = ("LocalServerInterface",)
LOCAL_CLIENT_TYPES = ("LocalClientInterface",)
UNKNOWN_GRACE_S = 15
TYPE_LABELS = {"RNodeInterface": "RNodeInterface", "RNodeMultiInterface": "RNodeMultiInterface"}
HIDDEN_TYPES = ("WeaveInterface",)
PASTE_HINT = N_("Paste one or more interface sections as they appear in the Reticulum config file, for example:")
PROGRAM_WARNING = N_("These interfaces run a program on this computer whenever Reticulum starts:\n\n{listing}\n\nAdd them anyway?")
MISMATCH_TEXT = N_(
    "The running Reticulum instance uses none of the interfaces in this file. It was probably started with another "
    "configuration directory, so changes made here will not reach it."
)
CONSENT_TEXT = N_(
    "Partyline is about to change the Reticulum configuration file\n\n{path}\n\n"
    "Comments and other settings in the file are kept, a copy of the original is saved next to it as config.bak, "
    "and changes take effect when Reticulum restarts. "
    "Allow Partyline to edit this file? This is asked only once."
)
SECRET_WORDS = ("passphrase", "password", "secret")
MASK = "********"
PASTE_EXAMPLE = "[[My Hub]]\n  type = TCPClientInterface\n  enabled = yes\n  target_host = hub.example.org\n  target_port = 4242"


@dataclass(frozen=True)
class InterfaceState:
    name: str
    kind: str
    up: bool
    extra: str = ""
    received: int = 0
    sent: int = 0

    @property
    def traffic(self):
        return self.received + self.sent

    def describe(self):
        parts = [self.kind, _("up") if self.up else _("down")]
        if self.extra:
            parts.append(self.extra)
        if self.traffic:
            parts.append(_("{0} in, {1} out").format(RNS.prettysize(self.received), RNS.prettysize(self.sent)))
        return f"{self.name}  ({', '.join(parts)})"


@dataclass
class Status:
    level: str
    text: str
    interfaces: list = field(default_factory=list)

    @property
    def key(self):
        return (self.level, self.text)


PLURALS = {
    "interface": (N_("{0} interface"), N_("{0} interfaces")),
    "client": (N_("{0} client"), N_("{0} clients")),
    "peer": (N_("{0} peer"), N_("{0} peers")),
    "other program": (N_("{0} other program"), N_("{0} other programs")),
}


def count(number, word):
    singular, plural = PLURALS[word]
    return ngettext(singular, plural, number).format(number)


def kind_label(type_name):
    if type_name in TYPE_LABELS:
        return TYPE_LABELS[type_name]
    return rnsconfig.type_label(type_name)


def local_kind(ifstat):
    kind = str(ifstat.get("type") or "")
    name = str(ifstat.get("name") or "")
    if kind in LOCAL_SERVER_TYPES or name.startswith("Shared Instance["):
        return "server"
    if kind in LOCAL_CLIENT_TYPES or name.startswith("LocalInterface["):
        return "client"
    return ""


def masked(entry):
    result = {}
    for key, value in entry.items():
        if isinstance(value, dict):
            result[key] = masked(value)
        elif value not in ("", None) and any(word in str(key).lower() for word in SECRET_WORDS):
            result[key] = MASK
        else:
            result[key] = value
    return result


def config_path():
    return rnsconfig.default_config_path()


def interface_state(ifstat):
    name = str(ifstat.get("short_name") or ifstat.get("name") or "?")
    extras = []
    if ifstat.get("clients"):
        extras.append(count(int(ifstat["clients"]), "client"))
    if ifstat.get("peers"):
        extras.append(count(int(ifstat["peers"]), "peer"))
    if ifstat.get("tunnelstate"):
        extras.append(str(ifstat["tunnelstate"]).lower())
    return InterfaceState(
        name,
        kind_label(str(ifstat.get("type") or "?")),
        bool(ifstat.get("status")),
        ", ".join(extras),
        int(ifstat.get("rxb") or 0),
        int(ifstat.get("txb") or 0),
    )


def status():
    reticulum = rnsconfig.instance()
    if reticulum is None:
        return Status(OFF, "Not running")
    via_shared = bool(getattr(reticulum, "is_connected_to_shared_instance", False))
    stats = rnsconfig.live_stats(reticulum)
    if stats is None:
        return Status(UNKNOWN, "The shared instance is not answering" if via_shared else "Interface status is unavailable")
    interfaces = []
    programs = 0
    for ifstat in stats.get("interfaces", []):
        local = local_kind(ifstat)
        if local == "server":
            programs = int(ifstat.get("clients") or 0)
        elif local == "client" or ifstat.get("parent_interface_name"):
            continue
        else:
            interfaces.append(interface_state(ifstat))
    interfaces.sort(key=lambda entry: (not entry.up, -entry.traffic))
    up = sum(1 for entry in interfaces if entry.up)
    total = len(interfaces)
    if via_shared:
        programs = max(0, programs - 1)
        if up:
            text = _("Online through the shared instance, {0} of {1} up").format(up, count(total, 'interface'))
        elif total:
            text = _("Offline, the shared instance has no interfaces up (0 of {0})").format(total)
        else:
            text = _("Offline, the shared instance has no interfaces configured")
        if programs:
            text += _(", alongside {0}").format(count(programs, 'other program'))
    else:
        if up:
            text = _("Online, {0} of {1} up").format(up, count(total, 'interface'))
        elif total:
            text = _("Offline, no interfaces up (0 of {0})").format(total)
        else:
            text = _("Offline, no interfaces configured")
        if getattr(reticulum, "is_shared_instance", False):
            text += (_(", serving {0}").format(count(programs, 'other program')) if programs else _(", running as the shared instance"))
        try:
            if RNS.Reticulum.transport_enabled():
                text += _(", transport enabled")
        except Exception:
            pass
    return Status(ONLINE if up else OFFLINE, text, interfaces)


class Monitor:
    def __init__(self, grace=UNKNOWN_GRACE_S):
        self.grace = grace
        self.unknown_since = None

    def poll(self):
        current = status()
        if current.level != UNKNOWN:
            self.unknown_since = None
            return current
        now = time.time()
        if self.unknown_since is None:
            self.unknown_since = now
        if now - self.unknown_since < self.grace:
            return current
        return Status(OFFLINE, _("Offline, {0}").format(current.text[0].lower() + current.text[1:]))


def place_near(window, parent, offset=40):
    window.update_idletasks()
    window.geometry(f"+{parent.winfo_rootx() + offset}+{parent.winfo_rooty() + offset}")


class InterfacesDialog(tk.Toplevel):
    def __init__(self, app, configdir=None):
        self.config_file = rnsconfig.ReticulumConfig(configdir=configdir)
        super().__init__(app.root)
        self.app = app
        self.palette = app.palette
        self.title(_("Reticulum Interfaces"))
        self.transient(app.root)
        self.configure(bg=self.palette["bg"])
        self.rows = {}
        self.stats = {}
        self.refresh_job = None
        self.changed = False
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.build()
        self.reload()
        self.schedule_refresh()
        place_near(self, app.root)

    def restart_target(self):
        reticulum = rnsconfig.instance()
        if reticulum is None:
            return _("start Reticulum again")
        if rnsconfig.uses_shared_instance(reticulum):
            return _("restart the shared instance (rnsd)")
        return _("restart Partyline")

    def instance_text(self):
        reticulum = rnsconfig.instance()
        if reticulum is None:
            return _("Reticulum is not running in this application.")
        if rnsconfig.uses_shared_instance(reticulum):
            return _("Connected to the shared Reticulum instance (rnsd). Restart it after changing interfaces.")
        return _("Partyline runs its own Reticulum instance. Restart Partyline after changing interfaces.")

    def build(self):
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=_("Configuration file: {0}").format(self.config_file.path)).pack(anchor="w")
        self.instance_label = ttk.Label(frame, text=self.instance_text(), foreground=self.palette["muted"], wraplength=760, justify="left")
        self.instance_label.pack(anchor="w", pady=(0, 8))

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        columns = ("type", "enabled", "status", "bitrate", "rx", "tx", "announces")
        self.table = ttk.Treeview(table_frame, columns=columns, show="tree headings", height=14, selectmode="browse")
        self.table.heading("#0", text=_("Interface"))
        self.table.column("#0", width=220, stretch=True)
        for column, heading, width, anchor in (
            ("type", _("Type"), 160, "w"),
            ("enabled", _("Enabled"), 64, "center"),
            ("status", _("Status"), 64, "center"),
            ("bitrate", _("Bitrate"), 90, "e"),
            ("rx", _("Received"), 90, "e"),
            ("tx", _("Sent"), 90, "e"),
            ("announces", _("Announces in/out"), 120, "center"),
        ):
            self.table.heading(column, text=heading)
            self.table.column(column, width=width, anchor=anchor, stretch=False)
        self.table.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(table_frame, command=self.table.yview).pack(side="right", fill="y")
        self.table.bind("<Double-1>", lambda event: self.edit())
        self.table.bind("<<TreeviewSelect>>", lambda event: self.update_buttons())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text=_("Add..."), command=self.add).pack(side="left")
        ttk.Button(buttons, text=_("Paste..."), command=self.paste).pack(side="left", padx=(4, 0))
        self.edit_button = ttk.Button(buttons, text=_("Edit..."), command=self.edit)
        self.edit_button.pack(side="left", padx=(4, 0))
        self.toggle_button = ttk.Button(buttons, text=_("Disable"), command=self.toggle)
        self.toggle_button.pack(side="left", padx=(4, 0))
        self.remove_button = ttk.Button(buttons, text=_("Remove"), command=self.remove)
        self.remove_button.pack(side="left", padx=(4, 0))
        self.details_button = ttk.Button(buttons, text=_("Details..."), command=self.details)
        self.details_button.pack(side="left", padx=(12, 0))
        ttk.Button(buttons, text=_("Close"), command=self.close).pack(side="right")
        ttk.Button(buttons, text=_("Refresh"), command=self.reload).pack(side="right", padx=(0, 4))

        self.notice = ttk.Label(frame, text="", foreground=self.palette["err"], wraplength=640, justify="left")
        self.notice.pack(anchor="w", pady=(8, 0))

    def selected_name(self):
        item = self.table.focus()
        return self.rows.get(item)

    def row_values(self, name, entry):
        ifstat = self.stats.get(name)
        enabled = _("yes") if rnsconfig.is_enabled(entry) else _("no")
        if ifstat is None:
            status = "" if not self.stats else _("down")
            return (str(entry.get("type", "?")), enabled, status, "", "", "", "")
        announces = f"{rnsconfig.format_rate(ifstat.get('incoming_announce_frequency'))} / {rnsconfig.format_rate(ifstat.get('outgoing_announce_frequency'))}"
        return (
            str(entry.get("type", "?")),
            enabled,
            _("up") if rnsconfig.is_up(ifstat) else _("down"),
            rnsconfig.format_speed(ifstat.get("bitrate")),
            rnsconfig.format_bytes(ifstat.get("rxb", 0)),
            rnsconfig.format_bytes(ifstat.get("txb", 0)),
            announces,
        )

    def sort_key(self, name):
        ifstat = self.stats.get(name) or {}
        traffic = int(ifstat.get("rxb") or 0) + int(ifstat.get("txb") or 0)
        return (not rnsconfig.is_up(ifstat), not self.config_file.is_enabled(name), -traffic, name.lower())

    def reload(self):
        try:
            self.config_file.reload()
        except Exception as error:
            messagebox.showerror("Reticulum", _("{0} could not be read: {1}").format(self.config_file.path, error), parent=self)
        self.stats = rnsconfig.by_config_name(rnsconfig.live_stats())
        selected = self.selected_name()
        self.table.delete(*self.table.get_children())
        self.rows = {}
        names = sorted(self.config_file.names(), key=self.sort_key)
        for index, name in enumerate(names):
            iid = f"i{index}"
            self.rows[iid] = name
            self.table.insert("", "end", iid=iid, text=f" {name}", values=self.row_values(name, self.config_file.get(name)))
            if name == selected:
                self.table.focus(iid)
                self.table.selection_set(iid)
        running = {name for name, ifstat in self.stats.items() if not local_kind(ifstat)}
        mismatch = bool(running) and not (running & set(names))
        self.instance_label.config(
            text=_(MISMATCH_TEXT) if mismatch else self.instance_text(), foreground=self.palette["err" if mismatch else "muted"]
        )
        self.update_buttons()

    def refresh_stats(self):
        self.stats = rnsconfig.by_config_name(rnsconfig.live_stats())
        for iid, name in self.rows.items():
            if self.table.exists(iid) and self.config_file.has(name):
                self.table.item(iid, values=self.row_values(name, self.config_file.get(name)))

    def schedule_refresh(self):
        self.refresh_job = self.after(REFRESH_MS, self.tick)

    def tick(self):
        self.refresh_job = None
        try:
            self.refresh_stats()
        except tk.TclError:
            return
        self.schedule_refresh()

    def update_buttons(self):
        name = self.selected_name()
        state = "normal" if name else "disabled"
        for button in (self.edit_button, self.toggle_button, self.remove_button, self.details_button):
            button.config(state=state)
        if name:
            self.toggle_button.config(text=(_("Disable") if self.config_file.is_enabled(name) else _("Enable")))

    def allowed(self, parent=None):
        settings = self.app.settings
        if settings.get("edit_rns_config"):
            return True
        if not messagebox.askyesno("Reticulum", _(CONSENT_TEXT).format(path=self.config_file.path), parent=parent or self):
            return False
        settings["edit_rns_config"] = True
        settings.save()
        return True

    def mark_changed(self):
        self.changed = True
        self.notice.config(text=_(RESTART_TEXT).format(target=self.restart_target()))

    def save(self):
        try:
            self.config_file.save()
        except Exception as error:
            messagebox.showerror("Reticulum", _("Could not write {0}: {1}").format(self.config_file.path, error), parent=self)
            self.reload()
            return False
        self.mark_changed()
        return True

    def add(self):
        if InterfaceDialog(self, self.config_file).show():
            self.mark_changed()
        self.reload()

    def paste(self):
        if PasteDialog(self, self.config_file).show():
            self.mark_changed()
        self.reload()

    def edit(self):
        name = self.selected_name()
        if name is None:
            return
        if InterfaceDialog(self, self.config_file, name).show():
            self.mark_changed()
        self.reload()

    def toggle(self):
        name = self.selected_name()
        if name is None or not self.allowed():
            return
        self.config_file.set_enabled(name, not self.config_file.is_enabled(name))
        self.save()
        self.reload()

    def remove(self):
        name = self.selected_name()
        if name is None or not self.allowed():
            return
        if not messagebox.askyesno(_("Remove interface"), _("Remove the interface “{0}” from the configuration?").format(name), parent=self):
            return
        self.config_file.remove(name)
        self.save()
        self.reload()

    def details(self):
        name = self.selected_name()
        if name is not None:
            DetailsDialog(self, name)

    def close(self):
        if self.refresh_job is not None:
            self.after_cancel(self.refresh_job)
            self.refresh_job = None
        self.destroy()


class InterfaceDialog(tk.Toplevel):
    def __init__(self, parent, config_file, name=None):
        super().__init__(parent)
        self.parent_dialog = parent
        self.palette = parent.palette
        self.config_file = config_file
        self.name = name
        self.original = config_file.get(name) if name else {}
        self.result = None
        self.widgets = {}
        self.variables = {}
        self.title((_("Edit Interface: {0}").format(name) if name else _("Add Interface")))
        self.transient(parent)
        self.configure(bg=self.palette["bg"])
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda event: self.cancel())
        self.labels = {kind_label(kind): kind for kind in rnsconfig.type_names() if kind not in HIDDEN_TYPES}
        original_type = str(self.original.get("type", "")) if name else "TCPClientInterface"
        self.custom_type = "" if rnsconfig.known_type(original_type) else original_type
        self.build(original_type)
        place_near(self, parent, 30)

    def build(self, original_type):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text=_("Type")).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.type_var = tk.StringVar()
        self.type_box = ttk.Combobox(head, textvariable=self.type_var, state="readonly", width=34, values=list(self.labels) + [_(CUSTOM_LABEL)])
        self.type_box.grid(row=0, column=1, sticky="w", pady=3)
        self.type_box.bind("<<ComboboxSelected>>", lambda event: self.rebuild_fields())
        self.custom_var = tk.StringVar(value=self.custom_type)
        self.custom_entry = ttk.Entry(head, textvariable=self.custom_var, width=30)
        ttk.Label(head, text=_("Name")).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.name_var = tk.StringVar(value=self.name or "")
        ttk.Entry(head, textvariable=self.name_var, width=36).grid(row=2, column=1, sticky="ew", pady=3)
        self.enabled_var = tk.BooleanVar(value=rnsconfig.is_enabled(self.original) if self.name else True)
        ttk.Checkbutton(head, text=_("Enabled"), variable=self.enabled_var).grid(row=3, column=1, sticky="w", pady=(3, 8))
        head.columnconfigure(1, weight=1)

        self.fields = ttk.Frame(outer)
        self.fields.pack(fill="both", expand=True)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text=_("Save"), command=self.save).pack(side="right")
        ttk.Button(buttons, text=_("Cancel"), command=self.cancel).pack(side="right", padx=6)

        if self.custom_type or original_type not in self.labels.values():
            self.type_var.set(_(CUSTOM_LABEL))
        else:
            self.type_var.set(kind_label(original_type))
        self.rebuild_fields()

    def selected_type(self):
        label = self.type_var.get()
        if label == _(CUSTOM_LABEL):
            return self.custom_var.get().strip()
        return self.labels.get(label, "")

    def rebuild_fields(self):
        for child in self.fields.winfo_children():
            child.destroy()
        self.widgets = {}
        self.variables = {}
        custom = self.type_var.get() == _(CUSTOM_LABEL)
        if custom:
            ttk.Label(self.fields.master.winfo_children()[0], text=_("Type name")).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
            self.custom_entry.grid(row=1, column=1, sticky="w", pady=3)
        else:
            self.custom_entry.grid_remove()
            for label in self.fields.master.winfo_children()[0].grid_slaves(row=1, column=0):
                label.destroy()
        type_name = self.selected_type()
        values, extras = rnsconfig.values_from_entry(type_name, self.original)
        original_type = str(self.original.get("type", ""))
        if original_type and original_type != type_name:
            stale = {opt.key for opt in rnsconfig.options_for(original_type)} - {opt.key for opt in rnsconfig.options_for(type_name)}
            extras = {key: value for key, value in extras.items() if key not in stale}
        if not self.name:
            for opt in rnsconfig.options_for(type_name):
                if opt.default is not None and not values.get(opt.key):
                    values[opt.key] = opt.default

        basic = ttk.Frame(self.fields)
        basic.pack(fill="x")
        advanced = ttk.Frame(self.fields)
        row_basic = 0
        row_advanced = 0
        for opt in rnsconfig.options_for(type_name):
            target = advanced if opt.advanced else basic
            row = row_advanced if opt.advanced else row_basic
            self.add_field(target, row, opt, values.get(opt.key))
            if opt.advanced:
                row_advanced += 1
            else:
                row_basic += 1
        basic.columnconfigure(1, weight=1)
        advanced.columnconfigure(1, weight=1)

        self.advanced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.fields, text=_("More options"), variable=self.advanced_var, command=lambda: self.show_advanced(advanced)).pack(anchor="w", pady=(6, 0))
        self.advanced_frame = advanced
        self.show_advanced(advanced)

        ttk.Label(self.fields, text=_("Other settings (key = value per line, sub-interfaces as [[[Name]]] sections)")).pack(anchor="w", pady=(8, 2))
        self.extras_text = tk.Text(
            self.fields,
            height=6 if not rnsconfig.text_only(type_name) else 12,
            width=60,
            bg=self.palette["field"],
            fg=self.palette["fg"],
            insertbackground=self.palette["fg"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.palette["border"],
        )
        self.extras_text.pack(fill="both", expand=True)
        self.extras_text.insert("1.0", rnsconfig.settings_text(extras))

    def show_advanced(self, frame):
        if self.advanced_var.get():
            frame.pack(fill="x", pady=(6, 0))
        else:
            frame.pack_forget()

    def add_field(self, frame, row, opt, value):
        ttk.Label(frame, text=opt.label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        if opt.kind == "bool":
            variable = tk.BooleanVar(value=bool(value))
            widget = ttk.Checkbutton(frame, variable=variable)
        elif opt.kind == "choice":
            variable = tk.StringVar(value=str(value or ""))
            choices = list(opt.choices) if opt.required else [""] + list(opt.choices)
            widget = ttk.Combobox(frame, textvariable=variable, values=choices, state="readonly", width=22)
        elif opt.kind == "port":
            variable = tk.StringVar(value=str(value or ""))
            widget = ttk.Combobox(frame, textvariable=variable, values=rnsconfig.port_choices(), width=30)
        else:
            if isinstance(value, list):
                value = ", ".join(value)
            variable = tk.StringVar(value=str(value or ""))
            widget = ttk.Entry(frame, textvariable=variable, width=32, show="*" if opt.kind == "password" else "")
        widget.grid(row=row, column=1, sticky="w", pady=2)
        if opt.hint:
            ttk.Label(frame, text=opt.hint, foreground=self.palette["muted"]).grid(row=row, column=2, sticky="w", padx=(8, 0))
        self.widgets[opt.key] = widget
        self.variables[opt.key] = variable

    def save(self):
        type_name = self.selected_type()
        if not type_name:
            messagebox.showerror(_("Interface"), _("Give the custom interface a type name."), parent=self)
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror(_("Interface"), _("Give the interface a name."), parent=self)
            return
        values = {key: variable.get() for key, variable in self.variables.items()}
        errors = rnsconfig.validate(type_name, values)
        if errors:
            messagebox.showerror(_("Interface"), "\n".join(errors), parent=self)
            return
        try:
            extras = rnsconfig.parse_settings(self.extras_text.get("1.0", "end"))
        except Exception as error:
            messagebox.showerror(_("Interface"), _("Other settings could not be read: {0}").format(error), parent=self)
            return
        entry = rnsconfig.entry_from_values(type_name, values, extras, self.original, self.enabled_var.get())
        if not self.parent_dialog.allowed(self):
            return
        try:
            if self.name:
                self.config_file.update(self.name, entry, new_name=name)
            else:
                self.config_file.add(name, entry)
            self.config_file.save()
        except Exception as error:
            messagebox.showerror(_("Interface"), str(error), parent=self)
            self.config_file.reload()
            return
        self.result = True
        self.destroy()

    def show(self):
        self.grab_set()
        self.wait_window()
        return self.result

    def cancel(self):
        self.result = None
        self.destroy()


class PasteDialog(tk.Toplevel):
    def __init__(self, parent, config_file):
        super().__init__(parent)
        self.parent_dialog = parent
        self.palette = parent.palette
        self.config_file = config_file
        self.result = None
        self.title(_("Paste Interface"))
        self.transient(parent)
        self.configure(bg=self.palette["bg"])
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda event: self.cancel())
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=_(PASTE_HINT)).pack(anchor="w")
        ttk.Label(frame, text=PASTE_EXAMPLE, foreground=self.palette["muted"], justify="left", font="TkFixedFont").pack(anchor="w", pady=(2, 8))
        self.text = tk.Text(
            frame,
            height=14,
            width=64,
            bg=self.palette["field"],
            fg=self.palette["fg"],
            insertbackground=self.palette["fg"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.palette["border"],
        )
        self.text.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text=_("Add"), command=self.add).pack(side="right")
        ttk.Button(buttons, text=_("Cancel"), command=self.cancel).pack(side="right", padx=6)
        self.text.focus_set()
        place_near(self, parent, 30)

    def add(self):
        try:
            entries = rnsconfig.parse_text(self.text.get("1.0", "end"))
        except Exception as error:
            messagebox.showerror(_("Paste Interface"), _("The text could not be read: {0}").format(error), parent=self)
            return
        if not entries:
            messagebox.showerror(_("Paste Interface"), _("No interface sections found. Each interface needs a [[Name]] line followed by its settings."), parent=self)
            return
        missing = [name for name, entry in entries.items() if not str(entry.get("type", "")).strip()]
        if missing:
            messagebox.showerror(_("Paste Interface"), "No type given for: " + ", ".join(missing), parent=self)
            return
        if not self.parent_dialog.allowed(self):
            return
        programs = {name: entry.get("command") for name, entry in entries.items() if str(entry.get("type", "")) == "PipeInterface" or entry.get("command")}
        if programs:
            listing = "\n".join(f"{name}: {command or '(no command given)'}" for name, command in programs.items())
            if not messagebox.askyesno(_("Paste Interface"), _(PROGRAM_WARNING).format(listing=listing), default="no", parent=self):
                return
        existing = [name for name in entries if self.config_file.has(name)]
        if existing and not messagebox.askyesno(_("Paste Interface"), "Replace the existing interface(s) " + ", ".join(existing) + "?", parent=self):
            return
        try:
            for name, entry in entries.items():
                if self.config_file.has(name):
                    self.config_file.update(name, entry)
                else:
                    self.config_file.add(name, entry)
            self.config_file.save()
        except Exception as error:
            messagebox.showerror(_("Paste Interface"), str(error), parent=self)
            self.config_file.reload()
            return
        self.result = True
        self.destroy()

    def show(self):
        self.grab_set()
        self.wait_window()
        return self.result

    def cancel(self):
        self.result = None
        self.destroy()


class DetailsDialog(tk.Toplevel):
    def __init__(self, parent, name):
        super().__init__(parent)
        self.parent_dialog = parent
        self.palette = parent.palette
        self.name = name
        self.refresh_job = None
        self.title(_("Interface: {0}").format(name))
        self.transient(parent)
        self.configure(bg=self.palette["bg"])
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        entry = parent.config_file.get(name)
        ttk.Label(frame, text=f"{name}", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"{entry.get('type', '?')} {rnsconfig.describe_entry(entry)}".strip(), foreground=self.palette["muted"]).pack(anchor="w", pady=(0, 8))
        self.rows_frame = ttk.Frame(frame)
        self.rows_frame.pack(fill="x")
        self.row_labels = {}
        ttk.Label(frame, text=_("Configuration")).pack(anchor="w", pady=(10, 2))
        text = tk.Text(frame, height=10, width=64, bg=self.palette["field"], fg=self.palette["fg"], relief="flat", highlightthickness=1, highlightbackground=self.palette["border"])
        text.insert("1.0", rnsconfig.entry_text(name, masked(entry)))
        text.config(state="disabled")
        text.pack(fill="both", expand=True)
        ttk.Button(frame, text=_("Close"), command=self.close).pack(anchor="e", pady=(10, 0))
        self.refresh()
        place_near(self, parent, 50)

    def refresh(self):
        self.refresh_job = None
        stats = rnsconfig.by_config_name(rnsconfig.live_stats())
        ifstat = stats.get(self.name)
        rows = rnsconfig.summary(ifstat) if ifstat else [("Status", "not active in the running instance")]
        wanted = [label for label, value in rows]
        if list(self.row_labels) != wanted:
            for child in self.rows_frame.winfo_children():
                child.destroy()
            self.row_labels = {}
            for index, (label, value) in enumerate(rows):
                ttk.Label(self.rows_frame, text=label, foreground=self.palette["muted"]).grid(row=index, column=0, sticky="w", padx=(0, 12), pady=1)
                widget = ttk.Label(self.rows_frame, text=value)
                widget.grid(row=index, column=1, sticky="w", pady=1)
                self.row_labels[label] = widget
        else:
            for label, value in rows:
                self.row_labels[label].config(text=value)
        try:
            self.refresh_job = self.after(REFRESH_MS, self.refresh)
        except tk.TclError:
            pass

    def close(self):
        if self.refresh_job is not None:
            self.after_cancel(self.refresh_job)
        self.destroy()
