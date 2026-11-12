"""Pack world.json into world.pack.json for the game: the same coordinates (all on a 0.01 degree grid), stored as
integer deltas so the download is ~60% smaller. Lossless: the game rebuilds the exact GeoJSON (see unpackWorld in index.html).
Run after any change to world.json:  python3 scripts/pack_world.py"""
import json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def ring(r):
    out, px, py = [], 0, 0
    for x, y in r:
        ix, iy = round(x * 100), round(y * 100)
        assert abs(x * 100 - ix) < 1e-6 and abs(y * 100 - iy) < 1e-6, "coordinate not on the 0.01 grid"
        out += [ix - px, iy - py]; px, py = ix, iy
    return out

def geom(g):
    t, c = g["type"], g["coordinates"]
    if t == "Polygon": return {"t": "P", "c": [ring(r) for r in c]}
    if t == "MultiPolygon": return {"t": "M", "c": [[ring(r) for r in p] for p in c]}
    if t == "MultiLineString": return {"t": "L", "c": [ring(l) for l in c]}
    if t == "LineString": return {"t": "S", "c": ring(c)}
    raise ValueError(t)

def feat(f):
    o = {"g": geom(f["geometry"])}
    if f.get("properties"): o["p"] = f["properties"]
    return o

def pack(v):
    if v.get("type") == "FeatureCollection": return {"fc": [feat(f) for f in v["features"]]}
    if v.get("type") == "Feature": return {"f": feat(v)}
    return {"g": geom(v)}

w = json.load(open(os.path.join(ROOT, "world.json")))
s = json.dumps({k: pack(v) for k, v in w.items()}, separators=(",", ":"))
open(os.path.join(ROOT, "world.pack.json"), "w").write(s)
print("world.pack.json", len(s), "bytes")
