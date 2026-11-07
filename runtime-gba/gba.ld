/* gba.ld — GBA cartridge layout for crt0.s. Section names follow libtonc's IWRAM_DATA, EWRAM_BSS etc. */
OUTPUT_ARCH(arm)
ENTRY(_start)

MEMORY {
    rom   : ORIGIN = 0x08000000, LENGTH = 32M
    iwram : ORIGIN = 0x03000000, LENGTH = 32K
    ewram : ORIGIN = 0x02000000, LENGTH = 256K
}

SECTIONS {
    .crt0   : { KEEP(*(.crt0)) } >rom
    .text   : { *(.text .text.* .glue_7 .glue_7t .vfp11_veneer .v4_bx) } >rom
    .rodata : { *(.rodata .rodata.*) . = ALIGN(4); } >rom
    /DISCARD/ : { *(.ARM.exidx* .ARM.extab*) }  /* C only: no unwinding (newlib's setjmp would pull in libgcc's unwinder) */

    .iwram : {                          /* code and initialised data, copied from ROM */
        __iwram_start__ = .;
        *(.iwram*)
        *(.data .data.*)
        . = ALIGN(4);
        __iwram_end__ = .;
    } >iwram AT>rom
    __iwram_lma = LOADADDR(.iwram);
    .bss (NOLOAD) : {
        __bss_start__ = .;
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
        __bss_end__ = .;
    } >iwram

    .ewram : {
        __ewram_start__ = .;
        *(.ewram*)
        . = ALIGN(4);
        __ewram_end__ = .;
    } >ewram AT>rom
    __ewram_lma = LOADADDR(.ewram);
    .sbss (NOLOAD) : {                  /* EWRAM_BSS */
        __sbss_start__ = .;
        *(.sbss .sbss.*)
        . = ALIGN(4);
        __sbss_end__ = .;
        __end__ = .;
    } >ewram
}
