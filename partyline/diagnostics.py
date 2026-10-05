import json
import platform
from datetime import datetime, timezone
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

from .common import APP_VERSION, PROTOCOL_VERSION
from .i18n import _


@lru_cache(maxsize=1)
def versions():
    result = {"Partyline": APP_VERSION, "Python": platform.python_version()}
    for label, package in (("Reticulum", "rns"), ("LXST", "lxst")):
        try:
            result[label] = version(package)
        except PackageNotFoundError:
            result[label] = "unknown"
    return result


def snapshot(client=None, rates=None):
    result = {
        "schema": 1,
        "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "versions": dict(versions()),
        "platform": platform.system(),
        "protocol": PROTOCOL_VERSION,
        "connected": bool(client and client.connected),
        "audio_active": bool(client and client.playout),
        "notes": [
    'test'
        ],
    }
    if client is None:
        return result
    packetizer = client.packetizer
    playout = client.playout
    tx = packetizer.diagnostics() if packetizer else None
    rx = playout.diagnostics() if playout else None
    packets, wire_bytes, payload_bytes = client.tx_totals()
    result.update(
        codec=client.audio_profile,
        frame_ms=client.frame_ms,
        mode=client.cfg.mode,
        ptt_room=client.ptt_room,
        muted=client.muted,
        deafened=client.deaf,
        transmitting=client.transmitting,
        tx=tx,
        rx=rx,
        packets={
            "sent": packets,
            "received": client.rx_packets,
            "tx_bytes": wire_bytes,
            "rx_bytes": client.rx_bytes,
            "tx_payload_bytes": payload_bytes,
            "late_or_duplicate": client.late_packets,
            "loss": None,
            "bad_audio_frames": client.bad_frames,
        },
        receive_worker_pending_packets=client.inbox.qsize(),
        reorder_pending_frames=sum(len(entry["pending"]) for entry in list(client._reorder.values())),
        rates={key: round(float((rates or {}).get(key, 0)), 2) for key in ("tx_kbps", "rx_kbps")},
    )
    return result


def report_text(report):
    return json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def summary(report):
    tx = report.get("tx") or {}
    rx = report.get("rx") or {}
    return _("TX batch {0:g} ms / queued {1:g} ms   RX queued {2:g} ms").format(
        tx.get("last_batch_ms", 0),
        tx.get("pending_ms", 0) + tx.get("sending_ms", 0),
        rx.get("receive_queue_ms", 0),
    )


def rows(report):
    tx = report.get("tx") or {}
    rx = report.get("rx") or {}
    packets = report.get("packets") or {}
    missing = _("unavailable")

    def ms(values, key):
        value = values.get(key)
        return missing if value is None else _("{0:g} ms").format(value)

    return [
        (_("versions"), " / ".join(f"{key} {value}" for key, value in report["versions"].items())),
        (_("Codec / frame"), f"{report.get('codec') or missing} / {report.get('frame_ms') or 0} ms"),
        (_("TX last batch / duration limit"), f"{ms(tx, 'last_batch_ms')} / {ms(tx, 'duration_limit_ms')}"),
        (_("TX waiting / being handed to transport"), f"{ms(tx, 'pending_ms')} / {ms(tx, 'sending_ms')}"),
        (_("Last transport send call"), ms(tx, "last_send_ms")),
        (_("Actual receive queue"), ms(rx, "receive_queue_ms")),
        (
            _("Speaker / mixed / sink queues"),
            " / ".join(ms(rx, key) for key in ("member_queue_ms", "mixed_queue_ms", "sink_queue_ms")),
        ),
        (
            _("Jitter floor / adaptive target / startup threshold"),
            " / ".join(ms(rx, key) for key in ("jitter_floor_ms", "jitter_target_ms", "startup_threshold_ms")),
        ),
        (_("Buffering / playing speakers"), f"{rx.get('buffering_speakers', 0)} / {rx.get('playing_speakers', 0)}"),
        (_("Packets sent / received"), f"{packets.get('sent', 0)} / {packets.get('received', 0)}"),
        (
            _("Receive worker packets / reorder frames waiting"),
            f"{report.get('receive_worker_pending_packets', 0)} / {report.get('reorder_pending_frames', 0)}",
        ),
        (_("Missing / recovered audio frames"), f"{rx.get('missing_frames', 0)} / {rx.get('recovered_frames', 0)}"),
        (_("Late or duplicate packets"), str(packets.get("late_or_duplicate", 0))),
        (_("Late or duplicate frames discarded"), str(rx.get("late_or_duplicate_discarded_frames", 0))),
        (
            _("Concealed / overflow / bad frames"),
            f"{rx.get('concealed_frames', 0)} / {rx.get('dropped_frames', 0)} / {packets.get('bad_audio_frames', 0)}",
        ),
        (_("Confirmed playout underruns / duration"), f"{rx.get('playout_underruns', 0)} / {ms(rx, 'underrun_ms')}"),
        (_("Currently starved speakers"), str(rx.get("starved_speakers", 0))),
    ]


def transmission_status(client, key="", toggle=False):
    if client is None or not client.connected:
        return "idle", _("Not connected"), ""
    if client.cfg.text_only:
        return "idle", _("Chat-only"), ""
    if client.my_room is None:
        return "idle", _("Join a room to talk"), ""
    if client.muted or client.deaf:
        return "muted", _("Microphone muted"), ""
    if not client.can_speak_here or client.cfg.listen:
        return "idle", _("Listen only"), ""
    packetizer = client.packetizer
    if not packetizer or not client.gate:
        return "idle", _("Audio unavailable"), ""
    tx = packetizer.diagnostics()
    pending = tx["pending_ms"] + tx["sending_ms"]
    detail = _("{0:.1f} s of audio queued locally").format(pending / 1000)
    if client.transmitting:
        if client.cfg.mode == "ptt" and client._ptt_hang_timer is not None:
            return "live", _("Finishing capture (mic still live)"), detail
        return "live", _("MIC LIVE   ") if tx["sending_ms"] else _("MIC LIVE"), detail
    if tx["sending_ms"]:
        return "sending", _("Handing audio to transport"), detail
    if pending:
        return "sending", _("Audio queued"), detail
    if client.cfg.mode == "vox":
        return "idle", _("Waiting for voice"), ""
    prompt = _("Press to start / stop") if toggle else _("Hold to talk")
    if key:
        prompt += f" [{key}]"
    return "idle", _("Ready to talk"), prompt
