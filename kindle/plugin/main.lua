-- MangaBridge for KOReader
-- Designed for non-touch Kindle models, including Kindle 4 on firmware 4.1.4.

local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local JSON = require("json")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local MultiInputDialog = require("ui/widget/multiinputdialog")
local NetworkMgr = require("ui/network/manager")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
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

local MangaBridge = WidgetContainer:extend{
    name = "mangabridge",
    is_doc_only = false,
    settings_file = DataStorage:getSettingsDir() .. "/mangabridge.lua",
    data_dir = "/mnt/us/mangabridge",
    library_dir = "/mnt/us/mangabridge/library",
    cache_dir = "/mnt/us/mangabridge/cache",
    settings = nil,
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
    self.settings = LuaSettings:open(self.settings_file)
    self:loadExternalConfig()
    self.ui.menu:registerToMainMenu(self)
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
        timeout_seconds = true,
        download_timeout_seconds = true,
        poll_attempts = true,
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
            text = _("Continue last MangaBridge chapter"),
            enabled_func = function()
                local path = self.settings:readSetting("last_file")
                return path and file_exists(path)
            end,
            callback = function()
                local path = self.settings:readSetting("last_file")
                if path and file_exists(path) then
                    self:openDocument(path)
                end
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
        ["User-Agent"] = "MangaBridge-KOReader/1.0.5",
        ["Connection"] = "close",
    }
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

function MangaBridge:requestJson(method, path, body)
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

    socket.TIMEOUT = tonumber(self.settings:readSetting("timeout_seconds", 45)) or 45
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

function MangaBridge:libraryCachePath()
    return self.cache_dir .. "/library.json"
end

function MangaBridge:seriesCachePath(series_id)
    return self.cache_dir .. "/series-" .. safe_id(series_id) .. ".json"
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
    local payload, err = self:requestJson("GET", "/api/kindle/v1/library")
    if not payload then
        return nil, err
    end
    save_json(self:libraryCachePath(), payload)
    return payload
end

function MangaBridge:refreshSeries(series_id, force)
    local suffix = force and "?refresh=1" or ""
    local payload, err = self:requestJson("GET", "/api/kindle/v1/series/" .. tostring(series_id) .. suffix)
    if not payload then
        return nil, err
    end
    save_json(self:seriesCachePath(series_id), payload)
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
    local last_file = self.settings:readSetting("last_file")
    if last_file and file_exists(last_file) then
        table.insert(items, {
            text = _("Continue reading"),
            mandatory = _("offline"),
            callback = function() self:openDocument(last_file) end,
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

    if #items == 1 then
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
    self:saveLocalSeriesMetadata(series)

    local items = {
        {
            text = _("Refresh chapter list"),
            callback = function()
                NetworkMgr:runWhenOnline(function() self:showSeries(series_id, true) end)
            end,
        },
        {
            text = _("Download chapter range"),
            callback = function() self:showRangeDialog(series) end,
        },
    }

    for chapter_index, chapter in ipairs(series.chapters or {}) do
        local chapter_number = chapter.number
        local chapter_path = self:chapterPath(series.id, chapter_number)
        local chapter_series = series
        local offline = file_exists(chapter_path)
        local state
        if offline then
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
        description = _("The server downloads missing chapters, creates Kindle-sized PDFs, then the Kindle stores them for offline reading."),
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
                        self.settings:saveSetting("server_url", server_url)
                        self.settings:saveSetting("api_token", trim(fields[2]))
                        self.settings:saveSetting("username", trim(fields[3]))
                        self.settings:saveSetting("password", tostring(fields[4] or ""))
                        self.settings:flush()
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
            _("Connected to %s. Kindle profile: %sx%s maximum, JPEG quality %s."),
            tostring(payload.server or "MangaDL"),
            tostring(profile.width or "?"),
            tostring(profile.max_height or "?"),
            tostring(profile.jpeg_quality or "?")
        ),
    })
end

function MangaBridge:showAbout()
    UIManager:show(InfoMessage:new{
        text = _([[MangaBridge stores all downloaded chapters under /mnt/us/mangabridge and works offline after download.

In lists:
- Page-turn buttons change menu pages.
- The 5-way selects items.
- Right/hold on an offline chapter offers deletion.

While reading, KOReader handles page-turn buttons. Press the Kindle Menu/Options button for zoom, crop, orientation, contrast, and reader settings.

Safety: the plugin only writes to USB-visible storage. It does not install firmware packages, replace the Kindle reader, or modify the system partition.]]),
    })
end

function MangaBridge:onFlushSettings()
    if self.settings then
        self.settings:flush()
    end
end

return MangaBridge
