; tiny.gb — gbforge's own test ROM (MIT). Smallest "game loop":
; VBlank IRQ counts frames, the D-pad moves a solid tile, A inverts the palette.
; Exercises LD, INC/DEC, ADD/SUB/CP, AND/OR/XOR/CPL/SWAP, JR/JP/CALL/RET/RETI, HALT, EI/DI.

DEF rP1   EQU $FF00
DEF rIF   EQU $FF0F
DEF rLCDC EQU $FF40
DEF rLY   EQU $FF44
DEF rBGP  EQU $FF47
DEF rIE   EQU $FFFF

DEF MAP0  EQU $9800
DEF TILE  EQU 1               ; tile 1 = solid color 3

SECTION "VBlank", ROM0[$40]
    jp VBlankHandler

SECTION "Header", ROM0[$100]
    nop
    jp Start
    ds $150 - @, 0            ; rgbfix fills logo/title/checksums

SECTION "Main", ROM0[$150]
Start:
    di
    ld sp, $FFFE
.waitVBlank:
    ldh a, [rLY]
    cp 144
    jr c, .waitVBlank
    xor a
    ldh [rLCDC], a            ; LCD off — VRAM is free

    ; tile 0 = blank, tile 1 = solid (16 bytes each)
    ld hl, $8000
    ld bc, 16
    xor a
    call Fill
    ld bc, 16
    ld a, $FF
    call Fill

    ; clear 32x32 BG map
    ld hl, MAP0
    ld bc, 32 * 32
    xor a
    call Fill

    ; WRAM state
    xor a
    ld hl, wFrames
    ld bc, wEnd - wFrames
    call Fill
    ld a, 10
    ld [wPosX], a
    ld a, 9
    ld [wPosY], a
    call DrawCursor

    ld a, %11100100
    ldh [rBGP], a
    ld a, %10010001           ; LCD on, BG on, tiles $8000, map $9800
    ldh [rLCDC], a
    ld a, 1
    ldh [rIE], a              ; VBlank only
    xor a
    ldh [rIF], a
    ei

MainLoop:
    halt
    nop
    call ReadJoypad           ; a = held, b = newly pressed
    ld a, b
    and $01                   ; A pressed → invert palette
    jr z, .noA
    ldh a, [rBGP]
    cpl
    ldh [rBGP], a
.noA:
    ld a, b
    and $F0
    jr z, MainLoop            ; no D-pad edge

    push bc
    call CursorAddr
    xor a
    ld [hl], a                ; erase old cursor
    pop bc

    ld hl, wPosX
    bit 4, b                  ; right
    jr z, .notRight
    ld a, [hl]
    cp 19
    jr nc, .notRight
    inc [hl]
.notRight:
    bit 5, b                  ; left
    jr z, .notLeft
    ld a, [hl]
    and a
    jr z, .notLeft
    dec [hl]
.notLeft:
    ld hl, wPosY
    bit 6, b                  ; up
    jr z, .notUp
    ld a, [hl]
    and a
    jr z, .notUp
    dec [hl]
.notUp:
    bit 7, b                  ; down
    jr z, .notDown
    ld a, [hl]
    cp 17
    jr nc, .notDown
    inc [hl]
.notDown:
    call DrawCursor
    jp MainLoop

; hl = MAP0 + y*32 + x
CursorAddr:
    ld a, [wPosY]
    ld l, a
    ld h, 0
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld a, [wPosX]
    ld e, a
    ld d, 0
    add hl, de
    ld de, MAP0
    add hl, de
    ret

DrawCursor:
    call CursorAddr
    ld [hl], TILE
    ret

; a = held keys (hi nibble: down/up/left/right, lo: start/select/b/a), b = new presses
ReadJoypad:
    ld a, $20                 ; select D-pad
    ldh [rP1], a
    ldh a, [rP1]
    ldh a, [rP1]
    cpl
    and $0F
    swap a
    ld b, a
    ld a, $10                 ; select buttons
    ldh [rP1], a
    ldh a, [rP1]
    ldh a, [rP1]
    ldh a, [rP1]
    ldh a, [rP1]
    cpl
    and $0F
    or b
    ld c, a                   ; c = held
    ld a, $30
    ldh [rP1], a
    ld a, [wJoyHeld]
    cpl
    and c
    ld b, a                   ; b = held & ~previous
    ld a, c
    ld [wJoyHeld], a
    ret

; fill bc bytes at hl with a
Fill:
    ld d, a
.loop:
    ld a, d
    ld [hl+], a
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret

VBlankHandler:
    push af
    push hl
    ld hl, wFrames
    inc [hl]
    jr nz, .done
    inc hl
    inc [hl]
.done:
    pop hl
    pop af
    reti

SECTION "WRAM", WRAM0[$C000]
wFrames:   ds 2               ; little-endian VBlank counter
wJoyHeld:  ds 1
wPosX:     ds 1
wPosY:     ds 1
wEnd:
