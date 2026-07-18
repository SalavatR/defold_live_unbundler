# Liveupdate: architecture

This document describes the interaction between two components:

- **`tools/liveupdate_pack.py`** — offline archive/manifest builder (invoked from `make buildliveupdateres`).
- **`live_unbundler/live_unbundler.lua`** — runtime client that downloads archives, mounts them and keeps the local cache consistent.

They communicate through two artifacts published to the CDN: `manifest.json` and `<archive>_<version>.arcd0`.

---

## 1. Data flow

```
┌────────────────────┐                   ┌────────────────────────┐
│    bob build       │  ───────────────▶ │ liveupdate_dist/       │
│                    │                   │   <hex>     (raw)      │
└────────────────────┘                   │   liveupdate.dmanifest │
                                         │   game.graph.json      │
                                         └─────────┬──────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │ tools/liveupdate_pack.py     │
                                    │  • groups resources          │
                                    │  • splits into chunks        │
                                    │  • computes version_hash     │
                                    │  • writes archives + manifest│
                                    └─────────┬────────────────────┘
                                              │
                                              ▼
                                    ┌──────────────────────────────┐
                                    │ liveupdate_zip/              │
                                    │   manifest.json              │
                                    │   <archive>_<ver>.arcd0      │
                                    └─────────┬────────────────────┘
                                              │ deploy
                                              ▼
                                       CDN  (lowres / highres)
                                              │
                                              ▼
                                    ┌──────────────────────────────┐
                                    │ live_unbundler.lua (client)  │
                                    │  • check manifests           │
                                    │  • diff against saved_list   │
                                    │  • download + mount          │
                                    └──────────────────────────────┘
```

The pipeline runs separately for **lowres** and **highres**. Either resolution may be
absent — the client runs whichever server paths it is given (see §6).

---

## 2. What `liveupdate_pack.py` does

### 2.1 Inputs

- `build/default/game.graph.json` — Defold's resource graph (`path`, `hexDigest`, `children`, `nodeType`, `isInMainBundle`).
- `liveupdate_dist/` — raw resources, file names = `hexDigest`.
- `liveupdate_dist/liveupdate.game.dmanifest` — engine's protobuf manifest.

### 2.2 Grouping

All resources outside the main bundle are split into six archive classes:

| Prefix | Contents |
| --- | --- |
| `<collection>`            | non-texture, non-sound resources of a specific collection-proxy |
| `<collection>_texture`    | textures (`.texturec`, `.a.texturesetc`) of that collection |
| `<collection>_sound`      | sounds (`.soundc` + its `.oggc`/`.opusc`/`.wavc` audio) of that collection |
| `common_<hash>`           | non-texture, non-sound resources shared by ≥ 2 collections |
| `common_texture_<hash>`   | shared textures used by ≥ 2 collections |
| `common_sound_<hash>`     | shared sounds used by ≥ 2 collections |

Archive names use the **bare collection name** — the last extension is stripped from
`<id>.collectionc` and replaced by `COLLECTION_SUFFIX` (the `LIVEUPDATE_COLLECTION_SUFFIX`
env var, empty by default, so `foo.collectionc` → `foo`). The same names are used as keys
in `manifest.json` and in the CDN file names.

Logic:

1. `get_deps_files` walks every `ExcludedCollectionProxy` and gathers their transitive dependencies.
2. `build_common_files` tags resources with `use_count` (how many collections reference each one).
3. `create_common_archives_by_dependency_sets` groups shared resources by the **exact set of consumer collections** — so a common chunk is re-downloaded only by the collections that actually use it.
4. `split_by_size` slices each class into chunks that target ≤ `MAX_ARCHIVE_SIZE` (7 MiB). It keeps `*.texturec` + `*.a.texturesetc` pairs together (by shared stem) and keeps each `*.soundc` together with its compiled audio (`*.oggc`/`*.opusc`/`*.wavc`) in the same chunk. Sound co-location follows the graph `children` edge, not a shared name, and is 1:N — a component with several format variants stays whole. A single indivisible unit (a texture pair or a sound + its audio) whose size alone exceeds the cap is emitted whole in its own archive rather than split, so an archive can legitimately exceed 7 MiB.

### 2.3 Archive version

`compute_version_hash_from_files` (and the equivalent in `create_zip_archive`) computes `version_hash` as a SHA-256 over:

- `dmanifest` `resource` entries sorted by `hash.data.hex()`,
- the string `content_hash_no_manifest:<sha256 of contents excluding dmanifest>`.

The first `HASH_LEN = 16` hex characters are kept. The same string is used in:

- the CDN file name: `<archive_name>_<version_hash>.arcd0`,
- the `file_versions` value in `manifest.json`.

`engine_versions` is **not** part of the hash — bumping the engine version does not invalidate archives.

### 2.4 Outputs

- `liveupdate_zip/<archive>_<ver>.arcd0` — zip archives with a `dmanifest` inside.
- `liveupdate_zip/manifest.json` — index for the client (format — see §3).
- `files_tree.json` — internal snapshot for reproducible rebuilds (`--restore_from_tree`).

---

## 3. `manifest.json` format

```json
{
  "version": "1777019959",
  "deps": {
    "common_texture_ad6d67fefa501371": ["baking_festival_window", "baking_festival_info_window"]
  },
  "file_versions": {
    "baking_festival_window":          "a3e317707f7a3947",
    "common_texture_ad6d67fefa501371": "21f9526418de6268"
  },
  "file_sizes": {
    "baking_festival_window":          51234,
    "common_texture_ad6d67fefa501371": 892145
  },
  "dmanifest_info": { ... }
}
```

- **`file_versions`** — `archive_name → version_hash`. The authoritative list of archives and their versions.
- **`deps`** — `dependency_archive → [collection names]`: which collections pull each shared/texture archive. Drives dependency enqueueing on the client.
- **`file_sizes`** — `archive_name → compressed `.arcd0` size in bytes. Powers byte-based download progress. Kept in a **separate top-level key** on purpose: clients deployed before this key existed keep parsing `file_versions` as plain strings and simply see size `0`.
- **`dmanifest_info`** — auxiliary metadata (signature, engine versions, header hashes).

This is the same schema the production client consumes. See §7 for why it is kept this way
rather than a more compact index-based form.

---

## 4. What `live_unbundler.lua` does

### 4.1 State

- `M.modules` — module declarations: `{ files, priority, res_mode?, by_request? }`.
- `M.saved_list[file_name] = version` — what is already on disk.
- `M.save_cache_key` — `sys.save` key for `saved_list`; set via `M.set_save_cache_key(...)` **before** init.
- `manifest_cache = { low?, high? }` — one normalized `manifest_side` per active resolution, each holding `versions`, `sizes`, `dep_lists`, `dep_set`, `path`, `prefix` (see `build_manifest_side`).
- `download_files_queue` / `download_queue_index` — the priority queue and its `file_name:version` index.
- `session_bytes.low/high` — queued vs downloaded bytes per resolution, for byte-based progress.

### 4.2 Init flow

Call `M.set_save_cache_key(...)` (and optionally `M.set_platform_supported(...)` /
`M.set_log_function(...)`) **before** `M.init`.

1. **Load `saved_list`** from `sys.load(M.save_cache_key)`.
2. **`check_manifests`** — HTTP requests for `manifest.json` of each **active** resolution (a single-resolution session hits one URL).
3. **`build_manifest_cache`** — normalize each fetched manifest into a `manifest_side`.
4. **`sync_local_files`** — remove stale local files (saved version matches neither active manifest), mount whatever is already valid, drop entries whose file is missing.
5. **`enqueue_modules`** — build the download queue (archives + their dependencies) for every non-`by_request`, available module.
6. **`check_modules_integrity`** — sanity-check modules vs manifest (warnings only, non-fatal).
7. **`try_start_load_resources`** — drain the queue sequentially.
8. After every successful mount: `M.saved_list[file] = version` + `MSG_FILE_LOADED`; `MSG_MODULE_LOADED` when a module's files are all present; `MSG_ALL_LOADED` when the queue empties.

### 4.3 Queue

- Dedup key: `file_name:version`. A new item with the same key wins only if its `priority` is lower (smaller number); the **in-flight** item can never be replaced.
- Dependencies (from `dep_lists`) are inserted at `module.priority - 0.5`, i.e. strictly before the main file.
- Highres items are added at `priority + 1000` so lowres always overtakes highres.
- Highres items are flagged `remount = true` — the old mount and local file are removed before saving the new one.
- Sort order: `(priority asc, order asc)`.

### 4.4 Network retries

`request_data` retries up to `max_attempts = 3` on a network error / non-200. When all attempts fail, it emits `MSG_NETWORK_ERROR` and reschedules the queue 5 s later.

### 4.5 Progress

- `M.get_download_progress()` — byte progress of the current download session, with a `low` / `high` per-resolution breakdown (requires `file_sizes`).
- `M.get_modules_download_progress(names)` — byte progress for a module or set of modules, counting shared archives once; can predict the future queue for `by_request` modules not requested yet.
- `M.get_total_downloadable_progress()` — coarse file-count based progress across all modules (no `file_sizes` needed).

---

## 5. Resolution modes (`RES_*`)

| Mode | Semantics |
| --- | --- |
| `RES_LOW_ONLY`  | module uses only the lowres variant |
| `RES_HIGH_ONLY` | module uses highres |
| `RES_BOTH`      | low is downloaded first, then upgraded to high |

Dedup by `file_name:version` guarantees that when low/high versions match, the file is downloaded once.
When only one resolution is active (§6), `res_mode` is ignored — see below.

---

## 6. Active resolutions

`lowres_server_path` and `hires_server_path` in `init_options` are **both optional, but at
least one must be provided**. The active set is decided by which paths are passed:

- **Both active** — `res_mode` governs per module (`RES_LOW_ONLY` / `RES_HIGH_ONLY` / `RES_BOTH`).
- **Only one active** — `res_mode` is **ignored**; every module (including `RES_HIGH_ONLY`)
  loads that single resolution. `check_manifests` skips the absent side, and the absent
  `manifest_cache` side stays `nil`.
- `check_modules_integrity` uses highres as the reference when present, otherwise lowres.

This lets projects/configurations without a dedicated highres server share a single pipeline.

---

## 7. Manifest format: why `deps` + `file_versions` + `file_sizes`

An index-based form (a deduplicated `collections[]` pool plus a single `files` map addressing
it by index) was explored — it shrinks the raw JSON by roughly 45 %. It is **not** used here.

The manifest is treated as a **backward-compatible wire format**: already-deployed clients must
keep parsing manifests produced by a newer packer. The rule is *extend only by adding new
top-level keys*. `file_sizes` was added exactly this way — older clients ignore the key and
fall back to size `0`. Switching to an index-based `collections`/`files` layout is a breaking
reshape of `file_versions`/`deps` that old clients cannot read, so it is deliberately avoided
to stay in lock-step with the production client this reference project mirrors.

---

## 8. Files and entry points

| File | Purpose |
| --- | --- |
| `tools/liveupdate_pack.py`                       | Pack: archive construction and `manifest.json` |
| `tools/liveupdate_collections_report.py`         | Optional: self-contained HTML report of collections / archives / download sizes (`make report`) |
| `live_unbundler/live_unbundler.lua`              | Runtime: download + mount + queue |
| `main/main.script`                               | Runnable example: module declarations (`priority`, `RES_*`, `by_request`) + init + event handling |
| `dist/output/<ver>/liveupdatelowres/`            | Lowres artifacts (for CDN deploy) |
| `dist/output/<ver>/liveupdatehighres/`           | Highres artifacts |

---

## 9. Invariants worth preserving

- `version_hash` is deterministic with respect to the contents of `dmanifest.resources` and the raw bytes of the resources in the archive — reordering / regrouping without changing content must not change versions.
- The CDN file name is always `<archive>_<file_versions[archive]>.arcd0`.
- The manifest is a backward-compatible wire format: extend it only by adding new top-level keys (see §7); never reshape `file_versions` / `deps`.
- Any client-side branch that reads the highres side must tolerate `nil` — a single-resolution session leaves `manifest_cache.high` (or `.low`) unset (`get_actual_versions`, `get_module_resolutions` are nil-safe).
- `saved_list` is the single source of truth about what is on disk; mounting without updating `saved_list` will desync on the next start-up.
- `M.set_save_cache_key(...)` must be called before `M.init` — the key must stay stable across releases, or existing installs orphan their saved archives and re-download everything.
