local HISTORY_SIZE = 100
local SAMPLE_INTERVAL_MS = 500   -- 500ms = 每秒 2 次
local history = {}
local lastSampleTime = 0

-- add rolling snapshot.
local function recordSnapshot(player)
    local item = player:getPrimaryHandItem()
    table.insert(history, {
        timestamp = getTimestampMs(),
        x = player:getX(),
        y = player:getY(),
        z = player:getZ(),
        item = item and item:getFullType() or "none",
    })
    if #history > HISTORY_SIZE then
        table.remove(history, 1)
    end
end

-- write into file
local function exportHistory()
    local writer = getFileWriter("history.json", true, false)
    -- false means overwrite
    writer:write("[\n")
    for i, s in ipairs(history) do
        writer:write(string.format(
            '  {"timestamp":%s,"x":%.2f,"y":%.2f,"z":%.2f,"item":"%s"}',
            tostring(s.timestamp), s.x, s.y, s.z, s.item))
        if i < #history then writer:write(",") end
        writer:write("\n")
    end
    writer:write("]\n")
    writer:close()
end

local function onPlayerUpdate(player)
    if not player then return end

    local now = tonumber(getTimestampMs())  -- transfer into numeric from original string
    if now - lastSampleTime < SAMPLE_INTERVAL_MS then return end
    lastSampleTime = now

    recordSnapshot(player)
    exportHistory()
end

Events.OnPlayerUpdate.Add(onPlayerUpdate)