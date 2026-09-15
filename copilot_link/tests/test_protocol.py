"""Unit tests: serialization, version/type/field/range/freshness validation."""

import time
import unittest

from copilot_link.common import protocol as P
from copilot_link.common.messages import build_command, build_heartbeat

GOOD_PILOT = {"roll": 1500, "pitch": 1600, "yaw": 1500, "throttle": 1400}
GOOD_COPILOT = {"roll_correction": 0, "pitch_correction": 0, "yaw_correction": 25, "throttle_correction": 0}


def good_cmd(seq=1, ts=None):
    return build_command(seq, GOOD_PILOT, GOOD_COPILOT, "HEADING_HOLD", timestamp=ts if ts is not None else time.time())


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        pkt = good_cmd(42)
        raw = P.encode_packet(pkt)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(P.decode_packet(raw), pkt)

    def test_decode_rejects_garbage(self):
        for bad in (b"", b"   \n", b"{not json\n", b"[1,2]\n", b"\xff\xfe\n"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                P.decode_packet(bad)


class TestValidation(unittest.TestCase):
    def test_valid_command(self):
        ok, _ = P.validate_packet(good_cmd())
        self.assertTrue(ok)

    def test_bad_version(self):
        pkt = good_cmd()
        pkt["version"] = 999
        ok, reason = P.validate_packet(pkt)
        self.assertFalse(ok)
        self.assertIn("version", reason)

    def test_unknown_type(self):
        pkt = good_cmd()
        pkt["type"] = "teleport"
        ok, _ = P.validate_packet(pkt)
        self.assertFalse(ok)

    def test_missing_fields(self):
        for key in ("version", "type", "sequence", "timestamp", "payload"):
            pkt = good_cmd()
            del pkt[key]
            ok, _ = P.validate_packet(pkt)
            self.assertFalse(ok, msg=key)

    def test_bad_sequence_types(self):
        for seq in (-1, 1.5, "7", True, None):
            pkt = good_cmd()
            pkt["sequence"] = seq
            ok, _ = P.validate_packet(pkt)
            self.assertFalse(ok, msg=repr(seq))

    def test_stale_packet(self):
        pkt = good_cmd(ts=time.time() - 5.0)
        ok, reason = P.validate_packet(pkt, max_age_s=1.0)
        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_future_timestamp(self):
        pkt = good_cmd(ts=time.time() + 5.0)
        ok, _ = P.validate_packet(pkt)
        self.assertFalse(ok)

    def test_range_validation(self):
        bad = dict(GOOD_PILOT)
        bad["roll"] = 999
        pkt = build_command(1, bad, GOOD_COPILOT, "HEADING_HOLD", timestamp=time.time())
        ok, _ = P.validate_packet(pkt)
        self.assertFalse(ok)
        bad2 = dict(GOOD_COPILOT)
        bad2["yaw_correction"] = 501
        pkt2 = build_command(1, GOOD_PILOT, bad2, "HEADING_HOLD", timestamp=time.time())
        ok, _ = P.validate_packet(pkt2)
        self.assertFalse(ok)

    def test_bad_mode(self):
        pkt = build_command(1, GOOD_PILOT, GOOD_COPILOT, "LUDA", timestamp=time.time())
        ok, _ = P.validate_packet(pkt)
        self.assertFalse(ok)

    def test_heartbeat_ok(self):
        ok, _ = P.validate_packet(build_heartbeat(3))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
