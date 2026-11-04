// gb.h — ABI between generated code (gbforge-core codegen) and the GBA runtime.
//
// Timing model: hardware-clocked, not cycle-counted. GB code runs as native Thumb and
// re-synchronises with GBA video at HALT and interrupt safepoints (loop back-edges, HALT, EI).
// ponytail: no per-instruction cycles — mid-frame raster effects (STAT/LYC splits) and
// cycle-timed loops are approximate; add per-block cycle budgets if a game needs them.
#pragma once
#include <stdint.h>

typedef struct {
    uint8_t a, b, c, d, e, h, l;
    uint8_t zf, nf, hf, cf;   // flags, each 0 or 1 (packed only for PUSH/POP AF)
    uint8_t ime;
    uint8_t irq;              // runtime-maintained: ime && (IE & IF & 0x1F)
    uint16_t sp;
    uint16_t ret;             // address popped by the last RET/RETI
    uint16_t rom_bank;        // MBC bank mapped at 0x4000
    uint16_t frame_sp;        // GB SP just after the innermost live C call pushed its return address
} GbCpu;

typedef void (*GbFn)(uint16_t at);
typedef struct { uint32_t key; GbFn fn; } GbEntry;  // key = bank << 16 | addr

extern GbCpu gb;
extern const uint8_t gb_rom[];
extern const uint8_t *gb_romx;    // indexed with 0x4000..0x7FFF
extern uint8_t gb_wram[0x8000];   // CGB: 8 x 4 KiB banks
extern uint8_t *gb_wramx;         // indexed with 0xD000..0xDFFF
extern uint8_t gb_hram[0x80];     // FF80..FFFF, [0x7F] = IE

// generated
extern const GbEntry gb_entries[];
extern const uint32_t gb_entry_count;
extern const GbFn gb_vectors[5];  // 0x40 VBlank, 0x48 STAT, 0x50 Timer, 0x58 Serial, 0x60 Joypad
void gb_entry(void);

// runtime
uint8_t gb_read_slow(uint16_t a);
void gb_write_slow(uint16_t a, uint8_t v);
uint8_t gb_read_io(uint8_t reg);
void gb_write_io(uint8_t reg, uint8_t v);
// Return-address protocol: RET stores the popped address in gb.ret and returns from the C
// function. A caller continues only if gb.ret is its own return site; otherwise it returns
// too (the GB code skipped frames), and the top level dispatches gb.ret.
// These return nonzero when control did not come back to `resume` (caller must return).
int gb_service_irq(uint16_t resume);
int gb_halt(uint16_t resume);
void gb_stop(uint16_t resume);
void gb_ei(void);
void gb_di(void);
void gb_dispatch(uint16_t addr);  // call the code at addr in the current bank (JP HL, unknown bank)
__attribute__((noreturn)) void gb_trap(uint16_t bank, uint16_t addr, uint8_t reason);

enum { TRAP_UNDEFINED = 1, TRAP_NO_ENTRY = 2, TRAP_RET_MISMATCH = 3, TRAP_STOP = 4, TRAP_EXIT = 5 };

// gb.irq is raised by the GBA VBlank ISR, so read it volatile (busy-wait loops must see it).
// CALL: record the frame, call, continue only if control came back to `ret`.
#define GB_CALL(RET_, CALL_) do { uint16_t fs_ = gb.frame_sp; push16(RET_); gb.frame_sp = gb.sp; \
    CALL_; gb.frame_sp = fs_; if (gb.ret != (RET_)) return; } while (0)

// Cross-function JP. If the innermost C call's GB return address was already popped (RST jump
// tables, handlers that JP back to a main loop), that C frame is dead: unwind to the top level,
// which dispatches `target`, instead of nesting deeper (unbounded C stack growth otherwise).
#define GB_JUMP(TGT_, CALL_) do { if (gb.sp > gb.frame_sp) { gb.ret = (TGT_); return; } \
    CALL_; return; } while (0)

#define GB_SAFEPOINT(resume) do { if (*(volatile uint8_t *)&gb.irq && gb_service_irq(resume)) return; } while (0)

// ---- memory -------------------------------------------------------------------------

static inline uint8_t rd8(uint16_t a)
{
    if (a < 0x4000) return gb_rom[a];
    if (a < 0x8000) return gb_romx[a];
    if ((uint16_t)(a - 0xC000) < 0x1000) return gb_wram[a - 0xC000];
    if ((uint16_t)(a - 0xD000) < 0x1000) return gb_wramx[a];
    if (a >= 0xFF80 && a != 0xFFFF) return gb_hram[a - 0xFF80];
    return gb_read_slow(a);
}

static inline void wr8(uint16_t a, uint8_t v)
{
    if ((uint16_t)(a - 0xC000) < 0x1000) { gb_wram[a - 0xC000] = v; return; }
    if ((uint16_t)(a - 0xD000) < 0x1000) { gb_wramx[a] = v; return; }
    if (a >= 0xFF80 && a != 0xFFFF) { gb_hram[a - 0xFF80] = v; return; }
    gb_write_slow(a, v);
}

static inline void push16(uint16_t v)
{
    wr8(--gb.sp, v >> 8);
    wr8(--gb.sp, v);
}

static inline uint16_t pop16(void)
{
    uint16_t v = rd8(gb.sp);
    v |= rd8(gb.sp + 1) << 8;
    gb.sp += 2;
    return v;
}

#define BC() ((uint16_t)(gb.b << 8 | gb.c))
#define DE() ((uint16_t)(gb.d << 8 | gb.e))
#define HL() ((uint16_t)(gb.h << 8 | gb.l))
#define SET_BC(v) do { uint16_t t_ = (v); gb.b = t_ >> 8; gb.c = t_; } while (0)
#define SET_DE(v) do { uint16_t t_ = (v); gb.d = t_ >> 8; gb.e = t_; } while (0)
#define SET_HL(v) do { uint16_t t_ = (v); gb.h = t_ >> 8; gb.l = t_; } while (0)

static inline uint8_t pack_f(void) { return gb.zf << 7 | gb.nf << 6 | gb.hf << 5 | gb.cf << 4; }
static inline void unpack_f(uint8_t f) { gb.zf = f >> 7 & 1; gb.nf = f >> 6 & 1; gb.hf = f >> 5 & 1; gb.cf = f >> 4 & 1; }

// ---- ALU (Pan Docs flag semantics) --------------------------------------------------

static inline void alu_add(uint8_t v, uint8_t carry)
{
    unsigned r = gb.a + v + carry;
    gb.hf = (gb.a & 0xF) + (v & 0xF) + carry > 0xF;
    gb.cf = r > 0xFF;
    gb.a = r;
    gb.zf = gb.a == 0;
    gb.nf = 0;
}

static inline uint8_t alu_sub(uint8_t v, uint8_t carry)  // returns result; caller stores for SUB/SBC
{
    int r = gb.a - v - carry;
    gb.hf = (gb.a & 0xF) - (v & 0xF) - carry < 0;
    gb.cf = r < 0;
    gb.zf = (uint8_t)r == 0;
    gb.nf = 1;
    return r;
}

static inline void alu_and(uint8_t v) { gb.a &= v; gb.zf = gb.a == 0; gb.nf = 0; gb.hf = 1; gb.cf = 0; }
static inline void alu_or(uint8_t v)  { gb.a |= v; gb.zf = gb.a == 0; gb.nf = gb.hf = gb.cf = 0; }
static inline void alu_xor(uint8_t v) { gb.a ^= v; gb.zf = gb.a == 0; gb.nf = gb.hf = gb.cf = 0; }

static inline uint8_t alu_inc(uint8_t x) { x++; gb.zf = x == 0; gb.nf = 0; gb.hf = (x & 0xF) == 0; return x; }
static inline uint8_t alu_dec(uint8_t x) { x--; gb.zf = x == 0; gb.nf = 1; gb.hf = (x & 0xF) == 0xF; return x; }

static inline void alu_add_hl(uint16_t v)
{
    uint16_t hl = HL();
    gb.hf = (hl & 0xFFF) + (v & 0xFFF) > 0xFFF;
    gb.cf = (unsigned)hl + v > 0xFFFF;
    gb.nf = 0;
    SET_HL(hl + v);
}

static inline uint16_t alu_sp_offset(int8_t n)  // ADD SP,n / LD HL,SP+n
{
    gb.hf = (gb.sp & 0xF) + ((uint8_t)n & 0xF) > 0xF;
    gb.cf = (gb.sp & 0xFF) + (uint8_t)n > 0xFF;
    gb.zf = gb.nf = 0;
    return gb.sp + n;
}

static inline void alu_daa(void)
{
    if (!gb.nf) {
        if (gb.cf || gb.a > 0x99) { gb.a += 0x60; gb.cf = 1; }
        if (gb.hf || (gb.a & 0x0F) > 0x09) gb.a += 0x06;
    } else {
        if (gb.cf) gb.a -= 0x60;
        if (gb.hf) gb.a -= 0x06;
    }
    gb.zf = gb.a == 0;
    gb.hf = 0;
}

// CB-prefixed rotates/shifts: result returned, Z set from result
static inline uint8_t sh_flags(uint8_t r, uint8_t c) { gb.zf = r == 0; gb.nf = gb.hf = 0; gb.cf = c; return r; }
static inline uint8_t cb_rlc(uint8_t x)  { return sh_flags(x << 1 | x >> 7, x >> 7); }
static inline uint8_t cb_rrc(uint8_t x)  { return sh_flags(x >> 1 | x << 7, x & 1); }
static inline uint8_t cb_rl(uint8_t x)   { return sh_flags(x << 1 | gb.cf, x >> 7); }
static inline uint8_t cb_rr(uint8_t x)   { return sh_flags(x >> 1 | gb.cf << 7, x & 1); }
static inline uint8_t cb_sla(uint8_t x)  { return sh_flags(x << 1, x >> 7); }
static inline uint8_t cb_sra(uint8_t x)  { return sh_flags(x >> 1 | (x & 0x80), x & 1); }
static inline uint8_t cb_swap(uint8_t x) { return sh_flags(x << 4 | x >> 4, 0); }
static inline uint8_t cb_srl(uint8_t x)  { return sh_flags(x >> 1, x & 1); }
static inline void cb_bit(uint8_t n, uint8_t x) { gb.zf = !(x >> n & 1); gb.nf = 0; gb.hf = 1; }

// RLCA/RRCA/RLA/RRA: like the CB forms but Z is always cleared
static inline void a_rot(uint8_t r) { gb.a = r; gb.zf = 0; }
