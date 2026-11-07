-- Convert ROM.app (macOS): drop .gb, .gbc or .zip ROMs on the icon, or open it and choose them.
-- GAME.gba is written next to each ROM by the bundled tool (gbforge convert).
on run
	convertRoms(choose file with prompt "Choose Game Boy ROMs (.gb, .gbc or .zip) to convert:" with multiple selections allowed)
end run

on open roms
	convertRoms(roms)
end open

on convertRoms(roms)
	set tool to POSIX path of (path to me) & "Contents/Resources/gbforge/gbforge"
	set cmd to quoted form of tool & " convert"
	repeat with r in roms
		set cmd to cmd & " " & quoted form of POSIX path of r
	end repeat
	set progress total steps to -1
	set progress description to "Converting… this can take a minute."
	set out to do shell script cmd & " 2>&1; true"
	set answer to button returned of (display dialog out buttons {"Show in Finder", "OK"} default button "OK" with title "gbforge")
	if answer is "Show in Finder" then do shell script "open -R " & quoted form of POSIX path of (item 1 of roms)
end convertRoms
