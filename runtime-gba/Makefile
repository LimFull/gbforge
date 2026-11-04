# Link generated code + runtime + embedded GB ROM into a .gba. Driven by `gb2gba port`.
#   make -C runtime-gba GEN=/abs/gen OUT=/abs/out/game.gba TITLE=GAME
BIN    := $(DEVKITARM)/bin/arm-none-eabi-
TONC   := $(DEVKITPRO)/libtonc
RT     := $(abspath .)
OBJDIR := $(OUT).obj
CFLAGS := -mthumb -mcpu=arm7tdmi -O2 -Wall -Wno-unused-label -ffunction-sections -I$(RT) -I$(TONC)/include
OBJS   := $(patsubst %.c,$(OBJDIR)/%.o,$(notdir $(RT)/gb.c $(wildcard $(GEN)/*.c))) $(OBJDIR)/rom.o

vpath %.c $(RT) $(GEN)

$(OUT): $(OBJS)
	$(BIN)gcc -mthumb -specs=gba.specs $^ -L$(TONC)/lib -ltonc -Wl,--gc-sections -Wl,-Map,$(OUT:.gba=.map) -o $(OUT:.gba=.elf)
	$(BIN)objcopy -O binary $(OUT:.gba=.elf) $@
	$(DEVKITPRO)/tools/bin/gbafix -t$(TITLE) -cGBFG -mGF $@
	$(BIN)nm $(OUT:.gba=.elf) > $(OUT:.gba=.sym)

$(OBJDIR)/%.o: %.c $(RT)/gb.h | $(OBJDIR)
	$(BIN)gcc $(CFLAGS) -c $< -o $@

$(OBJDIR)/rom.o: $(GEN)/rom.s | $(OBJDIR)
	cd $(GEN) && $(BIN)as $< -o $@

$(OBJDIR):
	mkdir -p $@
