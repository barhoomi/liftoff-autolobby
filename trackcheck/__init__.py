"""trackcheck — shared track/race validation, quality-gate and playlist-lint library.

Single source of truth for "is this track/race pair correct, and is it any good?",
used by (or destined to be used by):

1. The workshop installer (`workshop-steamcmd-install.md`, not yet built) — filters
   downloaded items before they enter the master list / rotation.
2. The procedural generator's quality gate (`procedural-gen-improvements.md`, not yet
   built) — rejects bad seeds before workshop upload.
3. The playlist lint CLI (this package, `python3 -m trackcheck.lint_playlists`) —
   validates playlists.json against master_tracks_list.json at commit/test time.
4. `orchestrator/gather_tracks.py` — imports the Layer 1 XML parser so there is
   exactly one implementation of the robust-parsing logic (CLAUDE.md rule 4).

See docs/features/doing/track-validation-quality-gate.md for the full spec.

Public API surface (what the three future consumers should import):

    from trackcheck.parser import parse_track_file, parse_race_file, ENV_MAPPING, normalize_env
    from trackcheck.validate import validate_item, Report, Reason
    from trackcheck.geometry import geometry_from_blueprints, geometry_from_files, TrackGeometry
    from trackcheck.quality import compute_metrics, quality_gate, classify, DEFAULT_THRESHOLDS, QualityMetrics, QualityResult
    from trackcheck.playlist_match import resolve_playlist_item, normalize_playlist_item, ENV_NORMALIZATION
"""
