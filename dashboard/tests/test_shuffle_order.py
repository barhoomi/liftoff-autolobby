"""Tests for dashboard.control.shuffle_order -- the read-only mirror of the plugin's
persisted shuffle deal (bug-comma-in-track-name.md, Bug 3, operator live report: the
dashboard's rotation panel always showed definition order even with shuffle mode on).
"""

import os

from dashboard.control.protocol import ProtocolDir
from dashboard.control.shuffle_order import compute_tracks_signature, read_active_order


def _write_raw_shuffle_order_file(proto, content):
    """Writes shuffle_order.txt directly to disk, bypassing ProtocolDir.write_text's
    RESET_ONLY ownership gate -- simulates the PLUGIN (the only legitimate writer)
    having produced this exact content, valid or malformed."""
    os.makedirs(proto.plugins_dir, exist_ok=True)
    with open(proto.path("shuffle_order.txt"), "w", encoding="utf-8") as f:
        f.write(content)


def _independent_fnv1a(lines):
    """A second, independently-written FNV-1a (32-bit) implementation -- not a call
    into the module under test -- used to cross-check compute_tracks_signature against
    the well-known algorithm rather than only against itself."""
    h = 0x811C9DC5  # 2166136261
    for line in lines:
        for byte in line.encode("utf-8"):
            h = (h ^ byte) & 0xFFFFFFFF
            h = (h * 0x01000193) & 0xFFFFFFFF  # 16777619
        h = (h ^ 0x0A) & 0xFFFFFFFF
        h = (h * 0x01000193) & 0xFFFFFFFF
    return format(h, "08x")


class TestComputeTracksSignature:
    def test_matches_an_independent_fnv1a_implementation(self):
        lines = ["A,Bando City,Race", "B,The Green,Race", "Iceberg, Right ahead!,Bando City,Race"]
        assert compute_tracks_signature(lines) == _independent_fnv1a(lines)

    def test_is_stable_across_calls(self):
        lines = ["A,Bando City,Race", "B,The Green,Race"]
        assert compute_tracks_signature(lines) == compute_tracks_signature(list(lines))

    def test_differs_when_content_changes(self):
        assert compute_tracks_signature(["A,Env,Mode"]) != compute_tracks_signature(["B,Env,Mode"])

    def test_line_order_matters(self):
        assert compute_tracks_signature(["A,Env,Mode", "B,Env,Mode"]) != \
            compute_tracks_signature(["B,Env,Mode", "A,Env,Mode"])

    def test_is_8_lowercase_hex_chars(self):
        sig = compute_tracks_signature(["A,Env,Mode"])
        assert len(sig) == 8
        assert sig == sig.lower()
        int(sig, 16)  # does not raise


class TestReadActiveOrder:
    def _proto(self, tmp_path):
        return ProtocolDir(str(tmp_path / "plugins"))

    def _write_valid_shuffle_order(self, proto, static_lines, order):
        sig = compute_tracks_signature(static_lines)
        content = "# signature:{}\n".format(sig) + "\n".join(str(i) for i in order) + "\n"
        _write_raw_shuffle_order_file(proto, content)

    def test_missing_file_is_not_shuffled(self, tmp_path):
        proto = self._proto(tmp_path)
        order, shuffled = read_active_order(proto, ["A,Env,Mode", "B,Env,Mode"])
        assert order is None
        assert shuffled is False

    def test_valid_permutation_is_returned(self, tmp_path):
        proto = self._proto(tmp_path)
        static_lines = ["A,Env,Mode", "B,Env,Mode", "C,Env,Mode"]
        self._write_valid_shuffle_order(proto, static_lines, [2, 0, 1])
        order, shuffled = read_active_order(proto, static_lines)
        assert order == [2, 0, 1]
        assert shuffled is True

    def test_stale_signature_falls_back(self, tmp_path):
        """The file was dealt against an OLDER tracks_to_rotate.txt (a playlist swap or
        hand-edit) -- the plugin self-heals by re-dealing; the dashboard must fall back,
        never try to fix the file up itself."""
        proto = self._proto(tmp_path)
        old_lines = ["A,Env,Mode", "B,Env,Mode"]
        new_lines = ["A,Env,Mode", "B,Env,Mode", "C,Env,Mode"]
        self._write_valid_shuffle_order(proto, old_lines, [1, 0])
        order, shuffled = read_active_order(proto, new_lines)
        assert order is None
        assert shuffled is False

    def test_wrong_length_falls_back(self, tmp_path):
        proto = self._proto(tmp_path)
        static_lines = ["A,Env,Mode", "B,Env,Mode", "C,Env,Mode"]
        sig = compute_tracks_signature(static_lines)
        _write_raw_shuffle_order_file(proto, "# signature:{}\n0\n1\n".format(sig))  # only 2 of 3
        order, shuffled = read_active_order(proto, static_lines)
        assert order is None
        assert shuffled is False

    def test_out_of_range_index_falls_back(self, tmp_path):
        proto = self._proto(tmp_path)
        static_lines = ["A,Env,Mode", "B,Env,Mode"]
        sig = compute_tracks_signature(static_lines)
        _write_raw_shuffle_order_file(proto, "# signature:{}\n0\n5\n".format(sig))
        order, shuffled = read_active_order(proto, static_lines)
        assert order is None
        assert shuffled is False

    def test_duplicate_index_falls_back(self, tmp_path):
        proto = self._proto(tmp_path)
        static_lines = ["A,Env,Mode", "B,Env,Mode"]
        sig = compute_tracks_signature(static_lines)
        _write_raw_shuffle_order_file(proto, "# signature:{}\n0\n0\n".format(sig))
        order, shuffled = read_active_order(proto, static_lines)
        assert order is None
        assert shuffled is False

    def test_missing_signature_header_falls_back(self, tmp_path):
        proto = self._proto(tmp_path)
        static_lines = ["A,Env,Mode"]
        _write_raw_shuffle_order_file(proto, "0\n")
        order, shuffled = read_active_order(proto, static_lines)
        assert order is None
        assert shuffled is False

    def test_corrupt_index_line_falls_back(self, tmp_path):
        proto = self._proto(tmp_path)
        static_lines = ["A,Env,Mode", "B,Env,Mode"]
        sig = compute_tracks_signature(static_lines)
        _write_raw_shuffle_order_file(proto, "# signature:{}\nnot-a-number\n1\n".format(sig))
        order, shuffled = read_active_order(proto, static_lines)
        assert order is None
        assert shuffled is False

    def test_empty_static_list_is_not_shuffled(self, tmp_path):
        proto = self._proto(tmp_path)
        order, shuffled = read_active_order(proto, [])
        assert order is None
        assert shuffled is False

    def test_never_writes_shuffle_order_txt(self, tmp_path):
        """The dashboard must never create/modify this plugin-owned file, even as a
        side effect of reading and finding it absent or invalid."""
        proto = self._proto(tmp_path)
        static_lines = ["A,Env,Mode"]
        read_active_order(proto, static_lines)
        assert not proto.exists("shuffle_order.txt")
