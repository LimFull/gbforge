// GBA runtime for gbforge-generated code: GB memory map, MBC, IO, interrupts, compat PPU.
//
// Compat renderer: GB VRAM/OAM/palette writes are mirrored straight into GBA BG/OBJ hardware
// (see the PPU section), so there is no per-frame copy besides OAM. The 160x144 picture is
// letterboxed at (40,8). ponytail: no audio yet.
#include <setjmp.h>
#include <string.h>
#include <tonc.h>
#include "gb.h"

// Hot runtime paths run as ARM code from IWRAM (no ROM waitstates); the linker adds the
// Thumb→ARM interworking veneers for calls from generated code in ROM.
#define HOT __attribute__((section(".iwram.text"), target("arm"), noinline))

GbCpu gb IWRAM_DATA;
const uint8_t *gb_romx;
uint8_t gb_wram[0x2000] IWRAM_DATA;      // banks 0-1 (C000-DFFF, incl. the usual stack): zero-wait IWRAM
static uint8_t wram_hi[0x6000] EWRAM_BSS; // CGB banks 2-7
uint8_t *gb_wramx;
uint8_t gb_hram[0x80] IWRAM_DATA;
const uint8_t *gb_rpage[256] IWRAM_DATA;
uint8_t *gb_wpage[256] IWRAM_DATA;

static uint8_t vram[0x4000] EWRAM_BSS;   // GB VRAM shadow (2 CGB banks)
static uint8_t oam[0xA0] EWRAM_BSS;
uint8_t io[0x80] IWRAM_DATA;          // non-static: port_probe reads it via the symbol table
static uint8_t cgb_mode, ram_enable;
static uint16_t bg_pal[32];               // CGB BG palette RAM (BCPS/BCPD)
static void ppu_obj(unsigned i);

// Save-type marker scanned by emulators and flashcarts: GB cart RAM lives in GBA SRAM.
__attribute__((used, aligned(4))) const char gb_save_type[] = "SRAM_V113";

// Exposed for verification tools (emucap reads it through gb2gba.map.json).
volatile struct { uint16_t bank, addr; uint8_t reason, hit; } gb_trap_info;
volatile uint32_t gb_frames, gb_lag_frames;  // GBA VBlanks; GB frames that finished after their real-time slot
uint32_t gb_next_event;

#define IF io[0x0F]
#define IE gb_hram[0x7F]
#define LCDC io[0x40]

static void update_irq(void) { gb.irq = gb.ime && (IE & IF & 0x1F); }

// ---- PPU (compat) -------------------------------------------------------------------
//
// GBA layout: BG0 = GB background, BG1 = GB window (inside WIN0), OBJ = GB sprites.
// BG tiles: charblocks 0-1 (GB VRAM bank 0 tile t → t, bank 1 → 512 + t), colour indices 1-4
// so every BG pixel is opaque with its own palette's colour 0. OBJ tiles: charblock 4
// (GB 0x8000-0x8FFF tiles, bank 0 → t, bank 1 → 256 + t), indices 0-3 (0 transparent).
// Maps: GB 9800/9C00 → screenblocks 28/29, CGB attributes folded into each entry.
// ponytail: no BG-over-OBJ priority (attr bit 7, OBJ bit 7), no 10-sprites-per-line limit,
// register changes apply immediately (no mid-frame raster splits).

#define SB_MAP0 28
#define SB_MAP1 29
#define OBJ_TILES ((u32 *)tile_mem[4])
static u32 spread[256];                   // bit i (MSB first) → nibble i of a 4bpp row
static OBJ_ATTR obj_shadow[40];           // copied to OAM in the VBlank ISR
static uint16_t obj_pal[32];              // CGB OBJ palette RAM (OCPS/OCPD)

static u16 dmg_shade(int s) { static const u16 c[4] = {0x7FFF, 0x5294, 0x294A, 0x0000}; return c[s & 3]; }

static void ppu_tile_row(unsigned bank, unsigned off)   // off: 0..0x17FF tile data
{
    unsigned row = off & ~1u;
    const uint8_t *v = vram + bank * 0x2000;
    u32 px = spread[v[row]] | spread[v[row + 1]] << 1;
    ((u32 *)tile_mem[0])[(bank * 0x2000 + row) >> 1] = px + 0x11111111;
    if (off < 0x1000) OBJ_TILES[(bank * 0x1000 + row) >> 1] = px;
}

static void ppu_map_write(unsigned off)   // off: 0..0x7FF within 9800-9FFF
{
    uint8_t idx = vram[0x1800 + off], attr = cgb_mode ? vram[0x3800 + off] : 0;
    unsigned t = (LCDC & 0x10) ? idx : (idx < 128 ? 256 + idx : idx);  // 8000 vs 8800 addressing
    se_mem[off < 0x400 ? SB_MAP0 : SB_MAP1][off & 0x3FF] = (t + ((attr & 0x08) ? 512 : 0))
        | ((attr & 0x20) ? SE_HFLIP : 0) | ((attr & 0x40) ? SE_VFLIP : 0) | SE_PALBANK(attr & 7);
}

static void ppu_palettes(void)
{
    for (int p = 0; p < 8; p++)
        for (int c = 0; c < 4; c++) {
            if (cgb_mode) {
                pal_bg_mem[p * 16 + 1 + c] = bg_pal[p * 4 + c];
                pal_obj_mem[p * 16 + c] = obj_pal[p * 4 + c];
            } else if (p < 2) {
                pal_bg_mem[1 + c] = dmg_shade(io[0x47] >> (2 * c));
                pal_obj_mem[p * 16 + c] = dmg_shade(io[0x48 + p] >> (2 * c));
            }
        }
}

static void ppu_obj(unsigned i)
{
    const uint8_t *o = &oam[i * 4];
    OBJ_ATTR *d = &obj_shadow[i];
    unsigned tall = LCDC & 0x04, attr = o[3];
    if (!(LCDC & 0x02) || o[0] == 0 || o[0] >= 160 || o[1] == 0 || o[1] >= 168) {
        d->attr0 = ATTR0_HIDE;
        return;
    }
    unsigned tile = (tall ? o[2] & 0xFE : o[2]) + ((cgb_mode && (attr & 0x08)) ? 256 : 0);
    unsigned pal = cgb_mode ? attr & 7 : (attr >> 4) & 1;
    d->attr0 = ((o[0] - 16 + 8) & 0xFF) | (tall ? ATTR0_TALL : ATTR0_SQUARE);
    d->attr1 = ((o[1] - 8 + 40) & 0x1FF) | ((attr & 0x20) ? ATTR1_HFLIP : 0) | ((attr & 0x40) ? ATTR1_VFLIP : 0);
    d->attr2 = tile | ATTR2_PALBANK(pal);
}

static void ppu_all_obj(void)
{
    for (unsigned i = 0; i < 40; i++) ppu_obj(i);
}

static int ppu_window_on(void) { return (LCDC & 0x20) && io[0x4B] <= 166 && io[0x4A] <= 143; }

static void ppu_dispcnt(void)
{
    if (!(LCDC & 0x80)) { REG_DISPCNT = DCNT_BLANK; return; }
    unsigned bg = cgb_mode || (LCDC & 0x01);  // CGB: bit 0 is priority, BG always drawn
    REG_DISPCNT = DCNT_MODE0 | DCNT_OBJ | DCNT_OBJ_1D | DCNT_WIN1 | (bg ? DCNT_BG0 : 0)
        | ((bg && ppu_window_on()) ? DCNT_BG1 | DCNT_WIN0 : 0);
}

static void ppu_scroll(void)
{
    REG_BG0HOFS = io[0x43] - 40;
    REG_BG0VOFS = io[0x42] - 8;
    unsigned wx = io[0x4B] < 7 ? 7 : io[0x4B], wy = io[0x4A];
    REG_BG1HOFS = -(40 + (int)wx - 7);
    REG_BG1VOFS = -(8 + (int)wy);
    REG_WIN0H = (40 + wx - 7) << 8 | 200;
    REG_WIN0V = (8 + wy) << 8 | 152;
}

static void ppu_lcdc(uint8_t old)
{
    if ((old ^ LCDC) & 0x10)
        for (unsigned off = 0; off < 0x800; off++) ppu_map_write(off);
    if ((old ^ LCDC) & 0x06) ppu_all_obj();
    REG_BG0CNT = BG_CBB(0) | BG_SBB((LCDC & 0x08) ? SB_MAP1 : SB_MAP0) | BG_PRIO(1);
    REG_BG1CNT = BG_CBB(0) | BG_SBB((LCDC & 0x40) ? SB_MAP1 : SB_MAP0) | BG_PRIO(0);
    ppu_dispcnt();
}

static void ppu_init(void)
{
    for (int b = 0; b < 256; b++) {
        u32 v = 0;
        for (int i = 0; i < 8; i++) v |= ((b >> (7 - i)) & 1) << (4 * i);
        spread[b] = v;
    }
    oam_init(oam_mem, 128);
    for (int i = 0; i < 40; i++) obj_shadow[i].attr0 = ATTR0_HIDE;
    REG_WIN1H = 40 << 8 | 200;            // WIN1: the whole GB screen; WIN0 (higher priority): GB window
    REG_WIN1V = 8 << 8 | 152;
    REG_WININ = (WIN_BG1 | WIN_OBJ) | (WIN_BG0 | WIN_OBJ) << 8;
    REG_WINOUT = 0;
    pal_bg_mem[0] = 0;                    // letterbox border
    for (unsigned t = 0; t < 0x1800; t += 2) ppu_tile_row(0, t), ppu_tile_row(1, t);
    ppu_lcdc(0);
    ppu_scroll();
    ppu_palettes();
}

// ---- GB time ------------------------------------------------------------------------
//
// gb.cyc counts GB CPU T-cycles (generated code adds each instruction's cycles). LCD frames,
// LY/STAT, STAT and VBlank interrupts, DIV and the timer are all derived from it, so lag frames
// and LCD-on phase resets behave as on the original. Real time only paces: GB time may not run
// ahead of GBA VBlanks (both 59.7275 Hz). Events are checked at safepoints (loop back-edges,
// HALT, EI), so an interrupt can land up to one loop iteration later than on hardware.
// ponytail: CGB double speed runs at normal speed; STAT mode-2 IRQ shares the HBlank event.

#define LINE 456u
#define FRAME (154u * LINE)
static uint32_t frame_start;              // GB time of the current LCD frame's line 0
static uint32_t pace_at;                  // next real-time sync point (LCD off)
static uint32_t sync_frame;               // real-time frame the next GB frame may not precede
volatile uint32_t gb_paced;               // GB frames so far, purely in GB time: scenarios count these
// KEYINPUT override for scenarios (0xFFFF: none). Emulators latch GBA input per frame, so a key set
// through them reaches the game a frame late; a write here is seen by the next P1 read.
volatile uint16_t gb_keys = 0xFFFF;
// Scenario frames (gb_paced) tick every FRAME of LCD-on GB time, like an emulator's frame step:
// it counts PPU cycles, and the PPU stops while the LCD is off. gb_pace_next is the LCD-on time of
// the next tick (first VBlank at boot); scenarios move it to their alignment point + FRAME. Real-time
// sync is separate and also runs while the LCD is off.
static uint32_t ppu_acc, ppu_on_at;       // LCD-on GB cycles before / since the LCD last came on
volatile uint32_t gb_pace_next;
static uint32_t div_base, tima_at;        // DIV origin; next TIMA overflow
static uint32_t vblank_line_done, lyc_done, hblank_line;  // per-frame event progress
static uint8_t lcd_first_line;            // still in line 0 of the first frame after the LCD came on

static const uint16_t tac_period[4] = {1024, 16, 64, 256};
uint32_t gb_irq_ready;
static uint32_t if_at[5];                 // GB time each IF bit was raised
// Calibration knob: cycles from line 144 to the VBlank handler's first instruction beyond our
// 20-cycle dispatch (the interrupted instruction finishing, IF latency). Measured against Mesen:
// DQ1&2's handler writes C890 at line 4 dot 179 there (43 cycles after line 144 dot 0).
#define VBLANK_IRQ_DELAY 39

static void raise_irq(unsigned bit, uint32_t at)
{
    if (!(IF & (1u << bit))) if_at[bit] = at;
    IF |= 1u << bit;
}

static uint32_t lcd_pos(void)             // cycles into the current LCD frame
{
    uint32_t f = gb.cyc - frame_start;
    while (f >= FRAME) { frame_start += FRAME; f -= FRAME; vblank_line_done = lyc_done = 0; hblank_line = 0; }
    if (f >= LINE) lcd_first_line = 0;
    return f;
}

// f / 456 for f < FRAME without a divide (Thumb-1 has no long multiply, so GCC would call
// __aeabi_uidiv; games poll STAT/LY in tight loops). Exact over the frame: 456 = 8 * 57.
static inline uint32_t line_of(uint32_t f) { return ((f >> 3) * 9199) >> 19; }

// Dots within a line, Mesen's CGB timing: OAM search from dot 4, drawing from 84, HBlank from
// HBLANK_AT (SCX=0, no sprites or window: measured 256..259); dots 0-3 keep the previous mode; LYC
// compares from dot 4 and the VBlank IRQ comes at line 144 dot 4. Line 153 reads LY=0 from dot 6
// and matches LYC=0 from dot 12. ponytail: drawing length ignores SCX, sprites and the window.
#define MODE2_AT 4
#define MODE3_AT 84
#define HBLANK_AT 256
#define LINE_IRQ_AT 4

static uint32_t ly_at(uint32_t f)         // LY register for frame position f
{
    uint32_t line = line_of(f);
    return line == 153 && f - 153 * LINE >= 6 ? 0 : line;
}

static int lyc_line(uint32_t f)           // the LY the LYC comparator sees, -1 between lines
{
    uint32_t line = line_of(f), x = f - line * LINE;
    if (line == 153) return x < 6 ? -1 : x < 8 ? 153 : x < 12 ? -1 : 0;
    return line && x < LINE_IRQ_AT ? -1 : (int)line;
}

static uint32_t lyc_event(uint8_t lyc)    // frame position where LY=LYC starts matching
{
    return lyc == 0 ? 153 * LINE + 12 : lyc == 153 ? 153 * LINE + 6 : lyc * LINE + LINE_IRQ_AT;
}

static uint8_t ppu_ly(void) { return (LCDC & 0x80) ? ly_at(lcd_pos()) : 0; }

// Polling skip: reading LY or STAT again within 64 cycles and getting the same value means a
// wait loop. Nothing it can observe changes before the next line / mode boundary, so GB time
// jumps there (the loop would only have burned cycles). Events are handled at its back-edge.
static uint32_t poll_cyc, poll_period;
static uint8_t poll_reg, poll_val;

static int polling(uint8_t reg, uint8_t val)
{
    poll_period = gb.cyc - poll_cyc;
    int again = reg == poll_reg && val == poll_val && poll_period < 64 && poll_period;
    poll_reg = reg;
    poll_val = val;
    poll_cyc = gb.cyc;
    return again;
}

static void skip_to_boundary(int mode_only)
{
    uint32_t f = lcd_pos(), line = line_of(f), x = f - line * LINE;
    uint32_t to = LINE;                           // next line
    if (line == 153) to = x < 6 ? 6 : !mode_only ? LINE : x < 8 ? 8 : x < 12 ? 12 : LINE;
    else if (mode_only && line < 144) to = x < MODE2_AT ? MODE2_AT : x < MODE3_AT ? MODE3_AT : x < HBLANK_AT ? HBLANK_AT : LINE;
    else if (mode_only && x < LINE_IRQ_AT) to = LINE_IRQ_AT;
    // Land on the loop's own read grid: hardware sees the change at the first read after the boundary.
    gb.cyc += (to - x + poll_period - 1) / poll_period * poll_period;
}

static void inject_poll(void);

static uint32_t ppu_time(void) { return ppu_acc + ((LCDC & 0x80) ? gb.cyc - ppu_on_at : 0); }

static void sync_real_time(void)          // one GB frame of time done: don't run ahead of real time
{
    sync_frame++;
    if (gb_frames > sync_frame) {         // GB was slower than real time: that time is lost, as on
        gb_lag_frames += gb_frames - sync_frame;  // hardware — never catch up by running fast later
        sync_frame = gb_frames;
    }
    while (gb_frames < sync_frame) Halt();
    inject_poll();
}

static void pace(void)                    // a scenario frame: count it, then sync real time
{
    gb_paced++;
    sync_real_time();
}

static void schedule(void)
{
    uint32_t now = gb.cyc, next = now + FRAME;
    if (!(LCDC & 0x80)) next = pace_at;
    if (LCDC & 0x80) {
        uint32_t f = lcd_pos(), base = now - f;
        uint32_t t = base + FRAME;                                    // next frame start
        if (!vblank_line_done) t = base + 144 * LINE + LINE_IRQ_AT;
        if (t - now < next - now) next = t;
        int32_t d = gb_pace_next - ppu_time();
        if (now + (d > 0 ? d : 0) - now < next - now) next = now + (d > 0 ? d : 0);
        if ((io[0x41] & 0x40) && !lyc_done && io[0x45] < 154 && lyc_event(io[0x45]) >= f) {
            t = base + lyc_event(io[0x45]);
            if (t - now < next - now) next = t;
        }
        if ((io[0x41] & 0x28) && hblank_line < 144) {
            t = base + hblank_line * LINE + HBLANK_AT;
            if (t - now < next - now) next = t;
        }
    }
    if ((io[0x07] & 4) && tima_at - now < next - now) next = tima_at;
    gb_next_event = next;
}

HOT static void run_events(void)
{
    uint32_t now = gb.cyc;
    // Real-time pacing happens at each GB VBlank, so GB VBlank (and the joypad read that usually
    // follows it) lands right after a GBA VBlank — the same input phase as a GB emulator frame.
    // With the LCD off there is no VBlank: pace every FRAME cycles instead.
    if (!(LCDC & 0x80) && (int32_t)(now - pace_at) >= 0) {
        pace_at += FRAME;
        sync_real_time();
    }
    if (LCDC & 0x80) {
        uint32_t f = lcd_pos();
        uint32_t base = now - f;
        if ((int32_t)(ppu_time() - gb_pace_next) >= 0) {
            gb_pace_next += FRAME;
            pace();
        }
        if (!vblank_line_done && f >= 144 * LINE + LINE_IRQ_AT) {
            vblank_line_done = 1;
            raise_irq(0, base + 144 * LINE + LINE_IRQ_AT + VBLANK_IRQ_DELAY);
            if (io[0x41] & 0x10) raise_irq(1, base + 144 * LINE + LINE_IRQ_AT + VBLANK_IRQ_DELAY);
        }
        if ((io[0x41] & 0x40) && !lyc_done && io[0x45] < 154 && f >= lyc_event(io[0x45])) {
            lyc_done = 1;
            raise_irq(1, base + lyc_event(io[0x45]));
        }
        while ((io[0x41] & 0x28) && hblank_line < 144 && f >= hblank_line * LINE + HBLANK_AT) {
            raise_irq(1, base + hblank_line * LINE + HBLANK_AT);
            hblank_line++;
        }
    }
    if ((io[0x07] & 4) && (int32_t)(now - tima_at) >= 0) {
        io[0x05] = io[0x06];
        raise_irq(2, tima_at);
        tima_at += (256 - io[0x06]) * tac_period[io[0x07] & 3];
    }
    update_irq();
    schedule();
}

static void timer_restart(void)
{
    tima_at = gb.cyc + (256 - io[0x05]) * tac_period[io[0x07] & 3];
}

static uint8_t timer_tima(void)
{
    if (!(io[0x07] & 4)) return io[0x05];
    uint32_t left = tima_at - gb.cyc, period = tac_period[io[0x07] & 3];
    return 256 - (left + period - 1) / period;
}

HOT static void oam_dma(uint8_t src)
{
    for (int i = 0; i < 0xA0; i++) oam[i] = rd8(src << 8 | i);
    ppu_all_obj();
}

static void vram_dma(uint8_t ctl)         // CGB HDMA5. ponytail: HBlank mode copies at once too
{
    uint16_t src = (io[0x51] << 8 | io[0x52]) & 0xFFF0, dst = (io[0x53] << 8 | io[0x54]) & 0x1FF0;
    for (unsigned i = 0, n = ((ctl & 0x7F) + 1) * 16; i < n; i++)
        gb_write_slow(0x8000 | ((dst + i) & 0x1FFF), rd8(src + i));
    gb.cyc += ((ctl & 0x7F) + 1) * 32;    // the CPU is halted 8 M-cycles per 16-byte block
    io[0x55] = 0xFF;
}

static void palette_write(uint8_t *spec, uint16_t *ram, uint8_t v, int bg)  // BCPD/OCPD, auto-increment
{
    unsigned i = *spec & 0x3F, e = i >> 1;
    uint16_t *p = &ram[e];
    *p = (i & 1) ? (*p & 0x00FF) | v << 8 : (*p & 0xFF00) | v;
    if (*spec & 0x80) *spec = 0x80 | ((i + 1) & 0x3F);
    if (bg) pal_bg_mem[(e >> 2) * 16 + 1 + (e & 3)] = *p;
    else pal_obj_mem[(e >> 2) * 16 + (e & 3)] = *p;
}

// ---- page tables (see rd8/wr8) --------------------------------------------------------

#define SRAM_PTR ((uint8_t *)MEM_SRAM)

static void map_pages(unsigned first, unsigned n, const uint8_t *r, uint8_t *w)
{
    for (unsigned i = 0; i < n; i++) {
        gb_rpage[first + i] = r ? r + i * 256 : 0;
        gb_wpage[first + i] = w ? w + i * 256 : 0;
    }
}

static void map_wramx(void)               // D000-DFFF and its echo F000-FDFF
{
    uint8_t *x = gb_wramx + 0xD000;
    map_pages(0xD0, 16, x, x);
    map_pages(0xF0, 14, x, x);
}

static void map_vram(void)                // reads direct from the shadow; writes mirror to GBA VRAM
{
    map_pages(0x80, 32, vram + (cgb_mode && (io[0x4F] & 1)) * 0x2000, 0);
}

static void map_cart_ram(void)
{
    unsigned pages = gb_rom[0x149] == 0 ? 0 : 32;  // ponytail: one 8 KiB bank
    map_pages(0xA0, 32, 0, 0);
    if (ram_enable && pages) map_pages(0xA0, pages, SRAM_PTR, SRAM_PTR);
}

// ---- MBC ------------------------------------------------------------------------------

static void set_rom_bank(unsigned bank)
{
    unsigned banks = 2u << gb_rom[0x148];
    bank &= banks - 1;
    gb.rom_bank = bank;
    gb_romx = (const uint8_t *)((uintptr_t)gb_rom + bank * 0x4000 - 0x4000);  // biased: index with 0x4000..0x7FFF
}

static void mbc_write(uint16_t a, uint8_t v)
{
    uint8_t type = gb_rom[0x147];
    if (type == 0x00 || type == 0x08 || type == 0x09) return;  // ROM only
    if (type >= 0x19 && type <= 0x1E) {                         // MBC5
        if (a < 0x2000) { ram_enable = (v & 0x0F) == 0x0A; map_cart_ram(); }
        else if (a < 0x3000) set_rom_bank((gb.rom_bank & 0x100) | v);
        else if (a < 0x4000) set_rom_bank((gb.rom_bank & 0xFF) | (v & 1) << 8);
        // ponytail: 0x4000-0x5FFF RAM bank ignored — single 8 KiB bank (DQ1&2); add banking for 32 KiB carts
        return;
    }
    // ponytail: MBC1/2/3 not yet — trap instead of silently mis-banking
    gb_trap(0, a, TRAP_NO_ENTRY);
}

// ---- IO -------------------------------------------------------------------------------

// ---- APU status (no audio output) -------------------------------------------------------
// Sound drivers read registers back and poll NR52's channel bits, and their cycle cost follows
// what they see. Model the read masks and when a channel is on: trigger with its DAC on, until
// the DAC goes off or an enabled length counter runs out. Length clocks every 16384 cycles on
// the DIV grid. ponytail: no sweep overflow, no frame-sequencer edge cases.
static const uint8_t apu_or[0x20] = {
    0x80, 0x3F, 0x00, 0xFF, 0xBF, 0xFF, 0x3F, 0x00, 0xFF, 0xBF, 0x7F, 0xFF, 0x9F, 0xFF, 0xBF, 0xFF,
    0xFF, 0x00, 0x00, 0xBF, 0x00, 0x00, 0x70, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static uint8_t ch_on, ch_len_on;          // NR52 bits 0-3; length enabled per channel
static uint16_t ch_len[4];                // length counter loaded by NRx1
static uint32_t ch_end[4];                // GB time the length counter silences the channel

static uint8_t apu_status(void)
{
    for (int n = 0; n < 4; n++)
        if ((ch_on & ch_len_on & (1 << n)) && (int32_t)(gb.cyc - ch_end[n]) >= 0) ch_on &= ~(1 << n);
    return ch_on;
}

static void apu_write(uint8_t r, uint8_t v)
{
    static const uint8_t nrx4[4] = {0x14, 0x19, 0x1E, 0x23}, nrx2[4] = {0x12, 0x17, 0x1A, 0x21};
    switch (r) {
    case 0x11: case 0x16: case 0x20: case 0x1B: {  // length load: a running channel counts from it now
        unsigned n = r == 0x11 ? 0 : r == 0x16 ? 1 : r == 0x20 ? 3 : 2;
        apu_status();
        ch_len[n] = n == 2 ? 256 - v : 64 - (v & 63);
        if (ch_on & ch_len_on & (1 << n))
            ch_end[n] = gb.cyc + (16384 - ((gb.cyc - div_base) & 16383)) + (ch_len[n] - 1) * 16384u;
        return;
    }
    case 0x12: case 0x17: case 0x21: if (!(v & 0xF8)) ch_on &= ~(1 << (r == 0x12 ? 0 : r == 0x17 ? 1 : 3)); return;
    case 0x1A: if (!(v & 0x80)) ch_on &= ~4; return;
    case 0x26: if (!(v & 0x80)) ch_on = 0; return;
    }
    for (int n = 0; n < 4; n++) {
        if (r != nrx4[n]) continue;
        apu_status();                         // settle expiry under the old length setting
        uint8_t bit = 1 << n, dac = n == 2 ? io[0x1A] & 0x80 : io[nrx2[n]] & 0xF8;
        if (v & 0x80) {
            if (dac) ch_on |= bit;
            if (!ch_len[n]) ch_len[n] = n == 2 ? 256 : 64;
        }
        if (v & 0x40) {
            ch_len_on |= bit;
            uint32_t next = gb.cyc + (16384 - ((gb.cyc - div_base) & 16383));
            ch_end[n] = next + (ch_len[n] - 1) * 16384u;
        } else {
            ch_len_on &= ~bit;
        }
    }
}

HOT uint8_t gb_read_io(uint8_t r)
{
    switch (r) {
    case 0x00: {                          // P1: GBA KEYINPUT has the same active-low layout
        uint8_t sel = io[0x00], keys = 0x0F;
        u16 k = gb_keys > 0x3FF ? REG_KEYINPUT : gb_keys;
        if (!(sel & 0x10)) keys &= (k >> 4) & 0x0F;  // Right Left Up Down
        if (!(sel & 0x20)) keys &= k & 0x0F;         // A B Select Start
        return 0xC0 | (sel & 0x30) | keys;
    }
    case 0x04: return (gb.cyc - div_base) >> 8;  // DIV: 16384 Hz
    case 0x05: return timer_tima();
    case 0x0F: return 0xE0 | IF;
    case 0x41: return gb_read_stat();
    case 0x44: return gb_read_ly();
    case 0x69: return bg_pal[(io[0x68] & 0x3F) >> 1] >> ((io[0x68] & 1) * 8);
    case 0x6B: return obj_pal[(io[0x6A] & 0x3F) >> 1] >> ((io[0x6A] & 1) * 8);
    case 0x26: return (io[0x26] & 0x80) | 0x70 | apu_status();
    default: return (uint8_t)(r - 0x10) < 0x20 ? io[r] | apu_or[r - 0x10] : io[r];
    }
}

HOT uint8_t gb_read_stat(void)
{
    for (int pass = 0;; pass++) {
        uint8_t mode = 0, ly = 0;
        if (LCDC & 0x80) {
            uint32_t f = lcd_pos(), line = line_of(f), x = f - line * LINE;
            if (line == 0 && lcd_first_line) mode = x < 87 ? 0 : x < HBLANK_AT + 3 ? 3 : 0;
            else if (line >= 144) mode = line == 144 && x < MODE2_AT ? 0 : 1;
            else if (x < MODE2_AT) mode = line ? 0 : 1;   // previous line's HBlank / VBlank (CGB)
            else mode = x < MODE3_AT ? 2 : x < HBLANK_AT ? 3 : 0;
            ly = lyc_line(f) == io[0x45];
        }
        uint8_t v = 0x80 | (io[0x41] & 0x78) | ((LCDC & 0x80) && ly) << 2 | mode;
        if (pass || !(LCDC & 0x80) || !polling(0x41, v)) return v;
        skip_to_boundary(1);
    }
}

HOT uint8_t gb_read_ly(void)
{
    uint8_t v = ppu_ly();
    if (!(LCDC & 0x80) || !polling(0x44, v)) return v;
    skip_to_boundary(0);
    return poll_val = ppu_ly();
}

HOT void gb_write_io(uint8_t r, uint8_t v)
{
    uint8_t old = io[r];
    io[r] = v;
    if ((uint8_t)(r - 0x10) < 0x17) apu_write(r, v);
    switch (r) {
    case 0x04: div_base = gb.cyc; break;
    case 0x05: io[0x05] = v; timer_restart(); schedule(); break;
    case 0x07: io[0x05] = timer_tima(); io[0x07] = old; io[0x05] = timer_tima(); io[0x07] = v; timer_restart(); schedule(); break;
    case 0x0F:
        for (unsigned b = 0; b < 5; b++) if ((v & ~old) >> b & 1) if_at[b] = gb.cyc;
        IF = v & 0x1F; update_irq(); break;
    case 0x40:
        if (!(old & 0x80) && (v & 0x80)) {  // the PPU restarts at line 0 dot 7, OAM search skipped (Mesen)
            frame_start = gb.cyc - 7;
            ppu_on_at = gb.cyc;
            lcd_first_line = 1;
            vblank_line_done = lyc_done = hblank_line = 0;
        }
        if ((old & 0x80) && !(v & 0x80)) { ppu_acc += gb.cyc - ppu_on_at; pace_at = gb.cyc + FRAME; }
        ppu_lcdc(old);
        schedule();
        break;
    case 0x41: case 0x45: schedule(); break;
    case 0x42: case 0x43: ppu_scroll(); break;
    case 0x4A: case 0x4B: ppu_scroll(); ppu_dispcnt(); break;
    case 0x46: oam_dma(v); break;
    case 0x47: case 0x48: case 0x49: if (!cgb_mode) ppu_palettes(); break;
    case 0x55: if (cgb_mode) vram_dma(v); break;
    case 0x69: palette_write(&io[0x68], bg_pal, v, 1); break;
    case 0x6B: palette_write(&io[0x6A], obj_pal, v, 0); break;
    case 0x70:                            // SVBK
        if (cgb_mode) {
            unsigned b = (v & 7) ? (v & 7) : 1;
            uint8_t *base = b == 1 ? gb_wram + 0x1000 : wram_hi + (b - 2) * 0x1000;
            gb_wramx = (uint8_t *)((uintptr_t)base - 0xD000);
            map_wramx();
        }
        break;
    case 0x4F: map_vram(); break;
    }
}

HOT uint8_t gb_read_slow(uint16_t a)
{
    if (a >= 0xFF80) return gb_hram[a - 0xFF80];  // includes IE at FFFF
    if (a < 0xA000) return vram[(cgb_mode && (io[0x4F] & 1)) * 0x2000 + a - 0x8000];
    if (a < 0xC000) return ram_enable ? ((volatile uint8_t *)MEM_SRAM)[a - 0xA000] : 0xFF;
    if (a < 0xFE00) return rd8(a - 0x2000);  // echo RAM
    if (a < 0xFEA0) return oam[a - 0xFE00];
    if (a < 0xFF00) return 0xFF;
    return gb_read_io(a & 0x7F);
}

HOT void gb_write_slow(uint16_t a, uint8_t v)
{
    if (a >= 0xFF80) {                    // hottest slow-path case: the GB stack usually lives in HRAM/WRAM
        gb_hram[a - 0xFF80] = v;
        if (a == 0xFFFF) { gb_irq_ready = gb.cyc; update_irq(); }
        return;
    }
    if (a < 0x8000) { mbc_write(a, v); return; }
    if (a < 0xA000) {
        unsigned off = a - 0x8000, bank = cgb_mode && (io[0x4F] & 1);
        vram[bank * 0x2000 + off] = v;
        if (off < 0x1800) ppu_tile_row(bank, off);
        else ppu_map_write(off - 0x1800);  // tile index (bank 0) or CGB attributes (bank 1)
        return;
    }
    if (a < 0xC000) { if (ram_enable) ((volatile uint8_t *)MEM_SRAM)[a - 0xA000] = v; return; }
    if (a < 0xFE00) { wr8(a - 0x2000, v); return; }
    if (a < 0xFEA0) { oam[a - 0xFE00] = v; ppu_obj((a - 0xFE00) >> 2); return; }
    if (a < 0xFF00) return;
    gb_write_io(a & 0x7F, v);
}

HOT uint16_t gb_pop16_slow(uint16_t sp) { return rd8(sp) | rd8(sp + 1) << 8; }
HOT void gb_push16_slow(uint16_t sp, uint16_t v) { wr8(sp + 1, v >> 8); wr8(sp, v); }

// ---- CPU control ------------------------------------------------------------------------


HOT int gb_service_irq(uint16_t resume)
{
    uint8_t pending = IE & IF & 0x1F;
    if (!pending) { update_irq(); return 0; }
    unsigned n = __builtin_ctz(pending);
    IF &= ~(1 << n);
    gb.ime = 0;
    gb.irq = 0;
    // We only notice interrupts at safepoints; hardware takes them at the next instruction. Run
    // the handler at the time it became serviceable and add the skipped cycles back afterwards,
    // so what it observes (LY, DIV, its own writes) lands when it would. Never step back across
    // the frame or DIV origin.
    uint32_t at = (int32_t)(gb_irq_ready - if_at[n]) > 0 ? gb_irq_ready : if_at[n];
    uint32_t late = gb.cyc - at, lim = gb.cyc - div_base;
    if ((LCDC & 0x80) && gb.cyc - frame_start < lim) lim = gb.cyc - frame_start;
    if ((int32_t)late < 0 || late > lim) late = (int32_t)late < 0 ? 0 : lim;
    gb.cyc -= late;
    gb.cyc += 20;                         // interrupt dispatch
    if (!gb_vectors[n]) gb_trap(0, 0x40 + 8 * n, TRAP_NO_ENTRY);
    uint16_t fs = gb.frame_sp;
    push16(resume);
    gb.frame_sp = gb.sp;
    gb_vectors[n](0x40 + 8 * n);
    int dead = gb_trampoline();
    gb.frame_sp = fs;
    gb.cyc += late;
    return dead || gb.ret != resume;
}

HOT int gb_poll(uint16_t resume)
{
    if ((int32_t)(gb.cyc - gb_next_event) >= 0) run_events();
    return gb.irq ? gb_service_irq(resume) : 0;
}

int gb_halt(uint16_t resume)
{
    while (!(IE & IF & 0x1F)) {           // skip GB time to the next event
        if ((int32_t)(gb_next_event - gb.cyc) > 0) gb.cyc = gb_next_event;
        run_events();
    }
    return gb.ime ? gb_service_irq(resume) : 0;
}

void gb_stop(uint16_t resume)
{
    uint8_t *key1 = &io[0x4D];
    if (cgb_mode && (*key1 & 1)) { *key1 = (*key1 ^ 0x80) & 0x80; return; }  // speed switch: no-op here
    gb_trap(gb.rom_bank, resume - 2, TRAP_STOP);
}

HOT int gb_trampoline(void)
{
    while (gb.jfn) {
        if (gb.sp > gb.frame_sp) return 1;  // this frame's return address is gone: propagate
        GbFn f = gb.jfn;
        gb.jfn = 0;
        f(gb.jat);
    }
    return 0;
}

// Native replacements for code games copy into HRAM (no interpreter). Unknown RAM code traps.
// ponytail: byte-pattern table; analyze the ROM source of copied routines if this list grows.
static void hram_dma_imm(uint16_t at)     // 3E nn E0 46 ...: LD A,nn / LDH (46),A / wait / RET
{
    const uint8_t *p = &gb_hram[at - 0xFF80];
    // 3E kk 3D 20 FD C9: LD A,kk / DEC A / JR NZ,-3 / RET; anything else: the 160-M-cycle DMA wait
    unsigned n = p[4] == 0x3E && p[6] == 0x3D && p[7] == 0x20 ? (p[5] ? p[5] : 256) : 40;
    gb.cyc += 8 + 12 + 8 + n * 16 - 4 + 16;
    oam_dma(p[1]);
    gb.a = 0;
    gb.ret = pop16();
}

static void hram_io_wait(uint16_t at)     // E2 05 20 FD C9: LDH (C),A / DEC B / JR NZ,-3 / RET
{
    (void)at;
    gb.cyc += 8 + (gb.b ? gb.b : 256) * 16 - 4 + 16;
    gb_write_io(gb.c & 0x7F, gb.a);       // C=$46 starts OAM DMA
    gb.b = 0;
    gb.zf = 1; gb.nf = 1; gb.hf = 0;
    gb.ret = pop16();
}

static void hram_jp(uint16_t at)          // C3 lo hi: a jump vector the game rewrites at run time
{
    const uint8_t *p = &gb_hram[at - 0xFF80];
    gb.cyc += 16;
    gb.jat = p[1] | p[2] << 8;
    gb.jfn = gb_lookup(gb.jat);
}

static const struct { uint8_t len, bytes[5]; GbFn fn; } hram_stubs[] = {
    {5, {0xE2, 0x05, 0x20, 0xFD, 0xC9}, hram_io_wait},
};

static GbFn hram_stub(uint16_t addr)
{
    const uint8_t *p = &gb_hram[addr - 0xFF80];
    if (p[0] == 0x3E && p[2] == 0xE0 && p[3] == 0x46) return hram_dma_imm;  // operand byte varies
    if (p[0] == 0xC3 && addr <= 0xFFFC) return hram_jp;
    for (unsigned i = 0; i < sizeof hram_stubs / sizeof *hram_stubs; i++) {
        unsigned n = hram_stubs[i].len, j = 0;
        while (j < n && p[j] == hram_stubs[i].bytes[j]) j++;
        if (j == n) return hram_stubs[i].fn;
    }
    return 0;
}

static struct { uint32_t key; GbFn fn; } lookup_cache[512] IWRAM_DATA;  // direct-mapped

HOT GbFn gb_lookup(uint16_t addr)
{
    uint32_t bank = addr < 0x4000 ? 0 : gb.rom_bank, key = bank << 16 | addr;
    unsigned slot = (key ^ key >> 9) & 511;
    if (lookup_cache[slot].key == key && lookup_cache[slot].fn) return lookup_cache[slot].fn;
    if (addr >= 0x8000) {
        GbFn stub = addr >= 0xFF80 ? hram_stub(addr) : 0;
        if (!stub) gb_trap(0xFFFF, addr, TRAP_NO_ENTRY);  // RAM code without a native hook
        return stub;
    }
    uint32_t lo = 0, hi = gb_entry_count;
    while (lo < hi) {
        uint32_t mid = (lo + hi) / 2;
        if (gb_entries[mid].key < key) lo = mid + 1; else hi = mid;
    }
    if (lo == gb_entry_count || gb_entries[lo].key != key) gb_trap(bank, addr, TRAP_NO_ENTRY);
    lookup_cache[slot].key = key;
    lookup_cache[slot].fn = gb_entries[lo].fn;
    return gb_entries[lo].fn;
}

void gb_trap(uint16_t bank, uint16_t addr, uint8_t reason)
{
    static const char *names[] = {"?", "undefined opcode", "no compiled entry", "ret mismatch", "stop", "exit"};
    gb_trap_info.bank = bank;
    gb_trap_info.addr = addr;
    gb_trap_info.reason = reason;
    gb_trap_info.hit = 1;
    REG_IME = 0;
    REG_DISPCNT = DCNT_MODE0 | DCNT_BG0;
    REG_BG0HOFS = REG_BG0VOFS = 0;
    pal_bg_mem[0] = CLR_BLACK;            // the game's palettes may hide TTE's ink colour
    for (int i = 1; i < 256; i++) pal_bg_mem[i] = CLR_WHITE;
    tte_init_se_default(0, BG_CBB(2) | BG_SBB(31));
    tte_init_con();                       // tte_printf goes through stdio: without this it prints nothing
    tte_printf("#{P:8,8}gbforge trap\n#{X:8}%s\n#{X:8}at %02X:%04X", names[reason < 6 ? reason : 0], bank, addr);
    for (;;) Halt();
}

// ---- state injection (verification) ---------------------------------------------------
// `gb2gba verify` can replace the whole GB state with one captured from the original, so a
// comparison restarts from identical state instead of accumulating timing drift (an idle-loop RNG
// follows every cycle). The tool fills gb_inject (fixed layout below, mirrored in
// scripts/emucap_run.py) and sets .armed; the next GB frame tick applies it and restarts native
// code at .pc, which must be a compiled entry (the tool captures at one).
struct GbInject {
    uint32_t armed;                       // 0
    uint16_t pc, sp;                      // 4
    uint8_t a, f, b, c, d, e, h, l;       // 8
    uint8_t ime, ie, ram_enable, pad;     // 16
    uint16_t rom_bank, line, dot, div;    // 20: PPU position of the capture, 16-bit DIV counter
    uint8_t io[0x80];                     // 28: FF00-FF7F
    uint8_t hram[0x80];                   // 156: FF80-FFFE
    uint8_t bgpal[64], objpal[64];        // 284, 348: CGB palette RAM
    uint8_t oam[0xA0];                    // 412
    uint8_t wram[0x8000];                 // 572: CGB banks 0-7
    uint8_t vram[0x4000];                 // 33340: banks 0-1
};
_Static_assert(__builtin_offsetof(struct GbInject, io) == 28 && __builtin_offsetof(struct GbInject, oam) == 412
               && __builtin_offsetof(struct GbInject, vram) == 33340, "GbInject layout is shared with emucap_run.py");
struct GbInject gb_inject EWRAM_BSS;
static jmp_buf gb_restart;

static void inject_apply(void)
{
    struct GbInject *s = &gb_inject;
    s->armed = 0;
    memcpy(gb_wram, s->wram, 0x2000);
    memcpy(wram_hi, s->wram + 0x2000, sizeof wram_hi);
    memcpy(vram, s->vram, sizeof vram);
    memcpy(oam, s->oam, sizeof oam);
    memcpy(gb_hram, s->hram, 0x7F);
    IE = s->ie;
    for (int i = 0; i < 32; i++) {
        bg_pal[i] = s->bgpal[2 * i] | s->bgpal[2 * i + 1] << 8;
        obj_pal[i] = s->objpal[2 * i] | s->objpal[2 * i + 1] << 8;
    }
    memcpy(io, s->io, 0x80);
    ram_enable = s->ram_enable;
    set_rom_bank(s->rom_bank);
    map_cart_ram();
    gb_write_io(0x70, io[0x70]);          // SVBK mapping
    map_vram();
    for (unsigned t = 0; t < 0x1800; t += 2) ppu_tile_row(0, t), ppu_tile_row(1, t);
    for (unsigned off = 0; off < 0x800; off++) ppu_map_write(off);
    ppu_lcdc(LCDC);
    ppu_all_obj();
    ppu_palettes();
    ppu_scroll();
    // GB time continues from the capture's frame position; it is also where frames now tick
    uint32_t f = s->line * LINE + s->dot;
    frame_start = gb.cyc - f;
    vblank_line_done = f >= 144 * LINE + LINE_IRQ_AT;
    lyc_done = f >= lyc_event(io[0x45]);
    hblank_line = s->line >= 144 ? 144 : s->line + (s->dot >= HBLANK_AT);
    ppu_acc = 0;                          // the original steps whole frames from the capture
    ppu_on_at = gb.cyc;
    gb_pace_next = FRAME;
    div_base = gb.cyc - s->div;
    if (io[0x07] & 4) timer_restart();
    for (int n = 0; n < 5; n++) if_at[n] = gb.cyc;
    gb_irq_ready = gb.cyc;
    poll_reg = 0;
    gb.a = s->a; unpack_f(s->f);
    gb.b = s->b; gb.c = s->c; gb.d = s->d; gb.e = s->e; gb.h = s->h; gb.l = s->l;
    gb.sp = s->sp;
    gb.ime = s->ime;
    update_irq();
    schedule();
    longjmp(gb_restart, 1);               // drop every native frame: the GB stack is the new one
}

static void inject_poll(void) { if (gb_inject.armed) inject_apply(); }

// ---- boot -----------------------------------------------------------------------------

#ifdef GB_PROFILE
// Sampling profiler (gb2gba port --profile): a timer IRQ at ~2 kHz, unrelated to video phase,
// records the interrupted PC (BIOS IRQ frame: lr_irq at 0x03007F9C). Read gb_prof_pcs via emucap.
uint32_t gb_prof_pcs[4096] EWRAM_BSS;
volatile uint32_t gb_prof_count;

static void prof_isr(void)
{
    gb_prof_pcs[gb_prof_count++ & 4095] = *(volatile uint32_t *)0x03007F9C - 4;
}
#endif

static void vblank_isr(void)
{
    oam_copy(oam_mem, obj_shadow, 40);
    gb_frames++;                          // real time; GB interrupts come from GB time (run_events)
}

int main(void)
{
    REG_WAITCNT = 0x4317;                 // 3/1 ROM waitstates + prefetch
    cgb_mode = gb_rom[0x143] & 0x80;
    set_rom_bank(1);
    gb_wramx = (uint8_t *)((uintptr_t)gb_wram + 0x1000 - 0xD000);
    map_pages(0xC0, 16, gb_wram, gb_wram);
    map_pages(0xE0, 16, gb_wram, gb_wram);  // echo of C000-CFFF
    map_wramx();
    map_vram();
    map_cart_ram();
    if (cgb_mode) {                       // post-boot-ROM register state (Pan Docs)
        gb.a = 0x11; gb.zf = 1;
        gb.b = 0x00; gb.c = 0x00; gb.d = 0xFF; gb.e = 0x56; gb.h = 0x00; gb.l = 0x0D;
        // ...and timing: the CGB boot ROM hands over at LY 146, dot 179, DIV counter 0xD4DE
        // (measured in Mesen at PC=0100). Games that run an RNG in their idle loop depend on it.
        frame_start = -(146 * LINE + 179);
        div_base = -0xD4DE;
        hblank_line = 144;
    } else {
        gb.a = 0x01; gb.zf = 1; gb.hf = 1; gb.cf = 1;
        gb.b = 0x00; gb.c = 0x13; gb.d = 0x00; gb.e = 0xD8; gb.h = 0x01; gb.l = 0x4D;
    }
    gb.sp = 0xFFFE;
    gb.frame_sp = 0xFFFF;                 // top level: no C call frame to protect
    io[0x00] = 0x30;
    io[0x40] = 0x91;
    io[0x47] = 0xFC;
    ppu_init();
    pace_at = FRAME;
    gb_pace_next = (144 * LINE + LINE_IRQ_AT + FRAME - (0u - frame_start) % FRAME) % FRAME;  // first VBlank
    schedule();
    irq_init(NULL);
    irq_add(II_VBLANK, vblank_isr);
#ifdef GB_PROFILE
    REG_TM2D = -8192;                     // 16.78 MHz / 8192 ≈ 2048 Hz
    REG_TM2CNT = TM_ENABLE | TM_IRQ | TM_FREQ_1;
    irq_add(II_TIMER2, prof_isr);
#endif
    if (setjmp(gb_restart)) {             // after a state injection: dispatch to its PC
        gb.frame_sp = 0xFFFF;
        gb.jat = gb_inject.pc;
        gb.jfn = gb_lookup(gb_inject.pc);
        for (;;) {
            if (!gb.jfn) { gb.jat = gb.ret; gb.jfn = gb_lookup(gb.ret); }
            GbFn fn = gb.jfn;
            gb.jfn = 0;
            fn(gb.jat);
        }
    }
    gb_entry();
    gb_trap(0, 0, TRAP_EXIT);
}
