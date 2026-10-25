# KILN_AutoColor

Automatic spool mapping for the FlashForge AD5X IFS on [Kiln OS](https://github.com/djvalic), like zMod's auto color selection: when a print starts, each slicer tool (T0–T3) is pointed at the loaded IFS slot whose material and color fit it best, so you don't have to map spools by hand.

Without it Kiln always sends T0 to slot 1, T1 to slot 2 and so on, unless you change the map with `IFS_MAP_TOOL` (or on HelixScreen) before every print.

## How it works

The plugin wraps `SDCARD_PRINT_FILE`, the command Mainsail, Fluidd, HelixScreen and OrcaSlicer's upload-and-print all go through. Before the print starts it:

1. reads the file's head and tail (not the whole file, ~0.15 s on the AD5X):
   - which filaments are used: OrcaSlicer / Bambu Studio's `; filament: 1,2,4` header, else `; filament used [mm]`, else every filament listed;
   - `; filament_colour` (or `; extruder_colour`) and `; filament_type` from the config block;
   - `; zmod_color_data` from [zmod_preprocess](https://github.com/DIEHARDave/zmod_preprocess), if present;
2. takes the loaded slots and their type and color from Kiln (`printer.ifs`, `printer.ifs_materials`: set on the screen, with `IFS_SET_MATERIAL`, or by an NFC tag);
3. tries every combination of tools and slots and keeps the one with the lowest total cost:
   - **material** first: the same type is free, the same family (PLA+ for PLA, Silk PLA, PLA-CF) costs `family_penalty`, another family is only used when nothing else is loaded (and reported);
   - **color**: CIE76 ΔE in CIELAB, so pairs are picked the way the eye sees them. ΔE 30 or more is reported as a weak match;
   - **sharing**: two of the file's colors only print from the same slot when giving each its own would be more than `share_penalty` worse;
   - ties keep the default map;
4. sets the map with `IFS_MAP_TOOL` and starts the print;
5. puts the map back to T0–T3 → slots 1–4 when the print ends, however it ends (`reset_at_end`). Kiln resets it too, but not after a print cancelled while paused, which would leave a map the next print start takes for a manual one.

To reset the map by hand at any time: `IFS_MAP_TOOL RESET=1`.

The console shows what it picked:

```
AutoColor (header):
T0 PLA #FF7F37 -> slot 4 PLA+ #FF7F37 (same family, exact)
T1 PLA #BAB86C -> slot 1 PLA #BAB86C (exact)
T3 PLA #FFC0CB -> slot 2 PLA #FFC0CB (exact)
```

It leaves things alone when:

- the tool map was already changed before the print (`IFS_MAP_TOOL`, HelixScreen's tool mapping) — `respect_manual_map`;
- the file has no filament colors or types, the IFS has no reading, or the file can't be read: the print starts with the current map;
- a print is already running.

Problems (a material that isn't loaded, T4 or higher) are reported with `!!`; with `stop_on_mismatch: True` the print is refused instead.

## Install

On the printer, as root:

```sh
git clone https://github.com/DIEHARDave/KILN_AutoColor.git /data/ad5x/plugins/autocolor
chown -R klipper:klipper /data/ad5x/plugins/autocolor
```

Then in the console:

```gcode
ENABLE_PLUGIN NAME=autocolor
```

Klipper restarts. Updates show up in Mainsail / Fluidd's Software Updates (`kiln-autocolor`) after Moonraker's next restart.

After a manual `git pull`, restart the Klipper service (`/etc/init.d/S61klipper restart` as root, or reboot). `FIRMWARE_RESTART` and `RESTART` keep the already-loaded Python module, so the old code keeps running. Updating from Software Updates restarts the service itself.

## Commands

| Command | |
| --- | --- |
| `AUTOCOLOR [FILENAME=] [APPLY=1]` | Show the slots it would pick for a file (default: the last print). `APPLY=1` sets the map now, for the next print. |
| `AUTOCOLOR_ENABLE ENABLE=1\|0` | Switch mapping at print start on or off (kept in save_variables). Without `ENABLE`: show the state. |

`printer.kiln_autocolor` has `enabled` and `last` (file, map, rows, problems) of the last print start.

## Options

Add a `[kiln_autocolor]` section to `local.cfg`; see [plugin.cfg](plugin.cfg) for all options and their defaults (`enabled`, `respect_manual_map`, `reset_at_end`, `stop_on_mismatch`, `family_penalty`, `share_penalty`).

## Tests

```sh
python3 -m unittest discover tests
```

## Status

Tested on an AD5X with Kiln OS 2026.02.3: unit tests, the matching against real OrcaSlicer files and the live slots, real 3-color prints started from the web UI (map set before the file opened), and the map reset after a print cancelled while paused.

## License

GPLv3, see [LICENSE](LICENSE).
