---@generic T
---@param obj T
---@param deep boolean?
---@return T
local function copy_table(obj, deep)
	if type(obj) ~= "table" then
		return obj
	end
	local result = {}
	for k, v in pairs(obj) do
		if type(v) == "table" and deep then
			result[k] = copy_table(v, deep)
		else
			result[k] = v
		end
	end
	return result
end

local EMPTY_HASH = hash("")

---@param url url
---@return string
local function url_to_key(url)
	return hash_to_hex(url.socket or EMPTY_HASH)
		.. hash_to_hex(url.path or EMPTY_HASH)
		.. hash_to_hex(url.fragment or EMPTY_HASH)
end

---@alias liveupdater.res_mode integer
---@alias liveupdater.resolution "low"|"high"

---@class liveupdater.module
---@field files string[]
---@field priority number
---@field res_mode liveupdater.res_mode?
---@field by_request boolean?
---@field processed boolean?

---@class liveupdater.file_version_info
---@field version string
---@field size integer?

---@class liveupdater.manifest
---@field version string
---@field deps table<string, string[]>
---@field file_versions table<string, liveupdater.file_version_info|string>
---@field file_sizes table<string, integer>?

---@class liveupdater.manifest_group
---@field lowres liveupdater.manifest? present only when the lowres resolution is active
---@field highres liveupdater.manifest? present only when the highres resolution is active

---Normalized view of one remote manifest, built once per init.
---@class liveupdater.manifest_side
---@field versions table<string, string> archive name -> version hash
---@field sizes table<string, integer> archive name -> archive size in bytes (0 if the manifest has no sizes)
---@field dep_lists table<string, string[]> collection file -> dependency archive names
---@field dep_set table<string, boolean> set of all dependency archive names
---@field path string server path for this resolution
---@field prefix string progress prefix for this resolution

---A resolution side is present only when its server path was passed to M.init.
---At least one of the two is always present.
---@class liveupdater.manifest_cache
---@field low liveupdater.manifest_side?
---@field high liveupdater.manifest_side?

---@class liveupdater.file_progress
---@field progress number
---@field prefix string

---@class liveupdater.queue_item
---@field file_name string
---@field priority number
---@field version string
---@field size integer
---@field module_name string
---@field path string
---@field prefix string
---@field resolution liveupdater.resolution
---@field remount boolean?
---@field order integer?
---@field reason string?

---@class liveupdater.module_file_progress
---@field name string
---@field progress number

---@class liveupdater.module_progress
---@field name string
---@field progress number
---@field files liveupdater.module_file_progress[]

---@class liveupdater.resolution_progress
---@field total_bytes integer
---@field downloaded_bytes integer
---@field pending_files integer
---@field progress number 0..100, reaches 100 exactly when nothing is left to download

---@class liveupdater.download_progress: liveupdater.resolution_progress
---@field low liveupdater.resolution_progress
---@field high liveupdater.resolution_progress

---At least one of lowres_server_path / hires_server_path must be provided. When
---only one is provided that resolution is the only active one: res_mode on the
---modules is ignored and every module loads that single resolution.
---@class liveupdater.init_options
---@field modules table<string, liveupdater.module>
---@field lowres_server_path string?
---@field hires_server_path string?

---@class liveupdater
---@field initialized boolean
---@field saved_list table<string, string>
---@field save_cache_key string
---@field modules table<string, liveupdater.module>
local M = {
	initialized = false,
	---@type table<string, string>
	saved_list = nil,
	---@type string
	save_cache_key = nil,
	---@type table<string, liveupdater.module>
	modules = nil,
}

M.MSG_INITIALIZED = hash("liveupdater_initialized")
M.MSG_INITIALIZATION_FAILED = hash("liveupdater_initialization_failed")
M.MSG_FILE_LOADED = hash("liveupdater_file_loaded")
M.MSG_MODULE_LOADED = hash("liveupdater_module_loaded")
M.MSG_ALL_LOADED = hash("liveupdater_all_loaded")
M.MSG_NETWORK_ERROR = hash("liveupdater_network_error")

-- Log levels handed to the injected log function. Also exposed as a
-- level -> name lookup (liveupdater[liveupdater.DEBUG] == "DEBUG").
M.DEBUG, M[1] = 1, "DEBUG"
M.INFO, M[2] = 2, "INFO"
M.WARN, M[3] = 3, "WARN"
M.ERROR, M[4] = 4, "ERROR"
M.FATAL, M[5] = 5, "FATAL"

---@type fun(level: integer, message: string)
local log_function = function(_, _) end

---Assign a project logger. Signature: function(level, message), where level is
---one of M.DEBUG..M.FATAL. Optional — defaults to a no-op.
---@param fn fun(level: integer, message: string)
function M.set_log_function(fn)
	log_function = fn
end

---@param level integer
---@return fun(message: string, ...: any)
local function make_log_method(level)
	return function(message, ...)
		if select("#", ...) > 0 then
			local ok, formatted = pcall(string.format, message, ...)
			if ok then
				message = formatted
			end
		end
		log_function(level, message)
	end
end

local log = {
	debug = make_log_method(M.DEBUG),
	info = make_log_method(M.INFO),
	warn = make_log_method(M.WARN),
	error = make_log_method(M.ERROR),
	fatal = make_log_method(M.FATAL),
}

---Whether liveupdate may run on the current platform. Projects that must
---disable it (e.g. on Android) call M.set_platform_supported(false) before init.
---@type boolean
local platform_supported = true

---@param supported boolean
function M.set_platform_supported(supported)
	platform_supported = supported
end

---Sets the save-file key used to persist the list of downloaded archives. Must
---be called before M.init. The value is whatever sys.get_save_file(...) returns;
---keep it stable across releases so existing installs keep their saved state.
---@param key string
function M.set_save_cache_key(key)
	M.save_cache_key = key
end

---Predicate gating whether a non by_request module may be enqueued. Supplied
---via M.init; defaults to always-available.
---@type fun(module_name: string): boolean
local is_module_available = function()
	return true
end

---@type table<string, url>
local event_listeners = {}

local COMMON_MODULE = "COMMON"
local FILE_EXTENSION = ".arcd0"

---@param file_name string
---@param version string
---@return string
local function build_archive_filename(file_name, version)
	return file_name .. "_" .. version .. FILE_EXTENSION
end

local RES_HIGH_ONLY = 1
local RES_LOW_ONLY = 2
local RES_BOTH = 3

M.RES_HIGH_ONLY = RES_HIGH_ONLY
M.RES_LOW_ONLY = RES_LOW_ONLY
M.RES_BOTH = RES_BOTH

local LOWRES_PREFIX = "lowres_"
local HIGH_PREFIX = "high_"
local NOTLOAD_PREFIX = "notloaded_"
local FROMLOCAL_PREFIX = "local_"

---@type string
local highres_path
---@type string
local low_res_path
---Which resolutions are active this session, decided by which server paths were
---passed to M.init. At least one is always true. When exactly one is active,
---res_mode on the modules is ignored and every module loads that single
---resolution; when both are active res_mode governs as before.
---@type boolean
local active_low = false
---@type boolean
local active_high = false
---@type string
local manifest_json = "manifest.json"
---@type string
local local_folder = "liveupdate_zip"

---@type integer
local max_attempts = 3

---@type table<string, liveupdater.file_progress>
local downloadable_files = {}
---@type table<string, string>
local downloadable_missing_files = {}

---@type liveupdater.manifest_cache?
local manifest_cache = nil

---@type liveupdater.queue_item[]
local download_files_queue = {}
---@type table<string, liveupdater.queue_item>
local download_queue_index = {}
---@type integer
local download_queue_order = 0
---key of the queue item currently being downloaded/mounted
---@type string?
local in_flight_key = nil
---bytes received so far for the in-flight item, clamped to its manifest size
---@type integer
local in_flight_bytes = 0

---bytes queued and completed in the current download session, per resolution
---@type table<liveupdater.resolution, {total: integer, downloaded: integer}>
local session_bytes = {
	low = { total = 0, downloaded = 0 },
	high = { total = 0, downloaded = 0 },
}

local files_load_completed = false

---is_module_loaded results, valid until the next saved_list change
---@type table<string, boolean>
local module_loaded_memo = {}

---@return boolean
local function is_liveupdate_available()
	return liveupdate.is_built_with_excluded_files() and platform_supported
end

---@param message_id hash
---@param message table?
local function notify_event_listeners(message_id, message)
	for _, url in pairs(event_listeners) do
		msg.post(url, message_id, message or {})
	end
end

---@param filename string
---@return string
local function get_local_path(filename)
	return sys.get_save_file(local_folder, filename)
end

---@param filename string
---@param data string
---@return boolean, string?
local function save_file(filename, data)
	local path = get_local_path(filename)
	local file, err = io.open(path, "w+")
	if err or not file then
		return false, err
	end
	local f, err = file:write(data)
	if err then
		return false, err
	end
	file:close()
	return true, nil
end

local function save_local_files_list()
	local data = {
		saved_list = M.saved_list,
	}
	local success, err = pcall(sys.save, M.save_cache_key, data)
	if not success then
		log.error("Failed to save local files list: %s", err)
		return
	end
end

local function load_local_files_list()
	if not M.saved_list then
		local data = sys.load(M.save_cache_key)
		M.saved_list = data.saved_list or {}
	end
end

---@param file_name string
---@param version string?
local function set_saved_file_version(file_name, version)
	M.saved_list[file_name] = version
	module_loaded_memo = {}
	save_local_files_list()
end

---@param filename string
---@return boolean
local function file_exists(filename)
	return sys.exists(get_local_path(filename))
end

---@param filename string
---@return boolean?, string?
local function remove_local_file(filename)
	if sys.exists(get_local_path(filename)) then
		return os.remove(get_local_path(filename))
	else
		return true, nil
	end
end

---@param data string?
---@return table?
local function decode_json_data(data)
	if not data then
		return nil
	end
	local parsed, response_data = pcall(json.decode, data)
	if parsed then
		return response_data
	end
end

---@param filename string
---@return boolean
local function is_mount_exist(filename)
	for _, mount in ipairs(liveupdate.get_mounts()) do
		if mount.name == filename then
			return true
		end
	end
	return false
end

---@return integer
local function get_next_priority()
	local max_priority = 0
	for _, mount in ipairs(liveupdate.get_mounts()) do
		if mount.priority > max_priority then
			max_priority = mount.priority
		end
	end
	return max_priority + 1
end

---@param filename string
---@param cb fun(success: boolean)?
local function mount_resource(filename, cb)
	if is_mount_exist(filename) then
		if liveupdate.remove_mount(filename) ~= liveupdate.LIVEUPDATE_OK then
			error("Unable to remove mount " .. filename)
		end
	end

	liveupdate.add_mount(
		filename,
		"zip:" .. get_local_path(filename),
		get_next_priority(),
		function(self, path, uri, result)
			if cb then
				cb(result == liveupdate.LIVEUPDATE_OK)
			end
		end
	)
end

---@param prefix string
---@param filename string
---@param bytes_received number
---@param bytes_total number
local function update_file_progress(prefix, filename, bytes_received, bytes_total)
	local data = {
		progress = (bytes_received / bytes_total) * 100,
		prefix = prefix,
	}

	downloadable_files[filename] = data
end

---@param prefix string
---@param path string
---@param filename string
---@param basename string
---@param cb fun(success: boolean, data: string?)
---@param attempt integer?
---@param version string?
---@param progress_cb fun(bytes_received: number, bytes_total: number)?
local function request_data(prefix, path, filename, basename, cb, attempt, version, progress_cb)
	attempt = attempt or 1
	version = version or tostring(math.floor(socket.gettime()))
	http.request(
		path .. filename .. "?" .. version,
		"GET",
		function(self, id, response)
			-- with report_progress the callback also fires for transfer ticks;
			-- they carry bytes_* but no body, no error and no http status
			if
				progress_cb
				and response.bytes_total ~= nil
				and response.response == nil
				and response.error == nil
				and (response.status == nil or response.status == 0)
			then
				progress_cb(response.bytes_received or 0, response.bytes_total)
				return
			end
			if
				(response.status == 200 or response.status == 304)
				and response.error == nil
				and response.response ~= nil
			then
				cb(true, response.response)
			elseif attempt <= max_attempts then
				attempt = attempt + 1
				request_data(prefix, path, filename, basename, cb, attempt, version, progress_cb)
			else
				cb(false, response.error)
			end
		end,
		nil,
		nil,
		{
			timeout = 200,
			report_progress = progress_cb ~= nil,
		}
	)
end

---@param manifest liveupdater.manifest
---@param path string
---@param prefix string
---@return liveupdater.manifest_side
local function build_manifest_side(manifest, path, prefix)
	local versions = {}
	local sizes = {}
	local file_sizes = manifest.file_sizes or {}
	for file_name, info in pairs(manifest.file_versions) do
		if type(info) == "table" then
			versions[file_name] = info.version
			sizes[file_name] = info.size or file_sizes[file_name] or 0
		else
			versions[file_name] = info
			-- legacy manifests have no file_sizes, size stays unknown
			sizes[file_name] = file_sizes[file_name] or 0
		end
	end

	local dep_lists = {}
	local dep_set = {}
	for dep_name, collections in pairs(manifest.deps or {}) do
		dep_set[dep_name] = true
		for _, file_name in ipairs(collections) do
			if not dep_lists[file_name] then
				dep_lists[file_name] = {}
			end
			table.insert(dep_lists[file_name], dep_name)
		end
	end

	return {
		versions = versions,
		sizes = sizes,
		dep_lists = dep_lists,
		dep_set = dep_set,
		path = path,
		prefix = prefix,
	}
end

---@param manifest_group liveupdater.manifest_group
---@return liveupdater.manifest_cache
local function build_manifest_cache(manifest_group)
	return {
		low = manifest_group.lowres and build_manifest_side(manifest_group.lowres, low_res_path, LOWRES_PREFIX) or nil,
		high = manifest_group.highres and build_manifest_side(manifest_group.highres, highres_path, HIGH_PREFIX) or nil,
	}
end

---@param file_name string
---@return string? low_version, string? high_version
local function get_actual_versions(file_name)
	assert(manifest_cache, "Liveupdater: manifest cache not built")
	local low_version = manifest_cache.low and manifest_cache.low.versions[file_name]
	local high_version = manifest_cache.high and manifest_cache.high.versions[file_name]
	return low_version or nil, high_version or nil
end

---@param module liveupdater.module
---@return liveupdater.res_mode
local function get_res_mode(module)
	return module.res_mode or RES_HIGH_ONLY
end

---Which resolutions a module should be processed in, intersecting its res_mode
---with the resolutions active this session. When only one resolution is active
---res_mode is ignored: every module loads that single resolution.
---@param module liveupdater.module
---@return boolean supports_low, boolean supports_high
local function get_module_resolutions(module)
	if active_low and active_high then
		local res_mode = get_res_mode(module)
		return res_mode == RES_LOW_ONLY or res_mode == RES_BOTH, res_mode == RES_HIGH_ONLY or res_mode == RES_BOTH
	end
	return active_low, active_high
end

---Fetches the manifest of each active resolution and returns them as a group.
---Only the active sides are downloaded; a single-resolution session hits one URL.
---@param cb fun(success: boolean, result: liveupdater.manifest_group|string)
local function check_manifests(client_version, cb)
	---@type liveupdater.manifest_group
	local result = {}

	local function check_client_version(manifest, resolution)
		if client_version ~= nil and manifest.client_version ~= client_version then
			cb(
				false,
				string.format(
					"Client version mismatch in %s manifest: expected %s, got %s",
					resolution,
					client_version,
					tostring(manifest.client_version)
				)
			)
			return false
		end
		return true
	end

	---@param next_step fun()
	local function fetch_lowres(next_step)
		if not active_low then
			next_step()
			return
		end
		request_data(LOWRES_PREFIX, low_res_path, manifest_json, manifest_json, function(success, data)
			if not success then
				cb(false, "Unable to get low res manifest")
				return
			end
			local manifest = decode_json_data(data)
			if not manifest then
				cb(false, "Unable to parse low res manifest")
				return
			end
			if not check_client_version(manifest, "lowres") then
				return
			end
			result.lowres = manifest
			next_step()
		end)
	end

	---@param next_step fun()
	local function fetch_highres(next_step)
		if not active_high then
			next_step()
			return
		end
		request_data(HIGH_PREFIX, highres_path, manifest_json, manifest_json, function(success, data)
			if not success then
				cb(false, "Unable to get highres manifest")
				return
			end
			local manifest = decode_json_data(data)
			if not manifest then
				cb(false, "Unable to parse highres manifest")
				return
			end
			if not check_client_version(manifest, "highres") then
				return
			end
			result.highres = manifest
			next_step()
		end)
	end

	fetch_lowres(function()
		fetch_highres(function()
			cb(true, result)
		end)
	end)
end

---@param file_name string
---@return boolean
local function remove_mount_and_local_file(file_name)
	for _, mount in ipairs(liveupdate.get_mounts()) do
		if mount.name == file_name then
			if liveupdate.remove_mount(file_name) == liveupdate.LIVEUPDATE_OK then
				break
			else
				log.error("Unable to remove mount %s", file_name)
				return false
			end
		end
	end
	local result, err = remove_local_file(file_name)
	if not result then
		log.error("Unable to remove file %s: %s", file_name, err)
		return false
	end
	set_saved_file_version(file_name, nil)
	return true
end

---Removes stale local files and mounts the up-to-date ones.
---@param callback fun(success: boolean, error_text: string?)
local function sync_local_files(callback)
	local to_mount = {}
	local stale = {}
	local missing = {}
	for filename, saved_version in pairs(M.saved_list) do
		local low_version, high_version = get_actual_versions(filename)
		if saved_version ~= low_version and saved_version ~= high_version then
			table.insert(stale, filename)
		elseif file_exists(filename) then
			table.insert(to_mount, filename)
		else
			table.insert(missing, filename)
		end
	end

	for _, filename in ipairs(stale) do
		if not remove_mount_and_local_file(filename) then
			log.error("Unable to remove file from storage %s", filename)
			callback(false, "Unable to remove file " .. filename)
			return
		end
	end

	for _, filename in ipairs(missing) do
		if not remove_mount_and_local_file(filename) then
			log.warn("Unable to cleanup invalid saved file %s", filename)
		end
	end

	local function mount_next(index)
		if index > #to_mount then
			callback(true)
			return
		end
		local filename = to_mount[index]
		mount_resource(filename, function(success)
			if not success then
				log.error("Mount existing files error: %s", filename)
				callback(false, "Unable to mount existing valid files")
				return
			end
			mount_next(index + 1)
		end)
	end

	mount_next(1)
end

---@param modules table<string, liveupdater.module>
local function enqueue_modules(modules)
	assert(manifest_cache, "Liveupdater: manifest cache not built")
	local low = manifest_cache.low
	local high = manifest_cache.high

	---@param item liveupdater.queue_item
	---@param reason string
	local function add_to_queue(item, reason)
		local key = item.file_name .. ":" .. item.version
		local existing = download_queue_index[key]
		if existing then
			-- an in-flight item cannot be replaced: its download already runs and
			-- completion credits its bucket, a swapped entry would only skew accounting
			if existing.priority <= item.priority or key == in_flight_key then
				return
			end
			-- new item has higher priority (smaller value) — drop the old entry;
			-- the replacement may come from the other resolution pass, so the
			-- bytes move between the per-resolution buckets
			for index, value in ipairs(download_files_queue) do
				if value == existing then
					table.remove(download_files_queue, index)
					break
				end
			end
			local existing_bytes = session_bytes[existing.resolution]
			existing_bytes.total = existing_bytes.total - existing.size
		end
		local item_bytes = session_bytes[item.resolution]
		item_bytes.total = item_bytes.total + item.size
		download_queue_order = download_queue_order + 1
		item.order = download_queue_order
		item.reason = reason
		download_queue_index[key] = item
		table.insert(download_files_queue, item)
	end

	---@param side liveupdater.manifest_side
	---@param file_name string
	---@param base_priority number
	---@param resolution liveupdater.resolution
	local function add_dependencies(side, file_name, base_priority, resolution)
		for _, dep_name in ipairs(side.dep_lists[file_name] or {}) do
			local saved_dep_version = M.saved_list[dep_name]
			local dep_version_low = low and low.versions[dep_name]
			local dep_version_high = high and high.versions[dep_name]
			local skip_dep = false
			if
				saved_dep_version
				and resolution == "low"
				and (saved_dep_version == dep_version_low or saved_dep_version == dep_version_high)
			then
				skip_dep = true
			end
			if saved_dep_version and resolution == "high" and saved_dep_version == dep_version_high then
				skip_dep = true
			end
			if not skip_dep then
				local version = resolution == "high" and dep_version_high or dep_version_low
				if version then
					add_to_queue({
						file_name = dep_name,
						priority = base_priority - 0.5,
						version = version,
						size = side.sizes[dep_name] or 0,
						module_name = COMMON_MODULE,
						path = side.path,
						prefix = side.prefix,
						resolution = resolution,
						remount = resolution == "high",
					}, string.format("dependency_of=%s", file_name))
				end
			end
		end
	end

	for module_name, module in pairs(modules) do
		local available = true
		if module.by_request then
			available = false
		elseif not is_module_available(module_name) then
			available = false
		end

		if available then
			module.processed = true
			for _, file_name in ipairs(module.files) do
				local saved_version = M.saved_list[file_name]
				local low_version = low and low.versions[file_name]
				local high_version = high and high.versions[file_name]
				local supports_low, supports_high = get_module_resolutions(module)

				local needs_update = not saved_version
					or (saved_version ~= low_version and saved_version ~= high_version)

				if supports_low then
					add_dependencies(low, file_name, module.priority, "low")
					if low_version and needs_update then
						add_to_queue({
							file_name = file_name,
							priority = module.priority,
							version = low_version,
							size = low.sizes[file_name] or 0,
							module_name = module_name,
							path = low.path,
							prefix = low.prefix,
							resolution = "low",
						}, "needs_low")
					end
				end

				if supports_high then
					add_dependencies(high, file_name, module.priority + 1000, "high")
					if high_version and needs_update and (not supports_low or high_version ~= low_version) then
						add_to_queue({
							file_name = file_name,
							priority = module.priority + 1000,
							version = high_version,
							size = high.sizes[file_name] or 0,
							module_name = module_name,
							path = high.path,
							prefix = high.prefix,
							resolution = "high",
							remount = true,
						}, "needs_high")
					end
				end
			end
		end
	end

	table.sort(download_files_queue, function(a, b)
		if a.priority == b.priority then
			return a.order < b.order
		end
		return a.priority < b.priority
	end)
end

---@param load_info liveupdater.queue_item
local function remove_mount(load_info)
	if load_info.remount then
		if is_mount_exist(load_info.file_name) then
			if liveupdate.remove_mount(load_info.file_name) ~= liveupdate.LIVEUPDATE_OK then
				log.error("Unable to remove mount %s", load_info.file_name)
			end
		end
		local remove_local_result, err = remove_local_file(load_info.file_name)
		if not remove_local_result then
			log.error("Unable to remove file %s: %s", load_info.file_name, err)
		end
		set_saved_file_version(load_info.file_name, nil)
	end
end

---@param modules table<string, liveupdater.module>
local function check_modules_integrity(modules)
	assert(manifest_cache, "Liveupdater: manifest cache not built")
	-- highres is the superset when both are active; fall back to lowres when it is
	-- the only active resolution
	local reference_side = manifest_cache.high or manifest_cache.low
	assert(reference_side, "Liveupdater: no active manifest side")

	local all_module_files = {}
	for _, module in pairs(modules) do
		for _, file_name in ipairs(module.files) do
			all_module_files[file_name] = true
		end
	end

	for file_name, _ in pairs(all_module_files) do
		if not reference_side.versions[file_name] then
			local error_text =
				string.format("The module specifies a file that is not listed in the manifest. Module: %s", file_name)
			log.warn(error_text)
			downloadable_missing_files[file_name] = error_text
		end
	end

	for file_name, _ in pairs(reference_side.versions) do
		if all_module_files[file_name] == nil and not reference_side.dep_set[file_name] then
			local error_text = string.format(
				"The manifest lists a file that is not present in the modules. File: %s.collectionc",
				file_name
			)
			log.warn(error_text)
			downloadable_missing_files[file_name] = error_text
		end
	end
end

---@param load_info liveupdater.queue_item
---@param load_resources fun()
local function add_mount(load_info, load_resources)
	mount_resource(load_info.file_name, function(success)
		if success then
			update_file_progress(load_info.prefix, load_info.file_name, 1, 1)
			set_saved_file_version(load_info.file_name, load_info.version)
			local res_bytes = session_bytes[load_info.resolution]
			res_bytes.downloaded = res_bytes.downloaded + load_info.size

			in_flight_key = nil
			in_flight_bytes = 0
			download_queue_index[load_info.file_name .. ":" .. load_info.version] = nil
			for index, value in ipairs(download_files_queue) do
				if value.file_name == load_info.file_name and value.version == load_info.version then
					table.remove(download_files_queue, index)
					break
				end
			end

			notify_event_listeners(M.MSG_FILE_LOADED, {
				file_name = load_info.file_name,
				module_name = load_info.module_name,
			})
			if load_info.module_name ~= COMMON_MODULE and M.is_module_loaded(load_info.module_name) then
				notify_event_listeners(M.MSG_MODULE_LOADED, {
					file_name = load_info.file_name,
					module_name = load_info.module_name,
				})
			end

			if next(download_files_queue) then
				load_resources()
			else
				files_load_completed = true
				notify_event_listeners(M.MSG_ALL_LOADED)
				log.debug("All global files loaded")
			end
		else
			log.error("Unable to mount file %s", load_info.file_name)
			timer.delay(5, false, load_resources)
		end
	end)
end

---@param success boolean
---@param data string?
---@param load_info liveupdater.queue_item
---@param load_resources fun()
local function handle_load_info_callback(success, data, load_info, load_resources)
	if success then
		remove_mount(load_info)
		---@cast data -?
		local result, err = save_file(load_info.file_name, data)
		if result then
			add_mount(load_info, load_resources)
		else
			log.error("Unable to save file %s: %s", load_info.file_name, err)
			timer.delay(5, false, load_resources)
		end
	else
		notify_event_listeners(M.MSG_NETWORK_ERROR)
		timer.delay(5, false, load_resources)
	end
end

local function try_start_load_resources()
	---@type liveupdater.queue_item?
	local load_info = download_files_queue[1]
	if load_info then
		in_flight_key = load_info.file_name .. ":" .. load_info.version
		in_flight_bytes = 0

		local callback = function(success, data)
			handle_load_info_callback(success, data, load_info, try_start_load_resources)
		end

		---@param bytes_received number
		---@param bytes_total number
		local progress_callback = function(bytes_received, bytes_total)
			if load_info.size > 0 then
				in_flight_bytes = math.min(bytes_received, load_info.size)
			end
			if bytes_total > 0 then
				-- 100 is reserved for a completed mount, live transfer caps just below
				update_file_progress(
					load_info.prefix,
					load_info.file_name,
					math.min(bytes_received / bytes_total, 0.999),
					1
				)
			end
		end

		request_data(
			load_info.prefix,
			load_info.path,
			build_archive_filename(load_info.file_name, load_info.version),
			load_info.file_name,
			callback,
			nil,
			load_info.version,
			progress_callback
		)
	else
		in_flight_key = nil
		in_flight_bytes = 0
		files_load_completed = true
	end
end

---@param manifest_group liveupdater.manifest_group
---@param cb fun(success: boolean, error_text: string?)
local function initialize_from_manifests(manifest_group, cb)
	manifest_cache = build_manifest_cache(manifest_group)
	module_loaded_memo = {}

	sync_local_files(function(success, error_text)
		if not success then
			cb(false, error_text)
			return
		end

		download_files_queue = {}
		download_queue_index = {}
		download_queue_order = 0
		in_flight_key = nil
		in_flight_bytes = 0
		session_bytes.low = { total = 0, downloaded = 0 }
		session_bytes.high = { total = 0, downloaded = 0 }
		enqueue_modules(M.modules)

		check_modules_integrity(M.modules)

		for filename, _ in pairs(M.saved_list) do
			update_file_progress(FROMLOCAL_PREFIX, filename, 1, 1)
		end

		try_start_load_resources()

		M.initialized = true
		notify_event_listeners(M.MSG_INITIALIZED)
		cb(true)
	end)
end

---@param module_name string
---@return boolean
function M.has_module(module_name)
	assert(M.initialized, "Liveupdater: not initialized")
	assert(M.modules, "Liveupdater: modules not initialized")
	return M.modules[module_name] ~= nil
end

---@param module liveupdater.module
---@return boolean
local function compute_module_loaded(module)
	local cache = assert(manifest_cache, "Liveupdater: manifest cache not built")
	local supports_low, supports_high = get_module_resolutions(module)

	for _, file_name in ipairs(module.files) do
		local saved_version = M.saved_list[file_name]
		if not saved_version or not file_exists(file_name) then
			return false
		end

		local low_version, high_version = get_actual_versions(file_name)
		local matches_low = supports_low and saved_version == low_version
		local matches_high = supports_high and saved_version == high_version
		if not matches_low and not matches_high then
			return false
		end

		local side = matches_high and cache.high or cache.low
		for _, dep_name in ipairs(side.dep_lists[file_name] or {}) do
			local dep_saved_version = M.saved_list[dep_name]
			local dep_low_version, dep_high_version = get_actual_versions(dep_name)
			local dep_valid = dep_saved_version
				and (dep_saved_version == dep_high_version or dep_saved_version == dep_low_version)
			if not dep_valid then
				return false
			end
		end
	end
	return true
end

---@param module_name string
---@return boolean
function M.is_module_loaded(module_name)
	assert(M.initialized, "Liveupdater: not initialized")
	assert(M.modules, "Liveupdater: modules not initialized")
	local module = M.modules[module_name]
	assert(module, "Liveupdater: No such module " .. tostring(module_name))

	if not is_liveupdate_available() then
		return true
	end

	local memo = module_loaded_memo[module_name]
	if memo ~= nil then
		return memo
	end

	local loaded = compute_module_loaded(module)
	module_loaded_memo[module_name] = loaded
	return loaded
end

---@param options liveupdater.init_options
---@param cb fun(success: boolean, error_text: string?)
---@param module_available_fn (fun(module_name: string): boolean)? predicate gating non by_request modules; defaults to always-available
function M.init(options, cb, module_available_fn)
	M.modules = copy_table(options.modules, true)
	if module_available_fn then
		is_module_available = module_available_fn
	end
	if not is_liveupdate_available() then
		M.initialized = true
		notify_event_listeners(M.MSG_INITIALIZED)
		cb(true)
		return
	end
	if
		options.client_version ~= nil
		and (type(options.client_version) ~= "string" or not options.client_version:match("%S"))
	then
		cb(false, "Liveupdater: client_version must be a non-empty string")
		return
	end
	assert(M.save_cache_key, "Liveupdater: save_cache_key not set; call M.set_save_cache_key before M.init")
	load_local_files_list()

	assert(options, "Liveupdater: options not provided")
	assert(
		options.lowres_server_path or options.hires_server_path,
		"Liveupdater: at least one of lowres_server_path / hires_server_path must be provided"
	)
	highres_path = options.hires_server_path
	low_res_path = options.lowres_server_path
	active_low = options.lowres_server_path ~= nil
	active_high = options.hires_server_path ~= nil

	if html5 then
		check_manifests(options.client_version, function(success, result)
			if success then
				---@cast result -string
				initialize_from_manifests(result, cb)
			else
				cb(false, result)
			end
		end)
	end
end

---@param url url?
function M.add_listener(url)
	url = url or msg.url()
	event_listeners[url_to_key(url)] = url
end

---@param url url?
function M.remove_listener(url)
	url = url or msg.url()
	event_listeners[url_to_key(url)] = nil
end

---@param module_name string
---@param module liveupdater.module?
---@return liveupdater.module_progress?
function M.get_module_progress(module_name, module)
	if not module then
		log.warn("Module not found")
		return nil
	end

	local module_info = {
		name = module_name,
		progress = 0,
		files = {},
	}

	local module_loaded = 0
	local module_total = #module.files
	for _, file_name in ipairs(module.files) do
		local file_info = downloadable_files[file_name]
		if file_info then
			local prefix_filename = file_info.prefix .. file_name
			local file_progress = file_info.progress or 0
			table.insert(module_info.files, {
				name = prefix_filename,
				progress = file_progress,
			})

			if file_progress == 100 then
				module_loaded = module_loaded + 1
			end
		else
			table.insert(module_info.files, {
				name = NOTLOAD_PREFIX .. file_name,
				progress = 0,
			})
		end
	end

	if module_loaded > 0 then
		module_info.progress = (module_loaded / module_total) * 100
	end

	return module_info
end

---@return boolean
function M.is_liveupdate_loaded()
	return M.initialized and not next(download_files_queue)
end

---@return table<string, liveupdater.file_progress>
function M.get_downloadable_files()
	return downloadable_files
end

---@return table<string, string>
function M.get_downloadable_missing_files()
	return downloadable_missing_files
end

---@param total_bytes integer
---@param downloaded_bytes integer
---@param pending_files integer
---@param forced_progress number?
---@return liveupdater.resolution_progress
local function build_progress(total_bytes, downloaded_bytes, pending_files, forced_progress)
	local progress = forced_progress
	if not progress then
		if pending_files == 0 then
			progress = 100
		elseif total_bytes > 0 then
			-- zero-size pending archives (manifest without file_sizes) must not
			-- let the value reach 100 while something is still downloading
			progress = math.min(99.9, (downloaded_bytes / total_bytes) * 100)
		else
			progress = 0
		end
	end
	return {
		total_bytes = total_bytes,
		downloaded_bytes = downloaded_bytes,
		pending_files = pending_files,
		progress = progress,
	}
end

---@param forced_progress number?
---@return liveupdater.download_progress
local function build_idle_progress(forced_progress)
	local result = build_progress(0, 0, 0, forced_progress) --[[@as liveupdater.download_progress]]
	result.low = build_progress(0, 0, 0, forced_progress)
	result.high = build_progress(0, 0, 0, forced_progress)
	return result
end

---Byte-based progress for a module or a set of modules, with a per-resolution
---breakdown in the low/high fields. Shared dependency archives are counted once
---per set; an archive identical in both manifests counts toward the resolution
---counted first (for RES_BOTH that is lowres — the pass that actually downloads
---it). Anchored to the download queue: an archive counts as downloaded when
---nothing for it is pending, so the result reaches 100 exactly when the set's
---downloads drain. For by_request modules that were not requested yet, the
---future queue content is predicted from the same rules enqueue_modules uses;
---that prediction can dip for a moment while a shared archive is remounted to
---its highres copy, it recovers on the next mount.
---@param module_names string|string[]
---@return liveupdater.download_progress
function M.get_modules_download_progress(module_names)
	if type(module_names) == "string" then
		module_names = { module_names }
	end

	if not is_liveupdate_available() then
		return build_idle_progress()
	end
	if not M.initialized or not manifest_cache then
		return build_idle_progress(0)
	end
	local cache = manifest_cache

	local counters = {
		low = { total = 0, downloaded = 0, pending = 0 },
		high = { total = 0, downloaded = 0, pending = 0 },
	}
	local counted = {}

	---@param archive_name string
	---@param side liveupdater.manifest_side
	---@param done boolean
	local function count_archive(archive_name, side, done)
		local version = side.versions[archive_name]
		if not version then
			return
		end
		local key = archive_name .. ":" .. version
		if counted[key] then
			return
		end
		counted[key] = true
		local size = side.sizes[archive_name]
		local counter = side == cache.low and counters.low or counters.high
		counter.total = counter.total + size
		if done then
			counter.downloaded = counter.downloaded + size
		else
			counter.pending = counter.pending + 1
			if key == in_flight_key and in_flight_bytes > 0 then
				counter.downloaded = counter.downloaded + math.min(in_flight_bytes, size)
			end
		end
	end

	---@param archive_name string
	---@param side liveupdater.manifest_side
	---@return boolean
	local function is_queued(archive_name, side)
		local version = side.versions[archive_name]
		return version ~= nil and download_queue_index[archive_name .. ":" .. version] ~= nil
	end

	---@param side liveupdater.manifest_side
	---@param file_name string
	---@param processed boolean?
	---@param file_actual boolean
	local function count_file_side(side, file_name, processed, file_actual)
		local done = processed and not is_queued(file_name, side) or not processed and file_actual
		count_archive(file_name, side, done)
		local deps = side.dep_lists[file_name]
		if not deps then
			return
		end
		for _, dep_name in ipairs(deps) do
			local dep_done
			if processed then
				dep_done = not is_queued(dep_name, side)
			else
				-- mirrors skip_dep in enqueue_modules: a highres copy always
				-- satisfies, a lowres copy satisfies only the lowres pass
				local dep_saved = M.saved_list[dep_name]
				dep_done = dep_saved ~= nil
					and (
						(cache.high ~= nil and dep_saved == cache.high.versions[dep_name])
						or (side == cache.low and dep_saved == cache.low.versions[dep_name])
					)
			end
			count_archive(dep_name, side, dep_done)
		end
	end

	for _, module_name in ipairs(module_names) do
		local module = M.modules[module_name]
		assert(module, "Liveupdater: No such module " .. tostring(module_name))
		local supports_low, supports_high = get_module_resolutions(module)
		local processed = module.processed

		for _, file_name in ipairs(module.files) do
			local saved_version = M.saved_list[file_name]
			-- same rule as needs_update in enqueue_modules, it gates both resolutions at once
			local file_actual = saved_version ~= nil
				and (
					(cache.low ~= nil and saved_version == cache.low.versions[file_name])
					or (cache.high ~= nil and saved_version == cache.high.versions[file_name])
				)

			if supports_low then
				count_file_side(cache.low, file_name, processed, file_actual)
			end
			if supports_high then
				count_file_side(cache.high, file_name, processed, file_actual)
			end
		end
	end

	local low = counters.low
	local high = counters.high
	local result = build_progress(low.total + high.total, low.downloaded + high.downloaded, low.pending + high.pending) --[[@as liveupdater.download_progress]]
	result.low = build_progress(low.total, low.downloaded, low.pending)
	result.high = build_progress(high.total, high.downloaded, high.pending)
	return result
end

---Byte-based progress of the current download session, with a per-resolution
---breakdown in the low/high fields (requires manifest with file_sizes).
---@return liveupdater.download_progress
function M.get_download_progress()
	local pending_low = 0
	local pending_high = 0
	for _, item in ipairs(download_files_queue) do
		if item.resolution == "low" then
			pending_low = pending_low + 1
		else
			pending_high = pending_high + 1
		end
	end

	local partial_low = 0
	local partial_high = 0
	if in_flight_key and in_flight_bytes > 0 then
		local in_flight_item = download_queue_index[in_flight_key]
		if in_flight_item then
			if in_flight_item.resolution == "low" then
				partial_low = in_flight_bytes
			else
				partial_high = in_flight_bytes
			end
		end
	end

	local low = session_bytes.low
	local high = session_bytes.high
	local result = build_progress(
		low.total + high.total,
		low.downloaded + high.downloaded + partial_low + partial_high,
		pending_low + pending_high
	) --[[@as liveupdater.download_progress]]
	result.low = build_progress(low.total, low.downloaded + partial_low, pending_low)
	result.high = build_progress(high.total, high.downloaded + partial_high, pending_high)
	return result
end

---@return number
function M.get_total_downloadable_progress()
	if not is_liveupdate_available() then
		return 0
	end
	local total_progress = 0
	local module_count = 0

	for module_name, module in pairs(M.modules) do
		local module_info = M.get_module_progress(module_name, module)
		if module_info then
			total_progress = total_progress + module_info.progress
			module_count = module_count + 1
		end
	end

	if module_count == 0 then
		return 0
	end

	return total_progress / module_count
end

---@param module_name string
---@return boolean?
function M.is_module_in_queue(module_name)
	if not is_liveupdate_available() then
		return true
	end
	local module = M.modules[module_name]
	return module and module.processed
end

---@param module_name string
function M.request_module_load(module_name)
	if not M.initialized then
		return
	end
	if not is_liveupdate_available() then
		return
	end
	local module = M.modules[module_name]
	if not module then
		return
	end
	if module.by_request and not module.processed then
		module.by_request = false
		enqueue_modules({ [module_name] = module })
		if files_load_completed then
			files_load_completed = false
			try_start_load_resources()
		end
	end
end

return M
