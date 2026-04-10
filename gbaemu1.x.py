import os
import sys
import time
import pygame

# =============================================================================
# acgbaemu - Advanced Python GBA Emulator Framework (Multi-Region Support)
# =============================================================================

class GBAConstants:
    """Hardware constants for the Game Boy Advance."""
    SCREEN_WIDTH = 240
    SCREEN_HEIGHT = 160
    SCALE = 3
    
    # Memory Map Constants
    BIOS_SIZE = 0x4000
    WRAM_BOARD_SIZE = 0x40000
    WRAM_ONCHIP_SIZE = 0x8000
    IO_REG_SIZE = 0x400
    PALETTE_SIZE = 0x400
    VRAM_SIZE = 0x18000
    OAM_SIZE = 0x400
    ROM_MAX_SIZE = 0x2000000  # 32MB

class RegionProfile:
    """Defines hardware behaviors for different world regions."""
    def __init__(self, name, code, hz, language_id):
        self.name = name
        self.code = code      # The 4th character of the Game ID (e.g., 'E' for USA)
        self.refresh_rate = hz
        self.language_id = language_id

REGIONS = {
    'J': RegionProfile("Japan", "J", 59.73, 0),
    'E': RegionProfile("North America (NTSC)", "E", 59.73, 1),
    'P': RegionProfile("Europe (PAL)", "P", 59.73, 2), 
    'C': RegionProfile("China (iQue)", "C", 59.73, 5),
    'D': RegionProfile("Germany", "D", 59.73, 3),
    'F': RegionProfile("France", "F", 59.73, 4),
    'I': RegionProfile("Italy", "I", 59.73, 6),
    'S': RegionProfile("Spain", "S", 59.73, 7)
}

class PPU:
    """The Pixel Processing Unit - responsible for rendering the GBA's graphics."""
    def __init__(self, bus):
        self.bus = bus
        self.display_surface = pygame.Surface((GBAConstants.SCREEN_WIDTH, GBAConstants.SCREEN_HEIGHT))
        self.mode = 0
        
    def update(self):
        """Processes VRAM and Palette data to update the display surface."""
        dispcnt = self.bus.read16(0x04000000)
        self.mode = dispcnt & 0x7
        
        self.display_surface.fill((0, 0, 0))
        
        # Mode 3: Single bitmap frame buffer (Direct rendering)
        if self.mode == 3:
            self.render_mode3()
            
    def render_mode3(self):
        """Simple Mode 3 (Bitmap) renderer - 16-bit colors in VRAM."""
        for y in range(GBAConstants.SCREEN_HEIGHT):
            for x in range(GBAConstants.SCREEN_WIDTH):
                addr = 0x06000000 + (y * GBAConstants.SCREEN_WIDTH + x) * 2
                color16 = self.bus.read16(addr)
                # Convert BGR555 to RGB888
                r = (color16 & 0x1F) << 3
                g = ((color16 >> 5) & 0x1F) << 3
                b = ((color16 >> 10) & 0x1F) << 3
                self.display_surface.set_at((x, y), (r, g, b))

class MemoryBus:
    """Handles the GBA's complex memory mapping and hardware registers."""
    def __init__(self):
        self.bios = bytearray(GBAConstants.BIOS_SIZE)
        self.wram_board = bytearray(GBAConstants.WRAM_BOARD_SIZE)
        self.wram_onchip = bytearray(GBAConstants.WRAM_ONCHIP_SIZE)
        self.io_regs = bytearray(GBAConstants.IO_REG_SIZE)
        self.palette = bytearray(GBAConstants.PALETTE_SIZE)
        self.vram = bytearray(GBAConstants.VRAM_SIZE)
        self.oam = bytearray(GBAConstants.OAM_SIZE)
        self.rom = bytearray()
        self.current_region = REGIONS['E'] # Default to US
        
        self.setup_simulated_bios()

    def setup_simulated_bios(self):
        """Injects a jump instruction into the BIOS to pass execution to the ROM."""
        self.bios[0:4] = [0x1e, 0x00, 0x00, 0xea] # ARM Jump

    def load_rom(self, data):
        """Loads commercial ROM data and performs regional identification."""
        self.rom = bytearray(data)
        print(f"[Memory] ROM Loaded: {len(self.rom) / 1024 / 1024:.2f} MB")
        
        if len(self.rom) >= 0xBC:
            title = self.rom[0xA0:0xAC].decode('ascii', errors='ignore').strip()
            # Game Code is at 0xAC. Example: AGB-AMCE-USA. 'E' is the region.
            game_code = self.rom[0xAC:0xB0].decode('ascii', errors='ignore')
            region_char = game_code[3] if len(game_code) == 4 else 'E'
            
            self.current_region = REGIONS.get(region_char, REGIONS['E'])
            
            print(f"[Memory] Detected Title: {title}")
            print(f"[Memory] Region: {self.current_region.name} ({self.current_region.code})")

    def read8(self, address):
        region = (address >> 24) & 0xFF
        offset = address & 0xFFFFFF
        
        try:
            if region == 0x00: return self.bios[offset % GBAConstants.BIOS_SIZE]
            elif region == 0x02: return self.wram_board[offset % GBAConstants.WRAM_BOARD_SIZE]
            elif region == 0x03: return self.wram_onchip[offset % GBAConstants.WRAM_ONCHIP_SIZE]
            elif region == 0x04: return self.io_regs[offset % GBAConstants.IO_REG_SIZE]
            elif region == 0x05: return self.palette[offset % GBAConstants.PALETTE_SIZE]
            elif region == 0x06: return self.vram[offset % GBAConstants.VRAM_SIZE]
            elif region == 0x07: return self.oam[offset % GBAConstants.OAM_SIZE]
            elif 0x08 <= region <= 0x0D: 
                if len(self.rom) > 0:
                    return self.rom[offset % len(self.rom)]
        except (IndexError, ZeroDivisionError):
            pass
        return 0

    def write8(self, address, value):
        region = (address >> 24) & 0xFF
        offset = address & 0xFFFFFF
        value &= 0xFF
        try:
            if region == 0x02: self.wram_board[offset % GBAConstants.WRAM_BOARD_SIZE] = value
            elif region == 0x03: self.wram_onchip[offset % GBAConstants.WRAM_ONCHIP_SIZE] = value
            elif region == 0x04: self.io_regs[offset % GBAConstants.IO_REG_SIZE] = value
            elif region == 0x05: self.palette[offset % GBAConstants.PALETTE_SIZE] = value
            elif region == 0x06: self.vram[offset % GBAConstants.VRAM_SIZE] = value
            elif region == 0x07: self.oam[offset % GBAConstants.OAM_SIZE] = value
        except IndexError:
            pass

    def read16(self, address):
        return self.read8(address) | (self.read8(address + 1) << 8)

    def read32(self, address):
        return self.read16(address) | (self.read16(address + 2) << 16)

class ARM7TDMI:
    """The GBA's CPU - handles logic, math, and program execution."""
    def __init__(self, bus):
        self.bus = bus
        self.registers = [0] * 16 
        self.cpsr = 0x000000D3     
        self.pc = 0x00000000       
        self.disassembly = "Waiting..."

    def step(self):
        """Execute one machine code cycle."""
        current_instruction = self.bus.read32(self.pc)
        
        # BIOS boot skip
        if self.pc == 0x00000000 and current_instruction == 0xea00001e:
            self.disassembly = "BIOS Booting Region..."
            self.pc = 0x08000000
            return 1

        # Basic Instruction Decoding
        if ((current_instruction >> 25) & 0b111) == 0b101:
            offset = current_instruction & 0xFFFFFF
            if offset & 0x800000: offset -= 0x1000000
            jump_address = self.pc + 8 + (offset << 2)
            self.disassembly = f"B 0x{jump_address:08X}"
            self.pc = jump_address
        elif ((current_instruction >> 26) & 0b11) == 0b00:
            self.disassembly = "DATA PROC"
            self.pc += 4
        else:
            self.disassembly = f"UNK: 0x{current_instruction:08X}"
            self.pc += 4 
            
        return 1

class EmulatorApp:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((
            GBAConstants.SCREEN_WIDTH * GBAConstants.SCALE,
            GBAConstants.SCREEN_HEIGHT * GBAConstants.SCALE
        ))
        pygame.display.set_caption("acgbaemu - Multi-Region Hardware")
        
        self.clock = pygame.time.Clock()
        self.bus = MemoryBus()
        self.cpu = ARM7TDMI(self.bus)
        self.ppu = PPU(self.bus)
        
        self.running = True
        self.rom_loaded = False

    def load_file(self, filepath):
        try:
            with open(filepath, "rb") as f:
                self.bus.load_rom(f.read())
                self.rom_loaded = True
                self.cpu.pc = 0x00000000 
                print(f"[System] GBA Region Set: {self.bus.current_region.name}. Booting...")
        except Exception as e:
            print(f"[Error] Load failed: {e}")

    def handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            if event.type == pygame.DROPFILE:
                self.load_file(event.file)

    def draw(self):
        self.screen.fill((10, 10, 15))
        
        if not self.rom_loaded:
            f = pygame.font.SysFont("Arial", 22)
            t = f.render("Drag GBA ROM (J/E/P/C) to boot hardware", True, (148, 163, 184))
            r = t.get_rect(center=(GBAConstants.SCREEN_WIDTH*GBAConstants.SCALE//2, 
                                    GBAConstants.SCREEN_HEIGHT*GBAConstants.SCALE//2))
            self.screen.blit(t, r)
        else:
            self.ppu.update()
            scaled = pygame.transform.scale(self.ppu.display_surface, self.screen.get_size())
            self.screen.blit(scaled, (0, 0))
            
            # Regional Display Overlay
            f = pygame.font.SysFont("Consolas", 14)
            region_tag = f.render(f"REGION: {self.bus.current_region.name}", True, (255, 255, 255))
            pc_tag = f.render(f"PC: 0x{self.cpu.pc:08X} | {self.cpu.disassembly}", True, (56, 189, 248))
            
            pygame.draw.rect(self.screen, (30, 41, 59), (0, 0, 720, 50))
            self.screen.blit(region_tag, (20, 5))
            self.screen.blit(pc_tag, (20, 25))

    def run(self):
        while self.running:
            self.handle_events()
            if self.rom_loaded:
                # Execution loop adjusted for simulated regional refresh
                # Standard GBA logic is ~280k cycles per frame
                for _ in range(2500):
                    self.cpu.step()
            self.draw()
            pygame.display.flip()
            self.clock.tick(60)
        pygame.quit()

if __name__ == "__main__":
    emu = EmulatorApp()
    if len(sys.argv) > 1: emu.load_file(sys.argv[1])
    emu.run()
