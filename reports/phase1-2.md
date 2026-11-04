# Phase 1–2 보고서 — analyze, strict native PoC

작성일: 2026-11-04 · 이전: [phase0.md](phase0.md), [gb-recompiled-spike.md](gb-recompiled-spike.md)

## 요약

| 게이트 | 결과 |
|---|---|
| **P1** 같은 ROM 반복 분석 → 같은 JSON, 명확한 종료 코드 | ✅ tiny·DQ 모두 2회 실행 바이트 동일, 무효 ROM은 2 |
| **P2** 홈브류 ROM이 GBA ARM/Thumb 코드로 실행, 화면·키 입력 정상, strict에서 누락 경로는 오류 | ✅ `tiny.gb` → strict native `.gba`, emucap 차분 검증 6/6 일치. DQ는 strict 게이트가 미해결 엣지로 차단(종료 코드 3) |

## 1. 작업 내용

### 1.1 `gbforge-core` (C++, `tools/core/`)
- `libgbrecomp_core`(gb-recompiled `9150f87`, MIT)를 CMake FetchContent로 고정해 링크. SDL·C emitter·PC 런타임은 쓰지 않는다.
- `analyze ROM OUT.json`: functions, blocks(+후속 엣지, reachable), bank_switches, cross_bank_calls, computed_jump_targets, diagnostics를 `gbforge.analysis` v1 스키마로 출력.
- `codegen ROM OUTDIR`: 분석 결과(디코드된 명령)에서 직접 C 생성. gb-recompiled IR은 쓰지 않았다(명령 단위 생성이 더 단순하고, IR 최적화가 필요해지면 그때 도입).

### 1.2 코드 생성 모델 (`runtime-gba/gb.h`가 ABI)
- GB 함수 1개 = C 함수 `void f_BB_AAAA(uint16_t at)`. 블록 내 분기는 `goto`, 함수 간 분기는 tail call, `at`으로 보조 진입점(호출 복귀 지점, 함수 중간 분기 대상, safepoint 재개 지점) 선택.
- **복귀 프로토콜**: RET은 꺼낸 주소를 `gb.ret`에 넣고 C 함수를 빠져나간다. 호출자는 `gb.ret`이 자기 복귀 지점일 때만 이어서 실행하고, 아니면 계속 빠져나가며, 최상위에서 `gb_dispatch(gb.ret)`. RST 점프 테이블·`pop`으로 프레임 건너뛰기·`push; ret` 점프를 인터프리터 없이 처리한다.
- **죽은 프레임 되감기**: 호출마다 `frame_sp`(복귀 주소를 push한 직후의 GB SP)를 기록하고, 함수 간 JP 시점에 `gb.sp > frame_sp`이면 그 C 프레임은 이미 소비된 것이므로 최상위로 되감아 디스패치한다. DQ의 `RST $00` 점프 테이블에서 발생한 C 스택 오버플로를 계기로 추가했다.
- **타이밍**: 사이클 카운트 없음(하드웨어 클럭 동기). HALT는 GBA `Halt()`로 대기, 인터럽트는 safepoint(역방향 분기, HALT, EI 이후)에서 처리. LY는 GBA VCOUNT에서 환산, DIV는 GBA TM0(16384 Hz).
- **strict**: 인터프리터 없음. 정적으로 진입점이 없는 대상은 `gb_dispatch`에서 trap(화면 표시 + `gb_trap_info`). `gb2gba port --native-policy strict`는 미해결 엣지(분석 진단 + 코드젠 미해결)가 1건이라도 있으면 빌드를 막는다(종료 코드 3).
- **RST 패딩**: 벡터가 0xFF로 채워진 RST는 데이터를 코드로 디코드한 것이므로 미해결 엣지가 아니라 trap으로 생성한다.

### 1.3 GBA 런타임 (`runtime-gba/gb.c`)
- GB 메모리 맵: ROM은 카트리지 영역에서 직접 읽기(복사 없음), WRAM 32 KiB(CGB 뱅크)·VRAM 섀도는 EWRAM, CPU 상태·HRAM·IO는 IWRAM. 카트 RAM은 GBA SRAM에 1:1 매핑(배터리 세이브).
- MBC: ROM only, MBC5(ROM 뱅크). MBC1/2/3은 trap.
- compat PPU: GB VRAM 쓰기를 즉시 GBA BG0(4bpp 타일 + 32×32 스크린블록 2개)에 반영 → 프레임 단위 복사 없음. SCX/SCY → BG0 스크롤, (40,8) 레터박스는 WIN0. DMG BGP, CGB BG 팔레트 0.
- 조이패드(P1 ↔ KEYINPUT 동일 비트 배치), VBlank IRQ, IE/IF, STAT/LY, OAM DMA, HRAM OAM DMA 루틴 패턴 인식.

### 1.4 CLI (`tools/gb2gba.py`)
- `analyze ROM --out DIR`, `port ROM --output OUT.gba`, `verify ROM PORT.gba --scenario S --out DIR`.
- `port` 산출물: `.gba`, `.map.json`(GB WRAM/HRAM ↔ GBA 주소, CPU 구조체, trap, `BB:AAAA` → ARM 주소), `.report.json`.
- `verify`: emucap으로 원본(Mesen2 GB)과 포트(Mesen2 GBA)를 같은 시나리오로 실행. 메모리 읽기는 `map.json`으로 변환해 정확 일치, 화면은 GBA 쪽 (40,8) 160×144를 팔레트 전단사 일치로 비교. 불일치 시 종료 코드 8.
- 시나리오 정렬: 부팅 시간이 달라(DMG 부트 ROM vs GBA BIOS) 프레임 수가 아니라 `until`(게임 상태 정확 일치)로 맞춘다.

## 2. 실행 명령

```bash
cmake -G Ninja -B build/core . && ninja -C build/core gbforge-core
tools/gb2gba.py analyze tests/fixtures/tiny/tiny.gb --out build/tiny
tools/gb2gba.py port tests/fixtures/tiny/tiny.gb --output out/tiny.gba
set -a; . ./local.env; set +a
tools/gb2gba.py verify tests/fixtures/tiny/tiny.gb out/tiny.gba --scenario tests/scenarios/tiny-diff.json --out build/verify/tiny
python3 -m unittest discover -s tests
```

## 3. 결과

### 3.1 tiny.gb 차분 검증 (`tests/scenarios/tiny-diff.json`)

```text
ok   read:aligned  0a00000a09
ok   read:moved  1600000c0a
ok   read:moved2  1c00000b08
ok   read:inverted  2000000b08
ok   shot:aligned  2 colours
ok   shot:inverted  2 colours
```

원본과 포트의 GB WRAM(wFrames, wJoyHeld, wPosX/Y)이 4개 시점 모두 같고, 화면 2장도 일치한다. 단위 테스트 7/7 통과(inspect 5, verify의 PNG 디코더·화면 비교 2).

### 3.2 분석 결정성 (Gate P1)

| ROM | 함수 | 블록 | 미해결 엣지(분석) | 2회 실행 |
|---|---:|---:|---:|---|
| tiny.gb | 26 | 61 | 0 | 동일 |
| dq12-jp.gbc | 69,878 | 131,971 | 543 | 동일 (20 MB JSON, 1.5 s) |

### 3.3 코드 크기 (스파이크 기준 대비)

| 대상 | 생성 명령 | Thumb `.text` | B/명령 | 비고 |
|---|---:|---:|---:|---|
| tiny (gbforge) | 447 | 10,780 B | 24.1 | gb-recompiled 생성물 71 B/명령 |
| DQ `--no-scan` (gbforge, strict 우회 조사용) | 1,012,923 | 14.95 MB | **14.8** | gb-recompiled 61 B/명령 |
| DQ 링크 결과 | | `.text` 14.07 MB + `.rodata` 3.0 MB | | `.gba` **17.1–18.4 MB** (32 MiB의 54–57%), 장거리 분기 veneer는 링커가 자동 삽입 |

생성 명령 수(101만)가 디코드 수(62만)보다 큰 것은 여러 함수가 공유하는 블록을 함수마다 복제하기 때문이다. 크기 여유가 있어 지금은 그대로 둔다.

### 3.4 DQ strict 게이트

| 구성 | 미해결 엣지 | 비고 |
|---|---:|---|
| 기본(aggressive scan) | 40,662 | RST $38/$28/$20(0xFF 패딩)로 가는 가짜 코드가 2만 건 |
| `--no-scan` | 38,417 | |
| + RST 패딩 trap, 명령 단위 진입점 | **19,571** | 고유 대상 15,610곳: 어떤 함수에서도 디코드되지 않은 주소 |

strict 우회 조사용 빌드는 부팅 직후 크래시한다(ARM PC가 VRAM 영역, GB SP=0x0801, 뱅크 값 비정상). C 스택 오버플로(첫 시도)는 죽은 프레임 되감기로 해결했고, 남은 크래시는 원인 미확인이다.

## 4. 남은 문제

- **DQ 부팅 크래시**: 원본(Mesen2 GB)과 포트를 부팅부터 emucap으로 단계 비교해 첫 분기점을 찾아야 한다. 후보: CGB 전용 레지스터(VBK/HDMA/KEY1) 처리, MBC5 RAM 뱅크, 정적 분석 오판 코드로의 진입.
- **미해결 엣지 1.9만 건**: 정적 분석만으로는 가짜 분기와 놓친 코드를 구별할 수 없다. emucap 실행 트레이스로 실제 실행된 `(bank, pc)`를 진입점으로 되먹이고(`--trace`/`--entry`), 실행되지 않은 가짜 코드는 annotation으로 data 처리한다(계획 11.3).
- **compat PPU 미구현**: 윈도우, 스프라이트(OAM → GBA OBJ), CGB 속성(VRAM 뱅크 1, 팔레트 0~7, 뒤집기), HDMA.
- **타이밍**: EI 지연, STAT/LYC·타이머 인터럽트, 사이클 기반 루프는 근사. 필요해지면 블록 단위 사이클 예산을 넣는다.
- **오디오**: 미구현.
- **라이선스 고지**: GBA 산출물에 링크되는 libtonc·newlib 라이선스 확인 후 NOTICE 작성 필요.
- CI는 emucap/Mesen2를 쓰지 않으므로 `verify`는 로컬에서만 실행된다.

## 5. 다음 작업 (Phase 3)

1. DQ 부팅 크래시 원인 추적(emucap 차분 단계 비교).
2. emucap 트레이스 → 진입점 되먹임 루프로 미해결 엣지 축소.
3. compat PPU: 스프라이트·윈도우·CGB 속성·팔레트.
4. 타이틀 → 필드 → 전투 → 세이브 시나리오를 `verify`로 회귀 세트화.
