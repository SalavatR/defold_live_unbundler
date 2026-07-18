#!/usr/bin/env python3
"""Generate a self-contained HTML report about liveupdate collections.

Reads the highres and lowres ``manifest.json`` produced by ``liveupdate_pack.py``
(``file_versions`` / ``file_sizes`` / ``deps``) and the highres ``files_tree.json``
(per-archive file listing), then builds an interactive report:

  * overview: versions, total download size per resolution, archive kinds
  * charts: heaviest collections, download size by archive kind
  * collections table: standalone download size, exclusive vs shared bytes
  * overlap selector: pick a set of collections and see the deduplicated
    download size versus the naive sum, i.e. how much sharing saves
  * common archives: shared archives and which collections pull them
  * archive drill-down: every archive with its files (from files_tree)

Primary metric everywhere is the *download size* = compressed ``.arcd0`` bytes
from ``file_sizes``. The uncompressed content size and per-file listing come
from ``files_tree.json`` and are shown as secondary detail.

Usage:
    python3 tools/liveupdate_collections_report.py --version-dir dist/output/22.00
    python3 tools/liveupdate_collections_report.py \
        --highres dist/output/22.00/liveupdatehighres \
        --lowres  dist/output/22.00/liveupdatelowres \
        --out report.html
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_archive_protos(highres_dir):
    """Per-resource info from each .arcd0's embedded dmanifest protobuf.

    Returns {hex_digest: {"csize": compressed_size, "flags": ResourceEntryFlag}}
    or None when protobuf or the archives are unavailable (the report then simply
    omits the proto columns). The compressed size and flags exist only in the
    protobuf — the manifest/files_tree carry the uncompressed size only.
    """
    highres_dir = Path(highres_dir)
    archives = sorted(highres_dir.glob("*.arcd0"))
    if not archives:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import liveupdate_ddf_pb2 as pb
    except Exception:
        return None

    result = {}
    for arc in archives:
        try:
            with zipfile.ZipFile(arc) as z:
                data = z.read("liveupdate.game.dmanifest")
            manifest_file = pb.ManifestFile()
            manifest_file.ParseFromString(data)
            manifest_data = pb.ManifestData()
            manifest_data.ParseFromString(manifest_file.data)
        except Exception:
            continue
        for res in manifest_data.resources:
            digest = res.hash.data.hex()
            if digest and digest not in result:
                result[digest] = {
                    "csize": res.compressed_size,
                    "flags": res.flags,
                    # 64-bit dmHashString64 of the resource URL — this is what
                    # Defold runtime errors reference ("dependency ... from <hash>")
                    "uhash": "%016x" % res.url_hash,
                }
    return result or None


def parse_full_dmanifest(dmanifest_path):
    """Resources that are byte-identical (same content hash) but live under more
    than one URL, read from the full game dmanifest. Unlike the per-archive
    liveupdate dmanifests (collapsed by content hash), the full manifest lists
    every resource URL, so it exposes same-content/different-path duplicates.
    Returns a list of {hex, size, paths} sorted by size, or None if unavailable.
    """
    dmanifest_path = Path(dmanifest_path)
    if not dmanifest_path.exists():
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import liveupdate_ddf_pb2 as pb
        manifest_file = pb.ManifestFile()
        manifest_file.ParseFromString(dmanifest_path.read_bytes())
        manifest_data = pb.ManifestData()
        manifest_data.ParseFromString(manifest_file.data)
    except Exception:
        return None

    by_hash = {}
    for res in manifest_data.resources:
        digest = res.hash.data.hex()
        entry = by_hash.setdefault(digest, {"hex": digest, "size": res.size, "paths": set()})
        if res.url:
            entry["paths"].add(res.url)
    dups = [
        {"hex": e["hex"], "size": e["size"], "paths": sorted(e["paths"])}
        for e in by_hash.values()
        if len(e["paths"]) > 1
    ]
    dups.sort(key=lambda e: (-e["size"], e["hex"]))
    return dups


def archive_resource_hexes(directory):
    """Map content-hash digest -> owning archive name across a resolution's
    .arcd0 dmanifests, or None when protobuf / the archives are unavailable.
    Used to compare which resources are byte-identical between highres and lowres."""
    directory = Path(directory)
    archives = sorted(directory.glob("*.arcd0"))
    if not archives:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import liveupdate_ddf_pb2 as pb
    except Exception:
        return None
    result = {}
    for arc in archives:
        try:
            with zipfile.ZipFile(arc) as z:
                data = z.read("liveupdate.game.dmanifest")
            manifest_file = pb.ManifestFile()
            manifest_file.ParseFromString(data)
            manifest_data = pb.ManifestData()
            manifest_data.ParseFromString(manifest_file.data)
        except Exception:
            continue
        key = re.sub(r"_[0-9a-f]+\.arcd0$", "", arc.name)
        for res in manifest_data.resources:
            digest = res.hash.data.hex()
            if digest:
                result.setdefault(digest, key)
    return result


_COLL_TEXTURE_RE = re.compile(r"_texture(_[0-9a-f]+)?$")
_COLL_SOUND_RE = re.compile(r"_sound(_[0-9a-f]+)?$")


def classify(name, dep_keys=None):
    """Classify an archive name into one of the packer's archive kinds.

    ``common_*`` archives are recognized by prefix. The per-collection texture /
    sound archives are recognized by their ``_texture`` / ``_sound`` tail — but
    only among archives that are actually dependencies (``dep_keys``, the keys of
    the manifest ``deps`` map), so a collection base whose id happens to end in
    ``_texture`` / ``_sound`` is not misread as a derived archive. When
    ``dep_keys`` is not supplied the tail match is applied unconditionally, which
    is unambiguous only under the ``<id>.collectionc`` naming.
    """
    if name.startswith("common_texture_"):
        return "common_texture"
    if name.startswith("common_sound_"):
        return "common_sound"
    if name.startswith("common_"):
        return "common"
    is_dep = dep_keys is None or name in dep_keys
    if is_dep and _COLL_TEXTURE_RE.search(name):
        return "coll_texture"
    if is_dep and _COLL_SOUND_RE.search(name):
        return "coll_sound"
    return "coll_base"


_ADD_LOAD_ITEM = re.compile(
    r"add_load_item\(\s*(?P<key>[^,{]+?)\s*,\s*\{(?P<block>.*?)\}\s*,\s*"
    r"(?P<res>[\w.]+)\s*(?:,\s*(?P<byreq>true|false))?\s*\)",
    re.DOTALL,
)
_RES_MODE = {"RES_BOTH": "both", "RES_HIGH_ONLY": "high", "RES_LOW_ONLY": "low"}


def parse_lua_modules(lua_path):
    """Parse liveupdater_modules_util.lua into ready-made collection presets.

    Each add_load_item(key, {files}, res_mode[, by_request]) call becomes one
    preset. Modules whose file list is computed at runtime (e.g. the current /
    other locations, which use get_proxy_factory_by_id) carry no string literals
    and are skipped. Returns a list of {label, files, res, by_request}.
    """
    path = Path(lua_path)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    presets = []
    for m in _ADD_LOAD_ITEM.finditer(text):
        files = re.findall(r"""["']([^"']+\.collectionc)["']""", m.group("block"))
        if not files:
            continue
        key = m.group("key").strip()
        label = key.strip("\"'") if key[:1] in "\"'" else key.split(".")[0]
        presets.append({
            "label": label,
            "files": files,
            "res": _RES_MODE.get(m.group("res").rsplit(".", 1)[-1], m.group("res")),
            "by_request": m.group("byreq") == "true",
        })
    return presets


def normalize_file_sizes(manifest):
    """Return archive -> download size in bytes.

    Supports the current format (separate top-level ``file_sizes``) and tolerates
    a legacy inline ``{"version", "size"}`` shape inside ``file_versions``.
    """
    sizes = dict(manifest.get("file_sizes") or {})
    if not sizes:
        for name, info in (manifest.get("file_versions") or {}).items():
            if isinstance(info, dict) and "size" in info:
                sizes[name] = info["size"]
    return sizes


def build_model(highres_dir, lowres_dir, modules_lua=None, dmanifest_path=None):
    highres_dir = Path(highres_dir)
    lowres_dir = Path(lowres_dir) if lowres_dir else None

    high_manifest = load_json(highres_dir / "manifest.json")
    low_manifest = None
    if lowres_dir and (lowres_dir / "manifest.json").exists():
        low_manifest = load_json(lowres_dir / "manifest.json")

    files_tree = None
    files_tree_path = highres_dir / "files_tree.json"
    if files_tree_path.exists():
        files_tree = load_json(files_tree_path)

    file_versions = high_manifest.get("file_versions") or {}
    deps = high_manifest.get("deps") or {}
    dep_keys = set(deps)  # authoritative set of dependency archives (texture/sound/common)
    high_sizes = normalize_file_sizes(high_manifest)
    low_sizes = normalize_file_sizes(low_manifest) if low_manifest else {}
    zip_files = (files_tree or {}).get("zip_files", {})

    warnings = []
    if not high_sizes:
        warnings.append(
            "Highres manifest has no file_sizes — download sizes are unavailable. "
            "Rebuild with the updated liveupdate_pack.py."
        )
    if low_manifest is None:
        warnings.append("Lowres manifest not found — lowres sizes are unavailable.")
    elif not low_sizes:
        warnings.append("Lowres manifest has no file_sizes — lowres download sizes are unavailable.")
    if not zip_files:
        warnings.append("Highres files_tree.json not found — per-file archive contents are unavailable.")

    if low_manifest is not None:
        only_high = set(file_versions) - set(low_manifest.get("file_versions") or {})
        only_low = set(low_manifest.get("file_versions") or {}) - set(file_versions)
        if only_high or only_low:
            warnings.append(
                "Highres and lowres manifests list different archives "
                f"({len(only_high)} only in highres, {len(only_low)} only in lowres)."
            )

    presets = parse_lua_modules(modules_lua) if modules_lua else None
    if modules_lua and presets is None:
        warnings.append(f"Module presets source not found: {modules_lua}")

    proto_res = parse_archive_protos(highres_dir)
    if proto_res is None:
        warnings.append("Per-resource proto data unavailable (no .arcd0 archives or protobuf) — compressed size / flags omitted.")

    # archive -> record
    archives = {}
    for name in file_versions:
        tree = zip_files.get(name, {})
        files = []
        for fr in tree.get("files", []):
            digest = fr.get("hexDigest", "")
            rec = {"path": fr.get("path", ""), "size": fr.get("size", 0), "hex": digest}
            proto = proto_res.get(digest) if proto_res else None
            if proto:
                rec["csize"] = proto["csize"]
                rec["flags"] = proto["flags"]
                rec["uhash"] = proto["uhash"]
            files.append(rec)
        files.sort(key=lambda f: f["size"], reverse=True)
        consumers = sorted(deps.get(name, []))
        archives[name] = {
            "name": name,
            "kind": classify(name, dep_keys),
            "high": high_sizes.get(name, 0),
            "low": low_sizes.get(name, 0),
            "uncompressed": tree.get("size", 0),
            "consumers": consumers,
            "fanout": len(consumers),
            "files": files,
        }

    # collection -> archive set (base + its texture archives + shared commons)
    coll_names = [n for n in file_versions if classify(n) == "coll_base"]
    coll_to_archives = {c: set() for c in coll_names}
    for archive, cols in deps.items():
        for col in cols:
            coll_to_archives.setdefault(col, set()).add(archive)
    for col in coll_names:
        coll_to_archives[col].add(col)  # the collection's own base archive

    collections = {}
    for col in coll_names:
        arcs = sorted(coll_to_archives[col])
        # exclusive: only this collection pulls it (base always; deps fanout <= 1)
        exclusive = [a for a in arcs if a == col or archives[a]["fanout"] <= 1]
        shared = [a for a in arcs if a != col and archives[a]["fanout"] > 1]
        collections[col] = {
            "name": col,
            "archives": arcs,
            "exclusive": exclusive,
            "shared": shared,
            "high": sum(archives[a]["high"] for a in arcs),
            "low": sum(archives[a]["low"] for a in arcs),
            "high_exclusive": sum(archives[a]["high"] for a in exclusive),
            "high_shared": sum(archives[a]["high"] for a in shared),
            "low_exclusive": sum(archives[a]["low"] for a in exclusive),
            "low_shared": sum(archives[a]["low"] for a in shared),
            "uncompressed": sum(archives[a]["uncompressed"] for a in arcs),
        }

    # per-kind totals
    kinds = {}
    for rec in archives.values():
        bucket = kinds.setdefault(rec["kind"], {"count": 0, "high": 0, "low": 0})
        bucket["count"] += 1
        bucket["high"] += rec["high"]
        bucket["low"] += rec["low"]

    distinct_resources = set()
    for tree in zip_files.values():
        for fr in tree.get("files", []):
            if fr.get("hexDigest"):
                distinct_resources.add(fr["hexDigest"])

    # ---- duplicate analysis --------------------------------------------------
    hi_size = {}
    for rec in archives.values():
        for f in rec["files"]:
            hi_size[f["hex"]] = f["size"]

    # same content under >1 path — read from the full (uncollapsed) game dmanifest
    manifest_dups = parse_full_dmanifest(dmanifest_path) if dmanifest_path else None
    if dmanifest_path and manifest_dups is None:
        warnings.append(f"Full game dmanifest unavailable ({dmanifest_path}) — same-content/different-path duplicates not listed.")

    # cross-resolution: resources byte-identical in highres & lowres (stat only)
    low_hexes = archive_resource_hexes(lowres_dir) if lowres_dir else None
    cross = None
    if low_hexes is not None and distinct_resources:
        identical = distinct_resources & set(low_hexes)
        cross = {
            "identical": len(identical),
            "hi_total": len(distinct_resources),
            "bytes": sum(hi_size.get(h, 0) for h in identical),
        }

    totals = {
        "high_version": high_manifest.get("version"),
        "low_version": low_manifest.get("version") if low_manifest else None,
        "high_total": sum(high_sizes.values()),
        "low_total": sum(low_sizes.values()),
        "high_uncompressed_total": sum(a["uncompressed"] for a in archives.values()),
        "archive_count": len(file_versions),
        "collection_count": len(coll_names),
        "kinds": kinds,
        "distinct_resources": len(distinct_resources),
        "has_files_tree": bool(zip_files),
        "has_low": low_manifest is not None,
    }

    return {
        "totals": totals,
        "warnings": warnings,
        "archives": archives,
        "collections": collections,
        "presets": presets or [],
        "duplicates": {"manifest": manifest_dups, "cross": cross},
        "proto_meta": {
            "highres": high_manifest.get("dmanifest_info"),
            "lowres": low_manifest.get("dmanifest_info") if low_manifest else None,
        },
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Liveupdate collections report</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    margin: 0; background: #0f1115; color: #e6e6e6;
  }
  header { padding: 20px 28px; background: #151922; border-bottom: 1px solid #262c38; }
  h1 { margin: 0 0 4px; font-size: 20px; }
  h2 { font-size: 16px; margin: 28px 0 12px; color: #fff; }
  .sub { color: #9aa4b2; font-size: 13px; }
  main { padding: 20px 28px 80px; max-width: 1400px; }
  .warn { background: #3a2410; border: 1px solid #6b4415; color: #ffd39b;
          padding: 8px 12px; border-radius: 6px; margin: 8px 0; font-size: 13px; }
  .cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 8px 0 4px; }
  .card { background: #161b24; border: 1px solid #262c38; border-radius: 8px;
          padding: 12px 16px; min-width: 150px; }
  .card .v { font-size: 22px; font-weight: 700; color: #fff; }
  .card .l { font-size: 12px; color: #9aa4b2; margin-top: 2px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  @media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { border-bottom: 1px solid #262c38; padding: 6px 10px; text-align: left; }
  th { background: #161b24; position: sticky; top: 0; cursor: pointer; user-select: none; white-space: nowrap; }
  th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tbody tr:hover { background: #161b24; }
  .kind { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; }
  .kind.common, .kind.common_texture { background: #1c3a24; color: #8ce8a4; }
  .kind.common_sound, .kind.coll_sound { background: #2a1c4a; color: #c9a8f5; }
  .kind.coll_base { background: #1c2c4a; color: #93bdf5; }
  .kind.coll_texture { background: #3a2a12; color: #f5c98c; }
  .kind.other { background: #333; color: #ccc; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 12px; }
  .bar-label { width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-track { flex: 1; background: #161b24; border-radius: 3px; height: 16px; position: relative; }
  .bar-fill { height: 100%; border-radius: 3px; }
  .bar-fill.high { background: #3d6bd6; }
  .bar-fill.low { background: #2f9e5f; }
  .bar-track.stack { display: flex; gap: 2px; }
  .seg { height: 100%; border-radius: 3px; min-width: 0; }
  .seg.exc { background: #4c8dff; }
  .seg.shr { background: #7a8494; }
  .seg.lo { opacity: 0.72; }
  .res-row { display: flex; align-items: center; gap: 6px; }
  .res-row + .res-row { margin-top: 2px; }
  .res-tag { width: 16px; font-size: 10px; color: #7b8494; text-align: right; flex: none; }
  .bar-val { width: 108px; text-align: right; color: #cbd3df; font-variant-numeric: tabular-nums; }
  .legend { font-size: 12px; color: #9aa4b2; margin-bottom: 6px; }
  .legend b.high { color: #6a95ee; } .legend b.low { color: #4fca8a; }
  .legend b.exc { color: #4c8dff; } .legend b.shr { color: #7a8494; }
  input[type=search] { background: #0f1115; border: 1px solid #2c3444; color: #e6e6e6;
                       padding: 6px 10px; border-radius: 6px; width: 320px; }
  textarea { width: 100%; min-height: 90px; background: #0f1115; border: 1px solid #2c3444;
             color: #e6e6e6; padding: 8px 10px; border-radius: 6px; font-family: monospace;
             font-size: 12px; margin: 6px 0; resize: vertical; }
  .selector { background: #161b24; border: 1px solid #262c38; border-radius: 8px; padding: 12px 16px; }
  .checks { max-height: 260px; overflow: auto; columns: 3; column-gap: 24px; margin-top: 8px; }
  @media (max-width: 900px) { .checks { columns: 1; } }
  .checks label { display: block; font-size: 12px; padding: 2px 0; break-inside: avoid; cursor: pointer; }
  .result-cards { margin-top: 14px; }
  details { border-top: 1px solid #1d232e; }
  details > summary { cursor: pointer; padding: 6px 0; font-size: 13px; }
  .files { width: 100%; margin: 4px 0 8px; font-size: 12px; color: #b6bdc9; }
  .files th { position: static; background: none; cursor: default; color: #7b8494; font-weight: 400; font-size: 11px; }
  .files td { border-bottom: 1px solid #1a1f28; padding: 3px 8px; }
  .files .hex { font-family: monospace; font-size: 11px; color: #7b8494; word-break: break-all; }
  .flag { display: inline-block; padding: 0 6px; border-radius: 8px; font-size: 10px; background: #2a3342; color: #9fb2cc; }
  .flag.compressed { background: #1c3a4a; color: #8cd0e8; }
  .flag.encrypted { background: #4a2422; color: #f0a8a0; }
  .flag.excluded { background: #333; color: #aaa; }
  .flag.bundled { background: #1c3a24; color: #8ce8a4; }
  .mono { font-family: monospace; font-size: 11px; word-break: break-all; }
  mark { background: #7a5f12; color: #ffe9a8; border-radius: 2px; padding: 0 1px; }
  .ok-note { background: #16261a; border: 1px solid #24502f; color: #8ce8a4; padding: 8px 12px; border-radius: 6px; font-size: 13px; }
  .arch-row.expandable { cursor: pointer; }
  .files-cell .caret { display: inline-block; color: #7b8494; transition: transform .1s; }
  .arch-row.open .caret { transform: rotate(90deg); }
  .arch-detail > td { padding: 2px 12px 10px 24px; background: #12161d; }
  .muted { color: #7b8494; }
  button { background: #253048; color: #dfe7f5; border: 1px solid #34405c;
           padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  button:hover { background: #2c3a56; }
  .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
  .presets { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 12px; }
  .presets button { padding: 4px 9px; font-size: 12px; }
  .presets button .cnt { color: #8aa0c0; margin-left: 4px; }
  .presets button.byreq { border-style: dashed; }
  .presets button.on { background: #2e5aa8; border-color: #5a8fd8; color: #fff; }
  .presets button.on .cnt { color: #cfe0ff; }
  .presets button.partial { border-color: #5a8fd8; }
  .presets button.empty { opacity: 0.4; cursor: default; }
</style>
</head>
<body>
<header>
  <h1>Liveupdate collections report</h1>
  <div class="sub" id="subtitle"></div>
</header>
<main>
  <div id="warnings"></div>

  <section>
    <div class="cards" id="overview-cards"></div>
  </section>

  <section class="grid2">
    <div>
      <h2>Heaviest collections <span class="sub">(by exclusive highres size)</span></h2>
      <div class="legend"><b class="exc">■ exclusive</b> — only this collection needs it&nbsp;&nbsp;<b class="shr">■ shared</b> — common, downloaded once &amp; usually already loaded&nbsp;&nbsp;<span class="muted">· rows: hi = highres, lo = lowres</span></div>
      <div id="chart-collections"></div>
    </div>
    <div>
      <h2>Download size by archive kind</h2>
      <div class="legend">download size&nbsp; <b class="high">■ highres</b>&nbsp; <b class="low">■ lowres</b></div>
      <div id="chart-kinds"></div>
    </div>
  </section>

  <section>
    <h2>Overlap explorer <span class="sub">(pick collections — shared archives are counted once)</span></h2>
    <div class="selector">
      <div id="sel-presets-wrap" style="display:none">
        <div class="legend">Module presets <span class="muted">— from liveupdater_modules_util.lua, click to add a module's collections</span></div>
        <div class="presets" id="sel-presets"></div>
      </div>
      <details style="margin:10px 0">
        <summary>Paste a file list (e.g. a module's collectionc list) to toggle those collections</summary>
        <textarea id="sel-paste" placeholder='e.g.
"lobby_scene.collectionc",
"settings_window.collectionc",
offer_window.collectionc'></textarea>
        <div class="toolbar">
          <button id="sel-paste-add">Add from list</button>
          <button id="sel-paste-set">Replace selection</button>
          <span class="muted" id="sel-paste-status"></span>
        </div>
      </details>
      <div class="toolbar">
        <button id="sel-none">Clear</button>
        <input type="search" id="sel-filter" placeholder="filter collections…" />
        <span class="muted" id="sel-count"></span>
      </div>
      <div class="checks" id="sel-checks"></div>
      <div class="result-cards cards" id="sel-result"></div>
      <div id="sel-shared"></div>
      <div id="sel-own"></div>
    </div>
  </section>

  <section>
    <h2>All archives</h2>
    <div class="toolbar">
      <div class="presets" id="arch-kinds"></div>
      <input type="search" id="arch-filter" placeholder="filter by archive / file path / content or url hash…" />
      <span class="muted" id="arch-count"></span>
    </div>
    <table id="arch-table">
      <thead><tr>
        <th data-sort="name">Archive</th>
        <th data-sort="kind">Kind</th>
        <th data-sort="fanout" class="num">Used by</th>
        <th data-sort="high" class="num">Highres</th>
        <th data-sort="low" class="num">Lowres</th>
        <th data-sort="uncompressed" class="num">Content</th>
        <th data-sort="fileCount" class="num">Files</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </section>

  <section>
    <h2>Duplicates</h2>
    <div id="dup-manifest"></div>
    <div id="dup-cross"></div>
  </section>

  <section>
    <h2>Manifest metadata <span class="sub">(from the dmanifest protobuf)</span></h2>
    <div id="proto-meta"></div>
  </section>
</main>

<script>
const DATA = __DATA__;
const A = DATA.archives, C = DATA.collections, T = DATA.totals;
const collList = Object.values(C);
const archList = Object.values(A);

function mb(bytes) {
  if (!bytes) return "0";
  return (bytes / 1048576).toFixed(bytes < 1048576 ? 2 : 1);
}
function human(bytes) {
  let s = Number(bytes || 0);
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (s < 1024) return s.toFixed(s < 10 && u !== "B" ? 2 : 0) + " " + u;
    s /= 1024;
  }
  return s.toFixed(2) + " PB";
}
function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function el(id) { return document.getElementById(id); }
// escape text, then wrap case-insensitive matches of the (non-empty) query in <mark>
function hl(text, q) {
  const s = esc(text);
  if (!q) return s;
  const needle = esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return s.replace(new RegExp(needle, "gi"), m => "<mark>" + m + "</mark>");
}

// ResourceEntryFlag bits from the dmanifest proto
const FLAG_BITS = [[8, "COMPRESSED"], [4, "ENCRYPTED"], [2, "EXCLUDED"], [1, "BUNDLED"]];
function flagNames(f) { return FLAG_BITS.filter(([b]) => f & b).map(([, n]) => n); }
const HAS_PROTO = archList.some(a => a.files.some(f => f.csize != null));

// Full file listing table for an archive. When per-resource proto data is
// available it adds compressed size + flags (compressed size exists only in the
// dmanifest protobuf; the manifest/files_tree carry the uncompressed size only).
function fileTableHtml(rec, q) {
  const head = '<tr><th>File</th><th>Content hash</th>' +
    (HAS_PROTO ? '<th>URL hash</th>' : "") + '<th class="num">Size</th>' +
    (HAS_PROTO ? '<th class="num">Compressed</th><th>Flags</th>' : "") + '</tr>';
  const rows = rec.files.map(f => '<tr><td>' + hl(f.path, q) + '</td><td class="hex">' + hl(f.hex || "", q) + '</td>' +
    (HAS_PROTO ? '<td class="hex">' + hl(f.uhash || "", q) + '</td>' : "") +
    '<td class="num">' + human(f.size) + '</td>' +
    (HAS_PROTO
      ? '<td class="num">' + (f.csize != null ? human(f.csize) : "—") + '</td>' +
        '<td>' + (f.flags != null ? flagNames(f.flags).map(n => '<span class="flag ' + n.toLowerCase() + '">' + n + '</span>').join(" ") : "") + '</td>'
      : "") +
    '</tr>').join("");
  return '<table class="files"><thead>' + head + '</thead><tbody>' + rows + '</tbody></table>';
}

// Expandable file listing for an archive record. Pass summaryHtml to override
// the (already-escaped) summary text; otherwise a "N files · content" summary is used.
function fileDetails(rec, summaryHtml) {
  const summary = summaryHtml != null
    ? summaryHtml
    : (rec.files.length + " files · content " + human(rec.uncompressed));
  if (!rec.files.length) {
    return summaryHtml != null
      ? summaryHtml + ' <span class="muted">(no file listing)</span>'
      : '<span class="muted">no file listing</span>';
  }
  return '<details><summary>' + summary + '</summary>' + fileTableHtml(rec) + '</details>';
}

// ---- subtitle + warnings + overview ---------------------------------------
el("subtitle").textContent =
  "highres v" + (T.high_version ?? "?") + (T.has_low ? "  ·  lowres v" + (T.low_version ?? "?") : "") +
  "  ·  " + T.archive_count + " archives  ·  " + T.collection_count + " collections";

el("warnings").innerHTML = (DATA.warnings || [])
  .map(w => '<div class="warn">' + esc(w) + '</div>').join("");

el("overview-cards").innerHTML = [
  ["Highres download", human(T.high_total)],
  ["Lowres download", human(T.low_total)],
  ["Highres content (uncompressed)", human(T.high_uncompressed_total)],
  ["Collections", T.collection_count],
  ["Archives", T.archive_count],
  ["Distinct resources", T.distinct_resources || "—"],
].map(([l, v]) => '<div class="card"><div class="v">' + v + '</div><div class="l">' + l + '</div></div>').join("");

// ---- charts ----------------------------------------------------------------
function barChart(target, rows, max) {
  target.innerHTML = rows.map(r => {
    const wHigh = max ? (r.high / max * 100) : 0;
    const wLow = max ? (r.low / max * 100) : 0;
    return '<div class="bar-row"><div class="bar-label" title="' + esc(r.label) + '">' + esc(r.label) + '</div>' +
      '<div style="flex:1">' +
        '<div class="bar-track"><div class="bar-fill high" style="width:' + wHigh + '%"></div></div>' +
        '<div class="bar-track" style="margin-top:2px"><div class="bar-fill low" style="width:' + wLow + '%"></div></div>' +
      '</div>' +
      '<div class="bar-val">' + mb(r.high) + " / " + mb(r.low) + ' MB</div></div>';
  }).join("");
}

// Collections ranked by EXCLUSIVE download size — the bytes only this collection
// pulls. Shared common archives (grey) are counted once across the game and are
// usually already downloaded by the time a given window opens, so ranking by the
// standalone total (which is dominated by shared) is misleading. Each bar is
// exclusive (bright, anchored left so bars are directly comparable) + shared
// (grey), scaled to the largest standalone total so the "real cost" reads as a
// slice of the whole.
function stackedCollChart(target, rows, max) {
  const w = v => (max ? v / max * 100 : 0);
  const resRow = (tag, lo, exc, shr, label) =>
    '<div class="res-row"><span class="res-tag">' + tag + '</span>' +
    '<div class="bar-track stack" style="flex:1">' +
      '<div class="seg exc' + lo + '" style="width:' + w(exc) + '%" title="' + esc(label + " — " + tag + " exclusive " + human(exc)) + '"></div>' +
      '<div class="seg shr' + lo + '" style="width:' + w(shr) + '%" title="' + esc(label + " — " + tag + " shared " + human(shr)) + '"></div>' +
    '</div></div>';
  target.innerHTML = rows.map(r =>
    '<div class="bar-row">' +
      '<div class="bar-label" title="' + esc(r.label) + '">' + esc(r.label) + '</div>' +
      '<div style="flex:1">' +
        resRow("hi", "", r.hiExc, r.hiShr, r.label) +
        resRow("lo", " lo", r.loExc, r.loShr, r.label) +
      '</div>' +
      '<div class="bar-val">' + mb(r.hiExc) + '<span class="muted"> +' + mb(r.hiShr) + '</span></div>' +
    '</div>').join("");
}

const topColls = collList.slice().sort((a, b) => b.high_exclusive - a.high_exclusive).slice(0, 15)
  .map(c => ({ label: c.name.replace(/\.collectionc$/, ""),
    hiExc: c.high_exclusive, hiShr: c.high_shared, loExc: c.low_exclusive, loShr: c.low_shared }));
stackedCollChart(el("chart-collections"), topColls, Math.max(1, ...topColls.map(r => r.hiExc + r.hiShr)));

const kindOrder = ["coll_texture", "coll_sound", "common_texture", "common_sound", "common", "coll_base", "other"];
const kindLabels = { coll_texture: "Collection textures", coll_sound: "Collection sounds",
  common_texture: "Common textures", common_sound: "Common sounds",
  common: "Common data", coll_base: "Collection base", other: "Other" };
const kindRows = kindOrder.filter(k => T.kinds[k]).map(k => ({
  label: kindLabels[k] + " (" + T.kinds[k].count + ")", high: T.kinds[k].high, low: T.kinds[k].low }));
barChart(el("chart-kinds"), kindRows, Math.max(1, ...kindRows.map(r => r.high)));

// ---- sortable table helper -------------------------------------------------
function makeTable(tableId, rows, cols, opts) {
  opts = opts || {};
  const tbody = el(tableId).querySelector("tbody");
  const ths = el(tableId).querySelectorAll("th[data-sort]");
  let sortKey = opts.sortKey, sortDir = -1, filter = "";

  function apply() {
    let r = rows;
    if (opts.prefilter) r = r.filter(opts.prefilter);
    if (filter) {
      const f = filter.toLowerCase();
      r = r.filter(row => opts.match(row, f));
    }
    r = r.slice().sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      if (Array.isArray(va)) va = va.length;
      if (Array.isArray(vb)) vb = vb.length;
      const cmp = typeof va === "string" ? va.localeCompare(vb) : (va - vb);
      return cmp * sortDir;
    });
    tbody.innerHTML = r.map(row => opts.render(row, filter)).join("") || '<tr><td colspan="9" class="muted">Nothing matches</td></tr>';
    if (opts.afterRender) opts.afterRender(r);
  }
  ths.forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = (k === "name" || k === "kind") ? 1 : -1; }
    apply();
  }));
  apply();
  return { setFilter(v) { filter = v; apply(); }, refresh: apply };
}

// ---- all archives table: kind toggles + search + file drill-down ------------
const KINDS = ["coll_base", "coll_texture", "coll_sound", "common", "common_texture", "common_sound"];
const KIND_LABELS = { coll_base: "collections", coll_texture: "collection textures",
  coll_sound: "collection sounds", common: "common data", common_texture: "common textures",
  common_sound: "common sounds" };
const activeKinds = new Set(KINDS);
const allArchives = archList.map(a => ({ ...a, fileCount: a.files.length }));

const archTable = makeTable("arch-table", allArchives, null, {
  sortKey: "high",
  prefilter: row => activeKinds.has(row.kind),
  match: (row, f) => row.name.toLowerCase().includes(f) ||
    row.files.some(fl => fl.path.toLowerCase().includes(f) ||
      (fl.hex && fl.hex.toLowerCase().includes(f)) || (fl.uhash && fl.uhash.includes(f))),
  render: (r, q) => {
    const has = r.files.length > 0;
    const ql = (q || "").toLowerCase();
    // auto-expand when the match is inside the files (e.g. a hash) rather than the archive name
    const fileMatch = ql && r.files.some(fl => fl.path.toLowerCase().includes(ql) ||
      (fl.hex && fl.hex.toLowerCase().includes(ql)) || (fl.uhash && fl.uhash.includes(ql)));
    const open = fileMatch && !r.name.toLowerCase().includes(ql);
    const cells = '<td>' + hl(r.name, q) + '</td>' +
      '<td><span class="kind ' + r.kind + '">' + r.kind + '</span></td>' +
      '<td class="num"' + (r.consumers.length ? ' title="' + esc(r.consumers.join(", ")) + '"' : "") + '>' +
        (r.fanout || "—") + '</td>' +
      '<td class="num">' + mb(r.high) + '</td>' +
      '<td class="num">' + mb(r.low) + '</td>' +
      '<td class="num">' + mb(r.uncompressed) + '</td>' +
      '<td class="files-cell">' + (has ? '<span class="caret">▸</span> ' + r.files.length + " files" : '<span class="muted">—</span>') + '</td>';
    // the file listing lives in its own full-width row below, toggled by the click handler
    const detail = has ? '<tr class="arch-detail"' + (open ? "" : " hidden") + '><td colspan="7">' + fileTableHtml(r, q) + '</td></tr>' : "";
    return '<tr class="arch-row' + (has ? " expandable" : "") + (open ? " open" : "") + '">' + cells + '</tr>' + detail;
  },
  afterRender: shown => {
    el("arch-count").textContent = shown.length + " / " + allArchives.length + " archives";
    el("arch-table").querySelectorAll("tr.arch-row.expandable").forEach(row =>
      row.addEventListener("click", () => {
        const d = row.nextElementSibling;
        if (d && d.classList.contains("arch-detail")) {
          d.hidden = !d.hidden;
          row.classList.toggle("open");
        }
      }));
  },
});
el("arch-filter").addEventListener("input", e => archTable.setFilter(e.target.value));

function renderKindToggles() {
  const counts = {};
  allArchives.forEach(a => { counts[a.kind] = (counts[a.kind] || 0) + 1; });
  el("arch-kinds").innerHTML = KINDS.filter(k => counts[k]).map(k =>
    '<button data-k="' + k + '" class="' + (activeKinds.has(k) ? "on" : "") + '">' +
    KIND_LABELS[k] + '<span class="cnt">' + counts[k] + '</span></button>').join("");
  el("arch-kinds").querySelectorAll("button").forEach(btn => btn.addEventListener("click", () => {
    const k = btn.dataset.k;
    if (activeKinds.has(k)) activeKinds.delete(k); else activeKinds.add(k);
    renderKindToggles();
    archTable.refresh();
  }));
}
renderKindToggles();

// ---- overlap explorer ------------------------------------------------------
const selected = new Set();

function renderChecks(filter) {
  const f = (filter || "").toLowerCase();
  const rows = collList.filter(c => !f || c.name.toLowerCase().includes(f))
    .sort((a, b) => a.name.localeCompare(b.name));
  el("sel-checks").innerHTML = rows.map(c =>
    '<label><input type="checkbox" value="' + esc(c.name) + '"' + (selected.has(c.name) ? " checked" : "") +
    '> ' + esc(c.name.replace(/\.collectionc$/, "")) + ' <span class="muted">' + mb(c.high) + '</span></label>'
  ).join("");
  el("sel-checks").querySelectorAll("input").forEach(cb =>
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
      recompute();
    }));
}

function recompute() {
  el("sel-count").textContent = selected.size + " selected";
  const union = new Set();
  for (const name of selected) {
    const c = C[name];
    if (!c) continue;
    c.archives.forEach(a => union.add(a));
  }
  let uHigh = 0, uLow = 0;
  union.forEach(a => { uHigh += A[a].high; uLow += A[a].low; });

  el("sel-result").innerHTML = selected.size ? [
    ["Download", human(uHigh) + " / " + human(uLow), "hi / lo"],
    ["Archives", union.size, "unique"],
  ].map(([l, v, note]) =>
    '<div class="card"><div class="v">' + v +
    '</div><div class="l">' + l + (note ? ' <span class="muted">' + note + '</span>' : "") + '</div></div>'
  ).join("") : '<div class="muted">Select collections to see the deduplicated download size.</div>';

  // split the union: archives shared by >1 selected collection vs pulled by just one
  if (selected.size >= 1) {
    const usedBy = {};
    for (const name of selected) {
      for (const a of C[name].archives) (usedBy[a] = usedBy[a] || []).push(name);
    }
    const entries = Object.entries(usedBy);
    const hint = '<span class="muted" style="font-weight:400">— click an archive to see its files</span>';

    const shared = entries.filter(([, cols]) => cols.length > 1)
      .map(([a, cols]) => ({ a, cols, high: A[a].high, low: A[a].low }))
      .sort((x, y) => y.high - x.high);
    el("sel-shared").innerHTML = shared.length
      ? '<h3 style="font-size:14px;margin:14px 0 6px">Shared archives (' + shared.length + ') ' + hint + '</h3>' +
        '<table><thead><tr><th>Archive</th><th class="num">Highres</th><th class="num">Lowres</th><th class="num">Used by</th><th>Collections</th></tr></thead><tbody>' +
        shared.map(s => '<tr><td>' + fileDetails(A[s.a], esc(s.a)) + '</td><td class="num">' + mb(s.high) +
          '</td><td class="num">' + mb(s.low) + '</td><td class="num">' + s.cols.length + '</td><td class="muted">' +
          esc(s.cols.map(c => c.replace(/\.collectionc$/, "")).join(", ")) + '</td></tr>').join("") +
        '</tbody></table>'
      : (selected.size >= 2 ? '<div class="muted" style="margin-top:10px">No archives are shared between the selected collections.</div>' : "");

    const own = entries.filter(([, cols]) => cols.length === 1)
      .map(([a, cols]) => ({ a, col: cols[0], high: A[a].high, low: A[a].low }))
      .sort((x, y) => y.high - x.high);
    el("sel-own").innerHTML = own.length
      ? '<h3 style="font-size:14px;margin:14px 0 6px">Own archives (' + own.length + ') ' +
        '<span class="muted" style="font-weight:400">— pulled by a single selected collection, click to see files</span></h3>' +
        '<table><thead><tr><th>Archive</th><th>Kind</th><th class="num">Highres</th><th class="num">Lowres</th><th>Collection</th></tr></thead><tbody>' +
        own.map(s => '<tr><td>' + fileDetails(A[s.a], esc(s.a)) +
          '</td><td><span class="kind ' + A[s.a].kind + '">' + A[s.a].kind + '</span></td><td class="num">' + mb(s.high) +
          '</td><td class="num">' + mb(s.low) + '</td><td class="muted">' +
          esc(s.col.replace(/\.collectionc$/, "")) + '</td></tr>').join("") +
        '</tbody></table>'
      : "";
  } else {
    el("sel-shared").innerHTML = "";
    el("sel-own").innerHTML = "";
  }
}

el("sel-filter").addEventListener("input", e => renderChecks(e.target.value));
el("sel-none").addEventListener("click", () => { selected.clear(); refreshSelection(); });

function refreshSelection() { renderPresets(); renderChecks(el("sel-filter").value); recompute(); }

// ---- select collections from a pasted / preset file list -------------------
// Adds every listed <name>.collectionc that exists as a collection; returns
// {matched, missing} so the caller can report unknown names.
function selectFiles(files, replace) {
  if (replace) selected.clear();
  let matched = 0; const missing = [];
  for (const f of files) {
    if (C[f]) { selected.add(f); matched++; }
    else missing.push(f);
  }
  refreshSelection();
  return { matched, missing };
}
function parseFileTokens(text) {
  const seen = new Set();
  for (const m of text.matchAll(/[A-Za-z0-9_./-]+\.collectionc/g)) seen.add(m[0].split("/").pop());
  return [...seen];
}

// module presets parsed from liveupdater_modules_util.lua — multi-select toggles
const hasPresets = DATA.presets && DATA.presets.length > 0;

// preset state derived from the current selection (robust to manual checkbox edits):
// "on" = all its present collections selected, "partial" = some, "off" = none.
function presetState(p) {
  const present = p.files.filter(f => C[f]);
  if (!present.length) return "empty";
  const sel = present.filter(f => selected.has(f)).length;
  return sel === 0 ? "off" : sel === present.length ? "on" : "partial";
}

function togglePreset(p) {
  const present = p.files.filter(f => C[f]);
  if (!present.length) return;
  if (present.every(f => selected.has(f))) {
    // toggle off, but keep collections still covered by another active preset
    const keep = new Set();
    for (const q of DATA.presets) {
      if (q === p) continue;
      const pr = q.files.filter(f => C[f]);
      if (pr.length && pr.every(f => selected.has(f))) pr.forEach(f => keep.add(f));
    }
    present.forEach(f => { if (!keep.has(f)) selected.delete(f); });
  } else {
    present.forEach(f => selected.add(f));
  }
  refreshSelection();
}

function renderPresets() {
  if (!hasPresets) return;
  el("sel-presets").innerHTML = DATA.presets.map((p, i) => {
    const st = presetState(p);
    const present = p.files.filter(f => C[f]).length;
    const cls = "preset " + st + (p.by_request ? " byreq" : "");
    const title = p.res.toUpperCase() + (p.by_request ? ", by request" : "") + "\n" + p.files.join("\n");
    return '<button class="' + cls + '" data-i="' + i + '" title="' + esc(title) + '">' +
      esc(p.label) + '<span class="cnt">' + present + "/" + p.files.length + '</span></button>';
  }).join("");
  el("sel-presets").querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => togglePreset(DATA.presets[+btn.dataset.i])));
}

if (hasPresets) el("sel-presets-wrap").style.display = "";

function pasteStatus(res) {
  el("sel-paste-status").textContent =
    res.matched + " added" + (res.missing.length ? ", " + res.missing.length + " not found: " + res.missing.join(", ") : "");
}
el("sel-paste-add").addEventListener("click", () => pasteStatus(selectFiles(parseFileTokens(el("sel-paste").value), false)));
el("sel-paste-set").addEventListener("click", () => pasteStatus(selectFiles(parseFileTokens(el("sel-paste").value), true)));

refreshSelection();

// ---- duplicates ------------------------------------------------------------
(function renderDuplicates() {
  const d = DATA.duplicates || {};

  // same content under different paths (byte-identical resources), from the full dmanifest
  const md = d.manifest;
  if (md == null) {
    el("dup-manifest").innerHTML = '<div class="muted">Same-content/different-path check unavailable (full game dmanifest not found).</div>';
  } else if (!md.length) {
    el("dup-manifest").innerHTML = '<div class="ok-note">✓ No byte-identical resources under different paths in the game manifest.</div>';
  } else {
    el("dup-manifest").innerHTML =
      '<h3 style="font-size:14px;margin:0 0 6px">Byte-identical resources under different paths (' + md.length + ') ' +
      '<span class="muted" style="font-weight:400">— same content hash, distinct URLs in the game dmanifest</span></h3>' +
      '<table class="files"><thead><tr><th>Content hash</th><th class="num">Size</th><th>Paths</th></tr></thead><tbody>' +
      md.map(g => '<tr><td class="hex">' + esc(g.hex) + '</td><td class="num">' + human(g.size) + '</td>' +
        '<td class="mono">' + g.paths.map(esc).join("<br>") + '</td></tr>').join("") +
      '</tbody></table>';
  }

  const c = d.cross;
  el("dup-cross").innerHTML = c
    ? '<div class="legend"><b>' + c.identical.toLocaleString() + '</b> of ' + c.hi_total.toLocaleString() +
      ' resources (' + Math.round(c.identical / c.hi_total * 100) + '%) are byte-identical in highres &amp; lowres — ' +
      human(c.bytes) + ' of resolution-independent content shared between the two builds.</div>'
    : '<div class="muted">Cross-resolution comparison unavailable (no lowres archives / protobuf).</div>';
})();

// ---- dmanifest (proto) metadata --------------------------------------------
(function renderProtoMeta() {
  const pm = DATA.proto_meta || {};
  const hi = pm.highres, lo = pm.lowres;
  if (!hi && !lo) { el("proto-meta").innerHTML = '<div class="muted">No dmanifest metadata in the manifest.</div>'; return; }
  const rows = [
    ["Manifest version", i => i.version],
    ["Archive identifier", i => i.archive_identifier || "—"],
    ["Signature", i => i.signature ? i.signature : "(empty / unsigned)"],
    ["Resource hash algorithm", i => (i.header || {}).resource_hash_algorithm],
    ["Signature hash algorithm", i => (i.header || {}).signature_hash_algorithm],
    ["Sign algorithm", i => (i.header || {}).signature_sign_algorithm],
    ["Project identifier", i => (i.header || {}).project_identifier],
    ["Engine versions", i => (i.engine_versions || []).join(", ")],
  ];
  const cell = (info, fn) => info ? esc(String(fn(info) ?? "—")) : "—";
  el("proto-meta").innerHTML =
    '<table style="max-width:900px"><thead><tr><th>Field</th><th>Highres</th>' + (lo ? '<th>Lowres</th>' : "") + '</tr></thead><tbody>' +
    rows.map(([label, fn]) => '<tr><td>' + label + '</td><td class="mono">' + cell(hi, fn) + '</td>' +
      (lo ? '<td class="mono">' + cell(lo, fn) + '</td>' : "") + '</tr>').join("") +
    '</tbody></table>';
})();
</script>
</body>
</html>
"""


def render_html(model):
    data_json = json.dumps(model, separators=(",", ":"), ensure_ascii=False)
    # guard against the closing script tag appearing inside embedded strings
    data_json = data_json.replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA__", data_json)


def resolve_dirs(args):
    if args.version_dir:
        base = Path(args.version_dir)
        return base / "liveupdatehighres", base / "liveupdatelowres"
    if not args.highres:
        raise SystemExit("Provide --version-dir or --highres/--lowres")
    return Path(args.highres), Path(args.lowres) if args.lowres else None


def default_out(args, highres_dir):
    if args.out:
        return Path(args.out)
    if args.version_dir:
        return Path(args.version_dir) / "liveupdate_collections_report.html"
    return Path(highres_dir).parent / "liveupdate_collections_report.html"


def latest_version_dir(root="dist/output"):
    root = Path(root)
    if not root.exists():
        return None
    versions = [p for p in root.iterdir() if p.is_dir() and (p / "liveupdatehighres").exists()]
    if not versions:
        return None

    def key(p):
        return [int(x) for x in re.findall(r"\d+", p.name)] or [0]

    return max(versions, key=key)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version-dir", help="dir containing liveupdatehighres/ and liveupdatelowres/")
    parser.add_argument("--highres", help="highres liveupdate dir (overrides --version-dir)")
    parser.add_argument("--lowres", help="lowres liveupdate dir")
    parser.add_argument("--out", help="output HTML path")
    parser.add_argument(
        "--modules-lua",
        default="",
        help=(
            "Lua module file to build collection presets from. Point it at a file "
            "with add_load_item(key, {\"<name>.collectionc\", ...}, res_mode[, by_request]) "
            "calls to get per-module preset buttons in the report. Empty (default) "
            "disables presets."
        ),
    )
    parser.add_argument(
        "--dmanifest",
        default="build/default/game.dmanifest",
        help="full game dmanifest for same-content/different-path duplicate detection (empty to disable)",
    )
    args = parser.parse_args()

    if not args.version_dir and not args.highres:
        auto = latest_version_dir()
        if auto:
            args.version_dir = str(auto)
            print(f"Auto-selected latest version dir: {auto}")
        else:
            raise SystemExit("No --version-dir/--highres given and none found under dist/output")

    highres_dir, lowres_dir = resolve_dirs(args)
    model = build_model(highres_dir, lowres_dir, args.modules_lua or None, args.dmanifest or None)
    out_path = default_out(args, highres_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(model), encoding="utf-8")

    t = model["totals"]
    print(f"Collections: {t['collection_count']}  Archives: {t['archive_count']}")
    print(f"Highres download: {t['high_total'] / 1048576:.1f} MB   Lowres download: {t['low_total'] / 1048576:.1f} MB")
    for w in model["warnings"]:
        print(f"  warning: {w}")
    print(f"Report written to {out_path}")


if __name__ == "__main__":
    main()
