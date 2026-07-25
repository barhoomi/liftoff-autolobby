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
    // Chat theme (ChatTheme/activeTheme, LoadThemeConfig, Format* helpers), the
    //     tag-aware message splitter (ParseTag..SplitMessage), and chat send/dedupe
    //     (SendChatMessage(Raw), SendTaggedLines, QueueChatMessage, IsDuplicateMessage).
    public partial class AutoLobbyPlugin
    {


        // Configurable chat color scheme, loaded from chat_theme.json in the plugins dir
        // (see LoadThemeConfig / /reloadtheme). JsonUtility requires public fields on a
        // [System.Serializable] class; the defaults here double as the fallback values.
        [System.Serializable]
        public class ChatTheme
        {
            public string systemTagColor = "#FF0000";
            public string infoTagColor = "#0000FF";
            public string adminTagColor = "#0000FF";
            public string democracyTagColor = "#FF00FF";
            public string welcomeTagColor = "#00FF88";
            public string alertTagColor = "#FF0000";
            public string variableValueColor = "#00FF88";
            public string highlightTextColor = "#00FFFF";
            public string defaultTextColor = "#FFFFFF";
            // Dim/muted color for the multi-line continuation marker (↳). See FormatContinuation.
            public string mutedTextColor = "#888888";
        }

        private static ChatTheme activeTheme = new ChatTheme();

        private static void QueueChatMessage(string message)
        {
            if (string.IsNullOrEmpty(message)) return;
            pendingRoomChatMessages.Add(message);
        }

        // Loads the chat color scheme from chat_theme.json in the plugins dir, following the
        // same file-access pattern as the other config loaders. Writes the default theme to
        // disk if the file is missing. Parsing uses Unity's built-in JsonUtility (no third-party
        // JSON dependency). Each color is validated against ^#[0-9A-Fa-f]{6}$ with a per-field
        // fallback to the built-in default, so a single bad field can't leak a broken tag into
        // chat. Returns false only when the JSON itself is unparseable (defaults are applied and
        // the caller — /reloadtheme — reports the failure); true otherwise.
        private static bool LoadThemeConfig()
        {
            string path = Path.Combine(pluginPath, "chat_theme.json");
            try
            {
                if (!File.Exists(path))
                {
                    activeTheme = new ChatTheme();
                    try
                    {
                        File.WriteAllText(path, JsonUtility.ToJson(activeTheme, true));
                        UnityEngine.Debug.Log("[AutoLobbyPlugin] chat_theme.json not found — wrote default theme.");
                    }
                    catch (Exception ex)
                    {
                        UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Could not write default chat_theme.json: {ex.Message}");
                    }
                    return true;
                }

                string jsonText = File.ReadAllText(path);
                ChatTheme parsed = null;
                try
                {
                    parsed = JsonUtility.FromJson<ChatTheme>(jsonText);
                }
                catch (Exception ex)
                {
                    UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Failed to parse chat_theme.json: {ex.Message}. Applying defaults.");
                }

                if (parsed == null)
                {
                    activeTheme = new ChatTheme();
                    return false;
                }

                var defaults = new ChatTheme();
                parsed.systemTagColor = ValidateColor(parsed.systemTagColor, defaults.systemTagColor);
                parsed.infoTagColor = ValidateColor(parsed.infoTagColor, defaults.infoTagColor);
                parsed.adminTagColor = ValidateColor(parsed.adminTagColor, defaults.adminTagColor);
                parsed.democracyTagColor = ValidateColor(parsed.democracyTagColor, defaults.democracyTagColor);
                parsed.welcomeTagColor = ValidateColor(parsed.welcomeTagColor, defaults.welcomeTagColor);
                parsed.alertTagColor = ValidateColor(parsed.alertTagColor, defaults.alertTagColor);
                parsed.variableValueColor = ValidateColor(parsed.variableValueColor, defaults.variableValueColor);
                parsed.highlightTextColor = ValidateColor(parsed.highlightTextColor, defaults.highlightTextColor);
                parsed.defaultTextColor = ValidateColor(parsed.defaultTextColor, defaults.defaultTextColor);
                parsed.mutedTextColor = ValidateColor(parsed.mutedTextColor, defaults.mutedTextColor);

                activeTheme = parsed;
                UnityEngine.Debug.Log("[AutoLobbyPlugin] Loaded chat theme from chat_theme.json.");
                return true;
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error loading chat theme: {ex.Message}. Applying defaults.");
                activeTheme = new ChatTheme();
                return false;
            }
        }

        private static readonly Regex HexColorRegex = new Regex("^#[0-9A-Fa-f]{6}$");

        private static string ValidateColor(string candidate, string fallback)
        {
            if (!string.IsNullOrEmpty(candidate) && HexColorRegex.IsMatch(candidate))
                return candidate;
            return fallback;
        }

        // Chat-color formatting helpers. Every helper emits a fully balanced, properly nested
        // tag block (<b>/<i>/<color=…>) so the SplitMessage tag-tracking/re-opening logic stays
        // correct across chunk boundaries. Do not emit unbalanced tags here.
        //
        // client-chat-presentation.md tag policy: in client mode, IsAdmin(userId) ==
        // IsLocalPlayer(userId) (Plugin.Config.cs) -- there is no other admin -- so every
        // [ADMIN]-tagged message in client mode narrates the local host's own action back to
        // them. Announcing your own action to yourself with a bot-style authority tag is noise,
        // so it's dropped here, once, for every caller (mine and the ones in files outside this
        // feature's partition) automatically -- no per-command special-casing. Informational tags
        // (SYSTEM/INFO/HISTORY/PLAYERS/TRACKS/DEMOCRACY/PRO TIP/HELP) are untouched: they narrate
        // room state or multi-party outcomes, not a single authorized action, so they stay in
        // both roles. Dropping to "" (not omitting the call) means every existing call site needs
        // no edit -- SendChatMessage below trims the resulting leading space.
        private static string FormatTag(string text, string colorHex)
        {
            if (text == "ADMIN" && IsClientMode) return "";
            return $"<b><color={colorHex}>[{text}]</color></b>";
        }

        private static string FormatVariable(string text)
        {
            return $"<color={activeTheme.variableValueColor}><i>{text}</i></color>";
        }

        private static string FormatHighlight(string text)
        {
            return $"<b><color={activeTheme.highlightTextColor}>{text}</color></b>";
        }

        // Continuation marker for multi-line bot messages: the tag ([INFO]/[ADMIN]/…) appears
        // only on line 1; each subsequent line gets this dim ↳ marker instead of repeating the
        // tag. Emits a fully balanced tag block so SplitMessage's tag tracking stays correct.
        private static string FormatContinuation()
        {
            return $"<color={activeTheme.mutedTextColor}>  ↳</color> ";
        }

        private const int CHAT_MAX_CHARS = 220;

        private static string ParseTag(string s, int index, out int nextIndex)
        {
            nextIndex = index;
            if (index >= s.Length || s[index] != '<') return null;

            int end = s.IndexOf('>', index);
            if (end == -1) return null;

            nextIndex = end + 1;
            return s.Substring(index, end - index + 1);
        }

        private static void CloseLastTag(List<string> openTags, string closingTag)
        {
            string target = "";
            if (closingTag == "</b>") target = "<b>";
            else if (closingTag == "</i>") target = "<i>";
            else if (closingTag == "</color>") target = "<color";

            if (string.IsNullOrEmpty(target)) return;

            for (int i = openTags.Count - 1; i >= 0; i--)
            {
                if (target == "<color" ? openTags[i].StartsWith("<color", StringComparison.OrdinalIgnoreCase) : openTags[i].Equals(target, StringComparison.OrdinalIgnoreCase))
                {
                    openTags.RemoveAt(i);
                    break;
                }
            }
        }

        private static int GetClosingTagsLength(List<string> openTags)
        {
            if (openTags == null) return 0;
            int len = 0;
            foreach (var tag in openTags)
            {
                if (tag.StartsWith("<color", StringComparison.OrdinalIgnoreCase)) len += 8; // </color>
                else if (tag.Equals("<b>", StringComparison.OrdinalIgnoreCase)) len += 4; // </b>
                else if (tag.Equals("<i>", StringComparison.OrdinalIgnoreCase)) len += 4; // </i>
            }
            return len;
        }

        private static string GetClosingTagsString(List<string> openTags)
        {
            if (openTags == null) return "";
            StringBuilder sb = new StringBuilder();
            for (int i = openTags.Count - 1; i >= 0; i--)
            {
                string tag = openTags[i];
                if (tag.StartsWith("<color", StringComparison.OrdinalIgnoreCase)) sb.Append("</color>");
                else if (tag.Equals("<b>", StringComparison.OrdinalIgnoreCase)) sb.Append("</b>");
                else if (tag.Equals("<i>", StringComparison.OrdinalIgnoreCase)) sb.Append("</i>");
            }
            return sb.ToString();
        }

        private static string GetOpeningTagsString(List<string> openTags)
        {
            if (openTags == null) return "";
            StringBuilder sb = new StringBuilder();
            foreach (var tag in openTags)
            {
                sb.Append(tag);
            }
            return sb.ToString();
        }

        private static List<string> SplitMessage(string message, int maxChars)
        {
            List<string> result = new List<string>();
            string currentString = message;

            while (currentString.Length > maxChars)
            {
                int n = currentString.Length;
                List<string>[] tagsAt = new List<string>[n];
                bool[] inTag = new bool[n];

                List<string> activeTags = new List<string>();
                int idx = 0;
                while (idx < n)
                {
                    if (currentString[idx] == '<')
                    {
                        int nextIdx;
                        string tag = ParseTag(currentString, idx, out nextIdx);
                        if (tag != null)
                        {
                            bool isClosing = tag.StartsWith("</");
                            for (int j = idx; j < nextIdx; j++)
                            {
                                inTag[j] = true;
                                tagsAt[j] = new List<string>(activeTags);
                            }
                            if (isClosing)
                            {
                                CloseLastTag(activeTags, tag.ToLower());
                            }
                            else
                            {
                                activeTags.Add(tag);
                            }
                            idx = nextIdx;
                            continue;
                        }
                    }
                    tagsAt[idx] = new List<string>(activeTags);
                    inTag[idx] = false;
                    idx++;
                }

                int bestSplitIdx = -1;
                int searchEnd = maxChars;

                // 1. Search for " | " separator
                int pipesIndex = -1;
                for (int i = searchEnd - 3; i >= 0; i--)
                {
                    if (i + 3 <= n && currentString.Substring(i, 3) == " | " && !inTag[i])
                    {
                        int candidateSplit = i + 3;
                        List<string> openTags = (candidateSplit >= 0 && candidateSplit < tagsAt.Length) ? tagsAt[candidateSplit] : new List<string>();
                        int closingLen = GetClosingTagsLength(openTags);
                        if (candidateSplit + closingLen <= maxChars)
                        {
                            pipesIndex = candidateSplit;
                            break;
                        }
                    }
                }

                if (pipesIndex != -1)
                {
                    bestSplitIdx = pipesIndex;
                }
                else
                {
                    // 2. Search for space character
                    int spaceIndex = -1;
                    for (int i = searchEnd - 1; i >= 0; i--)
                    {
                        if (currentString[i] == ' ' && !inTag[i])
                        {
                            int candidateSplit = i + 1;
                            List<string> openTags = (candidateSplit >= 0 && candidateSplit < tagsAt.Length) ? tagsAt[candidateSplit] : new List<string>();
                            int closingLen = GetClosingTagsLength(openTags);
                            if (candidateSplit + closingLen <= maxChars)
                            {
                                spaceIndex = candidateSplit;
                                break;
                            }
                        }
                    }

                    if (spaceIndex != -1)
                    {
                        bestSplitIdx = spaceIndex;
                    }
                    else
                    {
                        // 3. Absolute fallback: split at character boundary
                        for (int i = searchEnd; i >= 1; i--)
                        {
                            if (!inTag[i - 1])
                            {
                                List<string> openTags = (i >= 0 && i < tagsAt.Length) ? tagsAt[i] : new List<string>();
                                int closingLen = GetClosingTagsLength(openTags);
                                if (i + closingLen <= maxChars)
                                {
                                    bestSplitIdx = i;
                                    break;
                                }
                            }
                        }
                    }
                }

                if (bestSplitIdx <= 0)
                {
                    bestSplitIdx = maxChars;
                }

                if (bestSplitIdx >= n)
                {
                    break;
                }

                string chunk = currentString.Substring(0, bestSplitIdx);
                List<string> openTagsAtSplit = (bestSplitIdx >= 0 && bestSplitIdx < tagsAt.Length) ? tagsAt[bestSplitIdx] : new List<string>();
                string closingTags = GetClosingTagsString(openTagsAtSplit);
                chunk += closingTags;
                result.Add(chunk);

                string openingTags = GetOpeningTagsString(openTagsAtSplit);
                currentString = openingTags + currentString.Substring(bestSplitIdx);
            }

            if (!string.IsNullOrEmpty(currentString))
            {
                result.Add(currentString);
            }
            return result;
        }

        // Sends a logically-single bot message that spans multiple chat lines: the tag block
        // ([INFO]/[ADMIN]/…) appears only on the first line; every later line is prefixed with the
        // dim ↳ continuation marker. Each line is routed through SendChatMessage individually so
        // per-line SplitMessage safety is preserved. Null/empty lines are skipped gracefully.
        private static void SendTaggedLines(string tagText, string tagColor, params string[] lines)
        {
            if (lines == null) return;
            bool firstEmitted = false;
            foreach (string line in lines)
            {
                if (string.IsNullOrEmpty(line)) continue;
                if (!firstEmitted)
                {
                    SendChatMessage($"{FormatTag(tagText, tagColor)} {line}");
                    firstEmitted = true;
                }
                else
                {
                    SendChatMessage($"{FormatContinuation()}{line}");
                }
            }
        }

        private static void SendChatMessage(string message)
        {
            if (string.IsNullOrEmpty(message)) return;
            lastActivityTime = DateTime.UtcNow;

            if (message.Length <= CHAT_MAX_CHARS)
            {
                SendChatMessageRaw(message);
                return;
            }

            try
            {
                List<string> chunks = SplitMessage(message, CHAT_MAX_CHARS);
                foreach (string chunk in chunks)
                {
                    SendChatMessageRaw(chunk);
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in SendChatMessage splitting: {ex}");
                // Fallback to sending raw if splitting fails for some reason
                SendChatMessageRaw(message);
            }
        }

        private static void SendChatMessageRaw(string message)
        {
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] SendChatMessage called: '{message}'");
            LogEvent("chat_response", ("message", message));
            try
            {
                Type chatType = FindType("Liftoff.Multiplayer.Chat.ChatWindowPanel");
                if (chatType != null)
                {
                    var chats = Resources.FindObjectsOfTypeAll(chatType);
                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Found {chats.Length} ChatWindowPanel objects.");
                    // Prefer an active panel — during a room recreate, a stale inactive instance
                    // from the old scene can briefly coexist with the new one.
                    object chatObj = chats.FirstOrDefault(c => c != null && ((MonoBehaviour)c).gameObject.activeInHierarchy) ?? chats.FirstOrDefault(c => c != null);
                    if (chatObj != null)
                    {
                        MonoBehaviour chatPanel = (MonoBehaviour)chatObj;
                        UnityEngine.Debug.Log($"[AutoLobbyPlugin] chatPanel activeInHierarchy: {chatPanel.gameObject.activeInHierarchy}");
                        var inputFieldField = chatType.GetField("fieldUserMessage", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                        if (inputFieldField != null)
                        {
                            UnityEngine.UI.InputField inputField = (UnityEngine.UI.InputField)inputFieldField.GetValue(chatPanel);
                            if (inputField != null)
                            {
                                inputField.text = message;
                                var sendMethod = chatType.GetMethod("SendUserMessage", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                                if (sendMethod != null)
                                {
                                    UnityEngine.Debug.Log($"[AutoLobbyPlugin] Invoking SendUserMessage on ChatWindowPanel.");
                                    sendMethod.Invoke(chatPanel, null);
                                }
                                else
                                {
                                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] SendUserMessage method not found.");
                                }
                            }
                            else
                            {
                                UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] fieldUserMessage is null.");
                            }
                        }
                        else
                        {
                            UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] fieldUserMessage field not found.");
                        }
                    }
                }
                else
                {
                    UnityEngine.Debug.LogWarning("[AutoLobbyPlugin] ChatWindowPanel type not found.");
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[AutoLobbyPlugin] Failed to send chat message: {ex.Message}");
            }
        }

        private static bool IsDuplicateMessage(string userName, string message)
        {
            try
            {
                // Clean up messages older than 5 seconds
                processedMessages.RemoveAll(m => (DateTime.Now - m.Item3).TotalSeconds > 5.0);

                // Check if this combination of userName + message was processed recently
                foreach (var pm in processedMessages)
                {
                    if (pm.Item1 == userName && pm.Item2 == message)
                    {
                        return true;
                    }
                }

                processedMessages.Add(new Tuple<string, string, DateTime>(userName, message, DateTime.Now));
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error in IsDuplicateMessage: {ex}");
            }
            return false;
        }
    }
}
