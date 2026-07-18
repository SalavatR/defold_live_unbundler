<p align="center">
  <img src="logo.png" alt="Defold Live Unbundler" width="320">
</p>

# Defold Live Unbundler

A reference project that shows how to split a Defold game into liveupdate archives, host them next to the HTML5 build, and download / mount them on the fly from a Lua client. Includes both the offline packer (`tools/`) and the runtime module (`live_unbundler/live_unbundler.lua`).

See [LIVEUPDATE_ARCHITECTURE.md](LIVEUPDATE_ARCHITECTURE.md) for the full architecture overview.

---

## Setup

### 1. Disable engine auto-mount in `game.project`

Mount handling is fully managed by the runtime client, so the engine's auto-mount **must be disabled**. Add to your [game.project](game.project):

```ini
[liveupdate]
mount_on_start = 0
```

Without this, engine and client will fight over mounts and the cache will desync.

The separate [liveupdate.settings](liveupdate.settings) file is the standard Defold liveupdate config (mode + zip output path) used by `bob` during the build — leave it as shipped.

### 2. Packer dependencies

The packer lives in [`tools/`](tools/). To build archives in your own project:

1. Copy the whole `tools/` folder into your project root.
2. Install the Python dependencies:

   ```sh
   make install_requirements
   # or: pip3 install -r tools/requirements.txt
   ```

### 3. Local end-to-end check

```sh
make buildlocalweb   # builds the web bundle + lowres/highres archives + copies them into dist/
make serve3          # serves dist/defold_live_unbundler on http://localhost:8000
```

Open the page and **enable network throttling** in the browser DevTools (Network → Throttling → Slow 3G or similar) — without it the downloads finish instantly and you can't observe staged loading.

---

## Build targets

All build recipes live in the [Makefile](Makefile). Highlights:

| Target | What it does |
| --- | --- |
| `make buildliveupdatehighres` | Bob-builds the project against `game_high_res.project` and packs the highres archives into `dist/output/<ver>/liveupdatehighres/`. |
| `make buildliveupdatelowres`  | Same for `game_low_res.project` → `dist/output/<ver>/liveupdatelowres/`. Reuses `files_tree.json` via `--restore_from_tree` for reproducible packing. |
| `make buildliveupdateres`     | Runs both of the above sequentially, then generates the collections report (`make report`). |
| `make buildweb`               | HTML5 build only (no liveupdate packing). |
| `make buildlocalweb`          | `buildweb` + `buildliveupdateres` + `copyliveupdateres` — full local artifact. |
| `make serve3`                 | Serves `dist/defold_live_unbundler/` on port 8000. |
| `make report`                 | Builds a self-contained HTML report of collections / archives / download sizes into `dist/output/<ver>/` (see `tools/liveupdate_collections_report.py`). Run automatically at the end of `buildliveupdateres`; use standalone to regenerate without rebuilding. |

---

## Runtime usage

Full runnable example: [main/main.script](main/main.script). The minimum integration is:

```lua
local live_unbundler = require("live_unbundler.live_unbundler")

live_unbundler.add_listener()

-- Must be set before init. Keep the key stable across releases so existing
-- installs keep their saved list of downloaded archives.
live_unbundler.set_save_cache_key(sys.get_save_file("my_game", "liveupdate_saved_files"))
-- Optional, before init:
-- live_unbundler.set_platform_supported(false)                       -- disable on a platform (e.g. Android PAD)
-- live_unbundler.set_log_function(function(level, message) end)      -- route logs (no-op by default)

live_unbundler.init({
    modules = {
        my_module = {
            priority = 1,
            files = { "my_collection" }, -- collection name without any extension
            res_mode = live_unbundler.RES_BOTH,
        },
    },
    lowres_server_path = "./liveupdatelowres/", -- optional if hires is given
    hires_server_path  = "./liveupdatehighres/", -- optional if lowres is given
}, function(success, err)
    -- init done
end, function(module_name)
    return true -- gating hook: return false to skip a module
end)
```

Pass only one of `lowres_server_path` / `hires_server_path` to run a single resolution
(then `res_mode` is ignored and every module loads that one); pass both to let `res_mode`
choose per module.

Then react to events (`MSG_FILE_LOADED`, `MSG_MODULE_LOADED`, `MSG_ALL_LOADED`, `MSG_NETWORK_ERROR`) inside `on_message`, and gate `collectionproxy` loads on `live_unbundler.is_module_loaded(...)` — see [main/main.script](main/main.script) for the pattern.

Byte-based download progress (requires a manifest with `file_sizes`, produced by the current packer) is available via `live_unbundler.get_download_progress()` and `live_unbundler.get_modules_download_progress(names)`, each with a `low` / `high` per-resolution breakdown.

---

## Credits

The project code is under the [MIT License](LICENSE). The demo additionally bundles one
third-party asset under a different license:

- **Music** — *“Alone (loop) — melancholic — slow”* by **Makoto Hiramatsu**
  ([makotohiramatsu.itch.io](https://makotohiramatsu.itch.io/)), licensed
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Transcoded to `.ogg` /
  `.opus` / `.wav` for the `music.collection` sound example. Full license text:
  [`music/licence.TXT`](music/licence.TXT).
