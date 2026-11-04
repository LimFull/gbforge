// gbforge-core — host tool on top of gb-recompiled's analyzer (gbrecomp_core).
//
//   gbforge-core analyze ROM OUT.json [--entry BB:AAAA]... [--trace FILE] [--cdl FILE] [--no-scan]
//   gbforge-core codegen ROM OUTDIR   [--entry BB:AAAA]... [--trace FILE] [--cdl FILE] [--no-scan]
//
// --cdl: Mesen Code/Data Logger flags for PRG ROM (scripts/mesen_cdl.py). Executed jump targets
// and sub entries become entry points; bytes only ever read as data become data annotations.
// This is coverage evidence from a real run, never a resolution of an unresolved indirect edge.
//
// Exit codes follow the gb2gba contract: 0 ok, 2 invalid ROM, 3 unresolved paths (codegen, strict).
#include "core.h"

#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>

using namespace gbrecomp;

std::string fmt_addr(uint32_t full) {
    char buf[16];
    std::snprintf(buf, sizeof buf, "%02X:%04X", full >> 16, full & 0xFFFF);
    return buf;
}

std::string fmt_addr(BankId bank, uint16_t addr) {
    return fmt_addr((uint32_t(bank) << 16) | addr);
}

std::string json_str(const std::string& s) {
    std::string out = "\"";
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { out += '\\'; out += char(c); }
        else if (c == '\n') out += "\\n";
        else if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); out += b; }
        else out += char(c);
    }
    return out + "\"";
}

static bool parse_addr(const char* s, uint32_t& out) {
    unsigned bank, addr;
    if (std::sscanf(s, "%x:%x", &bank, &addr) != 2 || bank > 0x1FF || addr > 0xFFFF) return false;
    out = (bank << 16) | addr;
    return true;
}

static bool load_cdl(const char* path, const ROM& rom, AnalyzerOptions& opts)
{
    std::ifstream f(path, std::ios::binary);
    std::vector<uint8_t> cdl((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (cdl.size() != rom.size()) {
        std::fprintf(stderr, "error: CDL %s has %zu bytes, ROM has %zu\n", path, cdl.size(), rom.size());
        return false;
    }
    auto full = [](size_t off) {
        uint32_t bank = off / 0x4000;
        return (bank << 16) | uint32_t(off % 0x4000 + (bank ? 0x4000 : 0));
    };
    enum { CODE = 1, DATA = 2, JUMP_TARGET = 4, SUB_ENTRY = 8 };
    for (size_t i = 0; i < cdl.size(); i++) {
        if (cdl[i] & (JUMP_TARGET | SUB_ENTRY)) opts.entry_points.push_back(full(i));
        // ponytail: data-only in this run; a byte executed only in another run would be mis-annotated
        if ((cdl[i] & (CODE | DATA)) == DATA && (i == 0 || (cdl[i - 1] & (CODE | DATA)) != DATA || i % 0x4000 == 0)) {
            size_t j = i;
            while (j < cdl.size() && (cdl[j] & (CODE | DATA)) == DATA && (j == i || j % 0x4000)) j++;
            opts.annotations.push_back({full(i), uint32_t(j - i), AnalysisAnnotationKind::DATA});
        }
    }
    return true;
}

static void write_analysis(const AnalysisResult& r, std::ostream& o) {
    std::map<std::string, size_t> by_kind;
    for (auto& [id, d] : r.analysis_diagnostics) by_kind[d.kind]++;
    size_t dynamic_switches = 0;
    for (auto& s : r.bank_tracker.switches()) dynamic_switches += s.is_dynamic;

    o << "{\n  \"schema\": \"gbforge.analysis\",\n  \"schema_version\": 1,\n";
    o << "  \"analyzer\": {\"name\": \"gb-recompiled\", \"commit\": \"" GBRECOMPILED_COMMIT "\"},\n";
    o << "  \"summary\": {\"instructions\": " << r.instructions.size()
      << ", \"functions\": " << r.functions.size() << ", \"blocks\": " << r.blocks.size()
      << ", \"bank_switches\": " << r.bank_tracker.switches().size()
      << ", \"dynamic_bank_switches\": " << dynamic_switches
      << ", \"cross_bank_calls\": " << r.bank_tracker.calls().size()
      << ", \"computed_jump_targets\": " << r.computed_jump_targets.size()
      << ", \"unresolved_edges\": " << count_unresolved(r) << ", \"diagnostics_by_kind\": {";
    bool first = true;
    for (auto& [k, n] : by_kind) { o << (first ? "" : ", ") << json_str(k) << ": " << n; first = false; }
    o << "}},\n";

    o << "  \"functions\": [";
    first = true;
    for (auto& [key, f] : r.functions) {
        o << (first ? "\n" : ",\n") << "    {\"at\": \"" << fmt_addr(key) << "\", \"blocks\": " << f.block_addresses.size()
          << ", \"interrupt\": " << (f.is_interrupt_handler ? "true" : "false")
          << ", \"switches_bank\": " << (f.may_switch_rom_bank ? "true" : "false") << "}";
        first = false;
    }
    o << "\n  ],\n  \"blocks\": [";
    first = true;
    for (auto& [key, b] : r.blocks) {
        o << (first ? "\n" : ",\n") << "    {\"at\": \"" << fmt_addr(key) << "\", \"end\": \"" << fmt_addr(b.bank, b.end_address)
          << "\", \"reachable\": " << (b.is_reachable ? "true" : "false") << ", \"succ\": [";
        for (size_t i = 0; i < b.successors.size(); i++) o << (i ? ", " : "") << "\"" << fmt_addr(b.successors[i]) << "\"";
        o << "]}";
        first = false;
    }
    o << "\n  ],\n  \"bank_switches\": [";
    first = true;
    for (auto& s : r.bank_tracker.switches()) {
        o << (first ? "\n" : ",\n") << "    {\"at\": \"" << fmt_addr(s.addr) << "\", \"bank\": ";
        if (s.target_bank == UNKNOWN_BANK) o << "null"; else o << s.target_bank;
        o << ", \"dynamic\": " << (s.is_dynamic ? "true" : "false") << "}";
        first = false;
    }
    o << "\n  ],\n  \"cross_bank_calls\": [";
    first = true;
    for (auto& c : r.bank_tracker.calls()) {
        o << (first ? "\n" : ",\n") << "    {\"from\": \"" << fmt_addr(c.from_bank, c.from_addr & 0xFFFF)
          << "\", \"to\": \"" << fmt_addr(c.to_bank, c.to_addr & 0xFFFF) << "\"}";
        first = false;
    }
    o << "\n  ],\n  \"computed_jump_targets\": [";
    first = true;
    for (auto t : r.computed_jump_targets) { o << (first ? "" : ", ") << "\"" << fmt_addr(t) << "\""; first = false; }
    o << "],\n  \"diagnostics\": [";
    first = true;
    for (auto& [id, d] : r.analysis_diagnostics) {
        o << (first ? "\n" : ",\n") << "    {\"id\": " << json_str(id) << ", \"kind\": " << json_str(d.kind)
          << ", \"at\": \"" << fmt_addr(d.bank, d.address) << "\", \"status\": " << json_str(d.status)
          << ", \"relationship\": " << json_str(d.relationship) << ", \"evidence\": " << json_str(d.evidence)
          << ", \"suggested_annotation\": " << json_str(d.suggested_annotation);
        if (d.has_related_address) o << ", \"related\": \"" << fmt_addr(d.related_bank, d.related_address) << "\"";
        o << "}";
        first = false;
    }
    o << "\n  ]\n}\n";
}

size_t count_unresolved(const AnalysisResult& r) {
    size_t n = 0;
    for (auto& [id, d] : r.analysis_diagnostics)
        n += d.relationship == "potential_dispatch_fallback";
    return n;
}

int main(int argc, char** argv) {
    if (argc < 4 || (std::strcmp(argv[1], "analyze") && std::strcmp(argv[1], "codegen"))) {
        std::fprintf(stderr, "usage: gbforge-core analyze|codegen ROM OUT [--entry BB:AAAA]... [--trace FILE] [--no-scan]\n");
        return 2;
    }
    AnalyzerOptions opts;
    const char* cdl_path = nullptr;
    for (int i = 4; i < argc; i++) {
        uint32_t a;
        if (!std::strcmp(argv[i], "--entry") && i + 1 < argc && parse_addr(argv[i + 1], a)) { opts.entry_points.push_back(a); i++; }
        else if (!std::strcmp(argv[i], "--trace") && i + 1 < argc) opts.trace_file_path = argv[++i];
        else if (!std::strcmp(argv[i], "--no-scan")) opts.aggressive_scan = false;
        else if (!std::strcmp(argv[i], "--cdl") && i + 1 < argc) cdl_path = argv[++i];
        else { std::fprintf(stderr, "unknown option: %s\n", argv[i]); return 2; }
    }

    auto rom = ROM::load(argv[2]);
    if (!rom || !rom->is_valid()) {
        std::fprintf(stderr, "error: cannot load ROM: %s\n", rom ? rom->error().c_str() : argv[2]);
        return 2;
    }
    if (cdl_path && !load_cdl(cdl_path, *rom, opts)) return 2;
    // The analyzer logs progress to stdout; keep stdout for our own output only.
    std::streambuf* saved = std::cout.rdbuf(std::cerr.rdbuf());
    AnalysisResult result = analyze(*rom, opts);
    std::cout.rdbuf(saved);

    if (!std::strcmp(argv[1], "analyze")) {
        std::ofstream out(argv[3]);
        write_analysis(result, out);
        return out ? 0 : 1;
    }
    return codegen(*rom, result, argv[3]);
}
