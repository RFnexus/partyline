<p align="center">
<img width="500" height="200" alt="partyline" src="https://github.com/user-attachments/assets/b0c00a1a-c8fa-4f2b-ac07-3031851ad178" />
</p>

# partyline
A group voice application for Reticulum, built on [LXST](https://github.com/markqvist/lxst), inspired by [Mumble](https://github.com/mumble-voip/mumble)

<img width="1915" height="1007" alt="Screenshot from 2026-09-04 07-05-40" src="https://github.com/user-attachments/assets/bb9c689b-2344-4627-a640-192582575b45" />

## Features
- Encrypted, realtime group voice over any Reticulum transport capable of >6 kilobits per second
- An easy to host lightweight server and protocol. Spin up a Partyline server in under 30 seconds. 
- Per room access and channel control. open, identified users only, allow lists of identity hashes, or a password. Servers can also require a password or an allow list
- Dial-in: rnphone and other LXST clients such as Sideband and MeshChatX can call the server and land in a room too, when enabled
- Rooms each with its own LXST codec configurable by the server. Opus for fast links, Codec2 down to 700 bps for slow ones.
- GUI and terminal client

## Installation
Requires Python 3.11 or newer, Reticulum, and LXST

    git clone https://github.com/RFnexus/partyline
    cd partyline
    pip install .

Or install via `rngit` with:


```
git clone rns://4cf8a0651c4d73cacd0f93ac1d95e80a/public/partyline
```

For a system wide push to talk key, install the optional extra with:

    pip install ".[hotkeys]"

This installs three commands: partyline, partyline-server and partyline-client.

## Running
Start the GUI client:

    partyline

Connect opens the server browser. Add a server by it's hash or use the server browser to find discovered servers from announces. Double click a room to join it. Settings has the audio devices, transmit mode, push to talk key, and UI appearance options 
<img width="1914" height="1006" alt="Screenshot from 2026-09-04 07-06-15" src="https://github.com/user-attachments/assets/1372962a-fcf9-421e-829c-2a1426a66cb5" />


Terminal client:

    partyline-client HASH --name Bob --room Lobby

Commands are: `/join, /say, /poke, /who, /mute, /deaf`

Type `/ptt` to toggle push to talk 

Or as an operator: /kick, /ban, /smute, /op and /move

## Server

Start a server:

    partyline-server --name "My server" --profile opus-high

The server prints its destination hash for clients and with dial in enabled the number rnphone users call if configured for rooms. For rooms, access lists, operators, a server password and dial in, copy server_example.json and start with:

    partyline-server --config server.json

Available `--profile` codecs for rooms:

- `opus-high`  - Opus voice, 16 kbps
- `opus-med`   - Opus voice, 8 kbps
- `opus-low`   - Opus voice, 6 kbps
- `c2-3200`    - Codec2, 3200 bps
- `c2-2400`    - Codec2, 2400 bps
- `c2-1200`    - Codec2, 1200 bps
- `c2-700`     - Codec2, 700 bps
- `music-low`  - Opus music, 14 kbps
- `music-med`  - Opus music, 28 kbps
- `music-high` - Opus music, 56 kbps

See `server_example.json` for a full list of options. 
opus-high-med-low and the Codec2 modes are meant for voice, and music-high-med-low is meant for streaming music or broadcasting to a room. As a rule, use the highest bitrate your link can carry

### Install as a systemd unit

To install as a systemd unit find the server command with `which partyline-server` then create `/etc/systemd/system/partyline.service`:

    [Unit]
    Description=Partyline voice server
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    User=partyline
    ExecStart=/usr/local/bin/partyline-server --config /home/partyline/server.json
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target

Then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now partyline

### Music/Broadcasting & Bots

Server operators can also configure broadcast-only rooms for high quality music and audio using LXST's Opus CELT profiles. The options are `music-high`, `music-med`, and `music-low`.  Music rooms are a special type of room where only the listed identities can send audio. Add their identity hash, seperated by commas, in `music`

```
{"name": "Broadcast", "profile": "music-med", "music": ["0123456789abcdef0123456789abcdef"], "description": "Music/broadcast room. Only listed identities may talk. Uses LXSTs Opus CELT profile"}
```
<img width="1181" height="648" alt="Screenshot from 2026-09-05 17-07-35" src="https://github.com/user-attachments/assets/f124833b-cb31-453f-a91f-9e61a171624c" />

An example Music Bot can be found under `bots/music_bot.py`



### Credits
- [Reticulum](https://github.com/markqvist/Reticulum) and [LXST](https://github.com/markqvist/lxst) by Mark Qvist
- [Mumble](https://github.com/mumble-voip/mumble). Great software that just works. The best UI is stolen UI
- Tree, toolbar and application icons from [Tabler Icons](https://tabler.io/icons) 
- Sound effects from [Kenney interface sounds](https://kenney.nl/assets/interface-sounds)
- Tin can telephone drawing in the logo from [openclipart.org](https://openclipart.org/detail/351397/classic-string-telephone)
