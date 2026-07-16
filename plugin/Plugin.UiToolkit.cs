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
    // Generic Unity UI-finding/introspection helpers with no game-state knowledge:
    //     button/input lookup by name or text, popup text search, popup dismissal,
    //     button-listener/scene-object dumps.
    public partial class AutoLobbyPlugin
    {


        private static string GetButtonText(Button btn)
        {
            if (btn == null) return "";

            // 1. Try legacy Text component
            Text t = btn.GetComponentInChildren<Text>();
            if (t != null) return t.text;

            // 2. Try TextMeshPro components via reflection to avoid static references
            foreach (var comp in btn.GetComponentsInChildren<Component>(true))
            {
                if (comp == null) continue;
                string typeName = comp.GetType().Name;
                if (typeName.Equals("TextMeshProUGUI", StringComparison.OrdinalIgnoreCase) ||
                    typeName.Equals("TextMeshPro", StringComparison.OrdinalIgnoreCase) ||
                    typeName.Equals("TMP_Text", StringComparison.OrdinalIgnoreCase))
                {
                    var prop = comp.GetType().GetProperty("text", BindingFlags.Public | BindingFlags.Instance);
                    if (prop != null)
                    {
                        object val = prop.GetValue(comp);
                        if (val != null) return val.ToString();
                    }
                }
            }

            return "";
        }

        private static Button FindButtonByTextOrName(string targetText, string[] targetNames)
        {
            Button[] buttons = Resources.FindObjectsOfTypeAll<Button>();
            
            // Pass 1: Active + Name Match
            if (targetNames != null)
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || !btn.gameObject.activeInHierarchy) continue;
                    foreach (string name in targetNames)
                    {
                        if (btn.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        {
                            return btn;
                        }
                    }
                }
            }

            // Pass 2: Active + Text Match
            if (!string.IsNullOrEmpty(targetText))
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || !btn.gameObject.activeInHierarchy) continue;
                    string txt = GetButtonText(btn);
                    if (!string.IsNullOrEmpty(txt) && txt.Equals(targetText, StringComparison.OrdinalIgnoreCase))
                    {
                        return btn;
                    }
                }
            }

            // Pass 3: Inactive + Name Match (fallback)
            if (targetNames != null)
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || btn.gameObject.activeInHierarchy) continue;
                    foreach (string name in targetNames)
                    {
                        if (btn.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        {
                            return btn;
                        }
                    }
                }
            }

            // Pass 4: Inactive + Text Match (fallback)
            if (!string.IsNullOrEmpty(targetText))
            {
                foreach (Button btn in buttons)
                {
                    if (btn == null || btn.gameObject.activeInHierarchy) continue;
                    string txt = GetButtonText(btn);
                    if (!string.IsNullOrEmpty(txt) && txt.Equals(targetText, StringComparison.OrdinalIgnoreCase))
                    {
                        return btn;
                    }
                }
            }

            return null;
        }

        private static bool PopupContainsText(GameObject popupCanvas, string needle)
        {
            foreach (var comp in popupCanvas.GetComponentsInChildren<Component>(true))
            {
                if (comp == null) continue;
                string text = null;
                Text legacyText = comp as Text;
                if (legacyText != null)
                {
                    text = legacyText.text;
                }
                else
                {
                    string typeName = comp.GetType().Name;
                    if (typeName.Equals("TextMeshProUGUI", StringComparison.OrdinalIgnoreCase) ||
                        typeName.Equals("TMP_Text", StringComparison.OrdinalIgnoreCase))
                    {
                        var prop = comp.GetType().GetProperty("text", BindingFlags.Public | BindingFlags.Instance);
                        if (prop != null) text = prop.GetValue(comp)?.ToString();
                    }
                }
                if (!string.IsNullOrEmpty(text) && text.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0)
                    return true;
            }
            return false;
        }

        private static void DismissPopups()
        {
            GameObject popupCanvas = GameObject.Find("PopupCanvas(Clone)");
            if (popupCanvas != null && popupCanvas.activeInHierarchy)
            {
                bool isTrackUnavailable = PopupContainsText(popupCanvas, "not shared") ||
                                          PopupContainsText(popupCanvas, "not available");

                Button[] buttons = popupCanvas.GetComponentsInChildren<Button>(true);
                foreach (Button btn in buttons)
                {
                    if (btn != null && btn.gameObject.activeInHierarchy && btn.interactable)
                    {
                        string txt = GetButtonText(btn);
                        if (!string.IsNullOrEmpty(txt) &&
                            (txt.Equals("Confirm", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("OK", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("Close", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("Got it", StringComparison.OrdinalIgnoreCase) ||
                             txt.Equals("Dismiss", StringComparison.OrdinalIgnoreCase)))
                        {
                            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Auto-dismissing popup (trackUnavailable={isTrackUnavailable}). Button: {txt}");
                            btn.onClick.Invoke();

                            if (isTrackUnavailable)
                            {
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Track not shareable, skipping: {targetTrackName}");
                                targetTrackName = GetNextTrackFromRotation(out targetEnvironment, out targetGameMode);
                                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Advanced to next track: {targetTrackName} ({targetEnvironment})");
                                isSubmittingSettings = false;
                            }
                            else if (isSubmittingSettings)
                            {
                                // Some other alert (e.g. "room already exists") appeared after a
                                // submit — don't leave the bot stuck waiting for a popup that will
                                // never close on its own.
                                UnityEngine.Debug.Log("[AutoLobbyPlugin] Dismissed an alert while a settings submission was in flight — resetting isSubmittingSettings so the bot can retry.");
                                isSubmittingSettings = false;
                            }
                            break;
                        }
                    }
                }
            }
        }

        private static void DumpActiveSceneObjects()
        {
            if (sceneObjectsDumped) return;
            sceneObjectsDumped = true;
            UnityEngine.Debug.Log("[AutoLobbyPlugin] === DUMPING ACTIVE SCENE OBJECTS IN MultiplayerMenu ===");
            try
            {
                foreach (GameObject obj in Resources.FindObjectsOfTypeAll<GameObject>())
                {
                    if (obj != null && obj.activeInHierarchy)
                    {
                        // Print path to object
                        string path = obj.name;
                        Transform p = obj.transform.parent;
                        while (p != null)
                        {
                            path = p.name + "/" + path;
                            p = p.parent;
                        }
                        // If it has a Button component, print it
                        Button b = obj.GetComponent<Button>();
                        string buttonInfo = b != null ? $" [BUTTON: '{GetButtonText(b)}', interactable={b.interactable}]" : "";
                        UnityEngine.Debug.Log($"  - {path}{buttonInfo}");
                    }
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[AutoLobbyPlugin] Error dumping scene objects: {ex.Message}");
            }
            UnityEngine.Debug.Log("[AutoLobbyPlugin] =====================================================");
        }

        private static InputField FindInputFieldByName(string[] targetNames, string placeholderSubstring = null)
        {
            InputField[] fields = Resources.FindObjectsOfTypeAll<InputField>();
            foreach (InputField f in fields)
            {
                if (f == null || !f.gameObject.activeInHierarchy) continue;
                foreach (string name in targetNames)
                {
                    if (f.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        return f;
                }
            }
            // Fallback: allow inactive match too, in case the panel hasn't been SetActive(true) yet
            foreach (InputField f in fields)
            {
                if (f == null) continue;
                foreach (string name in targetNames)
                {
                    if (f.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        return f;
                }
            }
            // Fallback: match by placeholder text, in case the field's GameObject name changes
            // between game versions (name-based lookup already proved brittle once here).
            if (!string.IsNullOrEmpty(placeholderSubstring))
            {
                foreach (InputField f in fields)
                {
                    if (f == null || f.placeholder == null) continue;
                    Text pt = f.placeholder as Text;
                    if (pt != null && pt.text != null &&
                        pt.text.IndexOf(placeholderSubstring, StringComparison.OrdinalIgnoreCase) >= 0)
                        return f;
                }
            }
            return null;
        }

        // Scoped lookup: searches only within root's hierarchy, to avoid matching one of many
        // identically-named per-row buttons in a room list (e.g. "buttonJoin" appears once per
        // visible public room row, so a global scene-wide search is ambiguous).
        private static Button FindButtonInHierarchy(Transform root, string[] targetNames)
        {
            if (root == null) return null;
            foreach (Button btn in root.GetComponentsInChildren<Button>(true))
            {
                if (btn == null) continue;
                foreach (string name in targetNames)
                {
                    if (btn.name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        return btn;
                }
            }
            return null;
        }

        private static void DumpButtonListeners(string name, Button btn)
        {
            if (btn == null)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin] Button '{name}' is null.");
                return;
            }
            UnityEngine.Debug.Log($"[AutoLobbyPlugin] Button '{name}': active={btn.gameObject.activeSelf}, activeInHierarchy={btn.gameObject.activeInHierarchy}, interactable={btn.interactable}");
            var onClick = btn.onClick;
            if (onClick == null)
            {
                UnityEngine.Debug.Log($"[AutoLobbyPlugin]   onClick is null.");
                return;
            }
            int persistentCount = onClick.GetPersistentEventCount();
            UnityEngine.Debug.Log($"[AutoLobbyPlugin]   onClick has {persistentCount} persistent listeners:");
            for (int i = 0; i < persistentCount; i++)
            {
                var target = onClick.GetPersistentTarget(i);
                var methodName = onClick.GetPersistentMethodName(i);
                UnityEngine.Debug.Log($"[AutoLobbyPlugin]     Persistent {i}: Target={target?.GetType().FullName}, Method={methodName}");
            }
        }
    }
}
