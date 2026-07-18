# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.0] - 2026-07-18

Sync with the production client this reference project mirrors: the runtime module
and the packer were brought in line with the shipped implementation.

Media archive model (packer): a collection's resources are split by kind into separate
archives so heavy media downloads and updates independently. **Textures** go to
`<collection>_texture` / `common_texture_<hash>` (a `.texturec` is kept with its
`.a.texturesetc` by shared name stem); **sounds** go to `<collection>_sound` /
`common_sound_<hash>` (a `.soundc` is kept with its `.oggc`/`.opusc`/`.wavc` audio via the
resource-graph `children` edge, 1:N); everything else is the base `<collection>` /
`common_<hash>`. Textures existed before this release; sounds are new here (see Added).

### Added
- Byte-based download progress: `get_download_progress()` and
  `get_modules_download_progress(names)`, each with a `low` / `high` per-resolution
  breakdown. Requires a manifest with the new `file_sizes` key.
- `file_sizes` in `manifest.json` — compressed `.arcd0` size per archive, emitted by
  the packer. Added as a **new top-level key** so clients built before it existed keep
  parsing `file_versions` unchanged and simply see size `0`.
- Single-resolution sessions: pass only one of `lowres_server_path` /
  `hires_server_path`. That resolution becomes the only active one, `res_mode` is
  ignored, and the absent `manifest_cache` side stays `nil`.
- `set_save_cache_key(key)` — the `sys.save` key for the saved-archives list is now
  injected before `init` via a setter instead of being derived inside the module.
- `set_platform_supported(supported)` and `set_log_function(fn)` runtime setters;
  logging is level-based (`DEBUG`..`FATAL`) and a no-op until a function is set.
- `tools/liveupdate_collections_report.py` — self-contained interactive HTML report
  (collections, archives, per-resolution download sizes, cross-collection overlap
  explorer, duplicate analysis). Available as `make report` and generated
  automatically at the end of `make buildliveupdateres`.
- Sound archives: the packer gives a collection's sounds their own archives —
  `<name>_sound` (exclusive) and `common_sound_<hash>` (shared) — mirroring textures.
  A `.soundc` is kept together with its compiled audio (`.oggc`/`.opusc`/`.wavc`) in one
  chunk, following the graph `children` edge (1:N, no shared stem). The report
  classifies/colors sound archives; the demo's `music.collection` (an excluded proxy
  with `.wav`/`.ogg`/`.opus` components) exercises the path end-to-end.

### Changed
- Runtime init is now injected-before-init. `init_options` no longer carry
  `save_cache_key` or `disabled`; call `set_save_cache_key(...)` and, if needed,
  `set_platform_supported(...)` before `init`. See `main/main.script`.
- `manifest.json` uses `deps` + `file_versions` + `file_sizes`. The previously
  documented index-based `collections` + `files` layout (smaller JSON, but a breaking
  reshape) was dropped in favor of the backward-compatible format the production
  client requires — extend the manifest only by adding new top-level keys. See
  `LIVEUPDATE_ARCHITECTURE.md` §7.
- `LIVEUPDATE_ARCHITECTURE.md` rewritten to describe the current manifest format,
  runtime state/flow, active-resolution handling and progress APIs.
- `README.md` runtime example updated to the setter-based API; added the `make report`
  target and the single-resolution note.
- Report `classify()` now recognizes collection / texture / sound archives under both
  the suffixed (`<id>.collectionc`) and bare (`<id>`) naming, so archives categorize
  correctly regardless of `LIVEUPDATE_COLLECTION_SUFFIX` (previously bare-named demo
  archives all fell through to "other").
- README gained a "What the packer produces" section describing the texture / sound /
  common archive layout; `LIVEUPDATE_ARCHITECTURE.md` §2 documents the full grouping rules.

### Removed
- `tools/liveupdate_report.py` and `tools/liveupdate_report_generator.py` — superseded
  by `tools/liveupdate_collections_report.py`.

### Notes
- Keep the `set_save_cache_key(...)` value stable across releases. Changing it orphans
  existing installs' saved archives and forces a full re-download.
- The module's public API and file name (`live_unbundler/live_unbundler.lua`) are
  unchanged; only the initialization surface and manifest schema moved.
