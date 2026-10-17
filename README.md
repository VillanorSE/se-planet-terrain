# se-planet-terrain

Edit a modded Space Engineers planet's heightmap from GPS points you take in game: flatten an area to one point's
height, raise or lower spots, lay a sloped plane between three points, smooth bumps, undo. Edits show after a save
reload.

It's a Claude Code plugin with three parts:

- **`se_terrain.py`**, a standalone Python CLI that does all the work. It runs without Claude.
- **The `se-planet-terrain` skill**, which tells Claude how to turn a request like "make these three GPS points level"
  into one command.
- **The `planet-terrain-editor` sub-agent** (Sonnet). It runs the edit, checks it and reports in a few lines. Terrain
  work is iterative, so every round stays cheap, and the main session only reads the report.

GPS points land on the right pixel with no calibration: the mapping, the planet centre and the height sampling follow
the game's own code (`MyCubemapHelpers`, `MyPlanetShapeProvider`, `MyVoxelBase`). Tested on one planet with 45
in-game GPS points at 9 sites: each landed within 0.6 px of the spot a per-site fitted calibration had found. Where
the ground was untouched, GPS height matched the map to 0.02-0.3 m rms per site.

## Install

In Claude Code:

```
/plugin marketplace add VillanorSE/se-planet-terrain
/plugin install se-planet-terrain@se-planet-terrain
```

Python 3.9+ with `pip install numpy pillow scipy`.

## Set up a planet (once)

```
python skills/se-planet-terrain/scripts/se_terrain.py --planet work/planet.json init \
    --sbc <mod>/Data/PlanetGeneratorDefinitions.sbc \
    --save "%APPDATA%/SpaceEngineers/Saves/<id>/<world>" \
    --heightmaps <mod>/Data/PlanetDataFiles/<Planet> \
    [--generator <SubtypeId>] [--water-config <mod>/Data/WaterConfig.xml]
```

This reads `HillParams` from the generator, then the planet's radius and position from the save, and Water Mod
settings if given. It writes `planet.json`. Keep that file in a work folder, not in the mod. Backups, previews and an
`EDITS.md` log go in `terrain-edits/` next to it. You can add protect zones there (bases, POIs) that edits never
touch.

Check the setup with `info` on a few GPS points taken standing on bare ground. `GPS-ground` should be 0-2 m.

## Use

Ask Claude in plain words, e.g. *"flatten from GPS:A to GPS:B and GPS:C, ~150 m wide, keep the shoreline"*. Or run
the CLI:

```
se_terrain.py info "GPS:A:..." "GPS:B:..."
se_terrain.py flatten --to "GPS:A:..." --at "GPS:B:..." --at "GPS:C:..." --half 75
se_terrain.py nudge --at "GPS:A:..." -3 --lower-only
se_terrain.py plane --at "GPS:A:..." 10 --at "GPS:B:..." 10 --at "GPS:C:..." 14
se_terrain.py smooth --at "GPS:A:..." --radius 200
se_terrain.py batch edits.txt --write
se_terrain.py restore terrain-edits/backups/down_20261016-195905.png --write
```

Edits are dry runs that print before/after heights, slopes and a preview PNG until you add `--write`. A write backs
up the face first.

Limits: one face per edit (seams aren't blended), no material or ore maps, and voxel changes saved in a world
override the heightmap where they exist.

## License

MIT
