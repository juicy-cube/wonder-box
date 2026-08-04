;
; EZNSF
;
; Original tool by bradsmith, 2016 - http://rainwarrior.ca
; This fork adds: combining multiple NSF files into one ROM, permanent
; bank-pinning so DPCM/DMC playback is never corrupted, patching of
; NSF-internal dynamic bankswitching so a second (or later) NSF's own
; runtime bank changes land on the right physical bank, a redesigned
; boot-to-list menu with a debounced "loop / continue" toggle, and a
; simple starfield background effect on the playback screen.
;

;
; RAM usage
;
; The NES only guarantees $00-$FB (zero page) and $0200-$03FF as "free for
; the currently playing NSF's own use" - the NSF format's own convention is
; that this range gets zeroed before each INIT call and the NSF may use it
; freely while playing. Everything the driver itself needs to survive
; across an NSF's INIT call (variables, and the RAM-resident code described
; further down) therefore has to live outside that range: zero page $FA-$FF
; and general RAM $0400-$07FF are reserved for the driver and are never
; touched by the per-track RAM clear (see play_init below). $0700-$07FF is
; further reserved for the hardware OAM (sprite) buffer.
;

.segment "OAM"
.align 256
oam: .res 256 ; shadow OAM buffer, DMA'd to the PPU once per frame by render_on

.segment "ZEROPAGE"
ptr:          .res 2 ; general purpose pointer for indirect-indexed addressing
dst_ptr:      .res 2 ; second pointer, used only by load_playcode as its copy destination
                      ; (load_playcode itself lives in ROM and can't self-modify its own
                      ; operands, since a write to a ROM address is simply ignored by real
                      ; hardware - so it needs a genuine writable pointer instead)
nmi_count:    .res 1 ; incremented once per NMI (vertical blank); used to detect a new frame
gamepad:      .res 1 ; most recently read, verified 8-bit controller state

.segment "BSS"
pal:          .res 1 ; detected TV system: 0 = NTSC (60 fps), 1 = PAL (50 fps)
fps:          .res 1 ; 60 or 50, matching "pal" above; used to time the on-screen MM:SS clock
frame:        .res 1 ; counts 0..fps-1 while a track plays, to mark off one real second
sec0:         .res 1 ; on-screen elapsed-time display: seconds, ones digit
sec1:         .res 1 ; on-screen elapsed-time display: seconds, tens digit (0-5)
min0:         .res 1 ; on-screen elapsed-time display: minutes, ones digit
min1:         .res 1 ; on-screen elapsed-time display: minutes, tens digit
list_choose:  .res 1 ; cursor position on the main list screen:
                      ;   0            = "PLAY ALL"
                      ;   1..TRACKS    = that track (list index - 1 = track_choose)
                      ;   TRACKS+1     = "INFO" (the last entry, eList::ENTRIES-1)
track_choose: .res 1 ; index (0-based) of the track currently loaded/playing
play_through: .res 1 ; 0 = loop the current track forever when it ends
                      ; 1 = when the current track ends, move on to the next one (wrapping
                      ;     back to track 0 after the last track) instead of repeating it
play_paused:  .res 1 ; 0 = playing, 1 = paused, 2 = just reached the end of the track
                      ;     (this state is handled and cleared again within the same frame -
                      ;     see the "@track_finished" label in play_loop - so in practice it's
                      ;     only ever observed transiently, not as a "waiting" state)
play_len:     .res 2 ; length of the current track in seconds, from album.txt's TRACK line
play_secs:    .res 2 ; number of seconds played so far in the current track
gamepad_last: .res 1 ; controller state as of the previous poll, used for edge detection
gamepad_new:  .res 1 ; buttons that became newly pressed since the previous poll
temp:         .res 2 ; scratch bytes, freely reused by many unrelated routines - never assume
                      ; its value survives a call to anything else
play_album:     .res 1 ; which NSF file (album index) the currently loaded track belongs to
play_bank_f000: .res 1 ; physical PRG bank to keep permanently mapped at $F000-$FFFF for as
                        ; long as this album's track is loaded (see the RAMCODE segment notes
                        ; below for why this has to be pinned rather than switched per call)
debounce_a:   .res 1 ; number of frames left to ignore the A button for, after it was just
                      ; used to toggle play_through - see the "A = toggle loop/continue" bit
                      ; of play_loop for why this exists

;
; useful definitions
;

PAD_A      = $01
PAD_B      = $02
PAD_SELECT = $04
PAD_START  = $08
PAD_U      = $10
PAD_D      = $20
PAD_L      = $40
PAD_R      = $80

; Sets the PPU address pointer ($2006/$2006) to `addr`, after first reading
; $2002 to reset the PPU's internal write-latch (so the two $2006 writes
; below are interpreted as high byte then low byte, and not thrown off by
; whatever write sequence happened earlier).
.macro PPU_LATCH addr
	lda $2002
	lda #>(addr)
	sta $2006
	lda #<(addr)
	sta $2006
.endmacro

; Converts a (column, row) tile coordinate into the corresponding nametable
; address in PPU space (each nametable row is 32 tiles).
.define PPU_TILE(ax,ay) ($2000+(ay*32)+ax)

; Points the "ptr" zero-page pointer at `addr`.
.macro PTR_LOAD addr
	lda #<(addr)
	sta ptr+0
	lda #>(addr)
	sta ptr+1
.endmacro

; Shorthand for "load val into A, then store A to addr".
.macro STB addr, val
	lda val
	sta addr
.endmacro

;
; Data and enums generated by eznsf.py from album.txt (track names, screen
; layout tables, NSF bank tables, etc). Included here so the rest of this
; file can refer to them as ordinary assembler symbols.
;

.segment "CODE"
.include "enums.sh"
.include "tables.sh"

;
; INFO screen: shows the free-form text entered via INFO lines in album.txt.
;

.proc mode_info
	lda #eScreen::INFO
	jsr load_screen
	INFO_ADDR = PPU_TILE(eCoord::INFO_X, eCoord::INFO_Y)
	lda #>INFO_ADDR
	sta temp+1
	sta $2006
	lda #<INFO_ADDR
	sta temp+0
	sta $2006
	PTR_LOAD dString::info
	:
		; the info text is one big null-terminated string with embedded
		; newline (13) markers between lines; print a line, then if the
		; byte that stopped us was a real newline (not the final null),
		; advance to the next screen row and keep going
		jsr ppu_string
		lda (ptr), Y
		beq :+
		jsr ppu_temp_line
		jmp :-
	:
	jsr oam_clear
@loop:
	jsr render_on
	jsr gamepad_poll
	; B = cancel, return to the main list
	lda gamepad_new
	and #(PAD_B)
	beq @loop
	jmp mode_list
.endproc

;
; Main list screen (the boot screen): shows "PLAY ALL", one line per track,
; then "INFO", and lets the player pick one with the d-pad + A/START.
;

.proc list_sprite
	; positions the single selection-cursor sprite next to whichever list
	; entry list_choose currently points at; each entry occupies one text
	; row, so moving the cursor is just an 8-pixel-per-entry Y offset
	jsr oam_clear
	STB oam+(0*4)+1, #eConst::SPRITE_CHOOSE
	STB oam+(0*4)+2, #2
	STB oam+(0*4)+3, #(eCoord::TRACKS_TRACK_X-2)*8
	lda list_choose
	asl
	asl
	asl
	clc
	adc #(eCoord::TRACKS_TRACK_Y*8)-1
	sta oam+(0*4)+0
	rts
.endproc

.proc mode_list
	; make sure nothing keeps making noise while we're back on the menu
	lda #0
	sta $4015
	sta $4011
	; load the screen graphics/nametable and print the title/artist/copyright
	lda #eScreen::TRACKS
	jsr load_screen
	PPU_LATCH PPU_TILE(eCoord::TRACKS_TITLE_X, eCoord::TRACKS_TITLE_Y)
	PTR_LOAD dString::title
	jsr ppu_string
	PPU_LATCH PPU_TILE(eCoord::TRACKS_ARTIST_X, eCoord::TRACKS_ARTIST_Y)
	PTR_LOAD dString::artist
	jsr ppu_string
	PPU_LATCH PPU_TILE(eCoord::TRACKS_COPYRIGHT_X, eCoord::TRACKS_COPYRIGHT_Y)
	PTR_LOAD dString::copyright
	jsr ppu_string
	; print the menu list itself: "PLAY ALL", then each track name, then
	; "INFO" (dList::string_table holds exactly these ENTRIES pointers, in
	; this order), one per screen row
	LIST_ADDR = PPU_TILE(eCoord::TRACKS_TRACK_X, eCoord::TRACKS_TRACK_Y)
	lda #>LIST_ADDR
	sta temp+1
	sta $2006
	lda #<LIST_ADDR
	sta temp+0
	sta $2006
	ldx #0
	:
		lda dList::string_table+0, X
		sta ptr+0
		lda dList::string_table+1, X
		sta ptr+1
		inx
		inx
		jsr ppu_string
		cpx #(eList::ENTRIES * 2)
		bcs :+
		jsr ppu_temp_line
		jmp :-
	:
@loop:
	lda gamepad
	sta gamepad_last
	jsr list_sprite
	jsr render_on
	jsr gamepad_poll
	; L/U = move the cursor up one entry, R/D = move it down one entry;
	; both are clamped at the top/bottom of the list (no wraparound here -
	; this is menu navigation, not track-looping)
	lda gamepad_new
	and #(PAD_L | PAD_U)
	beq :+
		lda list_choose
		beq @move_end
		dec list_choose
		jmp @move_end
	:
	lda gamepad_new
	and #(PAD_R | PAD_D)
	beq :+
		lda list_choose
		cmp #(eList::ENTRIES-1)
		bcs @move_end
		inc list_choose
		jmp @move_end
	:
@move_end:
	; A or START = activate whatever entry the cursor is currently on
	lda gamepad_new
	and #(PAD_A | PAD_START)
	beq @loop
		lda list_choose
		bne @not_play_all
			; entry 0: "PLAY ALL" - start from track 0, in "continue to
			; next track" mode so it plays through the whole list
			lda #0
			sta track_choose
			lda #1
			sta play_through
			jmp mode_play
		@not_play_all:
		cmp #(eList::ENTRIES-1)
		bne @not_info
			; last entry: "INFO"
			jmp mode_info
		@not_info:
		; anything else is a specific track: list entries 1..TRACKS map to
		; track_choose 0..TRACKS-1, and a directly-chosen track defaults to
		; looping itself (play_through = 0) rather than continuing onward
		sec
		sbc #1
		sta track_choose
		lda #0
		sta play_through
		jmp mode_play
.endproc

;
; playback screen setup
;

;
; Starfield background effect, shown behind the playback screen. Sprites
; 6 and up (OAM byte offset 24 and up - sprites 0-5 are reserved for the
; timer digits/colon/play-state icon set up by play_sprite_init below) are
; used as scrolling background stars.
;

.proc starfield_init
	; one-time setup, run from ROM before the track's own bank gets pinned
	; (see mode_play below): give every star sprite from OAM offset 24
	; onward a random starting position and a fixed appearance.
	ldx #24 ; first OAM byte offset not used by the playback HUD sprites
	lda nmi_count ; seed the PRNG from whatever the frame counter happens
	              ; to be right now, so the pattern differs between runs
	bne @skip_zero
	lda #$89 ; the PRNG below can't recover from a zero seed (it would
	         ; just keep producing zero), so substitute a fixed nonzero
	         ; value in the unlikely case nmi_count is currently 0
@skip_zero:
	sta temp
@init_loop:
	; random Y coordinate for this star
	jsr @rand
	sta oam+0, X
	; fixed tile index (a single star glyph) and no flip/palette bits
	lda #$0B
	sta oam+1, X
	lda #0
	sta oam+2, X
	; random X coordinate for this star
	jsr @rand
	sta oam+3, X
	; advance 4 bytes (one sprite) at a time; X is only 8 bits, so this
	; loop naturally stops once X wraps back around to 0, i.e. after
	; exactly 64 sprites (all of OAM) have been touched
	inx
	inx
	inx
	inx
	bne @init_loop
	rts

	; Simple 8-bit Galois LFSR pseudo-random number generator: shifts the
	; seed right 8 times, XORing in the constant $B4 whenever a 1 bit
	; shifts out. Cheap and good enough for scattering star positions -
	; not intended to be statistically rigorous.
@rand:
	lda temp
	ldy #8
@rand_loop:
	lsr
	bcc @skip_eor
	eor #$B4
@skip_eor:
	dey
	bne @rand_loop
	sta temp
	rts
.endproc

.proc play_sprite_init
	; lay out the 6 fixed HUD sprites (2 minute digits, colon, 2 second
	; digits, play-state icon) left to right starting at PLAY_TIME_X/Y;
	; the actual digit values get filled in every frame by play_sprite
	STB temp, #eCoord::PLAY_TIME_X+(0*8)
	ldx #0
	:
		lda #eCoord::PLAY_TIME_Y-1
		sta oam+0, X
		lda #2
		sta oam+2, X
		lda temp
		sta oam+3, X
		clc
		adc #8
		sta temp
		inx
		inx
		inx
		inx
		cpx #(6*4)
		bcc :-
	lda temp
	sta oam+(5*4)+3
	STB oam+(2*4)+1, #eConst::SPRITE_COLON
	jmp play_sprite
.endproc

.proc mode_play
	; make sure nothing is making noise while the new track's screen loads
	lda #0
	sta $4015
	sta $4011
	; load the playback screen graphics and print the track's name
	lda #eScreen::PLAY
	jsr load_screen
	PPU_LATCH PPU_TILE(eCoord::PLAY_TRACK_X, eCoord::PLAY_TRACK_Y)
	lda track_choose
	asl
	tax
	lda dTrack::string_table+0, X
	sta ptr+0
	lda dTrack::string_table+1, X
	sta ptr+1
	jsr ppu_string
	jsr play_sprite_init

	jsr starfield_init

	jsr play_init
	; play_init has already called the NSF's INIT routine and restored $5FFF
	; back to the driver bank (via ramcode_return) so that we could safely
	; get back here into ROM. Now hand off to a tiny RAM-resident helper
	; that permanently pins $5FFF to this album's high bank and enters the
	; RAM-resident play_loop. NOTE: this pin+jump MUST happen from RAM, not
	; from here - the instruction that writes $5FFF is itself fetched from
	; the $F000-$FFFF window, so the moment it executes, that window shows
	; different (NSF) data; any following instruction fetched from that same
	; ROM window would then come from the NSF's own bank instead of our
	; code. Doing the pin+jump from RAM sidesteps that entirely, since RAM
	; is a separate address range unaffected by which PRG bank is selected.
	jmp ramcode_pin_and_enter_play_loop
.endproc

;
; player
;

.proc play_init
	; Reset state for a fresh INIT call. Per the NSF format's own
	; convention, zero page and $0200-$03FF are considered the NSF's own
	; scratch space and are expected to be cleared before every INIT -
	; well-behaved NSF sound engines rely on starting from a clean slate.
	;
	; "gamepad" (in zero page) is an exception: it's OUR variable, not the
	; NSF's, so its current value is saved before the clear and restored
	; right after, to avoid a spurious "everything just got released" edge
	; on the very next controller poll.
	lda gamepad
	pha
	lda #0
	tax
	:
		sta $00, X
		inx
		cpx #$FA
		bcc :-
	pla
	sta gamepad
	lda #0
	tax
	:
		sta $0200, X
		sta $0300, X
		inx
		bne :-
	; silence and reset the APU before handing control to the NSF's INIT
	lda #0
	tax
	:
		sta $4000, X
		inx
		cpx #$14
		bcc :-
	STB $4015, #$0F
	STB $4017, #$40
	; reset the driver's own per-track state
	lda #0
	sta frame
	sta sec0
	sta sec1
	sta min0
	sta min1
	sta play_paused
	sta play_secs+0
	sta play_secs+1
	sta debounce_a
	lda track_choose
	asl
	tax
	lda dTrack::length_table+0, X
	sta play_len+0
	lda dTrack::length_table+1, X
	sta play_len+1
	; figure out which NSF file (album) this track belongs to
	ldx track_choose
	lda dTrack::album_table, X
	sta play_album
	; self-modify the INIT call trampoline (in RAMCODE, so this is a
	; genuine, allowed code patch) to jump into this album's own INIT
	; routine instead of whichever album played last
	asl ; A = album * 2 (word-sized index into the tables below)
	tay
	lda dNSF::init_table+0, Y
	sta nsf_call_init_target+1
	lda dNSF::init_table+1, Y
	sta nsf_call_init_target+2
	; same patch for the PLAY call trampoline
	lda play_album
	asl
	tay
	lda dNSF::play_table+0, Y
	sta nsf_call_play_target+1
	lda dNSF::play_table+1, Y
	sta nsf_call_play_target+2
	; remember which physical bank must stay mapped at $F000-$FFFF for the
	; whole time this album's track is loaded (see play_bank_f000 above)
	ldx play_album
	lda dNSF::bankf000_table, X
	sta play_bank_f000
	; hand off to the RAM trampoline that sets up this album's PRG banks
	; and calls its INIT routine with the right song number in A
	ldx track_choose
	lda dTrack::song_table, X
	jmp ramcode_nsf_init
.endproc

;
; RAMCODE - small trampolines that must run from RAM rather than ROM,
; because they touch the $5FF8-$5FFF mapper bank-select registers that
; control what's currently visible in the $8000-$FFFF PRG window. Any
; instruction fetched from that window immediately after a write to it
; would come from whatever bank the write just selected - so code that
; changes the mapping and code that needs to keep running afterwards can't
; both live inside the window being changed. Living in RAM instead sidesteps
; the problem entirely, since RAM is a separate part of the address space.
;

.segment "RAMCODE"

.if (MAPPER = 31)
; Vector target used only if the CPU's reset line is ever asserted while an
; NSF's own bank happens to be mapped at $F000-$FFFF (see NSF_VECTORS
; further down) - forces the driver's own bank back in before continuing
; into the normal reset routine.
ramcode_reset:
	lda #$FF
	sta $5FFF
	jmp vec_reset
; Likewise, the NMI/IRQ vector targets used while an NSF's own bank is
; mapped at $F000-$FFFF. Deliberately tiny and side-effect-free (just
; bumps the frame counter) so it's safe to run no matter what ROM bank is
; currently selected elsewhere, or what the rest of the driver was doing
; when the interrupt happened.
ramcode_nmi:
	inc nmi_count
ramcode_irq:
	rti
.endif

; Calls the currently selected album's NSF INIT routine.
; Entry: A = song number to pass to INIT (NSF convention: 0-based).
;        play_album must already have been set by play_init.
; NOTE: this is a plain label, not a .proc, specifically so that the
; embedded jump target below (nsf_call_init_target) can be referenced and
; self-modified from other files/procs without needing "::" scope syntax.
ramcode_nsf_init:
	.if (::MAPPER = 31)
		pha
		; point ptr at this album's 8-byte PRG bank table
		; (dNSF::bank_table holds ALBUMS*8 bytes, 8 per album)
		lda play_album
		asl
		asl
		asl ; A = album * 8
		clc
		adc #<dNSF::bank_table
		sta ptr+0
		lda #>dNSF::bank_table
		adc #0
		sta ptr+1
		; copy those 8 bytes into the mapper's bank-select registers,
		; mapping this album's own PRG data across all of $8000-$FFFF
		ldy #0
		:
			lda (ptr), Y
			sta $5FF8, Y
			iny
			cpy #8
			bcc :-
		pla
	.endif
	ldx pal
	ldy #0
nsf_call_init_target:
	jsr $FFFF ; operand overwritten by play_init to point at this album's INIT
	jmp ramcode_return

; Calls the currently selected album's NSF PLAY routine (once per frame).
; Unlike ramcode_nsf_init, this does NOT touch $5FFF: by the time play_loop
; is running, $5FFF has already been permanently pinned to this album's high
; bank (by mode_play, see ramcode_pin_and_enter_play_loop below), and it
; stays that way for as long as the track is loaded. That's what guarantees
; DPCM/DMC sample DMA always sees the correct sample data, instead of
; occasionally reading whatever ROM bank the driver happened to switch in
; for its own purposes between PLAY calls.
ramcode_nsf_play:
	lda #0
	tax
	tay
nsf_call_play_target:
	jsr $FFFF ; operand overwritten by play_init to point at this album's PLAY
	rts

.proc ramcode_return
	.if (::MAPPER = 31)
		; restore the driver's own bank at $F000-$FFFF so that code
		; immediately after the call site (which lives in ROM) is safe
		; to execute again
		lda #$FF
		sta $5FFF
	.endif
	rts
.endproc

; Entered via jmp (not jsr - no return is expected) from mode_play once
; play_init/ramcode_nsf_init have finished calling the NSF's INIT routine.
; From this point until the player leaves the current track, $5FFF stays
; fixed on this album's own high bank; see the comment on mode_play for why
; the pin and the jump into play_loop both have to happen from here (RAM)
; rather than from mode_play's own ROM code.
ramcode_pin_and_enter_play_loop:
	.if (::MAPPER = 31)
		lda play_bank_f000
		sta $5FFF
	.endif
	jmp play_loop

;
; PLAYCODE - like RAMCODE above, this segment is copied into RAM at boot
; and runs from there. Everything here executes on every frame *while a
; track is loaded and $5FFF is pinned*, so none of it can live in ROM: any
; of these routines being fetched from the shared $F000 window would be
; reading whatever the currently-playing NSF's own bank happens to contain
; there instead of driver code.
;

.segment "PLAYCODE"

; Moves every starfield sprite (see starfield_init above) one step further
; left, at a per-sprite fixed speed derived from its own OAM position so
; that different stars drift at different rates. X position wraps around
; via ordinary 8-bit underflow when it passes 0 - there's no explicit
; "respawn on the right edge" logic, the wraparound does that for free.
.proc starfield_update
	ldx #24 ; first star sprite's OAM byte offset (see starfield_init)
@update_loop:
	; derive a per-sprite speed of 1-8 pixels/frame from bits 2-4 of the
	; sprite's own OAM byte offset (X), so each star's speed is fixed for
	; its lifetime but differs from its neighbours
	txa
	and #%00011100
	lsr
	lsr
	sta temp
	; subtract that speed (plus a constant 1, so the range is 1-8 rather
	; than 0-7) from the sprite's X coordinate
	lda oam+3, X
	sec
	sbc temp
	sbc #1
	sta oam+3, X
	; next sprite (4 OAM bytes); loop wraps back to 0 (and stops) after
	; covering the rest of OAM, same as starfield_init's loop
	inx
	inx
	inx
	inx
	bne @update_loop
	rts
.endproc

; Busy-waits until the next NMI (vertical blank) increments nmi_count,
; i.e. waits for the start of a new frame. Also makes sure NMI generation
; is actually enabled first, in case something turned it off.
.proc wait_nmi
	lda #%10000000
	sta $2000
	lda nmi_count
	:
		cmp nmi_count
		beq :-
	rts
.endproc

; Waits for the next frame, then turns rendering off. Used while the PPU
; nametable/palette/CHR data needs to be rewritten outside of vblank-safe
; $2007 access patterns (see load_screen).
.proc render_off
	jsr wait_nmi
	lda #0
	sta $2001
	rts
.endproc

; Waits for the next frame, then does the once-per-frame PPU update: OAM
; DMA (pushes the whole shadow "oam" buffer to the PPU), resets the scroll
; position, and (re-)enables background + sprite rendering.
.proc render_on
	jsr wait_nmi
	lda #0
	sta $2003
	lda #>oam
	sta $4014
	lda $2002
	lda #0
	sta $2005
	sta $2005
	lda #%00011110
	sta $2001
	rts
.endproc

; Reads the standard NES controller into "gamepad" (8 bits: A, B, Select,
; Start, Up, Down, Left, Right, in that read order, packed so that
; PAD_A..PAD_R above line up with the resulting bits), and computes
; "gamepad_new" = buttons that just became pressed since the previous poll.
;
; The 8-bit read is done twice in a row and retried until both reads agree.
; This matters because DMC/DPCM sample playback drives the APU's DMA
; engine, which can steal CPU cycles at unpredictable times - most notably
; during the one-off "restart" DMA stall when a DPCM sample is first
; enabled - and a stolen cycle landing in the middle of a $4016 read can
; corrupt a single bit of that read. Comparing two independent reads and
; retrying on mismatch filters that out.
.proc gamepad_poll
	lda gamepad
	sta gamepad_last
	; strobe the controller (latch its current button state)
	lda #1
	sta $4016
	lda #0
	sta $4016
	; shift in 8 bits, using the top bit of "gamepad" as an end-of-read
	; marker: it starts as the only 1 bit, and gets shifted out (making
	; the byte go from negative to non-negative) exactly after 8 rounds
	lda #%10000000
	sta gamepad
	:
		lda $4016
		and #%00000001
		cmp #%00000001
		ror gamepad
	bcc :-
@reread:
	; repeat the exact same read, compare against the first attempt, and
	; go back for a third (fourth, ...) attempt if they disagree
	lda gamepad
	pha
	lda #1
	sta $4016
	lda #0
	sta $4016
	lda #%10000000
	sta gamepad
	:
		lda $4016
		and #%00000001
		cmp #%00000001
		ror gamepad
	bcc :-
	pla
	cmp gamepad
	bne @reread
	; a bit is "newly pressed" if it's set now but wasn't set last poll
	lda gamepad_last
	eor gamepad
	and gamepad
	sta gamepad_new
	rts
.endproc

; Updates the 6 fixed HUD sprites (see play_sprite_init) with the current
; elapsed-time digits and the correct play-state icon (playing / paused /
; "play all" vs "loop" indicator).
.proc play_sprite
	lda min1
	clc
	adc #eConst::SPRITE_ZERO
	sta oam+(0*4)+1
	lda min0
	clc
	adc #eConst::SPRITE_ZERO
	sta oam+(1*4)+1
	lda sec1
	clc
	adc #eConst::SPRITE_ZERO
	sta oam+(3*4)+1
	lda sec0
	clc
	adc #eConst::SPRITE_ZERO
	sta oam+(4*4)+1
	; choose the icon: stopped > paused > (continue-to-next vs loop)
	lda play_paused
	cmp #2
	bcc :+
		lda #eConst::SPRITE_STOP
		jmp @indicator
	:
	cmp #1
	bcc :+
		lda #eConst::SPRITE_PAUSE
		jmp @indicator
	:
	lda play_through
	beq :+
		lda #eConst::SPRITE_PLAY_ALL
		jmp @indicator
	:
		lda #eConst::SPRITE_PLAY
		;jmp @indicator
	@indicator:
	sta oam+(5*4)+1
	rts
.endproc

; Entry: A = the play_paused value to switch to (0 = playing, 1 = paused).
; No-op if that's already the current state. Also mutes/unmutes the APU so
; a paused track is actually silent rather than just frozen on-screen.
.proc play_pause
	cmp play_paused
	bne :+
		rts
	:
	cmp #0
	bne @pause
@unpause:
	lda #$0F
	sta $4015
	lda #0
	sta play_paused
	rts
@pause:
	lda #0
	sta $4015
	lda #1
	sta play_paused
	rts
.endproc

; Called once per frame from play_loop while a track is actively playing
; (play_paused = 0). Calls the NSF's own PLAY routine, then advances the
; on-screen MM:SS clock once every "fps" calls (i.e. once per real second),
; and finally checks whether the track has reached its declared length.
; If it has, play_secs is clamped to play_len, the APU is silenced, and
; play_paused is set to 2 to signal "just finished" - play_loop notices
; that state immediately afterwards (within the same frame) and either
; restarts this track (loop mode) or moves on to the next one (continue
; mode), so this "2" state is never actually shown to the player waiting
; for input; it's purely a same-frame handoff.
.proc play_play
	lda play_paused
	beq :+
		rts
	:
	jsr ramcode_nsf_play
	inc frame
	lda frame
	cmp fps
	bcc @second_end
		lda #0
		sta frame
		inc sec0
		lda sec0
		cmp #10
		bcc :+
		lda #0
		sta sec0
		inc sec1
		lda sec1
		cmp #6
		bcc :+
		lda #0
		sta sec1
		inc min0
		lda min0
		cmp #10
		bcc :+
		lda #0
		sta min0
		inc min1
		lda min1
		cmp #10
		bcc :+
		; clamp the display at 99:59 rather than rolling over to 00:00 -
		; in practice play_secs/play_len will have already ended the
		; track long before a real NSF track runs this long
		lda #9
		sta min1
		sta min0
		sta sec0
		lda #5
		sta sec1
		:
		; advance the track-length counter and compare it against this
		; track's declared length (both 16-bit, compared as unsigned via
		; the standard CMP/SBC idiom)
		inc play_secs+0
		bne :+
			inc play_secs+1
		:
		lda play_secs+0
		cmp play_len+0
		lda play_secs+1
		sbc play_len+1
		bcc @second_end
		lda play_len+0
		sta play_secs+0
		lda play_len+1
		sta play_secs+1
		lda #2
		sta play_paused
		lda #0
		sta $4015
	@second_end:
	rts
.endproc

; The main playback loop. Entered (via jmp, not jsr) from
; ramcode_pin_and_enter_play_loop once $5FFF has been pinned to the
; playing album's own bank. Runs once per frame for as long as the current
; track stays loaded. Whenever it needs to leave - back to the main list,
; or to reload a different track - it must restore $5FFF to the driver
; bank ($FF) first, since mode_list/mode_play are ROM code living in the
; very same $F000-$FFFF window that's currently pinned to the NSF.
.proc play_loop
@loop:
	jsr play_sprite

	jsr starfield_update

	jsr render_on
	jsr play_play
	jsr gamepad_poll
	lda play_paused
	cmp #2
	bcs @track_finished
@playing:
	; START = pause/unpause
	lda gamepad_new
	and #(PAD_START)
	beq :+
		lda play_paused
		eor #1
		jsr play_pause
	:
	; A = toggle between "loop this track" and "continue to the next
	; track" (wrapping at the end of the list) once it ends.
	;
	; This is debounced: a real NES controller read can occasionally
	; register a single spurious extra "just pressed" edge for A right
	; around the same moment DPCM playback starts (see the comment on
	; gamepad_poll's double-read for why), which without any further
	; protection could flip play_through twice in quick succession and
	; make a single tap look like it did nothing. debounce_a counts down
	; from 15 frames (~250ms at 60fps) after a genuine toggle and simply
	; ignores A while it's nonzero, well past the length of a single
	; spurious extra edge but short enough that a deliberate second tap
	; from the player still feels responsive.
	lda debounce_a
	beq @check_a
		dec debounce_a
		jmp @skip_a
	@check_a:
	lda gamepad_new
	and #(PAD_A)
	beq @skip_a
		lda play_through
		eor #1
		sta play_through
		lda #15
		sta debounce_a
	@skip_a:
	; B = cancel, back to the main list
	lda gamepad_new
	and #(PAD_B)
	beq :+
		jmp @to_list
	:
	; L/U = previous track, R/D = next track (clamped at the ends of the
	; list - unlike the natural end-of-track handling below, skipping
	; manually with the d-pad does not wrap around)
	lda gamepad_new
	and #(PAD_L | PAD_U)
	beq :+
		lda track_choose
		beq :+
		dec track_choose
		jmp @to_play
	:
	lda gamepad_new
	and #(PAD_R | PAD_D)
	beq :+
		lda track_choose
		cmp #(eNSF::TRACKS-1)
		bcs :+
		inc track_choose
		jmp @to_play
	:
	jmp @loop
@track_finished:
	; play_play just detected the track reached its declared length; act
	; on it immediately, with no button press required
	lda play_through
	beq @to_play
		; "continue" mode: advance to the next track, wrapping back to
		; the first track after the last one
		lda track_choose
		cmp #(eNSF::TRACKS-1)
		bcs @wrap
		inc track_choose
		jmp @to_play
	@wrap:
		lda #0
		sta track_choose
		;jmp @to_play
		; "loop" mode falls straight through to @to_play with
		; track_choose unchanged, so mode_play reloads the same track
@to_list:
	.if (::MAPPER = 31)
		lda #$FF
		sta $5FFF
	.endif
	jmp mode_list
@to_play:
	.if (::MAPPER = 31)
		lda #$FF
		sta $5FFF
	.endif
	jmp mode_play
.endproc

.segment "CODE"

.import __RAMCODE_SIZE__
.import __RAMCODE_LOAD__
.import __RAMCODE_RUN__

; Copies the RAMCODE segment from ROM to its RAM run address, once at boot.
; Limited to 256 bytes because it uses a single 8-bit index; RAMCODE is
; small (just the trampolines above) so this is fine. See load_playcode
; below for the larger, unlimited-size version used for PLAYCODE.
.proc load_ramcode
	.assert (__RAMCODE_SIZE__ < 256), error, "RAMCODE segment is too large."
	.assert (__RAMCODE_SIZE__ > 0), error, "RAMCODE segment empty."
	ldx #0
	:
		lda __RAMCODE_LOAD__, X
		sta __RAMCODE_RUN__, X
		inx
		cpx #<__RAMCODE_SIZE__
		bcc :-
	rts
.endproc

.import __PLAYCODE_SIZE__
.import __PLAYCODE_LOAD__
.import __PLAYCODE_RUN__

; Copies the PLAYCODE segment from ROM to its RAM run address, once at
; boot. Unlike load_ramcode this supports sizes of 256 bytes or more, by
; copying whole 256-byte pages first and then any leftover partial page.
; NOTE: this routine is itself part of the ROM-resident CODE segment, so it
; can NOT self-modify its own instructions (a write to a ROM address is
; simply ignored by real hardware) - that's why it uses a second zero page
; pointer (dst_ptr) for the destination, exactly the way "ptr" is used for
; the source, instead of the self-modifying-operand trick used elsewhere.
.proc load_playcode
	.assert (__PLAYCODE_SIZE__ > 0), error, "PLAYCODE segment empty."
	lda #<__PLAYCODE_LOAD__
	sta ptr+0
	lda #>__PLAYCODE_LOAD__
	sta ptr+1
	lda #<__PLAYCODE_RUN__
	sta dst_ptr+0
	lda #>__PLAYCODE_RUN__
	sta dst_ptr+1
	ldx #>__PLAYCODE_SIZE__ ; number of full 256-byte pages to copy
	ldy #0
@full_pages:
	cpx #0
	beq @partial
		:
			lda (ptr), Y
			sta (dst_ptr), Y
			iny
			bne :-
		inc ptr+1
		inc dst_ptr+1
		dex
		jmp @full_pages
@partial:
	ldy #0
	cpy #<__PLAYCODE_SIZE__
	beq @done
	:
		lda (ptr), Y
		sta (dst_ptr), Y
		iny
		cpy #<__PLAYCODE_SIZE__
		bcc :-
@done:
	rts
.endproc

;
; main
;

.proc main
	; clear the second nametable; the driver only ever uses one nametable
	; at a time; this just avoids showing PPU power-on garbage on it
	; before the first real screen is drawn
	PPU_LATCH $2400
	ldy #4
	lda #0
	tax
	:
		sta $2007
		inx
		bne :-
		dey
		bne :-
	jmp mode_list
.endproc

;
; various helper functions
;

; Reads one byte from (ptr),Y and advances ptr by one (with Y left at 0,
; the usual convention used everywhere else in this file). Returns with
; the zero flag reflecting whether the byte just read was 0.
; Entry: ptr = source address, Y = 0.
.proc read_ptr
	lda (ptr), Y
	inc ptr+0
	bne :+
		inc ptr+1
	:
	cmp #0
	rts
.endproc

; Unpacks one RLE-compressed PPU data chunk (as produced by eznsf.py's
; pack_ppu) and streams the decompressed bytes straight out to $2007 - the
; caller is responsible for having already set up the PPU address via
; PPU_LATCH beforehand.
; Entry: A = ePPU index of the chunk to unpack.
; RLE stream format (see eznsf.py's pack_ppu/unpack_ppu for the encoder):
;   1-255 = that many literal bytes follow, then back to step 1
;   0, 1-255 = that many repeats of one following byte, then back to step 1
;   0, 0 = end of stream
.proc ppu_unpack
	asl
	tax
	lda dPPU::data_table+0, X
	sta ptr+0
	lda dPPU::data_table+1, X
	sta ptr+1
	ldy #0
	@rle_loop:
		jsr read_ptr
		beq @compressed
	@uncompressed:
		tax
		:
			jsr read_ptr
			sta $2007
			dex
			bne :-
		jmp @rle_loop
	@compressed:
		jsr read_ptr
		beq @finished
		tax
		jsr read_ptr
		:
			sta $2007
			dex
			bne :-
		jmp @rle_loop
	@finished:
		rts
.endproc

; Streams a null-terminated (or newline-terminated) string straight out to
; $2007, without advancing past the terminator - the caller decides what
; to do next (stop, or move to the next screen row and keep printing).
; Entry: ptr = string address, Y = 0.
.proc ppu_string
	ldy #0
	:
		jsr read_ptr
		beq :+
		cmp #13 ; newline: stop here too, but let the caller see it
		beq :+
		sta $2007
		jmp :-
	:
	rts
.endproc

; Loads one full screen's worth of graphics: PRG banks holding the PPU
; data, then (with rendering off) palettes, background CHR, sprite CHR,
; and the nametable itself.
; Entry: A = eScreen index of the screen to load.
.proc load_screen
	sta temp
	jsr load_ppu_banks
	jsr render_off
	; palettes first, so the rest can complete within the vblank we're
	; already borrowing time from (all the actual writes still happen with
	; rendering off, so there's no strict deadline, but this ordering
	; matches the original tool's behaviour)
	PPU_LATCH $3F00
	ldx temp
	lda dScreen::pal0_table, X
	jsr ppu_unpack
	ldx temp
	lda dScreen::pal1_table, X
	jsr ppu_unpack
	PPU_LATCH $0000
	ldx temp
	lda dScreen::chr0_table, X
	jsr ppu_unpack
	ldx temp
	lda dScreen::chr1_table, X
	jsr ppu_unpack
	PPU_LATCH $2000
	ldx temp
	lda dScreen::name_table, X
	jsr ppu_unpack
	rts
.endproc

; Hides every sprite by setting all 64 OAM Y-coordinates to $FF
; (off-screen), leaving tile/attribute/X bytes untouched.
.proc oam_clear
	lda #$FF
	ldx #0
	:
		sta oam, X
		inx
		inx
		inx
		inx
		bne :-
	rts
.endproc

; Advances the current PPU write address (held in temp) down by exactly
; one nametable row (32 tiles) and re-latches $2006 to it. Used between
; ppu_string calls when printing multiple lines to the same screen.
.proc ppu_temp_line
	lda temp+0
	clc
	adc #<32
	sta temp+0
	lda temp+1
	adc #>32
	sta temp+1
	sta $2006
	lda temp+0
	sta $2006
	rts
.endproc

;
; vector handlers
;

.proc vec_reset
	; standard NES power-on/reset boilerplate
	sei       ; set interrupt flag (unnecessary, unless reset is called from code)
	cld       ; disable decimal mode
	ldx #$40
	stx $4017 ; disable APU frame IRQ
	ldx #$ff
	txs       ; set up stack
	ldx #$00
	stx $2000 ; disable NMI
	stx $2001 ; disable rendering
	stx $4010 ; disable DPCM IRQ
	stx $4015 ; mute APU
	;
	bit $2002 ; clear the PPU's vblank flag
	; wait for the PPU to finish warming up (first vblank)
	:
		bit $2002
		bpl :-
	; zero all of internal RAM, including the driver's own reserved
	; range ($0400-$07FF) - anything the driver needs to survive this
	; gets (re)written right afterwards by load_ramcode/load_playcode and
	; the NTSC/PAL detection below
	ldx #$00
	:
		lda #$00
		sta $0000, X
		sta $0100, X
		sta $0200, X
		sta $0300, X
		sta $0400, X
		sta $0500, X
		sta $0600, X
		sta $0700, X
		inx
		bne :-
	; wait for the PPU's second vblank (now it's fully warmed up)
	:
		bit $2002
		bpl :-
	; copy the RAM-resident code segments into place
	jsr load_ramcode
	jsr load_playcode
	; detect NTSC vs PAL (only actually measures anything if the NSF's
	; region byte requested it - see detect_region further down)
	lda $2002
	lda #%10000000
	sta $2000
	jsr detect_region
	bne :+
		lda #60
		sta fps
		lda #0
		jmp :++
	:
		lda #50
		sta fps
		lda #1
	:
	sta pal
	; NMI now stays enabled for the rest of the program's life
	jmp main
.endproc

vec_nmi:
	inc nmi_count
vec_irq:
	rti

;
; mapper 31 (bankswitched) build
;

.if (MAPPER = 31)

dPPU::data_base = $8000
INES_CHR = 0 ; no CHR ROM - graphics are decompressed into CHR RAM at runtime

.segment "CODE"
; Maps the PPU graphics banks into windows 0-6 ($8000-$EFFF), leaving
; window 7 ($F000-$FFFF, register $5FFF) untouched - that one is managed
; separately by the NSF-playback code (see play_bank_f000/ramcode_*).
; The PPU bank data always immediately follows the last NSF album's banks
; (eNSF::BANKS), so this just needs to write 7 consecutive bank numbers.
.proc load_ppu_banks
	ldx #eNSF::BANKS
	stx $5FF8
	inx
	stx $5FF9
	inx
	stx $5FFA
	inx
	stx $5FFB
	inx
	stx $5FFC
	inx
	stx $5FFD
	inx
	stx $5FFE
	rts
.endproc

; The high bank for album 0 only - raw NSF bank-7 data followed by the
; NSF-context vectors below. This is the one high bank ld65 actually links
; directly (see eznsf.py); every other album's equivalent high bank is
; synthesized by eznsf.py itself by reusing the vector bytes written here.
.segment "NSF_F000"
.incbin NSF_F000

; Reset/NMI/IRQ vectors as seen while an NSF's own high bank (rather than
; the driver's own bank) happens to be mapped at $F000-$FFFF - i.e. these
; are the vectors actually baked into the tail of every album's high bank,
; not the "real" vectors down in the VECTORS segment near the end of this
; file (those only apply while the driver's own bank is selected).
.segment "NSF_VECTORS"
.addr ramcode_nmi
.addr ramcode_reset
.addr ramcode_irq

;
; mapper 0 (NROM) build - only reachable when the whole ROM is a single,
; non-bankswitched NSF file (NROM 1 in album.txt); everything fits in one
; fixed 32KB PRG + 8KB CHR image, so none of the mapper-31-specific
; bank-pinning machinery above is needed here.
;

.else

.segment "CODE"
ppu_nrom:
	.incbin PPU_NROM_BIN
	dPPU::data_base = ppu_nrom

.segment "TILES"
	.incbin NROM_CHR0
	.incbin NROM_CHR1
	INES_CHR = 1 ; real CHR ROM this time, not CHR RAM

.segment "NSF"
nsf_nrom:
	.incbin NSF_NROM_BIN
	.assert (nsf_nrom = $8000), error, "nsf_nrom.bin loaded at the wrong address?"

.segment "CODE"
; nothing to do - NROM has no banks to switch
.proc load_ppu_banks
	rts
.endproc

.endif

;
; NTSC/PAL region detection
;

.if (eNSF::REGION & 2) ; the NSF(s) requested dual-region auto-detection
	.segment "ALIGN"
	.proc detect_region
		; Measures how many NMIs occur while counting a large, fixed
		; number of loop iterations; NTSC and PAL run this loop at
		; different real-world speeds relative to their own vblank rate,
		; so the measured count reliably tells them apart.
		; Based on the detection routine by Damian Yerrick:
		; http://wiki.nesdev.com/w/index.php/Detect_TV_system
		.align 32
		ldx #0
		ldy #0
		lda nmi_count
		@wait1:
			cmp nmi_count
			beq @wait1
			lda nmi_count
		@wait2:
			inx
			bne :+
				iny
			:
			cmp nmi_count
			beq @wait2
		tya
		sec
		sbc #10
		; result is 0 for NTSC, nonzero for PAL/Dendy/unknown
		rts
	.endproc
.else
	; no auto-detection requested: just use the fixed region bit from the
	; (first, if there are several) NSF's own header
	.segment "CODE"
	.proc detect_region
		lda #(eNSF::REGION & 1)
		rts
	.endproc
.endif

;
; hardware vectors (used only while the driver's own bank, not an NSF's
; high bank, is mapped at $F000-$FFFF - see the NSF_VECTORS segment above
; for the other case)
;

.segment "VECTORS"
.addr vec_nmi
.addr vec_reset
.addr vec_irq

;
; iNES header
;

.segment "HEADER"
INES_MAPPER = MAPPER
INES_MIRROR = 1
INES_SRAM   = 0
.byte 'N', 'E', 'S', $1A ; ID
.byte BANKS / 4 ; PRG size in 16KB units (BANKS is a count of 4KB banks)
.byte INES_CHR ; CHR RAM (mapper 31) or real CHR ROM (NROM) size in 8KB units
.byte INES_MIRROR | (INES_SRAM << 1) | ((INES_MAPPER & $f) << 4)
.byte (INES_MAPPER & %11110000)
.byte $0, $0, $0, $0, $0, $0, $0, $0 ; padding

; end of file
