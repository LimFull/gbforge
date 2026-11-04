// gb.h — ABI between generated code (gbforge-core codegen) and the GBA runtime.
//
// Timing model: generated code adds each instruction's GB T-cycles to gb.cyc; video/timer
// events run on that GB time at safepoints (loop back-edges, HALT, EI), paced to real time.
#pragma once
#include <stdint.h>

typedef void (*GbFn)(uint16_t at);

// Register pairs are little-endian unions so BC/DE/HL are single halfword loads/stores.
typedef struct {
    uint8_t a;
    uint8_t zf, nf, hf, cf;   // flags, each 0 or 1 (packed only for PUSH/POP AF)
    uint8_t ime;
    uint8_t irq;              // runtime-maintained: ime && (IE & IF & 0x1F)
    uint8_t pad_;
    union { struct { uint8_t c, b; }; uint16_t bc; };
    union { struct { uint8_t e, d; }; uint16_t de; };
    union { struct { uint8_t l, h; }; uint16_t hl; };
    uint16_t sp;
    uint16_t ret;             // address popped by the last RET/RETI
    uint16_t rom_bank;        // MBC bank mapped at 0x4000
    uint16_t frame_sp;        // GB SP just after the innermost live C call pushed its return address
    uint16_t jat;             // pending tail jump: target address ...
    GbFn jfn;                 // ... and the compiled function that holds it (0 = none)
    uint32_t cyc;             // GB time in CPU T-cycles (see gb.c "GB time")
} GbCpu;

typedef struct { uint32_t key; GbFn fn; } GbEntry;  // key = bank << 16 | addr

extern GbCpu gb;
extern const uint8_t gb_rom[];
extern const uint8_t *gb_romx;    // indexed with 0x4000..0x7FFF
extern uint8_t gb_wram[0x2000];   // WRAM banks 0-1 (C000-DFFF); CGB banks 2-7 live in the runtime
extern uint8_t *gb_wramx;         // indexed with 0xD000..0xDFFF
extern uint8_t gb_hram[0x80];     // FF80..FFFF, [0x7F] = IE
extern const uint8_t *gb_rpage[256];  // per 256-byte page: direct read pointer, 0 = slow path
extern uint8_t *gb_wpage[256];        // per page: direct write pointer, 0 = slow (MBC, VRAM, OAM, IO)

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
// Control-flow protocol (no C recursion for jumps — GCC emits no Thumb-1 tail calls):
// - JP to another function sets gb.jfn/gb.jat and returns; the nearest CALL site (or the top
//   level) runs it in a loop (gb_trampoline). If that CALL's GB return address was already
//   popped (RST jump tables, handlers that JP back to a main loop) its frame is dead and the
//   jump keeps propagating outward instead.
// - RET stores the popped address in gb.ret and returns. A CALL site continues only if gb.ret
//   is its own return site; otherwise it returns too, and the top level dispatches gb.ret.
// These return nonzero when control did not come back to `resume` (caller must return).
int gb_service_irq(uint16_t resume);
int gb_poll(uint16_t resume);     // run due GB-time events, then service a pending interrupt
int gb_halt(uint16_t resume);
extern uint32_t gb_next_event;
void gb_stop(uint16_t resume);
uint8_t gb_read_stat(void);
uint8_t gb_read_ly(void);
extern uint8_t io[0x80];         // FF00-FF7F stored values
GbFn gb_lookup(uint16_t addr);    // compiled function holding addr in the current bank; traps if none
int gb_trampoline(void);
__attribute__((noreturn)) void gb_trap(uint16_t bank, uint16_t addr, uint8_t reason);

enum { TRAP_UNDEFINED = 1, TRAP_NO_ENTRY = 2, TRAP_RET_MISMATCH = 3, TRAP_STOP = 4, TRAP_EXIT = 5 };

#define GB_CALL(RET_, CALL_) do { uint16_t fs_ = gb.frame_sp; push16(RET_); gb.frame_sp = gb.sp; \
    CALL_; int dead_ = gb.jfn ? gb_trampoline() : 0; gb.frame_sp = fs_; \
    if (dead_ || gb.ret != (RET_)) return; } while (0)

#define GB_JUMP(TGT_, FN_) do { gb.jat = (TGT_); gb.jfn = (FN_); return; } while (0)

#define GB_SAFEPOINT(resume) do { if ((gb.irq || (int32_t)(gb.cyc - gb_next_event) >= 0) && gb_poll(resume)) return; } while (0)

// ---- memory -------------------------------------------------------------------------

static inline __attribute__((always_inline)) void gb_di(void) { gb.ime = 0; gb.irq = 0; }
// ponytail: EI takes effect immediately (codegen adds a safepoint right after it)
static inline __attribute__((always_inline)) void gb_ei(void) { gb.ime = 1; gb.irq = (gb_hram[0x7F] & io[0x0F] & 0x1F) != 0; }

// Constant addresses fold to a direct access at compile time; others go through the page table.
static inline __attribute__((always_inline)) uint8_t rd8(uint16_t a)
{
    if (!__builtin_constant_p(a)) {
        if (a < 0x8000) return a < 0x4000 ? gb_rom[a] : gb_romx[a];  // bank switches touch one pointer
        const uint8_t *p = gb_rpage[a >> 8];
        return p ? p[a & 0xFF] : gb_read_slow(a);
    }
    if (a < 0x4000) return gb_rom[a];
    if (a < 0x8000) return gb_romx[a];
    if ((uint16_t)(a - 0xC000) < 0x1000) return gb_wram[a - 0xC000];
    if ((uint16_t)(a - 0xD000) < 0x1000) return gb_wramx[a];
    if (a >= 0xFF80 && a != 0xFFFF) return gb_hram[a - 0xFF80];
    if (a == 0xFF41) return gb_read_stat();  // wait loops poll these
    if (a == 0xFF44) return gb_read_ly();
    return gb_read_slow(a);
}

static inline __attribute__((always_inline)) void wr8(uint16_t a, uint8_t v)
{
    if (!__builtin_constant_p(a)) {
        uint8_t *p = gb_wpage[a >> 8];
        if (p) p[a & 0xFF] = v; else gb_write_slow(a, v);
        return;
    }
    if ((uint16_t)(a - 0xC000) < 0x1000) { gb_wram[a - 0xC000] = v; return; }
    if ((uint16_t)(a - 0xD000) < 0x1000) { gb_wramx[a] = v; return; }
    if (a >= 0xFF80 && a != 0xFFFF) { gb_hram[a - 0xFF80] = v; return; }
    gb_write_slow(a, v);
}

uint16_t gb_pop16_slow(uint16_t sp);
void gb_push16_slow(uint16_t sp, uint16_t v);

// Fast path: both stack bytes in one directly mapped page (the usual WRAM stack).
static inline __attribute__((always_inline)) uint16_t pop16(void)
{
    uint16_t sp = gb.sp;
    const uint8_t *p = gb_rpage[sp >> 8];
    gb.sp = sp + 2;
    if (p && (sp & 0xFF) != 0xFF) return p[sp & 0xFF] | p[(sp & 0xFF) + 1] << 8;
    return gb_pop16_slow(sp);
}

static inline __attribute__((always_inline)) void push16(uint16_t v)
{
    uint16_t sp = gb.sp - 2;
    uint8_t *p = gb_wpage[sp >> 8];
    gb.sp = sp;
    if (p && (sp & 0xFF) != 0xFF) { p[sp & 0xFF] = v; p[(sp & 0xFF) + 1] = v >> 8; return; }
    gb_push16_slow(sp, v);
}

#define BC() (gb.bc)
#define DE() (gb.de)
#define HL() (gb.hl)
#define SET_BC(v) (gb.bc = (uint16_t)(v))
#define SET_DE(v) (gb.de = (uint16_t)(v))
#define SET_HL(v) (gb.hl = (uint16_t)(v))

static inline __attribute__((always_inline)) uint8_t pack_f(void) { return gb.zf << 7 | gb.nf << 6 | gb.hf << 5 | gb.cf << 4; }
static inline __attribute__((always_inline)) void unpack_f(uint8_t f) { gb.zf = f >> 7 & 1; gb.nf = f >> 6 & 1; gb.hf = f >> 5 & 1; gb.cf = f >> 4 & 1; }

// ---- ALU (Pan Docs flag semantics) --------------------------------------------------
//
// `m` is a compile-time mask of the flags whose values are read later (codegen liveness,
// per block, all flags assumed live at block ends and control flow). Dead flags are not stored.

#define INL static inline __attribute__((always_inline))
#define FZ 1
#define FN 2
#define FH 4
#define FC 8
#define SETF(f, bit, v) do { if (m & (bit)) gb.f = (v); } while (0)

INL void alu_add(uint8_t v, uint8_t carry, unsigned m)
{
    unsigned r = gb.a + v + carry;
    SETF(hf, FH, (gb.a & 0xF) + (v & 0xF) + carry > 0xF);
    SETF(cf, FC, r > 0xFF);
    gb.a = r;
    SETF(zf, FZ, gb.a == 0);
    SETF(nf, FN, 0);
}

INL uint8_t alu_sub(uint8_t v, uint8_t carry, unsigned m)  // returns result; SUB/SBC store it, CP doesn't
{
    int r = gb.a - v - carry;
    SETF(hf, FH, (gb.a & 0xF) - (v & 0xF) - carry < 0);
    SETF(cf, FC, r < 0);
    SETF(zf, FZ, (uint8_t)r == 0);
    SETF(nf, FN, 1);
    return r;
}

INL void alu_and(uint8_t v, unsigned m) { gb.a &= v; SETF(zf, FZ, gb.a == 0); SETF(nf, FN, 0); SETF(hf, FH, 1); SETF(cf, FC, 0); }
INL void alu_or(uint8_t v, unsigned m)  { gb.a |= v; SETF(zf, FZ, gb.a == 0); SETF(nf, FN, 0); SETF(hf, FH, 0); SETF(cf, FC, 0); }
INL void alu_xor(uint8_t v, unsigned m) { gb.a ^= v; SETF(zf, FZ, gb.a == 0); SETF(nf, FN, 0); SETF(hf, FH, 0); SETF(cf, FC, 0); }

INL uint8_t alu_inc(uint8_t x, unsigned m) { x++; SETF(zf, FZ, x == 0); SETF(nf, FN, 0); SETF(hf, FH, (x & 0xF) == 0); return x; }
INL uint8_t alu_dec(uint8_t x, unsigned m) { x--; SETF(zf, FZ, x == 0); SETF(nf, FN, 1); SETF(hf, FH, (x & 0xF) == 0xF); return x; }

INL void alu_add_hl(uint16_t v, unsigned m)
{
    uint16_t hl = HL();
    SETF(hf, FH, (hl & 0xFFF) + (v & 0xFFF) > 0xFFF);
    SETF(cf, FC, (unsigned)hl + v > 0xFFFF);
    SETF(nf, FN, 0);
    SET_HL(hl + v);
}

INL uint16_t alu_sp_offset(int8_t n, unsigned m)  // ADD SP,n / LD HL,SP+n
{
    SETF(hf, FH, (gb.sp & 0xF) + ((uint8_t)n & 0xF) > 0xF);
    SETF(cf, FC, (gb.sp & 0xFF) + (uint8_t)n > 0xFF);
    SETF(zf, FZ, 0);
    SETF(nf, FN, 0);
    return gb.sp + n;
}

INL void alu_daa(unsigned m)
{
    uint8_t c = gb.cf;
    if (!gb.nf) {
        if (c || gb.a > 0x99) { gb.a += 0x60; c = 1; }
        if (gb.hf || (gb.a & 0x0F) > 0x09) gb.a += 0x06;
    } else {
        if (c) gb.a -= 0x60;
        if (gb.hf) gb.a -= 0x06;
    }
    SETF(cf, FC, c);
    SETF(zf, FZ, gb.a == 0);
    SETF(hf, FH, 0);
}

// CB-prefixed rotates/shifts: result returned, Z from the result
INL uint8_t sh_flags(uint8_t r, uint8_t c, unsigned m) { SETF(zf, FZ, r == 0); SETF(nf, FN, 0); SETF(hf, FH, 0); SETF(cf, FC, c); return r; }
INL uint8_t cb_rlc(uint8_t x, unsigned m)  { return sh_flags(x << 1 | x >> 7, x >> 7, m); }
INL uint8_t cb_rrc(uint8_t x, unsigned m)  { return sh_flags(x >> 1 | x << 7, x & 1, m); }
INL uint8_t cb_rl(uint8_t x, unsigned m)   { return sh_flags(x << 1 | gb.cf, x >> 7, m); }
INL uint8_t cb_rr(uint8_t x, unsigned m)   { return sh_flags(x >> 1 | gb.cf << 7, x & 1, m); }
INL uint8_t cb_sla(uint8_t x, unsigned m)  { return sh_flags(x << 1, x >> 7, m); }
INL uint8_t cb_sra(uint8_t x, unsigned m)  { return sh_flags(x >> 1 | (x & 0x80), x & 1, m); }
INL uint8_t cb_swap(uint8_t x, unsigned m) { return sh_flags(x << 4 | x >> 4, 0, m); }
INL uint8_t cb_srl(uint8_t x, unsigned m)  { return sh_flags(x >> 1, x & 1, m); }
INL void cb_bit(uint8_t n, uint8_t x, unsigned m) { SETF(zf, FZ, !(x >> n & 1)); SETF(nf, FN, 0); SETF(hf, FH, 1); }

// RLCA/RRCA/RLA/RRA: the CB forms with Z always cleared (call with m & ~FZ, then a_rot)
INL void a_rot(uint8_t r, unsigned m) { gb.a = r; SETF(zf, FZ, 0); }
