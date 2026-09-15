"""Ground-side communication client (runs on Pi 4B / Pi 5).

Sends simulated COMMANDs at a configurable rate plus periodic HEARTBEATs,
listens for ACK/STATUS, tracks RTT, and auto-reconnects.

SIMULATED DATA ONLY: values come from messages.simulated_command().
Nothing here reads RC hardware or drives a flight controller.

Stress-test (chaos) options intentionally corrupt the outgoing stream to
prove the aircraft side detects loss/duplicates/reordering/malformed
packets and link loss. Disabled by default (all rates = 0).
"""

from __future__ import annotations

import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, field

from ..common import protocol as P
from ..common.config import LinkConfig
from ..common.messages import build_heartbeat, simulated_command

log = logging.getLogger("copilot.ground")


@dataclass
class ChaosConfig:
    loss_rate: float = 0.0  # drop packet before send
    delay_s: float = 0.0  # extra sleep before send (applies to a fraction)
    delay_rate: float = 0.0  # fraction of packets delayed
    duplicate_rate: float = 0.0  # send packet twice
    reorder_rate: float = 0.0  # hold one packet, send after the next
    malformed_rate: float = 0.0  # send garbage instead of packet
    drop_connection_s: float = 0.0  # if >0: close socket every N sec ...
    drop_duration_s: float = 0.0  # ... for M sec to test reconnect


@dataclass
class ClientStats:
    sent: int = 0  # COMMAND packets sent
    acked: int = 0  # COMMAND packets acknowledged ok
    rejected: int = 0  # COMMAND packets negatively acknowledged
    dup_acks: int = 0  # extra ACKs for already-acked seqs (expected when chaos duplicates packets)
    hb_sent: int = 0
    hb_acked: int = 0
    rtt_sum: float = 0.0  # COMMAND round-trip samples only
    rtt_max: float = 0.0
    last_status: dict = field(default_factory=dict)


# Heartbeat sequence numbers live in their own space so they can never
# collide with COMMAND sequence numbers (ACKs carry only a number).
HB_SEQ_BASE = 1_000_000_000


class CopilotClient:
    def __init__(self, config: LinkConfig, chaos: ChaosConfig | None = None, verbose: bool = False) -> None:
        self.config = config
        self.chaos = chaos or ChaosConfig()
        self.verbose = verbose
        self.stats = ClientStats()
        self._cmd_times: dict[int, float] = {}
        self._hb_times: dict[int, float] = {}
        self._unacked: set[int] = set()  # diagnostics: COMMAND seqs awaiting ACK
        self._acked_done: set[int] = set()  # COMMAND seqs already ACKed (to recognize duplicate ACKs)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._held: bytes | None = None  # one-packet reorder buffer
        self._sock: socket.socket | None = None
        # Stream progress lives on self so a mid-run reconnect resumes
        # exactly where it left off (no seq rewind, no count reset).
        self._seq = 1
        self._hb_seq = HB_SEQ_BASE
        self._sent_total = 0

    def run(self, count: int = 0, duration_s: float = 0.0) -> ClientStats:
        """Connect (with retry) and stream until count/duration reached.

        count=0 and duration_s=0.0 means run forever (until Ctrl-C).
        Reconnects automatically on failure, preserving sequence numbers.
        """
        start = time.time()
        rng = random.Random()
        finished = False
        while not self._stop.is_set():
            try:
                self._connect()
            except OSError as exc:
                log.warning("connect failed (%s); retrying in 1s", exc)
                time.sleep(1.0)
                continue
            try:
                finished = self._stream_loop(count, duration_s, start, rng)
            except (OSError, ConnectionError) as exc:
                log.warning("connection lost (%s); reconnecting", exc)
                print(f"[ground] connection lost ({exc}); reconnecting...", flush=True)
                time.sleep(0.5)
                continue
            break  # _stream_loop returned normally (count/duration reached)
        if finished:
            # Linger briefly so trailing ACKs and the final STATUS arrive
            # before we print the summary. Keep heartbeating so the server
            # does not (correctly) declare link loss during the linger.
            linger_until = time.time() + self.config.status_interval_s + 0.5
            while time.time() < linger_until and not self._stop.is_set():
                try:
                    assert self._sock is not None
                    with self._lock:
                        self._hb_times[self._hb_seq] = time.time()
                    self._send_raw(self._sock, P.encode_packet(build_heartbeat(self._hb_seq)))
                    self.stats.hb_sent += 1
                    self._hb_seq += 1
                except (OSError, AssertionError):
                    with self._lock:
                        self._hb_times.pop(self._hb_seq, None)
                    break
                time.sleep(self.config.heartbeat_interval_s)
        self._close_current()
        self.print_summary()
        return self.stats

    def stop(self) -> None:
        self._stop.set()
        self._close_current()

    def _close_current(self) -> None:
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        finally:
            self._sock = None

    # -- internals -----------------------------------------------------
    def _connect(self) -> None:
        sock = socket.create_connection((self.config.server_host, self.config.server_port), timeout=5.0)
        sock.settimeout(0.2)
        self._sock = sock
        log.info("connected to %s:%d", self.config.server_host, self.config.server_port)
        print(f"[ground] connected to {self.config.server_host}:{self.config.server_port}", flush=True)
        reader = threading.Thread(target=self._reader_loop, args=(sock,), daemon=True)
        reader.start()

    def _stream_loop(self, count: int, duration_s: float, start: float, rng: random.Random) -> bool:
        """Stream until count/duration reached (True) or stop requested (False).

        Raises OSError/ConnectionError on transport failure; self._seq /
        self._hb_seq / self._sent_total already reflect everything sent, so
        the caller can reconnect and resume without rewinding.
        """
        period = 1.0 / max(self.config.command_rate_hz, 0.1)
        last_hb = 0.0
        next_drop_at = time.time() + self.chaos.drop_connection_s if self.chaos.drop_connection_s > 0 else 0.0
        assert self._sock is not None
        sock = self._sock
        t0 = time.time()
        while not self._stop.is_set():
            if count and self._sent_total >= count:
                return True
            if duration_s and (time.time() - start) >= duration_s:
                return True
            loop_start = time.time()

            # Chaos: periodic connection drop.
            if next_drop_at and time.time() >= next_drop_at:
                print(f"[ground] CHAOS: dropping connection for {self.chaos.drop_duration_s}s", flush=True)
                try:
                    sock.close()
                except OSError:
                    pass
                time.sleep(self.chaos.drop_duration_s)
                next_drop_at = time.time() + self.chaos.drop_connection_s
                raise ConnectionError("chaos connection drop")

            # Heartbeat on its own cadence.
            # NOTE: bookkeeping goes BEFORE sendall. The ACK cannot arrive
            # before the bytes hit the wire, so recording first eliminates
            # a send/recv race where a fast ACK overtakes the bookkeeping.
            if time.time() - last_hb >= self.config.heartbeat_interval_s:
                hb = build_heartbeat(self._hb_seq)
                with self._lock:
                    self._hb_times[self._hb_seq] = time.time()
                try:
                    self._send_raw(sock, P.encode_packet(hb))
                except OSError:
                    with self._lock:
                        self._hb_times.pop(self._hb_seq, None)
                    raise
                self.stats.hb_sent += 1
                self._hb_seq += 1
                last_hb = time.time()

            # COMMAND
            elapsed = time.time() - t0
            pkt = simulated_command(self._seq, elapsed)
            raw = P.encode_packet(pkt)
            r = rng.random()
            if r < self.chaos.malformed_rate:
                raw = b"{not valid json\n"
                print(f"[ground] CHAOS: malformed packet (seq {self._seq})", flush=True)
            elif r < self.chaos.malformed_rate + self.chaos.loss_rate:
                print(f"[ground] CHAOS: dropping seq={self._seq}", flush=True)
                self._seq += 1
                self._sent_total += 1
                time.sleep(max(0.0, period - (time.time() - loop_start)))
                continue
            if rng.random() < self.chaos.delay_rate and self.chaos.delay_s > 0:
                time.sleep(self.chaos.delay_s)

            if rng.random() < self.chaos.reorder_rate:
                # Hold this packet, flush previously held one after it (swap order).
                if self._held is not None:
                    with self._lock:
                        self._cmd_times[self._seq] = time.time()
                        self._unacked.add(self._seq)
                    self._send_raw(sock, raw)
                    self._send_raw(sock, self._held)
                    self._held = None
                    print(f"[ground] CHAOS: reordered seq={self._seq}", flush=True)
                else:
                    self._held = raw
                    with self._lock:
                        self._cmd_times[self._seq] = time.time()
                        self._unacked.add(self._seq)
                    self.stats.sent += 1
                    self._sent_total += 1
                    self._seq += 1
                    time.sleep(max(0.0, period - (time.time() - loop_start)))
                    continue
            else:
                if self._held is not None:
                    self._send_raw(sock, self._held)
                    self._held = None
                with self._lock:
                    self._cmd_times[self._seq] = time.time()
                    self._unacked.add(self._seq)
                try:
                    self._send_raw(sock, raw)
                except OSError:
                    with self._lock:
                        self._cmd_times.pop(self._seq, None)
                        self._unacked.discard(self._seq)
                    raise
            log.debug("SEND seq=%d", self._seq)
            self.stats.sent += 1
            self._sent_total += 1

            if rng.random() < self.chaos.duplicate_rate:
                self._send_raw(sock, raw)  # identical bytes -> duplicate seq
                print(f"[ground] CHAOS: duplicated seq={self._seq}", flush=True)

            payload = pkt["payload"]
            msg = (
                f"SEQ {self._seq:05d} roll={payload['pilot']['roll']} pitch={payload['pilot']['pitch']} "
                f"yaw={payload['pilot']['yaw']} throttle={payload['pilot']['throttle']} "
                f"yaw_correction={payload['copilot']['yaw_correction']} mode={payload['mode']}"
            )
            if self.verbose:
                print(msg, flush=True)
            elif self._sent_total == 1 or self._sent_total % max(int(self.config.command_rate_hz), 1) == 0:
                print(f"[ground] {msg}", flush=True)
            self._seq += 1
            time.sleep(max(0.0, period - (time.time() - loop_start)))
        return False

    def _send_raw(self, sock: socket.socket, raw: bytes) -> None:
        sock.sendall(raw)

    def _reader_loop(self, sock: socket.socket) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        try:
            packet = P.decode_packet(line)
        except ValueError:
            log.debug("ignoring undecodable server line")
            return
        ok, _ = P.validate_packet(packet, max_age_s=10.0)
        if not ok:
            return
        if packet["type"] == P.TYPE_ACK:
            ack_seq = packet["payload"]["ack_seq"]
            with self._lock:
                t_cmd = self._cmd_times.pop(ack_seq, None)
                t_hb = None if t_cmd is not None else self._hb_times.pop(ack_seq, None)
            if t_cmd is not None:
                rtt = time.time() - t_cmd
                if packet["payload"]["result"] == "ok":
                    self.stats.acked += 1
                else:
                    self.stats.rejected += 1
                with self._lock:
                    self._unacked.discard(ack_seq)
                    self._acked_done.add(ack_seq)
                    if len(self._acked_done) > 200000:  # bound memory on endless runs
                        self._acked_done.clear()
                self.stats.rtt_sum += rtt
                if rtt > self.stats.rtt_max:
                    self.stats.rtt_max = rtt
                log.debug("ACK seq=%d rtt=%.1fms", ack_seq, rtt * 1000)
            elif t_hb is not None:
                self.stats.hb_acked += 1
            elif ack_seq in self._acked_done:
                # Expected when chaos duplicates packets: the server ACKs
                # both copies; the second ACK has nothing to match.
                self.stats.dup_acks += 1
                log.debug("duplicate ACK seq=%d", ack_seq)
            else:
                log.debug("ACK for unknown seq=%d (no matching send record)", ack_seq)
        elif packet["type"] == P.TYPE_STATUS:
            with self._lock:
                self.stats.last_status = packet["payload"]
            if self.verbose:
                print(f"[ground] STATUS {packet['payload']}", flush=True)

    def print_summary(self) -> None:
        s = self.stats
        avg_rtt = (s.rtt_sum / s.acked * 1000) if s.acked else 0.0
        print("", flush=True)
        print(f"{s.sent} packets transmitted", flush=True)
        print(f"received (server): see server STATUS below", flush=True)
        print(f"acked:          {s.acked}", flush=True)
        print(f"rejected acks:  {s.rejected}", flush=True)
        print(f"duplicate acks: {s.dup_acks}", flush=True)
        print(f"unacked:        {s.sent - s.acked - s.rejected}", flush=True)
        print(f"heartbeats:     sent={s.hb_sent} acked={s.hb_acked}", flush=True)
        print(f"average RTT:    {avg_rtt:.1f} ms", flush=True)
        print(f"max RTT:        {s.rtt_max*1000:.1f} ms", flush=True)
        with self._lock:
            missing = sorted(self._unacked)[:20]
        if missing:
            print(f"unacked seqs (first {len(missing)}): {missing}", flush=True)
        if s.last_status:
            st = s.last_status
            print(
                f"server: received={st.get('received')} rejected={st.get('rejected')} "
                f"duplicates={st.get('duplicates')} out_of_order={st.get('out_of_order')} "
                f"lost_estimate={st.get('lost_estimate')} state={st.get('connection_state')}",
                flush=True,
            )
