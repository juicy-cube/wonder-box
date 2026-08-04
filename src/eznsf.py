#!/usr/bin/env python3
import sys

if sys.version_info[0] < 3:
    print("Python 3 required.")
    sys.exit(1)

#
# EZNSF
#
# Original tool by bradsmith, 2016 - http://rainwarrior.ca
#
# This fork adds support for combining more than one NSF file into a
# single ROM (selectable from an in-ROM menu), permanently pins each
# playing NSF's high PRG bank so DPCM/DMC sample playback is never
# corrupted by the driver's own code, and patches each NSF's own
# in-song dynamic bankswitching (if any) so it keeps working correctly
# once that NSF's data has been relocated to make room for the others.
#
# This script reads an album.txt description file plus one or more NSF
# files, and produces a NES ROM (and, optionally, one .nsfe file per NSF)
# that plays them back with an on-screen menu. Broadly, it:
#   STEP 0  - cleans out any old build output
#   STEP 1  - parses album.txt
#   STEP 2  - parses each NSF's header and works out its PRG bank layout
#   STEP 2b - patches in-song dynamic bankswitching for every NSF after
#             the first one, so it targets the right physical banks
#   STEP 3  - compresses the screen graphics (nametables/CHR/palettes)
#   STEP 4  - generates the assembler include files (enums.sh/tables.sh)
#             that eznsf.s consumes for all of the above
#   STEP 5  - assembles and links the ROM with ca65/ld65
#   STEP 6  - writes out one .nsfe file per NSF
#

import os
import datetime
import shlex
import subprocess

album = "album.txt"       # default album description file, overridable via argv[1]
outdir = "temp"            # default output directory, overridable via argv[2]
ca65 = "tools/ca65.exe"    # path to the ca65 assembler
ld65 = "tools/ld65.exe"    # path to the ld65 linker
output_nsfe = True         # set to False to skip writing .nsfe files (STEP 6)

def errmsg(msg):
    print("Error: " + msg)
    sys.exit(1)

if len(sys.argv) > 1:
    album = sys.argv[1]
if len(sys.argv) > 2:
    outdir = sys.argv[2]
if len(sys.argv) > 3:
    errmsg("Error: Too many arguments on command line.\n" + \
        "Usage: eznsf.py [album] [directory]")

now_string = datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y")

# All of the state parsed out of album.txt lives in these variables.
# nsf_albums holds one dict per "NSF" line, filled in further as we go
# (see STEP 2 below for what else gets added to each entry).
nsf_albums = []             # [{"file": "some.nsf"}, ...] - one per "NSF" line
nsf_nrom = 0                 # NROM 0/1 from album.txt (0 = mapper 31, 1 = plain NROM)
nsf_title = ""                # TITLE line
nsf_artist = ""                # ARTIST line
nsf_copyright = ""               # COPYRIGHT line
nsf_info_text = "INFO"             # menu label for the info screen entry; override with INFOTEXT
nsf_playall_text = "PLAY ALL"        # menu label for the "play everything" entry; override with PLAYALL
nsf_tracks = []  # (track_title, song_num-1, minutes, seconds, album_index), one per TRACK line
nsf_screens = []  # (name, nametable_file, chr0_file, chr1_file, pal0_file, pal1_file), one per SCREEN line
nsf_info = []     # one string per INFO line (the info screen's body text, printed one per row)
nsf_coord = []    # (name, x, y), one per COORD line
nsf_const = []    # (name, value), one per CONST line

# STEP 0: create the output directory (if it doesn't already exist) and
# delete any files left over from a previous build, so stale output can
# never get mixed in with what we're about to generate.

try:
    os.makedirs(outdir)
except OSError:
    if not os.path.isdir(outdir):
        raise

for file in os.listdir(outdir):
    if \
          file.endswith(".bin") \
       or file.endswith(".sh") \
       or file.endswith(".o") \
       or file.endswith(".nes") \
       or file.endswith(".map") \
       or file.endswith(".lab") \
       or file.endswith(".nsfe") \
       :
        path = os.path.join(outdir,file)
        try:
            os.remove(path)
        except:
            errmsg("Unable to remove temporary file: " + path)

# STEP 1: parse album.txt line by line into the nsf_* variables above.
# Each line is a single directive: a keyword followed by its arguments
# (or, for several directives, free-form text running to the end of the
# line). Unrecognised keywords, and most malformed argument lists, are
# reported as errors with the offending line number.

try:
    album_lines = open(album,"rt").readlines()
except:
    errmsg("Unable to read album file: " + album)

for i in range(len(album_lines)):
    def line_error(msg):
        errmsg(("Line %d: " % (i+1)) + msg)
    l = album_lines[i]
    # strip comments (anything from the first '#' onward)
    comment = l.find("#")
    if comment >= 0:
        l = l[0:comment]
    # strip trailing whitespace and the newline
    l = l.rstrip()
    # tokenize what's left (shlex handles quoted filenames with spaces, etc)
    try:
        tokens = shlex.split(l)
    except Exception as e:
        line_error("shlex parsing error: " + str(e))
    if (len(tokens) < 1):
        continue # skip blank lines
    c = tokens[0]
    if c == "NSF":
        # starts a new album; every TRACK line from here on belongs to it,
        # until the next NSF line (if any)
        nsf_file = l[l.find(c)+len(c)+1:] # rest of the line, verbatim
        nsf_albums.append({"file": nsf_file})
    elif c == "NROM":
        if len(tokens) != 2:
            line_error("NROM expects one argument.")
        if tokens[1] != "0" and tokens[1] != "1":
            line_error("NROM expects 0 or 1.")
        nsf_nrom = int(tokens[1])
    elif c == "TITLE":
        nsf_title = l[l.find(c)+len(c)+1:] # rest of the line
    elif c == "ARTIST":
        nsf_artist = l[l.find(c)+len(c)+1:] # rest of the line
    elif c == "COPYRIGHT":
        nsf_copyright = l[l.find(c)+len(c)+1:] # rest of the line
    elif c == "PLAYALL":
        nsf_playall_text = l[l.find(c)+len(c)+1:] # rest of the line
    elif c == "INFOTEXT":
        nsf_info_text = l[l.find(c)+len(c)+1:] # rest of the line
    elif c == "TRACK":
        if len(tokens) < 3:
            line_error("TRACK expects a time and song argument.")
        time = tokens[1]
        tnum = tokens[2]
        # everything after the song number is the track's title, used both
        # in the playback screen and as this track's line in the main menu
        track = l[l.find(tnum,l.find(time)+len(time))+len(tnum)+1:]
        time_mins = "0"
        time_secs = time
        colon = time.find(":")
        if colon >= 0:
            time_mins = time[0:colon]
            time_secs = time[colon+1:]
        if len(nsf_albums) == 0:
            line_error("TRACK specified before any NSF line.")
        try:
            mins = int(time_mins)
            secs = int(time_secs)
            num = int(tnum)
            if (num < 1):
                line_errot("TRACK song number may not be less than 1.")
            # this track belongs to whichever NSF line came most recently
            album_index = len(nsf_albums) - 1
            nsf_tracks.append((track,num-1,mins,secs,album_index))
        except:
            line_error("Unable to read time or track argument for TRACK.")
    elif c == "SCREEN":
        if len(tokens) != 7:
            line_error("SCREEN expects 6 arguments.")
        nsf_screens.append((tokens[1],tokens[2],tokens[3],tokens[4],tokens[5],tokens[6]))
    elif c == "INFO":
        nsf_info.append(l[l.find(c)+len(c)+1:]) # rest of the line, one row of info text
    elif c == "COORD":
        if len(tokens) != 4:
            line_error("COORD expects 3 arguments.")
        coord = tokens[1]
        try:
            coord_x = int(tokens[2])
            coord_y = int(tokens[3])
            nsf_coord.append((coord,coord_x,coord_y))
        except:
            line_error("Unable to read number argument for COORD.")
    elif c == "CONST":
        if len(tokens) != 3:
            line_error("CONST expects 2 arguments.")
        ct = tokens[1]
        try:
            cv = int(tokens[2])
            nsf_const.append((ct,cv))
        except:
            line_error("Unable to read number argument for CONST.")
    else:
        line_error("Unknown statement type.")

if len(nsf_albums) == 0:
    errmsg("No NSF file specified in album file.")
if nsf_nrom != 0 and len(nsf_albums) > 1:
    errmsg("NROM mode only supports a single NSF file. Use NROM 0 for multiple NSF files.")

print("album info:")
for idx in range(len(nsf_albums)):
    print("  NSF #%d: %s" % (idx, nsf_albums[idx]["file"]))
print("  title: " + nsf_title)
print("  artist: " + nsf_artist)
print("  copyright: " + nsf_copyright)
print("  tracks: %d" % len(nsf_tracks))
print("  screens: %d" % len(nsf_screens))
print("  info lines: %d" % len(nsf_info))
print("  coordinates: %d" % len(nsf_coord))
print("  constants: %d" % len(nsf_const))
print()

# STEP 2: parse each NSF file's header and work out how its PRG data needs
# to be split into 4KB banks. Populates each entry in nsf_albums with the
# fields used by every later step (see the alb.update(...) call below for
# the full list).

nsf_banks = 0 # running total of 4k banks across all NSF albums (mapper 31 mode only)

for alb in nsf_albums:
    nsf_file = alb["file"]
    try:
        nsf = open(nsf_file,"rb").read()
    except:
        errmsg("Unable to read NSF file: " + nsf_file)

    if len(nsf) < 0x80:
        errmsg("NSF file too small: " + nsf_file)

    # NSF header fields we care about (see the NSF format spec for the
    # full layout); everything at 0x80 and beyond is the actual 6502
    # program data.
    bank = [ 0,0,0,0,0,0,0,0 ]
    load_addr = nsf[0x08] + (nsf[0x09] << 8)
    init_addr = nsf[0x0A] + (nsf[0x0B] << 8)
    play_addr = nsf[0x0C] + (nsf[0x0D] << 8)
    region = nsf[0x7A]
    banked = False

    for i in range(8):
        b = nsf[0x70+i]
        bank[i] = b
        if b != 0:
            banked = True

    if not banked and load_addr < 0x8000:
        errmsg("NSF LOAD address below $8000. WRAM or FDS not supported: " + nsf_file)

    rom_padding = 0

    if banked:
        # the NSF already specifies its own initial 8-register bank
        # layout; rom_padding just accounts for the load address not
        # necessarily starting exactly on a 4KB bank boundary
        rom_padding = load_addr & 0x0FFF
        if nsf_nrom != 0:
            errmsg("NSF requires bankswitching, cannot be used with NROM: " + nsf_file)
    else:
        # unbanked NSF: it expects to be loaded as one contiguous block
        # starting at load_addr, so give it an "identity" bank table
        # (bank i = physical bank i) and pad from $8000 up to load_addr
        rom_padding = load_addr - 0x8000
        for i in range(8):
            bank[i] = i

    highest_bank = 0
    for i in range(8):
        if bank[i] > highest_bank:
            highest_bank = bank[i]

    f000_local = bank[7] # which of this NSF's own banks sits at $F000 initially
    rom = bytearray([0] * rom_padding) + nsf[0x80:]

    alb.update({
        "nsf": nsf, "bank": bank, "banked": banked,
        "load_addr": load_addr, "init_addr": init_addr, "play_addr": play_addr,
        "region": region, "rom_padding": rom_padding, "highest_bank": highest_bank,
        "f000_local": f000_local, "rom": rom, "song_count": nsf[0x06],
    })

    print("NSF (%s):" % nsf_file)
    print("  LOAD: %04X" % load_addr)
    print("  INIT: %04X" % init_addr)
    print("  PLAY: %04X" % play_addr)
    print("  ROM size: %d bytes" % len(rom))
print()

# Splits `data` into consecutive 4KB (0x1000-byte) files named
# "<prefix><bank>.bin" in the output directory, zero-padding the last one
# up to a full bank if needed. `minbanks` forces at least that many banks
# to be written even if `data` is shorter (used for identity-mapped
# unbanked NSFs, which always occupy the full 8 banks / 32KB regardless of
# their actual size). If `trim` matches a bank's index, its last 6 bytes
# are left off to make room for the CPU vectors that get appended later
# (see the "high bank" handling in STEP 5).
# Returns the number of banks written.
def output_banks(prefix,data,trim=-1,minbanks=0):
    banks = 0
    extent = max(len(data),minbanks * 0x1000)
    while (banks * 0x1000) < extent:
        bank = banks
        of = os.path.join(outdir,prefix + ("%02X.bin" % bank))
        try:
            offset = bank * 0x1000
            s = data[offset : offset + 0x1000]
            if len(s) < 0x1000:
                s += bytearray([0] * (0x1000 - len(s))) # pad up to 4k
            if bank == trim:
                s = s[0:len(s)-6] # trim to make space for vectors
            open(of,"wb").write(s)
            print("Output: " + of)
        except:
            errmsg("Unable to write file: " + of)
        banks += 1
    return banks

# STEP 2b: patch in-song dynamic bankswitching (mapper 31 only).
#
# Some NSFs (typically FamiTracker exports with DPCM samples that don't fit
# in the static per-window bank table from the header) contain their own
# driver code that bankswitches windows $B000-$EFFF *during playback*,
# writing raw bank numbers relative to that NSF's own file. Since a second
# (or later) NSF in a combined ROM has its data offset by a base amount
# (its physical banks start partway through the ROM, not at bank 0), any
# such raw write would land on the wrong NSF's data unless corrected.
#
# Rather than trying to trace where those raw values come from at runtime
# (they can be buried deep in compiled pattern/instrument data, which is
# far too risky to try to identify and rewrite by hand), this patches the
# small, shared driver routines that actually perform the writes, so they
# add the album's base offset to the value right before storing it -
# regardless of where that value originally came from. This covers two
# known FamiTracker routine shapes:
#   A: STA $5FFx ; RTS                                   (one window)
#   B: CLC ; STA $5FFx ; ADC #1 ; STA $5FF(x+1)
#        ; ADC #1 ; STA $5FF(x+2) ; RTS                  (three consecutive windows)
# Patched routines are relocated into free (zero-padded) space within the
# same statically-mapped bank, and every JSR call site that used to target
# the original routine is repointed at the patched copy instead.

# Finds a run of at least `min_len` consecutive zero bytes within the 4KB
# chunk of `rom` that contains file offset `chunk`, and returns its start
# offset (or None if no long enough run exists). If that chunk happens to
# be the one that will end up as the "high bank" merged with the CPU
# vectors (avoid_tail_chunk), its last 6 bytes are excluded from the
# search, since they'll be overwritten by those vectors later.
def find_free_run(rom, chunk, min_len, avoid_tail_chunk=None):
    chunk_start = (chunk // 0x1000) * 0x1000
    chunk_bytes = rom[chunk_start:chunk_start+0x1000]
    usable_len = 0x1000
    if avoid_tail_chunk is not None and (chunk_start // 0x1000) == avoid_tail_chunk:
        usable_len = 0x1000 - 6
    run = 0
    run_start = 0
    for i in range(usable_len):
        if chunk_bytes[i] == 0:
            if run == 0:
                run_start = i
            run += 1
            if run >= min_len:
                return chunk_start + run_start
        else:
            run = 0
    return None

# Finds and patches every instance of the two routine shapes described
# above within `rom`, offsetting the bank value each one stores by
# `offset`. `bank_table` is this album's own (unmodified) 8-entry header
# bank table, used to translate between file offsets and the CPU addresses
# those routines/call sites actually appear at. `f000_local` identifies
# which bank is the one that gets trimmed for vectors (see find_free_run).
# Returns (possibly patched) rom, and the number of each routine shape found.
# A no-op (returns rom unchanged) when offset is 0, since album 0 never
# needs any translation.
def patch_dynamic_bankswitch(rom, bank_table, offset, label, f000_local=None):
    if offset == 0:
        return rom, 0, 0
    rom = bytearray(rom)
    n = len(rom)
    # maps "which of this album's own banks" -> "CPU address it's mapped
    # to at INIT time", using the album's original (unpatched) header
    # bank table; only banks that are actually part of that initial
    # mapping can contain statically-addressable code like these routines
    reverse_map = {}
    for win in range(8):
        reverse_map[bank_table[win]] = 0x8000 + win*0x1000
    def file_to_cpu(off):
        chunk = off // 0x1000
        within = off % 0x1000
        if chunk in reverse_map:
            return reverse_map[chunk] + within
        return None
    def find_callsites(target_addr):
        lo = target_addr & 0xFF
        hi = (target_addr >> 8) & 0xFF
        return [i for i in range(n-2) if rom[i]==0x20 and rom[i+1]==lo and rom[i+2]==hi]

    # find every occurrence of routine shape B (three consecutive windows)
    routB = []
    for p in range(n-14):
        if (rom[p]==0x18 and rom[p+1]==0x8D and 0xF8 <= rom[p+2] <= 0xFD and rom[p+3]==0x5F
            and rom[p+4]==0x69 and rom[p+5]==0x01
            and rom[p+6]==0x8D and rom[p+7]==rom[p+2]+1 and rom[p+8]==0x5F
            and rom[p+9]==0x69 and rom[p+10]==0x01
            and rom[p+11]==0x8D and rom[p+12]==rom[p+2]+2 and rom[p+13]==0x5F
            and rom[p+14]==0x60):
            routB.append(p)
    def overlaps_routB(p):
        # routine B's own tail ("...STA $5FF(x+2) ; RTS") looks exactly
        # like a standalone routine A match; exclude those so a single
        # routine B isn't also counted as a routine A
        return any(b <= p < b+15 for b in routB)
    # find every occurrence of routine shape A (single window), excluding
    # anything that's actually just the tail of a routine B match
    routA = [p for p in range(n-3)
             if rom[p]==0x8D and 0xF8 <= rom[p+1] <= 0xFF and rom[p+2]==0x5F and rom[p+3]==0x60
             and not overlaps_routB(p)]

    countA = 0
    countB = 0

    for p in routA:
        cpu_addr = file_to_cpu(p)
        if cpu_addr is None:
            continue
        reg = rom[p+1]
        free = find_free_run(rom, p, 8, f000_local)
        if free is None:
            errmsg("%s: no free space found to patch an in-song bankswitch routine. "
                   "Combining this NSF with others at a non-zero bank offset isn't possible "
                   "without more free space in its own banks." % label)
        # CLC ; ADC #offset ; STA $5FFx ; RTS - same effect as the
        # original "STA $5FFx ; RTS", but adding the album's base offset
        # to whatever value the caller passed in A first
        new_code = bytes([0x18, 0x69, offset & 0xFF, 0x8D, reg, 0x5F, 0x60])
        rom[free:free+len(new_code)] = new_code
        new_cpu = file_to_cpu(free)
        callsites = find_callsites(cpu_addr)
        for cs in callsites:
            rom[cs+1] = new_cpu & 0xFF
            rom[cs+2] = (new_cpu >> 8) & 0xFF
        print("  %s: patched in-song bank routine at $%04X (single window $5F%02X), "
              "%d call site(s), offset +%d" % (label, cpu_addr, reg, len(callsites), offset))
        countA += 1

    for p in routB:
        cpu_addr = file_to_cpu(p)
        if cpu_addr is None:
            continue
        reg1 = rom[p+2]
        free = find_free_run(rom, p, 18, f000_local)
        if free is None:
            errmsg("%s: no free space found to patch an in-song bankswitch routine. "
                   "Combining this NSF with others at a non-zero bank offset isn't possible "
                   "without more free space in its own banks." % label)
        # same idea as routine A above, but offsetting once and then
        # keeping the original "+1 twice more" chain for the other two windows
        new_code = bytes([0x18, 0x69, offset & 0xFF,
                           0x8D, reg1, 0x5F,
                           0x69, 0x01,
                           0x8D, reg1+1, 0x5F,
                           0x69, 0x01,
                           0x8D, reg1+2, 0x5F,
                           0x60])
        rom[free:free+len(new_code)] = new_code
        new_cpu = file_to_cpu(free)
        callsites = find_callsites(cpu_addr)
        for cs in callsites:
            rom[cs+1] = new_cpu & 0xFF
            rom[cs+2] = (new_cpu >> 8) & 0xFF
        print("  %s: patched in-song bank routine at $%04X (three windows starting $5F%02X), "
              "%d call site(s), offset +%d" % (label, cpu_addr, reg1, len(callsites), offset))
        countB += 1

    if countA == 0 and countB == 0:
        print("  %s: no in-song dynamic bankswitch routines found (nothing to patch)" % label)

    return rom, countA, countB

if nsf_nrom != 0:
    # NROM mode = a single, non-bankswitched 32KB PRG blob (only one
    # album is allowed here, enforced back in STEP 1)
    alb = nsf_albums[0]
    of = os.path.join(outdir,"nsf_nrom.bin")
    try:
        open(of,"wb").write(alb["rom"])
        print("Output: " + of)
    except:
        errmsg("Unable to write file: " + of)
else:
    # mapper 31: lay every album's banks out one after another in the
    # ROM. total_banks tracks the running "base" bank offset for whatever
    # album comes next.
    total_banks = 0
    for idx in range(len(nsf_albums)):
        alb = nsf_albums[idx]
        base = total_banks
        alb["base"] = base
        if base != 0:
            # every album except the first needs its in-song dynamic
            # bankswitching (if any) patched to account for its base
            # offset, and this has to happen before output_banks slices
            # it up into individual 4KB bank files below
            alb["rom"], patchedA, patchedB = patch_dynamic_bankswitch(
                alb["rom"], alb["bank"], base, "NSF #%d (%s)" % (idx, alb["file"]), alb["f000_local"])
        banks_out = output_banks("nsf%d_" % idx, alb["rom"], alb["f000_local"], alb["highest_bank"]+1)
        alb["banks_out"] = banks_out
        # translate this album's own local bank numbers (0-7, from its
        # header) into physical/global bank numbers within the combined ROM
        alb["bank_global"] = [base + b for b in alb["bank"]]
        alb["f000_global"] = base + alb["f000_local"]
        total_banks += banks_out
        print("NSF #%d 4k banks: %d (physical base %d)" % (idx, banks_out, alb["base"]))
    nsf_banks = total_banks
    print("Total NSF 4k banks: %d" % nsf_banks)
print()

# STEP 3: compress and pack the screen graphics (nametable + CHR + palette
# data) referenced by every SCREEN line in album.txt.

# Decodes one pack_ppu-compressed byte stream back into raw bytes. Used
# only to self-verify pack_ppu's output immediately after compressing (see
# below) - the actual decompression at runtime happens in eznsf.s's
# ppu_unpack, which implements the same format directly in 6502.
def unpack_ppu(rle):
    data = bytearray()
    run = 0
    command = 0
    # command:
    # 0 = read new RLE packet
    # 1 = read RLE length
    # 2 = read RLE byte and emit
    # 3 = read uncompressed bytes
    # 4 = end of stream reached
    for b in rle:
        if command == 0:
            if b == 0:
                command = 1
            else:
                run = b
                command = 3
        elif command == 1:
            if b == 0:
                command = 4
                break # end of stream
            else:
                run = b
                command = 2
        elif command == 2:
            data = data + bytearray([b] * run)
            command = 0
        elif command == 3:
            data.append(b)
            run -= 1
            if (run < 1):
                command = 0
        else:
            errmsg("Internal problem in RLE verificaiton.")
    if command != 4:
        errmsg("RLE end of stream reached without marker.")
    return data

# Compresses `data` with a simple run-length encoding: runs of 4 or more
# identical bytes become a 3-byte "repeat" packet, everything else is
# copied through as literal bytes prefixed with a length byte. The
# encoded stream is self-verified by decompressing it again immediately
# (via unpack_ppu above) and comparing against the original, so a bug here
# would be caught at build time rather than producing a corrupted ROM.
def pack_ppu(data):
    # RLE compression
    # 1. 0 = RLE to follow
    #    1-255 = 1-255 uncompressed bytes to follow, return to 1.
    # 2. 0 = end of stream
    #    1-255 = number of repeated bytes to follow
    # 3. byte to be repeated, return to 1.

    # helper functions to compress an RLE or non-RLE chunk of the stream
    def emit_compressed(s):
        output = bytearray()
        # ensure that s is all the same byte before we RLE compress
        for sb in s:
            if sb != s[0]:
                errmsg("Internal problem in RLE compressor!")
        # emit compressed packets
        while (len(s) > 0):
            emit = min(len(s),255)
            output.append(0)
            output.append(emit)
            output.append(s[0])
            s = s[emit:]
        return output
    def emit_uncompressed(s):
        output = bytearray()
        # emit uncompressed packets
        while (len(s) > 0):
            emit = min(len(s),255)
            output.append(emit)
            output += s[0:emit]
            s = s[emit:]
        return output

    # do the compression
    rle = bytearray()
    run = bytearray()
    running = False
    for d in data:
        run.append(d)
        rl = len(run)
        if running:
            if d != run[rl-2]:
                rle += emit_compressed(run[0:rl-1])
                run = run[rl-1:]
                running = False
        else:
            if rl >= 4:
                if d == run[rl-2] and d == run[rl-3] and d == run[rl-4]:
                    rle += emit_uncompressed(run[0:rl-4])
                    run = run[rl-4:]
                    running = True
    # any unfinished data left in the buffer should be emitted
    if running:
        rle += emit_compressed(run)
    else:
        rle += emit_uncompressed(run)
    # mark end of stream
    rle.append(0)
    rle.append(0)
    # verify:
    unpacked = unpack_ppu(rle)
    if len(unpacked) != len(data):
        #compare_rle(data,unpacked,rle) # diagnostic
        errmsg("RLE packing verification failed; length mismatch.")
    for i in range(0,len(data)):
        if unpacked[i] != data[i]:
            #compare_rle(data,unpacked,rle) # diagnostic
            errmsg("RLE packing verification failed.")
    # ready
    return rle

# Debug helper (not called during a normal build): prints `data`,
# `unpacked`, and `packed` side by side as hex dumps, for tracking down a
# pack_ppu/unpack_ppu mismatch by hand if the self-verification above
# ever fails.
def compare_rle(data,unpacked,packed):
    def printbin(s,name):
        print("data %s, size: %d" % (name,len(s)))
        os = ""
        pos = 0
        line_len = 32
        o = 0
        for b in s:
            os += (" %02X " % b)
            o += 1
            if (o >= line_len):
                print (("%04X: " % pos) + os)
                os = ""
                pos += o
                o = 0
        if (o > 0):
            print(("%04X: " % pos) + os)
    printbin(data,"data")
    printbin(unpacked,"unpacked")
    printbin(packed,"packed")          

ppu_files = {}     # filename -> index into ppu_files_immutable, first-seen order
ppu_offsets = {}    # filename -> byte offset within the combined ppu_data blob
ppu_data = bytearray() # every referenced graphics file's compressed data, concatenated

nrom_chr0 = ""   # NROM mode only: the one CHR file used as pattern table 0 by every screen
nrom_chr1 = ""   # NROM mode only: the one CHR file used as pattern table 1 by every screen

print("PPU data compression:")
for s in nsf_screens:
    for i in range(1,6):
        f = s[i]
        if nsf_nrom != 0:
            # NROM has real, fixed CHR ROM (no runtime bank switching for
            # graphics), so every screen must agree on exactly the same
            # two CHR files
            if i == 2:
                if nrom_chr0 == "":
                    nrom_chr0 = f
                elif nrom_chr0 != f:
                    errmsg("All NROM screens must use the same two CHR pages.")
            elif i == 3:
                if nrom_chr1 == "":
                    nrom_chr1 = f
                elif nrom_chr1 != f:
                    errmsg("All NROM screens must use the same two CHR pages.")
        if f in ppu_files:
            continue
        ppu_files[f] = len(ppu_files)

ppu_offset = 0
ppu_files_immutable = sorted(ppu_files.items())
for (f,i) in ppu_files_immutable:
    try:
        data = open(f,"rb").read()
    except:
        errmsg("Unable to read file: " + f)
    packed = pack_ppu(data)
    ppu_offsets[f] = ppu_offset
    if (nrom_chr0 == f) or (nrom_chr1 == f):
        # NROM's CHR files are copied in as real, raw CHR ROM elsewhere
        # (see the "TILES" segment in eznsf.s), so here they're just
        # placeholder entries - no need to also store compressed copies
        packed = bytearray([0,0])
    ppu_offset += len(packed)
    ppu_data += packed
    print("Compressed: %-20s from %4d / %4d bytes" % (f,len(packed),len(data)))

if nsf_nrom != 0:
    # NROM mode = single binary blob
    of = os.path.join(outdir,"ppu_nrom.bin")
    try:
        open(of,"wb").write(ppu_data)
        print("Output: " + of)
    except:
        errmsg("Unable to write file: " + of)
else:
    ppu_banks = output_banks("ppu_",ppu_data)
    print("PPU 4k banks: %d" % ppu_banks)
    if (ppu_banks > 7):
        errmsg("Too many PPU banks! Maximum: 7")
print()

# STEP 4: generate the two assembler include files eznsf.s pulls in
# (enums.sh and tables.sh), containing everything computed above in a form
# ca65 can consume directly - compile-time constants, enums, and
# initialized data tables.

# compute how many 4KB PRG banks the finished ROM needs in total, rounded
# up to the next power of two (mapper 31 bank-select registers select
# banks by number, and rounding to a power of two lets code rely on
# hardware address-line masking - e.g. "$FF" always safely selects the
# last bank regardless of how many banks actually exist)
if nsf_nrom == 0:
    needed_banks = nsf_banks + ppu_banks + 1
    padded_banks = 1
    while padded_banks < needed_banks:
        padded_banks *= 2
else:
    padded_banks = int(32 / 4)

# Turns a filename into a valid ca65 identifier fragment (letters only,
# everything else becomes an underscore) so it can be used as an enum
# member name.
def ppu_file_enum(s):
    so = ""
    for c in s:
        so += c if ((c>='a' and c<='z') or (c>='A' and c<='Z')) else "_"
    return so

s = ""
s += "; automatically generated by eznsf.py\n"
s += "; " + now_string + "\n"
s += "\n"
s += "MAPPER = %d\n" % (0 if (nsf_nrom != 0) else 31)
s += "BANKS = %d ; 4k bank count\n" % padded_banks
if (nsf_nrom != 0):
    alb = nsf_albums[0]
    s += ".define NROM_CHR0 \"%s\"\n" % nrom_chr0
    s += ".define NROM_CHR1 \"%s\"\n" % nrom_chr1
    s += ".define PPU_NROM_BIN \"%s/ppu_nrom.bin\"\n" % outdir
    s += ".define NSF_NROM_BIN \"%s/nsf_nrom.bin\"\n" % outdir
else:
    alb0 = nsf_albums[0]
    # album 0's high bank is still linked normally through ld65 (unchanged
    # from the original single-NSF build); every other album's equivalent
    # high bank is synthesized directly in STEP 5 below instead, since
    # ld65 only ever assembles this one high-bank page
    s += ".define NSF_F000 \"%s/nsf0_%02X.bin\"\n" % (outdir,alb0["f000_local"])
s += "\n"
s += ".enum eNSF\n"
if (nsf_nrom != 0):
    alb = nsf_albums[0]
    region_or = alb["region"]
else:
    # combine every album's region byte with a bitwise OR: if ANY of them
    # requests dual-region auto-detection, the ROM performs it once at
    # boot and uses the result for every album, not just whichever one
    # happened to ask for it
    region_or = 0
    for alb in nsf_albums:
        region_or |= alb["region"]
s += "\tREGION = %d ; combined region flags across all NSFs (dual-region if any NSF requests it)\n" % region_or
s += "\tTRACKS = %d\n" % len(nsf_tracks)
s += "\tALBUMS = %d\n" % len(nsf_albums)
if (nsf_nrom == 0):
    s += "\tBANKS = $%02X\n" % nsf_banks
s += ".endenum\n"
s += "\n"
s += ".enum eScreen\n"
for i in range(len(nsf_screens)):
    s += "\t%-20s = %2d\n" % (nsf_screens[i][0],i)
s += ".endenum\n"
s += "\n"
s += ".enum ePPU\n"
i = 0
for (k,v) in ppu_files_immutable:
    s += "\t%-20s = %2d\n" % (ppu_file_enum(k),i)
    i += 1
s += ".endenum\n"
s += "\n"
s += ".enum eCoord\n"
for coord in nsf_coord:
    s += "\t%-30s = %d\n" % (coord[0] + "_X", coord[1])
    s += "\t%-30s = %d\n" % (coord[0] + "_Y", coord[2])
s += ".endenum\n"
s += "\n"
s += ".enum eConst\n"
for c in nsf_const:
    s += "\t%-30s = %d\n" % (c[0], c[1])
s += ".endenum\n"
s += "\n"
s += "; end of file\n"

of = os.path.join(outdir,"enums.sh")
try:
    open(of,"wt").write(s)
    print("Output: " + of)
except:
    errmsg("Unable to write file: " + of)

s = ""
s += "; automatically generated by eznsf.py\n"
s += "; " + now_string + "\n"
s += "\n"
s += ".scope dString\n"
s += "\ttitle:     .asciiz \"" + nsf_title     + "\"\n"
s += "\tartist:    .asciiz \"" + nsf_artist    + "\"\n"
s += "\tcopyright: .asciiz \"" + nsf_copyright + "\"\n"
for i in range(0,len(nsf_tracks)):
    s += "\ttrack_%02d:  .asciiz \"%s\"\n" % (i,nsf_tracks[i][0])
s += "\tlist_play_all: .asciiz \"%s\"\n" % nsf_playall_text
s += "\tlist_info:     .asciiz \"%s\"\n" % nsf_info_text
s += "\tinfo:\n"
for si in nsf_info:
    s += "\t\t.byte \"%s\",13\n" % si
s += "\t\t.byte 0\n"
s += ".endscope\n"
s += "\n"
s += ".scope dNSF\n"
s += "\tbank_table: ; 8 bytes per album, physical/global bank numbers\n"
for idx in range(len(nsf_albums)):
    alb = nsf_albums[idx]
    bt = alb["bank"] if nsf_nrom != 0 else alb["bank_global"]
    s += "\t\t.byte " + ", ".join("$%02X" % b for b in bt) + (" ; album %d (%s)\n" % (idx, alb["file"]))
s += "\tinit_table:\n"
for idx in range(len(nsf_albums)):
    alb = nsf_albums[idx]
    s += "\t\t.word $%04X ; album %d (%s)\n" % (alb["init_addr"], idx, alb["file"])
s += "\tplay_table:\n"
for idx in range(len(nsf_albums)):
    alb = nsf_albums[idx]
    s += "\t\t.word $%04X ; album %d (%s)\n" % (alb["play_addr"], idx, alb["file"])
s += "\tbankf000_table:\n"
for idx in range(len(nsf_albums)):
    alb = nsf_albums[idx]
    f = alb["f000_local"] if nsf_nrom != 0 else alb["f000_global"]
    s += "\t\t.byte $%02X ; album %d (%s)\n" % (f, idx, alb["file"])
s += ".endscope\n"
s += "\n"
s += ".scope dTrack\n"
s += "\tstring_table:\n"
for i in range(len(nsf_tracks)):
    s += "\t\t.addr dString::track_%02d ; %s\n"  % (i,nsf_tracks[i][0])
s += "\tsong_table:\n"
for i in range(len(nsf_tracks)):
    s += "\t\t.byte %3d ; %s\n"  % (nsf_tracks[i][1],nsf_tracks[i][0])
s += "\tlength_table:\n"
for i in range(len(nsf_tracks)):
    s += "\t\t.word (%2d * 60) + %2d ; %s\n"  % (nsf_tracks[i][2],nsf_tracks[i][3],nsf_tracks[i][0])
s += "\talbum_table:\n"
for i in range(len(nsf_tracks)):
    s += "\t\t.byte %d ; %s\n" % (nsf_tracks[i][4], nsf_tracks[i][0])
s += ".endscope\n"
s += "\n"
s += ".enum eList\n"
s += "\tENTRIES = %d ; \"PLAY ALL\" + one per track + \"INFO\"\n" % (len(nsf_tracks) + 2)
s += ".endenum\n"
s += "\n"
s += ".scope dList\n"
s += "\tstring_table:\n"
s += "\t\t.addr dString::list_play_all\n"
for i in range(len(nsf_tracks)):
    s += "\t\t.addr dString::track_%02d ; %s\n" % (i, nsf_tracks[i][0])
s += "\t\t.addr dString::list_info\n"
s += ".endscope\n"
s += "\n"
s += ".scope dScreen\n"
s += "\tname_table:\n"
for i in range(len(nsf_screens)):
    s += "\t\t.byte ePPU::%-25s ; %s\n" % (ppu_file_enum(nsf_screens[i][1]),nsf_screens[i][0])
s += "\tchr0_table:\n"
for i in range(len(nsf_screens)):
    s += "\t\t.byte ePPU::%-25s ; %s\n" % (ppu_file_enum(nsf_screens[i][2]),nsf_screens[i][0])
s += "\tchr1_table:\n"
for i in range(len(nsf_screens)):
    s += "\t\t.byte ePPU::%-25s ; %s\n" % (ppu_file_enum(nsf_screens[i][3]),nsf_screens[i][0])
s += "\tpal0_table:\n"
for i in range(len(nsf_screens)):
    s += "\t\t.byte ePPU::%-25s ; %s\n" % (ppu_file_enum(nsf_screens[i][4]),nsf_screens[i][0])
s += "\tpal1_table:\n"
for i in range(len(nsf_screens)):
    s += "\t\t.byte ePPU::%-25s ; %s\n" % (ppu_file_enum(nsf_screens[i][5]),nsf_screens[i][0])
s += ".endscope\n"
s += "\n"
s += ".scope dPPU\n"
s += "\tdata_table:\n"
i = 0
for (k,v) in ppu_files_immutable:
    s += "\t\t.addr data_base + $%04X ; %-20s = %d\n" % (ppu_offsets[k],ppu_file_enum(k),i)
    i += 1
s += ".endscope\n"
s += "\n"
s += "; end of file\n"

of = os.path.join(outdir,"tables.sh")
try:
    open(of,"wt").write(s)
    print("Output: " + of)
except:
    errmsg("Unable to write file: " + of)

print()

# STEP 5: assemble eznsf.s and link the finished ROM.

def execute(args):
    print("Run: " + " ".join(args))
    print()
    proc = subprocess.Popen(args, stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
    proc.wait()
    for l in proc.stdout:
        print (l.decode().rstrip())
    return proc.returncode

link_object = os.path.join(outdir,"eznsf.o")

#assemble
if (0 != execute([ca65, "eznsf.s", "-I", outdir, "-g", "-o", link_object])):
    print()
    errmsg("Assemble of eznsf.s has failed!")

#link
ld65_debug = [ "-m", os.path.join(outdir,"eznsf.map"), "-Ln", os.path.join(outdir,"eznsf.lab") ]
if nsf_nrom != 0:
    if (0 != execute([ld65, "-o", os.path.join(outdir,"eznsf.nes"), "-C", "eznsf_nrom.cfg"] + ld65_debug + [link_object])):
        print()
        errmsg("Link of eznsf.nes has failed!")
else:
    # three separate links from the same object file: the "plain" driver
    # page (used for every padding bank and, briefly, whenever an NSF
    # isn't actively selected), album 0's high bank (driver code merged
    # with album 0's own raw bank-7 data plus the CPU vectors), and the
    # iNES header block
    if (0 != execute([ld65, "-o", os.path.join(outdir,"eznsf.bin"), "-C", "eznsf.cfg"] + ld65_debug + [link_object])):
        print()
        errmsg("Link of eznsf.bin has failed!")
    if (0 != execute([ld65, "-o", os.path.join(outdir,"f000.bin"), "-C", "eznsf_f000.cfg"] + [link_object])):
        print()
        errmsg("Link of f000.bin has failed!")
    if (0 != execute([ld65, "-o", os.path.join(outdir,"header.bin"), "-C", "eznsf_header.cfg"] + [link_object])):
        print()
        errmsg("Link of header.bin has failed!")

    # album 0's high bank ($F000 page) was just built normally by ld65 above
    # (it contains album 0's raw NSF bank-7 data plus EZNSF's vectors). For
    # any additional albums we need the exact same trick, but since ld65 only
    # assembled ONE such page, we synthesize the others directly: take the
    # trimmed raw NSF bank-7 data already written by output_banks() and glue
    # on the same 6 vector bytes ld65 already placed at the end of f000.bin
    # (those vectors just point at the shared driver code, so they're
    # identical no matter which album is playing).
    nsf_albums[0]["f000_file"] = os.path.join(outdir,"f000.bin")
    try:
        vector_bytes = open(nsf_albums[0]["f000_file"],"rb").read()[-6:]
    except:
        errmsg("Unable to read file: " + nsf_albums[0]["f000_file"])
    for idx in range(1,len(nsf_albums)):
        alb = nsf_albums[idx]
        src = os.path.join(outdir, "nsf%d_%02X.bin" % (idx, alb["f000_local"]))
        try:
            data = open(src,"rb").read()
        except:
            errmsg("Unable to read file: " + src)
        if len(data) != 0x1000 - 6:
            errmsg("Unexpected size for trimmed NSF bank: " + src)
        of = os.path.join(outdir, "f000_%d.bin" % idx)
        try:
            open(of,"wb").write(data + vector_bytes)
            print("Output: " + of)
        except:
            errmsg("Unable to write file: " + of)
        alb["f000_file"] = of

    # concatenate every bank, in physical order, into the final ROM image
    def readbin(f,seg):
        try:
            b = open(f,"rb").read()
        except:
            errmsg("Unable to read file: " + f)
        print ("Segment %03X: %s" % (seg & 0xFFF, f))
        return b

    print("Concatenating %d banks..." % padded_banks)
    output_nes = bytearray()
    output_nes += readbin(os.path.join(outdir,"header.bin"),-1)
    seg = 0
    for idx in range(len(nsf_albums)):
        alb = nsf_albums[idx]
        for i in range(alb["banks_out"]):
            if i == alb["f000_local"]:
                # this is the album's high bank: use its synthesized/linked
                # driver+vectors version instead of the plain raw NSF data
                output_nes += readbin(alb["f000_file"], seg)
            else:
                output_nes += readbin(os.path.join(outdir, "nsf%d_%02X.bin" % (idx,i)), seg)
            seg += 1
    for i in range(ppu_banks):
        output_nes += readbin(os.path.join(outdir,"ppu_%02X.bin" % i), seg)
        seg += 1
    for i in range(padded_banks - (nsf_banks + ppu_banks)):
        # pad the rest of the ROM out to a power-of-two bank count with
        # copies of the plain driver page
        output_nes += readbin(os.path.join(outdir,"eznsf.bin"), seg)
        seg += 1

    # output the file
    try:
        of = os.path.join(outdir,"eznsf.nes")
        open(of,"wb").write(output_nes)
        print("Output: " + of)
    except:
        errmsg("Unable to write file: " + of)
    print()

# STEP 6: write out one .nsfe file per NSF album. NSFe is like NSF but with
# extra chunks for playlist metadata (track names/lengths); each album's
# file only describes that album's own tracks.

if (output_nsfe):
    def fourcc(s):
        fcc = bytearray()
        for i in range(4):
            fcc.append(ord(s[i]))
        return fcc

    def packword(w):
        w = w & 0xFFFF
        wb = bytearray()
        wb.append(w & 255)
        wb.append(w >> 8)
        return wb

    def packlong(l):
        l = l & 0xFFFFFFFF
        lb = bytearray()
        lb.append(l & 255)
        lb.append((l >> 8) & 255)
        lb.append((l >> 16) & 255)
        lb.append(l >> 24)
        return lb

    def packstring(s):
        sb = bytearray(s.encode(encoding="utf-8"))
        sb.append(0)
        return sb

    def nsfe_chunk(fcc,data):
        chunk = bytearray()
        chunk += packlong(len(data))
        chunk += fourcc(fcc)
        chunk += data
        return chunk

    for idx in range(len(nsf_albums)):
        alb = nsf_albums[idx]
        nsf = alb["nsf"]
        nsf_bank = alb["bank"]
        nsf_banked = alb["banked"]
        song_count = alb["song_count"]

        # this album's own tracks, keyed by their NSF song number
        nsfe_tracks = {}
        for (t,n,m,s,a) in nsf_tracks:
            if a == idx:
                nsfe_tracks[n] = (t,m,s)

        nsfe_rom = bytearray()
        nsfe_rom += fourcc("NSFE")

        nsfe_info = bytearray()
        nsfe_info += packword(alb["load_addr"])
        nsfe_info += packword(alb["init_addr"])
        nsfe_info += packword(alb["play_addr"])
        nsfe_info.append(alb["region"])
        nsfe_info.append(nsf[0x7B]) # Expansion
        nsfe_info.append(song_count)
        nsfe_info.append(nsf[0x07]-1) # Starting song
        nsfe_rom += nsfe_chunk("INFO",nsfe_info)

        if nsf_banked:
            nsfe_rom += nsfe_chunk("BANK",bytearray(nsf_bank))

        nsfe_rom += nsfe_chunk("DATA",nsf[0x80:])

        nsfe_auth = bytearray()
        nsfe_auth += packstring(nsf_title)
        nsfe_auth += packstring(nsf_artist)
        nsfe_auth += packstring(nsf_copyright)
        nsfe_auth += packstring("eznsf.py")
        nsfe_rom += nsfe_chunk("auth",nsfe_auth)

        nsfe_plst = bytearray()
        for (t,n,m,s,a) in nsf_tracks:
            if a == idx:
                nsfe_plst.append(n)
        nsfe_rom += nsfe_chunk("plst",nsfe_plst)

        nsfe_time = bytearray()
        for ti in range(0,song_count):
            if ti in nsfe_tracks:
                (t,m,s) = nsfe_tracks[ti]
                nsfe_time += packlong(1000 * ((m*60) + s))
            else:
                nsfe_time += packlong(-1)
        nsfe_rom += nsfe_chunk("time",nsfe_time)

        nsfe_tlbl = bytearray()
        for ti in range(0,song_count):
            if ti in nsfe_tracks:
                (t,m,s) = nsfe_tracks[ti]
                nsfe_tlbl += packstring(t)
            else:
                nsfe_tlbl.append(0)
        nsfe_tlbl.append(0)
        nsfe_rom += nsfe_chunk("tlbl",nsfe_tlbl)

        nsfe_rom += nsfe_chunk("text",packstring("eznsf.py"))

        nsfe_rom += nsfe_chunk("NEND",bytearray())

        # only add the "_<index>" suffix when there's more than one album,
        # so a single-NSF album.txt still produces the same plain
        # "eznsf.nsfe" filename as before this fork's changes
        if len(nsf_albums) > 1:
            of = os.path.join(outdir,"eznsf_%d.nsfe" % idx)
        else:
            of = os.path.join(outdir,"eznsf.nsfe")
        try:
            open(of,"wb").write(nsfe_rom)
            print("Output: " + of)
        except:
            errmsg("Unable to write file: " + of)
    print()

# STEP OFF!

print("Success!")
sys.exit(0)
