"""Space Engineers planet heightmap edits from in-game GPS points. Dry run unless --write.

  python se_terrain.py init    --sbc PlanetGeneratorDefinitions.sbc --save WORLD_DIR --heightmaps DIR [--generator ID]
                               [--water-config WaterConfig.xml | --water-ratio R [--wave M] [--tide M]] [--out planet.json]
  python se_terrain.py info    GPS [GPS ...]
  python se_terrain.py flatten --to GPS --at GPS [--at GPS ...] [--height M] [--path] [--half 75] [--blend 350]
  python se_terrain.py nudge   --at GPS DZ [--at GPS DZ ...] [--half 40] [--blend 150]
  python se_terrain.py plane   --at GPS M --at GPS M --at GPS M [--half 15] [--blend 350]
  python se_terrain.py smooth  --at GPS [--at GPS ...] [--radius 150] [--strength 1]
  python se_terrain.py batch   FILE            (one command per line, applied in order, one preview, one write)
  python se_terrain.py restore BACKUP.png

GPS is the string SE copies from the GPS screen, "GPS:Name:x:y:z:..." (quote it). Points of one edit must be on one
cube face. Distances (--half, --blend, --radius) are metres along the ground; heights are metres above the planet's
Radius (the same datum as GPS elevation = |position - centre| - Radius).

The planet comes from planet.json (--planet, else $SE_TERRAIN_PLANET, else ./planet.json); `init` writes it.
Mapping and height sampling follow the game's own code (Sandbox.Engine.Voxels.Planet.MyCubemapHelpers.
CalculateSampleTexcoord, MyPlanetShapeProvider: pixel = texcoord * (resolution - 1), Catmull-Rom bicubic,
height = value * (max - min) * Radius + min * Radius).

Edits: pad = ground within --half of the shape, moved to its target; around it the change fades to 0 over --blend
(Laplace solve). Never changed unless asked: water cells (--water), land touching water (--move-coast), protect zones
from planet.json (--no-protect). --lower-only / --raise-only clip the change. --write saves the face PNGs, keeps a
backup in <work_dir>/backups/ and appends a line to <work_dir>/EDITS.md.
"""
import argparse, datetime, json, math, os, re, shlex, shutil, sys
import xml.etree.ElementTree as ET
import numpy as np
from PIL import Image
from scipy import ndimage, sparse
from scipy.sparse.linalg import spsolve

FACES = ['front', 'back', 'left', 'right', 'up', 'down']   # MyCubemapHelpers.Faces order
CR = 0.5 * np.array([[0, 2, 0, 0], [-1, 0, 1, 0], [2, -5, 4, -1], [-1, 3, -3, 1]], float)   # Catmull-Rom, a = 0.5


# ---------------------------------------------------------------- planet
class Planet:
    def __init__(self, path, heightmaps=None):
        self.path = os.path.abspath(path)
        if not os.path.exists(self.path):
            sys.exit(f'no planet config at {self.path}; run `init` first or pass --planet')
        c = json.load(open(self.path, encoding='utf-8'))
        here = os.path.dirname(self.path)
        self.cfg = c
        self.dir = os.path.join(here, heightmaps or c['heightmaps'])
        self.R, self.hmin, self.hmax = c['radius'], c['hill_min'], c['hill_max']
        self.centre = np.array(c['centre'], float)
        fwd, up = np.array(c.get('forward', [0, 0, -1]), float), np.array(c.get('up', [0, 1, 0]), float)
        self.rot = np.array([np.cross(fwd, up), up, -fwd])   # rows: planet local x, y, z in world space
        w = c.get('water')
        self.water = w['level_m'] if w else None
        self.wave, self.tide = (w.get('wave_m', 0), w.get('tide_m', 0)) if w else (0, 0)
        self.work = os.path.join(here, c.get('work_dir', 'terrain-edits'))
        self.protect = c.get('protect', [])
        self.faces = {}

    # heights
    def to_elev(self, raw):
        return self.R * (self.hmin + raw.astype(np.float64) / 65535.0 * (self.hmax - self.hmin))

    def to_raw(self, e):
        return np.clip(np.rint((e / self.R - self.hmin) / (self.hmax - self.hmin) * 65535.0), 0, 65535).astype(np.uint16)

    def face(self, f):
        """[path, original raw, current raw] of a face, loaded once."""
        if f not in self.faces:
            p = os.path.join(self.dir, f + '.png')
            a = np.array(Image.open(p))
            if a.dtype != np.uint16:
                sys.exit(f'{p} is not a 16-bit grayscale PNG')
            self.faces[f] = [p, a, a.copy()]
        return self.faces[f]

    # mapping
    def local(self, xyz):
        return self.rot @ (np.asarray(xyz, float) - self.centre)

    def to_pixel(self, xyz):
        x, y, z = self.local(xyz)
        ax, ay, az = abs(x), abs(y), abs(z)
        if ax > ay and ax > az:
            fi, u, v = (2, -z / ax, -y / ax) if x > 0 else (3, z / ax, -y / ax)
        elif ay > az and not ax > ay:
            fi, u, v = (4, -x / ay, -z / ay) if y > 0 else (5, x / ay, -z / ay)
        else:
            fi, u, v = (1, x / az, -y / az) if z > 0 else (0, -x / az, -y / az)
        n = self.face(FACES[fi])[1].shape[0] - 1
        return FACES[fi], (u + 1) / 2 * n, (v + 1) / 2 * n

    def dir_of(self, f, px, py, n):
        u, v = np.asarray(px, float) / n * 2 - 1, np.asarray(py, float) / n * 2 - 1
        one = np.ones_like(u)
        d = {'front': (-u, -v, -one), 'back': (u, -v, one), 'left': (one, -v, -u), 'right': (-one, -v, u),
             'up': (-u, one, -v), 'down': (u, -one, -v)}[f]
        d = np.stack(d, -1)
        return d / np.linalg.norm(d, axis=-1, keepdims=True)

    def mpp(self, f, px, py):
        n = self.face(f)[1].shape[0] - 1
        a, b = self.dir_of(f, px, py, n), self.dir_of(f, px + 1, py, n)
        return float(np.linalg.norm(a - b) * self.R)

    def max_water(self, f, xx, yy):
        """Highest water surface (Water Mod: level + full wave + tide * cos(latitude about the planet y axis))."""
        if self.water is None:
            return None
        d = self.dir_of(f, xx, yy, self.face(f)[1].shape[0] - 1)
        return self.water + self.wave + self.tide * np.sqrt(np.clip(1 - d[..., 1] ** 2, 0, 1))


def bicubic(E, x, y):
    """The game's Catmull-Rom patch: samples x-1..x+2, y-1..y+2 around floor(x), floor(y)."""
    n = E.shape[0] - 1
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    xs = np.clip(np.arange(x0 - 1, x0 + 3), 0, n); ys = np.clip(np.arange(y0 - 1, y0 + 3), 0, n)
    C = E[np.ix_(ys, xs)]
    s, t = x - x0, y - y0
    return float(np.array([1, t, t * t, t ** 3]) @ CR @ C @ CR.T @ np.array([1, s, s * s, s ** 3]))


def parse_gps(s):
    m = re.match(r'\s*GPS:([^:]*):(-?[\d.]+):(-?[\d.]+):(-?[\d.]+)', s)
    if not m:
        sys.exit(f'not a GPS string: {s!r} (expected "GPS:Name:x:y:z:...")')
    return m.group(1), (float(m.group(2)), float(m.group(3)), float(m.group(4)))


def point(P, s):
    name, xyz = parse_gps(s)
    f, px, py = P.to_pixel(xyz)
    return {'name': name, 'face': f, 'px': px, 'py': py, 'gps_elev': float(np.linalg.norm(P.local(xyz)) - P.R)}


# ---------------------------------------------------------------- commands
def cmd_info(P, A):
    for s in A.gps:
        p = point(P, s)
        E = P.to_elev(P.face(p['face'])[2])
        g = bicubic(E, p['px'], p['py'])
        mpp = P.mpp(p['face'], p['px'], p['py'])
        line = (f"{p['name']}: {p['face']} px {p['px']:.1f} py {p['py']:.1f}  GPS elev {p['gps_elev']:.2f}  "
                f"map ground {g:.2f} (GPS-ground {p['gps_elev'] - g:+.2f})  {mpp:.1f} m/px")
        if P.water is not None:
            x, y = int(round(p['px'])), int(round(p['py']))
            win = E[max(y - 60, 0):y + 61, max(x - 60, 0):x + 61] <= P.water
            wd = ndimage.distance_transform_edt(~win)[min(y, 60), min(x, 60)] * mpp if win.any() else None
            line += f"  max water {float(P.max_water(p['face'], p['px'], p['py'])):.2f}  water {'%.0f m' % wd if wd is not None else '>1.5 km'}"
        zones = protect_zones(P, p['face'])
        if zones:
            z = min(zones, key=lambda z: math.hypot(z['px'] - p['px'], z['py'] - p['py']))
            line += f"  nearest protect '{z['name']}' {math.hypot(z['px'] - p['px'], z['py'] - p['py']) * mpp:.0f} m"
        if min(p['px'], p['py'], E.shape[0] - 1 - p['px'], E.shape[0] - 1 - p['py']) < 3:
            line += '  (at a face edge)'
        print(line)


def protect_zones(P, face):
    out = []
    for z in P.protect:
        q = point(P, z['gps'])
        if q['face'] == face:
            out.append({'name': z.get('name', q['name']), 'px': q['px'], 'py': q['py'], 'radius_m': z.get('radius_m', 500)})
    return out


def segdist(xx, yy, a, b):
    (ax, ay), (bx, by) = a, b
    L = (bx - ax) ** 2 + (by - ay) ** 2
    t = np.clip(((xx - ax) * (bx - ax) + (yy - ay) * (by - ay)) / L, 0, 1) if L > 0 else 0
    return np.hypot(xx - ax - t * (bx - ax), yy - ay - t * (by - ay))


def edit(P, A, points, target_fn, shapes, label, inside=None):
    """target_fn(e, xx, yy) -> pad delta (m). Changes P.faces[face][2] in place; returns a summary dict."""
    face = points[0]['face']
    bad = [p['name'] for p in points if p['face'] != face]
    if bad:
        sys.exit(f'points on different faces ({face} vs {", ".join(bad)}); edits across a face seam are not supported')
    path, _, raw0 = P.face(face)
    N = raw0.shape[0]
    E = P.to_elev(raw0)
    xs, ys = [p['px'] for p in points], [p['py'] for p in points]
    mpp = P.mpp(face, float(np.mean(xs)), float(np.mean(ys)))
    half, blend = A.half / mpp, A.blend / mpp
    m = int(half + blend + 6)
    OX, OY = max(int(min(xs)) - m, 0), max(int(min(ys)) - m, 0)
    X1, Y1 = min(int(max(xs)) + m + 2, N), min(int(max(ys)) + m + 2, N)
    e = E[OY:Y1, OX:X1].copy(); H, W = e.shape
    yy, xx = np.mgrid[OY:Y1, OX:X1].astype(float)
    d = np.minimum.reduce([segdist(xx, yy, a, b) for a, b in shapes])
    use_water = getattr(A, 'water', False)
    water = e <= P.water if P.water is not None else np.zeros_like(e, bool)
    water_fixed = water & (not use_water)
    coast = ndimage.binary_dilation(water, np.ones((3, 3), bool)) & ~water & (not A.move_coast)
    zone = np.zeros_like(water)
    prot = []
    if not A.no_protect:
        for z in protect_zones(P, face):
            r = np.hypot(xx - z['px'], yy - z['py']) * mpp <= z['radius_m']
            if r.any():
                zone |= r; prot.append(z['name'])
    fixed = water_fixed | coast | zone
    pad = d <= half
    if inside is not None:
        pad |= inside(xx, yy)
    for p in points:   # a point between pixel centres still edits its nearest pixel
        pad[min(int(round(p['py'])) - OY, H - 1), min(int(round(p['px'])) - OX, W - 1)] = True
    pad &= ~fixed
    free = (d > half) & (d < half + blend) & ~fixed & ~pad
    delta = np.where(pad, target_fn(e, xx, yy), 0.0)
    idx = -np.ones((H, W), int); fy, fx = np.nonzero(free); idx[fy, fx] = np.arange(len(fy))
    rows, cols, vals, rhs = [], [], [], np.zeros(len(fy))
    for k, (y, x) in enumerate(zip(fy, fx)):
        rows.append(k); cols.append(k); vals.append(4.0)
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < H and 0 <= nx < W and idx[ny, nx] >= 0:
                rows.append(k); cols.append(idx[ny, nx]); vals.append(-1.0)
            elif 0 <= ny < H and 0 <= nx < W:
                rhs[k] += delta[ny, nx]
    if len(fy):
        delta[fy, fx] = spsolve(sparse.csr_matrix((vals, (rows, cols)), shape=(len(fy), len(fy))), rhs)
    if A.lower_only:
        delta = np.minimum(delta, 0)
    if A.raise_only:
        delta = np.maximum(delta, 0)
    e2 = e + delta
    if P.water is not None:   # land never ends at or below the water line, unless it already was
        e2 = np.where(water_fixed, e, np.maximum(e2, np.minimum(e, P.water + 0.5)))
        if use_water:
            e2 = np.where(water, e + delta, e2)
    new = P.to_raw(e2)
    keep = water_fixed | (np.abs(delta) < 1e-9)
    win0 = raw0[OY:Y1, OX:X1].copy()
    raw0[OY:Y1, OX:X1] = np.where(keep, win0, new)
    e_after = P.to_elev(raw0[OY:Y1, OX:X1]); ch = e_after - e
    n = int((raw0[OY:Y1, OX:X1] != win0).sum())
    E2 = P.to_elev(raw0)
    print(f'{label}  [{face}, {mpp:.1f} m/px]')
    if prot:
        print('  protected:', ', '.join(prot))
    for p in points:
        y, x = min(int(round(p['py'])) - OY, H - 1), min(int(round(p['px'])) - OX, W - 1)
        why = 'water' if water_fixed[y, x] else 'protect zone' if fixed[y, x] and not water.any() or \
            fixed[y, x] and not ndimage.binary_dilation(water, np.ones((3, 3), bool))[y, x] else 'coast' if fixed[y, x] else ''
        print(f"  {p['name']}: ground {bicubic(E, p['px'], p['py']):7.2f} -> {bicubic(E2, p['px'], p['py']):7.2f} m"
              + (f'  (fixed: {why})' if why else ''))
    print(f'  {n} px changed, max raise {ch.max():+.2f} m, max lower {ch.min():+.2f} m', end='')
    if n:
        gy, gx = np.gradient(e_after, mpp); sl = np.hypot(gx, gy)[(np.abs(ch) > 0.05) & ~water]
        if sl.size:
            print(', slope median %.0f%% / 95th %.0f%% / max %.0f%%' % tuple(100 * np.percentile(sl, [50, 95, 100])), end='')
    print()
    if P.water is not None and n:
        mw = P.max_water(face, xx, yy)
        low = (raw0[OY:Y1, OX:X1] != win0) & (e_after > P.water) & (e_after < mw)
        if low.any():
            print(f'  WARN {int(low.sum())} changed land px end below max water ({mw.min():.1f}-{mw.max():.1f} m): can flood at wave/tide peaks')
    if min(OX, OY) == 0 or max(X1, Y1) == N:
        print('  WARN edit window reaches the face edge: the seam with the next face is not blended')
    return {'face': face, 'box': (OX, OY, X1, Y1), 'before': e, 'points': points, 'label': label, 'n': n,
            'lo': float(ch.min()), 'hi': float(ch.max())}


def cmd_flatten(P, A):
    to = point(P, A.to); ats = [point(P, s) for s in A.at]
    pts = [to] + ats
    xy = [(p['px'], p['py']) for p in pts]
    shapes = list(zip(xy[:-1], xy[1:])) if A.path else [(xy[0], b) for b in xy[1:]] or [(xy[0], xy[0])]
    T = A.height if A.height is not None else bicubic(P.to_elev(P.face(to['face'])[2]), to['px'], to['py'])
    return edit(P, A, pts, lambda e, xx, yy: T - e, shapes,
                f"flatten to {T:.2f} m ({to['name']} -> {', '.join(p['name'] for p in ats) or 'itself'})")


def cmd_nudge(P, A):
    pts = [point(P, s) for s, _ in A.at]; dz = np.array([float(v) for _, v in A.at])

    def target(e, xx, yy):   # each pad pixel takes the dz of its nearest point
        return dz[np.stack([np.hypot(xx - p['px'], yy - p['py']) for p in pts]).argmin(0)]
    return edit(P, A, pts, target, [((p['px'], p['py']),) * 2 for p in pts],
                'nudge ' + ', '.join(f"{p['name']} {d:+.2f} m" for p, d in zip(pts, dz)))


def cmd_plane(P, A):
    if len(A.at) != 3:
        sys.exit('plane needs exactly three --at GPS M')
    pts = [point(P, g) for g, _ in A.at]; hs = [float(h) for _, h in A.at]
    (x1, y1), (x2, y2), (x3, y3) = [(p['px'], p['py']) for p in pts]
    a, b, c = np.linalg.solve(np.array([[x1, y1, 1], [x2, y2, 1], [x3, y3, 1]], float), np.array(hs))
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)

    def inside(xx, yy):
        l1 = ((y2 - y3) * (xx - x3) + (x3 - x2) * (yy - y3)) / det
        l2 = ((y3 - y1) * (xx - x3) + (x1 - x3) * (yy - y3)) / det
        return (l1 >= 0) & (l2 >= 0) & (1 - l1 - l2 >= 0)
    return edit(P, A, pts, lambda e, xx, yy: a * xx + b * yy + c - e,
                [((x1, y1), (x2, y2)), ((x2, y2), (x3, y3)), ((x3, y3), (x1, y1))],
                'plane ' + ', '.join(f"{p['name']} {h:.2f} m" for p, h in zip(pts, hs)), inside)


def cmd_smooth(P, A):
    pts = [point(P, s) for s in A.at]
    A.half, A.blend = A.radius, A.radius * 0.5
    mpp = P.mpp(pts[0]['face'], pts[0]['px'], pts[0]['py'])
    sigma = max(A.radius / mpp / 3, 0.7)

    def target(e, xx, yy):
        return (ndimage.gaussian_filter(e, sigma, mode='nearest') - e) * A.strength
    shapes = list(zip([(p['px'], p['py']) for p in pts[:-1]], [(p['px'], p['py']) for p in pts[1:]])) if A.path \
        else [((p['px'], p['py']),) * 2 for p in pts]
    return edit(P, A, pts, target, shapes, f"smooth r {A.radius:.0f} m x{A.strength:g} at " + ', '.join(p['name'] for p in pts))


def cmd_restore(P, A):
    face = os.path.basename(A.backup).split('_')[0]
    path = os.path.join(P.dir, f'{face}.png')
    n = int((np.array(Image.open(A.backup)) != np.array(Image.open(path))).sum())
    if A.write:
        shutil.copy2(A.backup, path)
        log(P, f"{face}: restored from `{os.path.basename(A.backup)}` ({n} px)")
        print('restored', path, 'from', A.backup)
    else:
        print(f'would restore {path} ({n} px differ); add --write')


def log(P, text):
    os.makedirs(P.work, exist_ok=True)
    with open(os.path.join(P.work, 'EDITS.md'), 'a', encoding='utf-8') as fh:
        fh.write(f"- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def hillshade(z, mpp):
    gy, gx = np.gradient(z, mpp)
    s = (-gx - gy) / np.sqrt(2) / np.sqrt(1 + gx * gx + gy * gy) + 1 / np.sqrt(1 + gx * gx + gy * gy)
    return np.clip(128 + 110 * (s - 0.7) * 2.5, 0, 255)


def preview(P, results, out):
    """One before|after PNG per edited face, covering every edit window on it."""
    os.makedirs(out, exist_ok=True)
    paths = []
    for f in sorted({r['face'] for r in results}):
        rs = [r for r in results if r['face'] == f]
        OX, OY = min(r['box'][0] for r in rs), min(r['box'][1] for r in rs)
        X1, Y1 = max(r['box'][2] for r in rs), max(r['box'][3] for r in rs)
        _, orig, cur = P.face(f)
        b, a = P.to_elev(orig[OY:Y1, OX:X1]), P.to_elev(cur[OY:Y1, OX:X1])
        mpp = P.mpp(f, (OX + X1) / 2, (OY + Y1) / 2)
        H, W = b.shape; sc = max(1, min(8, 700 // max(H, W)))
        water = b <= P.water if P.water is not None else np.zeros_like(b, bool)
        lo = np.percentile(b[~water], 2) if (~water).any() else b.min()
        imgs = []
        for z in (b, a):
            hs, c = hillshade(z, mpp), np.clip((z - lo) / 40, 0, 1)
            rgb = np.stack([hs * (0.5 + 0.5 * c), hs * (0.75 + 0.25 * c), hs * (1 - 0.4 * c)], -1)
            rgb[z <= (P.water if P.water is not None else -1e9)] = [40, 80, 160]
            rgb[(np.abs(a - b) > 0.05) & (z is a)] *= [1, 0.85, 0.85]
            rgb = np.repeat(np.repeat(rgb, sc, 0), sc, 1).astype(np.uint8)
            for r in rs:
                for p in r['points']:
                    X, Y = int((p['px'] - OX + 0.5) * sc), int((p['py'] - OY + 0.5) * sc)
                    rgb[max(Y - 3, 0):Y + 4, max(X - 3, 0):X + 4] = [255, 0, 255]
            imgs.append(rgb)
        side = np.concatenate([imgs[0], np.full((H * sc, 6, 3), 255, np.uint8), imgs[1]], 1)
        p = os.path.join(out, f'{f}_before_after.png'); Image.fromarray(side).save(p); paths.append(p)
    return paths


def finish(P, A, results):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    for p in preview(P, results, A.out or os.path.join(P.work, 'previews', stamp)):
        print('preview', p, '(left before, right after; magenta = GPS points, pink tint = changed)')
    if not A.write:
        print('dry run (add --write to save)')
        return
    bdir = os.path.join(P.work, 'backups'); os.makedirs(bdir, exist_ok=True)
    for f, (path, orig, cur) in P.faces.items():
        if (orig != cur).any():
            bk = os.path.join(bdir, f'{f}_{stamp}.png'); shutil.copy2(path, bk)
            Image.fromarray(cur).save(path)
            rs = [r for r in results if r['face'] == f]
            log(P, f"{f}: " + '; '.join(f"{r['label']} ({r['n']} px, {r['lo']:+.1f}/{r['hi']:+.1f} m)" for r in rs)
                + f"; backup `backups/{os.path.basename(bk)}`")
            print('WRITTEN', path, '\nbackup ', bk)


# ---------------------------------------------------------------- init
def cmd_init(P_unused, A):
    root = ET.parse(A.sbc).getroot()
    gens = [d for d in root.iter() if d.tag == 'Definition' or d.tag.endswith('PlanetGeneratorDefinition')]
    gens = [d for d in gens if d.find('HillParams') is not None]
    if A.generator:
        gens = [d for d in gens if d.findtext('Id/SubtypeId') == A.generator]
    if len(gens) != 1:
        sys.exit('pick the planet with --generator: ' + ', '.join(d.findtext('Id/SubtypeId') or '?' for d in gens))
    g = gens[0]; gid = g.findtext('Id/SubtypeId')
    hp = g.find('HillParams'); hmin, hmax = float(hp.get('Min')), float(hp.get('Max'))
    sbs = A.save if A.save.lower().endswith('.sbs') else os.path.join(A.save, 'SANDBOX_0_0_0_.sbs')
    planets = []
    for _, el in ET.iterparse(sbs):
        if el.tag == 'MyObjectBuilder_EntityBase' and el.get('{http://www.w3.org/2001/XMLSchema-instance}type') == 'MyObjectBuilder_Planet':
            if el.findtext('PlanetGenerator') == gid:
                po = el.find('PositionAndOrientation')
                v = lambda t: [float(po.find(t).get(k)) for k in 'xyz']
                planets.append((float(el.findtext('Radius')), v('Position'), v('Forward'), v('Up')))
            el.clear()
    if not planets:
        sys.exit(f'no planet with PlanetGenerator {gid} in {sbs}')
    if len(planets) > 1:
        print(f'WARN {len(planets)} planets use {gid}; using the first')
    R, corner, fwd, up = planets[0]
    size = 32   # MyVoxelCoordSystems.FindBestOctreeSize(2 * (R + R * HillParams.Max))
    while size < 2 * (R + R * hmax):
        size *= 2
    centre = [c + size / 2 for c in corner]   # MyVoxelBase: world translation = PositionMinCorner + storage.Size / 2
    out = os.path.abspath(A.out)
    rel = lambda p: os.path.relpath(os.path.abspath(p), os.path.dirname(out))
    cfg = {'generator': gid, 'heightmaps': rel(A.heightmaps), 'radius': R, 'hill_min': hmin, 'hill_max': hmax,
           'centre': centre, 'forward': fwd, 'up': up, 'water': None, 'protect': [], 'work_dir': 'terrain-edits'}
    if A.water_config:
        wr = ET.parse(A.water_config).getroot()
        pc = [p for p in wr.iter('PlanetConfig') if p.get('SubtypeId') == gid]
        if not pc:
            sys.exit(f'no PlanetConfig for {gid} in {A.water_config}')
        ws = pc[0].find('WaterSettings')
        A.water_ratio = float(ws.findtext('Radius'))
        A.wave = float(ws.findtext('WaveHeight') or 0); A.tide = float(ws.findtext('TideHeight') or 0)
    if A.water_ratio:   # Water Mod radius is a ratio of the lowest terrain radius, R * (1 + HillParams.Min)
        cfg['water'] = {'level_m': round(A.water_ratio * R * (1 + hmin) - R, 3), 'wave_m': A.wave, 'tide_m': A.tide,
                        'ratio': A.water_ratio}
    if not (np.allclose(fwd, [0, 0, -1]) and np.allclose(up, [0, 1, 0])):
        print('WARN the planet is rotated in the save; rotation handling is untested')
    json.dump(cfg, open(out, 'w', encoding='utf-8'), indent=1)
    print(f'wrote {out}\n  {gid}: radius {R}, hills {hmin * R:.1f} .. {hmax * R:.1f} m, centre {centre}'
          + (f", water {cfg['water']['level_m']} m (+{A.wave} wave, +{A.tide} tide)" if cfg['water'] else ', no water'))
    missing = [f for f in FACES if not os.path.exists(os.path.join(A.heightmaps, f + '.png'))]
    if missing:
        print('WARN missing heightmaps:', ', '.join(missing))
    print('Check: `info` on 3-4 GPS points taken standing on untouched ground; GPS-ground should be within ~0-2 m.')


# ---------------------------------------------------------------- cli
EDITS = {'flatten': cmd_flatten, 'nudge': cmd_nudge, 'plane': cmd_plane, 'smooth': cmd_smooth}


def parser():
    ap = argparse.ArgumentParser(prog='se_terrain.py', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--planet', default=os.environ.get('SE_TERRAIN_PLANET', 'planet.json'))
    ap.add_argument('--heightmaps', help='edit the faces in this folder instead (a copy, for testing)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('init', 'info', 'flatten', 'nudge', 'plane', 'smooth', 'batch', 'restore'):
        p = sub.add_parser(name)
        p.add_argument('--write', action='store_true'); p.add_argument('--out')
        if name in EDITS:
            p.add_argument('--water', action='store_true', help='also edit water cells')
            p.add_argument('--move-coast', action='store_true', help='allow changing land that touches water')
            p.add_argument('--no-protect', action='store_true', help='ignore planet.json protect zones')
            p.add_argument('--lower-only', action='store_true'); p.add_argument('--raise-only', action='store_true')
        if name == 'init':
            p.add_argument('--sbc', required=True); p.add_argument('--save', required=True)
            p.add_argument('--heightmaps', required=True, dest='heightmaps_init')
            p.add_argument('--generator'); p.add_argument('--water-config'); p.add_argument('--water-ratio', type=float)
            p.add_argument('--wave', type=float, default=0); p.add_argument('--tide', type=float, default=0)
        elif name == 'info':
            p.add_argument('gps', nargs='+')
        elif name == 'flatten':
            p.add_argument('--to', required=True); p.add_argument('--at', action='append', default=[])
            p.add_argument('--height', type=float); p.add_argument('--path', action='store_true')
            p.add_argument('--half', type=float, default=75); p.add_argument('--blend', type=float, default=350)
        elif name == 'nudge':
            p.add_argument('--at', nargs=2, action='append', required=True, metavar=('GPS', 'DZ'))
            p.add_argument('--half', type=float, default=40); p.add_argument('--blend', type=float, default=150)
        elif name == 'plane':
            p.add_argument('--at', nargs=2, action='append', required=True, metavar=('GPS', 'M'))
            p.add_argument('--half', type=float, default=15); p.add_argument('--blend', type=float, default=350)
        elif name == 'smooth':
            p.add_argument('--at', action='append', required=True); p.add_argument('--path', action='store_true')
            p.add_argument('--radius', type=float, default=150); p.add_argument('--strength', type=float, default=1.0)
        elif name == 'batch':
            p.add_argument('file')
        elif name == 'restore':
            p.add_argument('backup')
    return ap


def main(argv=None):
    ap = parser()
    A = ap.parse_args(argv)
    if A.cmd == 'init':
        A.heightmaps = A.heightmaps_init; A.out = A.out or A.planet
        return cmd_init(None, A)
    P = Planet(A.planet, A.heightmaps)
    if A.cmd == 'info':
        return cmd_info(P, A)
    if A.cmd == 'restore':
        return cmd_restore(P, A)
    if A.cmd in EDITS:
        return finish(P, A, [EDITS[A.cmd](P, A)])
    results = []   # batch: one op per line, '#' comments; --write/--out come from the batch command line
    for ln in open(A.file, encoding='utf-8'):
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        B = ap.parse_args(['--planet', A.planet] + shlex.split(ln, posix=True))
        if B.cmd not in EDITS:
            sys.exit(f'batch lines must be flatten/nudge/plane/smooth: {ln}')
        results.append(EDITS[B.cmd](P, B))
    finish(P, A, results)


if __name__ == '__main__':
    main()
