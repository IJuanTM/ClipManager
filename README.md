# Clip Manager

An OBS Studio script that sorts replay-buffer clips into per-game folders, names each file after the program it captured, and pops up a toast when it does. Game capture, desktop capture, and the replay buffer are all driven by fullscreen detection and hotkeys, so you're not flipping scenes by hand.

---

## Features

- Sorts each clip into a folder named after the captured program's executable, with aliases for custom names
- Saving stays on OBS's own Save Replay Buffer hotkey; the script just renames and moves the file after
- Detects a fullscreen program on the primary monitor, shows the Game Capture source, and starts the buffer once OBS confirms the hook
- Keeps the buffer running while any detected program is open; stops it a grace period after the last one exits
- Exceptions list so fullscreen apps that aren't games (browsers, media players, the desktop) don't count
- Display override: a hotkey mode that forces Display Capture and the buffer on, with its own auto-off timer and clip subfolder
- Optional fall back to Display Capture when OBS can't hook a detected program
- A toast popup and a sound per event, both configurable and individually overridable
- Mic mute and monitoring toggles on their own hotkeys; mute stays in sync with OBS's own controls
- Warns when the clips folder grows past a size limit
- Periodic and/or post-save replay-buffer restart, to work around long-session OBS buffer bugs
- Logs every capture decision to the OBS Script Log

---

## Requirements

- **Windows only**: the script leans on Win32 APIs and `winsound` throughout
- **OBS Studio** with Python scripting enabled and pointed at **Python 3.10** (_Tools → Scripts → Python Settings_). 3.12+ fails to load with _"Could not load library"_
- **Pillow** installed into that same Python, for the popup renderer:
  ```text
  "<Python Settings path>\pythonw.exe" -m pip install pillow
  ```
- A configured **replay buffer** in OBS
- A **Game Capture** source set to _"Capture any fullscreen application"_ and a **Display Capture** source, both present in the scene you record

---

## Installation

1. Download `ClipManager-vX.Y.Z.zip` from the [latest release](https://github.com/IJuanTM/ClipManager/releases/latest) and extract the `ClipManager` folder somewhere permanent. It holds `clip_manager.py` with its `icons/` and `sounds/` folders next to it, keep the three together.
2. In OBS: **Tools → Scripts → Python Settings**, and point it at a Python 3.10 install.
3. Install Pillow into that Python (see [Requirements](#requirements)).
4. On the **Scripts** tab, click **+** and pick `clip_manager.py`.
5. Fill in the Game / Display / Mic source names at the top of the script settings.
6. Open **Settings → Hotkeys**, search _"Clip Manager"_, and bind what you want, including OBS's own **Save Replay Buffer**, which is what actually writes a clip.

To update, overwrite the old `clip_manager.py` (and `icons/` / `sounds/`) in place and reload the script. Removing and re-adding it in the Scripts list instead wipes every setting back to default.

---

## How it works

### Game clipping

Every few seconds the script checks the foreground window. If it's fullscreen on the primary monitor and not in the exceptions list, the script shows the Game Capture source and waits for OBS to latch onto it: it listens for OBS's _hooked_ signal and also checks whether the source has started rendering.

- **Hooked**: starts the replay buffer and shows _Game clipping on_, usually a second or two after the program finishes loading.
- **Not hooked**: a short first wait (~30 s), then one longer retry (~2 min). If it still hasn't hooked after that, it shows _Couldn't capture \<program\>_ and, with _Fall back to desktop on fail_ on, switches to Display override. That long retry exists because a program caught mid-load can take close to a minute before OBS is actually able to hook it. If the process exits while waiting, the script gives up immediately instead of sitting out the timeout.

The link is really a set of processes: the buffer keeps running while at least one detected program is open, and stops a grace period (default **30 s**) after the last one closes, so a program that quits and relaunches inside that window never drops the buffer. _Game clipping off_ drops the foreground program right away and won't auto-detect it again until it restarts. _Game clipping on_ just runs the same detect-and-hook sequence once, on demand.

### Display override

A hotkey mode that hides the Game Capture source, shows Display Capture, and starts the buffer. It outranks game detection (detection is ignored while it's on) and turns itself off after a set time, default **60 min**, reset by every clip saved while it's active. Clips saved this way go to a fixed subfolder (default `Desktop`) rather than a per-program one, unless a game is still linked, in which case they still go to that game's folder.

### Clip naming

```text
<Clips folder>/<Name>/<filename template>.<ext>
```

`<Name>` is the linked program's executable name, or an alias set for that path. With nothing linked, it falls back to whichever window process was seen most often across the buffer's length, not whatever happens to be focused at save time. Display-override clips use the desktop subfolder name unless a game is still linked.

### The Script Log

Every capture decision (detection, hook attempts, links, grace timers, fallbacks, hotkey presses) gets printed to **Tools → Scripts → Script Log**, tagged `[detect]` / `[hook]` / `[capture]` / `[display]` / `[hotkey]`. Check it first if detection is acting up.

---

## Notifications

Toasts appear top-right by default and stack downward. One capture change can show two at once: a buffer toast (_Replay on/off_) and a source toast (game/desktop). If a toast is still visible when it repeats, it updates in place instead of stacking a second copy.

```text
+-------+---------------------+
|  (o)  |  Replay on          |
+-------+---------------------+
|  [#]  |  Game clipping on   |
+-------+---------------------+
```

| Toast                                       | Shown when                                                                       |
|---------------------------------------------|----------------------------------------------------------------------------------|
| Replay saved / Saving failed                | A clip was filed, or the move failed                                             |
| Replay on / Replay off                      | The script started or stopped the replay buffer                                  |
| Game clipping on / off                      | A program was hooked or released                                                 |
| Couldn't capture / No fullscreen game found | A detected app couldn't be hooked, or the manual hotkey found nothing fullscreen |
| Desktop capture on / off                    | Display override engaged or released                                             |
| Microphone on / off                         | Mic source unmuted or muted                                                      |
| Listening on / off                          | Mic monitoring toggled                                                           |
| Warning / Info                              | A misconfiguration, a source whose device is gone, or the folder-size limit      |

**Status popups** (_Extra status popups_, on by default) add short "still working" toasts: _Game detected_ before the hook, _Connecting_ during a retry, _Game closed_ when the last program exits. They're popup-only, and also need _Popup on capture toggles_ switched on.

Each toast has its own WAV in `sounds/` (override per event under **Sounds**), plus a global dB offset. The renderer is per-monitor DPI-aware, so popups stay sharp and correctly sized on scaled displays.

---

## Hotkeys

Bound in **Settings → Hotkeys** (search _"Clip Manager"_).

| Action                  | What it does                                                                     |
|-------------------------|----------------------------------------------------------------------------------|
| Game clipping on        | Detect and hook the current fullscreen program now                               |
| Game clipping off       | Drop the foreground program (or all linked ones); no re-detect until it restarts |
| Game clipping toggle    | On or off, depending on current state                                            |
| Display override on     | Switch to Display Capture and buffer, ignoring game detection                    |
| Display override off    | Leave Display override; hands back to game clipping if a program is still linked |
| Display override toggle | On or off                                                                        |
| Toggle mic mute         | Mute or unmute the mic source                                                    |
| Toggle mic monitoring   | Turn monitoring ("hearing yourself") on or off                                   |

Saving a clip uses OBS's built-in **Save Replay Buffer** hotkey, not one of these.

### Stream Deck

[Clip Manager for Stream Deck](https://github.com/IJuanTM/ClipManagerStreamDeckPlugin) adds keys for these actions that show their current state (game clipping, desktop capture, mic, listening, save replay). It talks to OBS over its built-in WebSocket server, so no hotkey bindings are needed.

---

## Settings

Grouped in the script's settings panel, behavioural groups first.

### Sources

| Setting                | Description                                                 |
|------------------------|-------------------------------------------------------------|
| Game capture source    | Name of your Game Capture source                            |
| Display capture source | Name of your Display Capture source                         |
| Mic source             | Name of the mic audio source for the mute / monitor hotkeys |

### Capture

| Setting                      | Options                                                                                                                                        | Default |
|------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------|---------|
| Auto game clipping           | On / Off                                                                                                                                       | On      |
| Detection interval           | 500–10000 ms                                                                                                                                   | 3000    |
| Grace after game exits       | 0–600 s                                                                                                                                        | 30      |
| Fall back to desktop on fail | On / Off                                                                                                                                       | On      |
| Desktop auto-off             | 0–1440 min (0 = never)                                                                                                                         | 60      |
| Exceptions                   | exe name or folder path, one per line — pre-filled with browsers, media players, the Windows shell, the lock screen, and the screen-snip tools |         |

### Clip files

| Setting                   | Options                                     | Default                          |
|---------------------------|---------------------------------------------|----------------------------------|
| Clips folder              | directory                                   | defaults to OBS's recording path |
| Filename template         | `%NAME` plus `strftime` codes               | `%NAME_%d.%m.%Y_%H-%M-%S`        |
| Desktop clips subfolder   | folder name for Display-override clips      | `Desktop`                        |
| Unresolved-game subfolder | folder name when no program can be resolved | `Unknown`                        |
| Spaces to underscores     | On / Off                                    | On                               |
| Warn over folder size     | 0–10000 GB (0 = off)                        | 100                              |

### Replay buffer

| Setting                  | Options            | Default |
|--------------------------|--------------------|---------|
| Restart after every save | On / Off           | Off     |
| Periodic restart         | 0–7200 s (0 = off) | 3600    |

### Notifications

| Setting                                                | Default |
|--------------------------------------------------------|---------|
| Sound on clip saved / failed                           | On      |
| Popup on clip saved / failed                           | On      |
| Sound on capture toggles                               | On      |
| Popup on capture toggles                               | On      |
| Extra status popups (detecting / connecting / closing) | On      |
| Popup when saving with clipping off                    | Off     |

### Popup

| Setting               | Options                                   | Default   |
|-----------------------|-------------------------------------------|-----------|
| Position (vertical)   | Top / Bottom                              | Top       |
| Position (horizontal) | Left / Center / Right                     | Right     |
| Scale                 | 0.5–2.0                                   | 1.0       |
| Height                | 0–800 px (0 = auto, tracks screen height) | 0         |
| Edge margin           | 0–300 px                                  | 16        |
| Stack gap             | 0–200 px                                  | 8         |
| Time on screen        | 0.5–10 s                                  | 2.0       |
| Slide-in / Slide-out  | 0–1000 ms                                 | 150 / 150 |

Width auto-fits the label; Scale multiplies height and text together.

### Sounds

| Setting       | Options                   | Default                                     |
|---------------|---------------------------|---------------------------------------------|
| Volume offset | −40 to +12 dB             | 0                                           |
| _(per event)_ | a WAV path for each toast | blank uses the bundled default in `sounds/` |

### Aliases

`full\path\to\program.exe > Display Name`, one per line. A folder path matches every executable beneath it. Overrides the auto-generated per-program folder name.

---

## Troubleshooting

- **"Couldn't capture" on something that's clearly running**: open **Tools → Scripts → Script Log** and check the `[hook]` lines. Usual causes are the Game Capture source not being set to _"Capture any fullscreen application"_, or it's not in the scene you're recording. A slow-loading program might just need the retry to finish.
- **No popups at all**: Pillow isn't installed in the Python OBS points at, or the OBS Python path (_Tools → Scripts → Python Settings_) is unset. The Script Log tells you which.
- **Script won't load**: OBS needs to be pointed at **Python 3.10**. 3.12+ fails with _"Could not load library"_.
- **Clips aren't being sorted**: the replay buffer actually has to be running. With clipping off and the buffer stopped, OBS's Save Replay hotkey does nothing (turn on _Popup when saving with clipping off_ if you want a reminder for that).
- **Clips have no audio, or desktop clips are black**: OBS remembers audio devices and monitors by an internal ID, and driver or audio-software updates (SteelSeries Sonar, for one) can swap a device for a new ID. The script warns with _Device not found for ..._; re-pick the device in that source's properties.
- **Stream Deck F13-F24 keys can't be bound**: if OBS runs as administrator, Windows blocks key presses the (non-admin) Stream Deck app sends to OBS's window. Run OBS normally, or use the Stream Deck plugin above.
- **Settings reset themselves**: removing and re-adding the script in the Scripts list wipes its config. Overwrite `clip_manager.py` in place and reload instead.

---

## Notes

- Fullscreen detection is **primary-monitor only**, by design.
- Linked programs don't survive an OBS restart or script reload; anything already fullscreen when the script loads is detected fresh.
