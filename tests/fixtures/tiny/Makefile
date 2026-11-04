tiny.gb: tiny.asm
	rgbasm -o tiny.o tiny.asm
	rgblink -n tiny.sym -o $@ tiny.o
	rgbfix -v -p 0xFF -t TINY $@
	rm -f tiny.o
