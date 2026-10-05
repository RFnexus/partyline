#!/usr/bin/env python3
import argparse
import hmac
import json
import math
import os
import queue
import threading
import time

import RNS
from RNS.vendor import umsgpack as msgpack
from LXST import APP_NAME

from .common import *

HELLO_TIMEOUT = 25.0    # seconds a new link has to send FIELD_HELLO before it is dropped
IDENTITY_GRACE = 2.0    # seconds to wait for link.identify to land when a join needs the identity
TEXT_RATE = 4.0         # text messages per second per member
CONTROL_RATE = 6.0      # membership and mute/deafen changes per second limit
DENY_CLOSE_DELAY = 0.5  # grace to let the FIELD_DENIED packet leave before tearing the link down 
MAX_PENDING_LINKS = 16
PASSWORD_FREE_TRIES = 2
PASSWORD_BACKOFF = 2.0
PASSWORD_BACKOFF_MAX = 300.0
PASSWORD_FORGET = 3600.0
MAX_PASSWORD_RECORDS = 4096
WRONG_PASSWORD = "wrong password"

OPERATORS_FILENAME = "operators.txt" #
BANNED_FILENAME = "banned.txt"
SAVED_CONFIG_FILENAME = "server.json"

# kept for older imports
load_allow_list = load_hash_list


def same_password(given, expected):
    if not isinstance(given, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def room_spec(payload, partial=False):
    if not isinstance(payload, dict):
        raise ValueError("room settings must be a map")
    spec = {}
    for key, value in payload.items():
        if key not in ROOM_SPEC_KEYS:
            raise ValueError(f"unknown room setting {key!r}")
        if value is None:
            if partial:
                spec[key] = None
            continue
        if key in ("name", "profile", "description", "password"):
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            if key == "profile" and value not in PROFILES:
                raise ValueError(f"unknown profile {value!r}")
            if key == "name" and not clean_name(value, ""):
                raise ValueError("a room needs a name")
            spec[key] = value
        elif key in ("require_identity", "ptt"):
            spec[key] = bool(value)
        elif key in ("max_members", "ptt_jitter_ms"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be a whole number")
            if key == "max_members" and not 1 <= value <= MAX_ROOM_MEMBERS:
                raise ValueError(f"max_members must be between 1 and {MAX_ROOM_MEMBERS}")
            spec[key] = value
        else:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"{key} must be a list of identity hashes")
            spec[key] = [parse_hash(item).hex() for item in value]
    if not partial and not spec.get("name"):
        raise ValueError("a room needs a name")
    return spec


class Room:
    def __init__(self, room_id, spec, default_profile, default_max_members, default_require_identity=False):

        self.id = room_id
        self.spec = dict(spec)
        self.name = clean_name(spec.get("name"), f"Room {room_id}")
        self.description = clean_text(spec.get("description", ""), MAX_DESCRIPTION)
        self.profile = spec.get("profile") or default_profile

        if self.profile not in PROFILES:
            raise SystemExit(f"room {self.name!r}: unknown profile {self.profile!r}")

        self.frame_ms = frame_ms(self.profile)
        self.ptt = bool(spec.get("ptt", "ptt_jitter_ms" in spec))
        self.ptt_jitter_ms = 0
        if self.ptt:
            try:
                self.ptt_jitter_ms = int(spec.get("ptt_jitter_ms", PTT_JITTER_MS))
            except (TypeError, ValueError):
                raise SystemExit(f"room {self.name!r}: ptt_jitter_ms must be a number of milliseconds")
            if not 1 <= self.ptt_jitter_ms <= MAX_JITTER_MS:
                raise SystemExit(f"room {self.name!r}: ptt_jitter_ms must be between 1 and {MAX_JITTER_MS}")
        self.password = spec.get("password") or None

        self.require_identity = bool(spec.get("require_identity", default_require_identity))

        if spec.get("allow") or spec.get("allowed_file"):
            self.allow = self.hash_list("allow", spec.get("allow"), spec.get("allowed_file"))
        else:
            self.allow = None


        self.max_members = int(spec.get("max_members") or default_max_members)
        if spec.get("music") is not None:
            self.music_speakers = self.hash_list("music", spec.get("music"))
        else:
            self.music_speakers = None
        self.members = set()

    def hash_list(self, key, hashes, path=None):
        try:
            return load_hash_list(hashes, path)
        except (ValueError, OSError) as error:
            raise SystemExit(f"room {self.name!r}: bad {key} list: {error}")

    @property
    def flags(self):
        return ROOM_BROADCAST if self.music_speakers is not None else 0

    @property
    def access(self):
        flags = 0
        if self.require_identity or self.allow is not None:
            flags |= ACCESS_IDENTITY
        if self.allow is not None:
            flags |= ACCESS_ALLOWLIST
        if self.password:
            flags |= ACCESS_PASSWORD
        return flags


    def check(self, member, password):
        if self.access & ACCESS_IDENTITY and member.identity is None:
            return "room requires an identified user"
        if self.allow is not None and member.identity.hash not in self.allow:
            return "you are not on this room's allow list"
        if self.password and not same_password(password, self.password):
            return WRONG_PASSWORD
        if len(self.members) >= self.max_members:
            return "room is full"
        return None

    def can_speak(self, member):
        if self.music_speakers is None:
            return True
        return member.identity is not None and member.identity.hash in self.music_speakers

    def as_channel(self, dialin_number=None):
        return [
            self.id,
            self.name,
            self.profile,
            self.frame_ms,
            self.access,
            self.description,
            dialin_number,
            self.ptt_jitter_ms,
            self.flags,
        ]

    def __str__(self):
        parts = [describe(self.profile)]
        if self.ptt:
            parts.append(f"push to talk with a {self.ptt_jitter_ms} ms buffer")
        if self.music_speakers is not None:
            parts.append(f"broadcast by {len(self.music_speakers)} listed speakers")
        if self.require_identity:
            parts.append("identified users")
        if self.allow is not None:
            parts.append(f"allow list of {len(self.allow)}")
        if self.password:
            parts.append("password")
        return f"{self.name} ({', '.join(parts)})"


class Member:
    def __init__(self, link, member_id, bridge=None):

        self.link = link
        self.member_id = member_id
        self.bridge = bridge  # a dialin call instead of a link

        self.identity = None
        self.name = f"guest-{member_id}"
        self.room = None

        self.muted = False
        self.deaf = False
        self.server_muted = False

        self.operator = False

        self.text_only = False  # for keyboard only
        self.speaker = True

        self.hops = None
        self.rtt = None
        self.admitted = False
        self.hello_started = False
        self.joined_at = time.time()
        self.rejected_frames = 0
        self.limited_frames = 0

        self.rate = 0.0
        self.tokens = 0.0
        self.capacity = 0.0
        self.talking = False

        self.last_refill = time.time()
        self.text_tokens = TEXT_RATE
        self.text_last_refill = time.time()
        self.control_tokens = CONTROL_RATE
        self.control_last_refill = time.time()

    def set_room(self, room):
        if self.room:
            self.room.members.discard(self)
        self.room = room
        self.talking = False
        if room:
            room.members.add(self)
            self.rate = 2 * 1000 / room.frame_ms
            self.capacity = max(2 * self.rate, MAX_BATCH)
        else:
            self.rate = 0.0
            self.capacity = 0.0
        self.tokens = self.capacity
        self.last_refill = time.time()

    def allow_frames(self, count):
        now = time.time()
        self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.rate)
        self.last_refill = now
        if self.tokens >= count:
            self.tokens -= count
            return True
        return False

    def allow_text(self):
        now = time.time()
        self.text_tokens = min(TEXT_RATE, self.text_tokens + (now - self.text_last_refill) * TEXT_RATE)
        self.text_last_refill = now
        if self.text_tokens >= 1:
            self.text_tokens -= 1
            return True
        return False

    def allow_control(self):
        now = time.time()
        self.control_tokens = min(CONTROL_RATE, self.control_tokens + (now - self.control_last_refill) * CONTROL_RATE)
        self.control_last_refill = now
        if self.control_tokens >= 1:
            self.control_tokens -= 1
            return True
        return False

    def as_user(self):
        identity_hex = self.identity.hash.hex() if self.identity else None
        room_id = self.room.id if self.room else None
        return [
            self.member_id,
            self.name,
            identity_hex,
            room_id,
            self.muted,
            self.deaf,
            self.hops,
            self.rtt,
            self.operator,
            self.server_muted,
            self.text_only,
            self.speaker,
        ]

    def label(self):
        if self.identity:
            return f"{self.name} #{self.member_id} <{self.identity.hash.hex()}>"
        return f"{self.name} #{self.member_id} (anonymous)"


class Server:
    def __init__(self, identity, config, state_dir=CONFIG_DIR, config_path=None):

        self.state_dir = state_dir
        self.config_path = config_path
        self.operators_file = os.path.join(state_dir, OPERATORS_FILENAME)
        self.banned_file = os.path.join(state_dir, BANNED_FILENAME)

        self.name = clean_name(config.get("name"), "Partyline server")
        self.motd = clean_text(config.get("motd", ""), MAX_TEXT)
        self.description = clean_text(config.get("description", ""), MAX_ANNOUNCE_DESCRIPTION)
        self.language = clean_text(config.get("language", ""), MAX_LOCALE)
        self.country = clean_text(config.get("country", ""), MAX_LOCALE)

        self.profile = config.get("profile", "opus-high")

        if self.profile not in PROFILES:
            raise SystemExit(f"unknown profile {self.profile!r}")

        self.max_members = int(config.get("max_members", 32))
        self.password = config.get("password") or None  
        self.hidden = bool(config.get("hidden", False))  
        if config.get("allow") or config.get("allowed_file"):
            self.allowed = load_hash_list(config.get("allow"), config.get("allowed_file"))
        else:
            self.allowed = None

        self.operators = load_hash_list(config.get("ops"))
        if os.path.isfile(self.operators_file):
            self.operators |= load_hash_list(path=self.operators_file)
        self.banned = set()
        if os.path.isfile(self.banned_file):
            self.banned = load_hash_list(path=self.banned_file)
        self.password_failures = {}

        self.require_identity = bool(config.get("require_identity", False))
        room_specs = config.get("rooms") or [{"name": "Lobby"}]
        self.rooms = {}
        for room_id, spec in enumerate(room_specs, 1):
            self.rooms[room_id] = Room(room_id, spec, self.profile, self.max_members, self.require_identity)
        self.default_room = self.rooms[1]

        self.members = {}  # keyed by either link or bridge if rnphone

        self.next_member_id = 1

        self.lock = threading.RLock()

        self.dialin = None



        self.rx_packets = 0
        self.rx_bytes = 0
        self.tx_packets = 0
        self.tx_bytes = 0

        self.destination = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        if self.hidden:
            self.destination.set_default_app_data(pack_announce("", self.announce_flags()))
        else:
            self.destination.set_default_app_data(
                pack_announce(self.name, self.announce_flags(), self.description, self.language, self.country)
            )
        self.destination.set_link_established_callback(self.link_established)

        # queue
        self.inbox = queue.Queue()
        threading.Thread(target=self.worker, daemon=True).start()


        dialin_spec = config.get("dialin")
        if isinstance(dialin_spec, dict) and dialin_spec.get("enabled"):
            from .dialin import DialIn

            self.dialin = DialIn(self, identity, dialin_spec)

    def announce_flags(self):
        flags = 0
        if self.hidden:
            flags |= ANNOUNCE_HIDDEN
        if self.password:
            flags |= ANNOUNCE_PASSWORD
        if self.allowed is not None:
            flags |= ANNOUNCE_ALLOWLIST
        return flags

#### MEMBERSHIP ####


    def link_established(self, link):
        with self.lock:
            if self.member_count() >= self.max_members:
                RNS.log("Server full, refusing link", RNS.LOG_NOTICE)
                self.deny_and_close(link, None, "server is full")
                return
            waiting = [member for member in self.members.values() if not member.admitted]
            if len(waiting) >= MAX_PENDING_LINKS:
                idle = [member for member in waiting if not member.hello_started]
                if not idle:
                    RNS.log("Too many links are waiting to join, refusing link", RNS.LOG_NOTICE)
                    self.deny_and_close(link, None, "server is busy, try again shortly")
                    return
                oldest = min(idle, key=lambda member: member.joined_at)
                RNS.log(f"member {oldest.member_id} dropped to make room for a new link", RNS.LOG_NOTICE)
                self.evict(oldest)
            member = Member(link, self.next_member_id)
            self.next_member_id += 1
            self.members[link] = member
        link.set_packet_callback(lambda data, packet, link=link: self.packet(link, data, packet))
        link.set_link_closed_callback(self.link_closed)
        link.set_remote_identified_callback(self.identified)
        link.set_resource_strategy(RNS.Link.ACCEPT_APP)
        link.set_resource_callback(self.accept_resource)
        link.set_resource_concluded_callback(self.resource_concluded)
        threading.Timer(HELLO_TIMEOUT, self.hello_timeout, [link]).start()


    def identified(self, link, identity):
        with self.lock:
            member = self.members.get(link)
            if member is None:
                return
            known = member.identity is not None
            member.identity = identity
            if identity.hash in self.banned:
                self.drop_banned(member)
                return
            if known or not member.admitted:
                return
            self.evict_older_sessions(member)
            member.operator = identity.hash in self.operators
            room = member.room
            if room is not None and room.can_speak(member) != member.speaker:
                member.speaker = room.can_speak(member)
                self.send(link, {FIELD_ROOM: [room.id, room.profile, room.frame_ms, member.speaker]})
            self.broadcast({FIELD_USER: member.as_user()})

    def drop_banned(self, member):
        RNS.log(f"{member.label()} is banned, dropping", RNS.LOG_NOTICE)
        self.remove_member(member.link)
        self.deny_and_close(member.link, None, "you are banned from this server")

    def evict_older_sessions(self, member):
        for other in list(self.members.values()):
            same_identity = other.identity is not None and other.identity.hash == member.identity.hash
            if other is not member and other.admitted and same_identity:
                RNS.log(f"{other.label()} replaced by a new session, dropping the old one", RNS.LOG_NOTICE)
                self.evict(other)


    def hello_timeout(self, link):
        with self.lock:
            member = self.members.get(link)
        if member and not member.admitted:
            RNS.log(f"member {member.member_id} sent no hello in time, dropping", RNS.LOG_NOTICE)
            link.teardown()


    def hello(self, member, fields):
        if not isinstance(fields, dict):
            return
        wanted_room = fields.get("room")
        password = fields.get("password")
        if wanted_room is not None:
            room = self.find_room(wanted_room)
        else:
            room = self.default_room

        needs_identity = (
            self.allowed is not None
            or self.password is not None
            or not self.admits_guests()
            or (room is not None and (room.access & ACCESS_IDENTITY or room.password))
        )
        link_rtt = getattr(member.link, "rtt", None) or 0.0
        deadline = time.time() + max(IDENTITY_GRACE, 4 * link_rtt)  



        while needs_identity and member.identity is None and time.time() < deadline:
            time.sleep(0.05)
            if member.link.get_remote_identity():
                member.identity = member.link.get_remote_identity()

        with self.lock:
            if member.link not in self.members or member.admitted:
                return

            client_protocol = fields.get("ver")
            if client_protocol != PROTOCOL_VERSION:
                RNS.log(
                    f"{member.label()} speaks protocol {client_protocol!r}, we need {PROTOCOL_VERSION}, dropping",
                    RNS.LOG_NOTICE,
                )
                self.deny_and_close(
                    member.link,
                    None,
                    f"this server speaks Partyline protocol {PROTOCOL_VERSION}, "
                    f"your client speaks {client_protocol}: Please update",
                )
                return
            client_app = fields.get("app")
            if client_app != APP_VERSION:
                RNS.log(
                    f"{member.label()} runs Partyline {client_app!r}, this server runs {APP_VERSION}", RNS.LOG_NOTICE
                )

            if member.identity is not None and member.identity.hash in self.banned:
                self.drop_banned(member)
                return
            if self.allowed is not None:
                if member.identity is None or member.identity.hash not in self.allowed:
                    RNS.log(f"{member.label()} is not on the server allow list, dropping", RNS.LOG_NOTICE)
                    self.deny_and_close(member.link, None, "you are not on this server's allow list")
                    return
            if member.identity is None and not self.admits_guests():
                RNS.log(f"{member.label()} did not identify and no room takes guests, dropping", RNS.LOG_NOTICE)
                self.deny_and_close(member.link, None, "this server requires an identified user")
                return

            if self.password:
                wait = self.password_wait(None, member)
                if wait:
                    self.deny_and_close(member.link, None, f"too many wrong passwords, try again in {wait} s")
                    return
                given = fields.get("server_password")
                if not same_password(given, self.password):
                    if isinstance(given, str):
                        self.password_failed(None, member)
                    RNS.log(f"{member.label()} gave a wrong server password, dropping", RNS.LOG_NOTICE)
                    self.deny_and_close(member.link, None, "wrong server password")
                    return
                self.password_passed(None, member)

            member.name = self.unique_name(clean_name(fields.get("name"), f"guest-{member.member_id}"), member)
            member.text_only = bool(fields.get("text_only", False))
            member.muted = bool(fields.get("muted", False)) or member.text_only
            member.deaf = bool(fields.get("deaf", False)) or member.text_only
            hops = fields.get("hops")
            if isinstance(hops, int) and 0 <= hops < 256:
                member.hops = hops
            rtt = fields.get("rtt")
            if isinstance(rtt, (int, float)) and 0 <= rtt < 1e6:
                member.rtt = int(rtt)

            if self.member_count() >= self.max_members:
                RNS.log(f"Server full, refusing {member.label()}", RNS.LOG_NOTICE)
                self.deny_and_close(member.link, None, "server is full")
                return

            member.operator = member.identity is not None and member.identity.hash in self.operators
            member.admitted = True
            try:
                self.admit(member, room, wanted_room, password)
            except Exception as error:
                RNS.log(f"Could not admit {member.label()}: {error}, dropping", RNS.LOG_ERROR)
                self.evict(member)

    def admit(self, member, room, wanted_room, password):
        if member.identity is not None:
            self.evict_older_sessions(member)

        welcome = {
            "name": self.name,
            "sid": member.member_id,
            "motd": self.motd,
            "ver": PROTOCOL_VERSION,
            "app": APP_VERSION,
        }
        self.send(member.link, {FIELD_WELCOME: welcome})
        channels = [self.channel_record(existing_room) for existing_room in self.rooms.values()]
        users = [other.as_user() for other in self.members.values() if other.admitted and other is not member]
        self.send_records(member.link, FIELD_CHANNEL, channels)
        self.send_records(member.link, FIELD_USER, users)
        self.send(member.link, {FIELD_SYNCED: True})

        if room is not None:
            reason = self.join(member, room, password)
        else:
            wanted = wanted_room if isinstance(wanted_room, int) else clean_name(wanted_room, "")
            reason = f"no such room {wanted}".strip()
        if reason is not None:
            if room is None:
                self.notice(member, reason)
            else:
                self.send(member.link, {FIELD_DENIED: [room.id, reason]})
            if room is self.default_room or self.enter(member, self.default_room, None) is not None:
                self.send(member.link, {FIELD_ROOM: [None, None, None, True]})

        self.broadcast({FIELD_USER: member.as_user()})
        room_name = member.room.name if member.room else "no room"
        RNS.log(f"{member.label()} joined {room_name}, {self.member_count()} on server", RNS.LOG_NOTICE)

    def admits_guests(self):
        return any(not room.access & ACCESS_IDENTITY for room in list(self.rooms.values()))

    def unique_name(self, name, member):
        taken = set()
        for other in self.members.values():
            replaced = (
                member.identity is not None
                and other.identity is not None
                and other.identity.hash == member.identity.hash
            )
            if other is not member and other.admitted and not replaced:
                taken.add(name_key(other.name))
        candidate = name
        number = 2
        while name_key(candidate) in taken:
            tail = f" ({number})"
            candidate = clean_text(name, MAX_NAME - len(tail)) + tail
            number += 1
        return candidate

    def password_key(self, target, member):
        return target, member.identity.hash if member.identity else None

    def password_wait(self, target, member):
        failures, retry_at = self.password_failures.get(self.password_key(target, member), (0, 0.0))
        return max(0, math.ceil(retry_at - time.time()))

    def password_failed(self, target, member):
        key = self.password_key(target, member)
        failures, retry_at = self.password_failures.pop(key, (0, 0.0))
        now = time.time()
        if now - retry_at > PASSWORD_FORGET:
            failures = 0
        failures += 1
        delay = 0.0
        if failures > PASSWORD_FREE_TRIES:
            delay = min(PASSWORD_BACKOFF_MAX, PASSWORD_BACKOFF * 2 ** min(failures - PASSWORD_FREE_TRIES - 1, 16))
        self.password_failures[key] = (failures, now + delay)
        while len(self.password_failures) > MAX_PASSWORD_RECORDS:
            del self.password_failures[next(iter(self.password_failures))]

    def password_passed(self, target, member):
        self.password_failures.pop(self.password_key(target, member), None)

    def join(self, member, room, password):
        if room.password:
            wait = self.password_wait(room.id, member)
            if wait:
                return f"too many wrong passwords, try again in {wait} s"
        reason = self.enter(member, room, password)
        if reason == WRONG_PASSWORD and isinstance(password, str):
            self.password_failed(room.id, member)
        elif reason is None and room.password:
            self.password_passed(room.id, member)
        return reason

    def enter(self, member, room, password):
        # Move a member into the room when allowed
        reason = room.check(member, password)
        if reason is not None:
            return reason
        member.set_room(room)
        member.speaker = room.can_speak(member)
        self.send(member.link, {FIELD_ROOM: [room.id, room.profile, room.frame_ms, member.speaker]})
        return None

    def move(self, member, fields):
        if not member.allow_control():
            return
        try:
            room_id = fields[0]
            password = fields[1]
        except Exception:
            return
        with self.lock:
            room = self.rooms.get(room_id) if isinstance(room_id, int) else None
            if room is None:
                self.send(member.link, {FIELD_DENIED: [room_id, "no such room"]})
                return
            if room is member.room:
                return
            reason = self.join(member, room, password)
            if reason is not None:
                self.send(member.link, {FIELD_DENIED: [room.id, reason]})
                return
            self.broadcast({FIELD_USER: member.as_user()})
            RNS.log(f"{member.label()} moved to {room.name}", RNS.LOG_NOTICE)

    def link_closed(self, link):
        self.remove_member(link)

    def evict(self, member):
        key = member.bridge if member.link is None else member.link
        self.remove_member(key)
        if member.link is not None and member.link.status == RNS.Link.ACTIVE:
            member.link.teardown()
        elif member.bridge is not None:
            member.bridge.hangup()

    def remove_member(self, key):
        with self.lock:
            member = self.members.pop(key, None)
            if member is None:
                return
            member.set_room(None)
            if member.admitted:
                self.broadcast({FIELD_USER_LEFT: member.member_id})
        if member.admitted:
            RNS.log(
                f"{member.label()} left, {self.member_count()} on server "
                f"(rejected {member.rejected_frames} frames, rate limited {member.limited_frames})",
                RNS.LOG_NOTICE,
            )

    def add_bridge_member(self, bridge, identity, name, room, rtt=None):
        # Dial-iner's as members 
        with self.lock:
            if self.member_count() >= self.max_members:
                return None, "server is full"
            if identity.hash in self.banned:
                return None, "banned"
            member = Member(None, self.next_member_id, bridge)
            self.next_member_id += 1
            member.identity = identity
            member.name = self.unique_name(name, member)
            member.rtt = rtt
            member.admitted = True
            reason = self.enter(member, room, room.password)
            if reason is not None:
                return None, reason
            self.members[bridge] = member
            self.broadcast({FIELD_USER: member.as_user()})
            RNS.log(f"{member.label()} joined {room.name} by phone, {self.member_count()} on server", RNS.LOG_NOTICE)
            return member, None

    def deny_and_close(self, link, room_id, reason):
        self.send(link, {FIELD_DENIED: [room_id, reason]})
        link_rtt = getattr(link, "rtt", None) or 0.0
        threading.Timer(max(DENY_CLOSE_DELAY, 2 * link_rtt), link.teardown).start()  # let the reason arrive first

    def find_room(self, key):
        if isinstance(key, int):
            return self.rooms.get(key)
        if isinstance(key, str):
            for room in self.rooms.values():
                if room.name.lower() == key.strip().lower():
                    return room
        return None


    def find_member(self, member_id):
        for member in self.members.values():
            if member.admitted and member.member_id == member_id:
                return member
        return None

    def member_count(self):
        with self.lock:
            return sum(1 for member in self.members.values() if member.admitted)

    def channel_record(self, room):
        dialin_number = None
        if self.dialin is not None and self.dialin.room is room:
            dialin_number = self.dialin.number
        return room.as_channel(dialin_number)





#### PACKET STRUCTURE ####
    def packet(self, link, data, packet):
        self.inbox.put((link, data, packet))

    def accept_resource(self, advertisement):
        with self.lock:
            member = self.members.get(advertisement.link)
        if member is None or not member.admitted or not member.operator:
            return False
        return advertisement.get_data_size() <= MAX_CONFIG_BYTES

    def resource_concluded(self, resource):
        if resource.status != RNS.Resource.COMPLETE:
            return
        data = resource.data.read(MAX_CONFIG_BYTES + 1)
        if len(data) <= MAX_CONFIG_BYTES:
            self.inbox.put((resource.link, data, None))

    def worker(self):
        while True:
            link, data, packet = self.inbox.get()
            try:
                self.handle(link, data, packet)
            except Exception as error:
                RNS.log(f"Packet handling error: {error}", RNS.LOG_ERROR)

    def handle(self, link, data, packet):
        with self.lock:
            member = self.members.get(link)
        if member is None:
            return
        try:
            fields = msgpack.unpackb(data)
        except Exception:
            member.rejected_frames += 1
            return
        if type(fields) is not dict:
            return
        if not member.admitted:
            if FIELD_HELLO in fields:
                with self.lock:
                    already = member.hello_started
                    member.hello_started = True
                if not already:
                    threading.Thread(target=self.hello, args=(member, fields[FIELD_HELLO]), daemon=True).start()
            return
        if packet is None:
            if FIELD_CONFIG in fields:
                self.config_request(member, fields[FIELD_CONFIG])
            return

        if FIELD_FRAMES in fields:
            raw_length = len(packet.raw) if packet.raw else len(data)
            self.relay(member, fields[FIELD_FRAMES], raw_length, fields.get(FIELD_SEQ))
        if FIELD_MOVE in fields:
            self.move(member, fields[FIELD_MOVE])
        if FIELD_TEXT in fields:
            self.text(member, fields[FIELD_TEXT])
        if FIELD_STATE in fields:
            self.state(member, fields[FIELD_STATE])
        if FIELD_POKE in fields:
            self.poke(member, fields[FIELD_POKE])
        if FIELD_ADMIN in fields:
            self.admin(member, fields[FIELD_ADMIN])
        if FIELD_DIALIN in fields:
            self.dialin_request(member, fields[FIELD_DIALIN])
        if FIELD_CONFIG in fields:
            self.config_request(member, fields[FIELD_CONFIG])
        if FIELD_TALK_END in fields:
            self.relay_end(member)

    def relay(self, member, frames, raw_length, sequence=None):
        room = member.room
        if room is None or member.server_muted or not member.speaker:
            return
        batch = frames if isinstance(frames, list) else [frames]
        if not 1 <= len(batch) <= MAX_BATCH:
            member.rejected_frames += 1
            return
        if not member.allow_frames(len(batch)):
            member.limited_frames += len(batch)
            return
        for frame in batch:
            if not valid_frame(frame, room.profile):
                member.rejected_frames += 1
                return

        member.talking = True
        self.rx_packets += 1
        self.rx_bytes += raw_length

        base = sequence if isinstance(sequence, int) and 0 <= sequence <= 0xFFFF else None
        outgoing = {FIELD_FRAMES: frames, FIELD_SPEAKER: member.member_id}
        if base is not None:
            outgoing[FIELD_SEQ] = base
        outgoing_data = msgpack.packb(outgoing)

        with self.lock:
            targets = [other for other in room.members if other is not member and not other.deaf]
        for other in targets:
            if other.link is None:
                seq = base
                for frame in batch:
                    other.bridge.deliver(frame, member.member_id, seq)
                    seq = None if seq is None else (seq + 1) & 0xFFFF
                continue
            if other.link.status != RNS.Link.ACTIVE:
                continue
            getter = getattr(other.link, "get_mdu", None)
            limit = getter() if getter else None
            if limit and len(outgoing_data) > limit:
                self.relay_split(other.link, batch, base, member.member_id, limit)
            else:
                outgoing_packet = RNS.Packet(other.link, outgoing_data, create_receipt=False)
                if outgoing_packet.send() is not False:
                    self.tx_packets += 1
                    self.tx_bytes += len(outgoing_packet.raw)

    def relay_split(self, link, batch, base, speaker_id, mdu):
        chunk = []
        chunk_seq = base
        chunk_packed = 0
        seq = base
        for frame in batch:
            cost = len(frame) + 3
            if chunk and 12 + chunk_packed + cost > mdu - 8:
                self.relay_send(link, chunk, chunk_seq, speaker_id)
                chunk = []
                chunk_packed = 0
                chunk_seq = seq
            chunk.append(frame)
            chunk_packed += cost
            seq = None if seq is None else (seq + 1) & 0xFFFF
        if chunk:
            self.relay_send(link, chunk, chunk_seq, speaker_id)

    def relay_send(self, link, frames, seq, speaker_id):
        payload = frames[0] if len(frames) == 1 else list(frames)
        outgoing = {FIELD_FRAMES: payload, FIELD_SPEAKER: speaker_id}
        if seq is not None:
            outgoing[FIELD_SEQ] = seq
        packet = RNS.Packet(link, msgpack.packb(outgoing), create_receipt=False)
        if packet.send() is not False:
            self.tx_packets += 1
            self.tx_bytes += len(packet.raw)




    def relay_end(self, member):
        room = member.room
        if room is None or member.server_muted or not member.speaker or not member.talking:
            return
        member.talking = False
        with self.lock:
            targets = [other for other in room.members if other is not member and not other.deaf]
        for other in targets:
            if other.link is None:
                other.bridge.deliver_end(member.member_id)
            else:
                self.send(other.link, {FIELD_TALK_END: member.member_id})

    def text(self, member, text):
        text = clean_text(text, MAX_TEXT)
        if not text or not member.allow_text() or member.room is None:
            return


        # Senders show their own local value
        self.broadcast({FIELD_TEXT: [member.member_id, text]}, room=member.room, exclude=member)

    def poke(self, member, fields):
        try:
            target_id = int(fields[0])
            text = clean_text(fields[1], MAX_TEXT)
        except Exception:
            return
        if not member.allow_text():
            return
        with self.lock:
            target = self.find_member(target_id)
        if target and target is not member:
            self.send(target.link, {FIELD_POKE: [member.member_id, text]})

    def state(self, member, fields):
        if not member.allow_control():
            return
        try:
            muted = bool(fields[0])
            deaf = bool(fields[1])
        except Exception:
            return
        with self.lock:
            if (muted, deaf) == (member.muted, member.deaf):
                return
            member.muted = muted
            member.deaf = deaf
            self.broadcast({FIELD_USER: member.as_user()})


### OPERATOR TOOLS ###
    def admin(self, operator, fields):
        try:
            action = fields[0]
            target_id = int(fields[1])
            argument = fields[2] if len(fields) > 2 else None
        except Exception:
            return
        if not operator.operator:
            self.notice(operator, "operators only")
            return
        if action not in ADMIN_ACTIONS:
            return
        with self.lock:
            target = self.find_member(target_id)
            if target is None:
                self.notice(operator, "no such user")
                return
            if target is operator and action != ADMIN_MOVE:
                self.notice(operator, "not on yourself")
                return
            if action == ADMIN_KICK:
                self.kick(target, f"kicked by {operator.name}")
            elif action == ADMIN_BAN:
                if target.identity is None:
                    self.notice(operator, "anonymous users cannot be banned")
                    return
                self.banned.add(target.identity.hash)
                save_hash_list(self.banned_file, self.banned)
                self.kick(target, f"banned by {operator.name}")
            elif action == ADMIN_MUTE:
                target.server_muted = True
                self.broadcast({FIELD_USER: target.as_user()})
                self.notice(target, f"You were muted by operator {operator.name}.")
            elif action == ADMIN_UNMUTE:
                target.server_muted = False
                self.broadcast({FIELD_USER: target.as_user()})
                self.notice(target, f"You were unmuted by operator {operator.name}.")
            elif action == ADMIN_OP:
                if target.identity is None:
                    self.notice(operator, "anonymous users cannot be operators")
                    return
                self.operators.add(target.identity.hash)
                save_hash_list(self.operators_file, self.operators)
                target.operator = True
                self.broadcast({FIELD_USER: target.as_user()})
                self.notice(target, f"You are now an operator, promoted by {operator.name}.")
            elif action == ADMIN_DEOP:
                if target.identity is not None:
                    self.operators.discard(target.identity.hash)
                    save_hash_list(self.operators_file, self.operators)
                target.operator = False
                self.broadcast({FIELD_USER: target.as_user()})
                self.notice(target, f"You are no longer an operator, demoted by {operator.name}.")
            elif action == ADMIN_MOVE:
                room = self.rooms.get(argument) if isinstance(argument, int) else None
                if room is None:
                    self.notice(operator, "no such room")
                    return
                if target.link is None:
                    self.notice(operator, "phone callers cannot be moved")
                    return
                if room is target.room:
                    return
                reason = self.enter(target, room, room.password)  # operators bypass the password, not the allow list
                if reason is not None:
                    self.send(operator.link, {FIELD_DENIED: [room.id, reason]})
                    return
                self.broadcast({FIELD_USER: target.as_user()})
                if target is not operator:
                    self.notice(target, f"You were moved to {room.name} by operator {operator.name}.")
            RNS.log(f"Operator {operator.label()}: {action} on {target.label()}", RNS.LOG_NOTICE)


    def kick(self, target, reason):
        if target.link is None:
            target.bridge.hangup()
            return
        self.deny_and_close(target.link, None, reason)


### DIAL-IN APPROVAL ###
    def dialin_members(self):
        with self.lock:
            room = self.dialin.room if self.dialin is not None else None
            return [
                member
                for member in self.members.values()
                if member.admitted and member.link is not None and (member.operator or member.room is room)
            ]

    def dialin_waiting(self, call):
        hex_hash = call.identity.hash.hex()
        text = (
            f"Phone call from {hex_hash} is waiting to join {self.dialin.room.name}. "
            f"Use /answer {hex_hash[:8]} to let it in or /reject {hex_hash[:8]} to turn it away."
        )
        for member in self.dialin_members():
            self.send(member.link, {FIELD_NOTICE: text, FIELD_DIALIN: ["waiting", hex_hash]})

    def dialin_event(self, call, event):
        if call.identity is None:
            return
        for member in self.dialin_members():
            self.send(member.link, {FIELD_DIALIN: [event, call.identity.hash.hex()]})

    def dialin_request(self, member, fields):
        if not member.allow_control():
            return
        try:
            action = fields[0]
            argument = fields[1] if len(fields) > 1 else None
        except Exception:
            return
        if action not in DIALIN_ACTIONS:
            return
        if self.dialin is None:
            self.notice(member, "this server has no dial-in")
            return
        if not (member.operator or member.room is self.dialin.room):
            self.notice(member, f"only operators and members of {self.dialin.room.name} can manage phone calls")
            return
        if action in (DIALIN_ANSWER, DIALIN_REJECT):
            call = self.dialin.find_pending(argument)
            if call is None:
                self.notice(member, "no waiting phone call matches that")
                return
            hex_hash = call.identity.hash.hex()
            if action == DIALIN_ANSWER:
                if not call.accept():
                    self.notice(member, f"The call from {hex_hash} could not be connected.")
                elif member.operator:
                    self.dialin.approve(call.identity.hash)
                    self.notice(member, f"Answered the call from {hex_hash}. That phone may now call in without asking.")
                else:
                    self.notice(member, f"Answered the call from {hex_hash}.")
            else:
                call.decline()
            RNS.log(f"{member.label()}: {action} phone call from {hex_hash}", RNS.LOG_NOTICE)
        elif action in (DIALIN_ADD, DIALIN_REMOVE):
            if not member.operator:
                self.notice(member, "only operators can pair or unpair phones")
                return
            try:
                identity_hash = parse_hash(str(argument))
            except ValueError as error:
                self.notice(member, f"bad identity hash: {error}")
                return
            if action == DIALIN_ADD:
                self.dialin.approve(identity_hash)
                self.notice(member, f"Phone {identity_hash.hex()} may now call in without asking.")
            else:
                self.dialin.forget(identity_hash)
                self.notice(member, f"Phone {identity_hash.hex()} must ask before joining again.")
            RNS.log(f"{member.label()}: dial-in {action} {identity_hash.hex()}", RNS.LOG_NOTICE)
        else:
            approved = sorted(item.hex() for item in self.dialin.approved)
            waiting = [call.identity.hash.hex() for call in self.dialin.pending()]
            shown = ", ".join(approved[:6]) + (f" and {len(approved) - 6} more" if len(approved) > 6 else "")
            self.notice(member, f"Phones that may call in: {shown or 'none'}. Waiting now: {', '.join(waiting[:4]) or 'none'}.")


### SERVER CONFIGURATION ###
    def config_request(self, operator, fields):
        if not operator.allow_control():
            return
        try:
            action = fields[0]
            payload = fields[1] if len(fields) > 1 else None
        except Exception:
            return
        if action not in CONFIG_ACTIONS:
            return
        if not operator.operator:
            self.notice(operator, "operators only")
            return
        try:
            with self.lock:
                if action == CONFIG_MOTD:
                    self.motd = clean_text(payload, MAX_TEXT)
                    self.broadcast({FIELD_CONFIG: [CONFIG_MOTD, self.motd]})
                elif action == CONFIG_ROOM_GET:
                    room = self.room_for(payload)
                    self.send_large(operator.link, {FIELD_CONFIG: [CONFIG_ROOM, room.id, room.spec]})
                    return
                elif action == CONFIG_ROOM_ADD:
                    if len(self.rooms) >= MAX_ROOMS:
                        raise ValueError(f"a server can have at most {MAX_ROOMS} rooms")
                    room = self.build_room(max(self.rooms) + 1, room_spec(payload))
                    self.rooms[room.id] = room
                    self.broadcast({FIELD_CHANNEL: [self.channel_record(room)]})
                elif action == CONFIG_ROOM_EDIT:
                    if not isinstance(payload, (list, tuple)) or len(payload) != 2:
                        raise ValueError("room edit needs a room and its changes")
                    room = self.room_for(payload[0])
                    spec = dict(room.spec)
                    for key, value in room_spec(payload[1], partial=True).items():
                        if value is None:
                            spec.pop(key, None)
                        else:
                            spec[key] = value
                    self.replace_room(room, self.build_room(room.id, spec))
                else:
                    room = self.room_for(payload)
                    if room is self.default_room:
                        raise ValueError("the default room cannot be removed")
                    self.drop_room(room)
            self.save_config()
            RNS.log(f"Operator {operator.label()}: {action}", RNS.LOG_NOTICE)
        except (ValueError, TypeError, SystemExit) as error:
            self.notice(operator, str(error) or "invalid room settings")

    def room_for(self, room_id):
        room = self.rooms.get(room_id) if isinstance(room_id, int) else None
        if room is None:
            raise ValueError("no such room")
        return room

    def build_room(self, room_id, spec):
        return Room(room_id, spec, self.profile, self.max_members, self.require_identity)

    def replace_room(self, old, new):
        new.members = old.members
        for member in list(new.members):
            member.room = new
        self.rooms[new.id] = new
        if old is self.default_room:
            self.default_room = new
        if self.dialin is not None and self.dialin.room is old:
            self.dialin.room = new
        self.broadcast({FIELD_CHANNEL: [self.channel_record(new)]})
        audio_changed = (new.profile, new.frame_ms, new.ptt, new.ptt_jitter_ms) != (old.profile, old.frame_ms, old.ptt, old.ptt_jitter_ms)
        for member in list(new.members):
            speaker = new.can_speak(member)
            if member.link is None:
                if audio_changed:
                    member.bridge.hangup()
                continue
            if audio_changed or speaker != member.speaker:
                member.speaker = speaker
                member.set_room(new)
                self.send(member.link, {FIELD_ROOM: [new.id, new.profile, new.frame_ms, member.speaker]})
                self.broadcast({FIELD_USER: member.as_user()})

    def drop_room(self, room):
        if self.dialin is not None and self.dialin.room is room:
            self.dialin.room = self.default_room
        del self.rooms[room.id]
        self.broadcast({FIELD_CHANNEL_GONE: room.id})
        for member in list(room.members):
            if member.link is None:
                member.bridge.hangup()
                continue
            if self.enter(member, self.default_room, self.default_room.password) is not None:
                member.set_room(None)
                self.send(member.link, {FIELD_ROOM: [None, None, None, True]})
            self.notice(member, f"{room.name} was removed by an operator.")
            self.broadcast({FIELD_USER: member.as_user()})

    def save_config(self):
        if not self.config_path:
            return
        path = os.path.expanduser(self.config_path)
        config = {}
        try:
            if os.path.isfile(path):
                with open(path) as config_file:
                    config = json.load(config_file)
            if not isinstance(config, dict):
                config = {}
            config["motd"] = self.motd
            config["rooms"] = [self.rooms[room_id].spec for room_id in sorted(self.rooms)]
            temporary = path + ".tmp"
            with open(temporary, "w") as config_file:
                json.dump(config, config_file, indent=2)
                config_file.write("\n")
            os.replace(temporary, path)
        except (OSError, ValueError) as error:
            RNS.log(f"Could not save the server configuration to {path}: {error}", RNS.LOG_ERROR)

    def notice(self, member, text):
        self.send(member.link, {FIELD_NOTICE: text})


    def send(self, link, fields):
        if link is not None and link.status == RNS.Link.ACTIVE:
            RNS.Packet(link, msgpack.packb(fields), create_receipt=False).send()

    def send_large(self, link, fields):
        if link is None or link.status != RNS.Link.ACTIVE:
            return
        data = msgpack.packb(fields)
        if len(data) <= (link.get_mdu() or RNS.Link.MDU):
            RNS.Packet(link, data, create_receipt=False).send()
        else:
            RNS.Resource(data, link)

    def send_records(self, link, field, records):
        # pack the join sync (rooms followed by users) users) into as few packets as fit the link MTU
        if link is None or link.status != RNS.Link.ACTIVE:
            return
        mdu = link.get_mdu() or RNS.Link.MDU
        limit = mdu - SYNC_MARGIN
        batch = []
        packed = SYNC_BASE
        for record in records:
            cost = len(msgpack.packb(record))
            if batch and packed + cost > limit:
                self.send(link, {field: batch})
                batch = []
                packed = SYNC_BASE
            batch.append(record)
            packed += cost
        if batch:
            self.send(link, {field: batch})

    def broadcast(self, fields, room=None, exclude=None):
        with self.lock:
            if room:
                pool = room.members
            else:
                pool = [member for member in self.members.values() if member.admitted]
            links = [member.link for member in pool if member is not exclude and member.link is not None]
        data = msgpack.packb(fields)
        for link in links:
            if link.status == RNS.Link.ACTIVE:
                RNS.Packet(link, data, create_receipt=False).send()


### CONFIG ###
def config_from_args(args):
    config = {}
    if args.config:
        with open(os.path.expanduser(args.config)) as config_file:
            config = json.load(config_file)
        if not isinstance(config, dict):
            raise SystemExit("config must be a JSON object")
    if args.name:
        config["name"] = args.name
    if args.profile:
        config["profile"] = args.profile
    if args.max_members:
        config["max_members"] = args.max_members
    if args.allow:
        config["allow"] = list(config.get("allow") or []) + args.allow
    if args.allowed_file:
        config["allowed_file"] = args.allowed_file
    if args.op:
        config["ops"] = list(config.get("ops") or []) + args.op
    if args.password:
        config["password"] = args.password
    if args.hidden:
        config["hidden"] = True
    if args.dialin:
        config["dialin"] = {"enabled": True, "room": args.dialin}
    if args.room:
        rooms = []
        for spec in args.room:
            name, _, profile = spec.partition(":")
            rooms.append({"name": name, "profile": profile or None})
        config["rooms"] = rooms
    return config


def main():
    parser = argparse.ArgumentParser(description="Partyline voice room server")
    parser.add_argument(
        "--version", action="version", version=f"Partyline server {APP_VERSION} (protocol {PROTOCOL_VERSION})"
    )
    parser.add_argument("--config", default=None, help="JSON server configuration (see server_example.json)")
    parser.add_argument("--configdir", default=None, help="Reticulum config directory (default ~/.reticulum)")
    parser.add_argument("--identity", default=None, help="identity file (default: server_identity in --statedir)")
    parser.add_argument(
        "--statedir",
        default=CONFIG_DIR,
        help="where the identity, operators.txt and banned.txt live (default ~/.config/partyline)",
    )
    parser.add_argument("--name", default=None, help="server name shown in clients and announces")
    parser.add_argument("--profile", choices=PROFILES.keys(), default=None, help="default codec profile for rooms")
    parser.add_argument(
        "--room",
        action="append",
        default=[],
        metavar="NAME[:PROFILE]",
        help="add a room  replaces rooms from --config",
    )
    parser.add_argument(
        "--allow", action="append", default=[], metavar="HASH", help="identity hash allowed on the server (repeatable)"
    )
    parser.add_argument("--allowed-file", default=None, help="file with one allowed identity hash per line")
    parser.add_argument(
        "--op", action="append", default=[], metavar="HASH", help="identity hash of an operator (repeatable)"
    )
    parser.add_argument("--max-members", type=int, default=None)
    parser.add_argument("--password", default=None, help="password everyone must give to connect")
    parser.add_argument(
        "--hidden", action="store_true", help="do not appear in server browsers (still reachable by hash)"
    )
    parser.add_argument(
        "--dialin",
        default=None,
        metavar="ROOM",
        help="answer rnphone calls to this server's identity hash and put callers in ROOM",
    )
    parser.add_argument("--announce-interval", type=float, default=3300.0)
    parser.add_argument("--stats-interval", type=float, default=5.0)
    args = parser.parse_args()

    state_dir = os.path.expanduser(args.statedir)
    if args.config is None and os.path.isfile(os.path.join(state_dir, SAVED_CONFIG_FILENAME)):
        args.config = os.path.join(state_dir, SAVED_CONFIG_FILENAME)
    config = config_from_args(args)
    RNS.Reticulum(configdir=args.configdir)
    identity_path = args.identity or os.path.join(args.statedir, "server_identity")
    server = Server(
        load_identity(identity_path),
        config,
        state_dir=state_dir,
        config_path=args.config or os.path.join(state_dir, SAVED_CONFIG_FILENAME),
    )

    print(f"Partyline server {APP_VERSION} (protocol {PROTOCOL_VERSION})", flush=True)
    print(f"Room destination: {server.destination.hash.hex()}", flush=True)
    if server.allowed is not None:
        access = f"{len(server.allowed)} identities allowed"
    else:
        access = "open to anyone with the hash"
    extras = []
    if server.password:
        extras.append("password protected")
    if server.hidden:
        extras.append("hidden from browsers")
    extra_text = f", {', '.join(extras)}" if extras else ""
    print(f"Server {server.name!r}, {access}, {len(server.operators)} operators{extra_text}", flush=True)
    for room in server.rooms.values():
        print(f"  room {room.id}: {room}", flush=True)
    if server.dialin:
        print(
            f"Dial-in number for rnphone: {server.dialin.number}  (callers land in {server.dialin.room.name})",
            flush=True,
        )

    last_announce = 0.0
    last_stats = time.time()
    last_rx_bytes = 0
    last_tx_bytes = 0

    try:
        while True:
            if time.time() - last_announce > args.announce_interval:
                server.destination.announce()
                last_announce = time.time()
                if server.dialin:
                    server.dialin.announce()
            time.sleep(0.5)
            if time.time() - last_stats >= args.stats_interval:
                elapsed = time.time() - last_stats
                rx_kbps = (server.rx_bytes - last_rx_bytes) * 8 / elapsed / 1000
                tx_kbps = (server.tx_bytes - last_tx_bytes) * 8 / elapsed / 1000
                if rx_kbps or tx_kbps:
                    print(
                        f"relay in {rx_kbps:6.1f} kbps  out {tx_kbps:6.1f} kbps  members {server.member_count()}",
                        flush=True,
                    )
                last_rx_bytes = server.rx_bytes
                last_tx_bytes = server.tx_bytes
                last_stats = time.time()
    except KeyboardInterrupt:
        pass
    print(
        f"total in {server.rx_packets} pkts / {server.rx_bytes} B, out {server.tx_packets} pkts / {server.tx_bytes} B",
        flush=True,
    )


if __name__ == "__main__":
    main()
