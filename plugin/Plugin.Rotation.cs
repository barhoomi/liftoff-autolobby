using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Reflection;
using System.Collections.Generic;
using System.Linq;
using BepInEx;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;
using Liftoff.Multiplayer;
using Liftoff.Multiplayer.GameSetup;
using Photon.Realtime;
using ExitGames.Client.Photon;
using HarmonyLib;

namespace LiftoffAutoLobby
{
    // MODE: shared
    // Track rotation: peeking/advancing the rotation cursor, shuffle, round-robin
    //     reshuffle, and the loaded-track/history capture used by /info and /history.
    public partial class AutoLobbyPlugin
    {

        // Most-recently-played tracks (newest last), "{env} - {track}" display names, capped at 5.
        // Appended at each submit-success point alongside currentTrackName; read by /history.
        private static readonly List<string> trackHistory = new List<string>();
        private const int TrackHistoryMax = 5;

        // Called at each settings-popup submit-success point (the same instant the track genuinely
        // becomes the loaded one). Snapshots the just-submitted target as the CURRENT track for
        // /info. (Track history is appended here too — see the /history slice.)
        private static void CaptureLoadedTrack()
        {
            currentTrackName = targetTrackName;
            currentEnvironment = targetEnvironment;

            trackHistory.Add($"{targetEnvironment} - {targetTrackName}");
            while (trackHistory.Count > TrackHistoryMax)
                trackHistory.RemoveAt(0);

            // democracy-skip.md: a new track just loaded — stale skip votes from the
            // previous track must not carry over.
            skipVotes.Clear();
        }

        private static string PeekNextTrackName(out string environment, out string gameMode, out int trackIndex)
        {
            environment = "The Drawing Board";
            gameMode = "Classic Race";
            trackIndex = 0;
            UnityEngine.Debug.Log("[AutoLobbyPlugin] PeekNextTrackName called.");
            try
            {
                string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                string statePath = Path.Combine(pluginPath, "rotation_state.txt");
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] tracksPath: {tracksPath}, statePath: {statePath}");

                if (!File.Exists(tracksPath))
                {
                    // Same actionable wording as GetNextTrackFromRotationOnce's identical check
                    // below (player-onboarding-ux.md work item 4) -- this is the "up next" peek,
                    // reached on the same missing-file condition.
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] tracks_to_rotate.txt not found at '{tracksPath}'. Create it there -- one line per track, format: TrackName, Environment, GameMode.");
                    return "";
                }

                List<string> validTracks = ReadStaticTracks(tracksPath);
                if (validTracks.Count == 0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] No valid tracks parsed.");
                    return "";
                }

                // Sync shuffleMode from disk (kept for logging parity; selection walks the
                // persisted/derived active order below -- see bug-shuffle-toggle-and-tracks-
                // incompatibility.md, Option 2).
                shuffleMode = GetShuffleMode();

                int cursor = 0;
                if (File.Exists(statePath))
                {
                    int.TryParse(File.ReadAllText(statePath).Trim(), out cursor);
                }
                if (cursor < 0 || cursor >= validTracks.Count)
                {
                    cursor = 0;
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Current state index: {cursor}, validTracks count: {validTracks.Count}");

                // Read-only: never deals/forces a fresh reshuffle. If shuffle mode is on and no
                // valid deal is persisted yet, this still self-heals (deals + persists one) --
                // that's establishing the derived cache, not advancing rotation progress, so it
                // doesn't violate "no state is persisted by Peek" below (which is about the
                // CURSOR, i.e. rotation_state.txt, never being written here).
                List<int> activeOrder = GetActiveRotationOrder(validTracks, forceReshuffle: false);

                string overrideMode = GetOverrideGameMode();
                string trackName = "";

                // Walk forward from the current position (read-only, no cursor state is
                // persisted by Peek) skipping any session-blacklisted (env, track, mode) combos,
                // so the "up next" chat announcement never names a track that will be skipped
                // instantly.
                for (int attempt = 0; attempt < validTracks.Count; attempt++)
                {
                    int walkPos = (cursor + attempt) % validTracks.Count;
                    int candidateIndex = activeOrder[walkPos];

                    string selectedLine = validTracks[candidateIndex];
                    string[] parts = selectedLine.Split(',');
                    string candidateTrackName = parts[0].Trim();
                    string candidateEnv = parts.Length > 1 ? parts[1].Trim() : environment;
                    string candidateMode = parts.Length > 2 ? parts[2].Trim() : gameMode;
                    if (!string.IsNullOrEmpty(overrideMode)) candidateMode = overrideMode;

                    string key = $"{candidateEnv}|{candidateTrackName}|{candidateMode}";
                    if (sessionBlacklistedTracks.Contains(key)) continue;

                    trackIndex = candidateIndex;
                    environment = candidateEnv;
                    gameMode = candidateMode;
                    trackName = candidateTrackName;
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Selected line: '{selectedLine}'");
                    break;
                }

                return trackName;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in PeekNextTrackName: {ex.Message}");
                return "";
            }
        }

        private static int CountValidRotationLines()
        {
            try
            {
                string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                if (!File.Exists(tracksPath)) return 0;
                int count = 0;
                foreach (var line in File.ReadAllLines(tracksPath))
                {
                    if (!string.IsNullOrWhiteSpace(line) && !line.Trim().StartsWith("#")) count++;
                }
                return count;
            }
            catch
            {
                return 0;
            }
        }

        // Wraps GetNextTrackFromRotationOnce() with a bounded skip-loop so tracks that failed at
        // runtime this session (see sessionBlacklistedTracks) are never selected again until the
        // process restarts. Bounded by the rotation's own line count so a fully-blacklisted
        // rotation can't loop forever.
        // Set inside GetNextTrackFromRotationOnce at the moment a line is selected; read by the
        // wrapper below for the structured "rotation" file event's optional index field.
        private static int lastRotationIndex = -1;

        private static string GetNextTrackFromRotation(out string environment, out string gameMode)
        {
            string trackName = "";
            environment = "The Drawing Board";
            gameMode = "Classic Race";

            int maxAttempts = Math.Max(1, CountValidRotationLines());
            for (int attempt = 0; attempt < maxAttempts; attempt++)
            {
                trackName = GetNextTrackFromRotationOnce(out environment, out gameMode);
                if (string.IsNullOrEmpty(trackName)) break;

                string key = $"{environment}|{trackName}|{gameMode}";
                if (!sessionBlacklistedTracks.Contains(key)) break;

                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Skipping session-blacklisted track '{trackName}' ({environment}, {gameMode}) in rotation.");
            }

            // Structured JSON file event (A3): the honest per-track rotation signal — the bot
            // committing to load a specific next track (rotation_state.txt was just advanced).
            // Emitted once per selection, after any blacklist skips, so it reflects the real
            // chosen track. mode/index are optional; index is the rotation cursor.
            if (!string.IsNullOrEmpty(trackName))
            {
                LogJsonEvent("rotation",
                    ("track", trackName),
                    ("env", environment),
                    ("mode", string.IsNullOrEmpty(gameMode) ? null : gameMode),
                    ("index", lastRotationIndex >= 0 ? (object)lastRotationIndex : null));
            }

            return trackName;
        }

        private static string GetNextTrackFromRotationOnce(out string environment, out string gameMode)
        {
            environment = "The Drawing Board";
            gameMode = "Classic Race";

            try
            {
                string tracksPath = Path.Combine(pluginPath, "tracks_to_rotate.txt");
                string statePath = Path.Combine(pluginPath, "rotation_state.txt");

                if (!File.Exists(tracksPath))
                {
                    // player-onboarding-ux.md work item 4: the old message ("Using default
                    // values.") named no defaults and no path -- actionable only to someone who
                    // already knows the file convention. Say exactly what file, where, and what a
                    // line looks like; "The Drawing Board"/"Classic Race" (this method's own
                    // fallback out-params) really is what gets used next, so that much stays true.
                    UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] tracks_to_rotate.txt not found at '{tracksPath}'. Falling back to 'The Drawing Board' / Classic Race until you create it -- one line per track, format: TrackName, Environment, GameMode (see track_mode_availability.txt in the same folder for names copied straight from this install).");
                    return "";
                }

                List<string> validTracks = ReadStaticTracks(tracksPath);
                if (validTracks.Count == 0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] tracks_to_rotate.txt is empty.");
                    return "";
                }

                // Sync shuffleMode from disk
                shuffleMode = GetShuffleMode();

                int cursor = 0;
                if (File.Exists(statePath))
                    int.TryParse(File.ReadAllText(statePath).Trim(), out cursor);

                if (cursor < 0 || cursor >= validTracks.Count)
                    cursor = 0;

                // tracks_to_rotate.txt's own line order is the STATIC playlist definition in
                // both modes now (bug-shuffle-toggle-and-tracks-incompatibility.md, Option 2) --
                // the plugin never rewrites it. The WALK order (sequential or shuffled) is a
                // separate, derived layer: GetActiveRotationOrder returns a permutation of
                // indices into validTracks (identity when shuffle is off, so plain rotation is
                // byte-for-byte the same behavior as before this fix).
                List<int> activeOrder = GetActiveRotationOrder(validTracks, forceReshuffle: false);
                int index = activeOrder[cursor];
                int nextCursor = (cursor + 1) % validTracks.Count;

                // Once a full pass completes, deal a fresh random order for the next cycle
                // before advancing the cursor back to 0 -- same shuffle-bag semantics as before
                // (see docs/features/done/bug-shuffle-state-persists-across-sessions.md), just
                // persisted as a derived overlay instead of physically rewriting the static file.
                if (shuffleMode && nextCursor == 0)
                {
                    GetActiveRotationOrder(validTracks, forceReshuffle: true);
                }

                File.WriteAllText(statePath, nextCursor.ToString());

                lastRotationIndex = index; // captured for the structured "rotation" file event
                string selectedLine = validTracks[index];

                // Parse line: TrackName,EnvironmentName,GameModeName
                string[] parts = selectedLine.Split(',');
                string trackName = parts[0].Trim();
                if (parts.Length > 1) environment = parts[1].Trim();
                if (parts.Length > 2) gameMode = parts[2].Trim();

                string overrideMode = GetOverrideGameMode();
                if (!string.IsNullOrEmpty(overrideMode))
                {
                    gameMode = overrideMode;
                }

                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Selection: '{trackName}' (Env: '{environment}', Mode: '{gameMode}') [Index {index}]");
                return trackName;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in GetNextTrackFromRotation: {ex.Message}");
                return "";
            }
        }

        // Parses tracks_to_rotate.txt into its ordered list of valid (non-blank, non-'#')
        // lines. This IS the static, authoritative playlist definition
        // (bug-shuffle-toggle-and-tracks-incompatibility.md, Option 2) -- every index used by
        // /tracks, /track, and the active rotation order below is a position in THIS list. The
        // plugin never writes to tracksPath; only the orchestrator (server mode) or the player
        // by hand (client mode) do.
        private static List<string> ReadStaticTracks(string tracksPath)
        {
            var validTracks = new List<string>();
            if (!File.Exists(tracksPath)) return validTracks;
            foreach (var line in File.ReadAllLines(tracksPath))
            {
                if (!string.IsNullOrWhiteSpace(line) && !line.Trim().StartsWith("#"))
                {
                    validTracks.Add(line.Trim());
                }
            }
            return validTracks;
        }

        // shuffle_order.txt persists (server AND client -- this is runtime rotation state, not
        // configuration, the same category as rotation_state.txt, see plugin-mode-split.md's
        // settings-source table) the active WALK order as a permutation of indices into
        // ReadStaticTracks' result. Absent/unused whenever shuffle mode is off -- sequential
        // order needs no derived state at all, it's just 0..N-1 computed on the fly.
        private const string ShuffleOrderFileName = "shuffle_order.txt";

        // Deterministic (cross-process-stable) fingerprint of the static track list's content.
        // Used to detect tracks_to_rotate.txt changing underneath a persisted shuffle_order.txt
        // (an orchestrator playlist swap, or a client player hand-editing the file) so a stale
        // permutation is never walked against content it no longer matches. Deliberately NOT
        // string.GetHashCode(): .NET randomizes string hashing per process by design, so
        // identical content would "change" on every game restart and defeat the entire point of
        // persisting this file (AGENTS.md rule 5 -- shuffle-active order surviving a mid-session
        // crash/restart matters for the server bot; see the feature doc's design record).
        private static string ComputeTracksSignature(List<string> validTracks)
        {
            unchecked
            {
                uint hash = 2166136261; // FNV-1a, 32-bit
                foreach (var line in validTracks)
                {
                    foreach (byte b in Encoding.UTF8.GetBytes(line))
                    {
                        hash ^= b;
                        hash *= 16777619;
                    }
                    hash ^= (byte)'\n'; // line separator, so ["ab","c"] hashes differently from ["a","bc"]
                    hash *= 16777619;
                }
                return hash.ToString("x8");
            }
        }

        // Returns the active walk order as a permutation of indices into validTracks. Identity
        // order (no file I/O at all) whenever shuffle mode is off -- there is nothing to persist
        // for plain sequential rotation, and this makes shuffle-off byte-for-byte the same
        // behavior as before this fix. When shuffle mode is on, loads the persisted deal from
        // shuffle_order.txt if it is still valid for the CURRENT static content; deals and
        // persists a fresh round-robin-by-environment order otherwise (forceReshuffle, a missing
        // file, a signature mismatch, or a corrupt/wrong-length file all count as "invalid" --
        // self-healing, so this can never get stuck referencing content that no longer exists,
        // and never requires the orchestrator to keep a second file in sync, per AGENTS.md
        // rule 4).
        private static List<int> GetActiveRotationOrder(List<string> validTracks, bool forceReshuffle)
        {
            int n = validTracks.Count;
            if (!shuffleMode || n == 0)
            {
                var identity = new List<int>(n);
                for (int i = 0; i < n; i++) identity.Add(i);
                return identity;
            }

            string path = Path.Combine(pluginPath, ShuffleOrderFileName);
            string signature = ComputeTracksSignature(validTracks);

            if (!forceReshuffle)
            {
                List<int> loaded = TryLoadPersistedShuffleOrder(path, signature, n);
                if (loaded != null) return loaded;
            }

            return DealAndPersistShuffleOrder(validTracks, path, signature);
        }

        private static List<int> TryLoadPersistedShuffleOrder(string path, string expectedSignature, int expectedCount)
        {
            try
            {
                if (!File.Exists(path)) return null;
                string[] lines = File.ReadAllLines(path);
                if (lines.Length < 1 || !lines[0].StartsWith("# signature:")) return null;
                string signature = lines[0].Substring("# signature:".Length).Trim();
                if (!string.Equals(signature, expectedSignature, StringComparison.Ordinal)) return null;

                var order = new List<int>();
                var seen = new HashSet<int>();
                for (int i = 1; i < lines.Length; i++)
                {
                    string trimmed = lines[i].Trim();
                    if (trimmed.Length == 0) continue;
                    int idx;
                    if (!int.TryParse(trimmed, out idx)) return null;
                    if (idx < 0 || idx >= expectedCount || !seen.Add(idx)) return null;
                    order.Add(idx);
                }
                return order.Count == expectedCount ? order : null;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to read {ShuffleOrderFileName}, dealing a fresh order: {ex.Message}");
                return null;
            }
        }

        private static List<int> DealAndPersistShuffleOrder(List<string> validTracks, string path, string signature)
        {
            List<int> order = RoundRobinShuffleByEnvironment(validTracks);
            try
            {
                var sb = new StringBuilder();
                sb.Append("# signature:").Append(signature).Append('\n');
                foreach (var idx in order) sb.Append(idx).Append('\n');
                File.WriteAllText(path, sb.ToString());
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to write {ShuffleOrderFileName}: {ex.Message}");
            }
            return order;
        }

        // Shuffles each environment's indices among themselves, shuffles the order environments
        // are visited in, then interleaves round-robin across environments. A flat shuffle can
        // (and does, by chance) land two tracks from the same environment back-to-back — the
        // environment is the dominant visual cue players notice, so that reads as "stale" even
        // though no individual track repeated. Round-robin guarantees same-environment picks
        // are spread apart (until an environment runs out of tracks) while staying random.
        // Operates on INDICES into validTracks (not the line strings themselves) so two
        // textually-identical lines can never be conflated — see bug-shuffle-toggle-and-tracks-
        // incompatibility.md, Option 2 (this used to shuffle and return List<string> lines
        // directly, back when tracks_to_rotate.txt itself was the thing being rewritten).
        private static List<int> RoundRobinShuffleByEnvironment(List<string> validTracks)
        {
            var groups = new Dictionary<string, List<int>>();
            var envOrder = new List<string>();
            for (int i = 0; i < validTracks.Count; i++)
            {
                string[] parts = validTracks[i].Split(',');
                string env = parts.Length > 1 ? parts[1].Trim() : "";
                if (!groups.ContainsKey(env))
                {
                    groups[env] = new List<int>();
                    envOrder.Add(env);
                }
                groups[env].Add(i);
            }

            foreach (var env in envOrder)
            {
                var g = groups[env];
                for (int i = g.Count - 1; i > 0; i--)
                {
                    int j = rng.Next(0, i + 1);
                    int temp = g[i]; g[i] = g[j]; g[j] = temp;
                }
            }

            for (int i = envOrder.Count - 1; i > 0; i--)
            {
                int j = rng.Next(0, i + 1);
                string temp = envOrder[i]; envOrder[i] = envOrder[j]; envOrder[j] = temp;
            }

            var result = new List<int>();
            int round = 0;
            bool addedAny = true;
            while (result.Count < validTracks.Count && addedAny)
            {
                addedAny = false;
                foreach (var env in envOrder)
                {
                    var g = groups[env];
                    if (round < g.Count)
                    {
                        result.Add(g[round]);
                        addedAny = true;
                    }
                }
                round++;
            }
            return result;
        }
    }
}
