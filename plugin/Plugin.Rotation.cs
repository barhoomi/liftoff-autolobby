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
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] tracksPath does not exist.");
                    return "";
                }

                string[] lines = File.ReadAllLines(tracksPath);
                var validTracks = new List<string>();
                foreach (var line in lines)
                {
                    if (!string.IsNullOrWhiteSpace(line) && !line.Trim().StartsWith("#"))
                    {
                        validTracks.Add(line.Trim());
                    }
                }

                if (validTracks.Count == 0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] No valid tracks parsed.");
                    return "";
                }

                // Sync shuffleMode from disk (kept for logging parity; selection itself is
                // mode-agnostic now that shuffling rewrites tracks_to_rotate.txt in place)
                shuffleMode = GetShuffleMode();

                int index = 0;
                if (File.Exists(statePath))
                {
                    int.TryParse(File.ReadAllText(statePath).Trim(), out index);
                }
                if (index < 0 || index >= validTracks.Count)
                {
                    index = 0;
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Current state index: {index}, validTracks count: {validTracks.Count}");

                string overrideMode = GetOverrideGameMode();
                string trackName = "";

                // Walk forward from the current position (read-only, no state is persisted by
                // Peek) skipping any session-blacklisted (env, track, mode) combos, so the "up
                // next" chat announcement never names a track that will be skipped instantly.
                for (int attempt = 0; attempt < validTracks.Count; attempt++)
                {
                    int candidateIndex = (index + attempt) % validTracks.Count;

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
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] tracks_to_rotate.txt not found. Using default values.");
                    return "";
                }

                string[] lines = File.ReadAllLines(tracksPath);
                var validTracks = new List<string>();
                foreach (var line in lines)
                {
                    if (!string.IsNullOrWhiteSpace(line) && !line.Trim().StartsWith("#"))
                    {
                        validTracks.Add(line.Trim());
                    }
                }

                if (validTracks.Count == 0)
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] tracks_to_rotate.txt is empty.");
                    return "";
                }

                // Sync shuffleMode from disk
                shuffleMode = GetShuffleMode();

                int index = 0;
                if (File.Exists(statePath))
                    int.TryParse(File.ReadAllText(statePath).Trim(), out index);

                if (index < 0 || index >= validTracks.Count)
                    index = 0;

                int nextIndex = (index + 1) % validTracks.Count;

                // tracks_to_rotate.txt's line order IS the rotation order in both modes.
                // In shuffle mode, once a full pass completes, deal a fresh random order
                // for the next cycle before advancing the cursor back to 0.
                if (shuffleMode && nextIndex == 0)
                {
                    ShuffleTracksFile(tracksPath);
                }

                File.WriteAllText(statePath, nextIndex.ToString());

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

        // Re-shuffles tracks_to_rotate.txt's own line order in place (a real Fisher-Yates
        // deal, not a derived index list) and preserves any leading '#' header/comment
        // lines as-is at the top. This is the only place a fresh shuffle gets dealt from
        // the C# side; the file's order is the single source of truth for both rotation
        // modes, so there is no separate shuffled/unshuffled state to keep in sync.
        private static void ShuffleTracksFile(string tracksPath)
        {
            try
            {
                string[] lines = File.ReadAllLines(tracksPath);
                var header = new List<string>();
                var trackLines = new List<string>();
                foreach (var line in lines)
                {
                    if (string.IsNullOrWhiteSpace(line)) continue;
                    if (line.Trim().StartsWith("#")) header.Add(line);
                    else trackLines.Add(line);
                }

                trackLines = RoundRobinShuffleByEnvironment(trackLines);

                var sb = new System.Text.StringBuilder();
                foreach (var h in header) sb.AppendLine(h);
                foreach (var t in trackLines) sb.AppendLine(t);
                File.WriteAllText(tracksPath, sb.ToString());
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error shuffling tracks file: {ex.Message}");
            }
        }

        // Shuffles each environment's lines among themselves, shuffles the order environments
        // are visited in, then interleaves round-robin across environments. A flat shuffle can
        // (and does, by chance) land two tracks from the same environment back-to-back — the
        // environment is the dominant visual cue players notice, so that reads as "stale" even
        // though no individual track repeated. Round-robin guarantees same-environment picks
        // are spread apart (until an environment runs out of tracks) while staying random.
        private static List<string> RoundRobinShuffleByEnvironment(List<string> trackLines)
        {
            var groups = new Dictionary<string, List<string>>();
            var envOrder = new List<string>();
            foreach (var line in trackLines)
            {
                string[] parts = line.Split(',');
                string env = parts.Length > 1 ? parts[1].Trim() : "";
                if (!groups.ContainsKey(env))
                {
                    groups[env] = new List<string>();
                    envOrder.Add(env);
                }
                groups[env].Add(line);
            }

            foreach (var env in envOrder)
            {
                var g = groups[env];
                for (int i = g.Count - 1; i > 0; i--)
                {
                    int j = rng.Next(0, i + 1);
                    string temp = g[i]; g[i] = g[j]; g[j] = temp;
                }
            }

            for (int i = envOrder.Count - 1; i > 0; i--)
            {
                int j = rng.Next(0, i + 1);
                string temp = envOrder[i]; envOrder[i] = envOrder[j]; envOrder[j] = temp;
            }

            var result = new List<string>();
            int round = 0;
            bool addedAny = true;
            while (result.Count < trackLines.Count && addedAny)
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
