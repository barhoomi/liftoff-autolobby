# plugin/libs/ — local-only game reference assemblies

This directory is the **default** target for `LiftoffAutoLobby.csproj`'s game-assembly
references when `$(LiftoffPath)` is not set (see that file's `LiftoffManagedPath` property
and `docs/features/doing/build-release-pipeline.md` section 2/2a for the full reasoning).

**Nothing in this directory is committed to git.** Liftoff's own assemblies are LuGus
Studios'/Unity's code, not this project's, so they don't belong in a public repo — see the
feature doc's licensing section for the decision (Option 1) and the operator runbook for how
CI restores them from a private source instead.

## Populating this directory for a local build

Either:

- Don't — just pass `-p:LiftoffPath=/path/to/your/Liftoff/install` to `dotnet build` (this is
  what `build.sh` and `infra/package_release.sh` do), **or**
- Copy these 7 files here from your own Liftoff install's `Liftoff_Data/Managed/` folder:
  - `Assembly-CSharp.dll`
  - `PhotonRealtime.dll`
  - `Photon3Unity3D.dll`
  - `com.rlabrecque.steamworks.net.dll`
  - `UnityEngine.dll`
  - `UnityEngine.CoreModule.dll`
  - `UnityEngine.UI.dll`
  - `UnityEngine.UIModule.dll`
  - `UnityEngine.InputLegacyModule.dll`
  - `UnityEngine.IMGUIModule.dll`
  - `UnityEngine.JSONSerializeModule.dll`

`BepInEx.dll` and `0Harmony.dll` do **not** go here — they come from NuGet
(`BepInEx.Core`, `HarmonyX`; see `LiftoffAutoLobby.csproj` and the repo-root
`NuGet.config`), since they're the modding framework, not game code.

Two decompiled reference `.cs` files used to live here too
(`Liftoff.Multiplayer.GameSetup.RoomSettingsPanel.decompiled.cs`,
`MultiplayerRaceScoreButtonPanel.decompiled.cs`) — also removed per the same licensing
decision. They were never part of the build (`<Compile Remove="libs/**" />` in the csproj
excludes this whole directory from compilation); regenerate them locally with `ilspycmd`
against your own install's DLLs if you need them for decompile work (see `AGENTS.md` rule 1).
