# Clip Manager

An OBS Studio script that files every replay-buffer clip into a per-game folder, names it after the game that was captured, and shows a popup and plays a sound when it does — plus hotkey-driven game/desktop capture switching and mic controls, so a normal clipping session never needs a scene switch or a manual folder.

---

## Features

- Files each clip into a **per-game folder** named from the captured game's executable, with **alias overrides** for custom names
- Runs off OBS's own **Save Replay** hotkey — the naming and filing happen on OBS's save event, not a hotkey of its own
- **Automatic game clipping** — detects a fullscreen app on the primary monitor, shows the Game Capture source, starts the replay buffer once OBS confirms the hook, and stops a grace period after the game exits
- Multi-game aware — the buffer stays up while any detected game is open, and a crash-relaunch inside the grace window never drops it
- **Exceptions list** (pre-filled with common browsers, media players and the Windows shell) keeps non-game fullscreen apps from triggering it
- **Display override** — a manual hotkey mode that shows the Display Capture source and buffer regardless of game detection, auto-off after a set time, with its own clip subfolder
- Optional **fall back to desktop capture** when OBS cannot hook a detected game
- Custom-rendered **toast popups** (per-pixel alpha, gradient, drop shadow) and a **sound per event**, both fully configurable and individually overridable
- **Mic mute** and **mic monitoring** toggles with their own hotkeys and popups; mute stays in sync with OBS's native controls
- **Folder-size warning** when the clips directory grows past a configurable limit
- **Auto-restart** of the replay buffer (periodic and/or after every save) to work around long-session OBS buffer bugs
- Windows-native — nothing to install beyond OBS's Python and Pillow

---

## How it works

### Game clipping

Every few seconds the script checks the foreground window. If it is a fullscreen app on the primary monitor and not in the exceptions list, the script shows the Game Capture source and, about **3 s** later, confirms OBS actually hooked it:

- **Hooked** — starts the replay buffer, shows _Game clipping on_.
- **Not hooked** — shows _No game to capture_ and, if _Fall back to desktop on fail_ is enabled, switches to Display override.

The link is a set of processes: the buffer runs while at least one detected game is open and stops a grace period (default **30 s**) after the last one exits. The _Game clipping off_ hotkey force-stops the foreground game immediately and won't re-detect it until it restarts. Manual _Game clipping on_ runs the same detect-and-hook sequence once, right away.

### Display override

A manual hotkey mode: it hides the game source, shows the Display Capture source, and starts the buffer. It outranks game detection (detection is ignored while it is on) and auto-offs after a configurable time (default **60 min**, reset on every clip saved while it is active). Clips saved in this mode go to a fixed subfolder (default `Desktop`) instead of a per-game folder.

### Clip naming

```text
<Clips folder>/<Game>/<filename template>.<ext>
```

`<Game>` is the linked game's executable name, or an alias you have set for that path. With no game linked, it falls back to the window process most seen across the buffer's length (not whatever is focused at save time). Display-override clips use the desktop subfolder name instead.

---

## Notifications

Toasts render at the top-right by default and stack downward. A capture change can show two at once — a **buffer** toast (_Replay on/off_) and a **source** toast (game/desktop). A repeat of a still-visible kind updates in place instead of stacking.

```text
                          ┌──────┬──────────────────────┐
                          │  ⏺   │  Replay on           │
                          └──────┴──────────────────────┘
                          ┌──────┬──────────────────────┐
                          │  ▣   │  Game clipping on    │
                          └──────┴──────────────────────┘
```

| Toast                          | Shown when                                                                    |
| ------------------------------ | ---------------------------------------------------------------------------- |
| Replay saved / Saving failed   | A clip was filed, or the move failed                                          |
| Replay on / Replay off         | The script started or stopped the replay buffer                              |
| Game clipping on / off         | A game was hooked or released                                                 |
| No game to capture             | A detected app could not be hooked, or the manual hotkey found no fullscreen game |
| Desktop capture on / off       | Display override engaged or released                                          |
| Microphone on / off            | Mic source unmuted or muted                                                   |
| Listening on / off             | Mic monitoring toggled                                                        |
| Warning / Info                 | A misconfiguration, or the folder-size limit was crossed                      |

Each toast has its own WAV in `sounds/` (override per event under **Sounds**), plus a global dB offset. The renderer is per-monitor DPI-aware, so popups stay sharp and correctly sized on scaled displays.

---

## Requirements

- **Windows** — the script uses Win32 APIs and `winsound` throughout
- **OBS Studio** with Python scripting enabled and pointed at **Python 3.10** (_Tools > Scripts > Python Settings_). 3.12+ fails with _"Could not load library"_
- **Pillow** in that same Python, for the popup renderer:
  ```text
  "<Python Settings path>\pythonw.exe" -m pip install pillow
  ```
- A configured **replay buffer** in OBS
- A **Game Capture** source set to _"Capture any fullscreen application"_ and a **Display Capture** source, both in your active scene

---

## Installation

1. Put `clip_manager.py` somewhere permanent, with its `icons/` and `sounds/` folders alongside it.
2. In OBS, open **Tools > Scripts > Python Settings** and point it at a Python 3.10 install.
3. Install Pillow into that Python (see [Requirements](#requirements)).
4. On the **Scripts** tab, click **+** and select `clip_manager.py`.
5. Fill in the three source names at the top of the script settings.
6. Open **Settings > Hotkeys**, search _"Clip Manager"_, and bind the actions you want — and make sure **Save Replay Buffer** is bound, since that is what actually saves a clip.

---

## Hotkeys

Bound in **Settings > Hotkeys** (search _"Clip Manager"_).

| Action                  | What it does                                                                       |
| ----------------------- | --------------------------------------------------------------------------------- |
| Game clipping on        | Detect and hook the current fullscreen app now                                    |
| Game clipping off       | Force-stop the foreground game (or all linked games); no re-detect until it restarts |
| Game clipping toggle    | On or off, depending on current state                                             |
| Display override on     | Switch to Display Capture and buffer, ignoring game detection                      |
| Display override off    | Leave Display override; hands back to game clipping if a game is still linked      |
| Display override toggle | On or off                                                                          |
| Toggle mic mute         | Mute or unmute the mic source (stays in sync with OBS's own mute)                 |
| Toggle mic monitoring   | Turn monitoring ("hearing yourself") on or off                                    |

Saving a clip uses OBS's built-in **Save Replay Buffer** hotkey, not one of these.

---

## Settings

Grouped in the script's settings panel, ordered behavioural first, cosmetic last.

### Sources

| Setting                | Description                                                     |
| ---------------------- | ------------------------------------------------------------- |
| Game capture source    | Name of your Game Capture source                              |
| Display capture source | Name of your Display Capture source                           |
| Mic source             | Name of the mic audio source for the mute / monitor hotkeys   |

### Capture

| Setting                      | Options — default                                                          |
| ---------------------------- | ------------------------------------------------------------------------ |
| Auto game clipping           | On / Off — **On**                                                          |
| Detection interval           | 500–10000 ms — **3000**                                                    |
| Grace after game exits       | 0–600 s — **30**                                                           |
| Fall back to desktop on fail | On / Off — **On**                                                          |
| Desktop auto-off             | 0–1440 min (**0** = never) — **60**                                        |
| Exceptions                   | exe name or folder path, one per line — pre-filled with browsers, media players, `explorer.exe`, `LockApp.exe` |

### Clip files

| Setting                 | Options — default                                                     |
| ----------------------- | ------------------------------------------------------------------- |
| Clips folder            | directory — defaults to OBS's recording path                        |
| Filename template       | `%NAME` plus `strftime` codes — **`%NAME_%d.%m.%Y_%H-%M-%S`**       |
| Desktop clips subfolder | folder name for Display-override clips — **`Desktop`**              |
| Spaces to underscores   | On / Off — **On**                                                   |
| Warn over folder size   | 0–10000 GB (**0** = off) — **100**                                  |

### Replay buffer

| Setting                  | Options — default                    |
| ------------------------ | --------------------------------- |
| Restart after every save | On / Off — **Off**                  |
| Periodic restart         | 0–7200 s (**0** = off) — **3600**   |

### Notifications

| Setting                             | Default |
| ----------------------------------- | ------- |
| Sound on clip saved / failed        | On      |
| Popup on clip saved / failed        | On      |
| Sound on capture toggles            | On      |
| Popup on capture toggles            | On      |
| Popup when saving with clipping off | Off     |

### Popup

| Setting                | Options — default                                          |
| ---------------------- | ------------------------------------------------------- |
| Position (vertical)    | Top / Bottom — **Top**                                   |
| Position (horizontal)  | Left / Center / Right — **Right**                        |
| Scale                  | 0.5–2.0 — **1.0**                                        |
| Height                 | 0–800 px (**0** = auto, tracks screen height) — **0**    |
| Edge margin            | 0–300 px — **16**                                        |
| Stack gap              | 0–200 px — **8**                                         |
| Time on screen         | 0.5–10 s — **2.0**                                       |
| Slide-in / Slide-out   | 0–1000 ms — **150 / 150**                                |

Width auto-fits the label; Scale multiplies height and text together.

### Sounds

| Setting          | Options — default                                                      |
| ---------------- | ------------------------------------------------------------------- |
| Volume offset    | −40 to +12 dB — **0**                                                |
| _(per event)_    | a WAV path for each toast; blank uses the bundled default in `sounds/` |

### Aliases

`full\path\to\game.exe > Display Name`, one per line. A folder path matches every executable beneath it. Used to override the auto-generated per-game folder name.

---

## Notes

- Fullscreen detection is **primary-monitor only**, by design.
- The replay buffer must be running for a save to produce anything. With clipping off and the buffer stopped, OBS's Save Replay hotkey does nothing; enable _Popup when saving with clipping off_ if you want a reminder.
- OBS stores script settings per scene collection. Removing and re-adding the script resets everything to defaults — overwrite `clip_manager.py` in place and reload instead to keep your configuration.
