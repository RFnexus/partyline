#!/usr/bin/env python3
import argparse
import hmac
import json
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
DENY_CLOSE_DELAY = 0.5  # grace to let the FIELD_DENIED packet leave before tearing the link down 

OPERATORS_FILENAME = "operators.txt" #
BANNED_FILENAME = "banned.txt"

# kept for older imports
load_allow_list = load_hash_list


class Room:
    def __init__(self, room_id, spec, default_profile, default_max_members):

        self.id = room_id
        self.name = clean_name(spec.get("name"), f"Room {room_id}")
        self.description = clean_text(spec.get("description", ""), MAX_DESCRIPTION)
        self.profile = spec.get("profile") or default_profile

        if self.profile not in PROFILES:
            raise SystemExit(f"room {self.name!r}: unknown profile {self.profile!r}")

        self.frame_ms = frame_ms(self.profile)
        self.password = spec.get("password") or None

        self.require_identity = bool(spec.get("require_identity", False))

        if spec.get("allow") or spec.get("allowed_file"):
            self.allow = load_hash_list(spec.get("allow"), spec.get("allowed_file"))
        else:
            self.allow = None


        self.max_members = int(spec.get("max_members") or default_max_members)
        self.members = set()





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
        if self.password:
            password_ok = isinstance(password, str) and hmac.compare_digest(password, self.password)
            if not password_ok:
                return "wrong password"
        if len(self.members) >= self.max_members:
            return "room is full"
        return None

    def as_channel(self, dialin_number=None):
        return [self.id, self.name, self.profile, self.frame_ms, self.access, self.description, dialin_number]

    def __str__(self):
        parts = [describe(self.profile)]
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

        self.hops = None
        self.rtt = None
        self.admitted = False
        self.joined_at = time.time()
        self.rejected_frames = 0
        self.limited_frames = 0

        self.rate = 0.0
        self.tokens = 0.0

        self.last_refill = time.time()
        self.text_tokens = TEXT_RATE
        self.text_last_refill = time.time()

    def set_room(self, room):
        if self.room:
            self.room.members.discard(self)
        self.room = room
        if room:
            room.members.add(self)
            self.rate = 2 * 1000 / room.frame_ms
            self.tokens = self.rate
        else:
            self.rate = 0.0
            self.tokens = 0.0

    def allow_packet(self):
        now = time.time()
        self.tokens = min(self.rate, self.tokens + (now - self.last_refill) * self.rate)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
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
        ]

    def label(self):
        if self.identity:
            return f"{self.name} #{self.member_id} <{self.identity.hash.hex()}>"
        return f"{self.name} #{self.member_id} (anonymous)"


class Server:
    def __init__(self, identity, config, state_dir=CONFIG_DIR):

        self.state_dir = state_dir
        self.operators_file = os.path.join(state_dir, OPERATORS_FILENAME)
        self.banned_file = os.path.join(state_dir, BANNED_FILENAME)

        self.name = clean_name(config.get("name"), "Partyline server")
        self.motd = clean_text(config.get("motd", ""), MAX_TEXT)

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

        room_specs = config.get("rooms") or [{"name": "Lobby"}]
        self.rooms = {}
        for room_id, spec in enumerate(room_specs, 1):
            self.rooms[room_id] = Room(room_id, spec, self.profile, self.max_members)
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
        announced_name = "" if self.hidden else self.name  # a hidden server announces no name at all
        self.destination.set_default_app_data(pack_announce(announced_name, self.announce_flags()))
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
            if len(self.members) >= self.max_members:
                RNS.log("Server full, refusing link", RNS.LOG_NOTICE)
                self.deny_and_close(link, None, "server is full")
                return
            member = Member(link, self.next_member_id)
            self.next_member_id += 1
            self.members[link] = member
        link.set_packet_callback(lambda data, packet, link=link: self.packet(link, data, packet))
        link.set_link_closed_callback(self.link_closed)
        link.set_remote_identified_callback(self.identified)
        threading.Timer(HELLO_TIMEOUT, self.hello_timeout, [link]).start()


    def identified(self, link, identity):
        with self.lock:
            member = self.members.get(link)
            if member:
                member.identity = identity


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

        needs_identity = self.allowed is not None or (room is not None and room.access & ACCESS_IDENTITY)
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
                RNS.log(f"{member.label()} is banned, dropping", RNS.LOG_NOTICE)
                self.deny_and_close(member.link, None, "you are banned from this server")
                return
            if self.allowed is not None:
                if member.identity is None or member.identity.hash not in self.allowed:
                    RNS.log(f"{member.label()} is not on the server allow list, dropping", RNS.LOG_NOTICE)
                    self.deny_and_close(member.link, None, "you are not on this server's allow list")
                    return

            if self.password:
                given = fields.get("server_password")
                if not (isinstance(given, str) and hmac.compare_digest(given, self.password)):
                    RNS.log(f"{member.label()} gave a wrong server password, dropping", RNS.LOG_NOTICE)
                    self.deny_and_close(member.link, None, "wrong server password")
                    return

            member.name = clean_name(fields.get("name"), f"guest-{member.member_id}")
            member.text_only = bool(fields.get("text_only", False))
            member.muted = bool(fields.get("muted", False)) or member.text_only
            member.deaf = bool(fields.get("deaf", False)) or member.text_only
            hops = fields.get("hops")
            if isinstance(hops, int) and 0 <= hops < 256:
                member.hops = hops
            rtt = fields.get("rtt")
            if isinstance(rtt, (int, float)) and 0 <= rtt < 1e6:
                member.rtt = int(rtt)

            member.operator = member.identity is not None and member.identity.hash in self.operators
            member.admitted = True

            welcome = {
                "name": self.name,
                "sid": member.member_id,
                "motd": self.motd,
                "ver": PROTOCOL_VERSION,
                "app": APP_VERSION,
            }
            self.send(member.link, {FIELD_WELCOME: welcome})
            for existing_room in self.rooms.values():
                self.send(member.link, {FIELD_CHANNEL: self.channel_record(existing_room)})
            for other in self.members.values():
                if other.admitted and other is not member:
                    self.send(member.link, {FIELD_USER: other.as_user()})
            self.send(member.link, {FIELD_SYNCED: True})


            if room is not None:
                reason = self.enter(member, room, password)
            else:
                reason = f"no such room {wanted_room!r}"
            if reason is not None:
                self.send(member.link, {FIELD_DENIED: [room.id if room else None, reason]})
                if room is self.default_room or self.enter(member, self.default_room, None) is not None:
                    self.send(member.link, {FIELD_ROOM: [None, None, None]})

            self.broadcast({FIELD_USER: member.as_user()})
            room_name = member.room.name if member.room else "no room"
            RNS.log(f"{member.label()} joined {room_name}, {self.member_count()} on server", RNS.LOG_NOTICE)

    def enter(self, member, room, password):
        # Move a member into the room when allowed
        reason = room.check(member, password)
        if reason is not None:
            return reason
        member.set_room(room)
        self.send(member.link, {FIELD_ROOM: [room.id, room.profile, room.frame_ms]})
        return None

    def move(self, member, fields):
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
            reason = self.enter(member, room, password)
            if reason is not None:
                self.send(member.link, {FIELD_DENIED: [room.id, reason]})
                return
            self.broadcast({FIELD_USER: member.as_user()})
            RNS.log(f"{member.label()} moved to {room.name}", RNS.LOG_NOTICE)

    def link_closed(self, link):
        self.remove_member(link)

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
            member.name = name
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
                threading.Thread(target=self.hello, args=(member, fields[FIELD_HELLO]), daemon=True).start()
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
        if FIELD_TALK_END in fields:
            self.relay_end(member)

    def relay(self, member, frame, raw_length, sequence=None):
        room = member.room
        if room is None or member.server_muted:
            return
        if not member.allow_packet():
            member.limited_frames += 1
            return
        if not valid_frame(frame, room.profile):
            member.rejected_frames += 1
            return

        self.rx_packets += 1
        self.rx_bytes += raw_length

        outgoing = {FIELD_FRAMES: frame, FIELD_SPEAKER: member.member_id}  


        if isinstance(sequence, int) and 0 <= sequence <= 0xFFFF:
            outgoing[FIELD_SEQ] = sequence
        outgoing_data = msgpack.packb(outgoing)

        with self.lock:
            targets = [other for other in room.members if other is not member and not other.deaf]
        for other in targets:
            if other.link is None:
                other.bridge.deliver(frame, member.member_id, sequence) 
                continue
            if other.link.status != RNS.Link.ACTIVE:
                continue
            outgoing_packet = RNS.Packet(other.link, outgoing_data, create_receipt=False)
            if outgoing_packet.send() is not False:
                self.tx_packets += 1
                self.tx_bytes += len(outgoing_packet.raw)




    def relay_end(self, member):
        room = member.room
        if room is None or member.server_muted:
            return
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
            self.send(operator.link, {FIELD_DENIED: [None, "operators only"]})
            return
        if action not in ADMIN_ACTIONS:
            return
        with self.lock:
            target = self.find_member(target_id)
            if target is None:
                self.send(operator.link, {FIELD_DENIED: [None, "no such user"]})
                return
            if target is operator and action != ADMIN_MOVE:
                self.send(operator.link, {FIELD_DENIED: [None, "not on yourself"]})
                return
            if action == ADMIN_KICK:
                self.kick(target, f"kicked by {operator.name}")
            elif action == ADMIN_BAN:
                if target.identity is None:
                    self.send(operator.link, {FIELD_DENIED: [None, "anonymous users cannot be banned"]})
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
                    self.send(operator.link, {FIELD_DENIED: [None, "anonymous users cannot be operators"]})
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
                    self.send(operator.link, {FIELD_DENIED: [None, "no such room"]})
                    return
                if target.link is None:
                    self.send(operator.link, {FIELD_DENIED: [None, "phone callers cannot be moved"]})
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

    def notice(self, member, text):
        self.send(member.link, {FIELD_NOTICE: text})


    def send(self, link, fields):
        if link is not None and link.status == RNS.Link.ACTIVE:
            RNS.Packet(link, msgpack.packb(fields), create_receipt=False).send()

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

    config = config_from_args(args)
    RNS.Reticulum(configdir=args.configdir)
    identity_path = args.identity or os.path.join(args.statedir, "server_identity")
    server = Server(load_identity(identity_path), config, state_dir=os.path.expanduser(args.statedir))

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
