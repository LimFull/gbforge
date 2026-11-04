// SM83 → C for the GBA runtime ABI in runtime-gba/gb.h.
//
// One C function per analyzed GB function: void f_BB_AAAA(uint16_t at). `at` selects the entry
// (primary or a secondary entry such as a call return site or a jump target inside the function).
// Intra-function branches are gotos; cross-function branches are tail calls; CALL pushes the
// GB return address, calls, and continues only if gb.ret matches (see gb.h).
// No interpreter: unknown targets go through gb_dispatch, which traps if no compiled entry exists.
#include "core.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <sstream>

using namespace gbrecomp;
namespace fs = std::filesystem;

namespace {

uint32_t key(BankId b, uint16_t a) { return (uint32_t(b) << 16) | a; }
BankId kbank(uint32_t k) { return k >> 16; }
uint16_t kaddr(uint32_t k) { return k & 0xFFFF; }

std::string hex4(unsigned v) { char b[8]; std::snprintf(b, sizeof b, "0x%04X", v & 0xFFFF); return b; }
std::string fn_name(uint32_t k) { char b[16]; std::snprintf(b, sizeof b, "f_%02X_%04X", kbank(k), kaddr(k)); return b; }
std::string label(uint16_t a) { char b[8]; std::snprintf(b, sizeof b, "L_%04X", a); return b; }

const char* r8(Reg8 r)
{
    switch (r) {
    case Reg8::B: return "gb.b"; case Reg8::C: return "gb.c"; case Reg8::D: return "gb.d";
    case Reg8::E: return "gb.e"; case Reg8::H: return "gb.h"; case Reg8::L: return "gb.l";
    case Reg8::A: return "gb.a"; default: return "rd8(HL())";
    }
}

std::string set8(Reg8 r, const std::string& v)
{
    if (r == Reg8::HL_IND) return "wr8(HL(), " + v + ");";
    return std::string(r8(r)) + " = " + v + ";";
}

std::string get16(Reg16 r)
{
    switch (r) {
    case Reg16::BC: return "BC()"; case Reg16::DE: return "DE()"; case Reg16::HL: return "HL()";
    case Reg16::SP: return "gb.sp"; default: return "(uint16_t)(gb.a << 8 | pack_f())";
    }
}

std::string set16(Reg16 r, const std::string& v)
{
    switch (r) {
    case Reg16::BC: return "SET_BC(" + v + ");"; case Reg16::DE: return "SET_DE(" + v + ");";
    case Reg16::HL: return "SET_HL(" + v + ");"; case Reg16::SP: return "gb.sp = " + v + ";";
    default: return "{ uint16_t t_ = " + v + "; gb.a = t_ >> 8; unpack_f(t_); }";
    }
}

const char* cond(Condition c)
{
    switch (c) {
    case Condition::NZ: return "!gb.zf"; case Condition::Z: return "gb.zf";
    case Condition::NC: return "!gb.cf"; case Condition::C: return "gb.cf"; default: return "1";
    }
}

// Bank a static branch target lands in, or UNKNOWN_BANK when only runtime MBC state knows.
BankId target_bank(const Instruction& in, uint16_t t)
{
    if (t < 0x4000) return 0;
    if (t >= 0x8000) return UNKNOWN_BANK;
    if (in.bank != 0) return in.bank;  // banked code runs with its own bank mapped
    return in.resolved_target_bank;    // bank-0 code: analyzer's guess, guarded at runtime
}

struct Gen {
    const AnalysisResult& r;
    std::map<uint32_t, std::set<uint32_t>> fn_blocks;  // function → its block keys
    std::map<uint32_t, std::set<uint32_t>> fn_insns;   // function → every instruction key in it
    std::map<uint32_t, uint32_t> owner;                 // entry key → function key
    std::map<uint32_t, std::set<uint32_t>> entries;     // function → secondary entry keys
    std::set<uint32_t> needed;                          // keys that must be enterable
    std::map<std::string, size_t> stats;
    std::vector<std::string> unresolved;                // "BB:AAAA reason"

    uint32_t cur = 0;                                   // function being emitted
    std::set<uint32_t> labels;                          // labels referenced in it

    const ROM& rom;

    Gen(const AnalysisResult& res, const ROM& rm) : r(res), rom(rm) {}

    // An RST vector filled with 0xFF/0x00 is padding: reaching it means data was decoded as code.
    bool rst_padding(uint8_t v) const
    {
        for (int i = 0; i < 8; i++)
            if (rom.read(v + i) != 0xFF) return false;
        return true;
    }

    // Every instruction's static targets and return sites must be enterable.
    void collect()
    {
        for (auto& [fk, f] : r.functions) {
            std::set<uint32_t> s;
            for (uint16_t a : f.block_addresses)
                if (r.blocks.count(key(f.bank, a))) s.insert(key(f.bank, a));
            if (!s.count(fk)) continue;  // a function must contain its entry block
            auto& ins = fn_insns[fk];
            for (uint32_t bk : s)
                for (size_t idx : r.blocks.at(bk).instruction_indices)
                    ins.insert(key(r.instructions[idx].bank, r.instructions[idx].address));
            fn_blocks[fk] = std::move(s);
        }
        needed.insert(key(0, r.entry_point));
        for (uint16_t v : {0x40, 0x48, 0x50, 0x58, 0x60}) needed.insert(key(0, v));
        for (auto* set : {&r.computed_jump_targets, &r.branch_entry_targets, &r.synthetic_entry_targets})
            needed.insert(set->begin(), set->end());
        for (auto& [bk, b] : r.blocks) {
            for (size_t idx : b.instruction_indices) {
                const Instruction& in = r.instructions[idx];
                uint16_t next = in.address + in.length;
                switch (in.type) {
                case InstructionType::CALL_NN: case InstructionType::CALL_CC_NN:
                case InstructionType::JP_NN: case InstructionType::JP_CC_NN: {
                    BankId tb = target_bank(in, in.imm16);
                    if (tb != UNKNOWN_BANK) needed.insert(key(tb, in.imm16));
                    break;
                }
                case InstructionType::JR_N: case InstructionType::JR_CC_N:
                    needed.insert(key(in.bank, uint16_t(next + in.offset)));
                    break;
                case InstructionType::RST:
                    if (!rst_padding(in.rst_vector)) needed.insert(key(0, in.rst_vector));
                    break;
                default: break;
                }
                if (in.is_call || in.type == InstructionType::HALT) needed.insert(key(in.bank, next));
                if (in.type == InstructionType::JR_N || in.type == InstructionType::JR_CC_N) {
                    uint16_t t = next + in.offset;
                    if (t <= in.address) needed.insert(key(in.bank, t));  // safepoint resume
                }
            }
        }
        for (auto& [fk, blocks] : fn_blocks) owner.emplace(fk, fk);
        for (uint32_t k : needed) {
            if (owner.count(k)) continue;
            for (auto& [fk, ins] : fn_insns)
                if (ins.count(k)) { owner[k] = fk; entries[fk].insert(k); break; }
        }
    }

    // Branch to k: goto inside this function, else tail-call the owner, else dynamic dispatch.
    std::string jump(uint32_t k, uint16_t from, bool safepoint)
    {
        std::string sp = safepoint && kaddr(k) <= from ? "GB_SAFEPOINT(" + hex4(kaddr(k)) + "); " : "";
        if (fn_insns[cur].count(k)) { labels.insert(k); return "{ " + sp + "goto " + label(kaddr(k)) + "; }"; }
        auto it = owner.find(k);
        std::string a = hex4(kaddr(k));
        if (it != owner.end()) return "{ " + sp + "GB_JUMP(" + a + ", " + fn_name(it->second) + "(" + a + ")); }";
        stats["dynamic_no_entry"]++;
        unresolved.push_back(fmt_addr(k) + " no compiled entry");
        return "{ " + sp + "GB_JUMP(" + a + ", gb_dispatch(" + a + ")); }";
    }

    std::string invoke(uint32_t k)
    {
        auto it = owner.find(k);
        if (it != owner.end()) return fn_name(it->second) + "(" + hex4(kaddr(k)) + ")";
        stats["dynamic_no_entry"]++;
        unresolved.push_back(fmt_addr(k) + " no compiled entry");
        return "gb_dispatch(" + hex4(kaddr(k)) + ")";
    }

    // Static target with runtime bank guard for bank-0 code calling into 0x4000-0x7FFF.
    std::string branch(const Instruction& in, uint16_t t, bool is_call)
    {
        uint16_t next = in.address + in.length;
        BankId tb = target_bank(in, t);
        std::string body;
        if (tb == UNKNOWN_BANK) {
            stats["dynamic_bank"]++;
            body = is_call ? "gb_dispatch(" + hex4(t) + ")" : "GB_JUMP(" + hex4(t) + ", gb_dispatch(" + hex4(t) + "));";
        } else if (in.bank == 0 && t >= 0x4000) {
            stats["guarded_bank"]++;
            std::string direct = is_call ? invoke(key(tb, t)) : jump(key(tb, t), in.address, false);
            if (is_call) {
                body = "if (gb.rom_bank == " + std::to_string(tb) + ") " + direct + "; else gb_dispatch(" + hex4(t) + ")";
            } else {
                body = "{ if (gb.rom_bank == " + std::to_string(tb) + ") " + direct + " GB_JUMP(" + hex4(t) + ", gb_dispatch(" + hex4(t) + ")); }";
            }
        } else {
            body = is_call ? invoke(key(tb, t)) : jump(key(tb, t), in.address, true);
        }
        if (!is_call) return body;
        return "GB_CALL(" + hex4(next) + ", " + body + ");";
    }

    std::string emit(const Instruction& in)
    {
        using T = InstructionType;
        uint16_t next = in.address + in.length;
        auto hl_rmw = [](const std::string& f) { return "{ uint16_t a_ = HL(); wr8(a_, " + f + "(rd8(a_))); }"; };
        std::string imm = std::to_string(in.imm8);
        switch (in.type) {
        case T::NOP: return "";
        case T::LD_R_R: return set8(in.reg8_dst, r8(in.reg8_src));
        case T::LD_R_N: return set8(in.reg8_dst, imm);
        case T::LD_R_HL: return set8(in.reg8_dst, "rd8(HL())");
        case T::LD_HL_R: return "wr8(HL(), " + std::string(r8(in.reg8_src)) + ");";
        case T::LD_HL_N: return "wr8(HL(), " + imm + ");";
        case T::LD_A_BC: return "gb.a = rd8(BC());";
        case T::LD_A_DE: return "gb.a = rd8(DE());";
        case T::LD_BC_A: return "wr8(BC(), gb.a);";
        case T::LD_DE_A: return "wr8(DE(), gb.a);";
        case T::LD_A_NN: return "gb.a = rd8(" + hex4(in.imm16) + ");";
        case T::LD_NN_A: return "wr8(" + hex4(in.imm16) + ", gb.a);";
        case T::LDH_A_N: return "gb.a = rd8(" + hex4(0xFF00 | in.imm8) + ");";
        case T::LDH_N_A: return "wr8(" + hex4(0xFF00 | in.imm8) + ", gb.a);";
        case T::LDH_A_C: return "gb.a = rd8(0xFF00 | gb.c);";
        case T::LDH_C_A: return "wr8(0xFF00 | gb.c, gb.a);";
        case T::LD_A_HLI: return "gb.a = rd8(HL()); SET_HL(HL() + 1);";
        case T::LD_A_HLD: return "gb.a = rd8(HL()); SET_HL(HL() - 1);";
        case T::LD_HLI_A: return "wr8(HL(), gb.a); SET_HL(HL() + 1);";
        case T::LD_HLD_A: return "wr8(HL(), gb.a); SET_HL(HL() - 1);";
        case T::LD_RR_NN: return set16(in.reg16, hex4(in.imm16));
        case T::LD_SP_HL: return "gb.sp = HL();";
        case T::LD_NN_SP: return "wr8(" + hex4(in.imm16) + ", gb.sp); wr8(" + hex4(in.imm16 + 1) + ", gb.sp >> 8);";
        case T::LD_HL_SP_N: return "SET_HL(alu_sp_offset(" + std::to_string(in.offset) + "));";
        case T::PUSH: return "push16(" + get16(in.reg16) + ");";
        case T::POP: return set16(in.reg16, "pop16()");
        case T::ADD_A_R: return "alu_add(" + std::string(r8(in.reg8_src)) + ", 0);";
        case T::ADD_A_N: return "alu_add(" + imm + ", 0);";
        case T::ADD_A_HL: return "alu_add(rd8(HL()), 0);";
        case T::ADC_A_R: return "alu_add(" + std::string(r8(in.reg8_src)) + ", gb.cf);";
        case T::ADC_A_N: return "alu_add(" + imm + ", gb.cf);";
        case T::ADC_A_HL: return "alu_add(rd8(HL()), gb.cf);";
        case T::SUB_A_R: return "gb.a = alu_sub(" + std::string(r8(in.reg8_src)) + ", 0);";
        case T::SUB_A_N: return "gb.a = alu_sub(" + imm + ", 0);";
        case T::SUB_A_HL: return "gb.a = alu_sub(rd8(HL()), 0);";
        case T::SBC_A_R: return "gb.a = alu_sub(" + std::string(r8(in.reg8_src)) + ", gb.cf);";
        case T::SBC_A_N: return "gb.a = alu_sub(" + imm + ", gb.cf);";
        case T::SBC_A_HL: return "gb.a = alu_sub(rd8(HL()), gb.cf);";
        case T::AND_A_R: return "alu_and(" + std::string(r8(in.reg8_src)) + ");";
        case T::AND_A_N: return "alu_and(" + imm + ");";
        case T::AND_A_HL: return "alu_and(rd8(HL()));";
        case T::OR_A_R: return "alu_or(" + std::string(r8(in.reg8_src)) + ");";
        case T::OR_A_N: return "alu_or(" + imm + ");";
        case T::OR_A_HL: return "alu_or(rd8(HL()));";
        case T::XOR_A_R: return "alu_xor(" + std::string(r8(in.reg8_src)) + ");";
        case T::XOR_A_N: return "alu_xor(" + imm + ");";
        case T::XOR_A_HL: return "alu_xor(rd8(HL()));";
        case T::CP_A_R: return "alu_sub(" + std::string(r8(in.reg8_src)) + ", 0);";
        case T::CP_A_N: return "alu_sub(" + imm + ", 0);";
        case T::CP_A_HL: return "alu_sub(rd8(HL()), 0);";
        case T::INC_R: return set8(in.reg8_dst, "alu_inc(" + std::string(r8(in.reg8_dst)) + ")");
        case T::DEC_R: return set8(in.reg8_dst, "alu_dec(" + std::string(r8(in.reg8_dst)) + ")");
        case T::INC_HL_IND: return hl_rmw("alu_inc");
        case T::DEC_HL_IND: return hl_rmw("alu_dec");
        case T::ADD_HL_RR: return "alu_add_hl(" + get16(in.reg16) + ");";
        case T::ADD_SP_N: return "gb.sp = alu_sp_offset(" + std::to_string(in.offset) + ");";
        case T::INC_RR: return set16(in.reg16, get16(in.reg16) + " + 1");
        case T::DEC_RR: return set16(in.reg16, get16(in.reg16) + " - 1");
        case T::RLCA: return "a_rot(cb_rlc(gb.a));";
        case T::RRCA: return "a_rot(cb_rrc(gb.a));";
        case T::RLA: return "a_rot(cb_rl(gb.a));";
        case T::RRA: return "a_rot(cb_rr(gb.a));";
#define CB(OP, f) case T::OP##_R: return set8(in.reg8_dst, std::string(f "(") + r8(in.reg8_dst) + ")"); \
                  case T::OP##_HL: return hl_rmw(f);
        CB(RLC, "cb_rlc") CB(RRC, "cb_rrc") CB(RL, "cb_rl") CB(RR, "cb_rr")
        CB(SLA, "cb_sla") CB(SRA, "cb_sra") CB(SWAP, "cb_swap") CB(SRL, "cb_srl")
#undef CB
        case T::BIT_N_R: return "cb_bit(" + std::to_string(in.bit_index) + ", " + r8(in.reg8_dst) + ");";
        case T::BIT_N_HL: return "cb_bit(" + std::to_string(in.bit_index) + ", rd8(HL()));";
        case T::SET_N_R: return std::string(r8(in.reg8_dst)) + " |= " + std::to_string(1 << in.bit_index) + ";";
        case T::RES_N_R: return std::string(r8(in.reg8_dst)) + " &= " + std::to_string(uint8_t(~(1 << in.bit_index))) + ";";
        case T::SET_N_HL: return "{ uint16_t a_ = HL(); wr8(a_, rd8(a_) | " + std::to_string(1 << in.bit_index) + "); }";
        case T::RES_N_HL: return "{ uint16_t a_ = HL(); wr8(a_, rd8(a_) & " + std::to_string(uint8_t(~(1 << in.bit_index))) + "); }";
        case T::CCF: return "gb.cf ^= 1; gb.nf = gb.hf = 0;";
        case T::SCF: return "gb.cf = 1; gb.nf = gb.hf = 0;";
        case T::DAA: return "alu_daa();";
        case T::CPL: return "gb.a = ~gb.a; gb.nf = gb.hf = 1;";
        case T::DI: return "gb_di();";
        case T::EI: return "gb_ei();";
        case T::HALT: return "if (gb_halt(" + hex4(next) + ")) return;";
        case T::STOP: return "gb_stop(" + hex4(next) + ");";
        case T::JP_NN: return branch(in, in.imm16, false);
        case T::JP_CC_NN: return "if (" + std::string(cond(in.condition)) + ") " + branch(in, in.imm16, false);
        case T::JR_N: return jump(key(in.bank, uint16_t(next + in.offset)), in.address, true);
        case T::JR_CC_N: return "if (" + std::string(cond(in.condition)) + ") " + jump(key(in.bank, uint16_t(next + in.offset)), in.address, true);
        case T::JP_HL: stats["jp_hl"]++; return "{ uint16_t t_ = HL(); GB_JUMP(t_, gb_dispatch(t_)); }";
        case T::CALL_NN: return branch(in, in.imm16, true);
        case T::CALL_CC_NN: return "if (" + std::string(cond(in.condition)) + ") { " + branch(in, in.imm16, true) + " }";
        case T::RST:
            if (rst_padding(in.rst_vector)) {
                stats["rst_padding_trap"]++;
                return "gb_trap(" + std::to_string(in.bank) + ", " + hex4(in.address) + ", TRAP_UNDEFINED);";
            }
            return "GB_CALL(" + hex4(next) + ", " + invoke(key(0, in.rst_vector)) + ");";
        case T::RET: return "{ gb.ret = pop16(); return; }";
        case T::RET_CC: return "if (" + std::string(cond(in.condition)) + ") { gb.ret = pop16(); return; }";
        case T::RETI: return "{ gb.ret = pop16(); gb_ei(); return; }";
        default:
            stats["undefined"]++;
            return "gb_trap(" + std::to_string(in.bank) + ", " + hex4(in.address) + ", TRAP_UNDEFINED);";
        }
    }

    static bool ends_flow(const Instruction& in)
    {
        using T = InstructionType;
        switch (in.type) {
        case T::JP_NN: case T::JR_N: case T::JP_HL: case T::RET: case T::RETI:
        case T::UNDEFINED: case T::INVALID: return true;
        default: return false;
        }
    }

    void emit_function(uint32_t fk, std::ostream& o)
    {
        cur = fk;
        labels = entries[fk];
        labels.insert(fk);
        const auto& blocks = fn_blocks[fk];
        // body first: jump() records which labels are referenced
        std::vector<std::pair<uint32_t, std::string>> lines;  // (instruction key or 0, text)
        for (auto it = blocks.begin(); it != blocks.end(); ++it) {
            const BasicBlock& b = r.blocks.at(*it);
            const Instruction* last = nullptr;
            for (size_t idx : b.instruction_indices) {
                const Instruction& in = r.instructions[idx];
                stats["instructions"]++;
                std::string code = emit(in);
                lines.emplace_back(key(in.bank, in.address), "    /* " + fmt_addr(in.bank, in.address) + " " +
                                   in.disassemble() + " */" + (code.empty() ? "" : " " + code));
                last = &in;
            }
            if (!last || ends_flow(*last)) continue;
            uint32_t fall = key(last->bank, last->address + last->length);
            auto nx = std::next(it);
            if (nx != blocks.end() && *nx == fall) continue;
            lines.emplace_back(0, "    " + jump(fall, last->address, false));
        }
        o << "void " << fn_name(fk) << "(uint16_t at)\n{\n";
        auto& extra = entries[fk];
        if (!extra.empty() || *blocks.begin() != fk) {
            o << "    switch (at) {\n";
            for (uint32_t e : extra) o << "    case " << hex4(kaddr(e)) << ": goto " << label(kaddr(e)) << ";\n";
            o << "    default: goto " << label(kaddr(fk)) << ";\n    }\n";
        } else {
            o << "    (void)at;\n";
        }
        std::set<uint32_t> placed;
        for (auto& [k, text] : lines) {
            if (k && labels.count(k) && placed.insert(k).second) o << label(kaddr(k)) << ":\n";
            o << text << "\n";
        }
        o << "}\n\n";
    }
};

}  // namespace

int codegen(const ROM& rom, const AnalysisResult& r, const std::string& outdir)
{
    fs::create_directories(outdir);
    Gen g(r, rom);
    g.collect();

    std::map<BankId, std::vector<uint32_t>> by_bank;
    for (auto& [fk, blocks] : g.fn_blocks) by_bank[kbank(fk)].push_back(fk);

    {
        std::ofstream h(fs::path(outdir) / "gen.h");
        h << "// generated by gbforge-core — do not edit\n#pragma once\n#include \"gb.h\"\n\n";
        for (auto& [fk, blocks] : g.fn_blocks) h << "void " << fn_name(fk) << "(uint16_t at);\n";
    }
    for (auto& [bank, fns] : by_bank) {
        char name[32];
        std::snprintf(name, sizeof name, "gen_%02X.c", bank);
        std::ofstream o(fs::path(outdir) / name);
        o << "// generated by gbforge-core — do not edit\n#include \"gen.h\"\n\n";
        for (uint32_t fk : fns) g.emit_function(fk, o);
    }

    std::ofstream t(fs::path(outdir) / "gen_table.c");
    t << "// generated by gbforge-core — do not edit\n#include \"gen.h\"\n\n";
    t << "const GbEntry gb_entries[] = {\n";
    size_t n = 0;
    for (auto& [k, fk] : g.owner) { t << "    {0x" << std::hex << k << std::dec << ", " << fn_name(fk) << "},\n"; n++; }
    t << "};\nconst uint32_t gb_entry_count = " << n << ";\n\n";
    t << "const GbFn gb_vectors[5] = {";
    for (uint16_t v : {0x40, 0x48, 0x50, 0x58, 0x60}) {
        auto it = g.owner.find(key(0, v));
        t << (v == 0x40 ? "" : ", ") << (it != g.owner.end() ? fn_name(it->second) : "0");
    }
    t << "};\n\n";
    auto entry = g.owner.find(key(0, r.entry_point));
    if (entry == g.owner.end()) {
        std::fprintf(stderr, "error: no function at entry point %04X\n", r.entry_point);
        return 3;
    }
    t << "void gb_entry(void)\n{\n    " << fn_name(entry->second) << "(" << hex4(r.entry_point)
      << ");\n    for (;;) gb_dispatch(gb.ret);\n}\n";

    std::ofstream j(fs::path(outdir) / "codegen.json");
    j << "{\n  \"schema\": \"gbforge.codegen\",\n  \"schema_version\": 1,\n";
    j << "  \"rom_banks\": " << rom.bank_count() << ",\n  \"functions\": " << r.functions.size()
      << ",\n  \"entries\": " << n << ",\n  \"analysis_unresolved_edges\": " << count_unresolved(r) << ",\n  \"stats\": {";
    bool first = true;
    for (auto& [k, v] : g.stats) { j << (first ? "" : ", ") << json_str(k) << ": " << v; first = false; }
    j << "},\n  \"unresolved\": [";
    first = true;
    for (auto& u : g.unresolved) { j << (first ? "" : ", ") << json_str(u); first = false; }
    j << "]\n}\n";
    return 0;
}
