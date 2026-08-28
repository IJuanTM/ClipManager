# Clip Manager

An OBS Studio script that files replay-buffer clips into per-game folders, names each one after the game it captured, and shows a popup when it does. Game and desktop capture, and the replay buffer itself, are driven by hotkeys and fullscreen detection instead of manual scene switching.

---

## Features

- Files each clip into a folder named after the captured game's executable, with alias overrides for custom names
- Saving stays on OBS's own Save Replay Buffer hotkey — the script only names and moves the file, on OBS's save event
- Detects a fullscreen game on the primary monitor, shows the Game Capture source, and starts the buffer once OBS confirms the hook
- Keeps the buffer running while any detected game is open; stops it a grace period after the last one exits
- Exceptions list (browsers, media players, the Windows shell) for fullscreen apps that are not games
- Display override — a hotkey mode that forces Display Capture and the buffer on, with its own auto-off timer and clip subfolder
- Optional fall back to Display Capture when OBS cannot hook a detected game
- A toast popup and a sound per event, both configurable, each overridable
- Mic mute and monitoring toggles on their own hotkeys; mute stays in sync with OBS's native controls
- Warns when the clips folder grows past a size limit
- Periodic and/or post-save replay-buffer restart, to work around long-session OBS buffer bugs

---

## How it works

### Game clipping

Every few seconds the script checks the foreground window. If it is a fullscreen app on the primary monitor and not in the exceptions list, the script shows the Game Capture source and waits for OBS to confirm the hook — it listens for OBS's own _hooked_ signal and also watches whether the source has started rendering:

- **Hooked** — start the replay buffer, show _Game clipping on_. This is usually within a second or two of the game finishing loading.
- **Not hooked after ~15 s** — show _No game to capture_ and, if _Fall back to desktop on fail_ is on, switch to Display override. The wait is generous on purpose: a game detected while still on a loading screen can take several seconds before OBS can hook it.

The link is a set of processes: the buffer runs while at least one detected game is open and stops a grace period (default **30 s**) after the last one exits. The _Game clipping off_ hotkey drops the foreground game immediately and won't re-detect it until it restarts. Manual _Game clipping on_ runs the same detect-and-hook sequence once, right away.

### Display override

A hotkey mode that hides the game source, shows the Display Capture source, and starts the buffer. It outranks game detection — detection is ignored while it is on — and turns itself off after a set time (default **60 min**, reset on every clip saved while it is active). Clips saved in this mode go to a fixed subfolder (default `Desktop`) instead of a per-game folder.

### Clip naming

```text
<Clips folder>/<Game>/<filename template>.<ext>
```

`<Game>` is the linked game's executable name, or an alias set for that path. With no game linked it falls back to the window process seen most across the buffer's length, not whatever is focused at save time. Display-override clips use the desktop subfolder name.

---

## Notifications

Toasts appear at the top-right by default and stack downward. A capture change can show two at once — a buffer toast (_Replay on/off_) and a source toast (game/desktop). A repeat of a still-visible kind updates in place instead of stacking.

```text
+-------+---------------------+
|  (o)  |  Replay on          |
+-------+---------------------+
|  [#]  |  Game clipping on   |
+-------+---------------------+
```

| Toast                        | Shown when                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------- |
| Replay saved / Saving failed | A clip was filed, or the move failed                                              |
| Replay on / Replay off       | The script started or stopped the replay buffer                                  |
| Game clipping on / off       | A game was hooked or released                                                     |
| No game to capture           | A detected app could not be hooked, or the manual hotkey found no fullscreen game |
| Desktop capture on / off     | Display override engaged or released                                              |
| Microphone on / off          | Mic source unmuted or muted                                                       |
| Listening on / off           | Mic monitoring toggled                                                            |
| Warning / Info               | A misconfiguration, or the folder-size limit was crossed                          |

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

1. Put `clip_manager.py` somewhere permanent, with its `icons/` and `sounds/` folders next to it.
2. In OBS, open **Tools > Scripts > Python Settings** and point it at a Python 3.10 install.
3. Install Pillow into that Python (see [Requirements](#requirements)).
4. On the **Scripts** tab, click **+** and pick `clip_manager.py`.
5. Fill in the three source names at the top of the script settings.
6. Open **Settings > Hotkeys**, search _"Clip Manager"_, and bind what you want — including **Save Replay Buffer**, which is what actually saves a clip.

---

## Hotkeys

Bound in **Settings > Hotkeys** (search _"Clip Manager"_).

| Action                  | What it does                                                                       |
| ----------------------- | ------------------------------------------------------------------------------- |
| Game clipping on        | Detect and hook the current fullscreen app now                                    |
| Game clipping off       | Drop the foreground game (or all linked games); no re-detect until it restarts    |
| Game clipping toggle    | On or off, depending on current state                                             |
| Display override on     | Switch to Display Capture and buffer, ignoring game detection                      |
| Display override off    | Leave Display override; hands back to game clipping if a game is still linked      |
| Display override toggle | On or off                                                                          |
| Toggle mic mute         | Mute or unmute the mic source                                                     |
| Toggle mic monitoring   | Turn monitoring ("hearing yourself") on or off                                    |

Saving a clip uses OBS's built-in **Save Replay Buffer** hotkey, not one of these.

---

## Settings

Grouped in the script's settings panel, behavioural groups first.

### Sources

| Setting                | Description                                                   |
| ---------------------- | --------------------------------------------------------- |
| Game capture source    | Name of your Game Capture source                            |
| Display capture source | Name of your Display Capture source                         |
| Mic source             | Name of the mic audio source for the mute / monitor hotkeys |

### Capture

| Setting                      | Options — default                                                          |
| ---------------------------- | ---------------------------------------------------------------------- |
| Auto game clipping           | On / Off — **On**                                                        |
| Detection interval           | 500–10000 ms — **3000**                                                  |
| Grace after game exits       | 0–600 s — **30**                                                         |
| Fall back to desktop on fail | On / Off — **On**                                                        |
| Desktop auto-off             | 0–1440 min (**0** = never) — **60**                                      |
| Exceptions                   | exe name or folder path, one per line — pre-filled with browsers, media players, the Windows shell, the lock screen, and the screen-snip tools |

### Clip files

| Setting                 | Options — default                                             |
| ----------------------- | -------------------------------------------------------- |
| Clips folder            | directory — defaults to OBS's recording path              |
| Filename template       | `%NAME` plus `strftime` codes — **`%NAME_%d.%m.%Y_%H-%M-%S`** |
| Desktop clips subfolder | folder name for Display-override clips — **`Desktop`**    |
| Spaces to underscores   | On / Off — **On**                                          |
| Warn over folder size   | 0–10000 GB (**0** = off) — **100**                        |

### Replay buffer

| Setting                  | Options — default                  |
| ------------------------ | ------------------------------ |
| Restart after every save | On / Off — **Off**                |
| Periodic restart         | 0–7200 s (**0** = off) — **3600** |

### Notifications

| Setting                             | Default |
| ----------------------------------- | ------- |
| Sound on clip saved / failed        | On      |
| Popup on clip saved / failed        | On      |
| Sound on capture toggles            | On      |
| Popup on capture toggles            | On      |
| Popup when saving with clipping off | Off     |

### Popup

| Setting               | Options — default                                       |
| --------------------- | -------------------------------------------------- |
| Position (vertical)   | Top / Bottom — **Top**                              |
| Position (horizontal) | Left / Center / Right — **Right**                   |
| Scale                 | 0.5–2.0 — **1.0**                                   |
| Height                | 0–800 px (**0** = auto, tracks screen height) — **0** |
| Edge margin           | 0–300 px — **16**                                   |
| Stack gap             | 0–200 px — **8**                                    |
| Time on screen        | 0.5–10 s — **2.0**                                  |
| Slide-in / Slide-out  | 0–1000 ms — **150 / 150**                           |

Width auto-fits the label; Scale multiplies height and text together.

### Sounds

| Setting       | Options — default                                                     |
| ------------- | --------------------------------------------------------------- |
| Volume offset | −40 to +12 dB — **0**                                            |
| _(per event)_ | a WAV path for each toast; blank uses the bundled default in `sounds/` |

### Aliases

`full\path\to\game.exe > Display Name`, one per line. A folder path matches every executable beneath it. Overrides the auto-generated per-game folder name.

---

## Notes

- Fullscreen detection is **primary-monitor only**, by design.
- The replay buffer has to be running for a save to produce anything. With clipping off and the buffer stopped, OBS's Save Replay hotkey does nothing; turn on _Popup when saving with clipping off_ for a reminder.
- OBS stores script settings per scene collection. Removing and re-adding the script resets everything to defaults — overwrite `clip_manager.py` in place and reload to keep your configuration.
