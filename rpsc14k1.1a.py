#!/usr/bin/env python3
"""
RPSC1 - PlayStation 1 Emulator
Synthesized BIOS (no external file required) + Bleem!-style GUI.
Now with CD-ROM emulation to load real disc images.
"""

import os
import sys
import struct
import threading
import queue
import time
import numpy as np
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ----------------------------------------------------------------------
# Memory map constants
# ----------------------------------------------------------------------
KSEG0_BASE   = 0x80000000
KSEG1_BASE   = 0xA0000000
BIOS_SIZE    = 512 * 1024
RAM_SIZE     = 2 * 1024 * 1024
SCRATCH_SIZE = 1024

# ----------------------------------------------------------------------
# CD-ROM controller emulation
# ----------------------------------------------------------------------
class CDROM:
    """Emulates the PlayStation CD-ROM controller (CXD1815Q)."""
    # Register offsets (relative to base 0x1F801800)
    REG_STATUS   = 0x00
    REG_COMMAND  = 0x04
    REG_READ     = 0x08
    REG_INTR     = 0x0C
    REG_SELECT   = 0x10
    REG_REQUEST  = 0x14
    REG_RESPONSE = 0x18

    # Commands (simplified)
    CMD_GETSTATUS   = 0x01
    CMD_READ_SECTOR = 0x02
    CMD_STOP        = 0x03

    # Status bits
    STATUS_READY = 0x20
    STATUS_BUSY  = 0x40

    def __init__(self):
        self.image = None          # file object for the disc image
        self.tracks = []           # list of (lba_start, lba_end, mode, file_offset)
        self.current_sector = 0
        self.sector_buffer = bytearray(2352)   # raw sector buffer
        self.interrupt_pending = False
        self.command = 0
        self.status = self.STATUS_READY
        self.regs = bytearray(0x20)   # scratch for read/write registers

    def load_cue(self, cue_path):
        """Parse .cue sheet and open the corresponding .bin file(s)."""
        try:
            with open(cue_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except:
            return False

        bin_path = None
        track_start = 0
        self.tracks.clear()

        for line in lines:
            line = line.strip()
            if line.startswith('FILE'):
                parts = line.split('"')
                if len(parts) >= 2:
                    bin_path = os.path.join(os.path.dirname(cue_path), parts[1])
            elif line.startswith('TRACK'):
                parts = line.split()
                if len(parts) >= 2 and parts[2] == 'MODE1/2352':
                    # Data track – we assume it's the first data track
                    if bin_path and os.path.exists(bin_path):
                        self.image = open(bin_path, 'rb')
                        # We'll assume the data track starts at LBA 0 (sector 0 of bin)
                        self.tracks.append((track_start, None, 'data', 0))
                        return True
        return False

    def load_bin(self, bin_path):
        """Simple single-track .bin file (Mode 1 2352 bytes/sector)."""
        if os.path.exists(bin_path) and bin_path.lower().endswith('.bin'):
            self.image = open(bin_path, 'rb')
            self.tracks.append((0, None, 'data', 0))
            return True
        return False

    def read_sector_raw(self, lba):
        """Read a full 2352-byte sector from the disc image."""
        if self.image is None:
            return b'\x00' * 2352

        # Assume the first data track starts at LBA 0
        offset = lba * 2352
        self.image.seek(offset)
        data = self.image.read(2352)
        if len(data) < 2352:
            data += b'\x00' * (2352 - len(data))
        return data

    def write_register(self, addr, value):
        """Handle writes to CD-ROM registers."""
        reg = addr - 0x1F801800
        if reg == self.REG_COMMAND:
            self.command = value & 0xFF
            self._execute_command()
        elif reg == self.REG_SELECT:
            self.current_sector = (self.current_sector & 0xFFFFFF00) | value
        elif reg == self.REG_REQUEST:
            self.current_sector = (self.current_sector & 0xFFFF00FF) | (value << 8)
        elif reg == self.REG_RESPONSE:
            self.current_sector = (self.current_sector & 0xFF00FFFF) | (value << 16)
        # Other registers are ignored in this simple emulation

    def read_register(self, addr):
        """Handle reads from CD-ROM registers."""
        reg = addr - 0x1F801800
        if reg == self.REG_STATUS:
            return self.status
        elif reg == self.REG_READ:
            if len(self.sector_buffer) > 0:
                return self.sector_buffer[0]   # simplistic; games would read byte by byte
            return 0
        elif reg == self.REG_INTR:
            if self.interrupt_pending:
                self.interrupt_pending = False
                return 2   # CD-ROM interrupt code (simplified)
            return 0
        return 0

    def _execute_command(self):
        """Process a CD-ROM command."""
        if self.command == self.CMD_GETSTATUS:
            self.status = self.STATUS_READY
        elif self.command == self.CMD_READ_SECTOR:
            # Read the requested sector into buffer
            self.sector_buffer[:] = self.read_sector_raw(self.current_sector)
            self.status = self.STATUS_READY
            self.interrupt_pending = True
        elif self.command == self.CMD_STOP:
            # Stop spinning (no action needed)
            pass

    def update(self):
        """Called each frame to raise interrupts if pending."""
        if self.interrupt_pending:
            # In a real emulator, this would set an IRQ flag in the interrupt controller
            # For simplicity, we'll just clear it here; actual games will poll REG_INTR.
            pass

    def close(self):
        """Close the disc image file if open."""
        if self.image:
            self.image.close()
            self.image = None


# ----------------------------------------------------------------------
# Synthesized BIOS (High-Level Emulation)
# ----------------------------------------------------------------------
class SynthesizedBIOS:
    """Emulates the PS1 BIOS functions using Python code."""

    def __init__(self, memory, cdrom):
        self.mem = memory
        self.cpu = None
        self.cdrom = cdrom
        self.handlers = {
            0xBFC00000: self._boot,
            0xBFC00100: self._init,
            0xBFC00200: self._exception,
            0xBFC00300: self._syscall,
            0xBFC01000: self._cdrom_init,
            0xBFC01010: self._cdrom_read,
            0xBFC01020: self._cdrom_stop,
            0xBFC02000: self._graphics_init,
            0xBFC02010: self._graphics_put,
            0xBFC03000: self._pad_read,
        }

    def attach_cpu(self, cpu):
        self.cpu = cpu

    def execute(self, pc):
        if pc in self.handlers:
            self.handlers[pc]()
            return True
        return False

    # -- BIOS HLE stubs --------------------------------------------------
    def _boot(self):
        self.cpu.regs[29] = 0x801FFF00
        self.cpu.regs[31] = 0x80030000
        self.cpu.pc = 0x80030000

    def _init(self):
        # Basic hardware init
        self.mem.write32(0x1F801000, 0x1F000000)
        self.mem.write32(0x1F801070, 0x000003E7)
        self.cpu.pc = 0xBFC00200

    def _exception(self):
        self.cpu.pc = self.cpu.regs[31]

    def _syscall(self):
        self.cpu.pc = self.cpu.regs[31]

    def _cdrom_init(self):
        # Reset CDROM hardware
        # Write to the CDROM registers to set up
        self.cdrom.status = CDROM.STATUS_READY
        self.cpu.pc = self.cpu.regs[31]

    def _cdrom_read(self):
        # This function is called by the BIOS to read a sector.
        # In real hardware, this would send a command to the CDROM controller.
        # We'll simulate by immediately reading the sector into RAM.
        # The arguments are passed in registers:
        # $a0 = destination address in RAM
        # $a1 = LBA (sector number)
        dest = self.cpu.regs[4]   # $a0
        lba = self.cpu.regs[5]    # $a1
        # Read sector into buffer
        data = self.cdrom.read_sector_raw(lba)
        # Copy to RAM
        for i in range(2352):
            self.mem.write8(dest + i, data[i] if i < len(data) else 0)
        # Return success
        self.cpu.regs[2] = 0   # $v0 = 0
        self.cpu.pc = self.cpu.regs[31]

    def _cdrom_stop(self):
        self.cdrom.write_register(0x1F801800 + CDROM.REG_COMMAND, CDROM.CMD_STOP)
        self.cpu.pc = self.cpu.regs[31]

    def _graphics_init(self):
        self.mem.write32(0x1F801100, 0x00000000)
        self.cpu.pc = self.cpu.regs[31]

    def _graphics_put(self):
        self.cpu.pc = self.cpu.regs[31]

    def _pad_read(self):
        self.cpu.regs[2] = 0xFFFF
        self.cpu.pc = self.cpu.regs[31]


# ----------------------------------------------------------------------
# CPU: MIPS R3000A interpreter
# ----------------------------------------------------------------------
class MIPS_CPU:
    def __init__(self, memory, bios):
        self.mem = memory
        self.bios = bios
        self.regs = [0] * 32
        self.pc = 0xBFC00000
        self.lo = 0
        self.hi = 0
        self.cycles = 0
        self.cop0 = [0] * 32
        self.cop0[12] = 0x10000000
        self.exception = None

    def load32(self, addr):  return self.mem.read32(addr)
    def store32(self, addr, val): self.mem.write32(addr, val)
    def load16(self, addr):  return self.mem.read16(addr)
    def store16(self, addr, val): self.mem.write16(addr, val)
    def load8(self, addr):   return self.mem.read8(addr)
    def store8(self, addr, val):  self.mem.write8(addr, val)

    def raise_exception(self, code, cause=0):
        self.exception = (code, cause)

    def step(self):
        if self.bios.execute(self.pc):
            return
        if self.exception:
            self.exception = None
            return

        inst = self.load32(self.pc)
        self.pc += 4
        op = (inst >> 26) & 0x3F

        if   op == 0x00: self._special(inst)
        elif op == 0x01: self._regimm(inst)
        elif op == 0x02: self._j(inst)
        elif op == 0x03: self._jal(inst)
        elif op == 0x04: self._beq(inst)
        elif op == 0x05: self._bne(inst)
        elif op == 0x06: self._blez(inst)
        elif op == 0x07: self._bgtz(inst)
        elif op == 0x08: self._addi(inst)
        elif op == 0x09: self._addiu(inst)
        elif op == 0x0A: self._slti(inst)
        elif op == 0x0B: self._sltiu(inst)
        elif op == 0x0C: self._andi(inst)
        elif op == 0x0D: self._ori(inst)
        elif op == 0x0E: self._xori(inst)
        elif op == 0x0F: self._lui(inst)
        elif op == 0x10: self._cop0(inst)
        elif op == 0x12: pass                          # COP2 (GTE) stub
        elif 0x20 <= op <= 0x2F: self._load_store(inst)

        self.regs[0] = 0          # $zero is always 0
        self.cycles += 1

    # -- helpers --
    @staticmethod
    def _sext16(v):
        return v if (v & 0x8000) == 0 else v - 0x10000

    @staticmethod
    def _sext32(v):
        return v if (v & 0x80000000) == 0 else v - 0x100000000

    # -- SPECIAL (opcode 0x00) -------------------------------------------
    def _special(self, inst):
        func = inst & 0x3F
        rs = (inst >> 21) & 0x1F
        rt = (inst >> 16) & 0x1F
        rd = (inst >> 11) & 0x1F
        sa = (inst >> 6)  & 0x1F

        if   func == 0x00: self.regs[rd] = (self.regs[rt] << sa) & 0xFFFFFFFF
        elif func == 0x02: self.regs[rd] = (self.regs[rt] & 0xFFFFFFFF) >> sa
        elif func == 0x03:
            v = self._sext32(self.regs[rt] & 0xFFFFFFFF)
            self.regs[rd] = (v >> sa) & 0xFFFFFFFF
        elif func == 0x04: self.regs[rd] = (self.regs[rt] << (self.regs[rs] & 0x1F)) & 0xFFFFFFFF
        elif func == 0x06: self.regs[rd] = (self.regs[rt] & 0xFFFFFFFF) >> (self.regs[rs] & 0x1F)
        elif func == 0x07:
            v = self._sext32(self.regs[rt] & 0xFFFFFFFF)
            self.regs[rd] = (v >> (self.regs[rs] & 0x1F)) & 0xFFFFFFFF
        elif func == 0x08: self.pc = self.regs[rs] & 0xFFFFFFFF
        elif func == 0x09:
            self.regs[rd] = self.pc & 0xFFFFFFFF
            self.pc = self.regs[rs] & 0xFFFFFFFF
        elif func == 0x0C: self.raise_exception(0x08)
        elif func == 0x0D: self.raise_exception(0x09)
        elif func == 0x10: self.regs[rd] = self.hi
        elif func == 0x11: self.hi = self.regs[rs]
        elif func == 0x12: self.regs[rd] = self.lo
        elif func == 0x13: self.lo = self.regs[rs]
        elif func == 0x18:
            a = self._sext32(self.regs[rs] & 0xFFFFFFFF)
            b = self._sext32(self.regs[rt] & 0xFFFFFFFF)
            res = a * b
            self.lo = res & 0xFFFFFFFF
            self.hi = (res >> 32) & 0xFFFFFFFF
        elif func == 0x19:
            res = (self.regs[rs] & 0xFFFFFFFF) * (self.regs[rt] & 0xFFFFFFFF)
            self.lo = res & 0xFFFFFFFF
            self.hi = (res >> 32) & 0xFFFFFFFF
        elif func == 0x1A:
            d = self._sext32(self.regs[rt] & 0xFFFFFFFF)
            if d != 0:
                n = self._sext32(self.regs[rs] & 0xFFFFFFFF)
                self.lo = int(n / d) & 0xFFFFFFFF
                self.hi = (n % d) & 0xFFFFFFFF
        elif func == 0x1B:
            d = self.regs[rt] & 0xFFFFFFFF
            if d != 0:
                n = self.regs[rs] & 0xFFFFFFFF
                self.lo = (n // d) & 0xFFFFFFFF
                self.hi = (n % d) & 0xFFFFFFFF
        elif func == 0x20: self.regs[rd] = (self.regs[rs] + self.regs[rt]) & 0xFFFFFFFF
        elif func == 0x21: self.regs[rd] = (self.regs[rs] + self.regs[rt]) & 0xFFFFFFFF
        elif func == 0x22: self.regs[rd] = (self.regs[rs] - self.regs[rt]) & 0xFFFFFFFF
        elif func == 0x23: self.regs[rd] = (self.regs[rs] - self.regs[rt]) & 0xFFFFFFFF
        elif func == 0x24: self.regs[rd] = self.regs[rs] & self.regs[rt]
        elif func == 0x25: self.regs[rd] = self.regs[rs] | self.regs[rt]
        elif func == 0x26: self.regs[rd] = self.regs[rs] ^ self.regs[rt]
        elif func == 0x27: self.regs[rd] = ~(self.regs[rs] | self.regs[rt]) & 0xFFFFFFFF
        elif func == 0x2A:
            a = self._sext32(self.regs[rs] & 0xFFFFFFFF)
            b = self._sext32(self.regs[rt] & 0xFFFFFFFF)
            self.regs[rd] = 1 if a < b else 0
        elif func == 0x2B:
            self.regs[rd] = 1 if (self.regs[rs] & 0xFFFFFFFF) < (self.regs[rt] & 0xFFFFFFFF) else 0

    # -- REGIMM (opcode 0x01) --------------------------------------------
    def _regimm(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        val = self._sext32(self.regs[rs] & 0xFFFFFFFF)
        if rt == 0x00 and val < 0:       # BLTZ
            self.pc = (self.pc + (imm << 2)) & 0xFFFFFFFF
        elif rt == 0x01 and val >= 0:    # BGEZ
            self.pc = (self.pc + (imm << 2)) & 0xFFFFFFFF

    # -- Jumps -----------------------------------------------------------
    def _j(self, inst):
        target = (inst & 0x3FFFFFF) << 2
        self.pc = (self.pc & 0xF0000000) | target

    def _jal(self, inst):
        self.regs[31] = self.pc & 0xFFFFFFFF
        target = (inst & 0x3FFFFFF) << 2
        self.pc = (self.pc & 0xF0000000) | target

    # -- Branches --------------------------------------------------------
    def _beq(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        if self.regs[rs] == self.regs[rt]:
            self.pc = (self.pc + (imm << 2)) & 0xFFFFFFFF

    def _bne(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        if self.regs[rs] != self.regs[rt]:
            self.pc = (self.pc + (imm << 2)) & 0xFFFFFFFF

    def _blez(self, inst):
        rs  = (inst >> 21) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        if self._sext32(self.regs[rs] & 0xFFFFFFFF) <= 0:
            self.pc = (self.pc + (imm << 2)) & 0xFFFFFFFF

    def _bgtz(self, inst):
        rs  = (inst >> 21) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        if self._sext32(self.regs[rs] & 0xFFFFFFFF) > 0:
            self.pc = (self.pc + (imm << 2)) & 0xFFFFFFFF

    # -- Immediate ALU ---------------------------------------------------
    def _addi(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        self.regs[rt] = (self.regs[rs] + imm) & 0xFFFFFFFF

    def _addiu(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        self.regs[rt] = (self.regs[rs] + imm) & 0xFFFFFFFF

    def _slti(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        self.regs[rt] = 1 if self._sext32(self.regs[rs] & 0xFFFFFFFF) < imm else 0

    def _sltiu(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        imm = self._sext16(inst & 0xFFFF)
        self.regs[rt] = 1 if (self.regs[rs] & 0xFFFFFFFF) < (imm & 0xFFFFFFFF) else 0

    def _andi(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        self.regs[rt] = self.regs[rs] & (inst & 0xFFFF)

    def _ori(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        self.regs[rt] = self.regs[rs] | (inst & 0xFFFF)

    def _xori(self, inst):
        rs  = (inst >> 21) & 0x1F
        rt  = (inst >> 16) & 0x1F
        self.regs[rt] = self.regs[rs] ^ (inst & 0xFFFF)

    def _lui(self, inst):
        rt  = (inst >> 16) & 0x1F
        self.regs[rt] = (inst & 0xFFFF) << 16

    # -- COP0 ------------------------------------------------------------
    def _cop0(self, inst):
        rs = (inst >> 21) & 0x1F
        rt = (inst >> 16) & 0x1F
        rd = (inst >> 11) & 0x1F
        if rs == 0x00:   # MFC0
            self.regs[rt] = self.cop0[rd]
        elif rs == 0x04: # MTC0
            self.cop0[rd] = self.regs[rt]

    # -- Load / Store ----------------------------------------------------
    def _load_store(self, inst):
        op   = (inst >> 26) & 0x3F
        base = (inst >> 21) & 0x1F
        rt   = (inst >> 16) & 0x1F
        imm  = self._sext16(inst & 0xFFFF)
        addr = (self.regs[base] + imm) & 0xFFFFFFFF

        if   op == 0x20: self.regs[rt] = self._sext32(self.load8(addr) & 0xFF) & 0xFFFFFFFF  # LB
        elif op == 0x21: self.regs[rt] = self._sext32(self.load16(addr) & 0xFFFF) & 0xFFFFFFFF  # LH
        elif op == 0x22: self.regs[rt] = self.load32(addr & ~3)    # LWL (simplified)
        elif op == 0x23: self.regs[rt] = self.load32(addr)          # LW
        elif op == 0x24: self.regs[rt] = self.load8(addr) & 0xFF    # LBU
        elif op == 0x25: self.regs[rt] = self.load16(addr) & 0xFFFF # LHU
        elif op == 0x26: self.regs[rt] = self.load32(addr & ~3)    # LWR (simplified)
        elif op == 0x28: self.store8(addr, self.regs[rt] & 0xFF)
        elif op == 0x29: self.store16(addr, self.regs[rt] & 0xFFFF)
        elif op == 0x2A: self.store32(addr & ~3, self.regs[rt])    # SWL
        elif op == 0x2B: self.store32(addr, self.regs[rt])          # SW
        elif op == 0x2E: self.store32(addr & ~3, self.regs[rt])    # SWR


# ----------------------------------------------------------------------
# Memory (now includes CDROM I/O)
# ----------------------------------------------------------------------
class Memory:
    def __init__(self):
        self.ram     = bytearray(RAM_SIZE)
        self.scratch = bytearray(SCRATCH_SIZE)
        self.bios    = bytearray(BIOS_SIZE)
        self.cdrom   = CDROM()
        self.io      = bytearray(0x10000)   # general I/O (unused)

    def _physical(self, addr):
        addr &= 0xFFFFFFFF
        if addr >= KSEG0_BASE and addr < KSEG0_BASE + RAM_SIZE:
            return addr - KSEG0_BASE
        if addr >= KSEG1_BASE and addr < KSEG1_BASE + RAM_SIZE:
            return addr - KSEG1_BASE
        return addr

    def read32(self, addr):
        # For CD-ROM registers, we handle them specially
        if 0x1F801800 <= addr <= 0x1F801FFF:
            val  = self.cdrom.read_register(addr)
            val |= self.cdrom.read_register(addr+1) << 8
            val |= self.cdrom.read_register(addr+2) << 16
            val |= self.cdrom.read_register(addr+3) << 24
            return val & 0xFFFFFFFF

        addr = self._physical(addr & 0xFFFFFFFF)
        if addr < RAM_SIZE:
            return struct.unpack('<I', self.ram[addr:addr + 4])[0]
        if 0xBFC00000 <= addr < 0xBFC00000 + BIOS_SIZE:
            off = addr - 0xBFC00000
            return struct.unpack('<I', self.bios[off:off + 4])[0]
        if 0x1F800000 <= addr < 0x1F800000 + SCRATCH_SIZE:
            off = addr - 0x1F800000
            return struct.unpack('<I', self.scratch[off:off + 4])[0]
        return 0

    def write32(self, addr, val):
        # CD-ROM registers
        if 0x1F801800 <= addr <= 0x1F801FFF:
            self.cdrom.write_register(addr, val & 0xFF)
            self.cdrom.write_register(addr+1, (val >> 8) & 0xFF)
            self.cdrom.write_register(addr+2, (val >> 16) & 0xFF)
            self.cdrom.write_register(addr+3, (val >> 24) & 0xFF)
            return

        addr = self._physical(addr & 0xFFFFFFFF)
        if addr < RAM_SIZE:
            struct.pack_into('<I', self.ram, addr, val & 0xFFFFFFFF)
        elif 0x1F800000 <= addr < 0x1F800000 + SCRATCH_SIZE:
            struct.pack_into('<I', self.scratch, addr - 0x1F800000, val & 0xFFFFFFFF)

    def read16(self, addr):
        # CD-ROM registers
        if 0x1F801800 <= addr <= 0x1F801FFF:
            low = self.cdrom.read_register(addr)
            high = self.cdrom.read_register(addr+1)
            return (high << 8) | low

        addr = self._physical(addr & 0xFFFFFFFF)
        if addr < RAM_SIZE:
            return struct.unpack('<H', self.ram[addr:addr + 2])[0]
        if 0xBFC00000 <= addr < 0xBFC00000 + BIOS_SIZE:
            off = addr - 0xBFC00000
            return struct.unpack('<H', self.bios[off:off + 2])[0]
        return 0

    def write16(self, addr, val):
        # CD-ROM registers
        if 0x1F801800 <= addr <= 0x1F801FFF:
            self.cdrom.write_register(addr, val & 0xFF)
            self.cdrom.write_register(addr+1, (val >> 8) & 0xFF)
            return

        addr = self._physical(addr & 0xFFFFFFFF)
        if addr < RAM_SIZE:
            struct.pack_into('<H', self.ram, addr, val & 0xFFFF)

    def read8(self, addr):
        # CD-ROM registers
        if 0x1F801800 <= addr <= 0x1F801FFF:
            return self.cdrom.read_register(addr)

        addr = self._physical(addr & 0xFFFFFFFF)
        if addr < RAM_SIZE:
            return self.ram[addr]
        if 0xBFC00000 <= addr < 0xBFC00000 + BIOS_SIZE:
            return self.bios[addr - 0xBFC00000]
        return 0

    def write8(self, addr, val):
        # CD-ROM registers
        if 0x1F801800 <= addr <= 0x1F801FFF:
            self.cdrom.write_register(addr, val & 0xFF)
            return

        addr = self._physical(addr & 0xFFFFFFFF)
        if addr < RAM_SIZE:
            self.ram[addr] = val & 0xFF

    def load_cdrom(self, path):
        """Mount a disc image (cue or bin)."""
        if not os.path.exists(path):
            return False

        # Try .cue first
        if path.lower().endswith('.cue'):
            if self.cdrom.load_cue(path):
                return True
        # Fallback to .bin
        if self.cdrom.load_bin(path):
            return True
        return False

    def close_cdrom(self):
        self.cdrom.close()


# ----------------------------------------------------------------------
# GPU (software renderer)
# ----------------------------------------------------------------------
class GPU:
    SCREEN_W = 320
    SCREEN_H = 240
    VRAM_W   = 1024
    VRAM_H   = 512

    def __init__(self):
        self.vram    = np.zeros((self.VRAM_H, self.VRAM_W, 3), dtype=np.uint8)
        self.display = np.zeros((self.SCREEN_H, self.SCREEN_W, 3), dtype=np.uint8)
        self._pattern_dirty = True

    def write_vram(self, x, y, color):
        if 0 <= x < self.VRAM_W and 0 <= y < self.VRAM_H:
            self.vram[y, x] = color

    def render_frame(self):
        self.display[:self.SCREEN_H, :self.SCREEN_W] = \
            self.vram[:self.SCREEN_H, :self.SCREEN_W]
        return self.display

    def draw_test_pattern(self):
        """Colour-gradient test pattern (shows the emulator is alive)."""
        if not self._pattern_dirty:
            return
        ys = np.arange(self.SCREEN_H, dtype=np.uint16)[:, None]
        xs = np.arange(self.SCREEN_W, dtype=np.uint16)[None, :]
        r = ((xs + ys) & 0xFF).astype(np.uint8)
        g = ((xs * 2 + ys) & 0xFF).astype(np.uint8)
        b = ((xs + ys * 2) & 0xFF).astype(np.uint8)
        self.vram[:self.SCREEN_H, :self.SCREEN_W, 0] = r
        self.vram[:self.SCREEN_H, :self.SCREEN_W, 1] = g
        self.vram[:self.SCREEN_H, :self.SCREEN_W, 2] = b
        self._pattern_dirty = False


# ----------------------------------------------------------------------
# SPU (stub)
# ----------------------------------------------------------------------
class SPU:
    def __init__(self):
        self.audio_queue = queue.Queue()
    def write_register(self, reg, val): pass
    def read_register(self, reg):       return 0
    def generate_samples(self, n=1024): return np.zeros((n, 2), dtype=np.int16)


# ----------------------------------------------------------------------
# Emulator core
# ----------------------------------------------------------------------
class Emulator:
    CYCLES_PER_FRAME = 33_868_800 // 60

    def __init__(self):
        self.memory = Memory()
        self.bios   = SynthesizedBIOS(self.memory, self.memory.cdrom)
        self.cpu    = MIPS_CPU(self.memory, self.bios)
        self.bios.attach_cpu(self.cpu)
        self.gpu = GPU()
        self.spu = SPU()
        self.running   = False
        self.disc_path = None
        self.pr_files  = "off"

    def load_disc(self, path):
        if self.memory.load_cdrom(path):
            self.disc_path = path
            # Reset the system to boot from disc
            self.cpu.pc = 0xBFC00000
            self.cpu.regs = [0]*32
            self.cpu.cycles = 0
            return True
        return False

    def run_frame(self):
        start = self.cpu.cycles
        while (self.cpu.cycles - start) < self.CYCLES_PER_FRAME:
            self.cpu.step()
            # Update CDROM (handle interrupts)
            self.memory.cdrom.update()
        # Draw test pattern (until we have proper GPU emulation)
        self.gpu.draw_test_pattern()

    def start(self):
        self.running = True
        while self.running:
            self.run_frame()

    def stop(self):
        self.running = False
        self.memory.close_cdrom()


# ----------------------------------------------------------------------
# RPSC1 Bleem!-style dark GUI  (600 x 400, tkinter)
# ----------------------------------------------------------------------
class RPSC1_GUI:
    BG     = '#1e1e22'
    PANEL  = '#252529'
    BORDER = '#3a3a42'
    FG     = '#d4d4dc'
    ACCENT = '#4a6fa5'

    def __init__(self, root, emulator):
        self.root = root
        self.emu  = emulator
        self._pr_dialog_open = False # Debounce flag for the combobox

        # ── window ──────────────────────────────────────────────────────
        self.root.title("RPSC1")
        self.root.geometry("600x400")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        # ── style ───────────────────────────────────────────────────────
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TCombobox',
                        fieldbackground=self.PANEL,
                        background=self.PANEL,
                        foreground=self.FG)

        # ── top bar (Pr files selector) ────────────────────────────────
        top = tk.Frame(self.root, bg=self.PANEL,
                       highlightbackground=self.BORDER, highlightthickness=1)
        top.pack(fill=tk.X, padx=8, pady=(6, 2))

        tk.Label(top, text="Pr files", bg=self.PANEL, fg=self.FG,
                 font=('Segoe UI', 9)).pack(side=tk.LEFT, padx=(8, 4), pady=4)
        tk.Label(top, text="=", bg=self.PANEL, fg=self.FG,
                 font=('Segoe UI', 9)).pack(side=tk.LEFT, padx=2)

        self.pr_var = tk.StringVar(value=self.emu.pr_files)
        pr_combo = ttk.Combobox(top, textvariable=self.pr_var,
                                values=("off", "/pr"), state="readonly",
                                width=8, font=('Segoe UI', 9))
        pr_combo.pack(side=tk.LEFT, padx=(4, 8), pady=4)
        pr_combo.bind("<<ComboboxSelected>>", self._on_pr_files)

        # ── display area ───────────────────────────────────────────────
        disp = tk.Frame(self.root, bg=self.PANEL,
                        highlightbackground=self.BORDER, highlightthickness=1)
        disp.pack(pady=6, padx=8, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(disp, width=320, height=240,
                                bg='#000000', highlightthickness=0)
        self.canvas.pack(expand=True, pady=4)

        # ── button row ─────────────────────────────────────────────────
        btn_frame = tk.Frame(self.root, bg=self.BG)
        btn_frame.pack(pady=(2, 6))

        # MODIFICATION: Button text color explicitly forced to black forever
        bstyle = dict(bg='#35353d', fg='#000000', activebackground='#505060',
                      activeforeground='#000000', relief=tk.FLAT, bd=0,
                      font=('Segoe UI', 9, 'bold'), width=11, height=1,
                      highlightthickness=1, highlightbackground=self.BORDER)

        tk.Button(btn_frame, text="Load Disc",
                  command=self._load_disc, **bstyle).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="Reset",
                  command=self._reset,     **bstyle).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="Settings",
                  command=self._settings,  **bstyle).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="Exit",
                  command=self._exit,      **bstyle).pack(side=tk.LEFT, padx=4)

        # ── status bar ─────────────────────────────────────────────────
        self.status = tk.StringVar(value="Ready")
        tk.Label(self.root, textvariable=self.status, bd=0, relief=tk.FLAT,
                 anchor=tk.W, bg=self.PANEL, fg=self.FG,
                 font=('Segoe UI', 9), padx=8, pady=4,
                 highlightbackground=self.BORDER,
                 highlightthickness=1).pack(side=tk.BOTTOM, fill=tk.X)

        # ── emulation thread ───────────────────────────────────────────
        self._emu_thread = threading.Thread(target=self.emu.start, daemon=True)
        self._emu_thread.start()
        self._update_display()

    # -- callbacks -------------------------------------------------------
    def _load_disc(self):
        path = filedialog.askopenfilename(
            title="Select PS1 Disc Image",
            filetypes=[("Cue sheets", "*.cue"),
                       ("Binary files", "*.bin"),
                       ("All files", "*.*")])
        if path:
            if self.emu.load_disc(path):
                self.status.set(f"Loaded: {os.path.basename(path)}")
            else:
                self.status.set("Failed to load disc")
                messagebox.showerror("Error", f"Could not load disc:\n{path}")

    def _reset(self):
        self.emu.stop()
        time.sleep(0.1)
        pr = self.pr_var.get() if self.pr_var.get() in ("off", "/pr") else "off"
        new_emu = Emulator()
        new_emu.pr_files = pr
        self.emu = new_emu
        self._emu_thread = threading.Thread(target=self.emu.start, daemon=True)
        self._emu_thread.start()
        self.status.set("Reset")

    def _settings(self):
        messagebox.showinfo("Settings",
                            "Settings not yet implemented.\n"
                            "BIOS is synthesized automatically.")

    def _exit(self):
        self.emu.stop()
        self.root.quit()
        self.root.destroy()

    def _on_pr_files(self, _evt=None):
        # Anti-bounce/double-trigger protection
        if getattr(self, '_pr_dialog_open', False):
            return
            
        v = self.pr_var.get()
        if v == "/pr":
            self._pr_dialog_open = True
            folder = filedialog.askdirectory(title="Select /pr Patch Folder")
            self._pr_dialog_open = False
            
            if folder:
                self.emu.pr_files = folder
                self.status.set(f"Pr files: {folder}")
            else:
                self.pr_var.set("off")
                self.emu.pr_files = "off"
                self.status.set("Pr files: off")
        else:
            self.emu.pr_files = "off"
            self.status.set("Pr files: off")

    def _update_display(self):
        if self.emu.running:
            frame = self.emu.gpu.render_frame()
            img = Image.fromarray(frame, 'RGB').resize((320, 240), Image.NEAREST)
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self.root.after(16, self._update_display)   # ~60 fps


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def main():
    root = tk.Tk()
    emu  = Emulator()

    if "/pr" in sys.argv:
        emu.pr_files = "/pr"

    app = RPSC1_GUI(root, emu)

    if emu.pr_files == "/pr":
        app.pr_var.set("/pr")

    root.mainloop()


if __name__ == "__main__":
    main()
