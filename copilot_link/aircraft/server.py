"""Aircraft-side communication server (runs on Pi Zero 2 W).

SAFETY: this module only receives simulated command data and reports
link statistics. It has NO code path to motors, ESCs, or a flight
controller (no MSP/DShot/Betaflight). Do not add one in this prototype.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from ..common import protocol as P
from ..common.config import LinkConfig
from ..common.messages import build_ack, build_status

STATE_NO_LINK = "NO_LINK"
STATE_LINK_OK = "LINK_OK"
STATE_LINK_LOST = "COPILOT_LINK_LOST"

log = logging.getLogger("copilot.aircraft")


class LinkStats:
    """Mutable counters for one server lifetime (across reconnects)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.received = 0  # valid COMMAND packets
        self.rejected = 0  # invalid packets (any type)
        self.duplicates = 0
        self.out_of_order = 0
        self.gaps = 0  # missing-sequence events detected
        self.lost_estimate = 0  # sum of gap sizes
        self.latest_seq = -1  # latest COMMAND sequence (heartbeats tracked separately)
        self.seen_cmd: set[int] = set()
        self.seen_hb: set[int] = set()
        self.latency_sum = 0.0
        self.latency_count = 0
        self.latency_max = 0.0
        self.last_heartbeat_time: float | None = None
        self.connection_state = STATE_NO_LINK
        self.heartbeat_ok = False

    def snapshot(self) -> dict:
        with self.lock:
            avg = self.latency_sum / self.latency_count if self.latency_count else 0.0
            return {
                "received": self.received,
                "rejected": self.rejected,
                "duplicates": self.duplicates,
                "out_of_order": self.out_of_order,
                "gaps": self.gaps,
                "lost_estimate": self.lost_estimate,
                "latest_seq": self.latest_seq,
                "avg_latency_ms": round(avg * 1000, 2),
                "max_latency_ms": round(self.latency_max * 1000, 2),
                "heartbeat_ok": self.heartbeat_ok,
                "connection_state": self.connection_state,
            }


class CopilotServer:
    def __init__(self, config: LinkConfig) -> None:
        self.config = config
        self.stats = LinkStats()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._status_seq = 0

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        """Block forever: accept clients, handle reconnects. Returns on stop()."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.config.server_host, self.config.server_port))
        srv.listen(1)
        srv.settimeout(0.5)
        self._sock = srv
        log.info("aircraft listening on %s:%d", self.config.server_host, self.config.server_port)
        print(f"[aircraft] listening on {self.config.server_host}:{self.config.server_port}", flush=True)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                self._check_heartbeat_timeout()
                continue
            except OSError:
                break
            log.info("ground connected from %s", addr)
            print(f"[aircraft] ground connected from {addr}", flush=True)
            with self.stats.lock:
                self.stats.connection_state = STATE_LINK_OK
            try:
                self._handle_client(conn)
            except Exception as exc:  # never crash the accept loop
                log.warning("client handler error: %s", exc)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            log.info("ground disconnected; waiting to reconnect")
            print("[aircraft] ground disconnected; waiting to reconnect", flush=True)
            with self.stats.lock:
                if self.stats.connection_state == STATE_LINK_OK:
                    self.stats.connection_state = STATE_NO_LINK
        srv.close()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    # -- per-client ----------------------------------------------------
    def _handle_client(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        buf = b""
        last_status_push = time.time()
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                self._check_heartbeat_timeout()
                self._maybe_push_status(conn, last_status_push)
                continue
            except OSError:
                break
            if not chunk:  # clean disconnect
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    last_status_push = self._process_line(conn, line, last_status_push)
            self._check_heartbeat_timeout()
            last_status_push = self._maybe_push_status(conn, last_status_push)

    def _maybe_push_status(self, conn: socket.socket, last: float) -> float:
        now = time.time()
        if now - last >= self.config.status_interval_s:
            try:
                self._status_seq += 1
                conn.sendall(P.encode_packet(build_status(self.stats.snapshot(), self._status_seq)))
            except OSError:
                pass
            return now
        return last

    def _process_line(self, conn: socket.socket, line: bytes, last_status: float) -> float:
        now = time.time()
        try:
            packet = P.decode_packet(line)
        except ValueError as exc:
            self._reject(f"undecodable packet: {exc}")
            return last_status
        ok, reason = P.validate_packet(packet, now=now, max_age_s=self.config.max_packet_age_s)
        if not ok:
            self._reject(f"seq={packet.get('sequence')} invalid: {reason}")
            # Best-effort negative ACK when we can extract a seq number.
            seq = packet.get("sequence")
            if isinstance(seq, int) and packet.get("type") in (P.TYPE_COMMAND, P.TYPE_HEARTBEAT):
                try:
                    conn.sendall(P.encode_packet(build_ack(seq, False, reason)))
                except OSError:
                    pass
            return last_status

        msg_type = packet["type"]
        seq = packet["sequence"]
        if msg_type == P.TYPE_HEARTBEAT:
            with self.stats.lock:
                if seq in self.stats.seen_hb:
                    self.stats.duplicates += 1
                else:
                    self.stats.seen_hb.add(seq)
                self.stats.last_heartbeat_time = now
                self.stats.heartbeat_ok = True
                if self.stats.connection_state != STATE_LINK_OK:
                    self.stats.connection_state = STATE_LINK_OK
            try:
                conn.sendall(P.encode_packet(build_ack(seq, True, "heartbeat")))
            except OSError:
                pass
            return last_status
        if msg_type == P.TYPE_COMMAND:
            self._process_command(conn, packet, now)
            return last_status
        # ACK/STATUS arriving at the server: legal packets, just informational.
        log.debug("ignoring %s packet from ground", msg_type)
        return last_status

    def _process_command(self, conn: socket.socket, packet: dict, now: float) -> None:
        seq = packet["sequence"]
        age = now - float(packet["timestamp"])
        payload = packet["payload"]
        with self.stats.lock:
            if seq in self.stats.seen_cmd:
                self.stats.duplicates += 1
                dup = True
            else:
                dup = False
                self.stats.seen_cmd.add(seq)
                if self.stats.latest_seq != -1 and seq <= self.stats.latest_seq:
                    self.stats.out_of_order += 1
                if self.stats.latest_seq != -1 and seq > self.stats.latest_seq + 1:
                    gap = seq - self.stats.latest_seq - 1
                    self.stats.gaps += 1
                    self.stats.lost_estimate += gap
                if seq > self.stats.latest_seq:
                    self.stats.latest_seq = seq
                self.stats.received += 1
                self.stats.latency_sum += max(age, 0.0)
                self.stats.latency_count += 1
                if age > self.stats.latency_max:
                    self.stats.latency_max = max(age, 0.0)
        if dup:
            log.info("duplicate COMMAND seq=%d ignored", seq)
            try:
                conn.sendall(P.encode_packet(build_ack(seq, True, "duplicate")))
                log.debug("ACK seq=%d sent (duplicate response)", seq)
            except OSError as exc:
                log.debug("ACK seq=%d send failed: %s", seq, exc)
            return
        pilot = payload["pilot"]
        copilot = payload["copilot"]
        print(
            f"RX COMMAND seq={seq} age={age*1000:.1f} ms "
            f"pitch={pilot['pitch']} yaw={pilot['yaw']} "
            f"yaw_correction={copilot['yaw_correction']} mode={payload['mode']}",
            flush=True,
        )
        log.debug("RX COMMAND seq=%d age=%.3fms", seq, age * 1000)
        try:
            conn.sendall(P.encode_packet(build_ack(seq, True, "ok")))
            log.debug("ACK seq=%d sent", seq)
        except OSError as exc:
            log.debug("ACK seq=%d send failed: %s", seq, exc)

    def _reject(self, reason: str) -> None:
        with self.stats.lock:
            self.stats.rejected += 1
        log.info("rejected packet: %s", reason)
        print(f"[aircraft] REJECT {reason}", flush=True)

    def _check_heartbeat_timeout(self) -> None:
        with self.stats.lock:
            last = self.stats.last_heartbeat_time
            state = self.stats.connection_state
        if last is None:
            return  # no heartbeat yet; stay in NO_LINK / LINK_OK
        if time.time() - last > self.config.heartbeat_timeout_s:
            with self.stats.lock:
                self.stats.heartbeat_ok = False
                self.stats.connection_state = STATE_LINK_LOST
            if state != STATE_LINK_LOST:
                log.warning("heartbeat timeout -> COPILOT_LINK_LOST")
                print("[aircraft] HEARTBEAT TIMEOUT -> COPILOT_LINK_LOST", flush=True)
