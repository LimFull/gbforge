// Phase 0 smoke test: text output, key input, VBlank counter.
#include <tonc.h>

// Read by verification tools via hello.map
volatile u32 g_frames;
volatile u16 g_keys;

int main(void)
{
	irq_init(NULL);
	irq_enable(II_VBLANK);

	REG_DISPCNT = DCNT_MODE0 | DCNT_BG0;
	tte_init_se_default(0, BG_CBB(0) | BG_SBB(31));
	tte_init_con();	// route stdio (tte_printf = iprintf) to TTE
	tte_write("#{P:8,8}hello gbforge");

	while (1) {
		VBlankIntrWait();
		key_poll();
		g_frames++;
		g_keys = key_curr_state();
		tte_printf("#{P:8,24}frame %8u", (unsigned)g_frames);
		tte_printf("#{P:8,40}keys  %04X", (unsigned)g_keys);
	}
}
