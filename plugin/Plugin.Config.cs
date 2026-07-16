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
    // File-protocol config readers: admin ids, Liftoff Pro toggle/nickname/credentials,
    //     client script, rotation/keep-alive intervals, auto-start, shuffle mode, override
    //     game mode, playlist existence/listing. Mirrors the plugin<->orchestrator text-file
    //     protocol described in AGENTS.md.
    public partial class AutoLobbyPlugin
    {


        private static void LoadAdminIds()
        {
            adminIds.Clear();
            string adminPath = Path.Combine(pluginPath, "admin_ids.txt");
            if (File.Exists(adminPath))
            {
                foreach (var line in File.ReadAllLines(adminPath))
                {
                    string id = line.Trim();
                    if (!string.IsNullOrEmpty(id) && !id.StartsWith("#"))
                        adminIds.Add(id);
                }
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded {adminIds.Count} admin ID(s).");
            }
            else
            {
                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] admin_ids.txt not found — no admins configured.");
            }
        }

        private static bool IsAdmin(string userId) => adminIds.Contains(userId);

        private static void LoadUseLiftoffPro()
        {
            useLiftoffPro = true;
            string path = Path.Combine(pluginPath, "use_liftoff_pro.txt");
            if (File.Exists(path))
            {
                string content = File.ReadAllText(path).Trim();
                if (content.Equals("false", StringComparison.OrdinalIgnoreCase))
                {
                    useLiftoffPro = false;
                }
            }
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] useLiftoffPro = {useLiftoffPro}");
        }

        private static void LoadBotNickname()
        {
            botNickname = "";
            string path = Path.Combine(pluginPath, "bot_nickname.txt");
            if (File.Exists(path))
            {
                botNickname = File.ReadAllText(path).Trim();
            }
            if (!string.IsNullOrEmpty(botNickname))
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded bot nickname override: '{botNickname}'");
            }
        }

        private static void LoadLiftoffProCredentials()
        {
            liftoffProUsername = "";
            liftoffProPassword = "";
            string userPath = Path.Combine(pluginPath, "liftoff_pro_username.txt");
            string passPath = Path.Combine(pluginPath, "liftoff_pro_password.txt");
            if (File.Exists(userPath)) liftoffProUsername = File.ReadAllText(userPath).Trim();
            if (File.Exists(passPath)) liftoffProPassword = File.ReadAllText(passPath).Trim();
            if (!string.IsNullOrEmpty(liftoffProUsername) && !string.IsNullOrEmpty(liftoffProPassword))
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded distinct Liftoff Pro credentials for user '{liftoffProUsername}'.");
            }
        }

        // client_script.txt format: one step per line, "<delaySeconds> <message>" — delay is
        // seconds since entering the room (not since the previous step). Blank lines and lines
        // starting with '#' are skipped. Read once at Awake(), same as every other config file.
        private static void LoadClientScript()
        {
            clientScriptSteps.Clear();
            clientScriptNextIndex = 0;
            string path = Path.Combine(pluginPath, "client_script.txt");
            if (!File.Exists(path)) return;
            foreach (var line in File.ReadAllLines(path))
            {
                string trimmed = line.Trim();
                if (string.IsNullOrEmpty(trimmed) || trimmed.StartsWith("#")) continue;
                int spaceIdx = trimmed.IndexOf(' ');
                if (spaceIdx < 0) continue;
                double delay;
                if (double.TryParse(trimmed.Substring(0, spaceIdx), out delay))
                {
                    clientScriptSteps.Add(Tuple.Create(delay, trimmed.Substring(spaceIdx + 1)));
                }
            }
            if (clientScriptSteps.Count > 0)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Loaded client script with {clientScriptSteps.Count} step(s).");
            }
        }

        private static double GetRotationInterval()
        {
            try
            {
                string intervalPath = Path.Combine(pluginPath, "rotation_interval.txt");
                if (File.Exists(intervalPath))
                {
                    double val;
                    if (double.TryParse(File.ReadAllText(intervalPath).Trim(), out val))
                    {
                        return val;
                    }
                }
            }
            catch {}
            return 600.0; // Default: 10 mins
        }

        private static double GetKeepAliveInterval()
        {
            try
            {
                string path = Path.Combine(pluginPath, "keep_alive_seconds.txt");
                if (File.Exists(path))
                {
                    double val;
                    if (double.TryParse(File.ReadAllText(path).Trim(), out val))
                    {
                        return val;
                    }
                }
            }
            catch {}
            return 240.0; // Default: 4 minutes (resets timer right before 5m kick)
        }

        private static bool GetAutoStart()
        {
            try
            {
                string autoStartPath = Path.Combine(pluginPath, "auto_start.txt");
                if (File.Exists(autoStartPath))
                {
                    string content = File.ReadAllText(autoStartPath).Trim();
                    return content.Equals("true", StringComparison.OrdinalIgnoreCase);
                }
            }
            catch {}
            return false; // Default: stay in lobby
        }

        private static bool GetShuffleMode()
        {
            try
            {
                string shuffleModePath = Path.Combine(pluginPath, "shuffle_mode.txt");
                if (File.Exists(shuffleModePath))
                {
                    string content = File.ReadAllText(shuffleModePath).Trim();
                    return content.Equals("true", StringComparison.OrdinalIgnoreCase);
                }
            }
            catch {}
            return false; // Default: false
        }

        // democracy-skip.md: whether /skip is a public majority vote (true) or admin-only
        // (false). Mirrors GetShuffleMode()'s file-read pattern exactly.
        private static bool GetDemocracyMode()
        {
            try
            {
                string democracyModePath = Path.Combine(pluginPath, "democracy_mode.txt");
                if (File.Exists(democracyModePath))
                {
                    string content = File.ReadAllText(democracyModePath).Trim();
                    return content.Equals("true", StringComparison.OrdinalIgnoreCase);
                }
            }
            catch {}
            return false; // Default: democracy off, /skip stays admin-only
        }

        private static string GetOverrideGameMode()
        {
            try
            {
                string path = Path.Combine(pluginPath, "override_game_mode.txt");
                if (File.Exists(path))
                {
                    string mode = File.ReadAllText(path).Trim();
                    if (!string.IsNullOrEmpty(mode))
                    {
                        return mode;
                    }
                }
            }
            catch {}
            return null;
        }

        private static bool PlaylistExists(string name)
        {
            try
            {
                string path = Path.Combine(pluginPath, "available_playlists.txt");
                if (File.Exists(path))
                {
                    foreach (var line in File.ReadAllLines(path))
                    {
                        if (line.Trim().Equals(name.Trim(), StringComparison.OrdinalIgnoreCase))
                        {
                            return true;
                        }
                    }
                }
            }
            catch {}
            return false;
        }

        private static string GetAvailablePlaylistsString()
        {
            try
            {
                string path = Path.Combine(pluginPath, "available_playlists.txt");
                if (File.Exists(path))
                {
                    var list = new List<string>();
                    foreach (var line in File.ReadAllLines(path))
                    {
                        if (!string.IsNullOrWhiteSpace(line))
                        {
                            list.Add(line.Trim());
                        }
                    }
                    return string.Join(", ", list.ToArray());
                }
            }
            catch {}
            return "";
        }
    }
}
