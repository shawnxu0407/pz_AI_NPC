--[[
CompanionZombieFollow.lua

用途：独立测试脚本，验证"生成一个僵尸 -> 打标记 -> 不攻击玩家 -> 保持跟随距离"这条链路。

放置路径建议：<你的测试mod>/media/lua/client/CompanionZombieFollow.lua

操作：游戏内按 F9，会在玩家附近生成一个僵尸，并给它打上 IsCompanion 标记。
之后每个僵尸 tick 都会检查这只僵尸：
  1) 如果它当前的攻击目标是玩家，尝试打断它的索敌/寻路状态
  2) 根据跟玩家的距离，决定要不要往玩家方向靠近

⚠️ 重要说明（请一定读完再测试）：
本脚本用到的方法（getPathFindBehavior2, cancel, setData, getTarget 等）
是从社区实际发布的 mod 代码（Bandits NPC mod 的 BanditUpdate.lua）里确认存在的，
但"清除目标 = 保证不会被攻击"这件事，僵尸的核心战斗状态机
（要不要发起攻击、什么时候挥拳）并不是靠这几个字段就能 100% 兜底的——
这只是"软性打断"，不是"从根源上禁止攻击"的官方开关。
所以这版脚本的定位是：先跑起来、观察它在多大程度上有效，
再根据实际测试结果（比如它是不是偶尔还是会打玩家一下）决定
要不要进一步去覆写僵尸的状态机（更彻底但工作量大得多，
参考 Bandits mod 的 Banditize(zombie, brain) 思路）。

建议测试方法：
1) 按 F9 生成
2) 观察它在附近走动、不主动往你身上扑
3) 故意靠近它、绕着它走，看跟随距离维持得好不好
4) 如果发现它还是会攻击你，把发生瞬间的场景记下来（比如"贴脸站着不动的时候"），
   这能帮助我们判断问题出在"目标没清干净"还是"状态机层面的攻击判定"。
--]]

local COMPANION_TAG = "IsCompanion"
local FOLLOW_DISTANCE_MIN = 2   -- 小于这个距离，停下别再往玩家身上挤
local FOLLOW_DISTANCE_MAX = 5   -- 大于这个距离，才重新计算路径靠近玩家

-- ============================================================
-- 生成
-- ============================================================

local function spawnCompanionZombie()
    local player = getPlayer()
    if not player then
        print("[CompanionZombie] No player instance, abort spawn.")
        return
    end

    local x = player:getX() + 2
    local y = player:getY()
    local z = player:getZ()

    -- addZombiesInOutfit(x, y, z, 数量, 服装, 女性概率, 是否爬行, 是否倒地,
    --                     是否假死, 是否被打倒, 是否无敌, 是否坐着, 血量)
    local zombies = addZombiesInOutfit(x, y, z, 1, "", nil,
        false, false, false, false, false, false, 100.0)

    if not zombies or zombies:size() == 0 then
        print("[CompanionZombie] Spawn call returned no zombie.")
        return
    end

    local companion = zombies:get(0)
    companion:setVariable(COMPANION_TAG, true)
    print(string.format("[CompanionZombie] Spawned at (%.1f, %.1f)", x, y))
end

local function onKeyPressed(key)
    if key == Keyboard.KEY_RBRACKET then
        spawnCompanionZombie()
    end
end
Events.OnKeyPressed.Add(onKeyPressed)

-- ============================================================
-- 阻止攻击玩家（尽力而为，见文件头说明）
-- ============================================================

local function neutralizeAgainstPlayer(zombie, player)
    local target = zombie:getTarget()
    if target == player then
        local pfb = zombie:getPathFindBehavior2()
        if pfb then
            pfb:cancel()
        end
        -- 顺手清一下当前寻路目标，避免它继续朝玩家方向"追"
        if zombie.setPath2 then
            zombie:setPath2(nil)
        end
    end
end

-- ============================================================
-- 跟随逻辑
-- ============================================================

local function updateFollow(zombie, player)
    local dx = player:getX() - zombie:getX()
    local dy = player:getY() - zombie:getY()
    local dist = math.sqrt(dx * dx + dy * dy)

    local pfb = zombie:getPathFindBehavior2()
    if not pfb then return end

    if dist > FOLLOW_DISTANCE_MAX then
        pfb:setData(player:getX(), player:getY(), player:getZ())
    elseif dist < FOLLOW_DISTANCE_MIN then
        pfb:cancel()
    end
end

-- ============================================================
-- 主循环：只处理带 IsCompanion 标记的僵尸
-- ============================================================

local function onZombieUpdate(zombie)
    if not zombie:getVariableBoolean(COMPANION_TAG) then
        return
    end

    local player = getPlayer()
    if not player then return end

    neutralizeAgainstPlayer(zombie, player)
    updateFollow(zombie, player)
end
Events.OnZombieUpdate.Add(onZombieUpdate)

print("[CompanionZombie] Test script loaded. Press F9 in-game to spawn.")
