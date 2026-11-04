BIN    := $(DEVKITARM)/bin/arm-none-eabi-
TONC   := $(DEVKITPRO)/libtonc
CFLAGS := -mthumb -mcpu=arm7tdmi -O2 -Wall -I$(TONC)/include

hello.gba: main.c
	$(BIN)gcc $(CFLAGS) -specs=gba.specs $< -L$(TONC)/lib -ltonc -Wl,-Map,hello.map -o hello.elf
	$(BIN)objcopy -O binary hello.elf $@
	$(DEVKITPRO)/tools/bin/gbafix -tHELLO -cGBFG -mGF $@
