# Phase 0 스파이크: gb-recompiled 재사용성 검증

- 작성일: 2026-11-04
- 범위: 계획서 §7 "GB Recompiled 재사용 전략"의 스파이크 질문 6개(Q1–Q6)와 재사용 게이트 판정
- 대상: `arcanite24/gb-recompiled` @ `9150f87d82fa98abcb6ea22329170463f9702eb8` (2026-09-10, "Fix safepoint checks for generated direct jumps")
- 입력 ROM: `roms/dq12-jp.gbc`(상용, 로컬 전용), `tests/fixtures/tiny/tiny.gb`(자체 homebrew)
- 모든 생성물은 `build/spike/`(gitignore)에만 있다. 상용 ROM에서 생성된 코드는 이 문서에 넣지 않았다. 아래 코드 예시는 전부 `tiny.gb` 생성물에서 가져왔다.

## 0. 결론 요약

**판정: Conditional.** 분석기와 IR(`gbrecomp_core`)은 재사용하고, GBA용 코드 생성기와 런타임은 새로 작성한다.

| 게이트 조건 (§7) | 결과 | 근거 |
|---|---|---|
| 분석·코드 생성기 분리 가능 | **충족** | SDL2 없이 `gbrecomp`와 `libgbrecomp_core.a`가 빌드된다. 외부 C++ 프로그램에서 라이브러리로 링크해 분석 결과를 얻는 것까지 확인했다. |
| strict 모드용 미해결 경로 검출 | **부분 충족** | 정적 진단 11,550건이 `*_metadata.json`에 나온다. 폴백은 `gbrt_execute_dispatch_fallback` 한 곳으로 모인다. 다만 strict 모드는 내장되어 있지 않다. 또 DQ 실행 중에는 정적 분석이 놓친 진입점 5곳이 실제로 폴백을 탔다. |
| GBA 메모리·코드 크기 제어 | **미충족** | DQ 생성 코드의 Thumb `.text`만 **37.50 MiB**다. GBA ROM 링크는 `region 'rom' overflowed by 9,073,628 bytes`로 실패한다. 런타임은 ROM 2 MiB를 `malloc`으로 복사하고, PPU 구조체 하나가 161,512 B다. |

생성 C를 그대로 GBA에 올리는 것은 구조적으로 맞지 않는다. 명령마다 `gb_tick` 호출, safepoint, single-step 검사가 들어가고, 모든 명령 주소에 dispatch case가 생긴다. 이것은 사실상 "CPU만 컴파일된 사이클 정확 에뮬레이터" 모델이다. 반면 분석 결과(함수/블록/뱅크 전환/교차 뱅크 호출/미해결 점프)와 IR은 공개 헤더로 깔끔하게 노출되고 MIT 라이선스다. 그래서 계획서의 Conditional 정의("IR 또는 CFG만 재활용하고 GBA용 코드 생성기를 별도로 작성")와 정확히 맞는다.

---

## 1. 환경

| 항목 | 버전 |
|---|---|
| OS | macOS 15.7.4 (24G517), arm64, 14코어 |
| CMake | 4.4.4 |
| Ninja | 1.13.2 |
| Host 컴파일러 | Apple clang 17.0.0 (clang-1700.6.3.2) |
| Python | 3.15.2 |
| SDL2 | sdl2-compat 2.32.74 (Homebrew) |
| Docker | 28.3.2 (14 CPU, 8 GiB 할당) |
| GBA 크로스 컴파일러 | `devkitpro/devkitarm` 이미지, `arm-none-eabi-gcc (devkitARM) 16.1.0` |

입력 ROM:

| ROM | SHA-256 | 헤더 |
|---|---|---|
| `dq12-jp.gbc` | `7cae9c90cf50e5b014f700090626e686aaf5978105901b0346410b0a89438caf` | DQ1&2, MBC5+RAM+BATTERY, 2048 KB(128 뱅크), RAM 8 KB, CGB Enhanced, SGB 지원, 체크섬 OK |
| `tiny.gb` | `133d82836e17e149046e572c64da8054f3d53c65d9bcd11158248c92c7a7791b` | TINY, ROM ONLY, 32 KB, DMG |

## 2. 클론과 라이선스

```bash
git clone https://github.com/arcanite24/gb-recompiled.git ~/dev-external/gb-recompiled
git -C ~/dev-external/gb-recompiled log -1   # 9150f87d82fa98abcb6ea22329170463f9702eb8
```

- 서브모듈 없음
- `LICENSE`: MIT, "Copyright (c) 2026 arcanite24"
- `runtime/vendor/imgui/LICENSE.txt`: Dear ImGui, MIT, "Copyright (c) 2014-2024 Omar Cornut". `imstb_*.h`(stb)도 함께 들어 있다.
- README §License: "GB Recompiled is available under the MIT License. Dear ImGui remains under its upstream license in the vendored runtime snapshot."
- 그 밖의 `LICENSE*` 파일은 없다(`find . -iname 'LICENSE*'`).

## 3. 빌드와 테스트

### 3.1 호스트 도구 (README 절차)

```bash
cd ~/dev-external/gb-recompiled
cmake -G Ninja -B build . && ninja -C build
```

- 결과: 성공, 33 스텝, 약 7.8 s(wall). 경고 19건(`-Wnested-anon-types`, unused variable 등)이 있지만 오류는 없다.
- 산출물: `build/bin/gbrecomp`, `build/lib/libgbrecomp_core.a`, `build/lib/libgbrt.a`(SDL2와 ImGui 포함)

### 3.2 SDL2 비활성 빌드 (Q1 검증용)

```bash
cmake -G Ninja -B build-nosdl . -DCMAKE_DISABLE_FIND_PACKAGE_SDL2=ON && ninja -C build-nosdl
otool -L build-nosdl/bin/gbrecomp
```

- 결과: 성공, 13 스텝, 약 4.8 s. "SDL2 not found - runtime will not be built" 메시지가 나오고 런타임 타깃은 빠진다.
- `gbrecomp`가 의존하는 동적 라이브러리는 `libc++.1.dylib`와 `libSystem.B.dylib` 두 개뿐이다.

### 3.3 테스트 구성

```bash
cmake -G Ninja -B build-tests . -DBUILD_TESTS=ON
ninja -C build-tests                       # 131 스텝, 성공
ctest --test-dir build-tests --output-on-failure -j8
```

- 결과: **85개 중 84개 통과, 1개 실패**, 40.2 s
- 실패: `#2 accuracy_runner_exit_policy`. 서브테스트 6개 중 2개가 실패한다.
  ```
  FAIL: test_dmg_only_blargg_oam_bug_is_forced_to_dmg
      self.assertEqual(1, len(tests))   AssertionError: 1 != 0
  FAIL: test_filter_matches_catalogue_paths_as_well_as_names
      self.assertGreater(len(ppu_tests), 0)   AssertionError: 0 not greater than 0
  ```
- 원인: `tools/run_tests.py`의 `build_test_list()`는 외부 Blargg/Mooneye 테스트 ROM이 디스크에 있을 때만 목록에 넣는다(`if rom.exists()`). 이 ROM들은 저장소에 없다. 그래서 목록이 비고 테스트가 실패한다. 생성기나 런타임 결함이 아니라 환경 의존 테스트다. 외부 ROM 카탈로그를 내려받은 뒤 다시 돌려 보지는 않았다(**미검증**).

## 4. 생성기 실행

```bash
OUT=build/spike
./build/bin/gbrecomp tests/fixtures/tiny/tiny.gb -o $OUT/tiny  --progress-json $OUT/tiny.progress.jsonl
./build/bin/gbrecomp roms/dq12-jp.gbc            -o $OUT/dq12  --progress-json $OUT/dq12.progress.jsonl
```

### 4.1 분석 통계

> 주의: DQ 실행에서 CLI는 일부 카운트를 **16진수로** 출력한다. 예를 들어 "Found 110f6 functions"는 69,878이다. 분석 로그 출력 뒤에 `std::hex` 상태가 복원되지 않는 버그다. 아래 표는 10진수로 바꾼 값이고, `*_metadata.json` 항목 수와 일치하는 것을 확인했다.

| 항목 | tiny.gb | dq12-jp.gbc |
|---|---:|---:|
| 종료 코드 | 0 | 0 |
| 소요 시간(wall) | 0.1 s | 8.3 s (user 50 s, 병렬) |
| 디코드된 명령 수 | 403 | 642,056 |
| 함수 | 26 | 69,878 |
| 기본 블록 | 61 | 131,971 |
| 필요한 레이블 | 38 | 70,801 |
| IR 블록 / IR 함수 | 66 / 26 | 192,261 / 69,878 |
| 분석 대상 뱅크 | 2 | 128 ("Analyzing all 128 banks") |
| 뱅크 전환 지점(정적+동적) | 0 | 323 (동적 278) ※라이브러리 프로브 |
| 교차 뱅크 호출 | 0 | 3,714 ※라이브러리 프로브 |
| computed jump 타깃(해결됨) | 0 | 57 ※라이브러리 프로브 |
| 로그의 `Unresolved JP HL` | 0 | 251줄 |
| 로그의 `Undefined instruction` | 0 | 2,872줄 (데이터를 코드로 디코드) |
| 점프 테이블 휴리스틱 / 정적 해결 JP HL | 0 / 0 | 3 / 1 |
| `analysis_diagnostics`(metadata.json) | 3 | 11,550 |

DQ `analysis_diagnostics`의 종류별 분포:

| kind | 건수 | relationship |
|---|---:|---|
| `data_as_code_candidate` | 7,685 | potential_false_code |
| `undefined_instruction` | 2,756 | potential_false_code |
| `uncertain_entry_point` | 566 | potential_dispatch_entry |
| `unresolved_direct_target` | 297 | potential_dispatch_fallback |
| `unresolved_indirect_jump` | 246 | potential_dispatch_fallback |

분석 옵션을 바꿔 본 결과(analyze-only `-a`):

| 옵션 | 함수 | 블록 | 레이블 | Unresolved JP HL 줄 | Undefined 줄 |
|---|---:|---:|---:|---:|---:|
| 기본(aggressive scan) | 69,878 | 131,971 | 70,801 | 251 | 2,872 |
| `--no-scan` | 62,170 | 119,792 | 65,836 | 198 | 2,771 |
| `--reachable-only` | 69,810 | 131,910 | 70,791 | 251 | 2,872 |

해석: 2 MiB ROM에서 64만 명령, 즉 대략 1 MB 이상을 코드로 디코드했다. 데이터를 코드로 오인한 후보가 1만 건을 넘는다. 스캔 옵션을 꺼도 함수 수는 11%만 줄어든다. 뱅크 시작 지점 휴리스틱 시드와 미확정 뱅크 분기가 주요 원인으로 보인다. 실제 DQ 코드 양은 이보다 훨씬 적을 것으로 추정한다(**미검증**). 정확히 줄이려면 트레이스나 annotation이 필요하다.

### 4.2 생성물 규모

| 항목 | tiny | dq12 |
|---|---:|---:|
| 출력 디렉터리의 전체 파일 | 59 | 301 |
| └ ROM 생성물(최상위) | 10 | 252 |
| └ 런타임 스냅샷 `runtime/` 복사본 | 49 (69,794줄, 3.4 MB) | 49 (동일) |
| 생성 C/H 파일 | 8 | 250 (`*_funcs_N.c` 189, `*_dispatch_chunk_N.c` 55, 그 외 6) |
| 생성 C/H 줄 수 | 9,337 | **7,143,091** |
| 생성 C/H 바이트 | 458,732 | **236,996,725 (226 MiB)** |
| `*_metadata.json` | 1,362줄 | 71,667,148 B |
| 디스크 사용량 | 3.9 MB | 298 MB |
| 가장 큰 파일 | — | `dq12_jp_rom.c` 13.1 MB (ROM을 `const uint8_t rom_data[2097152]`로 임베드), `dq12_jp_funcs_91.c` 3.5 MB, `dq12_jp_native.h` 2.7 MB |

CLI의 "Generated files" 목록에는 5개만 표시되지만 실제로는 위 표처럼 훨씬 많이 생성된다.

### 4.3 PC 빌드와 실행 (폴백 실측용, GBA 포트 아님)

```bash
cmake -G Ninja -S build/spike/dq12 -B build/spike/dq12/build && ninja -C build/spike/dq12/build   # 45 s, 268 스텝, 성공
./dq12_jp --headless --no-audio --limit-frames 1800 --save-dir run-saves --report-interpreter-hotspots
./dq12_jp --headless --no-audio --limit-frames 10800 --save-dir run-saves --ignore-rtc-persistence \
          --report-interpreter-hotspots --input "<300프레임부터 30프레임 간격 S/A/B/방향키 340개 이벤트>"
```

- 실행 파일: 49,426,728 B (arm64, MinSizeRel `-Os`, strip)
- 입력 없음, 1,800프레임(약 1 s): `[INTERP] No interpreter fallback recorded.`
- 버튼 연타, 10,800프레임(약 3 s): **폴백 지점 5곳, 폴백 진입 13,735회, 인터프리터 실행 68,760명령 / 879,744 cycle**
  - 첫 폴백은 frame 1,018에 발생했다(`001:4001`, `bank_not_compiled`).
  - `002:5505`와 `010:65F9`(`bank_not_compiled`)는 frame 3,444부터 **매 프레임** 1회씩 진입한다(각각 6,856회와 6,874회).
  - 인터프리터 cycle 비중은 약 0.12%다(단일 속도 70,224 cycle/frame 가정, 추정).
  - 연타 입력으로 게임이 실제 어느 장면까지 갔는지는 화면으로 확인하지 않았다(**미검증**).

## 5. 질문별 답변

### Q1. SDL2 런타임 없이 analyzer/codegen을 빌드하고 쓸 수 있는가? → **예 (실측)**

- 최상위 `CMakeLists.txt`는 `find_package(SDL2 QUIET)`를 쓰고, SDL2가 없으면 `runtime/`을 건너뛴다. 분석기와 코드 생성기는 `recompiler/` 아래의 `gbrecomp_core` 정적 라이브러리(rom, decoder, analyzer, bank_tracker, symbol_table, native_patch, generator, ir_builder, ir_optimizer, c_emitter)이고 SDL 의존성이 없다.
- `-DCMAKE_DISABLE_FIND_PACKAGE_SDL2=ON` 빌드가 성공했고, 생성된 `gbrecomp`는 libc++와 libSystem에만 의존한다.
- **라이브러리 사용 실증**: 외부 C++ 프로그램(`build/spike/probe/probe.cpp`, 15줄)을 `libgbrecomp_core.a`에만 링크했다(SDL·codegen 미사용). `gbrecomp::ROM::load()`와 `gbrecomp::analyze()`로 DQ 분석 결과를 얻었다.
  ```
  instructions=642056 functions=69878 blocks=131971 bank_switches=323 dynamic_switches=278
  cross_bank_calls=3714 computed_jump_targets=57 diagnostics=11550
  ```
- 제약: CLI가 만드는 프로젝트는 항상 SDL 기반 `<prefix>_main.c`와 `find_package(SDL2 REQUIRED)`가 들어간 `CMakeLists.txt`를 포함한다. 런타임 스냅샷 전체(ImGui 포함)도 복사된다. `EmitterOptions`에는 `emit_main_entry_point`, `emit_cmake` 같은 플래그가 있지만 CLI로 노출되어 있지 않다. GBA 쪽에서는 이 두 파일을 무시하면 된다. 실제로 `_main.c`를 빼고 나머지만 컴파일해 보니 아무것도 스텁할 필요가 없었다(§Q5).

### Q2. 생성 C가 요구하는 헤더 / libc / 동적 할당 / 런타임 API

**헤더**: 생성 소스(`_main.c` 제외)는 `<prefix>_internal.h` → `<prefix>.h` → `gbrt.h` → `<stdint.h> <stdbool.h> <stddef.h>` 순서로 include한다. 디스패처 파일 `<prefix>.c`만 `<stdio.h> <stdlib.h>`를 쓴다(`fprintf(stderr, …)`, `exit(0)`: 명령 수 제한과 트레이스용). SDL은 `<prefix>_main.c`에서만 쓴다.

**동적 할당**: 생성 코드에는 `malloc/calloc/free`가 없다. 전역 데이터는 `rom_data[]`(const, 2 MiB) 하나다. dispatch는 테이블이 아니라 `switch` 코드로 생성된다. 동적 할당은 **런타임**이 한다(§Q5의 RAM 표).

**GBContext ABI 결합**: 생성 코드는 `GBContext` 필드를 직접 읽고 쓴다. DQ 기준 접근 횟수는 다음과 같다: `single_step_mode`(751,846), `pc`(626,252), 레지스터 `a b c d e h l bc de hl af sp`, 플래그 `f_z f_n f_h f_c`, `stopped`, `ime`, `ime_pending`, `halted`, `wram`, `wram_bank`, `hram`, `io`, `dma.active`, `div_counter`, `mbc_type`, `trace_entries_enabled`. 이 구조체 레이아웃과 의미가 사실상 생성 코드의 ABI다.

**런타임 심볼 목록**: DQ 오브젝트 246개에 `arm-none-eabi-nm -u`를 돌린 외부 심볼 80개와, 소스에서 센 `gb_*`/`gbrt_*` 호출 지점 수(괄호 안)를 합쳐 정리했다. *inline*은 `gbrt.h`의 `static inline`이라 링크 심볼에는 나타나지 않는 항목이다.

| 그룹 | 심볼 (DQ 호출 지점 수) |
|---|---|
| 메모리 read/write | `gb_read8` (33,843; `<prefix>_fast_read8` inline 매크로를 거쳐 WRAM/HRAM은 직접 접근하고 나머지는 extern `gb_read8`), `gb_write8` (37,991), `gb_write16` (4,842), `gbrt_timed_bus_read8`* (4,523), `gbrt_timed_bus_rmw_write8`* (4,382), `gbrt_timed_bus_write8`*, `gbrt_timed_hl_read_auto` (3,711), `gbrt_timed_hl_write_auto` (4,247), `gbrt_timed_push16` (4,694), `gbrt_timed_pop16` (10,831), `gbrt_note_generated_{generic,specialized}_{read,write}`* |
| ALU/플래그 (전부 extern 함수 호출) | `gb_dec8` (70,116), `gb_rlca` (42,763), `gb_inc8` (38,017), `gb_sub8` (21,428), `gb_add8` (19,336), `gb_and8` (13,705), `gb_add16` (9,083), `gb_or8` (8,634), `gb_sbc8` (7,915), `gb_adc8` (7,413), `gb_cp8` (6,981), `gb_rra` (6,029), `gb_xor8` (5,395), `gb_rrca` (4,952), `gb_daa` (1,496), `gb_rla` (1,372), `gb_bit` (937), `gb_swap`, `gb_sla`, `gb_rr`, `gb_srl`, `gb_rlc`, `gb_rl`, `gb_rrc`, `gb_sra`, `gb_pack_flags`*, `gb_unpack_flags`* |
| 뱅킹 / 제어 흐름 | `gb_resolve_rom_bank` (9,290), `gbrt_timed_jump` (66,058), `gbrt_timed_rst` (28,026), `gbrt_timed_ret_cc` (11,699), `gbrt_timed_call` (9,167), `gbrt_timed_ret` (3,253), `gbrt_timed_reti` (1,227), `gbrt_note_generated_direct_transition`* (17,865), `gbrt_note_generated_indirect_dispatch`*; `gb_dispatch`와 `gb_dispatch_call`은 생성 코드가 정의하고 런타임이 호출한다 |
| 타이밍 / 사이클 | `gb_tick` (**605,611**), `gbrt_generated_safepoint`* (**711,592**, 내용은 `ctx->stopped != 0`), `gbrt_timed_inc16` (34,725), `gbrt_timed_dec16` (8,387), `gbrt_timed_ld_hl_sp_n` (2,603), `gbrt_timed_add_sp` (998), 변수 `gbrt_benchmark_fast_tick_enabled` |
| 인터럽트 / CPU 상태 | `gb_stop` (6,706), `gbrt_execute_halt` (533); 생성 코드가 정의하고 런타임이 호출하는 진입점 `int_vblank`, `int_lcd_stat`, `int_timer`, `int_serial`, `int_joypad`, `rst_00`…`rst_38`, `gb_main`; `ime`/`ime_pending`은 필드를 직접 조작 |
| PPU / APU 훅 | 생성 코드에서 **직접 호출하는 것은 없다**. PPU, APU, 타이머, DMA는 `gb_tick` / `gb_read8` / `gb_write8` 안에서 런타임이 사이클 단위로 진행시킨다(`ppu.c` 2,007줄, `audio.c` 1,076줄). |
| 인터프리터 폴백 / RAM 코드 | `gbrt_execute_dispatch_fallback` (**31,313**), `gbrt_try_execute_hram_stub`, `gbrt_try_execute_highmem_stub`, `gbrt_try_execute_ram_stub` |
| 초기화 / ROM | `gb_context_load_rom`, `gb_context_reset`, `rom_data`, `rom_size` |
| 디버그 / 기타 | `gbrt_log_trace`, `gbrt_trace_enabled`, `gbrt_instruction_count`, `gbrt_instruction_limit`, `gbrt_test_breakpoint_enabled` (3,173) |
| libc / libgcc | `fprintf`, `exit`, `__getreent`(newlib stderr), `__gnu_thumb1_case_{sqi,uqi,shi,uhi,si}`(Thumb switch 헬퍼) |

**런타임 코어**(`runtime/src`에서 `platform_sdl.cpp`를 뺀 C 파일 12개)의 외부 의존성 61개(GBA 크로스 컴파일 후 nm 기준):
- stdio/파일: `fopen fclose fread fwrite fflush fsync fileno fgetc fputc fputs fprintf vfprintf printf puts snprintf sscanf remove rename`
- 힙: `malloc calloc free strdup`
- 문자열: `memcpy memmove memset memcmp memchr strlen strcmp strcpy strchr strtok strtoull _ctype_`
- 기타: `getenv time exit __errno __getreent`
- soft-float/64비트 연산: `__aeabi_{fmul,fdiv,fcmplt,ui2f,ul2f,f2d,dmul,ddiv,ul2d,lmul,llsl,llsr,uldivmod,idiv,uidiv,uidivmod}`
- SDL 계층 전역: `g_joypad_buttons g_joypad_dpad`(`platform_sdl.cpp`에 정의)

생성 코드와 런타임 코어를 합치면 libc와 위 두 전역을 빼고 닫힌 집합이다.

### Q3. 인터프리터 폴백: 위치, 차단 가능 여부, 제거 시 동작

**위치와 경로**
- 구현은 `runtime/src/interpreter.c`(828줄)의 `gb_interpret(ctx, addr)`다. Thumb `-Os`로 5,820 B다.
- 진입점은 `runtime/src/gbrt.c:4619`의 `gbrt_execute_dispatch_fallback()`이 거의 전부다. 여기서 사이트 통계를 갱신한 뒤 `gb_interpret`를 호출한다. 다른 진입 경로는 다음과 같다.
  - `gb_step()`의 HALT bug 처리(`gbrt.c:5461`)
  - 차등(differential) 모드의 interpreter 실행 모드(`gbrt.c:5501`)
  - 생성 코드가 없을 때만 쓰이는 weak `gb_dispatch`
- 생성 코드에서 폴백이 일어나는 곳은 다음과 같다.
  1. `<prefix>.c`의 트램펄린 `gb_dispatch()`: 컴파일되지 않은 페이지(`GB_DISPATCH_FALLBACK_PAGE_NOT_COMPILED`), 쓰기 가능 HRAM(`GB_DISPATCH_FALLBACK_WRITABLE_HRAM`)
  2. `*_dispatch_chunk_N.c`: **모든 디코드된 명령 주소마다** `switch(addr)` → `switch(bank)`가 있고, 해당 뱅크 변형이 없으면 `default: gbrt_execute_dispatch_fallback(...)`. DQ에서는 `BANK_NOT_COMPILED` 31,183건과 `ADDRESS_NOT_COMPILED` 128건이다. `*_funcs_N.c` 안에는 폴백 호출이 0건이다.
  3. 미해결 `JP HL`과 뱅크를 모르는 `JP/CALL`은 `gbrt_timed_jump(ctx, ctx->hl, …)`처럼 `ctx->pc`만 설정하고 `return`한다. 그러면 트램펄린이 다시 dispatch하고, 거기서 컴파일된 진입점이 없으면 2번 경로로 폴백한다.
  4. WRAM/HRAM에서 실행되는 코드는 `gbrt_try_execute_{hram,highmem,ram}_stub`이 알려진 패턴(OAM DMA 루틴 등)만 네이티브로 처리한다. 나머지는 위 폴백으로 간다(RUNTIME.md: "Writable WRAM/HRAM code executes … through runtime helpers and interpreter fallback").

**strict 모드 존재 여부**: 빌드 시점의 strict 또는 no-interpreter 옵션은 **없다**. 있는 것은 진단 기능뿐이다.
- 생성 실행 파일의 `--differential N --differential-fail-on-fallback`: 차등 비교 모드에서 폴백을 불일치로 처리한다.
- `--report-interpreter-hotspots`: 폴백 사이트 목록을 출력한다.
- 정적으로는 `*_metadata.json`의 `relationship: potential_dispatch_fallback` 진단 543건이 있다.

**차단 방법(설계)**: 폴백이 사실상 함수 하나로 모이므로 strict 빌드는 간단하다.
1. `gbrt_execute_dispatch_fallback`을 `(bank, addr, reason)`을 기록한 뒤 정지하는 trap 구현으로 교체한다.
2. HALT bug 경로를 네이티브로 처리한다.
3. `interpreter.c`를 링크하지 않는다.

생성 코드는 수정할 필요가 없다. 링크 시점에 대체하면 된다.

**폴백을 제거했을 때 실제 동작(실측 기반 추론)**: DQ를 10,800프레임 연타로 돌리면 frame 1,018에 첫 폴백이 나온다. strict 빌드라면 **여기서 정지**한다. 이후에도 `bank_not_compiled` 사이트 4곳이 더 나오고, 그중 2곳은 매 프레임 실행된다. 정적 분석만으로는 DQ를 폴백 없이 돌릴 수 없다는 뜻이다. 해결하려면 `--add-entry-point b:a`, `--annotations`, `--use-trace`(런타임 트레이스로 진입점 추가)를 써야 한다. 계획서 §11.3의 emucap `(bank, pc)` 트레이스를 이 입력으로 쓰는 경로가 바로 쓸 수 있는 해법이다.

### Q4. 뱅크 전환 / 동적 분기 메타데이터를 외부 도구로 추출할 수 있는가? → **예. 일부는 JSON으로 이미 나오고, 나머지는 라이브러리 API로 얻는다**

- **이미 나오는 것**: `<prefix>_metadata.json`(`schema: gbrecomp.metadata`, `schema_version: 2`)
  - `rom.sha256`
  - `functions[]`: id, bank, address, emitted_name, kind, provenance, memory_space
  - `labels[]`: block_label
  - `analysis_diagnostics[]`: kind, bank, address, status, evidence, `suggested_annotation`, relationship, related_*
  - `builtin_address_constants[]`
  - 미해결 간접 점프(246), 뱅크 미확정 직접 분기(297), 추정 진입점(566), 데이터 오인 후보는 여기서 바로 뽑을 수 있다. `suggested_annotation`이 `--annotations` 형식으로 나오므로 되먹임 루프를 만들기 쉽다.
- **나오지 않는 것**: 뱅크 전환 지점 목록, 교차 뱅크 호출 엣지, CFG 엣지(블록 후속), 해결된 computed jump 타깃. 이 정보는 `AnalysisResult`(`recompiler/include/recompiler/analyzer.h`)의 공개 필드 `bank_tracker.switches()`(addr, target_bank, is_dynamic), `bank_tracker.calls()`(from/to, from_bank/to_bank), `blocks`, `computed_jump_targets`, `call_targets`에 있다.
- **추가 난이도**: 낮다. 위 Q1 프로브처럼 `libgbrecomp_core.a`에 링크하는 별도 도구에서 이 필드를 JSON으로 내보내면 된다(약 50–100줄). upstream fork는 필요 없다. IR(`recompiler/include/recompiler/ir/ir.h`, `IRBuilder`)도 공개 헤더라 자체 코드 생성기의 입력으로 쓸 수 있다.
- 참고: `--progress-json`은 진행 이벤트만 담는 JSONL이라 분석 메타데이터가 아니다.

### Q5. GBA 예산 (devkitARM 크로스 컴파일 실측)

**명령** (`build/spike`에서 실행. 플래그는 지시대로 쓰고, `-fdata-sections`와 `-std=gnu11`을 추가했다):

```bash
docker run --rm -v "$PWD":/src -w /src devkitpro/devkitarm sh -c '
  ls dq12/*.c | grep -v _main.c | xargs -P 10 -I{} sh -c "$DEVKITARM/bin/arm-none-eabi-gcc \
     -mthumb -mcpu=arm7tdmi -Os -ffunction-sections -fdata-sections -std=gnu11 \
     -Idq12 -Idq12/runtime/include -c {} -o gba/dq12/\$(basename {} .c).o"'
arm-none-eabi-size -A gba/dq12/*.o   # 섹션별 합산은 build/spike/probe/sumsize.py
```

- **스텁한 것: 없다.** `<prefix>_main.c`(SDL 사용) 하나만 컴파일에서 뺐다. 나머지 246개 파일(`_funcs_` 189, `_dispatch_chunk_` 55, `<prefix>.c`, `_rom.c`)은 **오류 0, 경고 0**으로 컴파일됐다. 10개 병렬로 80 s 걸렸다. 그래서 일부만 컴파일해 외삽할 필요 없이 **전체를 실측**했다.
- 런타임 코어 12개 파일(`platform_sdl.cpp`와 ImGui 제외)도 스텁 없이 오류 0으로 크로스 컴파일됐다.

**섹션 크기 (compile-only, 링크 전)**:

| 대상 | .text | .rodata | .data | .bss |
|---|---:|---:|---:|---:|
| DQ 생성 코드 전체 | **39,316,384 B (37.50 MiB)** | 2,097,479 B (2.00 MiB) | 0 | 0 |
| └ `_funcs_*` | 30,966,382 B (29.53 MiB) | 0 | 0 | 0 |
| └ `_dispatch_chunk_*` | 8,348,360 B (7.96 MiB) | 0 | 0 | 0 |
| └ `_rom.c` (`rom_data`) | 0 | 2,097,156 B | 0 | 0 |
| tiny 생성 코드 전체 | 28,776 B | 33,095 B | 0 | 0 |
| 런타임 코어 (SDL/ImGui 제외) | 60,772 B | 11,467 B | 5 B | 8,928 B |

- 명령당 Thumb 코드량: DQ 기준 GB 명령 1개당 약 **61 B**(함수 본문 48 B + dispatch 13 B)다.
- switch 점프 테이블은 Thumb에서 `__gnu_thumb1_case_*` 방식이라 `.text` 안에 인라인으로 들어간다. 그래서 `.rodata`는 사실상 ROM 임베드뿐이다.

**링크 프로브** (`-specs=gba.specs -Wl,--gc-sections`, 생성 코드, 런타임 코어, newlib, SDL 대신 쓰는 최소 `main`과 `g_joypad_*` 정의 `build/spike/gba/linkmain_*.c`):

| 대상 | 결과 |
|---|---|
| tiny | **링크 성공**. ELF `.text` 117,336 B / `.rodata` 40,200 B / `.data` 6,460 B / `.bss` 8,936 B (런타임과 newlib stdio 포함) |
| DQ | **링크 실패**. `region 'rom' overflowed by 9,073,628 bytes`: gc-sections 뒤에도 약 **40.65 MiB**가 필요하다(32 MiB의 127%). 또 `relocation truncated to fit: R_ARM_THM_CALL`이 발생한다. Thumb `BL`의 ±4 MiB 범위를 넘기 때문이고, `-mlong-calls`나 veneer가 필요해 코드가 더 커진다. |

GBA 한계와 비교:

| 자원 | GBA 한계 | DQ 측정/추정 | 판단 |
|---|---:|---:|---|
| ROM (코드+데이터) | 32 MiB | 생성 코드 37.50 MiB + ROM 2.00 MiB + 런타임 약 0.07 MiB. 링크 시 32 MiB를 9.07 MB 초과 | **초과** |
| EWRAM | 256 KiB | 런타임 그대로일 때 아래 표 참고. ROM 복사 때문에 불가능 | **초과** |
| IWRAM | 32 KiB | 생성 코드는 `.data/.bss`가 0. 런타임 `.bss`는 8.9 KiB(대부분 `audio.c` 디버그 버퍼). 스택과 핫 코드 배치는 아직 정하지 않았다 | 여유 있음(배치 미정) |

**런타임 RAM 사용(현재 구현 그대로, ARM에서 sizeof 실측)**:

| 항목 | 크기 | 비고 |
|---|---:|---|
| ROM 복사본 | **2,097,152 B** | `gb_context_load_rom()`이 `malloc`과 `memcpy`를 한다(`gbrt.c:1349`). GBA에서는 카트리지 주소를 직접 가리키도록 반드시 고쳐야 한다. |
| `GBPPU` | **161,512 B** | 프레임버퍼를 u8, u16, u32 세 형식으로 들고 있다. GBA에서는 하드웨어 BG/OBJ 매핑으로 대체해야 한다. |
| `GBContext` | 14,040 B | 진단용 배열 포함(`dispatch_fallback_sites[256]` 등) |
| WRAM / VRAM / ERAM | 32,768 / 16,384 / 8,192 B | CGB 8뱅크, 2뱅크, DQ 8 KB |
| OAM+HRAM+IO | 416 B | |
| 런타임 `.bss` | 8,928 B | |
| 합계(ROM 복사 제외) | 242,240 B | EWRAM의 **92%** |
| 합계(ROM 복사와 GBPPU 제외) | 80,728 B | EWRAM의 31% |

`GBAudio`(calloc) 크기는 측정하지 않았다(**미검증**, 작을 것으로 추정).

**코드 크기 요인 분해 실험** (참고용 추정치. 의미를 깨는 변형이므로 실행용이 아니다): DQ `_funcs_` 파일 3개에서 명령마다 붙는 `if (ctx->single_step_mode) return;`, `if (gbrt_generated_safepoint(ctx)) return;`, `gb_tick(ctx, N);`를 sed로 지우고 다시 컴파일했다.

| 파일 | 원본 | single_step 제거 | +safepoint 제거 | +gb_tick 제거 |
|---|---:|---:|---:|---:|
| funcs_0 | 163,320 | 142,740 | 121,710 | 98,810 (60.5%) |
| funcs_91 | 710,748 | 557,444 | 402,476 | 280,044 (39.4%) |
| funcs_150 | 210,544 | 167,840 | 125,452 | 82,100 (39.0%) |

함수 코드의 약 40–60%가 명령 단위 계측 코드다. 계측을 모두 빼면 funcs는 약 11.6–17.9 MiB가 되고, dispatch 8 MiB와 ROM 2 MiB를 더하면 약 22–28 MiB다. 32 MiB 안에 아슬아슬하게 들어가지만 타이밍 의미가 사라지므로 대체 타이밍 모델이 필요하다. 데이터 오인 코드 제거, 블록 단위 사이클 집계, 간접 타깃만 남기는 sparse dispatch까지 적용해야 의미 있는 여유가 생긴다.

**한 장면/한 함수 예시** (tiny 생성물, 자체 homebrew): GB 명령 하나(`LD L,A`)가 다음처럼 생성된다.

```c
pc_01f0:  /* LD L,A */
    ctx->l = ctx->a;
    ctx->pc = 0x1f1;
    gb_tick(ctx, 4);                               /* extern 호출: 타이머/PPU/APU 진행 */
    if (gbrt_generated_safepoint(ctx)) return;     /* ctx->stopped 검사 */
    if (ctx->single_step_mode) return;
```

**성능**: GBA에서 실행해 보지는 않았다(**미검증**). 다만 GB 명령마다 extern `gb_tick`을 부르고, 그 안에서 64비트 카운터를 갱신하고 PPU/APU/타이머를 사이클 단위로 진행시킨다. 코드는 ROM 웨이트스테이트가 있는 카트리지 버스에서 실행된다. 이 조건을 보면 ARM7TDMI 16.78 MHz에서 GB CPU 실시간 속도를 내기는 어려울 것으로 추정한다. 같은 실행 파일이 호스트(Apple Silicon)에서는 10,800프레임을 약 3 s에 처리했다.

### Q6. 라이선스 제약

- 재사용 후보인 `recompiler/`(분석기, IR, codegen)와 `runtime/`: **MIT**(arcanite24). 저작권 고지와 라이선스 문구만 유지하면 수정, 재배포, 상업적 사용이 모두 가능하다.
- `runtime/vendor/imgui/`: MIT(Omar Cornut). `imstb_*`는 stb(public domain/MIT 이중 라이선스)다. GBA 경로에서는 쓰지 않으므로 재사용 대상이 아니다.
- SDL2(zlib): 호스트 빌드 전용이므로 GBA 산출물과 무관하다.
- `ports/pokemon-crystal/`: 특정 상용 게임 전용 자료라 재사용 대상이 아니다.
- **산출물 제약(중요)**: 생성 결과물은 ROM 전체를 `rom_data[]`로 임베드하고, 원본 코드를 기계적으로 번역한 것이다. 따라서 원 ROM의 저작권(DQ1&2: Enix/Square Enix 측)을 그대로 따른다. 변환기와 런타임은 배포할 수 있지만, **상용 ROM에서 나온 생성 C, ELF, GBA, metadata.json은 배포할 수 없다.** 저장소에도 넣지 않는다(현재 `.gitignore`의 `build/` 정책과 일치).
- 재사용할 때 할 일: upstream 커밋 `9150f87`을 고정하고, 원본 LICENSE를 그대로 들고 오고, NOTICE에 출처를 적는다.

## 6. 판정과 다음 단계

### 판정: **Conditional**

- 재사용(Go 수준): `gbrecomp_core`의 ROM 로더, 디코더, 분석기(뱅크 추적, 진단, annotation/trace 입력), IR 빌더. SDL 없이 라이브러리로 링크된다는 것을 실측했고, MIT이며, 진단 체계가 strict 모드에 바로 쓸 만하다.
- 재사용하지 않음(No-Go 수준): `c_emitter`의 생성 C와 `runtime/`의 PC 런타임. 근거는 다음과 같다.
  1. 생성 코드만 37.50 MiB이고, 링크하면 32 MiB를 9.07 MB 넘는다.
  2. 명령 단위 계측(`gb_tick` 605,611회, safepoint 711,592회, single_step 751,846회)이 코드의 40–60%를 차지한다.
  3. 모든 명령 주소에 dispatch case가 생기고, 그것만 7.96 MiB다.
  4. 런타임이 ROM을 2 MiB 복사하고 PPU에 161 KB를 쓴다.
  5. `GBContext` 필드에 직접 결합되어 있다.
- No-Go(자체 분석기 작성)까지 갈 필요는 없다. 분석기 출력이 유용하고, 미해결 경로 진단, annotation, trace 입력 같은 strict 모드에 필요한 장치가 이미 있기 때문이다.

### Phase 1 권장 작업

1. `gbforge analyze`를 `libgbrecomp_core.a` 링크(커밋 고정, 처음에는 fork 없이)로 구현한다. 우리 JSON 스키마로 functions, blocks와 엣지, `bank_tracker.switches()/calls()`, `computed_jump_targets`, `analysis_diagnostics`를 내보낸다. 같은 ROM이면 같은 JSON이 나오는지(Gate P1) 이 도구에서 확인한다.
2. emucap 트레이스(`(bank, pc)`)를 `--use-trace`나 annotation으로 되먹인다. 목표는 DQ 연타 시나리오의 폴백 사이트 5곳을 0으로 만들고, `data_as_code_candidate` 7,685건과 `undefined_instruction` 2,756건을 줄여 실제 코드 범위를 확정하는 것이다.
3. CLI 출력의 16진수 카운트 버그 때문에 숫자를 잘못 읽지 않도록 metadata.json 기준으로만 집계한다.

### Phase 2 권장 작업

1. `ir.h`의 IR을 입력으로 받는 GBA 전용 코드 생성기를 작성한다. 요구 사항은 다음과 같다.
   - 블록 단위 사이클 집계
   - 명령 단위 safepoint와 single-step 제거
   - 간접 점프 타깃만 담는 sparse dispatch 테이블
   - 레지스터를 `GBContext` 필드가 아닌 로컬 변수에 캐시
   - ROM은 GBA 카트리지 주소에서 직접 읽기
2. strict 모드: 폴백 대신 trap(`bank:addr:reason` 기록 후 정지)을 넣고, `interpreter.c`는 링크하지 않는다.
3. tiny.gb로 먼저 Thumb 바이트/GB 명령 지표를 잰다. 현재 기준은 61 B/명령이고 목표는 그 1/3 이하다(제안값). 그다음 DQ 일부 뱅크로 확대한다.
4. PPU, APU, 타이머는 GB 레지스터 쓰기를 GBA 하드웨어(BG/OBJ, PSG)로 매핑하는 GBA 전용 런타임으로 새로 작성한다. `ppu.c`는 정확도 참조로만 쓴다.

## 7. 미해결 / 미검증 항목

- **미검증**: 외부 Blargg/Mooneye ROM을 받은 뒤 `accuracy_runner_exit_policy`가 통과하는지
- **미검증**: GBA 실기나 에뮬레이터에서의 성능. 이번에는 링크 단계까지만 확인했고 실행하지 않았다.
- **미검증**: DQ의 실제 코드 바이트 범위. 분석기 결과(642,056명령)에는 데이터 오인이 상당량 섞여 있다.
- **미검증**: DQ 연타 실행이 도달한 장면. 화면을 캡처하지 않았다. 폴백 5곳은 그 경로 안에서만 관측한 값이다.
- **미검증**: `GBAudio` 구조체 크기. `-mlong-calls`를 적용했을 때의 최종 크기.
- 확인된 upstream 결함: (1) DQ 분석 후 CLI 카운트가 16진수로 출력된다(`std::hex` 미복원). (2) "Generated files" 목록에 일부 파일만 표시된다. (3) `--reachable-only`가 DQ 함수 수를 거의 줄이지 못한다(69,878 → 69,810).
- DQ PC 빌드의 배터리 세이브가 `build/spike/run-saves/`에 생성됐다(gitignore 대상).

## 부록: 재현 자료 위치 (모두 gitignore 대상)

| 경로 | 내용 |
|---|---|
| `~/dev-external/gb-recompiled/{build,build-nosdl,build-tests}` | 호스트 빌드 3종 |
| `build/spike/tiny`, `build/spike/dq12` | 생성 프로젝트(+ `dq12/build/dq12_jp` PC 실행 파일) |
| `build/spike/gba/{tiny,dq12,runtime}/*.o` | devkitARM 오브젝트 |
| `build/spike/gba/*.sizeA.txt`, `*.undef*.txt`, `dq12.callsites.txt` | size/nm 원자료 |
| `build/spike/gba/{tiny,dq12}.map`, `tiny.elf` | 링크 프로브 결과 |
| `build/spike/gba/exp/` | 코드 크기 요인 분해 실험 |
| `build/spike/probe/` | `probe.cpp`(라이브러리 프로브), `probe.c`(sizeof), `linkmain.c`, `sumsize.py`, 각 단계 로그 |
