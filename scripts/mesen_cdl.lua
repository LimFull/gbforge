-- Play a GB/GBC ROM headless in Mesen2 (--testRunner) with a scripted input schedule, then dump
-- the Code/Data Logger flags for PRG ROM (1 byte per ROM byte: 1 code, 2 data, 4 jump target,
-- 8 sub entry). Driven by scripts/mesen_cdl.py through environment variables:
--   CDL_OUT     output file
--   CDL_FRAMES  frames to run
--   CDL_INPUTS  "frame:buttons:hold;..." e.g. "700:start:2;900:a,down:4"
--   CDL_CART_IN / CDL_CART_OUT  cart RAM file loaded at start / written at the end
--   CDL_RAM     file receiving each instruction executed from RAM as "PPPP bbbbbbbb" (PC, 4 bytes
--               from PC as executed), so code copied from ROM to RAM can be found and compiled
local out = os.getenv("CDL_OUT")
local total = tonumber(os.getenv("CDL_FRAMES"))
local events = {}
for f, buttons, hold in string.gmatch(os.getenv("CDL_INPUTS") or "", "(%d+):([%a,]+):(%d+)") do
  local set = {}
  for b in string.gmatch(buttons, "%a+") do set[b] = true end
  events[#events + 1] = {from = tonumber(f), to = tonumber(f) + tonumber(hold), buttons = set}
end

local frame = 0
local ram_out = os.getenv("CDL_RAM")
local ram_seen = {}
if ram_out and ram_out ~= "" then
  emu.addMemoryCallback(function(address)
    local b = {}
    for i = 0, 3 do b[#b + 1] = string.format("%02x", emu.read((address + i) & 0xFFFF, emu.memType.gameboyMemory)) end
    ram_seen[string.format("%04x %s", address, table.concat(b))] = true
  end, emu.callbackType.exec, 0x8000, 0xFFFE, emu.cpuType.gameboy)
end
local clear_cart = os.getenv("CDL_CLEAR_CART")   -- "N": write N zero bytes to cart RAM at start
local cart_in = os.getenv("CDL_CART_IN")         -- file loaded into cart RAM at start (resume after a reset)
local cart_out = os.getenv("CDL_CART_OUT")       -- file receiving cart RAM at the end

emu.addEventCallback(function()
  local input = {}
  for _, e in ipairs(events) do
    if frame >= e.from and frame < e.to then
      for b in pairs(e.buttons) do input[b] = true end
    end
  end
  emu.setInput(input, 0)
end, emu.eventType.inputPolled)

emu.addEventCallback(function()
  if frame == 0 and clear_cart then
    for i = 0, tonumber(clear_cart) - 1 do emu.write(i, 0, emu.memType.gbCartRam) end
  end
  if frame == 0 and cart_in and cart_in ~= "" then
    local data = io.open(cart_in, "rb"):read("a")
    for i = 1, #data do emu.write(i - 1, data:byte(i), emu.memType.gbCartRam) end
  end
  frame = frame + 1
  if frame < total then return end
  local cdl = emu.getCdlData(emu.memType.gbPrgRom)
  local f = io.open(out, "wb")
  local chunk = {}
  for i = (cdl[0] ~= nil) and 0 or 1, #cdl do  -- Mesen returns 0-based arrays
    chunk[#chunk + 1] = string.char(cdl[i])
    if #chunk == 4096 then f:write(table.concat(chunk)); chunk = {} end
  end
  f:write(table.concat(chunk))
  f:close()
  if ram_out and ram_out ~= "" then
    local r = io.open(ram_out, "w")
    for line in pairs(ram_seen) do r:write(line, "\n") end
    r:close()
  end
  if cart_out and cart_out ~= "" then
    local bytes = {}
    for i = 0, emu.getMemorySize(emu.memType.gbCartRam) - 1 do bytes[#bytes + 1] = string.char(emu.read(i, emu.memType.gbCartRam)) end
    local c = io.open(cart_out, "wb"); c:write(table.concat(bytes)); c:close()
  end
  emu.stop(0)
end, emu.eventType.endFrame)
