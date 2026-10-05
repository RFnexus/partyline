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
import os
import shutil
import stat
from dataclasses import dataclass

import RNS

try:
    from RNS.vendor.configobj import ConfigObj, Section
except ImportError:
    from configobj import ConfigObj, Section

DEFAULT_CONFIG_DIR = os.path.expanduser("~/.reticulum")
FALSE_WORDS = ("false", "off", "no", "0")
ENABLED_KEYS = ("interface_enabled", "enabled")
NAME_FORBIDDEN = "[]\r\n"
NEW_FILE_MODE = 0o600
ENCODING = "utf-8"
BACKUP_SUFFIX = ".bak"


def default_config_path(configdir=None):
    if configdir:
        return os.path.join(os.path.expanduser(configdir), "config")
    path = getattr(RNS.Reticulum, "configpath", None)
    if path:
        return path
    directory = getattr(RNS.Reticulum, "configdir", None) or DEFAULT_CONFIG_DIR
    return os.path.join(directory, "config")


def truthy(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in FALSE_WORDS


def entry_enabled(entry):
    for key in ENABLED_KEYS:
        if key in entry and truthy(entry.get(key)):
            return True
    return False


def check_name(name):
    name = str(name or "").strip()
    if not name:
        raise ValueError("an interface needs a name")
    if any(char in name for char in NAME_FORBIDDEN):
        raise ValueError(f"the interface name {name!r} may not contain [ ] or line breaks")
    holder = ConfigObj()
    holder["interfaces"] = {name: {"type": "check"}}
    try:
        names = list(ConfigObj(infile=holder.write())["interfaces"].keys())
    except Exception:
        names = []
    if names != [name]:
        raise ValueError(f"the interface name {name!r} cannot be stored in the configuration file")
    return name


def merge_section(section, entry):
    for key in list(section.keys()):
        if key not in entry:
            del section[key]
    for key, value in entry.items():
        current = section.get(key)
        if isinstance(current, Section) and isinstance(value, dict):
            merge_section(current, value)
            continue
        if key in section and (isinstance(current, Section) or isinstance(value, dict)):
            del section[key]
        section[key] = value


def put_entry(interfaces, name, entry):
    current = interfaces.get(name)
    if isinstance(current, Section):
        merge_section(current, dict(entry))
    else:
        interfaces[name] = dict(entry)


class ReticulumConfig:
    def __init__(self, path=None, configdir=None):
        self.path = path or default_config_path(configdir)
        self.config = None
        self.changes = {}
        self.signature = None
        self.load()

    def read(self):
        if os.path.isfile(self.path):
            config = ConfigObj(self.path, encoding=ENCODING)
        else:
            config = ConfigObj(encoding=ENCODING)
            config.filename = self.path
        if "reticulum" not in config:
            config["reticulum"] = {}
        if "interfaces" not in config:
            config["interfaces"] = {}
        return config

    def load(self):
        self.config = self.read()
        self.changes = {}
        self.signature = self.file_signature()
        return self

    reload = load

    def file_signature(self):
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    @property
    def interfaces(self):
        return self.config["interfaces"]

    @property
    def reticulum(self):
        return self.config["reticulum"]

    def exists(self):
        return os.path.isfile(self.path)

    def names(self):
        return list(self.interfaces.keys())

    def has(self, name):
        return name in self.interfaces

    def get(self, name):
        return self.interfaces[name].dict()

    def interface_type(self, name):
        return str(self.interfaces[name].get("type", ""))

    def is_enabled(self, name):
        return entry_enabled(self.interfaces[name])

    def set_enabled(self, name, enabled):
        section = self.interfaces[name]
        section["interface_enabled"] = bool(enabled)
        if "enabled" in section:
            section["enabled"] = bool(enabled)
        self.changes[name] = section.dict()

    def set(self, name, entry):
        name = check_name(name)
        put_entry(self.interfaces, name, entry)
        self.changes[name] = dict(entry)

    def add(self, name, entry):
        name = check_name(name)
        if name in self.interfaces:
            raise ValueError(f"an interface named {name!r} already exists")
        self.set(name, entry)

    def update(self, name, entry, new_name=None):
        if name not in self.interfaces:
            raise KeyError(name)
        new_name = check_name(new_name or name)
        if new_name != name:
            if new_name in self.interfaces:
                raise ValueError(f"an interface named {new_name!r} already exists")
            self.interfaces.rename(name, new_name)
            self.changes[name] = None
        self.set(new_name, entry)
        return new_name

    def remove(self, name):
        if name in self.interfaces:
            del self.interfaces[name]
        self.changes[name] = None

    def rename(self, name, new_name):
        new_name = check_name(new_name)
        if new_name == name:
            return
        if new_name in self.interfaces:
            raise ValueError(f"an interface named {new_name!r} already exists")
        self.interfaces.rename(name, new_name)
        self.changes[name] = None
        self.changes[new_name] = self.interfaces[new_name].dict()

    def transport_enabled(self):
        return truthy(self.reticulum.get("enable_transport"), False)

    def shared_instance(self):
        return truthy(self.reticulum.get("share_instance"), True)

    def instance_name(self):
        return str(self.reticulum.get("instance_name", "default"))

    def save(self):
        if self.signature != self.file_signature():
            fresh = self.read()
            for name, entry in self.changes.items():
                if entry is None:
                    fresh["interfaces"].pop(name, None)
                else:
                    put_entry(fresh["interfaces"], name, entry)
            self.config = fresh
        target = os.path.realpath(self.path)
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        temporary = os.path.join(directory, "." + os.path.basename(target) + ".tmp")
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode)
        except OSError:
            mode = NEW_FILE_MODE
        try:
            os.unlink(temporary)
        except OSError:
            pass
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, NEW_FILE_MODE), "wb") as handle:
                self.config.write(handle)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.chmod(temporary, mode)
            backup = target + BACKUP_SUFFIX
            if os.path.isfile(target) and not os.path.exists(backup):
                shutil.copy2(target, backup)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self.changes = {}
        self.signature = self.file_signature()

    def as_text(self, name):
        return entry_text(name, self.get(name))


def entry_text(name, entry):
    holder = ConfigObj()
    holder["interfaces"] = {name: dict(entry)}
    lines = holder.write()
    body = [line for line in lines[1:]]
    while body and not body[0].strip():
        body.pop(0)
    indent = len(body[0]) - len(body[0].lstrip()) if body else 0
    return "\n".join(line[indent:] if line[:indent].strip() == "" else line for line in body) + "\n"


def parse_text(text):
    lines = [line.rstrip("\n") for line in text.splitlines()]
    if not any(line.strip().lower() == "[interfaces]" for line in lines):
        lines = ["[interfaces]"] + lines
    parsed = ConfigObj(infile=lines)
    found = {}
    for name, section in parsed.get("interfaces", {}).items():
        if hasattr(section, "dict"):
            found[name] = section.dict()
    return found


def settings_text(settings):
    holder = ConfigObj()
    holder["interfaces"] = {"_": dict(settings or {})}
    lines = holder.write()[2:]
    if not lines:
        return ""
    indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    return "\n".join(line[indent:] for line in lines) + "\n"


def parse_settings(text):
    lines = ["[interfaces]", "[[_]]"] + [line.rstrip("\n") for line in (text or "").splitlines()]
    parsed = ConfigObj(infile=lines)
    section = parsed.get("interfaces", {}).get("_")
    return section.dict() if hasattr(section, "dict") else {}


MANAGED_KEYS = ("type",) + ENABLED_KEYS
MODES = ("full", "gateway", "access_point", "roaming", "boundary")
RNODE_BANDWIDTHS = ("7800", "10400", "15600", "20800", "31250", "41700", "62500", "125000", "250000", "500000", "1625000")
RNODE_SPREADING_FACTORS = tuple(str(value) for value in range(5, 13))
RNODE_CODING_RATES = tuple(str(value) for value in range(5, 9))


@dataclass
class Option:
    key: str
    label: str
    kind: str = "text"
    required: bool = False
    default: object = None
    choices: tuple = ()
    hint: str = ""
    advanced: bool = False
    scale: float = 1.0


@dataclass
class InterfaceType:
    name: str
    label: str
    summary: str
    options: tuple
    text_only: bool = False


def option(key, label, kind="text", **extra):
    return Option(key, label, kind, **extra)


SERIAL_OPTIONS = (
    option("port", "Port", "port", required=True, hint="/dev/ttyUSB0 or COM3"),
    option("speed", "Speed (bps)", "int", required=True, default="115200"),
    option("databits", "Data bits", "int", default="8"),
    option("parity", "Parity", "choice", choices=("none", "even", "odd"), default="none"),
    option("stopbits", "Stop bits", "int", default="1"),
)

KISS_TIMING_OPTIONS = (
    option("preamble", "Preamble (ms)", "int", default="150"),
    option("txtail", "TX tail (ms)", "int", default="10"),
    option("persistence", "Persistence (0-255)", "int", default="200"),
    option("slottime", "Slot time (ms)", "int", default="20"),
)

STATION_ID_OPTIONS = (
    option("id_callsign", "ID callsign", "text", hint="e.g. MYCALL-0", advanced=True),
    option("id_interval", "ID interval (s)", "int", hint="e.g. 600", advanced=True),
)

TCP_CLIENT_EXTRAS = (
    option("kiss_framing", "KISS framing", "bool", advanced=True),
    option("i2p_tunneled", "I2P tunneled", "bool", advanced=True),
    option("connect_timeout", "Connect timeout (s)", "int", advanced=True),
    option("max_reconnect_tries", "Max reconnect tries", "int", advanced=True),
)

COMMON_OPTIONS = (
    option("mode", "Interface mode", "choice", choices=MODES, advanced=True, hint="full is the default"),
    option("outgoing", "Allow outgoing traffic", "bool", default=True, advanced=True),
    option("network_name", "Virtual network name", "text", advanced=True, hint="IFAC network name"),
    option("passphrase", "IFAC passphrase", "password", advanced=True),
    option("ifac_size", "IFAC size (bits)", "int", advanced=True, hint="8 to 512"),
    option("bitrate", "Bitrate (bps)", "int", advanced=True, hint="override the inferred bitrate"),
    option("announce_cap", "Announce cap (%)", "float", advanced=True, hint="default 2.0"),
    option("announce_rate_target", "Announce rate target (s)", "int", advanced=True),
    option("announce_rate_grace", "Announce rate grace", "int", advanced=True),
    option("announce_rate_penalty", "Announce rate penalty (s)", "int", advanced=True),
    option("ingress_control", "Ingress control", "bool", advanced=True),
    option("egress_control", "Egress control", "bool", advanced=True),
)

INTERFACE_TYPES = {}


def register(name, label, summary, options, text_only=False):
    INTERFACE_TYPES[name] = InterfaceType(name, label, summary, tuple(options), text_only)


register(
    "AutoInterface",
    "Auto (local network)",
    "Finds other Reticulum peers on the local network without any configuration",
    (
        option("group_id", "Group ID", "text", hint="only peers with the same group ID connect"),
        option("discovery_scope", "Discovery scope", "choice", choices=("link", "admin", "site", "organisation", "global")),
        option("devices", "Devices", "list", hint="comma separated, e.g. eth0, wlan0"),
        option("ignored_devices", "Ignored devices", "list", hint="comma separated"),
        option("discovery_port", "Discovery port", "int", advanced=True),
        option("data_port", "Data port", "int", advanced=True),
        option("multicast_address_type", "Multicast address type", "choice", choices=("permanent", "temporary"), advanced=True),
    ),
)

register(
    "TCPClientInterface",
    "TCP client",
    "Connects to a TCP server interface somewhere on the internet or a LAN",
    (
        option("target_host", "Target host", "text", required=True, hint="hostname or IP address"),
        option("target_port", "Target port", "int", required=True, default="4242"),
    )
    + TCP_CLIENT_EXTRAS,
)

register(
    "TCPServerInterface",
    "TCP server",
    "Accepts TCP client connections from other Reticulum peers",
    (
        option("listen_ip", "Listen IP", "text", required=True, default="0.0.0.0"),
        option("listen_port", "Listen port", "int", required=True, default="4242"),
        option("device", "Device", "text", advanced=True, hint="listen only on this network device, e.g. eth0"),
        option("prefer_ipv6", "Prefer IPv6", "bool", advanced=True),
        option("i2p_tunneled", "I2P tunneled", "bool", advanced=True),
        option("fixed_mtu", "Fixed MTU", "bool", advanced=True),
    ),
)

register(
    "BackboneInterface",
    "Backbone server",
    "High performance server interface for many connecting peers",
    (
        option("listen_ip", "Listen IP", "text", default="0.0.0.0", hint="leave empty when connecting outward"),
        option("listen_port", "Listen port", "int", default="4242"),
        option("device", "Device", "text", advanced=True, hint="listen only on this network device"),
        option("prefer_ipv6", "Prefer IPv6", "bool", advanced=True),
        option("target_host", "Target host", "text", advanced=True, hint="set to connect to a remote backbone instead"),
        option("target_port", "Target port", "int", advanced=True),
        option("i2p_tunneled", "I2P tunneled", "bool", advanced=True),
        option("connect_timeout", "Connect timeout (s)", "int", advanced=True),
        option("max_reconnect_tries", "Max reconnect tries", "int", advanced=True),
    ),
)

register(
    "BackboneClientInterface",
    "Backbone client",
    "Connects to a remote backbone interface",
    (
        option("target_host", "Target host", "text", required=True),
        option("target_port", "Target port", "int", required=True, default="4242"),
        option("prefer_ipv6", "Prefer IPv6", "bool", advanced=True),
        option("i2p_tunneled", "I2P tunneled", "bool", advanced=True),
        option("connect_timeout", "Connect timeout (s)", "int", advanced=True),
        option("max_reconnect_tries", "Max reconnect tries", "int", advanced=True),
    ),
)

register(
    "UDPInterface",
    "UDP",
    "Sends packets over UDP to a fixed or broadcast address",
    (
        option("listen_ip", "Listen IP", "text", required=True, default="0.0.0.0"),
        option("listen_port", "Listen port", "int", required=True, default="4242"),
        option("forward_ip", "Forward IP", "text", required=True, hint="e.g. 255.255.255.255 for broadcast"),
        option("forward_port", "Forward port", "int", required=True, default="4242"),
        option("device", "Device", "text", advanced=True),
    ),
)

register(
    "I2PInterface",
    "I2P",
    "Reaches peers over the I2P network",
    (
        option("peers", "Peers", "list", hint="comma separated .b32.i2p addresses"),
        option("connectable", "Accept incoming peers", "bool", advanced=True),
    ),
)

register(
    "RNodeInterface",
    "RNode (LoRa)",
    "An RNode LoRa transceiver on a serial or Bluetooth port",
    (
        option("port", "Port", "port", required=True, hint="/dev/ttyUSB0, COM3 or ble://RNode name"),
        option("frequency", "Frequency (MHz)", "float", required=True, default="868.5", scale=1000000),
        option("bandwidth", "Bandwidth (Hz)", "choice", required=True, choices=RNODE_BANDWIDTHS, default="125000"),
        option("txpower", "TX power (dBm)", "int", required=True, default="7"),
        option("spreadingfactor", "Spreading factor", "choice", required=True, choices=RNODE_SPREADING_FACTORS, default="8"),
        option("codingrate", "Coding rate (4:x)", "choice", required=True, choices=RNODE_CODING_RATES, default="5"),
        option("flow_control", "Flow control", "bool", advanced=True),
        option("airtime_limit_long", "Airtime limit long (%)", "float", advanced=True),
        option("airtime_limit_short", "Airtime limit short (%)", "float", advanced=True),
    )
    + STATION_ID_OPTIONS,
)

register(
    "RNodeMultiInterface",
    "RNode multi (several radios)",
    "An RNode with several radios, each configured as a sub-interface",
    (option("port", "Port", "port", required=True),) + STATION_ID_OPTIONS,
    text_only=True,
)

register(
    "SerialInterface",
    "Serial",
    "A raw serial link to another Reticulum instance",
    SERIAL_OPTIONS,
)

register(
    "KISSInterface",
    "KISS modem",
    "A packet radio modem or TNC speaking KISS",
    SERIAL_OPTIONS + KISS_TIMING_OPTIONS + (option("flow_control", "Flow control", "bool", advanced=True),) + STATION_ID_OPTIONS,
)

register(
    "AX25KISSInterface",
    "AX.25 KISS modem",
    "A KISS modem with AX.25 framing and a callsign",
    (SERIAL_OPTIONS[0],)
    + (
        option("callsign", "Callsign", "text", required=True, hint="e.g. NO1CLL"),
        option("ssid", "SSID", "int", required=True, default="0"),
    )
    + SERIAL_OPTIONS[1:]
    + KISS_TIMING_OPTIONS
    + (option("flow_control", "Flow control", "bool", advanced=True),),
)

register(
    "PipeInterface",
    "Pipe (external program)",
    "Runs a program and exchanges packets over its standard input and output",
    (
        option("command", "Command", "text", required=True, hint="e.g. netcat -l 5757"),
        option("respawn_delay", "Respawn delay (s)", "float", advanced=True),
    ),
)

register(
    "WeaveInterface",
    "Weave",
    "A Weave mesh interface",
    (option("port", "Port", "text"),),
)

CUSTOM_TYPE = "CustomInterface"


def type_names():
    return list(INTERFACE_TYPES.keys())


def known_type(type_name):
    return type_name in INTERFACE_TYPES


def type_label(type_name):
    definition = INTERFACE_TYPES.get(type_name)
    return definition.label if definition else str(type_name)


def options_for(type_name):
    definition = INTERFACE_TYPES.get(type_name)
    if definition is None:
        return list(COMMON_OPTIONS)
    return list(definition.options) + list(COMMON_OPTIONS)


def text_only(type_name):
    definition = INTERFACE_TYPES.get(type_name)
    return definition is None or definition.text_only


def display_value(option, raw):
    if raw is None:
        return "" if option.kind != "bool" else False
    if option.kind == "bool":
        return truthy(raw, False)
    if option.kind == "list":
        if isinstance(raw, (list, tuple)):
            return [str(item).strip() for item in raw if str(item).strip()]
        return [item.strip() for item in str(raw).split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        raw = ", ".join(str(item) for item in raw)
    text = str(raw).strip()
    if option.kind == "float" and option.scale != 1.0 and text:
        try:
            scaled = float(text) / option.scale
        except ValueError:
            return text
        text = f"{scaled:.6f}".rstrip("0").rstrip(".")
    return text


def values_from_entry(type_name, entry):
    values = {}
    extras = {}
    keyed = {opt.key: opt for opt in options_for(type_name)}
    for key, raw in entry.items():
        if key in MANAGED_KEYS:
            continue
        if key in keyed:
            values[key] = display_value(keyed[key], raw)
        else:
            extras[key] = raw
    for key, opt in keyed.items():
        values.setdefault(key, False if opt.kind == "bool" else "")
    return values, extras


def parse_value(option, value):
    if option.kind == "bool":
        return bool(value)
    if option.kind == "list":
        if value is None:
            return None
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        else:
            items = [str(item).strip() for item in value if str(item).strip()]
        return items or None
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if option.kind == "int":
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{option.label} must be a whole number")
    if option.kind == "float":
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"{option.label} must be a number")
        if option.scale != 1.0:
            return int(round(number * option.scale))
        return number
    if option.kind == "choice" and option.choices and text not in option.choices:
        raise ValueError(f"{option.label} must be one of {', '.join(option.choices)}")
    return text


def validate(type_name, values):
    errors = []
    for opt in options_for(type_name):
        value = values.get(opt.key)
        if opt.required and opt.kind != "bool" and (value is None or str(value).strip() == ""):
            errors.append(f"{opt.label} is required")
            continue
        try:
            parse_value(opt, value)
        except ValueError as error:
            errors.append(str(error))
    return errors


def entry_from_values(type_name, values, extras=None, original=None, enabled=True):
    original = original or {}
    entry = {"type": type_name, "interface_enabled": bool(enabled)}
    if "enabled" in original:
        entry["enabled"] = bool(enabled)
    for opt in options_for(type_name):
        value = parse_value(opt, values.get(opt.key))
        if opt.kind == "bool":
            if value or opt.key in original:
                entry[opt.key] = value
        elif value is not None:
            entry[opt.key] = value
    for key, value in (extras or {}).items():
        if key not in entry and key not in MANAGED_KEYS:
            entry[key] = value
    return entry


def describe_entry(entry):
    kind = str(entry.get("type", "?"))
    parts = []
    for key in ("target_host", "listen_ip", "port", "forward_ip", "command", "group_id"):
        if entry.get(key):
            value = entry.get(key)
            if key == "target_host" and entry.get("target_port"):
                value = f"{value}:{entry.get('target_port')}"
            if key == "listen_ip" and entry.get("listen_port"):
                value = f"{value}:{entry.get('listen_port')}"
            parts.append(str(value))
            break
    if kind == "RNodeInterface" and entry.get("frequency"):
        try:
            parts.append(f"{float(entry['frequency']) / 1000000:g} MHz")
        except (TypeError, ValueError):
            pass
    return " ".join(parts)


def is_enabled(entry):
    return entry_enabled(entry)


def list_ports():
    try:
        import serial.tools.list_ports as list_ports
    except ImportError:
        return []
    found = []
    try:
        ports = list_ports.comports()
    except Exception:
        return []
    for port in ports:
        description = ""
        if port.description and port.description != port.device:
            description = port.description
        if port.manufacturer and port.manufacturer not in description:
            description = f"{description} {port.manufacturer}".strip()
        generic = port.device.startswith("COM") or "/dev/ttyS" in port.device
        found.append({"device": port.device, "description": description, "generic": generic})
    found.sort(key=lambda port: (port["generic"], port["device"]))
    return found


def port_choices():
    ports = list_ports()
    if not ports:
        return []
    if any(not port["generic"] for port in ports):
        ports = [port for port in ports if not port["generic"]]
    return [port["device"] for port in ports]


PEER_PREFIXES = (
    "LocalInterface[",
    "TCPInterface[Client",
    "BackboneInterface[Client on",
    "AutoInterfacePeer[",
    "WeaveInterfacePeer[",
    "I2PInterfacePeer[Connected peer",
)


def mode_names():
    base = RNS.Interfaces.Interface.Interface
    names = {}
    for attribute, label in (
        ("MODE_FULL", "Full"),
        ("MODE_POINT_TO_POINT", "Point-to-Point"),
        ("MODE_ACCESS_POINT", "Access Point"),
        ("MODE_ROAMING", "Roaming"),
        ("MODE_BOUNDARY", "Boundary"),
        ("MODE_GATEWAY", "Gateway"),
        ("MODE_INTERNAL", "Internal"),
    ):
        value = getattr(base, attribute, None)
        if value is not None:
            names[value] = label
    return names


MODE_NAMES = mode_names()


def instance():
    try:
        return RNS.Reticulum.get_instance()
    except Exception:
        return None


def live_stats(reticulum=None):
    reticulum = reticulum or instance()
    if reticulum is None:
        return None
    try:
        return reticulum.get_interface_stats()
    except Exception:
        return None


def uses_shared_instance(reticulum=None):
    reticulum = reticulum or instance()
    return bool(reticulum and getattr(reticulum, "is_connected_to_shared_instance", False))


def config_name(ifstat):
    short = ifstat.get("short_name")
    if short:
        return str(short)
    name = str(ifstat.get("name", ""))
    start = name.find("[")
    end = name.rfind("]")
    if 0 <= start < end:
        return name[start + 1 : end]
    return name


def is_peer(ifstat):
    return str(ifstat.get("name", "")).startswith(PEER_PREFIXES)


def by_config_name(stats, include_peers=False):
    found = {}
    if not stats:
        return found
    for ifstat in stats.get("interfaces", []):
        if include_peers or not is_peer(ifstat):
            found.setdefault(config_name(ifstat), ifstat)
    return found


def is_up(ifstat):
    return bool(ifstat and ifstat.get("status"))


def mode_name(mode):
    return MODE_NAMES.get(mode, "Full")


def format_bytes(count):
    try:
        return RNS.prettysize(int(count or 0))
    except Exception:
        return str(count)


def format_speed(bps):
    try:
        bps = float(bps or 0)
    except (TypeError, ValueError):
        return "-"
    if bps <= 0:
        return "-"
    try:
        return RNS.prettyspeed(bps)
    except Exception:
        return f"{int(bps)} bps"


def format_rate(hz):
    try:
        hz = float(hz or 0)
    except (TypeError, ValueError):
        return "-"
    if hz <= 0:
        return "-"
    per_hour = hz * 3600
    if per_hour < 1:
        return "<1/hr"
    if per_hour < 100:
        return f"{round(per_hour)}/hr"
    return f"{round(hz * 60)}/min"


def format_percent(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "0"
    if value <= 1.0:
        value *= 100.0
    return str(int(round(value)))


def summary(ifstat):
    rows = [("Status", "Up" if is_up(ifstat) else "Down"), ("Mode", mode_name(ifstat.get("mode")))]
    if ifstat.get("bitrate") is not None:
        rows.append(("Bitrate", format_speed(ifstat.get("bitrate"))))
    rows.append(("Received", format_bytes(ifstat.get("rxb", 0))))
    rows.append(("Sent", format_bytes(ifstat.get("txb", 0))))
    if "rxs" in ifstat or "txs" in ifstat:
        rows.append(("Speed", f"RX {format_speed(ifstat.get('rxs'))}, TX {format_speed(ifstat.get('txs'))}"))
    if ifstat.get("incoming_announce_frequency") is not None:
        rows.append(
            (
                "Announces",
                f"in {format_rate(ifstat.get('incoming_announce_frequency'))}, "
                f"out {format_rate(ifstat.get('outgoing_announce_frequency'))}",
            )
        )
    if ifstat.get("incoming_pr_frequency") is not None:
        rows.append(
            (
                "Path requests",
                f"in {format_rate(ifstat.get('incoming_pr_frequency'))}, "
                f"out {format_rate(ifstat.get('outgoing_pr_frequency'))}",
            )
        )
    if ifstat.get("clients") is not None:
        rows.append(("Clients", str(ifstat.get("clients"))))
    if ifstat.get("peers") is not None:
        rows.append(("Peers", f"{ifstat.get('peers')} reachable"))
    if ifstat.get("ifac_netname") is not None or ifstat.get("ifac_signature") is not None:
        size = ifstat.get("ifac_size")
        bits = f"{size * 8}-bit IFAC" if size else "IFAC"
        network = ifstat.get("ifac_netname")
        rows.append(("Network", f"{network} ({bits})" if network else bits))
    if ifstat.get("announce_queue") is not None:
        rows.append(("Queued", f"{ifstat.get('announce_queue') or 0} announces"))
    if "held_announces" in ifstat:
        rows.append(("Held", f"{ifstat.get('held_announces') or 0} announces"))
    if ifstat.get("tunnelstate") is not None:
        rows.append(("I2P tunnel", str(ifstat.get("tunnelstate"))))
    if ifstat.get("i2p_b32"):
        rows.append(("I2P address", str(ifstat.get("i2p_b32"))))
    if "channel_load_short" in ifstat and "channel_load_long" in ifstat:
        rows.append(
            (
                "Channel load",
                f"{format_percent(ifstat.get('channel_load_short'))}% (15s), "
                f"{format_percent(ifstat.get('channel_load_long'))}% (1h)",
            )
        )
    if "airtime_short" in ifstat and "airtime_long" in ifstat:
        rows.append(
            (
                "Airtime",
                f"{format_percent(ifstat.get('airtime_short'))}% (15s), "
                f"{format_percent(ifstat.get('airtime_long'))}% (1h)",
            )
        )
    if "noise_floor" in ifstat:
        rows.append(("Noise floor", f"{ifstat.get('noise_floor')} dBm"))
    if ifstat.get("battery_percent") is not None:
        rows.append(("Battery", f"{ifstat.get('battery_percent')}% ({ifstat.get('battery_state', 'unknown')})"))
    cpu = []
    if ifstat.get("cpu_load") is not None:
        cpu.append(f"{ifstat.get('cpu_load')}% load")
    if ifstat.get("cpu_temp") is not None:
        cpu.append(f"{ifstat.get('cpu_temp')} C")
    if cpu:
        rows.append(("CPU", ", ".join(cpu)))
    if ifstat.get("mem_load") is not None:
        rows.append(("Memory", f"{ifstat.get('mem_load')}%"))
    return rows
