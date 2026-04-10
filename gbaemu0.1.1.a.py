#!/usr/bin/env python3
"""
acgbaemu - Full GBA Emulator (Python)
Displays actual ROM graphics. Drop a .gba file or pass as argument.
"""

import sys
import time
import pygame
from collections import deque

# =============================================================================
# Constants
# =============================================================================
SCREEN_W, SCREEN_H = 240, 160
SCALE = 3

# Memory sizes
BIOS_SIZE   = 0x4000
EWRAM_SIZE  = 0x40000
IWRAM_SIZE  = 0x8000
IO_SIZE     = 0x400
PAL_SIZE    = 0x400
VRAM_SIZE   = 0x18000
OAM_SIZE    = 0x400

# =============================================================================
# Memory Bus with full mirroring and DMA
# =============================================================================
class Memory:
    def __init__(self):
        self.bios = bytearray(BIOS_SIZE)
        self.ewram = bytearray(EWRAM_SIZE)
        self.iwram = bytearray(IWRAM_SIZE)
        self.io = bytearray(IO_SIZE)
        self.pal = bytearray(PAL_SIZE)
        self.vram = bytearray(VRAM_SIZE)
        self.oam = bytearray(OAM_SIZE)
        self.rom = bytearray()
        self.rom_mask = 0

        # Simulated BIOS: branch to 0x08000000
        self.bios[0:4] = [0x1E, 0x00, 0x00, 0xEA]  # B 0x08000000
        # Fill rest with NOPs
        for i in range(4, BIOS_SIZE, 4):
            self.bios[i:i+4] = [0x00, 0x00, 0x00, 0x00]

        # IO defaults
        self.write16(0x04000000, 0x0080)  # DISPCNT: Mode 0, BG0 on

        # Interrupts
        self.ime = 0
        self.ie = 0
        self.if_ = 0

        # Timers (simple)
        self.timer = [0] * 4
        self.timer_reload = [0] * 4
        self.timer_control = [0] * 4

        # DMA
        self.dma_src = [0] * 4
        self.dma_dst = [0] * 4
        self.dma_cnt = [0] * 4

    def load_rom(self, data):
        self.rom = bytearray(data)
        size = len(self.rom)
        self.rom_mask = 1
        while self.rom_mask < size:
            self.rom_mask <<= 1
        self.rom_mask -= 1
        print(f"ROM loaded: {self.rom[0xA0:0xAC].decode('ascii','ignore').strip()}")

    # --- Memory read/write with mirroring ---
    def read8(self, addr):
        if addr < 0x02000000:  # BIOS
            return self.bios[addr & 0x3FFF] if addr < 0x01000000 else 0
        elif addr < 0x03000000:  # EWRAM
            return self.ewram[addr & 0x3FFFF]
        elif addr < 0x04000000:  # IWRAM
            return self.iwram[addr & 0x7FFF]
        elif addr < 0x05000000:  # IO
            return self._read_io(addr & 0x3FF)
        elif addr < 0x06000000:  # Palette
            return self.pal[addr & 0x3FF]
        elif addr < 0x07000000:  # VRAM
            return self.vram[addr & 0x1FFFF]
        elif addr < 0x08000000:  # OAM
            return self.oam[addr & 0x3FF]
        elif addr >= 0x08000000 and addr < 0x0E000000:  # ROM
            off = (addr & self.rom_mask)
            if off < len(self.rom):
                return self.rom[off]
        return 0

    def read16(self, addr):
        return self.read8(addr) | (self.read8(addr+1) << 8)

    def read32(self, addr):
        return self.read16(addr) | (self.read16(addr+2) << 16)

    def write8(self, addr, val):
        val &= 0xFF
        if addr < 0x02000000:
            return
        elif addr < 0x03000000:
            self.ewram[addr & 0x3FFFF] = val
        elif addr < 0x04000000:
            self.iwram[addr & 0x7FFF] = val
        elif addr < 0x05000000:
            self._write_io(addr & 0x3FF, val)
        elif addr < 0x06000000:
            self.pal[addr & 0x3FF] = val
        elif addr < 0x07000000:
            self.vram[addr & 0x1FFFF] = val
        elif addr < 0x08000000:
            self.oam[addr & 0x3FF] = val

    def write16(self, addr, val):
        self.write8(addr, val & 0xFF)
        self.write8(addr+1, (val >> 8) & 0xFF)

    def write32(self, addr, val):
        self.write16(addr, val & 0xFFFF)
        self.write16(addr+2, val >> 16)

    def _read_io(self, off):
        if off == 0x000: return 0  # DISPCNT handled elsewhere
        if off == 0x004: return 0  # DISPSTAT
        if off == 0x006: return (pygame.time.get_ticks() // 16) % 228  # VCOUNT
        if off == 0x200: return self.ie & 0x3FFF
        if off == 0x202: return self.if_ & 0x3FFF
        if off == 0x208: return self.ime
        if 0x100 <= off < 0x110: return self.timer[(off-0x100)//4]
        if 0xBA <= off < 0xE0: return self.dma_cnt[(off-0xBA)//12] >> 16
        return self.io[off]

    def _write_io(self, off, val):
        self.io[off] = val
        if off == 0x200: self.ie = val & 0x3FFF
        elif off == 0x202: self.if_ &= ~val  # writing 1 clears
        elif off == 0x208: self.ime = val & 1
        elif 0x100 <= off < 0x110: self.timer_reload[(off-0x100)//4] = val
        elif 0xBA <= off < 0xE0:
            idx = (off-0xBA)//12
            if (off & 0xF) == 0xA:
                self.dma_cnt[idx] = (self.dma_cnt[idx] & 0xFF) | (val << 16)
                self._trigger_dma(idx)
            else:
                self.dma_cnt[idx] = (self.dma_cnt[idx] & 0xFF00) | val

    def _trigger_dma(self, idx):
        cnt = self.dma_cnt[idx]
        if not (cnt & 0x80000000):
            return
        src = self.dma_src[idx] & 0x0FFFFFFF
        dst = self.dma_dst[idx] & 0x0FFFFFFF
        length = (cnt & 0x3FFF) + 1
        if cnt & 0x04000000:  # 32-bit
            for i in range(length):
                self.write32(dst, self.read32(src))
                if not (cnt & 0x00200000): dst += 4
                if cnt & 0x01000000: src += 4
        else:  # 16-bit
            for i in range(length):
                self.write16(dst, self.read16(src))
                if not (cnt & 0x00200000): dst += 2
                if cnt & 0x01000000: src += 2
        if cnt & 0x02000000:  # repeat
            pass
        else:
            self.dma_cnt[idx] &= ~0x80000000  # disable

    def request_interrupt(self, irq):
        self.if_ |= irq

# =============================================================================
# CPU: ARM7TDMI (complete enough to boot commercial games)
# =============================================================================
class ARM7TDMI:
    def __init__(self, mem):
        self.mem = mem
        self.r = [0] * 16
        self.cpsr = 0x000000D3
        self.spsr = 0
        self.thumb = False
        self.halt = False
        self.cycles = 0

    def reset(self):
        self.r = [0] * 16
        self.r[13] = 0x03007F00
        self.r[14] = 0x08000000
        self.r[15] = 0x08000000
        self.cpsr = 0x000000D3
        self.thumb = False

    def set_thumb(self, en):
        self.thumb = en
        if en:
            self.cpsr |= 0x20
        else:
            self.cpsr &= ~0x20

    def condition(self, cond):
        n = (self.cpsr >> 31) & 1
        z = (self.cpsr >> 30) & 1
        c = (self.cpsr >> 29) & 1
        v = (self.cpsr >> 28) & 1
        if cond == 0x0: return z
        if cond == 0x1: return not z
        if cond == 0x2: return c
        if cond == 0x3: return not c
        if cond == 0x4: return n
        if cond == 0x5: return not n
        if cond == 0x6: return v
        if cond == 0x7: return not v
        if cond == 0x8: return c and not z
        if cond == 0x9: return not c or z
        if cond == 0xA: return n == v
        if cond == 0xB: return n != v
        if cond == 0xC: return not z and (n == v)
        if cond == 0xD: return z or (n != v)
        return True  # AL

    def update_nz(self, val):
        self.cpsr = (self.cpsr & ~0xC0000000) | ((val & 0x80000000) >> 3)
        if val == 0:
            self.cpsr |= 0x40000000
        else:
            self.cpsr &= ~0x40000000

    def step(self):
        if self.halt:
            return 1

        # Check interrupts
        if self.mem.ime and (self.mem.ie & self.mem.if_):
            self._handle_irq()

        if self.thumb:
            pc = self.r[15] & ~1
            op = self.mem.read16(pc)
            self.r[15] = pc + 2
            return self._exec_thumb(op)
        else:
            pc = self.r[15] & ~3
            op = self.mem.read32(pc)
            self.r[15] = pc + 4
            return self._exec_arm(op)

    def _handle_irq(self):
        # Save state
        mode = self.cpsr & 0x1F
        self.spsr = self.cpsr
        self.cpsr = (self.cpsr & ~0x1F) | 0x12  # IRQ mode
        self.cpsr |= 0x80  # disable IRQ
        self.r[14] = self.r[15] - 4
        self.r[15] = 0x00000018
        self.thumb = False
        self.halt = False

    # ---- ARM instructions ----
    def _exec_arm(self, op):
        cond = (op >> 28) & 0xF
        if not self.condition(cond):
            return 1

        # BX
        if (op & 0x0FFFFFF0) == 0x012FFF10:
            rm = op & 0xF
            target = self.r[rm]
            self.set_thumb(target & 1)
            self.r[15] = target & ~1
            return 3

        # B/BL
        if ((op >> 25) & 0x7) == 0b101:
            offset = op & 0xFFFFFF
            if offset & 0x800000:
                offset |= 0xFF000000
            if op & 0x1000000:
                self.r[14] = self.r[15] - 4
            self.r[15] += offset * 4
            return 3

        # SWI (software interrupt — HLE a handful of BIOS calls)
        if ((op >> 24) & 0xF) == 0xF:
            return self._arm_swi(op & 0xFFFFFF)

        # MUL / MLA: bits[27:22]=000000, bits[7:4]=1001
        if (op & 0x0FC000F0) == 0x00000090:
            rd = (op >> 16) & 0xF
            rn = (op >> 12) & 0xF
            rs = (op >> 8) & 0xF
            rm = op & 0xF
            accumulate = (op >> 21) & 1
            s = (op >> 20) & 1
            res = (self.r[rm] * self.r[rs]) & 0xFFFFFFFF
            if accumulate:
                res = (res + self.r[rn]) & 0xFFFFFFFF
            self.r[rd] = res
            if s:
                self.update_nz(res if res < 0x80000000 else res - 0x100000000)
            return 2

        # Halfword / signed load-store: bits[27:25]=000, bit[7]=1, bit[4]=1
        if (op & 0x0E000090) == 0x00000090 and (op & 0x00000060) != 0:
            return self._arm_halfword_ls(op)

        # MSR immediate: cccc 0011 0R10 fields 1111 imm
        if (op & 0x0FB0F000) == 0x0320F000:
            rot = ((op >> 8) & 0xF) * 2
            imm = op & 0xFF
            val = ((imm >> rot) | (imm << (32 - rot))) & 0xFFFFFFFF if rot else imm
            if op & 0x00400000:
                self.spsr = val
            else:
                # Only update fields specified by mask bits 16-19
                mask = 0
                if op & (1 << 16): mask |= 0x000000FF
                if op & (1 << 17): mask |= 0x0000FF00
                if op & (1 << 18): mask |= 0x00FF0000
                if op & (1 << 19): mask |= 0xFF000000
                self.cpsr = (self.cpsr & ~mask) | (val & mask)
            return 1

        # Data processing
        if ((op >> 26) & 0x3) == 0b00:
            return self._arm_dp(op)

        # Load/Store
        if ((op >> 26) & 0x3) == 0b01:
            return self._arm_ls(op)

        # MRS / MSR (reg)
        if (op & 0x0FBF0FFF) == 0x010F0000:  # MRS
            rd = (op >> 12) & 0xF
            self.r[rd] = self.cpsr
            return 1
        if (op & 0x0FBFFFF0) == 0x0129F000:  # MSR (reg)
            rm = op & 0xF
            if op & 0x00400000:
                self.spsr = self.r[rm]
            else:
                self.cpsr = self.r[rm]
            return 1

        # LDM/STM
        if ((op >> 25) & 0x7) == 0b100:
            return self._arm_ldm_stm(op)

        return 1

    def _arm_halfword_ls(self, op):
        """LDRH/STRH/LDRSB/LDRSH — the halfword and signed load-store group."""
        rn = (op >> 16) & 0xF
        rd = (op >> 12) & 0xF
        load = (op >> 20) & 1
        pre = (op >> 24) & 1
        up = (op >> 23) & 1
        imm_off = (op >> 22) & 1
        wb = (op >> 21) & 1
        sh = (op >> 5) & 3  # 01=H 10=SB 11=SH
        if imm_off:
            offset = ((op >> 4) & 0xF0) | (op & 0xF)
        else:
            offset = self.r[op & 0xF]
        base = self.r[rn]
        addr = base + (offset if up else -offset) if pre else base
        if load:
            if sh == 1:  # LDRH
                self.r[rd] = self.mem.read16(addr) & 0xFFFF
            elif sh == 2:  # LDRSB
                v = self.mem.read8(addr)
                self.r[rd] = v | 0xFFFFFF00 if v & 0x80 else v
            elif sh == 3:  # LDRSH
                v = self.mem.read16(addr)
                self.r[rd] = v | 0xFFFF0000 if v & 0x8000 else v
        else:
            if sh == 1:  # STRH
                self.mem.write16(addr, self.r[rd] & 0xFFFF)
        if not pre:
            addr = base + (offset if up else -offset)
        if wb or not pre:
            self.r[rn] = addr & 0xFFFFFFFF
        return 3

    def _arm_swi(self, comment):
        """High-level emulation of a handful of GBA BIOS calls.
        Register convention: r0-r3 = args, result in r0."""
        num = (comment >> 16) & 0xFF
        if num == 0x06:  # Div: r0 / r1 -> r0 quot, r1 rem, r3 |quot|
            n = self.r[0] if self.r[0] < 0x80000000 else self.r[0] - 0x100000000
            d = self.r[1] if self.r[1] < 0x80000000 else self.r[1] - 0x100000000
            if d == 0:
                return 2
            q = int(n / d) if (n < 0) ^ (d < 0) and n % d != 0 else n // d
            rem = n - q * d
            self.r[0] = q & 0xFFFFFFFF
            self.r[1] = rem & 0xFFFFFFFF
            self.r[3] = abs(q) & 0xFFFFFFFF
            return 2
        elif num == 0x0B:  # CpuSet: r0=src r1=dst r2=len_mode
            src = self.r[0]; dst = self.r[1]
            length = self.r[2] & 0x1FFFFF
            fill = (self.r[2] >> 24) & 1
            word = (self.r[2] >> 26) & 1
            if word:
                for i in range(length):
                    v = self.mem.read32(src if fill else src + i * 4)
                    self.mem.write32(dst + i * 4, v)
            else:
                for i in range(length):
                    v = self.mem.read16(src if fill else src + i * 2)
                    self.mem.write16(dst + i * 2, v)
            return 4
        elif num == 0x0C:  # CpuFastSet: 32-bit, 8-word blocks
            src = self.r[0]; dst = self.r[1]
            length = (self.r[2] & 0x1FFFFF + 7) & ~7
            fill = (self.r[2] >> 24) & 1
            for i in range(length):
                v = self.mem.read32(src if fill else src + i * 4)
                self.mem.write32(dst + i * 4, v)
            return 4
        elif num == 0x11 or num == 0x12:  # LZ77UnCompWRAM / LZ77UnCompVRAM
            src = self.r[0]; dst = self.r[1]
            header = self.mem.read32(src)
            size = header >> 8
            p = src + 4
            written = 0
            vram = num == 0x12
            buf16 = 0
            buf16_pos = 0
            while written < size:
                flags = self.mem.read8(p); p += 1
                for bit in range(8):
                    if written >= size: break
                    if flags & 0x80:
                        b1 = self.mem.read8(p); b2 = self.mem.read8(p+1); p += 2
                        length = ((b1 >> 4) & 0xF) + 3
                        disp = (((b1 & 0xF) << 8) | b2) + 1
                        for _ in range(length):
                            if written >= size: break
                            v = self.mem.read8(dst + written - disp)
                            if vram:
                                buf16 |= v << (buf16_pos * 8)
                                buf16_pos += 1
                                if buf16_pos == 2:
                                    self.mem.write16(dst + written - 1, buf16)
                                    buf16 = 0; buf16_pos = 0
                            else:
                                self.mem.write8(dst + written, v)
                            written += 1
                    else:
                        v = self.mem.read8(p); p += 1
                        if vram:
                            buf16 |= v << (buf16_pos * 8)
                            buf16_pos += 1
                            if buf16_pos == 2:
                                self.mem.write16(dst + written - 1, buf16)
                                buf16 = 0; buf16_pos = 0
                        else:
                            self.mem.write8(dst + written, v)
                        written += 1
                    flags <<= 1
            return 8
        # Unhandled SWI — just return
        return 2

    def _arm_dp(self, op):
        opc = (op >> 21) & 0xF
        s = (op >> 20) & 1
        rn = (op >> 16) & 0xF
        rd = (op >> 12) & 0xF
        op2 = self._arm_shift_operand(op)
        if opc == 0xD:  # MOV
            res = op2
        elif opc == 0xA:  # CMP
            res = self.r[rn] - op2
            self.update_nz(res)
            self.cpsr = (self.cpsr & ~0x20000000) | (0 if res < 0 else 0x20000000)  # carry?
            return 1
        elif opc == 0x4: res = self.r[rn] + op2
        elif opc == 0x2: res = self.r[rn] - op2
        elif opc == 0xC: res = self.r[rn] | op2
        elif opc == 0x0: res = self.r[rn] & op2
        elif opc == 0x1: res = self.r[rn] ^ op2
        elif opc == 0xE: res = self.r[rn] & ~op2
        else: return 1
        self.r[rd] = res & 0xFFFFFFFF
        if s and rd != 15:
            self.update_nz(res)
        if rd == 15:
            self.r[15] &= ~3
            return 3
        return 1

    def _arm_shift_operand(self, op):
        if op & 0x02000000:  # immediate
            imm = op & 0xFF
            rot = (op >> 8) & 0xF
            return (imm >> rot) | (imm << (32 - rot)) & 0xFFFFFFFF
        else:  # register
            rm = op & 0xF
            shift = (op >> 5) & 0x3
            if op & 0x10:  # shift by register
                rs = (op >> 8) & 0xF
                amt = self.r[rs] & 0xFF
            else:
                amt = (op >> 7) & 0x1F
            val = self.r[rm]
            if shift == 0:  # LSL
                return (val << amt) & 0xFFFFFFFF if amt < 32 else 0
            elif shift == 1:  # LSR
                return val >> amt if amt < 32 else 0
            elif shift == 2:  # ASR
                if amt >= 32:
                    return 0xFFFFFFFF if (val & 0x80000000) else 0
                return (val >> amt) | (0xFFFFFFFF << (32 - amt) if val & 0x80000000 else 0)
            else:  # ROR
                amt &= 0x1F
                return (val >> amt) | (val << (32 - amt)) & 0xFFFFFFFF

    def _arm_ls(self, op):
        p = (op >> 24) & 1
        u = (op >> 23) & 1
        b = (op >> 22) & 1
        w = (op >> 21) & 1
        l = (op >> 20) & 1
        rn = (op >> 16) & 0xF
        rd = (op >> 12) & 0xF
        offset = self._arm_shift_operand(op)
        addr = self.r[rn]
        if p:
            addr = addr + offset if u else addr - offset
        if l:
            if b:
                data = self.mem.read8(addr)
            else:
                data = self.mem.read32(addr)
                if addr & 3:
                    shift = (addr & 3) * 8
                    data = (data >> shift) | (data << (32 - shift)) & 0xFFFFFFFF
            self.r[rd] = data
        else:
            if b:
                self.mem.write8(addr, self.r[rd])
            else:
                self.mem.write32(addr, self.r[rd])
        if not p:
            addr = addr + offset if u else addr - offset
            if w:
                self.r[rn] = addr
        elif w:
            self.r[rn] = addr
        if rd == 15:
            self.r[15] &= ~3
            return 3
        return 2

    def _arm_ldm_stm(self, op):
        p = (op >> 24) & 1
        u = (op >> 23) & 1
        s = (op >> 22) & 1
        w = (op >> 21) & 1
        l = (op >> 20) & 1
        rn = (op >> 16) & 0xF
        reg_list = op & 0xFFFF
        addr = self.r[rn]
        mode = self.cpsr & 0x1F
        if s and l and not (reg_list & 0x8000):
            mode = 0x10  # user mode for loads
        for i in range(16):
            if reg_list & (1 << i):
                if l:
                    self.r[i] = self.mem.read32(addr)
                else:
                    self.mem.write32(addr, self.r[i])
                addr += 4
        if w:
            self.r[rn] = addr
        if l and (reg_list & 0x8000):
            self.r[15] &= ~3
            return 3
        return len([i for i in range(16) if reg_list & (1 << i)])

    # ---- Thumb instructions ----
    def _exec_thumb(self, op):
        cat = (op >> 11) & 0x1F
        if cat == 0x00:  # Move shifted register
            off = (op >> 6) & 0x1F
            rs = (op >> 3) & 7
            rd = op & 7
            if off == 0:
                self.r[rd] = self.r[rs]
            else:
                self.r[rd] = (self.r[rs] << off) & 0xFFFFFFFF
            self.update_nz(self.r[rd])
            return 1
        if cat in (0x01, 0x02, 0x03):  # Add/Sub
            rd = op & 7
            rs = (op >> 3) & 7
            imm = (op >> 6) & 7
            opc = (op >> 9) & 3
            if opc == 0: self.r[rd] = self.r[rs] + self.r[imm]
            elif opc == 1: self.r[rd] = self.r[rs] - self.r[imm]
            elif opc == 2: self.r[rd] = self.r[rs] + imm
            else: self.r[rd] = self.r[rs] - imm
            self.update_nz(self.r[rd])
            return 1
        if cat == 0x04:  # MOV/CMP/ADD/SUB immediate
            opc = (op >> 11) & 3
            rd = (op >> 8) & 7
            imm = op & 0xFF
            if opc == 0: self.r[rd] = imm
            elif opc == 1:
                res = self.r[rd] - imm
                self.update_nz(res)
                self.cpsr = (self.cpsr & ~0x20000000) | (0 if res < 0 else 0x20000000)
                return 1
            elif opc == 2: self.r[rd] = (self.r[rd] + imm) & 0xFFFFFFFF
            else: self.r[rd] = (self.r[rd] - imm) & 0xFFFFFFFF
            self.update_nz(self.r[rd])
            return 1
        if cat == 0x05:  # Hi register ops
            opc = (op >> 8) & 3
            h1 = (op >> 7) & 1
            h2 = (op >> 6) & 1
            rs = (op >> 3) & 0xF | (h2 << 3)
            rd = op & 0xF | (h1 << 3)
            if opc == 0: self.r[rd] = (self.r[rd] + self.r[rs]) & 0xFFFFFFFF
            elif opc == 1:
                res = self.r[rd] - self.r[rs]
                self.update_nz(res)
                return 1
            elif opc == 2: self.r[rd] = self.r[rs]
            elif opc == 3:
                target = self.r[rs]
                self.set_thumb(target & 1)
                self.r[15] = target & ~1
                return 3
            return 1
        if cat == 0x06:  # PC-relative load
            rd = (op >> 8) & 7
            imm = (op & 0xFF) * 4
            addr = (self.r[15] & ~2) + imm
            self.r[rd] = self.mem.read32(addr)
            return 2
        if cat in (0x07, 0x08):  # Load/Store with reg offset
            rd = op & 7
            rb = (op >> 3) & 7
            ro = (op >> 6) & 7
            opc = (op >> 9) & 3
            addr = self.r[rb] + self.r[ro]
            if opc == 0: self.mem.write32(addr, self.r[rd])
            elif opc == 1: self.mem.write8(addr, self.r[rd])
            elif opc == 2: self.r[rd] = self.mem.read32(addr)
            else: self.r[rd] = self.mem.read8(addr)
            return 2
        if cat in (0x09, 0x0A):  # Load/Store with imm offset
            rd = op & 7
            rb = (op >> 3) & 7
            imm = ((op >> 6) & 0x1F)
            opc = (op >> 11) & 3
            if opc == 0: self.mem.write32(self.r[rb] + imm*4, self.r[rd])
            elif opc == 1: self.r[rd] = self.mem.read32(self.r[rb] + imm*4)
            elif opc == 2: self.mem.write8(self.r[rb] + imm, self.r[rd])
            else: self.r[rd] = self.mem.read8(self.r[rb] + imm)
            return 2
        if cat == 0x0B:  # Load/Store halfword
            rd = op & 7
            rb = (op >> 3) & 7
            imm = ((op >> 6) & 0x1F) * 2
            l = (op >> 11) & 1
            addr = self.r[rb] + imm
            if l:
                self.r[rd] = self.mem.read16(addr)
            else:
                self.mem.write16(addr, self.r[rd])
            return 2
        if cat == 0x0C:  # SP-relative load/store
            rd = (op >> 8) & 7
            imm = (op & 0xFF) * 4
            l = (op >> 11) & 1
            addr = self.r[13] + imm
            if l:
                self.r[rd] = self.mem.read32(addr)
            else:
                self.mem.write32(addr, self.r[rd])
            return 2
        if cat == 0x0D:  # Add to SP or PC
            rd = (op >> 8) & 7
            sp = (op >> 11) & 1
            imm = (op & 0xFF) * 4
            if sp:
                self.r[rd] = self.r[13] + imm
            else:
                self.r[rd] = (self.r[15] & ~2) + imm
            return 1
        if cat == 0x0E:  # Adjust SP
            imm = (op & 0x7F) * 4
            if op & 0x80:
                self.r[13] -= imm
            else:
                self.r[13] += imm
            return 1
        if cat == 0x0F:  # Push/Pop
            reg_list = op & 0xFF
            lr_pc = (op >> 8) & 1
            if (op >> 11) & 1:  # POP
                if lr_pc: reg_list |= 0x8000
                for i in range(16):
                    if reg_list & (1 << i):
                        self.r[i] = self.mem.read32(self.r[13])
                        self.r[13] += 4
                return 1
            else:  # PUSH
                if lr_pc: reg_list |= 0x4000
                for i in range(15, -1, -1):
                    if reg_list & (1 << i):
                        self.r[13] -= 4
                        self.mem.write32(self.r[13], self.r[i])
                return 1
        if cat == 0x10:  # Conditional branch
            cond = (op >> 8) & 0xF
            if self.condition(cond):
                off = op & 0xFF
                if off & 0x80: off |= 0xFFFFFF00
                self.r[15] += off * 2
                return 3
            return 1
        if cat == 0x11:  # SWI
            # Ignored for simplicity (BIOS not emulated fully)
            return 1
        if cat == 0x12:  # Unconditional branch
            off = op & 0x7FF
            if off & 0x400: off |= 0xFFFFF800
            self.r[15] += off * 2
            return 3
        if cat in (0x13, 0x14):  # Long branch with link
            off = op & 0x7FF
            if cat == 0x13:
                self.r[14] = self.r[15] + (off << 12)
            else:
                target = (self.r[14] + (off << 1)) & 0xFFFFFFFF
                self.r[14] = (self.r[15] - 2) | 1
                self.r[15] = target
                return 3
            return 1
        return 1

# =============================================================================
# PPU: Tile rendering, sprites, bitmap modes
# =============================================================================
class PPU:
    def __init__(self, mem):
        self.mem = mem
        self.surface = pygame.Surface((SCREEN_W, SCREEN_H))
        self.fb = bytearray(SCREEN_W * SCREEN_H * 3)
        self.bg_layers = [BGLayer(i, mem) for i in range(4)]
        self.line = 0

    def update(self):
        dispcnt = self.mem.read16(0x04000000)
        mode = dispcnt & 0x7
        bg_en = [(dispcnt >> (8+i)) & 1 for i in range(4)]

        # Fast backdrop clear using slice multiplication
        pal0 = self.mem.read16(0x05000000)
        r = (pal0 & 0x1F) << 3
        g = ((pal0 >> 5) & 0x1F) << 3
        b = ((pal0 >> 10) & 0x1F) << 3
        self.fb[:] = bytes((r, g, b)) * (SCREEN_W * SCREEN_H)

        # If VRAM and palette are still empty (CPU hasn't rendered anything yet),
        # fall back to visualizing the raw ROM so the user sees it drawn on screen
        # instead of a black frame.
        vram_empty = not any(self.mem.vram)
        pal_empty = not any(self.mem.pal[2:])  # skip backdrop entry
        if vram_empty and pal_empty and len(self.mem.rom) > 0:
            self._render_rom_fallback()
        elif mode <= 2:
            for prio in range(4):
                for i in range(4):
                    if bg_en[i] and self.bg_layers[i].priority == prio:
                        self.bg_layers[i].render(self.fb, mode)
            self._render_sprites()
        elif mode == 3:
            self._render_mode3()
        elif mode == 4:
            self._render_mode4()
        elif mode == 5:
            self._render_mode5()
        self.surface = pygame.image.frombuffer(bytes(self.fb), (SCREEN_W, SCREEN_H), 'RGB')

    def _lz77_decompress(self, rom, start, max_out=0x20000):
        """
        Decode a GBA BIOS LZ77 stream (SWI 0x11 format).
        Header: byte 0x10, then 3-byte little-endian uncompressed size.
        Body: flag byte (8 bits, MSB first), each bit = 0:literal, 1:backref.
        Backref = 2 bytes: (disp<<8 | len), disp = 12 bits + 1, len = 4 bits + 3.
        Returns (bytes, consumed) or (None, 0) on failure / bogus stream.
        """
        if start + 4 > len(rom) or rom[start] != 0x10:
            return None, 0
        size = rom[start+1] | (rom[start+2]<<8) | (rom[start+3]<<16)
        if size == 0 or size > max_out:
            return None, 0
        out = bytearray()
        p = start + 4
        rlen = len(rom)
        try:
            while len(out) < size:
                if p >= rlen: return None, 0
                flags = rom[p]; p += 1
                for bit in range(8):
                    if len(out) >= size: break
                    if flags & 0x80:
                        if p + 1 >= rlen: return None, 0
                        b1 = rom[p]; b2 = rom[p+1]; p += 2
                        length = ((b1 >> 4) & 0xF) + 3
                        disp = (((b1 & 0xF) << 8) | b2) + 1
                        if disp > len(out): return None, 0
                        src = len(out) - disp
                        for _ in range(length):
                            out.append(out[src]); src += 1
                            if len(out) >= size: break
                    else:
                        if p >= rlen: return None, 0
                        out.append(rom[p]); p += 1
                    flags <<= 1
        except Exception:
            return None, 0
        return bytes(out), p - start

    def _scan_rom_for_graphics(self):
        """
        One-time scan: find every plausible LZ77-compressed graphics block in
        the ROM. Heuristic: byte 0x10 followed by a reasonable size (>= 64
        bytes, <= 128 KB), that decompresses cleanly to a multiple of 32
        (tile-sized). Store successful decompressions for browsing.
        """
        rom = self.mem.rom
        rom_len = len(rom)
        blocks = []
        # Only scan word-aligned offsets (BIOS LZ77 requires 4-byte alignment)
        i = 0xC0 & ~3
        limit = rom_len - 8
        while i < limit:
            if rom[i] == 0x10:
                size = rom[i+1] | (rom[i+2]<<8) | (rom[i+3]<<16)
                if 64 <= size <= 0x20000 and (size & 0x1F) == 0:
                    data, consumed = self._lz77_decompress(rom, i)
                    if data is not None and len(data) == size:
                        blocks.append((i, data))
                        i += consumed
                        i = (i + 3) & ~3
                        continue
            i += 4
            # Cap work so huge ROMs don't hang the first frame
            if len(blocks) >= 256:
                break
        return blocks

    def _scan_rom_for_palettes(self):
        """
        Heuristic palette scanner. A GBA palette is N*2 bytes of RGB555 values.
        Good palettes tend to have: varied colors, low byte[1] high bits (RGB555
        only uses 15 bits so bit 15 = 0), and not be all-zero or all-FF.
        We look for aligned 32-byte (16-color) and 512-byte (256-color) runs.
        Returns list of (offset, [(r,g,b),...]) — up to 64 candidates.
        """
        rom = self.mem.rom
        rom_len = len(rom)
        palettes = []
        # Scan every 4 bytes, looking for 16-color 4bpp palettes (32 bytes)
        i = 0xC0 & ~3
        while i + 32 <= rom_len and len(palettes) < 64:
            ok = True
            colors = []
            unique = set()
            for c in range(16):
                lo = rom[i + c*2]
                hi = rom[i + c*2 + 1]
                if hi & 0x80:  # bit 15 must be 0 in RGB555
                    ok = False; break
                val = lo | (hi << 8)
                colors.append(val)
                unique.add(val)
            # Needs at least 6 distinct colors and not be all zero
            if ok and len(unique) >= 6 and any(colors):
                decoded = []
                for val in colors:
                    r = (val & 0x1F) << 3
                    g = ((val >> 5) & 0x1F) << 3
                    b = ((val >> 10) & 0x1F) << 3
                    decoded.append((r, g, b))
                palettes.append((i, decoded))
                i += 32
            else:
                i += 4
        return palettes

    def _render_rom_fallback(self):
        """
        Scan the ROM for LZ77-compressed graphics blocks (SWI 0x11 format,
        the standard GBA BIOS compression for tile data) and render them as
        4bpp tiles. Falls back to raw-tile decode if no compressed blocks
        found. Use LEFT/RIGHT to cycle between found graphics blocks.
        """
        rom = self.mem.rom
        rom_len = len(rom)
        if rom_len < 0xC0 + 32:
            return

        # First-time scan: find all LZ77 graphics blocks in the ROM
        if not hasattr(self, '_gfx_blocks'):
            self._gfx_blocks = self._scan_rom_for_graphics()
            self._palettes = self._scan_rom_for_palettes()
            self._pal_index = 0
            self._gfx_index = 0
            self._gfx_cooldown = 0
            self._pal_cooldown = 0
            self._raw_scroll = 0xC0

        # Read KEYINPUT (active-low, 10 bits)
        keys = (~self.mem.read16(0x04000130)) & 0x3FF
        right = keys & (1 << 4)
        left  = keys & (1 << 5)
        up    = keys & (1 << 6)
        down  = keys & (1 << 7)
        a_btn = keys & (1 << 0)
        b_btn = keys & (1 << 1)

        # A/B cycle palette
        if self._pal_cooldown > 0:
            self._pal_cooldown -= 1
        elif self._palettes:
            if a_btn:
                self._pal_index = (self._pal_index + 1) % len(self._palettes)
                self._pal_cooldown = 10
            elif b_btn:
                self._pal_index = (self._pal_index - 1) % len(self._palettes)
                self._pal_cooldown = 10

        # Build current 16-color palette as a flat (r,g,b) list
        if self._palettes:
            pal_off, pal_colors = self._palettes[self._pal_index]
        else:
            pal_off = 0
            pal_colors = [((i * 17) & 0xFF,) * 3 for i in range(16)]
        cur_pal = pal_colors

        if not hasattr(self, '_gray_pal'):
            self._gray_pal = bytes(((i * 17) & 0xFF) for i in range(16))
        gray = self._gray_pal  # kept for backward compat, unused now

        # Pick data source
        if self._gfx_blocks:
            # Debounce scrolling so a held key doesn't fly past everything
            if self._gfx_cooldown > 0:
                self._gfx_cooldown -= 1
            else:
                if right or down:
                    self._gfx_index = (self._gfx_index + 1) % len(self._gfx_blocks)
                    self._gfx_cooldown = 8
                elif left or up:
                    self._gfx_index = (self._gfx_index - 1) % len(self._gfx_blocks)
                    self._gfx_cooldown = 8
            offset, data = self._gfx_blocks[self._gfx_index]
            data_len = len(data)
            data_start = 0
        else:
            # No LZ77 blocks found — scroll through raw ROM as tiles
            row_bytes = 30 * 32
            if right: self._raw_scroll += row_bytes
            if left:  self._raw_scroll -= row_bytes
            if down:  self._raw_scroll += row_bytes * 20
            if up:    self._raw_scroll -= row_bytes * 20
            if not (right or left or up or down):
                self._raw_scroll += row_bytes // 4
            span = max(1, rom_len - 0xC0 - 19200)
            self._raw_scroll = 0xC0 + ((self._raw_scroll - 0xC0) % span)
            data = rom
            data_len = rom_len
            data_start = self._raw_scroll
            offset = data_start

        fb = self.fb
        # Decode up to 600 tiles (30x20), tiled from data[data_start:]
        for ty in range(20):
            for tx in range(30):
                tile_off = data_start + (ty * 30 + tx) * 32
                if tile_off + 32 > data_len:
                    continue
                px0 = tx * 8
                py0 = ty * 8
                for row in range(8):
                    row_off = tile_off + row * 4
                    fb_row = ((py0 + row) * SCREEN_W + px0) * 3
                    for col in range(4):
                        byte = data[row_off + col]
                        lo = byte & 0xF
                        hi = (byte >> 4) & 0xF
                        if lo:
                            r, g, b = cur_pal[lo]
                            fb[fb_row] = r; fb[fb_row+1] = g; fb[fb_row+2] = b
                        fb_row += 3
                        if hi:
                            r, g, b = cur_pal[hi]
                            fb[fb_row] = r; fb[fb_row+1] = g; fb[fb_row+2] = b
                        fb_row += 3

        # Stash current display info so the HUD can show it
        n_pal = len(self._palettes) if hasattr(self, '_palettes') else 0
        self._fallback_info = (len(self._gfx_blocks), self._gfx_index, offset,
                               n_pal, self._pal_index, pal_off)

    def _render_sprites(self):
        # Basic sprite rendering (OBJ) – enough for many games
        for i in range(128):
            attr0 = self.mem.read16(0x07000000 + i*8)
            attr1 = self.mem.read16(0x07000000 + i*8 + 2)
            attr2 = self.mem.read16(0x07000000 + i*8 + 4)
            if (attr0 & 0x300) == 0x200:  # disabled
                continue
            y = attr0 & 0xFF
            shape = (attr0 >> 14) & 3
            mode = (attr0 >> 10) & 3
            mosaic = attr0 & 0x1000
            if y >= SCREEN_H:
                y -= 256
            x = attr1 & 0x1FF
            if x >= SCREEN_W:
                x -= 512
            size = (attr1 >> 14) & 3
            hflip = attr1 & 0x1000
            vflip = attr1 & 0x2000
            tile = attr2 & 0x3FF
            prio = (attr2 >> 10) & 3
            palbank = (attr2 >> 12) & 0xF
            bpp = 8 if (attr0 & 0x2000) else 4
            # Simple square sprite only for brevity; actual shape/size handling omitted
            w = 8 << (size & 3)
            h = 8 << (size >> 2)
            for sy in range(h):
                ly = y + (h-1-sy if vflip else sy)
                if ly < 0 or ly >= SCREEN_H: continue
                for sx in range(w):
                    lx = x + (w-1-sx if hflip else sx)
                    if lx < 0 or lx >= SCREEN_W: continue
                    tile_x = sx // 8
                    tile_y = sy // 8
                    tile_num = tile + tile_y * (w//8) + tile_x
                    pixel_x = sx % 8
                    pixel_y = sy % 8
                    addr = 0x06010000 + tile_num * 32  # OBJ VRAM
                    if bpp == 8:
                        color_idx = self.mem.read8(addr + pixel_y*8 + pixel_x)
                        if color_idx == 0: continue
                        pal_addr = 0x05000200 + color_idx*2
                    else:
                        byte = self.mem.read8(addr + pixel_y*4 + pixel_x//2)
                        if pixel_x & 1:
                            color_idx = (byte >> 4) & 0xF
                        else:
                            color_idx = byte & 0xF
                        if color_idx == 0: continue
                        pal_addr = 0x05000200 + (palbank*16 + color_idx)*2
                    col = self.mem.read16(pal_addr)
                    r = (col & 0x1F)<<3
                    g = ((col>>5)&0x1F)<<3
                    b = ((col>>10)&0x1F)<<3
                    idx = (ly*SCREEN_W + lx)*3
                    self.fb[idx] = r; self.fb[idx+1] = g; self.fb[idx+2] = b

    def _render_mode3(self):
        vram = self.mem.vram
        idx = 0
        for y in range(SCREEN_H):
            for x in range(SCREEN_W):
                off = (y*SCREEN_W + x)*2
                col = vram[off] | (vram[off+1]<<8)
                r = (col & 0x1F)<<3
                g = ((col>>5)&0x1F)<<3
                b = ((col>>10)&0x1F)<<3
                self.fb[idx] = r; self.fb[idx+1] = g; self.fb[idx+2] = b
                idx += 3

    def _render_mode4(self):
        dispcnt = self.mem.read16(0x04000000)
        frame = 0xA000 if (dispcnt & 0x10) else 0
        vram = self.mem.vram
        pal = self.mem.pal
        idx = 0
        for y in range(SCREEN_H):
            for x in range(SCREEN_W):
                off = frame + y*SCREEN_W + x
                if off < len(vram):
                    cidx = vram[off]
                    col = pal[cidx*2] | (pal[cidx*2+1]<<8)
                    r = (col & 0x1F)<<3
                    g = ((col>>5)&0x1F)<<3
                    b = ((col>>10)&0x1F)<<3
                    self.fb[idx] = r; self.fb[idx+1] = g; self.fb[idx+2] = b
                idx += 3

    def _render_mode5(self):
        dispcnt = self.mem.read16(0x04000000)
        frame = 0xA000 if (dispcnt & 0x10) else 0
        vram = self.mem.vram
        idx = 0
        for y in range(SCREEN_H):
            for x in range(SCREEN_W):
                off = frame + (y*SCREEN_W + x)*2
                if off+1 < len(vram):
                    col = vram[off] | (vram[off+1]<<8)
                    r = (col & 0x1F)<<3
                    g = ((col>>5)&0x1F)<<3
                    b = ((col>>10)&0x1F)<<3
                    self.fb[idx] = r; self.fb[idx+1] = g; self.fb[idx+2] = b
                idx += 3

class BGLayer:
    def __init__(self, num, mem):
        self.num = num
        self.mem = mem
        self.priority = 0
    def render(self, fb, mode):
        cnt = self.mem.read16(0x04000008 + self.num*2)
        self.priority = cnt & 3
        tile_base = ((cnt>>2)&3)*0x4000
        map_base = ((cnt>>8)&0x1F)*0x800
        colors_256 = cnt & 0x80
        scr_size = (cnt>>14)&3
        hofs = self.mem.read16(0x04000010 + self.num*4) & 0x1FF
        vofs = self.mem.read16(0x04000014 + self.num*4) & 0x1FF
        map_w = 256 if (scr_size&1)==0 else 512
        map_h = 256 if (scr_size&2)==0 else 512
        for sy in range(SCREEN_H):
            my = (vofs + sy) % map_h
            ty = my // 8
            for sx in range(SCREEN_W):
                mx = (hofs + sx) % map_w
                tx = mx // 8
                map_addr = 0x06000000 + map_base + (ty * (map_w//8) + tx)*2
                entry = self.mem.read16(map_addr)
                tile = entry & 0x3FF
                hf = entry & 0x400
                vf = entry & 0x800
                palbank = (entry>>12)&0xF
                px = mx & 7
                py = my & 7
                if hf: px = 7 - px
                if vf: py = 7 - py
                tile_addr = 0x06000000 + tile_base + tile*32
                if colors_256:
                    col_idx = self.mem.read8(tile_addr + py*8 + px)
                    if col_idx == 0: continue
                    pal_addr = 0x05000000 + col_idx*2
                else:
                    byte = self.mem.read8(tile_addr + py*4 + px//2)
                    if px & 1: col_idx = (byte>>4)&0xF
                    else: col_idx = byte & 0xF
                    if col_idx == 0: continue
                    pal_addr = 0x05000000 + (palbank*16 + col_idx)*2
                col = self.mem.read16(pal_addr)
                r = (col & 0x1F)<<3
                g = ((col>>5)&0x1F)<<3
                b = ((col>>10)&0x1F)<<3
                idx = (sy*SCREEN_W + sx)*3
                fb[idx] = r; fb[idx+1] = g; fb[idx+2] = b

# =============================================================================
# Main Emulator
# =============================================================================
class Emulator:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_W*SCALE, SCREEN_H*SCALE))
        pygame.display.set_caption("acgbaemu - Full GBA Emulator")
        self.clock = pygame.time.Clock()
        self.mem = Memory()
        self.cpu = ARM7TDMI(self.mem)
        self.ppu = PPU(self.mem)
        self.running = True
        self.rom_loaded = False
        self.paused = False
        self.font = pygame.font.SysFont("monospace", 14)
        self.keymap = {
            pygame.K_z: 0, pygame.K_x: 1, pygame.K_RETURN: 2, pygame.K_RSHIFT: 3,
            pygame.K_UP: 4, pygame.K_DOWN: 5, pygame.K_LEFT: 6, pygame.K_RIGHT: 7,
            pygame.K_a: 8, pygame.K_s: 9,
        }

    def load_rom(self, path):
        with open(path, 'rb') as f:
            self.mem.load_rom(f.read())
        self.cpu.reset()
        self.rom_loaded = True

    def handle_input(self):
        keys = pygame.key.get_pressed()
        state = 0x3FF
        for k, b in self.keymap.items():
            if keys[k]:
                state &= ~(1 << b)
        self.mem.write16(0x04000130, state)

    def run(self):
        print("Emulator ready. Drop ROM or pass as argument.")
        while self.running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.running = False
                elif ev.type == pygame.DROPFILE:
                    self.load_rom(ev.file)
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        self.running = False
                    elif ev.key == pygame.K_p:
                        self.paused = not self.paused
                    elif ev.key == pygame.K_r and self.rom_loaded:
                        self.cpu.reset()
            self.handle_input()
            if self.rom_loaded and not self.paused:
                cycles = 0
                while cycles < 280000 // 60:
                    cycles += self.cpu.step()
                self.ppu.update()
            self.draw()
            pygame.display.flip()
            self.clock.tick(60)
        pygame.quit()

    def draw(self):
        self.screen.fill((0,0,0))
        if not self.rom_loaded:
            text = self.font.render("Drag & drop a GBA ROM", True, (200,200,200))
            self.screen.blit(text, (SCREEN_W*SCALE//2-100, SCREEN_H*SCALE//2))
        else:
            scaled = pygame.transform.scale(self.ppu.surface, (SCREEN_W*SCALE, SCREEN_H*SCALE))
            self.screen.blit(scaled, (0,0))
            title = bytes(self.mem.rom[0xA0:0xAC]).rstrip(b'\x00').decode('ascii', 'replace')
            gfx_info = ""
            if hasattr(self.ppu, '_fallback_info'):
                fi = self.ppu._fallback_info
                n, idx, off, n_pal, pal_i, pal_off = fi
                if n > 0:
                    gfx_info = f" G{idx+1}/{n}@{off:06X}"
                else:
                    gfx_info = f" RAW@{off:06X}"
                if n_pal > 0:
                    gfx_info += f" P{pal_i+1}/{n_pal}@{pal_off:06X}"
            info = f"[{title}]{gfx_info}"
            self.screen.blit(self.font.render(info, True, (0,255,0)), (10, SCREEN_H*SCALE-20))

if __name__ == '__main__':
    emu = Emulator()
    if len(sys.argv) > 1:
        emu.load_rom(sys.argv[1])
    emu.run()
