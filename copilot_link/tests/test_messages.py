"""Unit tests: sequence handling (duplicates, gaps, reorder) + builders + integration."""

import socket
import threading
import time
import unittest

from copilot_link.aircraft.server import CopilotServer
from copilot_link.common import protocol as P
from copilot_link.common.config import LinkConfig
from copilot_link.common.messages import build_ack, build_command, build_heartbeat, build_status, simulated_command


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeConn:
    """Minimal stand-in capturing server -> client bytes."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


def _cmd(seq, ts=None):
    pilot = {"roll": 1500, "pitch": 1600, "yaw": 1500, "throttle": 1400}
    copilot = {"roll_correction": 0, "pitch_correction": 0, "yaw_correction": 0, "throttle_correction": 0}
    return build_command(seq, pilot, copilot, "MANUAL", timestamp=time.time() if ts is None else ts)


class TestSequenceHandling(unittest.TestCase):
    def setUp(self):
        cfg = LinkConfig(server_host="127.0.0.1", server_port=1)
        self.server = CopilotServer(cfg)
        self.conn = FakeConn()

    def feed(self, seq):
        self.server._process_command(self.conn, _cmd(seq), time.time())

    def test_in_order_no_flags(self):
        for s in (10, 11, 12):
            self.feed(s)
        snap = self.server.stats.snapshot()
        self.assertEqual(snap["received"], 3)
        self.assertEqual(snap["duplicates"], 0)
        self.assertEqual(snap["out_of_order"], 0)
        self.assertEqual(snap["lost_estimate"], 0)

    def test_duplicate_detection(self):
        self.feed(20)
        self.feed(20)
        snap = self.server.stats.snapshot()
        self.assertEqual(snap["duplicates"], 1)
        self.assertEqual(snap["received"], 1)  # duplicate not counted as new

    def test_gap_detected_as_loss(self):
        self.feed(30)
        self.feed(33)  # 31, 32 missing
        snap = self.server.stats.snapshot()
        self.assertEqual(snap["gaps"], 1)
        self.assertEqual(snap["lost_estimate"], 2)

    def test_out_of_order(self):
        self.feed(40)
        self.feed(41)
        self.feed(40)  # dup, not ooo
        self.feed(39)  # late arrival below latest -> ooo (new seq value)
        snap = self.server.stats.snapshot()
        self.assertEqual(snap["duplicates"], 1)
        self.assertEqual(snap["out_of_order"], 1)

    def test_stale_rejected_never_crashes(self):
        old = _cmd(50, ts=time.time() - 10.0)
        line = P.encode_packet(old)
        before = self.server.stats.snapshot()["rejected"]
        self.server._process_line(self.conn, line, 0.0)
        after = self.server.stats.snapshot()["rejected"]
        self.assertEqual(after, before + 1)

    def test_malformed_line_rejected(self):
        before = self.server.stats.snapshot()["rejected"]
        self.server._process_line(self.conn, b"{nope\n", 0.0)
        self.assertEqual(self.server.stats.snapshot()["rejected"], before + 1)


class TestBuilders(unittest.TestCase):
    def test_simulated_command_valid(self):
        pkt = simulated_command(7, 1.23)
        ok, reason = P.validate_packet(pkt)
        self.assertTrue(ok, reason)

    def test_ack_status_validate(self):
        ok, _ = P.validate_packet(build_heartbeat(1))
        self.assertTrue(ok)
        ok, _ = P.validate_packet(build_ack(1, True))
        self.assertTrue(ok)
        ok, _ = P.validate_packet(build_status({"received": 1}))
        self.assertTrue(ok)


class TestIntegrationLoopback(unittest.TestCase):
    """Full TCP round trip on localhost: server + raw client exchange."""

    def test_command_ack_status_flow(self):
        port = _free_port()
        cfg = LinkConfig(server_host="127.0.0.1", server_port=port,
                         heartbeat_timeout_s=5.0, status_interval_s=10.0)
        server = CopilotServer(cfg)
        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.settimeout(3.0)
            f = sock.makefile("rb")

            def read_ack(want_seq: int, tries: int = 10) -> dict:
                for _ in range(tries):
                    line = f.readline()
                    assert line, "expected ACK from server"
                    pkt = P.decode_packet(line)
                    if pkt["type"] == "status":
                        continue  # periodic STATUS may interleave; skip
                    assert pkt["type"] == "ack", pkt
                    assert pkt["payload"]["ack_seq"] == want_seq, pkt
                    return pkt
                raise AssertionError(f"no ACK for seq={want_seq}")

            for seq in range(1, 11):
                sock.sendall(P.encode_packet(_cmd(seq)))
                read_ack(seq)
            # heartbeat then clean close
            sock.sendall(P.encode_packet(build_heartbeat(99)))
            read_ack(99)
            sock.close()
            time.sleep(0.5)
            snap = server.stats.snapshot()
            self.assertEqual(snap["received"], 10)
            self.assertEqual(snap["rejected"], 0)
        finally:
            server.stop()
            time.sleep(0.6)


if __name__ == "__main__":
    unittest.main()
