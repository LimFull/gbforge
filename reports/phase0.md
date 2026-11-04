# Phase 0 보고서 — 환경 검증과 기준선

작성일: 2026-11-04 · 기준 문서: `GB_to_GBA_Native_Port_Project_Plan.md` v0.2.0

## 요약

| Gate P0 항목 | 결과 |
|---|---|
| 재현 가능한 빌드 명령으로 `hello.gba` 생성 | ✅ `scripts/dkarm.sh make -C examples/hello-gba` |
| mGBA에서 실행 확인 | ✅ GDB 스텁으로 3초 실행 후 `g_frames`=139 증가 확인 |
| emucap(Mesen2 GBA)에서 표시·키 입력 확인 | ✅ 화면 텍스트, `g_frames` 123→126(3프레임 입력), `g_keys`=0x0011(A+RIGHT) |
| 환경 의존성 문서화 | ✅ `scripts/setup-check.sh`, `local.env.example`, README Development |

**판정: Gate P0 통과.** gb-recompiled 재사용 판단은 [gb-recompiled-spike.md](gb-recompiled-spike.md) 참조.

## 1. 환경

```text
$ scripts/setup-check.sh
git            git version 2.50.1 (Apple Git-155)
cmake          cmake version 4.4.4
ninja          1.13.2
python3        Python 3.15.2
rgbds          rgbasm v1.0.4
devkitARM      docker: arm-none-eabi-gcc (devkitARM) 16.1.0
mGBA           0.10.5 (26b7884bc25a5933960f3cdcd98bac1ae14d42e2)
emucap         …/emucap/target/release/emucap-mcp
Mesen2         "tag": "2.2.1"
GBA-BIOS       fd2547724b505f48
```

- 호스트: macOS (Apple Silicon). sudo 없이 구성: devkitARM은 **devkitPro Docker 이미지**(`scripts/dkarm.sh`가 `$DEVKITARM` 미설정 시 자동 사용), RGBDS·mGBA는 Homebrew.
- emucap Control MCP는 이 Claude Code 세션에 등록돼 있지 않아 `scripts/mcp_client.py`(stdio MCP 클라이언트, 표준 라이브러리만 사용)로 직접 구동했다. 11.2의 "후속 2안"(verify가 MCP 클라이언트로 동작)의 기반이 이미 생긴 셈.
- emucap 코어는 로컬 소스 빌드(`2bb7074-dirty`), Mesen2는 emucap 1.1.0 네이티브 패키지(MesenCE 2.2.1, host API 5). 버전 조합은 launch_plan이 차단하지 않았다.

## 2. 테스트 ROM — `tests/fixtures/tiny/tiny.gb` (자체 제작, MIT)

공개 homebrew 대신 **직접 만든 32 KiB ROM-only DMG ROM**을 테스트 기준으로 삼았다(라이선스 문제 없음, 동작을 완전히 알고 있음).

- VBlank IRQ로 프레임 카운트(`wFrames` $C000), D-pad로 타일 커서 이동(`wPosX/Y` $C003/$C004), A로 팔레트 반전.
- 사용 명령: LD/LDH, INC/DEC, ADD/SUB/CP, AND/OR/XOR/CPL/SWAP, BIT, JR/JP/CALL/RET/RETI, PUSH/POP, HALT, EI/DI. Phase 2 첫 변환 대상.
- 빌드: `make -C tests/fixtures/tiny` (RGBDS 1.0.4). sha256 `133d8283…7791b`.

emucap 시나리오 `tests/scenarios/tiny-smoke.json` 결과:

| 시점 | wFrames | wJoyHeld | wPosX | wPosY |
|---|---:|---:|---:|---:|
| 부팅 300프레임 후 | 220 | 0 | 10 | 9 |
| →, →, ↓ 입력 후 (12프레임) | 232 | 0 | 12 | 10 |

DMG 부트 ROM(로고 스크롤) 이후 실행되므로 첫 관찰은 300프레임 이후로 잡았다. A 입력 후 팔레트 반전 화면도 확인.

## 3. `examples/hello-gba`

- libtonc(TTE) 기반 텍스트 출력 + `key_poll` + `VBlankIntrWait` 카운터. 검증용 전역 `g_frames`(0x03000144), `g_keys`(0x03000148) — 주소는 `hello.map`에서 확인.
- Mesen2 GBA는 실제 BIOS 인트로가 돌기 때문에 첫 관찰을 400프레임 이후로 잡았다(120프레임에서는 BIOS 로고 화면).
- 첫 빌드에서 숫자 줄이 안 나온 문제: libtonc의 `tte_printf`는 `iprintf` 매크로라 `tte_init_con()`이 필요했다. 수정 후 정상.

## 4. ROM Inspect (Phase 1 선행)

`tools/gb2gba.py inspect ROM [--json]` — 헤더(제목, CGB/SGB, 카트리지 타입·매퍼, ROM/RAM 크기), SHA-256/SHA-1, 헤더/글로벌 체크섬, 부트 로고(해시 비교) 검사. 종료 코드 `2` = 유효하지 않은 ROM.

정책: 헤더 체크섬 불일치·파일이 선언 크기보다 작음·미지의 코드 → **오류(2)**. 글로벌 체크섬 불일치·파일이 더 큼·로고 불일치 → **경고**.

테스트(`python3 -m unittest discover -s tests`): golden JSON 일치 + 2회 실행 출력 동일(Gate P1 결정성), 헤더 체크섬 오류, 뱅크 누락 오류, 글로벌 체크섬 경고, 종료 코드 2 — **5/5 통과**.

## 5. 대상 ROM 확정 (16장 질문 1 해결)

| 항목 | 값 |
|---|---|
| 파일 | `Dragon Quest I & II (Japan) (SGB Enhanced) (GB Compatible).gbc` (No-Intro 명명, zip 내 단일 파일) |
| SHA-256 | `7cae9c90cf50e5b014f700090626e686aaf5978105901b0346410b0a89438caf` |
| 제목 / 제조 코드 | `DQ1&2` / `AEDJ`, licensee `B4`, version 0, 일본판 |
| 모드 | CGB 호환(0x80), SGB 지원 |
| 카트리지 | **MBC5+RAM+BATTERY** (0x1B) |
| ROM / RAM | 2 MiB (128 뱅크) / 8 KiB |
| 체크섬 | 헤더·글로벌 모두 일치, 로고 정상 |

- 다운로드 폴더의 다른 파일 `Dragon Quest I + II.gbc`(sha256 `538d0363…85d6`)는 헤더는 같지만 **글로벌 체크섬 불일치** → 수정(패치)된 ROM으로 판단, 사용하지 않는다.
- 결론: Phase 3의 매퍼 우선순위는 **MBC5**, CGB 모드 필수, 세이브는 8 KiB 배터리 SRAM.
- emucap 기준선(`tests/scenarios/dq12-title.json`): Mesen2가 `gbc` 시스템으로 부팅, 600프레임에 인트로 크레딧, Start 후 타이틀 화면. 스크린샷·덤프는 `build/`에만 보관(커밋 안 함).

## 6. gb-recompiled 스파이크

상세: [gb-recompiled-spike.md](gb-recompiled-spike.md) · 대상 `arcanite24/gb-recompiled@9150f87` (MIT)

**판정: Conditional** — 분석기·IR(`libgbrecomp_core.a`)은 재사용, 생성 C와 PC 런타임은 GBA용으로 새로 작성.

| 질문 | 결과 |
|---|---|
| Q1 SDL2 분리 | ✅ SDL 없이 빌드, 외부 프로그램에서 라이브러리로 DQ 분석 성공 |
| Q2 생성 C 의존성 | `stdint/stdbool/stddef` + 런타임 심볼 ~75개, `GBContext` 필드 직접 접근 |
| Q3 인터프리터 폴백 | strict 모드 없음. 단 폴백이 `gbrt_execute_dispatch_fallback` 한 곳으로 모여 trap 교체 가능. DQ 연타 10,800프레임에서 폴백 사이트 5곳(첫 발생 frame 1,018) |
| Q4 메타데이터 | `metadata.json`에 함수·진단 11,550건. 뱅크 전환·교차 뱅크 호출·CFG 엣지는 라이브러리 API로 추출(50–100줄) |
| Q5 GBA 예산 | DQ 생성 코드 Thumb `.text` **37.50 MiB** → GBA 링크 시 32 MiB를 9.07 MB 초과. 명령당 계측(`gb_tick` 60만 곳 등)이 40–60%. 런타임은 ROM 2 MiB를 `malloc` 복사 |
| Q6 라이선스 | MIT, 재사용 가능. 상용 ROM 생성물은 ROM 전체를 포함하므로 배포·커밋 금지 |

호스트 빌드·테스트: ctest 84/85 통과(실패 1건은 외부 Blargg/Mooneye ROM 부재로 인한 환경 의존 테스트).

## 7. CI

`.github/workflows/ci.yml`
- `host-tests`: Ubuntu + Python 3.12, `unittest`.
- `gba-smoke`: `devkitpro/devkitarm` 컨테이너에서 `hello.gba` 빌드, 아티팩트 업로드.
- emucap/Mesen2·mGBA 실행 검증은 GUI 에뮬레이터가 필요해 CI에 넣지 않았다(로컬 `scripts/emucap_run.py`). 무인 실행이 필요해지면 self-hosted macOS 러너를 검토.

## 8. 남은 문제

- 실기(GBA/EZ-Flash) 검증은 미실시 — 16장 질문 3(목표 실행 환경) 답변 필요.
- `hello.gba`의 검증 주소는 `hello.map`에서 수동으로 옮겼다. Phase 2에서 `gb2gba.map.json`이 생기면 시나리오가 주소를 직접 참조하도록 바꾼다.
- 계획 문서의 권장 디렉터리 구조 중 아직 내용이 없는 폴더(runtime-gba, plugins 등)는 만들지 않았다. 해당 Phase에서 생성.
- `CMakeLists.txt`는 아직 C/C++ 호스트 코드가 없어 만들지 않았다(스파이크 판정 후 결정).

## 9. 다음 작업 (Phase 1)

1. `gb2gba analyze`: `libgbrecomp_core.a`(커밋 고정)에 링크하는 C++ 호스트 도구로 functions·blocks·엣지·뱅크 전환·교차 뱅크 호출·미해결 경로를 우리 JSON 스키마로 출력. 반복 실행 동일 출력(Gate P1).
2. emucap `(bank, pc)` 트레이스를 entry point로 되먹여 DQ 폴백 사이트와 데이터 오인 코드를 줄인다(관측은 *코드 존재* 근거로만 쓰고, 미해결 간접 분기의 해결 근거로는 쓰지 않는다).
3. Phase 2 PoC: IR → GBA 전용 C 생성기(블록 단위 사이클, sparse dispatch, ROM 직접 읽기, strict trap) + 최소 GBA 런타임으로 `tiny.gb`를 strict native 실행.
