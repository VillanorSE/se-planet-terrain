---
name: se-planet-terrain
description: Use this skill when editing a Space Engineers planet's heightmap from in-game GPS points, for example flattening an area for a base, airfield or road to one point's height, raising or lowering spots by some metres, making a sloped ramp between points, smoothing bumps, undoing an edit, or working out which heightmap face and pixel a GPS point lands on and how high the map ground is there.
---

# SE planet terrain edits

Local heightmap edits on a modded SE planet, driven by GPS points the user copies in game. One Python script does the
work (`scripts/se_terrain.py` next to this file); your job is to turn the request into one command, read its short
output, and report. Iterations are cheap only if you keep them that way: don't read the script, don't explore the
mod, don't write new code.

**Delegate the runs.** If the `planet-terrain-editor` agent is available (it is, when this skill came from the
`se-planet-terrain` plugin), hand each request to it: the user's message verbatim, the absolute path of
`scripts/se_terrain.py` (from this skill's base directory), the `planet.json` path, and "user asked: write" only when
the user asked for the change itself (not just a preview). Relay its short report. Do the runs yourself only when the
agent isn't there.

## Setup (once per planet)

Needs Python 3.9+ with `pip install numpy pillow scipy`.

```
python se_terrain.py --planet <work>/planet.json init --sbc <mod>/Data/PlanetGeneratorDefinitions.sbc \
    --save "<SE saves>/<world>" --heightmaps <mod>/Data/PlanetDataFiles/<Planet> [--generator <SubtypeId>] \
    [--water-config <mod>/Data/WaterConfig.xml]
```

- `--save` is a world that has the planet (its `SANDBOX_0_0_0_.sbs` gives the planet's radius and position).
- Keep `planet.json` in a work folder outside the mod (it isn't shipped): backups, previews and `EDITS.md` go in
  `terrain-edits/` next to it.
- Optional `protect` list in `planet.json`: `{"name": "Port", "gps": "GPS:...", "radius_m": 500}`. Edits never
  change ground inside a zone (bases, POIs that must stay put).
- Check the setup once: `info` on 3-4 GPS points taken standing on untouched bare ground. `GPS-ground` should be
  0-2 m (the character stands a little above the ground). Tens of metres means the wrong world, planet or heightmap
  folder.

## Commands

All edits are dry runs until `--write`. Each prints the ground height at each point before -> after, pixels changed,
slopes, warnings and a `before_after.png` preview.

| Request | Command |
|---|---|
| Where is this point / how high is the ground | `info GPS [GPS ...]` |
| Make B, C level with A (pad along A->B, A->C) | `flatten --to A --at B --at C` |
| Level a road/strip through points in order | `flatten --to A --at B --at C --path` |
| Level to a given height | `flatten --to A --at B --height 12.5` |
| Raise/lower spots | `nudge --at A 3 --at B -2` |
| Sloped flat area between three points | `plane --at A 10 --at B 10 --at C 14` |
| Smooth bumps around points / along a line | `smooth --at A [--at B --path] [--radius 150]` |
| Several edits as one change | `batch edits.txt` (one command per line, `#` comments) |
| Undo | `restore terrain-edits/backups/<face>_<stamp>.png --write` |

Sizes are metres along the ground: `--half` = pad half-width (pad is about 2 x half wide), `--blend` = distance over
which the change fades out. Defaults: flatten 75/350, nudge 40/150, plane 15/350; smooth `--radius 150`,
`--strength 1`.

Flags: `--lower-only` (use for every "lower / cut / remove a hill" request), `--raise-only`, `--water` (also edit
water cells: raising land out of the sea or digging a channel, only when asked), `--move-coast` (allow changing land
that touches water), `--no-protect`, `--heightmaps DIR` (work on a copy).

## Reading requests

- "Same height as point A" means the map ground under A (`--to A`), not A's GPS elevation: a point taken standing on
  a grid or a block reads the grid's height.
- "Raise X by N" on a plane: use X's current map ground (from `info`) + N.
- "Generous smoothing" = larger `--blend`; "keep the shoreline" = the default.
- Width: a road ~ `--half 15-25`; an airfield ~ `--half 40-75`.

## Checking a result (before --write)

- Every point reached its target? A point marked `(fixed: water|coast|protect zone)` was not changed: say so, and
  offer the flag that allows it.
- Slopes: median under ~15% is gentle; a high max right at the pad edge is normal for a steep cut.
- `WARN ... below max water`: changed land can flood at wave and tide peaks (Water Mod). For "keep it dry" aim at
  least 1 m above the max water `info` prints.
- `WARN edit window reaches the face edge`: the seam with the next face isn't blended; keep edits a few pixels from
  face edges. Points on two faces are refused.
- Look at the preview once if anything looks off (magenta = the GPS points, pink = changed).
- More than ~20 m of change or a median slope over ~25%: don't write even if asked; show the dry run and ask (the
  user rarely means a deep trench or a tall wall).
- At most two reruns with adjusted settings; then report what can't be reached and why.

## Facts (verified in the game's code)

- Faces are `front back left right up down` (16-bit grayscale PNG, the same names in the planet's data folder). The
  mapping is `MyCubemapHelpers.CalculateSampleTexcoord` on (position - planet centre). Planet centre = the saved
  `PositionAndOrientation.Position` (min corner) + half the voxel storage size (the smallest power of two from 32 up
  that holds 2 x (Radius + Radius x HillParams.Max)). Pixel = texcoord x (resolution - 1).
- Height above Radius = value / 65535 x (Max - Min) x Radius + Min x Radius (`HillParams`). The game samples it with a
  Catmull-Rom bicubic patch; `info` uses the same.
- A pixel is roughly 2 x Radius / resolution metres (less near face edges); the tool prints the local m/px.
- Water Mod (optional): water radius = `WaterSettings.Radius` x (Radius x (1 + HillParams.Min)); waves add up to
  `WaveHeight`, tide up to `TideHeight` x cos(latitude). World settings set in game override `WaterConfig.xml`; if
  water sits elsewhere in game, edit `water.level_m` in `planet.json`.

## Seeing changes in game

- Reload the save; no game restart needed.
- Voxel changes stored in the save (digging, drilling, explosions, voxel hands) override the heightmap wherever
  they are. If an edit doesn't show, the save probably has voxel data there: test on a fresh world, or revert that
  area's voxels in game.
- The heightmap PNGs are the mod's own files. If the mod is loaded from a local folder, the next reload shows the
  edit. A Workshop copy only changes when it's republished.
