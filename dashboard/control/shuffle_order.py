"""Read-only mirror of the plugin's persisted shuffle deal (``shuffle_order.txt``).

bug-comma-in-track-name.md's sibling bug report (operator, same live session, filed as
"Bug 3"): the dashboard's rotation panel always showed ``tracks_to_rotate.txt``'s own
(definition) order, even with shuffle mode on -- because ``dashboard.control.state`` never
looked at the actual walk order the plugin is using.

``shuffle_order.txt`` is **plugin-owned runtime state** (see AGENTS.md's protocol-files
bullet and ``docs/features/done/bug-shuffle-toggle-and-tracks-incompatibility.md``): the
plugin is the only writer, self-healing on any mismatch. This module only ever *reads* it
(``ProtocolDir.read_text`` has no ownership gate on reads -- only writes are gated) and
never writes it; when the persisted deal cannot be trusted for the CURRENT
``tracks_to_rotate.txt`` content, callers must fall back to definition order rather than
try to reconstruct or "correct" the file themselves (AGENTS.md rule 4 -- re-deriving a
second copy of the plugin's own derived state is exactly the stale-copy shape that bit
the original shuffle bug).

File format (mirrors ``Plugin.Rotation.cs``'s ``DealAndPersistShuffleOrder`` /
``TryLoadPersistedShuffleOrder``)::

    # signature:<8 hex chars>
    <index>
    <index>
    ...

one line per static-file index (a permutation of ``0..n-1``), guarded by a content
signature of the static track list so a deal computed against an old
``tracks_to_rotate.txt`` is never walked against content it no longer matches.
"""

import re

FNV_OFFSET_BASIS_32 = 2166136261
FNV_PRIME_32 = 16777619
MASK_32 = 0xFFFFFFFF

SIGNATURE_PREFIX = "# signature:"


def compute_tracks_signature(static_lines):
    """Python twin of the plugin's ``ComputeTracksSignature`` (FNV-1a, 32-bit, hex).

    ``static_lines`` must be the exact raw (trimmed, comment/blank-filtered) lines of
    ``tracks_to_rotate.txt`` -- i.e. ``ProtocolDir.read_static_track_lines()`` -- NOT the
    parsed ``{track, environment, mode}`` dicts from ``read_rotation_tracks()``, whose
    reconstruction loses the exact original whitespace the plugin's own hash is over.
    """
    h = FNV_OFFSET_BASIS_32
    for line in static_lines:
        for b in line.encode("utf-8"):
            h ^= b
            h = (h * FNV_PRIME_32) & MASK_32
        h ^= 0x0A  # '\n' line separator, matching the plugin's per-line terminator
        h = (h * FNV_PRIME_32) & MASK_32
    return format(h, "08x")


def read_active_order(protocol, static_lines):
    """Return ``(order, shuffled)``.

    ``order`` is a permutation of ``range(len(static_lines))`` -- the plugin's actual
    walk order -- when ``shuffle_order.txt`` exists, is well-formed, and its signature
    matches ``static_lines`` exactly as persisted (byte-identical validation to the
    plugin's own ``TryLoadPersistedShuffleOrder``). Otherwise ``order`` is ``None``: the
    caller must fall back to definition order rather than guess, because the dashboard
    is not the plugin and must never write this file (see module docstring).

    ``shuffled`` is ``True`` only when a permutation was found and trusted (i.e.
    ``order is not None``) -- it is what callers use to label the presented order in the
    UI ("shuffled" vs "definition order").
    """
    n = len(static_lines)
    if n == 0:
        return None, False

    raw = protocol.read_text("shuffle_order.txt")
    if raw is None:
        return None, False

    lines = raw.splitlines()
    if not lines or not lines[0].startswith(SIGNATURE_PREFIX):
        return None, False

    signature = lines[0][len(SIGNATURE_PREFIX):].strip()
    if signature != compute_tracks_signature(static_lines):
        return None, False

    order = []
    seen = set()
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        # Strict digits-only match (mirrors C#'s int.TryParse rejecting anything
        # int.Parse itself wouldn't accept, e.g. Python-only underscore separators).
        if not re.fullmatch(r"-?\d+", stripped):
            return None, False
        idx = int(stripped)
        if idx < 0 or idx >= n or idx in seen:
            return None, False
        seen.add(idx)
        order.append(idx)

    if len(order) != n:
        return None, False
    return order, True
