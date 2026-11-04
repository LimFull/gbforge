// GBA runtime for gbforge-generated code: GB memory map, MBC, IO, interrupts, compat PPU.
//
// Compat renderer: GB VRAM writes are mirrored straight into GBA VRAM (BG0, 4bpp tiles,
// two 32x32 screenblocks), so the GBA hardware draws the GB background with no per-frame
// copy. The 160x144 picture is letterboxed at (40,8) with WIN0.
// ponytail: BG + DMG/CGB BG palettes only; window, sprites, CGB attributes and audio come next.
#include <tonc.h>
#include "gb.h"

GbCpu gb IWRAM_DATA;
const uint8_t *gb_romx;
uint8_t gb_wram[0x8000] EWRAM_BSS;
uint8_t *gb_wramx;
uint8_t gb_hram[0x80] IWRAM_DATA;

static uint8_t vram[0x4000] EWRAM_BSS;   // GB VRAM shadow (2 CGB banks)
static uint8_t oam[0xA0] EWRAM_BSS;
static uint8_t io[0x80] IWRAM_DATA;
static uint8_t cgb_mode, ram_enable;
static uint16_t bg_pal[32];               // CGB BG palette RAM (BCPS/BCPD)

// Exposed for verification tools (emucap reads it through gb2gba.map.json).
volatile struct { uint16_t bank, addr; uint8_t reason, hit; } gb_trap_info;

#define IF io[0x0F]
#define IE gb_hram[0x7F]
#define LCDC io[0x40]

static void update_irq(void) { gb.irq = gb.ime && (IE & IF & 0x1F); }

// ---- PPU (compat) -------------------------------------------------------------------

#define SB_MAP0 28
#define SB_MAP1 29
static u32 spread[256];                   // bit i (MSB first) → nibble i of a 4bpp row

static u16 dmg_shade(int s) { static const u16 c[4] = {0x7FFF, 0x5294, 0x294A, 0x0000}; return c[s & 3]; }

static void ppu_tile_row(unsigned off)    // off: 0..0x17FF within bank 0 tile data
{
    unsigned row = off & ~1u;
    ((u32 *)tile_mem[0])[row >> 1] = spread[vram[row]] | spread[vram[row + 1]] << 1;
}

static u16 ppu_map_entry(uint8_t idx)
{
    unsigned t = (LCDC & 0x10) ? idx : (idx < 128 ? 256 + idx : idx);  // 8000 vs 8800 addressing
    return t;
}

static void ppu_map_write(unsigned off)   // off: 0..0x7FF within 9800-9FFF
{
    se_mem[off < 0x400 ? SB_MAP0 : SB_MAP1][off & 0x3FF] = ppu_map_entry(vram[0x1800 + off]);
}

static void ppu_bg_palette(void)
{
    if (cgb_mode) {
        for (int i = 0; i < 4; i++) pal_bg_mem[i] = bg_pal[i];  // ponytail: palette 0 only until CGB attributes land
    } else {
        for (int i = 0; i < 4; i++) pal_bg_mem[i] = dmg_shade(io[0x47] >> (2 * i));
    }
}

static void ppu_lcdc(uint8_t old)
{
    if ((old ^ LCDC) & 0x10)
        for (unsigned off = 0; off < 0x800; off++) ppu_map_write(off);
    REG_BG0CNT = BG_CBB(0) | BG_SBB((LCDC & 0x08) ? SB_MAP1 : SB_MAP0) | BG_4BPP | BG_REG_32x32;
    REG_DISPCNT = (LCDC & 0x80) ? (DCNT_MODE0 | DCNT_WIN0 | ((LCDC & 0x01) ? DCNT_BG0 : 0)) : DCNT_BLANK;
}

static void ppu_scroll(void)
{
    REG_BG0HOFS = io[0x43] - 40;
    REG_BG0VOFS = io[0x42] - 8;
}

static void ppu_init(void)
{
    for (int b = 0; b < 256; b++) {
        u32 v = 0;
        for (int i = 0; i < 8; i++) v |= ((b >> (7 - i)) & 1) << (4 * i);
        spread[b] = v;
    }
    REG_WIN0H = 40 << 8 | 200;
    REG_WIN0V = 8 << 8 | 152;
    REG_WININ = WIN_BG0;
    REG_WINOUT = 0;
    ppu_lcdc(0);
    ppu_scroll();
}

static uint8_t ppu_ly(void)               // GB LY from GBA VCOUNT (160+68 lines → 144+10)
{
    unsigned v = REG_VCOUNT;
    return v < 160 ? v * 144 / 160 : 144 + (v - 160) * 10 / 68;
}

static void oam_dma(uint8_t src)
{
    for (int i = 0; i < 0xA0; i++) oam[i] = rd8(src << 8 | i);
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
        if (a < 0x2000) ram_enable = (v & 0x0F) == 0x0A;
        else if (a < 0x3000) set_rom_bank((gb.rom_bank & 0x100) | v);
        else if (a < 0x4000) set_rom_bank((gb.rom_bank & 0xFF) | (v & 1) << 8);
        // ponytail: 0x4000-0x5FFF RAM bank ignored — single 8 KiB bank (DQ1&2); add banking for 32 KiB carts
        return;
    }
    // ponytail: MBC1/2/3 not yet — trap instead of silently mis-banking
    gb_trap(0, a, TRAP_NO_ENTRY);
}

// ---- IO -------------------------------------------------------------------------------

uint8_t gb_read_io(uint8_t r)
{
    switch (r) {
    case 0x00: {                          // P1: GBA KEYINPUT has the same active-low layout
        uint8_t sel = io[0x00], keys = 0x0F;
        u16 k = REG_KEYINPUT;
        if (!(sel & 0x10)) keys &= (k >> 4) & 0x0F;  // Right Left Up Down
        if (!(sel & 0x20)) keys &= k & 0x0F;         // A B Select Start
        return 0xC0 | (sel & 0x30) | keys;
    }
    case 0x04: return REG_TM0CNT_L;       // DIV: TM0 at 16384 Hz
    case 0x0F: return 0xE0 | IF;
    case 0x41: {
        uint8_t ly = ppu_ly(), mode = ly >= 144 ? 1 : (REG_DISPSTAT & DSTAT_IN_HBL) ? 0 : 3;
        return 0x80 | (io[0x41] & 0x78) | (ly == io[0x45]) << 2 | mode;
    }
    case 0x44: return ppu_ly();
    case 0x69: return bg_pal[(io[0x68] & 0x3F) >> 1] >> ((io[0x68] & 1) * 8);
    default: return io[r];
    }
}

void gb_write_io(uint8_t r, uint8_t v)
{
    uint8_t old = io[r];
    io[r] = v;
    switch (r) {
    case 0x04: REG_TM0CNT_H = 0; REG_TM0CNT_H = TM_ENABLE | TM_FREQ_1024; break;
    case 0x0F: IF = v & 0x1F; update_irq(); break;
    case 0x40: ppu_lcdc(old); break;
    case 0x42: case 0x43: ppu_scroll(); break;
    case 0x46: oam_dma(v); break;
    case 0x47: ppu_bg_palette(); break;
    case 0x69: {                          // BCPD with auto-increment
        unsigned i = io[0x68] & 0x3F;
        uint16_t *p = &bg_pal[i >> 1];
        *p = (i & 1) ? (*p & 0x00FF) | v << 8 : (*p & 0xFF00) | v;
        if (io[0x68] & 0x80) io[0x68] = 0x80 | ((i + 1) & 0x3F);
        ppu_bg_palette();
        break;
    }
    case 0x70:                            // SVBK
        if (cgb_mode) gb_wramx = (uint8_t *)((uintptr_t)gb_wram + ((v & 7) ? (v & 7) : 1) * 0x1000 - 0xD000);
        break;
    }
}

uint8_t gb_read_slow(uint16_t a)
{
    if (a < 0xA000) return vram[(io[0x4F] & 1) * 0x2000 + a - 0x8000];
    if (a < 0xC000) return ram_enable ? ((volatile uint8_t *)MEM_SRAM)[a - 0xA000] : 0xFF;
    if (a < 0xFE00) return rd8(a - 0x2000);  // echo RAM
    if (a < 0xFEA0) return oam[a - 0xFE00];
    if (a < 0xFF00) return 0xFF;
    if (a < 0xFF80) return gb_read_io(a & 0x7F);
    return IE;
}

void gb_write_slow(uint16_t a, uint8_t v)
{
    if (a < 0x8000) { mbc_write(a, v); return; }
    if (a < 0xA000) {
        unsigned off = a - 0x8000;
        if (io[0x4F] & 1) { vram[0x2000 + off] = v; return; }  // ponytail: CGB bank 1 (attrs, extra tiles) not drawn yet
        vram[off] = v;
        if (off < 0x1800) ppu_tile_row(off);
        else ppu_map_write(off - 0x1800);
        return;
    }
    if (a < 0xC000) { if (ram_enable) ((volatile uint8_t *)MEM_SRAM)[a - 0xA000] = v; return; }
    if (a < 0xFE00) { wr8(a - 0x2000, v); return; }
    if (a < 0xFEA0) { oam[a - 0xFE00] = v; return; }
    if (a < 0xFF00) return;
    if (a < 0xFF80) { gb_write_io(a & 0x7F, v); return; }
    IE = v;
    update_irq();
}

// ---- CPU control ------------------------------------------------------------------------

void gb_ei(void) { gb.ime = 1; update_irq(); }  // ponytail: EI takes effect immediately, not after one instruction
void gb_di(void) { gb.ime = 0; gb.irq = 0; }

int gb_service_irq(uint16_t resume)
{
    uint8_t pending = IE & IF & 0x1F;
    if (!pending) { update_irq(); return 0; }
    unsigned n = __builtin_ctz(pending);
    IF &= ~(1 << n);
    gb.ime = 0;
    gb.irq = 0;
    if (!gb_vectors[n]) gb_trap(0, 0x40 + 8 * n, TRAP_NO_ENTRY);
    uint16_t fs = gb.frame_sp;
    push16(resume);
    gb.frame_sp = gb.sp;
    gb_vectors[n](0x40 + 8 * n);
    gb.frame_sp = fs;
    return gb.ret != resume;
}

int gb_halt(uint16_t resume)
{
    while (!(IE & *(volatile uint8_t *)&IF & 0x1F)) Halt();  // sleep until a GBA IRQ raises a GB IF bit
    return gb.ime ? gb_service_irq(resume) : 0;
}

void gb_stop(uint16_t resume)
{
    uint8_t *key1 = &io[0x4D];
    if (cgb_mode && (*key1 & 1)) { *key1 = (*key1 ^ 0x80) & 0x80; return; }  // speed switch: no-op here
    gb_trap(gb.rom_bank, resume - 2, TRAP_STOP);
}

void gb_dispatch(uint16_t addr)
{
    uint32_t bank = addr < 0x4000 ? 0 : gb.rom_bank;
    if (addr >= 0x8000) {
        const uint8_t *p = &gb_hram[addr - 0xFF80];
        // OAM DMA routine copied to HRAM: LD A,n / LDH (46),A / ... / RET
        if (addr >= 0xFF80 && p[0] == 0x3E && p[2] == 0xE0 && p[3] == 0x46) {
            oam_dma(p[1]);
            gb.ret = pop16();
            return;
        }
        gb_trap(0xFFFF, addr, TRAP_NO_ENTRY);  // RAM code: needs a native hook
    }
    uint32_t key = bank << 16 | addr, lo = 0, hi = gb_entry_count;
    while (lo < hi) {
        uint32_t mid = (lo + hi) / 2;
        if (gb_entries[mid].key < key) lo = mid + 1; else hi = mid;
    }
    if (lo == gb_entry_count || gb_entries[lo].key != key) gb_trap(bank, addr, TRAP_NO_ENTRY);
    gb_entries[lo].fn(addr);
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
    tte_init_se_default(0, BG_CBB(2) | BG_SBB(31));
    tte_printf("#{P:8,8}gbforge trap\n#{X:8}%s\n#{X:8}at %02X:%04X", names[reason < 6 ? reason : 0], bank, addr);
    for (;;) Halt();
}

// ---- boot -----------------------------------------------------------------------------

static void vblank_isr(void)
{
    if (LCDC & 0x80) { IF |= 0x01; update_irq(); }
}

int main(void)
{
    REG_WAITCNT = 0x4317;                 // 3/1 ROM waitstates + prefetch
    cgb_mode = gb_rom[0x143] & 0x80;
    set_rom_bank(1);
    gb_wramx = (uint8_t *)((uintptr_t)gb_wram + 0x1000 - 0xD000);
    if (cgb_mode) {                       // post-boot-ROM register state (Pan Docs)
        gb.a = 0x11; gb.zf = 1;
        gb.b = 0x00; gb.c = 0x00; gb.d = 0xFF; gb.e = 0x56; gb.h = 0x00; gb.l = 0x0D;
    } else {
        gb.a = 0x01; gb.zf = 1; gb.hf = 1; gb.cf = 1;
        gb.b = 0x00; gb.c = 0x13; gb.d = 0x00; gb.e = 0xD8; gb.h = 0x01; gb.l = 0x4D;
    }
    gb.sp = 0xFFFE;
    gb.frame_sp = 0xFFFF;                 // top level: no C call frame to protect
    io[0x00] = 0x30;
    io[0x40] = 0x91;
    io[0x47] = 0xFC;
    REG_TM0CNT_H = TM_ENABLE | TM_FREQ_1024;
    ppu_init();
    ppu_bg_palette();
    irq_init(NULL);
    irq_add(II_VBLANK, vblank_isr);
    gb_entry();
    gb_trap(0, 0, TRAP_EXIT);
}
