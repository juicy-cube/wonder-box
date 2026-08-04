      ---------------------------------------------
     /   EEEEE   ZZZZZ   N   N    SSSS   FFFFF   /
    /   E          Z    NN  N   S       F       /
   /   EEE       Z     N N N    SSS    FFF     /
  /   E        Z      N  NN       S   F       /
 /   EEEEE   ZZZZZ   N   N   SSSS    F       /
---------------------------------------------

 EZNSF is a tool for transforming NSF music files into NES ROMs
 
 Version 1.0
 Written by Brad Smith, 2016-12-05

 *** This copy is a fork with a number of additional changes on top of
 *** the original 1.0 release - see "CHANGES IN THIS FORK" near the end
 *** of this file for the full list. The short version: it can combine
 *** more than one NSF file into a single ROM with an in-ROM menu to pick
 *** between them, it's more careful about protecting DPCM/DMC sample
 *** playback from corruption, and the on-screen menu has been redesigned.


Prerequisites:

 1. Python 3
      Available here: https://www.python.org/downloads/
 2. CC65 assembler and linker
      Available here: http://cc65.github.io/cc65/
      A windows version of CC65's assembler and linker are included in the tools folder.
      Users with other platforms can adapt the python script to use another version of CC65.
 3. NES Screen Tool
      Available here: https://shiru.untergrund.net/software.shtml
      The screen tool is helpful if you wish to modify the graphics produced by the ROM,
      but it is not needed for simply building ROMs.

Directions:

 1. Create a suitable NSF (see below for requirements).
 2. Edit album.txt with track times and names and other info. (This fork lets you list
    more than one NSF file in album.txt - see "Multiple NSF files" under CHANGES IN THIS
    FORK below.)
 3. Run eznsf.py to build a ROM as "temp/output.nes".
    If there are any errors, look at the output from eznsf.py and try to fix them.
    Make sure to run eznsf.py from a command line or shell where you can read its result.
    (The included "eznsf.bat" will open a command window and pause to show the output.)

 
NSF Requirements:

 * No expansion sound.
 * Standard engine rate only (60/50 Hz)
 * NSF may not bankswitch F000
 * NSF may not use RAM (or clear it during init) in the range $400-7FF
 * NSF may not use zero page (or clear it) in the range $FA-FF
 * NSF may not depend on WRAM present at $6000-7FFF

 This program was primarily intended for use with Famitracker, which
 should normally obey all of these requirements since at least 0.4.5 if not earlier.
 It can potentially work with many other NSFs.

 (Note: the RAM and zero page ranges above are wider than in the original 1.0 release -
 $600-7FF and $FC-FF respectively. This fork's driver needs the extra room; see CHANGES
 IN THIS FORK below for why.)

Notes:

 The produced ROM will use mapper 31 by default, which is a relatively new creation.
 Many old emulators will not support this. Recent versions of the following will:
   * FCEUX: http://www.fceux.com/
   * MESS: http://www.mess.org/
   * BizHawk: http://tasvideos.org/BizHawk.html
   * Nintendulator: http://www.qmtpro.com/~nes/nintendulator/
   * puNES: http://forums.nesdev.com/viewtopic.php?t=6928
   * Everdrive N8: http://krikzz.com/store/home/31-everdrive-n8-nes.html

 If the NSF does not require bankswitching (many NSFs under 32k), it may be possible to
 build as the simple NROM mapper 0, which is supported by all emulators.
 Simply create a line that says "NROM = 1" in your album.txt file.
 The NROM version will require about 3kb of empty space, so the build may fail if not
 enough room can be found. An example is included as "album_nrom.txt".

 This tool also uses the track times and names to produce an NSFe file in the output
 directory. An NSFe is like an NSF but with playlist features like individual track
 names and times. If undesired you can turn this extra step off in eznsf.py by
 setting "output_nsfe = False" near the top of the file. (This fork writes one .nsfe
 file per NSF album when album.txt lists more than one - see below.)

 NSFs with the "dual region" bit set will attempt to auto-detect the system region on
 startup, and will pass the appropriate value to the NSF INIT function when tracks are played.
 Otherwise, the detection will be omitted and the region speciied in the NSF header will be used.

 This program is open source, and I give permission to modify and reuse it in any way you like.
 I encourage experimentation. This is hopefully intended as a learning example, and not just as
 a tool.

 The eznsf.py script can be run with two command line arguments. The first argument is the name
 of the album.txt file (if omitted it will use "album.txt"). The second argument is the output
 folder (if omitted it will use "temp").

 The sample music is from the Classic Chips album of classical music arranged for NES:
 http://rainwarrior.ca/music/classic_chips.html

 The graphics in this are configurable with album.txt, and by editing their definitions in that file.
 Take a look at the comments and experiment to see what they do. Use the NES Screen Tool
 to modify the example CHR/NAM/PAL files.

 Contact me if you have questions or comments.
 http://rainwarrior.ca

--- end of original readme ---


CHANGES IN THIS FORK
=====================

Everything below was added on top of Brad Smith's original 1.0 release.
It's organised by feature rather than strictly by the order it was built
in; eznsf.py and eznsf.s are both commented throughout with the same
level of detail, so treat this as an overview and the source itself as
the definitive reference.


1. Multiple NSF files in one ROM
---------------------------------

album.txt can now contain more than one "NSF <file>" line. Every TRACK
line belongs to whichever NSF line most recently came before it, so you
group each NSF's tracks under it, e.g.:

    NROM 0
    TITLE My Album

    NSF song1.nsf
    TRACK 03:00 1 Song 1

    NSF song2.nsf
    TRACK 02:30 1 Song 2

All tracks from every listed NSF show up together in one combined menu.
This only works in mapper 31 (bankswitched) builds - NROM 1 still only
supports a single NSF file, since NROM has no room to spare for more than
one NSF's worth of PRG data.

Internally, each NSF after the first has its PRG banks placed after the
previous ones in the ROM, and gets an added base offset applied to every
bank number it uses - both the ones from its own header (used once at
track-start) and, importantly, any bank numbers its own compiled code
might write out on its own while playing (see "In-song dynamic
bankswitch patching" below for why that needs special handling).

One .nsfe file is written per NSF album (e.g. "eznsf_0.nsfe",
"eznsf_1.nsfe", ...) instead of a single "eznsf.nsfe", since an NSFe file
can only describe one NSF's own tracks. With a single NSF file in
album.txt, the output is still just "eznsf.nsfe", same as before.


2. Permanent bank-pinning for correct DPCM/DMC playback
----------------------------------------------------------

The original driver kept its own menu/UI code resident in the same
$F000-$FFFF PRG window used by the currently playing NSF's own high bank,
switching that window over to the NSF's data only for the brief moment of
each INIT/PLAY call and switching it back to the driver's own code the
rest of the time. That's harmless for regular sample playback, but the
DMC (DPCM sample) channel reads its sample data via its own DMA engine,
completely independently of what the CPU is doing - so for almost the
entire time a track was playing, DMC DMA would be reading whatever the
*driver's own code* happened to be, rather than the NSF's actual sample
data, whenever the currently playing sample happened to live in that
shared bank.

This fork instead pins $F000-$FFFF to the currently playing NSF's own
bank for the *entire* time a track is loaded (not just during INIT/PLAY
calls), so DMC DMA always sees correct data. To make that possible, every
piece of driver code that needs to keep running while a track is loaded
(the whole per-frame playback loop: timer, controller reading, on-screen
sprites, pause handling, track switching, the starfield effect) had to be
moved out of that ROM window entirely and into RAM instead (see the
RAMCODE/PLAYCODE segments in eznsf.s), since code fetched from a bank
that's just been switched away from would otherwise be reading the wrong
data mid-instruction-stream.

This needed more RAM than the original driver reserved for itself, hence
the wider reserved ranges noted under NSF Requirements above ($400-7FF
instead of $600-7FF, $FA-FF instead of $FC-FF zero page). A well-behaved
FamiTracker export shouldn't be affected by this - the extra reserved
range is still well within what FamiTracker's own sound engine typically
uses - but it's worth knowing about if you're combining this driver with
an unusually memory-hungry hand-written NSF.


3. In-song dynamic bankswitch patching
-----------------------------------------

Some NSFs (typically FamiTracker exports with DPCM samples that don't fit
in the static per-window bank table declared in the NSF header) contain
their own driver code that bankswitches windows $B000-$EFFF *while
playing*, to bring in extra DPCM sample banks on demand. That code writes
bank numbers relative to that NSF's own file - it has no idea its data
has been relocated to make room for other NSFs in a combined ROM, so
those raw writes would otherwise land on the wrong NSF's data (and,
depending on exactly which banks collide, on the wrong physical ROM
bank's worth of code or samples).

Tracing exactly where those raw bank numbers come from at runtime isn't
practical in general - they can be buried arbitrarily deep in compiled
pattern/instrument data, and guessing wrong risks corrupting a real note
or effect. Instead, eznsf.py scans each NSF (other than the first, which
never needs an offset) for two specific, small, shared driver-routine
shapes that FamiTracker's own compiled output uses to perform these
writes, and patches copies of them - relocated into free space within the
NSF's own banks - to add that NSF's base bank offset to the value before
it's stored, regardless of where the value originally came from. Every
call site that used to jump to the original routine is repointed at the
patched copy. If no such routines are found in a given NSF, eznsf.py just
says so and moves on - most NSFs that don't use this technique don't need
anything patched at all.


4. Redesigned menu
----------------------

The ROM now boots directly into a menu (what used to be the separate
"tracks" screen) instead of a title screen with a Start/Info choice:

    PLAY ALL
    <first track>
    <second track>
    ...
    INFO

The "PLAY ALL" label is configurable via a PLAYALL line in album.txt, and
the "INFO" label via an INFOTEXT line; both default to the text shown
above if not specified. The title screen (and its "title.nam" screen
graphic) is no longer used at all.

Button mapping:
    A / START  - on the menu: activate the highlighted entry
               - while playing: A toggles between looping the current
                 track forever (the default when you pick a single track
                 directly) and continuing on to the next track once it
                 ends, wrapping back to the first track after the last one
                 (the default when you pick "PLAY ALL"); START pauses/
                 resumes, same as before
    B          - while playing or on the INFO screen: cancel, back to
                 the menu
               - on the menu itself: does nothing (there's nothing to
                 cancel back to - it's the home screen now)
    Direction  - unchanged: move the menu cursor, or skip to the
      buttons     previous/next track while playing (this always clamps
                  at the first/last track - it does not wrap around,
                  unlike reaching the natural end of a track)
    SELECT     - does nothing

Reaching the natural end of a track no longer stops and waits for a
button press: it loops or advances immediately, in the same frame,
according to whichever of the two modes above is currently active.

The A-button mode toggle is debounced (ignored for a short window right
after it fires) to guard against an occasional spurious extra button-press
reading right around when DPCM playback starts - see the comment on
gamepad_poll's double-read logic in eznsf.s, and on debounce_a, for the
underlying hardware reason this can happen.


5. More robust controller reading
-------------------------------------

DMC/DPCM sample playback drives the APU's own DMA engine, which can
occasionally steal a CPU cycle at an inconvenient moment - most notably
during the one-off "restart" stall when a DPCM sample is first enabled -
and a stolen cycle landing mid-read can corrupt a single bit of a
$4016 controller read. gamepad_poll reads the controller twice and
retries until both reads agree, so a single corrupted read can't be
misread as a button press; combined with the debounced A toggle mentioned
above, this is what keeps track switching and the loop/continue toggle
reliable even while DPCM samples are actively playing.


6. Starfield background
---------------------------

The playback screen shows a simple scrolling starfield behind the track
info, using OAM sprites 6 and up (the first 6 are reserved for the
elapsed-time digits and the play-state icon). See starfield_init and
starfield_update in eznsf.s.


--- end of file ---
