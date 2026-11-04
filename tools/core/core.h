#pragma once
#include <recompiler/analyzer.h>
#include <recompiler/rom.h>
#include <string>

std::string fmt_addr(uint32_t full);
std::string fmt_addr(gbrecomp::BankId bank, uint16_t addr);
std::string json_str(const std::string& s);
size_t count_unresolved(const gbrecomp::AnalysisResult& r);
int codegen(const gbrecomp::ROM& rom, const gbrecomp::AnalysisResult& r, const std::string& outdir);
