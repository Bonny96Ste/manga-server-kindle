-- MangaBridge for KOReader
-- Designed for non-touch Kindle models, including Kindle 4 on firmware 4.1.4.

local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Event = require("ui/event")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local JSON = require("json")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local MultiInputDialog = require("ui/widget/multiinputdialog")
local NetworkMgr = require("ui/network/manager")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local InputContainer = require("ui/widget/container/inputcontainer")
local http = require("socket.http")
local lfs = require("libs/libkoreader-lfs")
local ltn12 = require("ltn12")
local mime = require("mime")
local socket = require("socket")
local _ = require("gettext")

local has_https, https = pcall(require, "ssl.https")

local MangaMenu = Menu:extend{}
function MangaMenu:onMenuHold(item)
    if item and item.hold_callback then
        item.hold_callback()
    end
    return true
end

local MangaBridge = InputContainer:extend{
    name = "mangabridge",
    is_doc_only = false,
    settings_file = DataStorage:getSettingsDir() .. "/mangabridge.lua",
    data_dir = "/mnt/us/mangabridge",
    library_dir = "/mnt/us/mangabridge/library",
    cache_dir = "/mnt/us/mangabridge/cache",
    cover_dir = "/mnt/us/mangabridge/covers",
    progress_file = "/mnt/us/mangabridge/progress.json",
    settings = nil,
    key_events = {
        MangaBridgeForwardRight = { { "RPgFwd" }, event = "MangaBridgeForward" },
        MangaBridgeForwardLeft = { { "LPgFwd" }, event = "MangaBridgeForward" },
    },
}

local function trim(value)
    return (tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

local function chapter_text(number)
    local text = tostring(number)
    text = text:gsub("%.0$", "")
    return text
end

local function safe_id(value)
    return tostring(value or ""):gsub("[^%w%-_]", "_")
end

local function ensure_dir(path)
    if lfs.attributes(path, "mode") == "directory" then
        return true
    end
    local command = string.format("mkdir -p %q", path)
    local result, reason, code = os.execute(command)
    return result == true or result == 0 or (reason == "exit" and code == 0)
end

local function file_exists(path)
    return lfs.attributes(path, "mode") == "file"
end

local function free_space_bytes()
    local pipe = io.popen("df -k /mnt/us 2>/dev/null | tail -n 1")
    if not pipe then
        return nil
    end
    local line = pipe:read("*l") or ""
    pipe:close()
    local fields = {}
    for value in line:gmatch("%S+") do
        table.insert(fields, value)
    end
    local available_kb = tonumber(fields[4])
    return available_kb and available_kb * 1024 or nil
end

local function read_all(path)
    local file = io.open(path, "rb")
    if not file then
        return nil
    end
    local value = file:read("*all")
    file:close()
    return value
end

local function write_all(path, value)
    local temporary = path .. ".tmp"
    local file = io.open(temporary, "wb")
    if not file then
        return false
    end
    file:write(value)
    file:close()
    os.remove(path)
    return os.rename(temporary, path)
end

local function load_json(path, fallback)
    local raw = read_all(path)
    if not raw or raw == "" then
        return fallback
    end
    local ok, value = pcall(function() return JSON.decode(raw) end)
    if ok and type(value) == "table" then
        return value
    end
    return fallback
end

local function save_json(path, value)
    local ok, raw = pcall(function() return JSON.encode(value) end)
    if not ok then
        return false
    end
    return write_all(path, raw)
end

local function append_error_log(path, label, error_text)
    local file = io.open(path, "ab")
    if not file then
        return
    end
    file:write(string.format("\n[%s] %s\n%s\n", os.date("%Y-%m-%d %H:%M:%S"), tostring(label), tostring(error_text)))
    file:close()
end

function MangaBridge:runSafely(label, callback)
    local ok, error_text = xpcall(callback, debug.traceback)
    if ok then
        return true
    end
    append_error_log(self.data_dir .. "/plugin-error.log", label, error_text)
    UIManager:show(InfoMessage:new{
        text = _("MangaBridge encountered an error and stayed open. Details were written to /mnt/us/mangabridge/plugin-error.log.\n\n") .. tostring(error_text),
    })
    return false
end

function MangaBridge:init()
    ensure_dir(self.data_dir)
    ensure_dir(self.library_dir)
    ensure_dir(self.cache_dir)
    ensure_dir(self.cover_dir)
    self.settings = LuaSettings:open(self.settings_file)
    self:loadExternalConfig()
    self.ui.menu:registerToMainMenu(self)

    -- If KOReader previously stopped unexpectedly while a MangaBridge chapter
    -- was open, do not leave that series cover as the global sleep screen.
    -- ReaderReady will re-apply it moments later when the current document is
    -- genuinely a MangaBridge chapter.
    if self.settings:readSetting("screensaver_override_active", false) then
        UIManager:nextTick(function()
            local current_path = self.ui and self.ui.document and self.ui.document.file
            if not self:seriesIdFromDocumentPath(current_path) then
                self:restoreScreensaverSettings()
            end
        end)
    end
    local startup_marker = self.data_dir .. "/open-on-start"
    if file_exists(startup_marker) then
        os.remove(startup_marker)
        UIManager:nextTick(function()
            self:runSafely("startup library", function() self:showLibrary(false) end)
        end)
    end
end

function MangaBridge:loadExternalConfig()
    local config_path = self.data_dir .. "/config.lua"
    if not file_exists(config_path) then
        return
    end
    local ok, config = pcall(dofile, config_path)
    if not ok or type(config) ~= "table" then
        return
    end
    local changed = false
    local keys = {
        server_url = true,
        api_token = true,
        username = true,
        password = true,
        profile_username = true,
        progress_sync = true,
        auto_next_chapter = true,
        timeout_seconds = true,
        download_timeout_seconds = true,
        poll_attempts = true,
        series_cover_screensaver = true,
    }
    for key, config_enabled in pairs(keys) do
        if config[key] ~= nil then
            self.settings:saveSetting(key, config[key])
            changed = true
        end
    end
    if changed then
        self.settings:flush()
    end
end

function MangaBridge:addToMainMenu(menu_items)
    menu_items.mangabridge = {
        text = _("MangaBridge"),
        sorting_hint = "more_tools",
        sub_item_table_func = function()
            return self:getMainMenuItems()
        end,
    }
end

function MangaBridge:getMainMenuItems()
    local continue_info = self:continueReadingInfo()
    return {
        {
            text = _("Open MangaBridge library"),
            callback = function() self:showLibrary(false) end,
        },
        {
            text = _("Refresh library from server"),
            callback = function()
                NetworkMgr:runWhenOnline(function() self:showLibrary(true) end)
            end,
        },
        {
            text = self:continueReadingLabel(continue_info),
            enabled_func = function() return self:continueReadingInfo() ~= nil end,
            callback = function() self:openContinueReading() end,
        },
        {
            text = _("Sync reading progress now"),
            enabled_func = function() return self:progressSyncEnabled() end,
            callback = function()
                NetworkMgr:runWhenOnline(function() self:syncPendingProgress(true) end)
            end,
        },
        {
            text = _("Sync reading progress with server"),
            checked_func = function()
                return self.settings:readSetting("progress_sync", true) ~= false
            end,
            callback = function()
                local enabled = self.settings:readSetting("progress_sync", true) ~= false
                self.settings:saveSetting("progress_sync", not enabled)
                self.settings:flush()
            end,
        },
        {
            text = _("Automatically continue to next chapter"),
            checked_func = function()
                return self.settings:readSetting("auto_next_chapter", true) ~= false
            end,
            callback = function()
                local enabled = self.settings:readSetting("auto_next_chapter", true) ~= false
                self.settings:saveSetting("auto_next_chapter", not enabled)
                self.settings:flush()
            end,
        },
        {
            text = _("Connection settings"),
            callback = function() self:showSettings() end,
        },
        {
            text = _("Test connection"),
            callback = function()
                NetworkMgr:runWhenOnline(function() self:testConnection() end)
            end,
        },
        {
            text = _("Use series cover while sleeping"),
            checked_func = function()
                return self.settings:readSetting("series_cover_screensaver", true) ~= false
            end,
            callback = function()
                local enabled = self.settings:readSetting("series_cover_screensaver", true) ~= false
                self.settings:saveSetting("series_cover_screensaver", not enabled)
                self.settings:flush()
                if enabled then
                    self:restoreScreensaverSettings()
                elseif self.ui and self.ui.document then
                    self:applyCoverForDocument(self.ui.document.file)
                end
            end,
        },
        {
            text = _("Controls and safety"),
            callback = function() self:showAbout() end,
        },
    }
end

function MangaBridge:serverUrl()
    local url = trim(self.settings:readSetting("server_url", ""))
    return url:gsub("/+$", "")
end

function MangaBridge:requestHeaders(content_length)
    local headers = {
        ["Accept"] = "application/json",
        ["User-Agent"] = "MangaBridge-KOReader/1.1.1",
        ["Connection"] = "close",
    }
    local profile_username = trim(self.settings:readSetting("profile_username", ""))
    if profile_username ~= "" then
        headers["X-MangaBridge-Profile"] = profile_username
    end
    local token = trim(self.settings:readSetting("api_token", ""))
    if token ~= "" then
        headers["X-MangaDL-Token"] = token
    end
    local username = trim(self.settings:readSetting("username", ""))
    local password = tostring(self.settings:readSetting("password", "") or "")
    if username ~= "" then
        headers["Authorization"] = "Basic " .. mime.b64(username .. ":" .. password)
    end
    if content_length then
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = tostring(content_length)
    end
    return headers
end

function MangaBridge:httpModule(url)
    if url:match("^https://") then
        if not has_https then
            return nil, _("This KOReader build has no HTTPS support. Use a trusted local HTTP address or update KOReader.")
        end
        return https
    end
    return http
end

function MangaBridge:requestJson(method, path, body, timeout_seconds)
    local base = self:serverUrl()
    if base == "" then
        return nil, _("Server URL is not configured."), 0
    end
    local url = base .. path
    local client, module_error = self:httpModule(url)
    if not client then
        return nil, module_error, 0
    end

    local response = {}
    local payload = nil
    if body ~= nil then
        local ok, encoded = pcall(function() return JSON.encode(body) end)
        if not ok then
            return nil, _("Could not encode request."), 0
        end
        payload = encoded
    end

    socket.TIMEOUT = tonumber(timeout_seconds) or tonumber(self.settings:readSetting("timeout_seconds", 45)) or 45
    local request = {
        url = url,
        method = method,
        headers = self:requestHeaders(payload and #payload or nil),
        sink = ltn12.sink.table(response),
    }
    if payload then
        request.source = ltn12.source.string(payload)
    end

    local call_ok, request_ok, code, response_headers, status = pcall(client.request, request)
    if not call_ok then
        return nil, tostring(request_ok), 0
    end
    code = tonumber(code) or 0
    local raw = table.concat(response)
    if not request_ok or code < 200 or code >= 300 then
        local message = status or raw or _("Network request failed.")
        local decoded_ok, decoded = pcall(function() return JSON.decode(raw) end)
        if decoded_ok and type(decoded) == "table" and decoded.error then
            message = tostring(decoded.error)
        end
        return nil, message, code
    end
    if raw == "" then
        return {}, nil, code
    end
    local decoded_ok, decoded = pcall(function() return JSON.decode(raw) end)
    if not decoded_ok or type(decoded) ~= "table" then
        local compact = tostring(raw or ""):gsub("%s+", " "):sub(1, 180)
        local message
        if compact:lower():find("<html", 1, true) or compact:lower():find("<!doctype", 1, true) then
            message = _("The server returned a web page instead of JSON. Check the server URL, authentication settings, and that the MangaBridge server update is installed.")
        elseif compact ~= "" then
            message = _("The server returned invalid JSON: ") .. compact
        else
            message = _("The server returned an empty or invalid JSON response.")
        end
        return nil, message, code
    end
    return decoded, nil, code
end

function MangaBridge:downloadFile(path, destination)
    local base = self:serverUrl()
    local url = path:match("^https?://") and path or (base .. path)
    local client, module_error = self:httpModule(url)
    if not client then
        return false, module_error
    end

    ensure_dir(destination:match("^(.*)/[^/]+$") or self.library_dir)
    local temporary = destination .. ".part"
    os.remove(temporary)
    local file = io.open(temporary, "wb")
    if not file then
        return false, _("Could not create the local chapter file.")
    end

    socket.TIMEOUT = tonumber(self.settings:readSetting("download_timeout_seconds", 300)) or 300
    local call_ok, request_ok, code, response_headers, status = pcall(client.request, {
        url = url,
        method = "GET",
        headers = self:requestHeaders(nil),
        sink = ltn12.sink.file(file),
    })
    if not call_ok then
        os.remove(temporary)
        return false, tostring(request_ok)
    end
    code = tonumber(code) or 0
    if not request_ok or code < 200 or code >= 300 then
        os.remove(temporary)
        return false, status or ("HTTP " .. tostring(code))
    end
    local probe = io.open(temporary, "rb")
    local signature = probe and probe:read(5) or nil
    if probe then
        probe:close()
    end
    if signature ~= "%PDF-" then
        os.remove(temporary)
        return false, _("The downloaded file is not a valid PDF.")
    end
    os.remove(destination)
    if not os.rename(temporary, destination) then
        os.remove(temporary)
        return false, _("Could not finish the local file.")
    end
    return true
end


function MangaBridge:downloadImage(path, destination)
    local base = self:serverUrl()
    local url = path:match("^https?://") and path or (base .. path)
    local client, module_error = self:httpModule(url)
    if not client then
        return false, module_error
    end

    ensure_dir(destination:match("^(.*)/[^/]+$") or self.cover_dir)
    local temporary = destination .. ".part"
    os.remove(temporary)
    local file = io.open(temporary, "wb")
    if not file then
        return false, _("Could not create the local cover file.")
    end

    socket.TIMEOUT = tonumber(self.settings:readSetting("timeout_seconds", 45)) or 45
    local call_ok, request_ok, code, response_headers, status = pcall(client.request, {
        url = url,
        method = "GET",
        headers = self:requestHeaders(nil),
        sink = ltn12.sink.file(file),
    })
    if not call_ok then
        os.remove(temporary)
        return false, tostring(request_ok)
    end
    code = tonumber(code) or 0
    if not request_ok or code < 200 or code >= 300 then
        os.remove(temporary)
        return false, status or ("HTTP " .. tostring(code))
    end

    local probe = io.open(temporary, "rb")
    local signature = probe and probe:read(3) or nil
    if probe then
        probe:close()
    end
    local is_jpeg = signature and #signature == 3
        and signature:byte(1) == 0xFF
        and signature:byte(2) == 0xD8
        and signature:byte(3) == 0xFF
    if not is_jpeg then
        os.remove(temporary)
        return false, _("The downloaded cover is not a valid JPEG image.")
    end

    os.remove(destination)
    if not os.rename(temporary, destination) then
        os.remove(temporary)
        return false, _("Could not finish the local cover file.")
    end
    return true
end

function MangaBridge:coverPath(series_id)
    return self.cover_dir .. "/" .. safe_id(series_id) .. ".jpg"
end

function MangaBridge:coverRevisionKey(series_id)
    return "cover_revision_" .. safe_id(series_id)
end

function MangaBridge:librarySeriesById(series_id)
    local payload = self:getCachedLibrary()
    for row_index, row in ipairs(payload.series or {}) do
        if tostring(row.id) == tostring(series_id) then
            return row
        end
    end
    return nil
end

function MangaBridge:cacheSeriesCover(series, force)
    if self.settings:readSetting("series_cover_screensaver", true) == false then
        return false, _("Series-cover sleep screens are disabled.")
    end
    if not series or not series.id then
        return false, _("Series information is incomplete.")
    end

    local library_row = self:librarySeriesById(series.id)
    local cover_url = trim(series.cover_url or (library_row and library_row.cover_url) or "")
    local server_revision = trim(series.cover_revision or (library_row and library_row.cover_revision) or "")
    local revision_key = self:coverRevisionKey(series.id)
    local cached_revision = trim(self.settings:readSetting(revision_key, "") or "")
    local destination = self:coverPath(series.id)

    -- A new server-side cover revision means the metadata source or conversion
    -- pipeline changed.  Replace the cached JPEG automatically instead of
    -- requiring the user to delete it over USB.
    if file_exists(destination) and not force then
        if server_revision == "" or cached_revision == server_revision then
            return true
        end
    end
    if cover_url == "" then
        return false, _("No metadata cover is available for this series.")
    end
    if force then
        cover_url = cover_url .. (cover_url:find("?", 1, true) and "&refresh=1" or "?refresh=1")
    end

    local ok, err = self:downloadImage(cover_url, destination)
    if ok then
        series.cover_url = series.cover_url or cover_url:gsub("[?&]refresh=1", "")
        series.cover_revision = server_revision ~= "" and server_revision or series.cover_revision
        if server_revision ~= "" then
            self.settings:saveSetting(revision_key, server_revision)
            self.settings:flush()
        end
        self:saveLocalSeriesMetadata(series)
    end
    return ok, err
end

local SCREENSAVER_SETTING_KEYS = {
    "screensaver_type",
    "screensaver_document_cover",
    "screensaver_show_message",
    "screensaver_img_background",
    "screensaver_stretch_images",
    "screensaver_stretch_limit_percentage",
    "screensaver_rotate_auto_for_best_fit",
}

function MangaBridge:captureScreensaverSettings()
    if self.settings:readSetting("screensaver_backup") then
        return
    end
    local backup = {}
    for key_index, key in ipairs(SCREENSAVER_SETTING_KEYS) do
        backup[key] = {
            present = G_reader_settings:has(key),
            value = G_reader_settings:readSetting(key),
        }
    end
    self.settings:saveSetting("screensaver_backup", backup)
    self.settings:flush()
end

function MangaBridge:restoreScreensaverSettings()
    if not self.settings or not self.settings:readSetting("screensaver_override_active", false) then
        return
    end
    local backup = self.settings:readSetting("screensaver_backup", {}) or {}
    for key_index, key in ipairs(SCREENSAVER_SETTING_KEYS) do
        local item = backup[key]
        if item and item.present then
            G_reader_settings:saveSetting(key, item.value)
        else
            G_reader_settings:delSetting(key)
        end
    end
    G_reader_settings:flush()
    self.settings:delSetting("screensaver_backup")
    self.settings:delSetting("screensaver_override_active")
    self.settings:delSetting("active_cover_series_id")
    self.settings:flush()
end

function MangaBridge:progressProfileKey()
    local username = trim(self.settings:readSetting("profile_username", ""))
    if username == "" then
        return "__default__"
    end
    return username:lower()
end

function MangaBridge:progressState()
    if type(self.progress_state) ~= "table" then
        self.progress_state = load_json(self.progress_file, { version = 3, profiles = {} })
    end
    self.progress_state.version = 3
    self.progress_state.profiles = type(self.progress_state.profiles) == "table" and self.progress_state.profiles or {}
    local key = self:progressProfileKey()
    local profile = self.progress_state.profiles[key]
    if type(profile) ~= "table" then
        profile = { series = {} }
        self.progress_state.profiles[key] = profile
    end
    profile.series = type(profile.series) == "table" and profile.series or {}

    -- v3 keeps the canonical continuation pointer inside each series.  The
    -- profile-level pointer is retained only for the global "continue latest"
    -- shortcut.
    for series_key, series in pairs(profile.series) do
        if type(series) == "table" then
            series.id = series.id or series_key
            series.chapters = type(series.chapters) == "table" and series.chapters or {}
            if type(series.last) ~= "table" or not series.last.chapter then
                local newest_chapter = nil
                local newest_time = -1
                for chapter_key, record in pairs(series.chapters) do
                    if type(record) == "table" then
                        local updated = tonumber(record.updated_at) or 0
                        if updated >= newest_time then
                            newest_time = updated
                            newest_chapter = tonumber(chapter_key)
                        end
                    end
                end
                if newest_chapter then
                    series.last = { chapter = newest_chapter, updated_at = math.max(0, newest_time) }
                end
            end
        end
    end
    return self.progress_state, profile
end

function MangaBridge:flushProgressState()
    if type(self.progress_state) == "table" then
        save_json(self.progress_file, self.progress_state)
    end
end

function MangaBridge:documentChapterInfo(path)
    path = tostring(path or "")
    local prefix = self.library_dir .. "/"
    if path:sub(1, #prefix) ~= prefix then
        return nil
    end
    local remainder = path:sub(#prefix + 1)
    local directory_id, filename = remainder:match("^([^/]+)/(.+)$")
    local raw_chapter = filename and filename:match("^chapter%-(%d+[%.%d]*)%.pdf$") or nil
    local chapter_num = raw_chapter and tonumber(raw_chapter) or nil
    if not directory_id or not chapter_num then
        return nil
    end
    local metadata = load_json(self.library_dir .. "/" .. directory_id .. "/series.json", {}) or {}
    local series_id = tostring(metadata.id or directory_id)
    return {
        id = series_id,
        title = tostring(metadata.title or series_id),
        chapter = chapter_num,
        path = path,
        directory_id = directory_id,
    }
end

function MangaBridge:seriesIdFromDocumentPath(path)
    local info = self:documentChapterInfo(path)
    return info and info.id or nil
end

function MangaBridge:chapterProgress(series_id, chapter_num)
    local state, profile = self:progressState()
    local series = profile.series[tostring(series_id)]
    if type(series) ~= "table" or type(series.chapters) ~= "table" then
        return nil
    end
    return series.chapters[chapter_text(chapter_num)]
end

function MangaBridge:updateProgressState(
    state,
    profile,
    series_id,
    title,
    chapter_num,
    page,
    total_pages,
    complete,
    updated_at,
    dirty,
    make_last
)
    local series_key = tostring(series_id)
    local chapter_key = chapter_text(chapter_num)
    local series = profile.series[series_key]
    if type(series) ~= "table" then
        series = { id = series_id, title = title or series_id, chapters = {} }
        profile.series[series_key] = series
    end
    series.id = series_id
    series.title = title or series.title or series_id
    series.chapters = series.chapters or {}
    local current = series.chapters[chapter_key] or {}
    current.page = math.max(tonumber(current.page) or 0, tonumber(page) or 0)
    current.total_pages = math.max(tonumber(current.total_pages) or 0, tonumber(total_pages) or 0)
    current.complete = current.complete == true or complete == true
    if current.total_pages > 0 and current.page >= current.total_pages then
        current.complete = true
    end
    current.updated_at = math.max(tonumber(current.updated_at) or 0, tonumber(updated_at) or os.time())
    if dirty ~= nil then
        current.dirty = dirty == true
    end
    series.chapters[chapter_key] = current
    if make_last then
        local timestamp = tonumber(updated_at) or os.time()
        local existing_series_time = series.last and tonumber(series.last.updated_at) or 0
        if timestamp >= existing_series_time then
            series.last = {
                chapter = chapter_num,
                updated_at = timestamp,
            }
        end
        local existing_time = profile.last and tonumber(profile.last.updated_at) or 0
        if timestamp >= existing_time then
            profile.last = {
                series_id = series_id,
                title = series.title,
                chapter = chapter_num,
                updated_at = timestamp,
            }
        end
    end
    return current
end

function MangaBridge:saveLocalProgress(info, page, total_pages, complete)
    if not info then
        return nil
    end
    local state, profile = self:progressState()
    local now = os.time()
    local record = self:updateProgressState(
        state,
        profile,
        info.id,
        info.title,
        info.chapter,
        page,
        total_pages,
        complete,
        now,
        true,
        true
    )
    self:flushProgressState()
    return record
end

function MangaBridge:mergeServerProgressRecord(series_id, title, chapter_num, payload, make_last)
    if not payload or chapter_num == nil then
        return nil
    end
    local state, profile = self:progressState()
    local series = profile.series[tostring(series_id)]
    local existing = series and series.chapters and series.chapters[chapter_text(chapter_num)] or nil
    local server_page = tonumber(payload.last_page) or 0
    local server_complete = payload.is_read == true or payload.read_on_server == true
    local can_clear_dirty = true
    if existing and existing.dirty == true then
        can_clear_dirty = server_page >= (tonumber(existing.page) or 0)
            and (existing.complete ~= true or server_complete)
    end
    local epoch = tonumber(payload.updated_epoch or payload.progress_updated_epoch or 0) or 0
    if epoch <= 0 then
        epoch = os.time()
    end
    local record = self:updateProgressState(
        state,
        profile,
        series_id,
        title,
        chapter_num,
        server_page,
        tonumber(payload.total_pages) or 0,
        server_complete,
        epoch,
        can_clear_dirty and false or nil,
        make_last == true
    )
    self:flushProgressState()
    return record
end

function MangaBridge:mergeServerLibraryProgress(payload)
    if type(payload) ~= "table" or type(payload.series) ~= "table" then
        return
    end
    local newest = nil
    for _, series in ipairs(payload.series) do
        local chapter_num = tonumber(series.last_chapter)
        local epoch = tonumber(series.last_read_epoch) or 0
        if chapter_num and (tonumber(series.last_page) or 0) > 0 then
            self:mergeServerProgressRecord(series.id, series.title, chapter_num, {
                last_page = series.last_page,
                total_pages = series.last_total_pages,
                is_read = (tonumber(series.last_total_pages) or 0) > 0
                    and (tonumber(series.last_page) or 0) >= (tonumber(series.last_total_pages) or 0),
                updated_epoch = epoch,
            }, false)
            if not newest or epoch > newest.epoch then
                newest = {
                    series = series,
                    chapter = tonumber(series.continue_chapter) or chapter_num,
                    epoch = epoch,
                }
            end
        end
    end
    if newest and newest.epoch > 0 then
        local state, profile = self:progressState()
        local last_time = profile.last and tonumber(profile.last.updated_at) or 0
        if newest.epoch >= last_time then
            profile.last = {
                series_id = newest.series.id,
                title = newest.series.title,
                chapter = newest.chapter,
                updated_at = newest.epoch,
            }
            self:flushProgressState()
        end
    end
end

function MangaBridge:mergeServerSeriesProgress(series)
    if type(series) ~= "table" then
        return
    end
    for _, chapter in ipairs(series.chapters or {}) do
        local page = tonumber(chapter.last_page) or 0
        local pages = tonumber(chapter.total_pages) or 0
        if page > 0 or chapter.read_on_server then
            self:mergeServerProgressRecord(series.id, series.title, chapter.number, {
                last_page = page,
                total_pages = pages,
                read_on_server = chapter.read_on_server,
                progress_updated_epoch = chapter.progress_updated_epoch,
            }, false)
        end
    end
    local last_chapter = tonumber(series.last_chapter)
    if last_chapter and (tonumber(series.last_page) or 0) > 0 then
        self:mergeServerProgressRecord(series.id, series.title, last_chapter, {
            last_page = series.last_page,
            total_pages = series.last_total_pages,
            updated_epoch = series.last_read_epoch,
        }, false)
    end
    local continue_chapter = tonumber(series.continue_chapter) or last_chapter
    if continue_chapter and (tonumber(series.last_read_epoch) or 0) > 0 then
        self:setContinueTarget(series.id, series.title, continue_chapter, tonumber(series.last_read_epoch) or 0)
    end
end

function MangaBridge:seriesContinueReadingInfo(series_id, title)
    local state, profile = self:progressState()
    local series = profile.series[tostring(series_id)]
    if type(series) ~= "table" or type(series.last) ~= "table" or not series.last.chapter then
        return nil
    end
    local chapter_num = tonumber(series.last.chapter)
    if not chapter_num then
        return nil
    end
    local record = self:chapterProgress(series_id, chapter_num) or {}
    return {
        id = series_id,
        title = title or series.title or series_id,
        chapter = chapter_num,
        page = tonumber(record.page) or 0,
        total_pages = tonumber(record.total_pages) or 0,
        complete = record.complete == true,
        path = self:chapterPath(series_id, chapter_num),
    }
end

function MangaBridge:continueReadingInfo()
    local state, profile = self:progressState()
    local last = profile.last
    if type(last) ~= "table" or not last.series_id or not last.chapter then
        local legacy_path = self.settings:readSetting("last_file")
        local legacy = legacy_path and self:documentChapterInfo(legacy_path) or nil
        if legacy then
            return {
                id = legacy.id,
                title = legacy.title,
                chapter = legacy.chapter,
                page = 0,
                total_pages = 0,
                complete = false,
                path = legacy.path,
            }
        end
        return nil
    end
    local record = self:chapterProgress(last.series_id, last.chapter) or {}
    return {
        id = last.series_id,
        title = last.title or last.series_id,
        chapter = tonumber(last.chapter),
        page = tonumber(record.page) or 0,
        total_pages = tonumber(record.total_pages) or 0,
        complete = record.complete == true,
        path = self:chapterPath(last.series_id, last.chapter),
    }
end

function MangaBridge:continueReadingLabel(info)
    if not info then
        return _("Continue reading")
    end
    local progress = ""
    if info.page > 0 and info.total_pages > 0 then
        progress = string.format(" - %d/%d", info.page, info.total_pages)
    elseif info.page > 0 then
        progress = string.format(" - p%d", info.page)
    end
    return string.format(_("Continue %s - Chapter %s%s"), tostring(info.title), chapter_text(info.chapter), progress)
end

function MangaBridge:openReadingInfo(info)
    if not info then
        return false
    end
    if file_exists(info.path) then
        self:openDocument(info.path)
        return true
    end
    UIManager:show(ConfirmBox:new{
        text = string.format(_("Chapter %s is not stored on this Kindle. Download it and continue reading?"), chapter_text(info.chapter)),
        ok_text = _("Download"),
        ok_callback = function()
            NetworkMgr:runWhenOnline(function()
                local series = self:getCachedSeries(info.id) or { id = info.id, title = info.title }
                self:prepareSingleChapter(series, info.chapter)
            end)
        end,
    })
    return true
end

function MangaBridge:openContinueReading()
    local info = self:continueReadingInfo()
    if not info then
        UIManager:show(InfoMessage:new{ text = _("No MangaBridge reading progress has been saved yet.") })
        return
    end
    self:openReadingInfo(info)
end

function MangaBridge:currentPageState(page_override)
    local page = tonumber(page_override)
    local total_pages = nil
    if self.ui and self.ui.paging then
        page = page or tonumber(self.ui.paging.current_page)
        total_pages = tonumber(self.ui.paging.number_of_pages)
    end
    if not total_pages and self.ui and self.ui.document and self.ui.document.getPageCount then
        local ok, count = pcall(function() return self.ui.document:getPageCount() end)
        if ok then
            total_pages = tonumber(count)
        end
    end
    return page, total_pages
end

function MangaBridge:recordCurrentProgress(page_override)
    local path = self.ui and self.ui.document and self.ui.document.file
    local info = self:documentChapterInfo(path)
    if not info then
        return nil, nil
    end
    local page, total_pages = self:currentPageState(page_override)
    if not page then
        return info, nil
    end
    local complete = total_pages and total_pages > 0 and page >= total_pages or false
    return info, self:saveLocalProgress(info, page, total_pages or 0, complete)
end

function MangaBridge:networkIsConnected()
    if type(NetworkMgr.isConnected) == "function" then
        local ok, connected = pcall(function() return NetworkMgr:isConnected() end)
        if ok then
            return connected == true
        end
    end
    if type(NetworkMgr.isOnline) == "function" then
        local ok, online = pcall(function() return NetworkMgr:isOnline() end)
        return ok and online == true
    end
    return false
end

function MangaBridge:progressSyncEnabled()
    return self.settings:readSetting("progress_sync", true) ~= false and self:serverUrl() ~= ""
end

function MangaBridge:syncProgressRecord(info, record, restore_if_ahead)
    if not info or not record or not self:progressSyncEnabled() then
        return nil, nil
    end
    local path = "/api/kindle/v1/series/" .. tostring(info.id)
        .. "/chapter/chapter-" .. chapter_text(info.chapter) .. "/progress"
    local payload, err = self:requestJson("POST", path, {
        page = tonumber(record.page) or 0,
        total_pages = tonumber(record.total_pages) or 0,
        complete = record.complete == true,
    }, 8)
    if not payload then
        return nil, err
    end
    local merged = self:mergeServerProgressRecord(info.id, info.title, info.chapter, payload, true)
    if merged then
        merged.dirty = false
        self:flushProgressState()
    end
    if restore_if_ahead then
        local current_path = self.ui and self.ui.document and self.ui.document.file
        local current_info = self:documentChapterInfo(current_path)
        local current_page, total_pages = self:currentPageState()
        local target = tonumber(payload.last_page) or 0
        if current_info and tostring(current_info.id) == tostring(info.id)
            and tonumber(current_info.chapter) == tonumber(info.chapter)
            and current_page and target > current_page
            and (not total_pages or target <= total_pages) then
            self.ui:handleEvent(Event:new("GotoPage", target))
        end
    end
    return payload, nil
end

function MangaBridge:syncPendingProgress(interactive)
    if not self:progressSyncEnabled() then
        return true
    end
    local state, profile = self:progressState()
    local pending = {}
    for _, series in pairs(profile.series or {}) do
        for chapter_key, record in pairs(series.chapters or {}) do
            if record.dirty == true and (tonumber(record.page) or 0) > 0 then
                table.insert(pending, {
                    info = {
                        id = series.id,
                        title = series.title,
                        chapter = tonumber(chapter_key),
                    },
                    record = record,
                })
            end
        end
    end
    table.sort(pending, function(a, b)
        return (tonumber(a.record.updated_at) or 0) < (tonumber(b.record.updated_at) or 0)
    end)
    for _, item in ipairs(pending) do
        local _, err = self:syncProgressRecord(item.info, item.record, false)
        if err then
            if interactive then
                UIManager:show(InfoMessage:new{ text = _("Progress sync failed: ") .. tostring(err) })
            end
            return false, err
        end
    end
    if interactive then
        UIManager:show(InfoMessage:new{
            text = string.format(_("Reading progress synced for profile %s."), trim(self.settings:readSetting("profile_username", "")) ~= "" and trim(self.settings:readSetting("profile_username", "")) or _("server default")),
        })
    end
    return true
end

function MangaBridge:onPageUpdate(page)
    self:recordCurrentProgress(page)
end

function MangaBridge:onReaderReady()
    local path = self.ui and self.ui.document and self.ui.document.file
    self:applyCoverForDocument(path)
    local info, record = self:recordCurrentProgress()
    if info and record and self:networkIsConnected() and self:progressSyncEnabled() then
        UIManager:nextTick(function()
            self:runSafely("progress sync on open", function()
                local latest = self:chapterProgress(info.id, info.chapter)
                self:syncProgressRecord(info, latest or record, true)
                self:syncPendingProgress(false)
            end)
        end)
    end
end

function MangaBridge:onCloseDocument()
    local info, record = self:recordCurrentProgress()
    self:restoreScreensaverSettings()
    if info and record and self:networkIsConnected() and self:progressSyncEnabled() then
        UIManager:nextTick(function()
            self:runSafely("progress sync on close", function()
                self:syncPendingProgress(false)
            end)
        end)
    end
end

function MangaBridge:setContinueTarget(series_id, title, chapter_num, updated_at)
    if not series_id or not chapter_num then
        return
    end
    local state, profile = self:progressState()
    local series_key = tostring(series_id)
    local series = profile.series[series_key]
    if type(series) ~= "table" then
        series = { id = series_id, title = title or series_id, chapters = {} }
        profile.series[series_key] = series
    end
    series.id = series_id
    series.title = title or series.title or series_id
    series.chapters = series.chapters or {}
    local timestamp = tonumber(updated_at) or os.time()
    local existing_series_time = series.last and tonumber(series.last.updated_at) or 0
    if timestamp >= existing_series_time then
        series.last = { chapter = chapter_num, updated_at = timestamp }
    end
    local existing_time = profile.last and tonumber(profile.last.updated_at) or 0
    if timestamp >= existing_time then
        profile.last = {
            series_id = series_id,
            title = series.title,
            chapter = chapter_num,
            updated_at = timestamp,
        }
    end
    self:flushProgressState()
end

function MangaBridge:nextChapter(series, current_chapter)
    local candidate = nil
    for _, chapter in ipairs((series and series.chapters) or {}) do
        local number = tonumber(chapter.number)
        if number and number > tonumber(current_chapter) and (not candidate or number < candidate) then
            candidate = number
        end
    end
    return candidate
end

function MangaBridge:finishAndAdvance(info)
    if not info then
        return false
    end
    local page, total_pages = self:currentPageState()
    local record = self:saveLocalProgress(info, page or 0, total_pages or 0, true)
    if self:networkIsConnected() and self:progressSyncEnabled() then
        self:syncProgressRecord(info, record, false)
    end

    local series = self:getCachedSeries(info.id) or { id = info.id, title = info.title, chapters = {} }
    local next_chapter = self:nextChapter(series, info.chapter)
    if not next_chapter and self:networkIsConnected() then
        local refreshed = self:refreshSeries(info.id, false)
        if refreshed then
            series = refreshed
            next_chapter = self:nextChapter(series, info.chapter)
        end
    end
    if not next_chapter then
        UIManager:show(InfoMessage:new{ text = _("No newer chapter is currently known. Refresh the series when online to check for updates.") })
        return true
    end

    self:setContinueTarget(info.id, series.title or info.title, next_chapter)
    local next_path = self:chapterPath(info.id, next_chapter)
    if file_exists(next_path) then
        UIManager:nextTick(function()
            self:openDocument(next_path)
        end)
        return true
    end

    UIManager:nextTick(function()
        UIManager:show(ConfirmBox:new{
            text = string.format(_("Chapter %s is next but is not downloaded. Download it now and continue?"), chapter_text(next_chapter)),
            ok_text = _("Download"),
            ok_callback = function()
                NetworkMgr:runWhenOnline(function()
                    self:prepareSingleChapter(series, next_chapter)
                end)
            end,
        })
    end)
    return true
end

function MangaBridge:onMangaBridgeForward()
    local path = self.ui and self.ui.document and self.ui.document.file
    local info = self:documentChapterInfo(path)
    local page, total_pages = self:currentPageState()
    if info and self.settings:readSetting("auto_next_chapter", true) ~= false
        and page and total_pages and total_pages > 0 and page >= total_pages then
        return self:finishAndAdvance(info)
    end

    -- This plugin owns the Kindle forward keys so it can detect the extra
    -- press on the last page.  Forward ordinary presses back into KOReader's
    -- native semantic paging event instead of relying on key-event fallthrough.
    if self.ui then
        self.ui:handleEvent(Event:new("GotoViewRel", 1))
        return true
    end
    return false
end

function MangaBridge:libraryCachePath()
    return self.cache_dir .. "/library-" .. safe_id(self:progressProfileKey()) .. ".json"
end

function MangaBridge:seriesCachePath(series_id)
    return self.cache_dir .. "/series-" .. safe_id(self:progressProfileKey()) .. "-" .. safe_id(series_id) .. ".json"
end

function MangaBridge:seriesDir(series_id)
    return self.library_dir .. "/" .. safe_id(series_id)
end

function MangaBridge:chapterPath(series_id, chapter_num)
    return self:seriesDir(series_id) .. "/chapter-" .. chapter_text(chapter_num) .. ".pdf"
end

function MangaBridge:localSeriesMetadataPath(series_id)
    return self:seriesDir(series_id) .. "/series.json"
end

function MangaBridge:saveLocalSeriesMetadata(series)
    if not series or not series.id then
        return
    end
    ensure_dir(self:seriesDir(series.id))
    save_json(self:localSeriesMetadataPath(series.id), {
        id = series.id,
        title = series.title or series.id,
        cover_url = series.cover_url,
    })
end

function MangaBridge:localSeriesRows()
    local rows = {}
    if lfs.attributes(self.library_dir, "mode") ~= "directory" then
        return rows
    end
    local ok, iterator, state = pcall(lfs.dir, self.library_dir)
    if not ok or not iterator then
        return rows
    end
    for entry in iterator, state do
        if entry ~= "." and entry ~= ".." then
            local metadata = load_json(self.library_dir .. "/" .. entry .. "/series.json", nil)
            if metadata and metadata.id then
                metadata.available_count = metadata.available_count or 0
                table.insert(rows, metadata)
            end
        end
    end
    return rows
end

function MangaBridge:localChapterNumbers(series_id)
    local directory = self:seriesDir(series_id)
    local numbers = {}
    if lfs.attributes(directory, "mode") ~= "directory" then
        return numbers
    end
    local ok, iterator, state = pcall(lfs.dir, directory)
    if not ok or not iterator then
        return numbers
    end
    for entry in iterator, state do
        local raw = entry:match("^chapter%-(%d+[%.%d]*)%.pdf$")
        local number = raw and tonumber(raw) or nil
        if number then
            table.insert(numbers, number)
        end
    end
    table.sort(numbers, function(a, b) return a > b end)
    return numbers
end

function MangaBridge:countLocalChapters(series_id)
    return #self:localChapterNumbers(series_id)
end

function MangaBridge:mergeLocalChapters(series)
    series.chapters = series.chapters or {}
    local present = {}
    for chapter_index, chapter in ipairs(series.chapters) do
        present[tostring(chapter.number)] = true
    end
    for local_index, number in ipairs(self:localChapterNumbers(series.id)) do
        if not present[tostring(number)] then
            table.insert(series.chapters, {
                number = number,
                downloaded_on_server = false,
                read_on_server = false,
            })
        end
    end
    table.sort(series.chapters, function(a, b)
        return (tonumber(a.number) or 0) > (tonumber(b.number) or 0)
    end)
    return series
end

function MangaBridge:getLocalSeries(series_id)
    local metadata = load_json(self:localSeriesMetadataPath(series_id), nil)
    if not metadata then
        return nil
    end
    metadata.id = metadata.id or series_id
    metadata.title = metadata.title or series_id
    metadata.chapters = {}
    return self:mergeLocalChapters(metadata)
end

function MangaBridge:getCachedLibrary()
    return load_json(self:libraryCachePath(), { series = {} })
end

function MangaBridge:getCachedSeries(series_id)
    local series = load_json(self:seriesCachePath(series_id), nil)
    if series then
        return self:mergeLocalChapters(series)
    end
    return self:getLocalSeries(series_id)
end

function MangaBridge:refreshLibrary()
    if self:networkIsConnected() and self:progressSyncEnabled() then
        self:syncPendingProgress(false)
    end
    local payload, err = self:requestJson("GET", "/api/kindle/v1/library")
    if not payload then
        return nil, err
    end
    self:mergeServerLibraryProgress(payload)
    save_json(self:libraryCachePath(), payload)
    return payload
end

function MangaBridge:refreshSeries(series_id, force)
    if self:networkIsConnected() and self:progressSyncEnabled() then
        self:syncPendingProgress(false)
    end
    local suffix = force and "?refresh=1" or ""
    local payload, err = self:requestJson("GET", "/api/kindle/v1/series/" .. tostring(series_id) .. suffix)
    if not payload then
        return nil, err
    end
    self:mergeServerSeriesProgress(payload)
    save_json(self:seriesCachePath(series_id), payload)
    self:cacheSeriesCover(payload, false)
    return self:mergeLocalChapters(payload)
end

function MangaBridge:showLibrary(force_refresh)
    local payload
    local refresh_error
    if force_refresh then
        payload, refresh_error = self:refreshLibrary()
    else
        payload = self:getCachedLibrary()
        if not payload or type(payload.series) ~= "table" or #payload.series == 0 then
            local server = self:serverUrl()
            if server ~= "" then
                payload, refresh_error = self:refreshLibrary()
            end
        end
    end
    payload = payload or { series = {} }
    payload.series = payload.series or {}
    self:mergeServerLibraryProgress(payload)
    local known = {}
    for series_index, series in ipairs(payload.series or {}) do
        known[tostring(series.id)] = true
    end
    for local_series_index, local_series in ipairs(self:localSeriesRows()) do
        if not known[tostring(local_series.id)] then
            table.insert(payload.series, local_series)
        end
    end
    table.sort(payload.series, function(a, b)
        return tostring(a.title or a.id):lower() < tostring(b.title or b.id):lower()
    end)

    local items = {}
    local continue_info = self:continueReadingInfo()
    if continue_info then
        table.insert(items, {
            text = self:continueReadingLabel(continue_info),
            mandatory = file_exists(continue_info.path) and _("offline") or _("download"),
            callback = function() self:openContinueReading() end,
        })
    end
    table.insert(items, {
        text = _("Refresh from server"),
        mandatory = refresh_error and _("failed") or nil,
        callback = function()
            NetworkMgr:runWhenOnline(function() self:showLibrary(true) end)
        end,
    })

    for series_index, series in ipairs(payload.series or {}) do
        local item_series_id = series.id
        local item_series_title = tostring(series.title or series.id)
        local local_count = self:countLocalChapters(item_series_id)
        local available_count = tonumber(series.available_count) or 0
        local marker = series.watching and "*" or ""
        table.insert(items, {
            text = item_series_title,
            mandatory = string.format("%d/%d%s", local_count, available_count, marker),
            callback = function() self:showSeries(item_series_id, false) end,
        })
    end

    if #(payload.series or {}) == 0 then
        table.insert(items, {
            text = refresh_error or _("No cached series. Connect to the server and refresh."),
            select_enabled = false,
        })
    end
    table.insert(items, {
        text = _("Connection settings"),
        callback = function() self:showSettings() end,
    })

    local menu = MangaMenu:new{
        title = _("MangaBridge library"),
        item_table = items,
        is_popout = false,
        is_borderless = true,
        title_bar_fm_style = true,
    }
    UIManager:show(menu)
    if refresh_error then
        UIManager:show(InfoMessage:new{ text = _("Using offline cache. Server error: ") .. tostring(refresh_error) })
    end
end

function MangaBridge:showSeries(series_id, force_refresh)
    local series
    local refresh_error
    if force_refresh then
        series, refresh_error = self:refreshSeries(series_id, true)
    else
        series = self:getCachedSeries(series_id)
        if not series then
            series, refresh_error = self:refreshSeries(series_id, false)
        end
    end
    if not series then
        UIManager:show(InfoMessage:new{ text = refresh_error or _("Series information is unavailable offline.") })
        return
    end
    self:mergeServerSeriesProgress(series)
    if not series.cover_url then
        local library_row = self:librarySeriesById(series_id)
        series.cover_url = library_row and library_row.cover_url or nil
    end
    self:saveLocalSeriesMetadata(series)

    local cover_cached = file_exists(self:coverPath(series.id))
    local series_continue = self:seriesContinueReadingInfo(series.id, series.title)
    local items = {}
    if series_continue then
        local progress = ""
        if series_continue.page > 0 and series_continue.total_pages > 0 then
            progress = string.format(" - %d/%d", series_continue.page, series_continue.total_pages)
        elseif series_continue.page > 0 then
            progress = string.format(" - p%d", series_continue.page)
        end
        table.insert(items, {
            text = string.format(_("Continue chapter %s%s"), chapter_text(series_continue.chapter), progress),
            mandatory = file_exists(series_continue.path) and _("offline") or _("download"),
            callback = function() self:openReadingInfo(self:seriesContinueReadingInfo(series.id, series.title)) end,
        })
    end
    table.insert(items, {
        text = _("Refresh chapter list"),
        callback = function()
            NetworkMgr:runWhenOnline(function() self:showSeries(series_id, true) end)
        end,
    })
    table.insert(items, {
        text = cover_cached and _("Update series sleep cover") or _("Download series sleep cover"),
        mandatory = cover_cached and _("cached") or _("missing"),
        enabled_func = function() return trim(series.cover_url or "") ~= "" end,
        callback = function()
            NetworkMgr:runWhenOnline(function()
                Trapper:wrap(function()
                    Trapper:info(_("Downloading series cover..."))
                    local ok, cover_error = self:cacheSeriesCover(series, true)
                    Trapper:clear()
                    Trapper:reset()
                    if not ok then
                        UIManager:show(InfoMessage:new{ text = tostring(cover_error) })
                        return
                    end
                    local current_path = self.ui and self.ui.document and self.ui.document.file
                    if current_path then
                        self:applyCoverForDocument(current_path)
                    end
                    self:showSeries(series_id, false)
                end)
            end)
        end,
    })
    table.insert(items, {
        text = _("Download chapter range"),
        callback = function() self:showRangeDialog(series) end,
    })

    for chapter_index, chapter in ipairs(series.chapters or {}) do
        local chapter_number = chapter.number
        local chapter_path = self:chapterPath(series.id, chapter_number)
        local chapter_series = series
        local offline = file_exists(chapter_path)
        local progress = self:chapterProgress(series.id, chapter_number) or {}
        local state
        if progress.complete == true then
            state = _("read")
        elseif (tonumber(progress.page) or 0) > 0 and (tonumber(progress.total_pages) or 0) > 0 then
            state = string.format("%d/%d", tonumber(progress.page), tonumber(progress.total_pages))
        elseif (tonumber(progress.page) or 0) > 0 then
            state = string.format("p%d", tonumber(progress.page))
        elseif offline then
            state = _("offline")
        elseif chapter.downloaded_on_server then
            state = _("server")
        else
            state = _("get")
        end
        table.insert(items, {
            text = _("Chapter ") .. chapter_text(chapter_number),
            mandatory = state,
            callback = function()
                if file_exists(chapter_path) then
                    self:openDocument(chapter_path)
                else
                    NetworkMgr:runWhenOnline(function()
                        self:prepareSingleChapter(chapter_series, chapter_number)
                    end)
                end
            end,
            hold_callback = function()
                if file_exists(chapter_path) then
                    UIManager:show(ConfirmBox:new{
                        text = _("Delete this offline chapter from the Kindle? The server copy is not affected."),
                        ok_text = _("Delete"),
                        ok_callback = function()
                            os.remove(chapter_path)
                            self:showSeries(chapter_series.id, false)
                        end,
                    })
                end
            end,
        })
    end

    if #(series.chapters or {}) == 0 then
        table.insert(items, {
            text = refresh_error or _("No chapter information is cached."),
            select_enabled = false,
        })
    end

    local menu = MangaMenu:new{
        title = tostring(series.title or series.id),
        item_table = items,
        is_popout = false,
        is_borderless = true,
        title_bar_fm_style = true,
    }
    UIManager:show(menu)
end

function MangaBridge:waitForJob(job)
    local job_id = job and job.id
    if not job_id then
        return nil, _("The server did not return a job ID.")
    end
    local attempts = 0
    local max_attempts = tonumber(self.settings:readSetting("poll_attempts", 400)) or 400
    while attempts < max_attempts do
        attempts = attempts + 1
        local payload, err = self:requestJson("GET", "/api/kindle/v1/jobs/" .. tostring(job_id))
        if not payload then
            return nil, err
        end
        local message = tostring(payload.message or payload.status or _("Working"))
        local progress = ""
        if tonumber(payload.progress_total) and tonumber(payload.progress_total) > 0 then
            progress = string.format("\n%d/%d", tonumber(payload.progress_current) or 0, tonumber(payload.progress_total))
        end
        if not Trapper:info(message .. progress, true) then
            return nil, _("Cancelled")
        end
        if payload.status == "done" then
            return payload
        elseif payload.status == "error" then
            return nil, tostring(payload.error or payload.message or _("Server job failed."))
        end
        socket.sleep(1.5)
    end
    return nil, _("Timed out waiting for the server.")
end

function MangaBridge:downloadPreparedFiles(series, files)
    self:saveLocalSeriesMetadata(series)
    self:cacheSeriesCover(series, false)
    local first_path = nil
    for index, file_info in ipairs(files or {}) do
        local number = file_info.chapter
        local destination = self:chapterPath(series.id, number)
        ensure_dir(self:seriesDir(series.id))
        if not file_exists(destination) then
            local expected_size = tonumber(file_info.size)
            local free_bytes = free_space_bytes()
            local reserve = 20 * 1024 * 1024
            if expected_size and free_bytes and free_bytes < expected_size + reserve then
                return nil, _("Not enough free Kindle storage for this chapter.")
            end
            if not Trapper:info(string.format(_("Downloading chapter %s (%d/%d)"), chapter_text(number), index, #files), true) then
                return nil, _("Cancelled")
            end
            local ok, err = self:downloadFile(file_info.download_url, destination)
            if not ok then
                return nil, err
            end
        end
        first_path = first_path or destination
    end
    return first_path
end

function MangaBridge:prepareSingleChapter(series, chapter_num)
    Trapper:wrap(function()
        Trapper:setPausedText(_("Chapter download paused"), _("Cancel"), _("Continue"))
        if not Trapper:info(_("Asking the server to prepare the chapter...")) then
            return
        end
        local payload, err = self:requestJson(
            "POST",
            "/api/kindle/v1/series/" .. tostring(series.id) .. "/chapter/chapter-" .. chapter_text(chapter_num) .. "/prepare",
            {}
        )
        if not payload then
            Trapper:clear()
            UIManager:show(InfoMessage:new{ text = tostring(err) })
            return
        end

        local files
        if payload.status == "ready" then
            files = { payload }
        else
            local finished, job_error = self:waitForJob(payload.job)
            if not finished then
                Trapper:clear()
                UIManager:show(InfoMessage:new{ text = tostring(job_error) })
                return
            end
            files = finished.result and finished.result.files or {}
        end
        local first_path, download_error = self:downloadPreparedFiles(series, files)
        Trapper:clear()
        Trapper:reset()
        if download_error then
            UIManager:show(InfoMessage:new{ text = tostring(download_error) })
            return
        end
        if first_path then
            self:openDocument(first_path)
        end
    end)
end

function MangaBridge:showRangeDialog(series)
    local dialog
    dialog = InputDialog:new{
        title = _("Download chapter range"),
        input = "",
        input_hint = _("Examples: 1-10, 1,3,7.5, or all"),
        description = _("The server downloads missing chapters, prepares PDF files, then the Kindle stores them for offline reading."),
        buttons = {
            {
                {
                    text = _("Cancel"),
                    id = "close",
                    callback = function() UIManager:close(dialog) end,
                },
                {
                    text = _("Download"),
                    is_enter_default = true,
                    callback = function()
                        local chapter_range = trim(dialog:getInputText())
                        if chapter_range == "" then
                            return
                        end
                        UIManager:close(dialog)
                        NetworkMgr:runWhenOnline(function()
                            self:prepareRange(series, chapter_range)
                        end)
                    end,
                },
            },
        },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function MangaBridge:prepareRange(series, chapter_range)
    Trapper:wrap(function()
        Trapper:setPausedText(_("Bulk download paused"), _("Cancel"), _("Continue"))
        if not Trapper:info(_("Preparing chapter range on the server...")) then
            return
        end
        local payload, err = self:requestJson(
            "POST",
            "/api/kindle/v1/series/" .. tostring(series.id) .. "/bulk",
            { chapter_range = chapter_range }
        )
        if not payload then
            Trapper:clear()
            UIManager:show(InfoMessage:new{ text = tostring(err) })
            return
        end
        local finished, job_error = self:waitForJob(payload.job)
        if not finished then
            Trapper:clear()
            UIManager:show(InfoMessage:new{ text = tostring(job_error) })
            return
        end
        local files = finished.result and finished.result.files or {}
        local first_path, download_error = self:downloadPreparedFiles(series, files)
        Trapper:clear()
        if download_error then
            UIManager:show(InfoMessage:new{ text = tostring(download_error) })
            return
        end
        if first_path then
            local read_now = Trapper:confirm(
                string.format(_("Downloaded %d chapter(s). Open the first one now?"), #files),
                _("Later"),
                _("Read")
            )
            Trapper:reset()
            if read_now then
                self:openDocument(first_path)
            else
                self:showSeries(series.id, false)
            end
        else
            Trapper:reset()
        end
    end)
end

function MangaBridge:openDocument(path)
    if not file_exists(path) then
        UIManager:show(InfoMessage:new{ text = _("The local chapter file is missing.") })
        return
    end
    self.settings:saveSetting("last_file", path)
    self.settings:flush()
    if self.ui.document then
        self.ui:switchDocument(path)
    else
        self.ui:openFile(path)
    end
end

function MangaBridge:showSettings()
    local dialog
    dialog = MultiInputDialog:new{
        title = _("MangaBridge connection"),
        fields = {
            {
                description = _("Server URL. For Kindle 4, a trusted LAN HTTP address is the most compatible option."),
                text = self:serverUrl(),
                hint = "http://192.168.1.10:8095",
            },
            {
                description = _("Kindle API token (recommended)"),
                text = tostring(self.settings:readSetting("api_token", "") or ""),
                text_type = "password",
                hint = _("Token"),
            },
            {
                description = _("MangaBridge profile username for library and progress sync"),
                text = tostring(self.settings:readSetting("profile_username", "") or ""),
                hint = _("Profile username"),
            },
            {
                description = _("Basic-auth username (optional)"),
                text = tostring(self.settings:readSetting("username", "") or ""),
                hint = _("Username"),
            },
            {
                description = _("Basic-auth password (optional)"),
                text = tostring(self.settings:readSetting("password", "") or ""),
                text_type = "password",
                hint = _("Password"),
            },
        },
        buttons = {
            {
                {
                    text = _("Cancel"),
                    id = "close",
                    callback = function() UIManager:close(dialog) end,
                },
                {
                    text = _("Save"),
                    is_enter_default = true,
                    callback = function()
                        local fields = dialog:getFields()
                        local server_url = trim(fields[1]):gsub("/+$", "")
                        if server_url ~= "" and not server_url:match("^https?://") then
                            server_url = "http://" .. server_url
                        end
                        local previous_profile = self:progressProfileKey()
                        self.settings:saveSetting("server_url", server_url)
                        self.settings:saveSetting("api_token", trim(fields[2]))
                        self.settings:saveSetting("profile_username", trim(fields[3]))
                        self.settings:saveSetting("username", trim(fields[4]))
                        self.settings:saveSetting("password", tostring(fields[5] or ""))
                        self.settings:flush()
                        if previous_profile ~= self:progressProfileKey() then
                            self.progress_state = nil
                        end
                        UIManager:close(dialog)
                        NetworkMgr:runWhenOnline(function() self:testConnection() end)
                    end,
                },
            },
        },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function MangaBridge:testConnection()
    local payload, err = self:requestJson("GET", "/api/kindle/v1/ping")
    if not payload then
        UIManager:show(InfoMessage:new{ text = _("Connection failed: ") .. tostring(err) })
        return
    end
    local profile = payload.kindle_profile or {}
    UIManager:show(InfoMessage:new{
        text = string.format(
            _("Connected to %s. MangaBridge profile: %s. Kindle PDF profile: %sx%s maximum, JPEG quality %s."),
            tostring(payload.server or "MangaDL"),
            tostring(payload.selected_profile or "?"),
            tostring(profile.width or "?"),
            tostring(profile.max_height or "?"),
            tostring(profile.jpeg_quality or "?")
        ),
    })
end

function MangaBridge:showAbout()
    UIManager:show(InfoMessage:new{
        text = _([[MangaBridge stores downloaded chapters under /mnt/us/mangabridge and keeps reading progress locally per configured MangaBridge profile.

In lists:
- Chapter rows show page progress or "read".
- Page-turn buttons change menu pages.
- The 5-way selects items.
- Right/hold on an offline chapter offers deletion.

While reading, KOReader handles normal page turns. On the last page, pressing forward opens the next local chapter automatically. If it is not local, MangaBridge asks before downloading it.

When progress sync is enabled, MangaBridge uploads pending local progress whenever it is already online and merges it with the configured server profile. Automatic sync never turns Wi-Fi on by itself; actions that need a missing chapter may ask to connect.

Press the Kindle Menu/Options button for zoom, crop, orientation, contrast, and reader settings.

When a metadata cover has been cached, MangaBridge temporarily uses it as KOReader's sleep screen while a MangaBridge chapter is open. Your previous KOReader sleep-screen settings are restored when the chapter closes.

Safety: the plugin only writes to USB-visible storage. It does not install firmware packages, replace the Kindle reader, or modify the system partition.]]),
    })
end

function MangaBridge:onFlushSettings()
    if self.settings then
        self.settings:flush()
    end
end

return MangaBridge
