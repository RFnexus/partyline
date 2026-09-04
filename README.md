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



### Credits
- [Reticulum](https://github.com/markqvist/Reticulum) and [LXST](https://github.com/markqvist/lxst) by Mark Qvist
- [Mumble](https://github.com/mumble-voip/mumble). Great software that just works. The best UI is stolen UI
- Tree, toolbar and application icons from [Tabler Icons](https://tabler.io/icons) 
- Sound effects from [Kenney interface sounds](https://kenney.nl/assets/interface-sounds)
- Tin can telephone drawing in the logo from [openclipart.org](https://openclipart.org/detail/351397/classic-string-telephone)
