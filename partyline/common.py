import os
import re
import sys

import RNS
from RNS.vendor import umsgpack as msgpack
import LXST.Codecs
from LXST.Codecs import Opus, Codec2

# Python <3.10 runtime hax for pycodec2
if sys.version_info < (3, 11):

    class _SingleByteInt(int):
        def to_bytes(self, length=1, byteorder="big", *, signed=False):
            return int.to_bytes(self, length, byteorder, signed=signed)

    for constant_name in ("NULL", "RAW", "OPUS", "CODEC2"):
        if hasattr(LXST.Codecs, constant_name):
            setattr(LXST.Codecs, constant_name, _SingleByteInt(getattr(LXST.Codecs, constant_name)))
    Codec2.MODE_HEADERS = {mode: _SingleByteInt(header) for mode, header in Codec2.MODE_HEADERS.items()}


CODEC_BYTES = {Opus: bytes([LXST.Codecs.OPUS]), Codec2: bytes([LXST.Codecs.CODEC2])}
CODEC_CLASSES = {header[0]: codec_class for codec_class, header in CODEC_BYTES.items()}


def codec_header_byte(codec_class):
    for base_class in codec_class.__mro__:
        if base_class in CODEC_BYTES:
            return CODEC_BYTES[base_class]
    raise TypeError(f"no header byte for pycodec2")



def codec_type(header_byte):
    return CODEC_CLASSES.get(header_byte)


### MAIN ###
APP_TITLE = "Partyline"
APP_VERSION = "1.0.0"  
ASPECT = "room"  



PROTOCOL_VERSION = (
    1  
)

CONFIG_DIR = os.path.expanduser("~/.config/partyline")


### MSGPACK KEYS ###
FIELD_FRAMES = 0x01     # codec header byte + encoded frame
FIELD_SPEAKER = 0x02    # server -> client member id the frame came from
FIELD_ROOM = 0x03       # server -> client: [room_id, profile, frame_ms] for the room you are now in | all None = no room
FIELD_HELLO = 0x05      # client -> server after link.identify: {"name", "room", "password", "server_password", "text_only", "muted", "deaf", "hops", "rtt", "ver"}
FIELD_WELCOME = 0x06    # server -> client: {"name", "sid", "motd", "ver"}
FIELD_CHANNEL = 0x07    # server -> client: [room_id, name, profile, frame_ms, access_flags, description, dialin_number]
FIELD_USER = 0x08       # server -> client: [member_id, name, identity_hex, room_id, muted, deaf, hops, rtt_ms, operator, server_muted, text_only]
FIELD_USER_LEFT = 0x09  # server -> client: member_id
FIELD_MOVE = 0x0A       # client -> server: [room_id, password_or_None]
FIELD_DENIED = 0x0B     # server -> client: [room_id_or_None, reason]
FIELD_TEXT = 0x0C       # client -> server: text;  server -> client: [member_id, text] 
FIELD_STATE = 0x0D      # client -> server: [muted, deaf]
FIELD_SYNCED = 0x0E     # server -> client: the initial channel and user lists are complete
FIELD_POKE = 0x0F       # client -> server: [target_member_id, text];  server -> target: [from_member_id, text]
FIELD_SEQ = 0x10        # 16-bit frame counter of the senderr
FIELD_ADMIN = 0x11      # client -> server (operators only): [action, target_member_id, argument]
FIELD_NOTICE = 0x12     # server -> client: a message for the user, e.g. "you were muted by an operator"
FIELD_TALK_END = 0x13   # client -> server: True when push-to-talk or the voice gate closes; server -> room: member_id


# FIELD_CHANNEL access flags. 0 means anyone who can reach the server can enter
ACCESS_IDENTITY = 1   # must have identified over the link (anonymous links are refused)
ACCESS_ALLOWLIST = 2  # identity must be on the room's allow list
ACCESS_PASSWORD = 4   # must present the room password


# announce app_data flags: what a browser can tell about a server before connecting
ANNOUNCE_HIDDEN = 1  # do not list in server browsers (still reachable by hash)
ANNOUNCE_PASSWORD = 2  # a server password is needed to connect
ANNOUNCE_ALLOWLIST = 4  # only listed identities may connect


# FIELD_ADMIN actions
ADMIN_KICK = "kick"
ADMIN_BAN = "ban"
ADMIN_MUTE = "mute"
ADMIN_UNMUTE = "unmute"
ADMIN_OP = "op"
ADMIN_DEOP = "deop"
ADMIN_MOVE = "move"  
ADMIN_ACTIONS = (ADMIN_KICK, ADMIN_BAN, ADMIN_MUTE, ADMIN_UNMUTE, ADMIN_OP, ADMIN_DEOP, ADMIN_MOVE)

MAX_FRAME_BYTES = 400
# text limits for messages

MAX_NAME = 48
MAX_TEXT = 300
MAX_DESCRIPTION = 120

# codec class, codec argument, frame ms, description
PROFILES = {
    "opus-high": (Opus, Opus.PROFILE_VOICE_HIGH, 20, "Opus 16 kbps, 20 ms frames"),
    "opus-med": (Opus, Opus.PROFILE_VOICE_MEDIUM, 60, "Opus 8 kbps, 60 ms frames"),
    "opus-low": (Opus, Opus.PROFILE_VOICE_LOW, 60, "Opus 6 kbps, 60 ms frames"),
    "c2-3200": (Codec2, Codec2.CODEC2_3200, 200, "Codec2 3200 bps, 200 ms frames"),
    "c2-2400": (Codec2, Codec2.CODEC2_2400, 200, "Codec2 2400 bps, 200 ms frames"),
    "c2-1200": (Codec2, Codec2.CODEC2_1200, 400, "Codec2 1200 bps, 400 ms frames"),
    "c2-700": (Codec2, Codec2.CODEC2_700C, 400, "Codec2 700 bps, 400 ms frames"),
}


def make_codec(profile_name):
    codec_class, codec_argument, _, _ = PROFILES[profile_name]
    return codec_class(codec_argument)


def codec_byte(profile_name):
    return codec_header_byte(PROFILES[profile_name][0])


def frame_ms(profile_name):
    return PROFILES[profile_name][2]




def describe(profile_name):
    if profile_name in PROFILES:
        return PROFILES[profile_name][3]
    return str(profile_name)



def opus_duration_ms(packet):
    if len(packet) < 1:
        return None
    toc = packet[0]
    config = toc >> 3
    code = toc & 3
    if config < 12:  # SILK
        size = (10, 20, 40, 60)[config % 4]
    elif config < 16:  # hybrid
        size = (10, 20)[config % 2]
    else:  # CELT
        size = (2.5, 5, 10, 20)[config % 4]

    if code == 0:
        count = 1
    elif code < 3:
        count = 2
    else:
        if len(packet) < 2:
            return None
        count = packet[1] & 0x3F
    return size * count


_codec2_frame_info = {}


def codec2_duration_ms(payload):
    import pycodec2

    if len(payload) < 2 or payload[0] not in Codec2.HEADER_MODES:
        return None
    mode = Codec2.HEADER_MODES[payload[0]]
    if mode not in _codec2_frame_info:
        codec = pycodec2.Codec2(mode)
        _codec2_frame_info[mode] = (codec.bytes_per_frame(), codec.samples_per_frame() / 8)  # 8 kHz
    bytes_per_frame, ms_per_frame = _codec2_frame_info[mode]
    body_length = len(payload) - 1
    if body_length % bytes_per_frame:
        return None
    return (body_length // bytes_per_frame) * ms_per_frame


def frame_duration_ms(frame):
    codec_class = codec_type(frame[0]) if frame else None
    if codec_class is Opus:
        return opus_duration_ms(frame[1:])
    if codec_class is Codec2:
        return codec2_duration_ms(frame[1:])
    return None


def valid_frame(frame, profile_name):
    if type(frame) is not bytes:
        return False
    if not 2 <= len(frame) <= MAX_FRAME_BYTES:
        return False
    if frame[0:1] != codec_byte(profile_name):
        return False
    return frame_duration_ms(frame) == frame_ms(profile_name)


_unprintable = re.compile(r"[\x00-\x1f\x7f-\x9f\u00a0\u2007\u202f]")


def clean_text(text, limit, fallback=""):
    if not isinstance(text, str):
        return fallback
    text = " ".join(_unprintable.sub("", text).split())
    if not text:
        return fallback
    encoded = text.encode("utf-8")
    if len(encoded) > limit:
        text = encoded[:limit].decode("utf-8", errors="ignore")  # cut on a character boundary
    return text


def text_bytes(text):
    return len(text.encode("utf-8"))


def clean_name(name, fallback):
    return clean_text(name, MAX_NAME, fallback)


def parse_hash(text):
    hash_bytes = bytes.fromhex(text.strip())
    if len(hash_bytes) != RNS.Reticulum.TRUNCATED_HASHLENGTH // 8:
        raise ValueError("hash must be 32 hex characters")
    return hash_bytes


def load_identity(path):
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        identity = RNS.Identity.from_file(path)
        if identity is None:
            raise SystemExit(f"could not read identity file {path}")
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        identity = RNS.Identity()
        identity.to_file(path)
    os.chmod(path, 0o600)
    return identity


def load_hash_list(hashes=(), path=None):
    lines = list(hashes or [])
    if path:
        with open(os.path.expanduser(path)) as list_file:
            for line in list_file:
                without_comment = line.split("#")[0]
                if without_comment.strip():
                    lines.append(without_comment)
    return {parse_hash(line) for line in lines}


def save_hash_list(path, hashes):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary_path = path + ".tmp"
    with open(temporary_path, "w") as list_file:
        for hash_bytes in sorted(hashes):
            list_file.write(hash_bytes.hex() + "\n")
    os.replace(temporary_path, path)



def pack_announce(server_name, flags=0):
    return msgpack.packb([PROTOCOL_VERSION, server_name, int(flags)])



def unpack_announce(app_data):
    try:
        unpacked = msgpack.unpackb(app_data)
        version = unpacked[0]
        name = unpacked[1]
    except Exception:
        return None
    if not isinstance(version, int):
        return None
    flags = 0
    if len(unpacked) > 2 and isinstance(unpacked[2], int):
        flags = unpacked[2]
    return {"name": clean_name(name, "") or None, "flags": flags, "version": version}


def is_room_destination(destination_hash, identity):
    try:
        from LXST import APP_NAME

        return RNS.Destination.hash_from_name_and_identity(f"{APP_NAME}.{ASPECT}", identity) == destination_hash
    except Exception:
        return False
