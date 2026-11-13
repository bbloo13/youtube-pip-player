-- YouTube PIP 용 컨트롤바 (youtube-music-widget 의 재생바 스타일)
--   * 둥근 반투명 패널, 6px 둥근 진행바(흰색 10% 바탕 + #7fb5ff 채움)
--   * 이전 / 재생·일시정지 / 다음, 74px 볼륨 슬라이더(8px 파란 점)
--   * 마우스를 움직이면 나타나고 2.5초 뒤 사라짐 (일시정지 중에는 계속 표시)
local mp = require 'mp'

-- ASS 색상은 BGR 순서
local ACCENT = "FFB57F"   -- #7fb5ff
local FG     = "EAE8E8"   -- #e8e8ea
local MUTE   = "A38F8F"   -- #8f8fa3
local PANEL  = "181414"   -- #141418
local WHITE  = "FFFFFF"

local overlay = mp.create_osd_overlay("ass-events")
local st = { visible = false, last = 0, drag = nil, mx = -1, my = -1, reg = {} }

local function f(n) return string.format("%.1f", n) end

local function mouse_pos()
    local x, y = mp.get_mouse_pos()
    return { x = x or -1, y = y or -1 }
end

local function rrect(x1, y1, x2, y2, r)
    r = math.min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    local k = r * 0.5523
    return table.concat({
        "m", f(x1 + r), f(y1),
        "l", f(x2 - r), f(y1),
        "b", f(x2 - r + k), f(y1), f(x2), f(y1 + r - k), f(x2), f(y1 + r),
        "l", f(x2), f(y2 - r),
        "b", f(x2), f(y2 - r + k), f(x2 - r + k), f(y2), f(x2 - r), f(y2),
        "l", f(x1 + r), f(y2),
        "b", f(x1 + r - k), f(y2), f(x1), f(y2 - r + k), f(x1), f(y2 - r),
        "l", f(x1), f(y1 + r),
        "b", f(x1), f(y1 + r - k), f(x1 + r - k), f(y1), f(x1 + r), f(y1),
    }, " ")
end

local function shape(path, color, alpha)
    return string.format("{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H%s&\\1a&H%s&\\p1}%s{\\p0}",
        color, alpha or "00", path)
end

local function text(x, y, str, size, color, anchor, clip_w)
    local clip = ""
    if clip_w then
        clip = string.format("\\clip(%s,%s,%s,%s)", f(x), f(y - size * 2), f(x + clip_w), f(y + size * 2))
    end
    return string.format("{\\an%d\\pos(%s,%s)\\fnSegoe UI\\fs%s\\bord0\\shad0\\1c&H%s&%s}%s",
        anchor or 4, f(x), f(y), f(size), color, clip, str)
end

local function fmt(t)
    if not t or t < 0 then t = 0 end
    t = math.floor(t)
    local h, m, s = math.floor(t / 3600), math.floor(t / 60) % 60, t % 60
    if h > 0 then return string.format("%d:%02d:%02d", h, m, s) end
    return string.format("%02d:%02d", m, s)
end

local function inside(r, x, y)
    return r and x >= r.x1 and x <= r.x2 and y >= r.y1 and y <= r.y2
end

local function draw()
    local w, h = mp.get_osd_size()
    if not w or w == 0 then return end
    overlay.res_x, overlay.res_y = w, h
    if not st.visible then overlay:remove() return end

    local s = math.max(1.0, math.min(1.6, w / 760))
    local m = 8 * s
    local ph = 68 * s
    local px1, px2 = m, w - m
    local py1, py2 = h - ph - m, h - m
    local pad = 16 * s
    local L = {}
    st.reg = {}

    -- 패널 (rgba(20,20,24,0.55) 느낌)
    L[#L + 1] = shape(rrect(px1, py1, px2, py2, 14 * s), PANEL, "40")

    -- 위쪽 제목줄 (기본 윈도우 제목줄 대신): 제목 + 닫기 버튼, 끌면 창 이동
    local th = 38 * s
    L[#L + 1] = shape(rrect(m, m, w - m, m + th, 14 * s), PANEL, "40")
    local title = mp.get_property("media-title") or ""
    title = title:gsub("[{}\\]", "")
    L[#L + 1] = text(m + pad, m + th / 2, title, 17 * s, FG, 4, (w - 2 * m) - pad * 2 - 36 * s)
    local ccx, ccy = w - m - 22 * s, m + th / 2
    local creg = { x1 = ccx - 16 * s, x2 = ccx + 16 * s, y1 = ccy - 16 * s, y2 = ccy + 16 * s }
    local cc, d = inside(creg, st.mx, st.my) and "6B6BFF" or "8080FF", 6 * s
    L[#L + 1] = string.format("{\\an7\\pos(0,0)\\bord%s\\shad0\\1a&HFF&\\3c&H%s&\\p1}m %s %s l %s %s m %s %s l %s %s{\\p0}",
        f(1.8 * s), cc, f(ccx - d), f(ccy - d), f(ccx + d), f(ccy + d), f(ccx + d), f(ccy - d), f(ccx - d), f(ccy + d))
    st.reg.top = { x1 = m, x2 = w - m, y1 = m, y2 = m + th }
    st.reg.close = creg

    -- 진행바
    local bx1, bx2 = px1 + pad, px2 - pad
    local by, bh = py1 + 18 * s, 7 * s
    local hot = st.drag == "seek" or inside({ x1 = bx1, x2 = bx2, y1 = by - 8 * s, y2 = by + bh + 8 * s }, st.mx, st.my)
    L[#L + 1] = shape(rrect(bx1, by, bx2, by + bh, 3 * s), WHITE, hot and "D6" or "E6")
    local pos = mp.get_property_number("time-pos") or 0
    local dur = mp.get_property_number("duration") or 0
    local frac = dur > 0 and math.max(0, math.min(1, pos / dur)) or 0
    if frac > 0 then
        L[#L + 1] = shape(rrect(bx1, by, bx1 + math.max(bh, (bx2 - bx1) * frac), by + bh, 3 * s), ACCENT)
    end
    st.reg.seek = { x1 = bx1, x2 = bx2, y1 = by - 10 * s, y2 = by + bh + 10 * s }

    -- 아래 줄
    local cy = py1 + 46 * s
    L[#L + 1] = text(bx1, cy, fmt(pos) .. " / " .. fmt(dur), 16 * s, MUTE, 4)

    local cx = (px1 + px2) / 2
    local paused = mp.get_property_bool("pause")
    local function hov(name) return inside(st.reg[name], st.mx, st.my) and ACCENT or FG end

    -- 이전
    local x = cx - 44 * s
    local c = hov("prev")
    L[#L + 1] = shape(rrect(x - 7 * s, cy - 6 * s, x - 4.5 * s, cy + 6 * s, 1 * s), c)
    L[#L + 1] = shape(string.format("m %s %s l %s %s %s %s", f(x + 6 * s), f(cy - 7 * s), f(x - 4 * s), f(cy), f(x + 6 * s), f(cy + 7 * s)), c)
    st.reg.prev = { x1 = x - 14 * s, x2 = x + 14 * s, y1 = cy - 14 * s, y2 = cy + 14 * s }
    -- 재생 / 일시정지
    c = hov("play")
    if paused then
        L[#L + 1] = shape(string.format("m %s %s l %s %s %s %s", f(cx - 5 * s), f(cy - 8 * s), f(cx + 8 * s), f(cy), f(cx - 5 * s), f(cy + 8 * s)), c)
    else
        L[#L + 1] = shape(rrect(cx - 6 * s, cy - 7 * s, cx - 2 * s, cy + 7 * s, 1.5 * s), c)
        L[#L + 1] = shape(rrect(cx + 2 * s, cy - 7 * s, cx + 6 * s, cy + 7 * s, 1.5 * s), c)
    end
    st.reg.play = { x1 = cx - 16 * s, x2 = cx + 16 * s, y1 = cy - 14 * s, y2 = cy + 14 * s }
    -- 다음
    x = cx + 44 * s
    c = hov("next")
    L[#L + 1] = shape(rrect(x + 4.5 * s, cy - 6 * s, x + 7 * s, cy + 6 * s, 1 * s), c)
    L[#L + 1] = shape(string.format("m %s %s l %s %s %s %s", f(x - 6 * s), f(cy - 7 * s), f(x + 4 * s), f(cy), f(x - 6 * s), f(cy + 7 * s)), c)
    st.reg.next = { x1 = x - 14 * s, x2 = x + 14 * s, y1 = cy - 14 * s, y2 = cy + 14 * s }

    -- 볼륨 (74px x 3px, 8px 점)
    local vw = 74 * s
    local vx2 = px2 - pad
    local vx1 = vx2 - vw
    local vol = math.max(0, math.min(100, mp.get_property_number("volume") or 100))
    L[#L + 1] = shape(rrect(vx1, cy - 1.5 * s, vx2, cy + 1.5 * s, 1.5 * s), WHITE, "DE")
    local vfx = vx1 + vw * vol / 100
    L[#L + 1] = shape(rrect(vx1, cy - 1.5 * s, math.max(vx1 + 3 * s, vfx), cy + 1.5 * s, 1.5 * s), ACCENT)
    L[#L + 1] = shape(rrect(vfx - 4 * s, cy - 4 * s, vfx + 4 * s, cy + 4 * s, 4 * s), ACCENT)
    L[#L + 1] = text(vx1 - 8 * s, cy, string.format("%d%%", vol), 15 * s, MUTE, 6)
    st.reg.vol = { x1 = vx1 - 6 * s, x2 = vx2 + 6 * s, y1 = cy - 12 * s, y2 = cy + 12 * s }

    overlay.data = table.concat(L, "\n")
    overlay:update()
end

local function poke()
    if st.inside == false and not st.drag then return end   -- 마우스가 창 밖이면 표시하지 않음
    st.last = mp.get_time()
    if not st.visible then st.visible = true end
    draw()
end

local function apply_drag(final)
    local pos = mouse_pos()
    st.mx, st.my = pos.x, pos.y
    if st.drag == "seek" and st.reg.seek then
        local r = st.reg.seek
        local p = math.max(0, math.min(1, (pos.x - r.x1) / (r.x2 - r.x1)))
        mp.commandv("seek", tostring(p * 100), final and "absolute-percent+exact" or "absolute-percent")
    elseif st.drag == "vol" and st.reg.vol then
        local r = st.reg.vol
        local p = math.max(0, math.min(1, (pos.x - (r.x1 + 6)) / ((r.x2 - r.x1) - 12)))
        mp.set_property_number("volume", p * 100)
    end
end

mp.add_forced_key_binding("mouse_move", "yt_move", function()
    if st.drag then apply_drag(false) end
    local pos = mouse_pos()
    st.mx, st.my = pos.x, pos.y
    poke()
end)

mp.add_forced_key_binding("MBTN_LEFT", "yt_click", function(e)
    local pos = mouse_pos()
    st.mx, st.my = pos.x, pos.y
    if e.event == "down" then
        if inside(st.reg.seek, pos.x, pos.y) and st.visible then
            st.drag = "seek"; apply_drag(false)
        elseif inside(st.reg.vol, pos.x, pos.y) and st.visible then
            st.drag = "vol"; apply_drag(false)
        elseif inside(st.reg.prev, pos.x, pos.y) and st.visible then
            mp.commandv("playlist-prev")
        elseif inside(st.reg.next, pos.x, pos.y) and st.visible then
            mp.commandv("playlist-next")
        elseif inside(st.reg.play, pos.x, pos.y) and st.visible then
            mp.commandv("cycle", "pause")
        elseif inside(st.reg.close, pos.x, pos.y) and st.visible then
            mp.commandv("stop")
        elseif inside(st.reg.top, pos.x, pos.y) and st.visible then
            mp.commandv("begin-vo-dragging")
        else
            mp.commandv("begin-vo-dragging")   -- 영상을 끌면 창 이동 (클릭해도 재생/일시정지는 바뀌지 않음)
        end
    elseif e.event == "up" then
        if st.drag then apply_drag(true); st.drag = nil end
    end
    poke()
end, { complex = true })

mp.add_periodic_timer(0.25, function()
    if not st.visible then return end
    local paused = mp.get_property_bool("pause")
    if st.inside == false and not st.drag then
        st.visible = false
    elseif not paused and not st.drag and mp.get_time() - st.last > 2.5 then
        st.visible = false
    end
    draw()
end)

mp.observe_property("pause", "bool", function() if st.visible then draw() end end)
mp.observe_property("volume", "number", function() if st.visible then draw() end end)
-- 미리 받아 두는 양: 영상 길이의 1/10 (최소 10초, 최대 60초). 용량 상한은 mpv 옵션(16MiB)이 따로 막는다
mp.register_event("file-loaded", function()
    local d = mp.get_property_number("duration") or 0
    if d > 0 then
        mp.set_property_number("demuxer-readahead-secs", math.max(10, math.min(60, d / 10)))
    end
    poke()
end)
mp.register_event("shutdown", function() overlay:remove() end)
mp.observe_property("osd-width", "number", function() if st.visible then draw() end end)

-- 마우스가 창 밖으로 나가면 바로 숨김
mp.observe_property("mouse-pos", "native", function(_, v)
    if not v then return end
    st.inside = v.hover and true or false
    if not st.inside and not st.drag then
        st.visible = false
        st.mx, st.my = -1, -1
        draw()
    end
end)

-- 창이 너무 작아지지 않게: 크기 조절이 끝난 뒤(마지막 변화 0.25초 후) 최소 크기보다 작으면 비율을 유지한 채 그 크기로 키운다.
-- 조절하는 도중에 막지 않는 건 끌기와 싸우지 않게 하려는 것이고, 끌 때 바로 막는 건 앱 쪽 크기 조절 띠가 맡는다.
-- 최소 크기는 앱이 화면 배율에 맞춰 --script-opts=yt-pip-min_w/min_h 로 넘겨준다.
local MIN_W = tonumber(mp.get_opt("yt-pip-min_w")) or 360
local MIN_H = tonumber(mp.get_opt("yt-pip-min_h")) or 200
local min_timer = nil
local function enforce_min()
    min_timer = nil
    if mp.get_property_bool("fullscreen") or mp.get_property_bool("window-maximized") then return end
    local d = mp.get_property_native("osd-dimensions")
    local scale = mp.get_property_number("current-window-scale")
    if not d or not scale or not d.w or not d.h or d.w <= 0 or d.h <= 0 then return end
    local k = math.max(MIN_W / d.w, MIN_H / d.h)
    if k > 1.01 then
        mp.set_property_number("window-scale", scale * k)
    end
end
mp.observe_property("osd-dimensions", "native", function()
    if min_timer then min_timer:kill() end
    min_timer = mp.add_timeout(0.25, enforce_min)
end)
