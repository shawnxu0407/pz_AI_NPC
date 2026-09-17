--[[
AIAgent.lua — Phase 1 Minimal Companion AI for Bandits NPC mod

All-in-one file: state management + JSON codec + file I/O protocol
+ AICompanion Bandits Program + player input.

Architecture:
  Player input -> write ai_request.json -> sidecar.py -> LLM
  LLM -> write ai_response.json -> Lua polls -> PendingCommand
  PendingCommand -> CurrentMode -> AICompanion.Main -> Bandits tasks

Key bindings:
  F7  - Take over nearest companion NPC (switch to AICompanion program)
  F8  - Open text input dialog -> send request to sidecar
  F9  - Quick STANDBY (bypass LLM, for testing)
  F10 - Quick FOLLOW  (bypass LLM, for testing)

Requires: Bandits NPC mod (Bandits2) loaded and active.
--]]

-- ========================================================================
-- SECTION 1: Module & State
-- ========================================================================

AIAgent = AIAgent or {}

-- Per-NPC state tables (keyed by Bandits zombie ID as string)
AIAgent.CurrentMode    = {}   -- [npcId] = "FOLLOW" | "STANDBY"
AIAgent.PendingCommand = {}   -- [npcId] = { action=string, request_id=string }
AIAgent.LastRequestId  = {}   -- [npcId] = last processed request_id (dedup)

-- Currently active AI-controlled NPC (single-player Phase 1: one at a time)
AIAgent.ActiveNpcId = nil

-- ========================================================================
-- SECTION 2: Config
-- ========================================================================

AIAgent.VALID_ACTIONS = { FOLLOW = true, STANDBY = true }
AIAgent.PROTOCOL_VERSION = 1

-- File paths are relative to <PZ user dir>/Lua/
AIAgent.REQUEST_FILE  = "AIAgent/ai_request.json"
AIAgent.RESPONSE_FILE = "AIAgent/ai_response.json"
AIAgent.POLL_INTERVAL_MS = 500
AIAgent.REQUEST_TIMEOUT_MS = 10000  -- discard response if older than this

local lastPollTime = 0
local requestCounter = 0

-- ========================================================================
-- SECTION 3: Minimal JSON codec
-- ========================================================================

local function json_encode(val)
    local t = type(val)
    if t == "string" then
        local s = val:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t")
        return '"' .. s .. '"'
    elseif t == "number" then
        if val ~= val or val == math.huge or val == -math.huge then return "0" end
        return tostring(val)
    elseif t == "boolean" then
        return val and "true" or "false"
    elseif t == "nil" then
        return "null"
    elseif t == "table" then
        local parts, n = {}, 0
        -- Detect array
        local isArray, count = true, 0
        for k in pairs(val) do
            count = count + 1
            if type(k) ~= "number" then isArray = false break end
        end
        if isArray and count > 0 then
            for i = 1, count do
                n = n + 1; parts[n] = json_encode(val[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            for k, v in pairs(val) do
                n = n + 1
                parts[n] = json_encode(tostring(k)) .. ":" .. json_encode(v)
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    end
    return "null"
end

local function json_decode(str)
    local pos = 1

    local function skipws()
        while pos <= #str and string.find(str, "^[%s]", pos) do pos = pos + 1 end
    end

    local parse_val

    local function parse_str()
        pos = pos + 1  -- skip opening quote
        local buf, n = {}, 0
        while pos <= #str do
            local c = string.sub(str, pos, pos)
            if c == '"' then pos = pos + 1 return table.concat(buf) end
            if c == "\\" then
                pos = pos + 1
                local e = string.sub(str, pos, pos)
                local map = { n="\n", t="\t", r="\r", ['"']='"', ["\\"]="\\", ["/"]="/" }
                n = n + 1; buf[n] = map[e] or e
            else
                n = n + 1; buf[n] = c
            end
            pos = pos + 1
        end
        error("unterminated string")
    end

    local function parse_num()
        local s = pos
        if string.sub(str, pos, pos) == "-" then pos = pos + 1 end
        while pos <= #str and string.find(str, "^[%d.eE+-]", pos) do pos = pos + 1 end
        return tonumber(string.sub(str, s, pos - 1)) or error("bad number")
    end

    local function parse_obj()
        pos = pos + 1  -- skip {
        local obj = {}
        skipws()
        if string.sub(str, pos, pos) == "}" then pos = pos + 1 return obj end
        while true do
            skipws()
            local k = parse_str()
            skipws()
            if string.sub(str, pos, pos) ~= ":" then error("expected :") end
            pos = pos + 1
            obj[k] = parse_val()
            skipws()
            local c = string.sub(str, pos, pos)
            if c == "," then pos = pos + 1
            elseif c == "}" then pos = pos + 1 return obj
            else error("expected , or }") end
        end
    end

    local function parse_arr()
        pos = pos + 1  -- skip [
        local arr = {}
        skipws()
        if string.sub(str, pos, pos) == "]" then pos = pos + 1 return arr end
        while true do
            skipws()
            arr[#arr + 1] = parse_val()
            skipws()
            local c = string.sub(str, pos, pos)
            if c == "," then pos = pos + 1
            elseif c == "]" then pos = pos + 1 return arr
            else error("expected , or ]") end
        end
    end

    parse_val = function()
        skipws()
        local c = string.sub(str, pos, pos)
        if c == '"' then return parse_str()
        elseif c == "{" then return parse_obj()
        elseif c == "[" then return parse_arr()
        elseif c == "t" then pos = pos + 4 return true
        elseif c == "f" then pos = pos + 5 return false
        elseif c == "n" then pos = pos + 4 return nil
        elseif c == "-" or tonumber(c) then return parse_num()
        else error("unexpected: " .. c) end
    end

    skipws()
    return parse_val()
end

-- ========================================================================
-- SECTION 4: File I/O helpers
-- ========================================================================

local function writeFile(path, content)
    local writer = getFileWriter(path, true, false)
    if not writer then return false end
    writer:write(content)
    writer:close()
    return true
end

local function readFile(path)
    local ok, reader = pcall(getFileReader, path, false)
    if not ok or not reader then return nil end
    local buf, n = {}, 0
    local ok2, line = pcall(function() return reader:readLine() end)
    while ok2 and line do
        n = n + 1; buf[n] = line
        ok2, line = pcall(function() return reader:readLine() end)
    end
    reader:close()
    if n == 0 then return nil end
    return table.concat(buf, "\n")
end

-- ========================================================================
-- SECTION 5: NPC lookup helpers
-- ========================================================================

local function getNpcById(npcId)
    local zlist = getCell():getZombieList()
    for i = 0, zlist:size() - 1 do
        local z = zlist:get(i)
        if z and z:getVariableBoolean("Bandit") then
            if tostring(BanditUtils.GetZombieID(z)) == tostring(npcId) then
                return z
            end
        end
    end
    return nil
end

local function findNearestCompanion()
    local player = getSpecificPlayer(0)
    if not player then return nil end
    local px, py = player:getX(), player:getY()
    local nearest, nearestDist = nil, math.huge
    local zlist = getCell():getZombieList()
    for i = 0, zlist:size() - 1 do
        local z = zlist:get(i)
        if z and z:getVariableBoolean("Bandit") then
            local brain = BanditBrain.Get(z)
            if brain and brain.program and brain.program.name == "Companion" then
                local d = BanditUtils.DistTo(z:getX(), z:getY(), px, py)
                if d < nearestDist then nearest, nearestDist = z, d end
            end
        end
    end
    return nearest, nearestDist
end

local function takeOverNearestCompanion()
    local bandit, dist = findNearestCompanion()
    if not bandit then
        print("[AIAgent] No companion NPC found. Recruit a bandit first (right-click > Join Me).")
        return false
    end
    local npcId = tostring(BanditUtils.GetZombieID(bandit))
    AIAgent.ActiveNpcId = npcId
    AIAgent.CurrentMode[npcId] = "FOLLOW"
    Bandit.SetProgram(bandit, "AICompanion", {})
    print(string.format("[AIAgent] Took over NPC %s (dist %.1f), mode=FOLLOW", npcId, dist))
    return true
end

-- ========================================================================
-- SECTION 6: Protocol validation & response polling
-- ========================================================================

local function validateResponse(resp)
    if type(resp) ~= "table" then return false, "not a table" end
    if resp.protocol_version ~= AIAgent.PROTOCOL_VERSION then
        return false, "bad protocol_version"
    end
    if type(resp.request_id) ~= "string" and type(resp.request_id) ~= "number" then
        return false, "bad request_id"
    end
    if type(resp.npc_id) ~= "string" and type(resp.npc_id) ~= "number" then
        return false, "bad npc_id"
    end
    if type(resp.action) ~= "string" or not AIAgent.VALID_ACTIONS[resp.action] then
        return false, "invalid action: " .. tostring(resp.action)
    end
    return true
end

local function pollResponse()
    local now = tonumber(getTimestampMs()) or 0
    if now - lastPollTime < AIAgent.POLL_INTERVAL_MS then return end
    lastPollTime = now

    -- Check if active NPC still exists
    if AIAgent.ActiveNpcId then
        local bandit = getNpcById(AIAgent.ActiveNpcId)
        if not bandit or bandit:isDead() then
            print("[AIAgent] Active NPC gone, clearing state.")
            AIAgent.CurrentMode[AIAgent.ActiveNpcId] = nil
            AIAgent.PendingCommand[AIAgent.ActiveNpcId] = nil
            AIAgent.LastRequestId[AIAgent.ActiveNpcId] = nil
            AIAgent.ActiveNpcId = nil
        end
    end

    if not AIAgent.ActiveNpcId then return end

    local content = readFile(AIAgent.RESPONSE_FILE)
    if not content or #content < 10 then return end

    local ok, resp = pcall(json_decode, content)
    if not ok then
        print("[AIAgent] JSON parse error: " .. tostring(resp))
        return
    end

    local valid, err = validateResponse(resp)
    if not valid then
        print("[AIAgent] Invalid response: " .. err)
        return
    end

    local npcId = tostring(resp.npc_id)
    local reqId = tostring(resp.request_id)

    -- Dedup: skip if already processed
    if AIAgent.LastRequestId[npcId] == reqId then return end

    -- Only accept responses for our active NPC
    if npcId ~= tostring(AIAgent.ActiveNpcId) then
        print("[AIAgent] Response for different NPC, ignoring.")
        return
    end

    AIAgent.PendingCommand[npcId] = { action = resp.action, request_id = reqId }
    print("[AIAgent] Received: action=" .. resp.action .. " req=" .. reqId)

    -- Clear response file so we don't re-read it
    writeFile(AIAgent.RESPONSE_FILE, "{}")
end

-- ========================================================================
-- SECTION 7: Request sending (player input -> file -> sidecar)
-- ========================================================================

local function sendRequest(playerMessage)
    if not AIAgent.ActiveNpcId then
        if not takeOverNearestCompanion() then return end
    end

    local bandit = getNpcById(AIAgent.ActiveNpcId)
    if not bandit then
        AIAgent.ActiveNpcId = nil
        print("[AIAgent] NPC not found.")
        return
    end

    local player = getSpecificPlayer(0)
    requestCounter = requestCounter + 1
    local reqId = "req-" .. tostring(requestCounter)

    local req = {
        protocol_version = AIAgent.PROTOCOL_VERSION,
        request_id = reqId,
        npc_id = AIAgent.ActiveNpcId,
        event = "PLAYER_MESSAGE",
        player_message = playerMessage,
        current_mode = AIAgent.CurrentMode[AIAgent.ActiveNpcId] or "STANDBY",
        npc_health = tonumber(string.format("%.2f", bandit:getHealth())),
        npc_distance = tonumber(string.format("%.1f",
            BanditUtils.DistTo(bandit:getX(), bandit:getY(), player:getX(), player:getY()))),
        timestamp = tonumber(getTimestampMs()) or 0
    }

    writeFile(AIAgent.REQUEST_FILE, json_encode(req))
    print("[AIAgent] Sent request " .. reqId .. ": \"" .. playerMessage .. "\"")
end

-- ========================================================================
-- SECTION 8: Player input (key bindings)
-- ========================================================================

local function openTextInput()
    if not AIAgent.ActiveNpcId then
        takeOverNearestCompanion()
    end
    if not AIAgent.ActiveNpcId then return end

    local modal = ISTextBox:new(
        getCore():getScreenWidth() / 2 - 150,
        getCore():getScreenHeight() / 2 - 100,
        300, 200,
        "Message to companion:",
        "",
        nil, 0
    )
    modal.onclick = function(self, button)
        if button and button.internal == "OK" then
            local text = self:getInternalText()
            if text and #text > 0 then
                sendRequest(text)
            end
        end
    end
    modal:initialise()
    modal:addToUIManager()
end

local function onKeyPressed(key)
    if key == Keyboard.KEY_F7 then
        takeOverNearestCompanion()

    elseif key == Keyboard.KEY_F8 then
        openTextInput()

    elseif key == Keyboard.KEY_F9 then
        -- Quick STANDBY (bypass LLM, for testing)
        if AIAgent.ActiveNpcId then
            AIAgent.PendingCommand[AIAgent.ActiveNpcId] = {
                action = "STANDBY",
                request_id = "manual-" .. tostring(getTimestampMs())
            }
            print("[AIAgent] Manual STANDBY")
        else
            print("[AIAgent] No active NPC. Press F7 first.")
        end

    elseif key == Keyboard.KEY_F10 then
        -- Quick FOLLOW (bypass LLM, for testing)
        if AIAgent.ActiveNpcId then
            AIAgent.PendingCommand[AIAgent.ActiveNpcId] = {
                action = "FOLLOW",
                request_id = "manual-" .. tostring(getTimestampMs())
            }
            print("[AIAgent] Manual FOLLOW")
        else
            print("[AIAgent] No active NPC. Press F7 first.")
        end
    end
end

Events.OnKeyPressed.Add(onKeyPressed)
Events.OnTick.Add(pollResponse)

-- ========================================================================
-- SECTION 9: AICompanion Bandits Program
-- ========================================================================

ZombiePrograms = ZombiePrograms or {}
ZombiePrograms.AICompanion = {}

ZombiePrograms.AICompanion.Prepare = function(bandit)
    local tasks = {}
    Bandit.ForceStationary(bandit, false)
    return { status = true, next = "Main", tasks = tasks }
end

ZombiePrograms.AICompanion.Main = function(bandit)
    local tasks = {}
    local npcId = tostring(BanditUtils.GetZombieID(bandit))

    -- 1. Process pending command (from LLM response or manual override)
    local pending = AIAgent.PendingCommand[npcId]
    if pending then
        if pending.request_id ~= AIAgent.LastRequestId[npcId] then
            AIAgent.CurrentMode[npcId] = pending.action
            AIAgent.LastRequestId[npcId] = pending.request_id
            Bandit.ClearTasks(bandit)
            print("[AIAgent] NPC " .. npcId .. " -> mode: " .. pending.action)
        end
        AIAgent.PendingCommand[npcId] = nil
    end

    local mode = AIAgent.CurrentMode[npcId] or "STANDBY"

    -- 2. Execute based on current mode
    if mode == "FOLLOW" then
        Bandit.ForceStationary(bandit, false)

        local master = BanditPlayer.GetMasterPlayer(bandit)
        if not master then
            table.insert(tasks, { action = "Time", anim = "Shrug", time = 200 })
            return { status = true, next = "Main", tasks = tasks }
        end

        -- Walk type selection (simplified from ZPCompanion.Main)
        local walkType = "Walk"
        local endurance = 0.0
        local dist = BanditUtils.DistTo(bandit:getX(), bandit:getY(),
                                         master:getX(), master:getY())

        if master:isSprinting() or dist > 10 then
            walkType = "Run"; endurance = -0.07
        elseif master:isSneaking() and dist < 12 then
            walkType = "SneakWalk"; endurance = -0.01
        end

        if bandit:getHealth() < 0.4 then
            walkType = "Limp"; endurance = 0
        end

        -- Core follow: move toward master if far, idle if close
        local dx, dy, dz = master:getX(), master:getY(), master:getZ()
        local did = BanditUtils.GetCharacterID(master)
        local distTarget = BanditUtils.DistTo(bandit:getX(), bandit:getY(), dx, dy)

        if distTarget > 1 or math.abs(dz - bandit:getZ()) >= 1 then
            table.insert(tasks, BanditUtils.GetMoveTaskTarget(
                endurance, dx, dy, dz, did, true, walkType, distTarget))
        else
            local sub = BanditPrograms.Idle(bandit)
            for _, t in pairs(sub) do table.insert(tasks, t) end
        end

    elseif mode == "STANDBY" then
        Bandit.ForceStationary(bandit, true)
        local sub = BanditPrograms.Idle(bandit)
        for _, t in pairs(sub) do table.insert(tasks, t) end
    end

    return { status = true, next = "Main", tasks = tasks }
end

-- ========================================================================
-- Print load confirmation
-- ========================================================================
print("[AIAgent] Phase 1 loaded. F7=takeover  F8=chat  F9=standby  F10=follow")
