/* unsp_core.c — native µ'nSP ISA 1.3 CPU core for the Furby emulator.
 *
 * Port of the validated Python core (emu/unsp_cpu.py), which itself mirrors
 * MAME's GPL-2.0 unSP core. Python stays the orchestrator/GUI; this runs the
 * instruction stream at native speed and is bound via ctypes (emu/unsp_native.py).
 *
 * Word-addressed, 22-bit space. SR = [DS:6][N Z S C][CS:6]. MMIO at 0x7000-0x7FFF
 * is handled in-core (fast) with small configurable tables (ready value / bits
 * that auto-clear); everything else is flat RAM backed by the firmware image.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#define ADDR_MASK 0x3fffffu
#define MEMW      0x400000u          /* 4 M-words */
#define MMIO_LO   0x7000u
#define MMIO_HI   0x7fffu
#define MMION     0x1000u

enum { SP, R1, R2, R3, R4, BP, SR, PC };
#define FN 0x0200u
#define FZ 0x0100u
#define FS 0x0080u
#define FC 0x0040u
#define N_SH 9
#define S_SH 7

typedef struct {
    uint16_t r[8];
    uint16_t ext[8];            /* R8-R15 (extended registers, ISA 2.0 EXTOP) */
    uint32_t sb;
    int irq_en, fiq_en, halted;
    uint64_t insns;
    uint16_t *mem;                    /* MEMW words */
    uint16_t mmio_last[MMION];
    uint8_t  mmio_has[MMION];
    uint16_t mmio_ready[MMION];
    uint16_t mmio_autoclear[MMION];
    uint16_t mmio_reador[MMION];   /* bits forced high on every read (status/done bits) */
    uint16_t mmio_readclear[MMION];/* bits returned then cleared on read (pulsed status) */
    uint32_t mmio_reads[MMION];
    uint32_t mmio_writes[MMION];
    /* debug: stop when the code segment exceeds cs_trap (0 = disabled) */
    uint32_t cs_trap;
    uint32_t trap_from, trap_to;
    int trapped;
    /* interrupts */
    uint8_t irq_pending;        /* bitmask of pending IRQ lines 0-7 */
    int in_irq;                 /* currently servicing an IRQ (no nesting) */
    uint16_t secbank[4];        /* shadow R1-R4 for secbank (fast-interrupt bank) */
    int bnk;                    /* secbank active flag */
    uint32_t divq_bit, divq_dividend, divq_divisor, divq_a;  /* iterative DIVQ state */
    uint32_t irq_vecbase;       /* jump target base; line n -> irq_vecbase + 2n */
    /* periodic timer heartbeat */
    uint64_t timer_period;      /* instructions per tick (0 = off) */
    uint64_t timer_count;
    int timer_line;             /* which IRQ line the timer raises */
    uint32_t timer_status_off;  /* MMIO offset (addr-0x7000) to set a status bit in; 0xffffffff=none */
    uint16_t timer_status_bits; /* bits to OR into that status reg on each tick */
    uint64_t irq_taken;         /* count of serviced IRQs (telemetry) */
    /* NAND flash controller (regs 0x7850-0x7857) */
    uint8_t *nand;
    uint32_t nand_size;         /* bytes */
    uint32_t nand_page_size;    /* 512 (OOB-stripped) or 528 (with inline OOB) */
    int nand_oob_emul;          /* 1 = present 528-byte logical pages over a 512 image */
    uint16_t nand_7850, nand_7856, nand_cmd, nand_addr_low, nand_addr_high;
    uint32_t nand_eff, nand_cur;
    uint64_t nand_reads;        /* telemetry */
    uint32_t nlog[1024][7];     /* NAND access log: cmd, alo, ahi, 7856, 7850, eff, lpc */
    uint32_t nlog_n;
    uint32_t dlog[2048][5];     /* DMA log: mode, source, dest, length, nand_eff */
    uint32_t dlog_n;
    /* system DMA (0x7a80-0x7a9f, 4 channels x 8 params) */
    uint16_t dma_params[8][4];
    uint16_t dma_status;
    uint64_t dma_runs;          /* telemetry */
    /* banked external (CS) window: 0x200000-0x3fffff -> NAND via 0x7810 */
    uint32_t cs_base;           /* word offset into NAND where cs-space starts */
    uint64_t cs_reads;
    /* debug PC watchpoints: count executions of up to 32 machine addresses */
    uint32_t watch[32];
    uint64_t watch_hits[32];
    int nwatch;
    /* HLE hooks: when LPC == hle_pc[k], run C handler hle_id[k] instead of the fn */
    uint32_t hle_pc[32];
    int      hle_id[32];
    int      n_hle;
    uint64_t hle_calls[32];    /* per-hook invocation counts (telemetry) */
    uint32_t run_until_pc;     /* if nonzero, cpu_run_until stops when LPC hits it */
    /* VFS open handles: file cluster/size/position for HLE'd open+read */
    uint32_t vfh_cluster[16], vfh_size[16], vfh_pos[16];
    int      vfh_used[16];
    uint32_t *wr_hist;          /* optional RAM-write histogram [0x4000], per 1K-word block */
    uint32_t wlog_addr;         /* capture values written to this MMIO addr (0 = off) */
    uint16_t *wlog;             /* captured value stream */
    uint32_t wlog_n, wlog_cap;
    uint16_t spriteram[0x800];  /* PPU sprite table, banked by 0x707e (bank0=[0..3ff] bank1=[400..7ff]) */
    uint16_t snap_spriteram[0x800]; /* snapshot of spriteram + palette + video regs at PPU-enable */
    uint16_t snap_pal[0x100];
    uint16_t snap_regs[0x80];
    int snap_valid;
    uint32_t palwpc[32];        /* distinct LPCs that write the palette 0x7300-0x73ff */
    int palwpc_n;
} Cpu;

/* ---- lifecycle ---- */
Cpu *cpu_new(void) {
    Cpu *c = (Cpu *)calloc(1, sizeof(Cpu));
    c->mem = (uint16_t *)calloc(MEMW, sizeof(uint16_t));
    return c;
}
void cpu_free(Cpu *c) { if (c) { free(c->mem); free(c); } }

void cpu_reset(Cpu *c, uint32_t entry, uint32_t cs) {
    memset(c->r, 0, sizeof(c->r));
    c->sb = 0; c->irq_en = c->fiq_en = c->halted = 0; c->insns = 0;
    c->trapped = 0; c->trap_from = c->trap_to = 0;
    c->bnk = 0; c->divq_bit = 0xffffffffu;
    c->irq_vecbase = 0x6ff0;    /* IRQ0-7 trampoline table in SRAM (line n -> +2n) */
    c->r[PC] = entry & 0xffff;
    c->r[SR] = (uint16_t)((c->r[SR] & 0xffc0) | (cs & 0x3f));
}

void cpu_load(Cpu *c, const uint8_t *data, uint32_t nbytes) {
    uint32_t nwords = nbytes / 2;
    if (nwords > MEMW) nwords = MEMW;
    memcpy(c->mem, data, nwords * 2);   /* host is little-endian: direct copy */
}

/* load the ROM image at a machine word offset (CS0 external space = 0x030000) */
void cpu_load_at(Cpu *c, uint32_t dest_word, const uint8_t *data, uint32_t nbytes) {
    uint32_t nwords = nbytes / 2;
    if (dest_word >= MEMW) return;
    if (dest_word + nwords > MEMW) nwords = MEMW - dest_word;
    memcpy(c->mem + dest_word, data, nwords * 2);
}

/* load the full NAND image (OOB-stripped, 512-byte pages) for the NAND controller */
void cpu_load_nand(Cpu *c, const uint8_t *data, uint32_t nbytes) {
    c->nand = (uint8_t *)malloc(nbytes);
    memcpy(c->nand, data, nbytes);
    c->nand_size = nbytes;
    if (!c->nand_page_size) c->nand_page_size = 512;
}
void cpu_set_nand_page_size(Cpu *c, uint32_t sz) { c->nand_page_size = sz; }
void cpu_set_nand_oob_emul(Cpu *c, int on) { c->nand_oob_emul = on ? 1 : 0; }
void cpu_wrhist_enable(Cpu *c) { if (!c->wr_hist) c->wr_hist = (uint32_t*)calloc(0x4000, sizeof(uint32_t)); }
void cpu_wrhist_reset(Cpu *c) { if (c->wr_hist) memset(c->wr_hist, 0, 0x4000*sizeof(uint32_t)); }
uint32_t cpu_wrhist_get(Cpu *c, uint32_t blk) { return (c->wr_hist && blk < 0x4000) ? c->wr_hist[blk] : 0; }
void cpu_wlog_set(Cpu *c, uint32_t addr) {
    if (!c->wlog) { c->wlog_cap = 1u << 20; c->wlog = (uint16_t*)malloc(c->wlog_cap * 2); }
    c->wlog_addr = addr; c->wlog_n = 0;
}
uint32_t cpu_wlog_n(Cpu *c) { return c->wlog_n; }
uint16_t cpu_wlog_get(Cpu *c, uint32_t i) { return (c->wlog && i < c->wlog_n) ? c->wlog[i] : 0; }
void cpu_wlog_reset(Cpu *c) { c->wlog_n = 0; }
uint16_t cpu_spriteram_get(Cpu *c, uint32_t i) { return (i < 0x800) ? c->spriteram[i] : 0; }
int      cpu_snap_valid(Cpu *c) { return c->snap_valid; }
uint16_t cpu_snap_spr(Cpu *c, uint32_t i) { return (i < 0x800) ? c->snap_spriteram[i] : 0; }
uint16_t cpu_snap_pal(Cpu *c, uint32_t i) { return (i < 0x100) ? c->snap_pal[i] : 0; }
uint16_t cpu_snap_reg(Cpu *c, uint32_t i) { return (i < 0x80) ? c->snap_regs[i] : 0; }
uint32_t cpu_palwpc_n(Cpu *c) { return c->palwpc_n; }
uint32_t cpu_palwpc_get(Cpu *c, uint32_t i) { return (i < (uint32_t)c->palwpc_n) ? c->palwpc[i] : 0; }

/* recompute the effective NAND byte address from the latched regs (MAME formula,
 * page size 512 because our image has the OOB stripped). */
static void nand_recalc(Cpu *c) {
    uint32_t type = c->nand_7856 & 0xf;
    uint32_t page_offset = (c->nand_cmd == 0x01) ? 256 : (c->nand_cmd == 0x50) ? 512 : 0;
    uint32_t nandaddress = ((uint32_t)c->nand_addr_high << 16) | c->nand_addr_low;
    if (c->nand_7850 & 0x4000) nandaddress *= 2;
    uint32_t page = type ? nandaddress : (nandaddress >> 8);
    /* page size = 512 (OOB-stripped) or 528 (reconstructed inline OOB). MAME uses
     * 528; with a reconstructed image + page_size 528 the firmware reads data+OOB. */
    c->nand_eff = page * c->nand_page_size + page_offset;
    c->nand_cur = 0;
    if (c->nlog_n < 1024) {
        uint32_t *e = c->nlog[c->nlog_n++];
        e[0]=c->nand_cmd; e[1]=c->nand_addr_low; e[2]=c->nand_addr_high;
        e[3]=c->nand_7856; e[4]=c->nand_7850; e[5]=c->nand_eff;
        e[6]=(((c->r[SR]&0x3f)<<16)|c->r[PC]) & ADDR_MASK;
    }
}
uint32_t cpu_nlog_n(Cpu *c) { return c->nlog_n; }
uint32_t cpu_nlog_get(Cpu *c, uint32_t i, int f) { return (i<1024 && f>=0 && f<7) ? c->nlog[i][f] : 0; }
void cpu_nlog_reset(Cpu *c) { c->nlog_n = 0; }
uint32_t cpu_dlog_n(Cpu *c) { return c->dlog_n; }
uint32_t cpu_dlog_get(Cpu *c, uint32_t i, int f) { return (i<2048 && f>=0 && f<5) ? c->dlog[i][f] : 0; }
void cpu_dlog_reset(Cpu *c) { c->dlog_n = 0; }

uint64_t cpu_nand_reads(Cpu *c) { return c->nand_reads; }
uint64_t cpu_dma_runs(Cpu *c) { return c->dma_runs; }
uint64_t cpu_cs_reads(Cpu *c) { return c->cs_reads; }
void cpu_set_cs_base(Cpu *c, uint32_t base) { c->cs_base = base; }

/* HLE boot: copy the initial code block from ROM (src) down to RAM (dest) */
void cpu_bootcopy(Cpu *c, uint32_t dest, uint32_t src, uint32_t nwords) {
    for (uint32_t i = 0; i < nwords; i++) {
        uint32_t d = (dest + i) & ADDR_MASK, s = (src + i) & ADDR_MASK;
        c->mem[d] = c->mem[s];
    }
}

/* ---- MMIO config / introspection ---- */
void cpu_set_ready(Cpu *c, uint32_t addr, uint16_t val) {
    if (addr >= MMIO_LO && addr <= MMIO_HI) c->mmio_ready[addr - MMIO_LO] = val;
}
void cpu_set_autoclear(Cpu *c, uint32_t addr, uint16_t mask) {
    if (addr >= MMIO_LO && addr <= MMIO_HI) c->mmio_autoclear[addr - MMIO_LO] = mask;
}
void cpu_set_reador(Cpu *c, uint32_t addr, uint16_t mask) {
    if (addr >= MMIO_LO && addr <= MMIO_HI) c->mmio_reador[addr - MMIO_LO] = mask;
}
void cpu_set_readclear(Cpu *c, uint32_t addr, uint16_t mask) {
    if (addr >= MMIO_LO && addr <= MMIO_HI) c->mmio_readclear[addr - MMIO_LO] = mask;
}
uint32_t cpu_mmio_reads(Cpu *c, uint32_t addr) {
    return (addr >= MMIO_LO && addr <= MMIO_HI) ? c->mmio_reads[addr - MMIO_LO] : 0;
}
uint32_t cpu_mmio_writes(Cpu *c, uint32_t addr) {
    return (addr >= MMIO_LO && addr <= MMIO_HI) ? c->mmio_writes[addr - MMIO_LO] : 0;
}
uint16_t cpu_mmio_last(Cpu *c, uint32_t addr) {
    return (addr >= MMIO_LO && addr <= MMIO_HI) ? c->mmio_last[addr - MMIO_LO] : 0;
}
void cpu_set_cs_trap(Cpu *c, uint32_t limit) { c->cs_trap = limit; }
int cpu_add_watch(Cpu *c, uint32_t addr) {
    if (c->nwatch >= 32) return -1;
    c->watch[c->nwatch] = addr; c->watch_hits[c->nwatch] = 0;
    return c->nwatch++;
}
void cpu_clear_watch(Cpu *c) { c->nwatch = 0; }
uint64_t cpu_watch_hits(Cpu *c, int idx) {
    if (idx < 0 || idx >= c->nwatch) return 0;
    return c->watch_hits[idx];
}
/* HLE hook registration: when LPC == pc, dispatch C handler `id` in lieu of the fn */
int cpu_add_hle(Cpu *c, uint32_t pc, int id) {
    if (c->n_hle >= 32) return -1;
    c->hle_pc[c->n_hle] = pc; c->hle_id[c->n_hle] = id; c->hle_calls[c->n_hle] = 0;
    return c->n_hle++;
}
void cpu_clear_hle(Cpu *c) { c->n_hle = 0; }
uint64_t cpu_hle_calls(Cpu *c, int idx) {
    return (idx >= 0 && idx < c->n_hle) ? c->hle_calls[idx] : 0;
}
uint16_t cpu_stack(Cpu *c, int off) { return c->mem[(c->r[SP] + off) & ADDR_MASK]; }
void cpu_set_timer(Cpu *c, int line, uint64_t period) {
    c->timer_line = line & 7;
    c->timer_period = period;
    c->timer_count = period;
    c->irq_vecbase = 0x6ff0;         /* observed IRQ0-7 trampoline table in SRAM */
    c->timer_status_off = 0xffffffffu;
}
void cpu_set_timer_status(Cpu *c, uint32_t addr, uint16_t bits) {
    if (addr >= MMIO_LO && addr <= MMIO_HI) { c->timer_status_off = addr - MMIO_LO; c->timer_status_bits = bits; }
}
void cpu_raise_irq(Cpu *c, int line) { c->irq_pending |= (uint8_t)(1u << (line & 7)); }
uint64_t cpu_irq_taken(Cpu *c) { return c->irq_taken; }
int cpu_trapped(Cpu *c) { return c->trapped; }
uint32_t cpu_trap_from(Cpu *c) { return c->trap_from; }
uint32_t cpu_trap_to(Cpu *c) { return c->trap_to; }

/* ---- register / memory accessors (for Python-side inspection) ---- */
uint16_t cpu_getreg(Cpu *c, int i) { return c->r[i & 7]; }
void     cpu_setreg(Cpu *c, int i, uint16_t v) { c->r[i & 7] = v; }
uint32_t cpu_getsb(Cpu *c) { return c->sb; }
int      cpu_halted(Cpu *c) { return c->halted; }
uint64_t cpu_insns(Cpu *c) { return c->insns; }
uint32_t cpu_lpc(Cpu *c) { return (((c->r[SR] & 0x3f) << 16) | c->r[PC]) & ADDR_MASK; }
uint16_t cpu_peek(Cpu *c, uint32_t a) { return c->mem[a & ADDR_MASK]; }
/* bulk read `count` words starting at `start` into out[] (fast RAM dump) */
void cpu_read_block(Cpu *c, uint32_t start, uint32_t count, uint16_t *out) {
    for (uint32_t i = 0; i < count; i++) out[i] = c->mem[(start + i) & ADDR_MASK];
}
void     cpu_poke(Cpu *c, uint32_t a, uint16_t v) { c->mem[a & ADDR_MASK] = v; }

/* ---- core internals ---- */
static inline uint32_t lpc(Cpu *c) { return (((c->r[SR] & 0x3f) << 16) | c->r[PC]) & ADDR_MASK; }
static inline void add_lpc(Cpu *c, int32_t off) {
    uint32_t n = (lpc(c) + (uint32_t)off) & ADDR_MASK;
    c->r[PC] = (uint16_t)(n & 0xffff);
    c->r[SR] = (uint16_t)((c->r[SR] & 0xffc0) | ((n >> 16) & 0x3f));
}
static inline uint32_t lreg_i(Cpu *c, int reg) {
    return (((c->r[SR] << 6) & 0x3f0000) | c->r[reg]) & ADDR_MASK;
}
static void dma_trigger(Cpu *c, int ch);   /* forward decl (defined after read/write16) */

static inline uint16_t read16(Cpu *c, uint32_t a) {
    a &= ADDR_MASK;
    if (a >= MMIO_LO && a <= MMIO_HI) {
        uint32_t o = a - MMIO_LO;
        c->mmio_reads[o]++;
        /* NAND controller */
        if (a == 0x7850) return (uint16_t)(c->nand_7850 | 0x8000);   /* status: ready */
        if (a == 0x7abf) return c->dma_status;                       /* system DMA status */
        if (a == 0x7854 && c->nand) {
            uint32_t C = c->nand_cur;
            c->nand_cur++;
            c->nand_reads++;
            if (c->nand_cmd == 0x70) return 0x0000;                  /* read status: no error */
            if (c->nand_cmd == 0x90) {                               /* read ident (romtype 2) */
                static const uint8_t id[8] = {0xad, 0xf1, 0x80, 0x1d, 0, 0, 0, 0};
                return id[C < 8 ? C : 7];
            }
            if (c->nand_oob_emul) {
                /* firmware reads 528-byte pages (512 data + 16 OOB); our image is
                 * OOB-stripped 512-byte pages. Map the stream: keep eff=page*512,
                 * insert 16 synthetic OOB bytes after each 512 data bytes. */
                uint32_t pg = C / 528u, off = C % 528u;
                if (off < 512u) {
                    uint32_t addr = c->nand_eff + pg * 512u + off;
                    return (addr < c->nand_size) ? c->nand[addr] : 0x00;
                }
                return 0xff;                                         /* synthetic OOB */
            }
            uint32_t addr = c->nand_eff + C;
            return (addr < c->nand_size) ? c->nand[addr] : 0x00;     /* data */
        }
        uint16_t v = c->mmio_has[o] ? (uint16_t)(c->mmio_last[o] & ~c->mmio_autoclear[o])
                                    : c->mmio_ready[o];
        v |= c->mmio_reador[o];
        if (c->mmio_readclear[o]) c->mmio_last[o] &= (uint16_t)~c->mmio_readclear[o];
        return v;
    }
    /* NOTE: 0x020000-0x1fffff is SDRAM the firmware writes to (not cs-space) —
     * mapping it to NAND breaks boot. Left as c->mem. */
    /* banked external window -> NAND (MAME cs_bank_space_r: bank = 0x7810 & 0x3f) */
    if (a >= 0x200000 && a <= 0x3fffff && c->nand) {
        uint32_t bank = c->mmio_last[0x7810 - MMIO_LO] & 0x3f;
        uint32_t realoffset = (a - 0x200000) + bank * 0x200000u - 0x20000u + c->cs_base;
        c->cs_reads++;
        uint32_t b = realoffset * 2;
        if (b + 1 < c->nand_size) return (uint16_t)(c->nand[b] | (c->nand[b + 1] << 8));
        return 0;
    }
    return c->mem[a];
}
static inline void write16(Cpu *c, uint32_t a, uint16_t v) {
    a &= ADDR_MASK;
    if (a >= MMIO_LO && a <= MMIO_HI) {
        uint32_t o = a - MMIO_LO;
        c->mmio_writes[o]++; c->mmio_last[o] = v; c->mmio_has[o] = 1;
        if (a == c->wlog_addr && c->wlog && c->wlog_n < c->wlog_cap) c->wlog[c->wlog_n++] = v;
        /* record distinct PCs that write the palette (the eye color loader) */
        if (a >= 0x7300 && a <= 0x73ff) {
            uint32_t lpc = (((uint32_t)(c->r[SR] & 0x3f)) << 16) | c->r[PC];
            int seen = 0;
            for (int i = 0; i < c->palwpc_n; i++) if (c->palwpc[i] == lpc) { seen = 1; break; }
            if (!seen && c->palwpc_n < 32) c->palwpc[c->palwpc_n++] = lpc;
        }
        /* PPU sprite table 0x7400-0x77ff, banked by 0x707e (MAME spriteram_w) */
        if (a >= 0x7400 && a <= 0x77ff) {
            uint32_t off = a - 0x7400;
            if (c->mmio_last[0x707e - MMIO_LO] & 1) off += 0x400;
            c->spriteram[off] = v;
        }
        /* snapshot the whole PPU state at each PPU-enable write (0x707f) — captures
         * the render moment before the firmware clears the table for the next frame */
        if (a == 0x707f && v) {
            memcpy(c->snap_spriteram, c->spriteram, sizeof(c->spriteram));
            for (int i = 0; i < 0x100; i++) c->snap_pal[i]  = c->mmio_last[(0x7300 - MMIO_LO) + i];
            for (int i = 0; i < 0x80;  i++) c->snap_regs[i] = c->mmio_last[(0x7000 - MMIO_LO) + i];
            c->snap_valid = 1;
        }
        /* NAND controller */
        switch (a) {
        case 0x7850: c->nand_7850 = v; break;
        case 0x7851: c->nand_cmd = v; break;
        case 0x7852: c->nand_addr_low = v; break;
        case 0x7853: c->nand_addr_high = v; nand_recalc(c); break;
        case 0x7856: c->nand_7856 = v; nand_recalc(c); break;
        default: break;
        }
        /* system DMA: 4 channels x 8 params at 0x7a80; write to param0 bit0 triggers */
        if (a >= 0x7a80 && a <= 0x7a9f) {
            int ch = (a - 0x7a80) >> 3, off = (a - 0x7a80) & 7;
            c->dma_params[off][ch] = v;
            if (off == 0 && (v & 1)) dma_trigger(c, ch);
        }
        return;
    }
    if (c->wr_hist) c->wr_hist[(a >> 10) & 0x3fff]++;   /* RAM-write histogram (per 1K words) */
    c->mem[a] = v;
}
/* system DMA transfer (MAME gpl_dma trigger_systemm_dma). source/dest are word
 * addresses; NAND loads use source == 0x7854 (the streaming data port). */
static void dma_trigger(Cpu *c, int ch) {
    uint16_t mode = c->dma_params[0][ch];
    uint32_t source = c->dma_params[1][ch] | ((uint32_t)c->dma_params[4][ch] << 16);
    uint32_t dest   = c->dma_params[2][ch] | ((uint32_t)c->dma_params[5][ch] << 16);
    uint32_t length = c->dma_params[3][ch] | ((uint32_t)c->dma_params[6][ch] << 16);
    int sd = ((mode & 0xa0) == 0x00) ? 1 : ((mode & 0xa0) == 0x20) ? -1 : 0;
    int dd = ((mode & 0x50) == 0x00) ? 1 : ((mode & 0x50) == 0x10) ? -1 : 0;
    source &= 0x0fffffff;
    length &= 0x0fffffff;
    if (c->dlog_n < 2048) {
        uint32_t *e = c->dlog[c->dlog_n++];
        e[0]=mode; e[1]=source; e[2]=dest; e[3]=length; e[4]=c->nand_eff + c->nand_cur;
    }
    for (uint32_t i = 0; i < length; i++) {
        uint16_t val;
        if (mode & 0x1000) {                 /* byte source: two reads -> one word */
            val = (uint16_t)((read16(c, source) & 0xff) | (read16(c, source) << 8));
            i++;
        } else {
            val = read16(c, source);
        }
        source += sd;
        if (mode & 0x2000) {                 /* byte dest */
            write16(c, dest, val & 0xff); dest += dd;
            write16(c, dest, val >> 8);
        } else {
            write16(c, dest, val);
        }
        dest += dd;
    }
    c->dma_params[0][ch] &= 0x00f7;
    for (int k = 1; k < 7; k++) c->dma_params[k][ch] = 0;
    c->dma_status |= (1u << ch);
    c->dma_runs++;
}

static inline void push(Cpu *c, uint16_t v, int reg) {
    write16(c, c->r[reg], v);
    c->r[reg] = (uint16_t)(c->r[reg] - 1);
}
static inline uint16_t pop(Cpu *c, int reg) {
    c->r[reg] = (uint16_t)(c->r[reg] + 1);
    return read16(c, c->r[reg]);
}
static inline void upd_nzsc(Cpu *c, uint32_t val, uint16_t r0, uint16_t r1) {
    uint16_t sr = c->r[SR] & ~(FN | FZ | FS | FC);
    if (((val >> 16) & 1) != (uint32_t)(((r0 ^ r1) >> 15) & 1)) sr |= FS;
    if ((val >> 15) & 1) sr |= FN;
    if ((val & 0xffff) == 0) sr |= FZ;
    if ((val >> 16) & 1) sr |= FC;
    c->r[SR] = sr;
}
static inline void upd_nz(Cpu *c, uint32_t val) {
    uint16_t sr = c->r[SR] & ~(FN | FZ);
    if (val & 0x8000) sr |= FN;
    if ((val & 0xffff) == 0) sr |= FZ;
    c->r[SR] = sr;
}

/* returns 1 if the ALU result should be written back */
static int do_alu(Cpu *c, int op0, uint32_t *lres, uint16_t r0, uint16_t r1, uint32_t r2, int upd) {
    uint32_t res;
    switch (op0) {
    case 0x0: res = (uint32_t)r0 + r1; if (upd) upd_nzsc(c, res, r0, r1); break;
    case 0x1: { uint32_t cc = (c->r[SR] & FC) ? 1 : 0; res = (uint32_t)r0 + r1 + cc; if (upd) upd_nzsc(c, res, r0, r1); break; }
    case 0x2: { uint16_t n = ~r1; res = (uint32_t)r0 + n + 1; if (upd) upd_nzsc(c, res, r0, n); break; }
    case 0x3: { uint32_t cc = (c->r[SR] & FC) ? 1 : 0; uint16_t n = ~r1; res = (uint32_t)r0 + n + cc; if (upd) upd_nzsc(c, res, r0, n); break; }
    case 0x4: { uint16_t n = ~r1; res = (uint32_t)r0 + n + 1; if (upd) upd_nzsc(c, res, r0, n); *lres = res; return 0; }
    case 0x6: res = (uint32_t)(-(int32_t)r1) & 0x1ffff; if (upd) upd_nz(c, res); break;
    case 0x8: res = (uint32_t)r0 ^ r1; if (upd) upd_nz(c, res); break;
    case 0x9: res = r1; if (upd) upd_nz(c, res); break;
    case 0xa: res = (uint32_t)r0 | r1; if (upd) upd_nz(c, res); break;
    case 0xb: res = (uint32_t)r0 & r1; if (upd) upd_nz(c, res); break;
    case 0xc: res = (uint32_t)r0 & r1; if (upd) upd_nz(c, res); *lres = res; return 0;
    case 0xd: write16(c, r2, r0); *lres = r0; return 0;
    default: *lres = 0; return 0;
    }
    *lres = res;
    return 1;
}

static void execute(Cpu *c, uint16_t op);

static void exec_remaining(Cpu *c, uint16_t op) {
    int op0 = op >> 12, opa = (op >> 9) & 7, op1 = (op >> 6) & 7, opn = (op >> 3) & 7, opb = op & 7;
    int lower = (op1 << 4) | op0;

    if (lower == 0x2d) {                 /* push */
        int r0 = opn, r1 = opa;
        while (r0) { push(c, c->r[r1], opb); r1--; r0--; }
        return;
    }
    if (lower == 0x29) {                 /* pop / reti / retf */
        if (op == 0x9a98) { c->r[SR] = pop(c, SP); c->r[PC] = pop(c, SP); c->in_irq = 0; return; }  /* reti */
        if (op == 0x9a90) { c->r[SR] = pop(c, SP); c->r[PC] = pop(c, SP); return; }                 /* retf */
        int r0 = opn, r1 = opa;
        while (r0) { r1++; c->r[r1] = pop(c, opb); r0--; }
        return;
    }

    uint16_t r0 = c->r[opa], r1 = 0;
    uint32_t r2 = 0;

    switch (op1) {
    case 0x0:
        r2 = (uint16_t)(c->r[BP] + (op & 0x3f));
        if (op0 != 0xd) r1 = read16(c, r2);
        break;
    case 0x1:
        r1 = op & 0x3f;
        break;
    case 0x3: {
        int lsb = opn & 3;
        if (opn & 4) {
            if (lsb == 3) { c->r[opb]++; if (c->r[opb] == 0) c->r[SR] += 0x0400; }
            r2 = lreg_i(c, opb);
            if (op0 != 0xd) r1 = read16(c, r2);
            if (lsb == 1) { c->r[opb]--; if (c->r[opb] == 0xffff) c->r[SR] -= 0x0400; }
            else if (lsb == 2) { c->r[opb]++; if (c->r[opb] == 0) c->r[SR] += 0x0400; }
        } else {
            if (lsb == 3) { c->r[opb]++; }
            r2 = c->r[opb];
            if (op0 != 0xd) r1 = read16(c, r2);
            if (lsb == 1) c->r[opb]--;
            else if (lsb == 2) c->r[opb]++;
        }
        break;
    }
    case 0x4:
        if (opn == 0) { r1 = c->r[opb]; }
        else if (opn == 1) { r0 = c->r[opb]; r1 = read16(c, lpc(c)); add_lpc(c, 1); }
        else if (opn == 2) { r0 = c->r[opb]; r2 = read16(c, lpc(c)); add_lpc(c, 1); if (op0 != 0xd) r1 = read16(c, r2); }
        else if (opn == 3) { r1 = r0; r0 = c->r[opb]; r2 = read16(c, lpc(c)); add_lpc(c, 1); }
        else { uint32_t sh = ((uint32_t)c->r[opb] << 4) | c->sb; if (sh & 0x80000) sh |= 0xf00000; sh >>= (opn - 3); c->sb = sh & 0xf; r1 = (uint16_t)(sh >> 4); }
        break;
    case 0x5:
        if (opn & 4) { uint32_t sh = (((uint32_t)c->r[opb] << 4) | c->sb) >> (opn - 3); c->sb = sh & 0xf; r1 = (uint16_t)(sh >> 4); }
        else { uint32_t sh = (((uint32_t)c->sb << 16) | c->r[opb]) << (opn + 1); c->sb = (sh >> 16) & 0xf; r1 = (uint16_t)sh; }
        break;
    case 0x6: {
        uint64_t sh = ((((uint64_t)c->sb << 16) | c->r[opb]) << 4) | c->sb;
        if (opn & 4) { sh >>= (opn - 3); c->sb = sh & 0xf; }
        else { sh <<= (opn + 1); c->sb = (sh >> 20) & 0xf; }
        r1 = (uint16_t)(sh >> 4);
        break;
    }
    case 0x7:
        r2 = op & 0x3f;
        r1 = read16(c, r2);
        break;
    default: break;
    }

    uint32_t lres = 0;
    int wr = do_alu(c, op0, &lres, r0, r1, r2, opa != 7);
    if (wr) {
        if (op1 == 0x4 && opn == 0x3) write16(c, r2, (uint16_t)lres);
        else c->r[opa] = (uint16_t)lres;
    }
}

static void exec_jumps(Cpu *c, uint16_t op) {
    int op0 = (op >> 12) & 15, op1 = (op >> 6) & 7;
    uint32_t imm = op & 0x3f;
    uint16_t sr = c->r[SR];
    int take = 0;
    switch (op0) {
    case 0: take = !(sr & FC); break;
    case 1: take = !!(sr & FC); break;
    case 2: take = !(sr & FS); break;
    case 3: take = !!(sr & FS); break;
    case 4: take = !(sr & FZ); break;
    case 5: take = !!(sr & FZ); break;
    case 6: take = !(sr & FN); break;
    case 7: take = !!(sr & FN); break;
    case 8: take = (sr & (FZ | FC)) != FC; break;
    case 9: take = (sr & (FZ | FC)) == FC; break;
    case 10: take = !!(sr & (FZ | FS)); break;
    case 11: take = !(sr & (FZ | FS)); break;
    case 12: take = ((sr & FN) >> N_SH) == ((sr & FS) >> S_SH); break;
    case 13: take = ((sr & FN) >> N_SH) != ((sr & FS) >> S_SH); break;
    case 14: take = 1; break;
    }
    if (take) add_lpc(c, (op1 == 0) ? (int32_t)imm : -(int32_t)imm);
}

static void mul_op(Cpu *c, uint16_t op) {
    /* MUL uu/us/su/ss -> MR (R4:R3). Sign bits: srd=op[8], srs=op[12] (MAME) */
    int rd = (op >> 9) & 7, rs = op & 7;
    int srd = (op >> 8) & 1, srs = (op >> 12) & 1;
    uint32_t p = (uint32_t)c->r[rd] * c->r[rs];
    if (srs && (c->r[rs] & 0x8000)) p -= (uint32_t)c->r[rd] << 16;
    if (srd && (c->r[rd] & 0x8000)) p -= (uint32_t)c->r[rs] << 16;
    c->r[R3] = p & 0xffff; c->r[R4] = (p >> 16) & 0xffff;
}

/* unified 16-register access: 0-7 = main (sp,r1-r4,bp,sr,pc), 8-15 = ext (r8-r15) */
static inline uint16_t rget(Cpu *c, int i) { return (i & 8) ? c->ext[i & 7] : c->r[i & 7]; }
static inline void rset(Cpu *c, int i, uint16_t v) { if (i & 8) c->ext[i & 7] = v; else c->r[i & 7] = v; }

/* ISA 2.0 EXTOP (0xff80 prefix + ximm). Implements the forms the firmware uses:
 * register push/pop of the extended bank, and 2-param register ALU. */
static void exec_extop(Cpu *c, uint16_t x) {
    int sub = (x & 0x01f0) >> 4;
    if (sub == 0x02) {                         /* Ext push / pop */
        int rb = x & 0x000f;                   /* stack pointer register */
        int size = (x & 0x7000) >> 12; if (size == 0) size = 8;
        int rx = (x & 0x0e00) >> 9;
        int spreg = rb & 7;
        if (x & 0x8000) {                      /* push extregs[rx..end] to [rb] */
            for (int i = 0; i < size; i++) push(c, c->ext[(rx - i) & 7], spreg);
        } else {                               /* pop into extregs[start..] */
            int start = (rx + 1) & 7;
            for (int i = 0; i < size; i++) c->ext[(start + i) & 7] = pop(c, spreg);
        }
        return;
    }
    if (sub == 0x00 || sub == 0x10) {          /* Ra = Ra op Rb (16-reg) */
        int aluop = (x & 0xf000) >> 12;
        int rb = (x & 0x000f);
        int ra = ((x & 0x0e00) >> 9) | ((x & 0x0100) >> 5);
        uint32_t lres = 0;
        int wr = do_alu(c, aluop, &lres, rget(c, ra), rget(c, rb), 0, 1);
        if (wr) rset(c, ra, (uint16_t)lres);
        return;
    }
    /* other extended forms (imm16 ALU, shifts) not yet used by this firmware */
}

static void exec_fxxx(Cpu *c, uint16_t op) {
    if (op == 0xff80) {                        /* EXTOP: 2-word extended instruction */
        uint16_t x = read16(c, lpc(c)); add_lpc(c, 1);
        exec_extop(c, x);
        return;
    }
    int sub = (op & 0x01c0) >> 6;
    if (sub == 1 && (op & 0xf3c0) == 0xf040) {                 /* call A22 */
        uint16_t imm = read16(c, lpc(c)); add_lpc(c, 1);
        push(c, c->r[PC], SP); push(c, c->r[SR], SP);
        c->r[PC] = imm; c->r[SR] = (uint16_t)((c->r[SR] & 0xffc0) | (op & 0x3f));
        return;
    }
    if (sub == 2 && (op & 0xffc0) == 0xfe80) {                 /* goto A22 */
        uint16_t t = read16(c, lpc(c));
        c->r[PC] = t; c->r[SR] = (uint16_t)((c->r[SR] & 0xffc0) | (op & 0x3f));
        return;
    }
    if (sub == 3 && (op & 0xffc0) == 0xfec0) {                 /* goto mr */
        c->r[PC] = c->r[R3]; c->r[SR] = (uint16_t)((c->r[SR] & 0xffc0) | (c->r[R4] & 0x3f));
        return;
    }
    if (sub == 0) {
        if ((op & 0xffc0) == 0xfe00) { c->r[SR] = (uint16_t)((c->r[SR] & 0x03ff) | ((op & 0x3f) << 10)); return; }
        if ((op & 0xf1f8) == 0xf020) { c->r[op & 7] = (c->r[SR] >> 10) & 0x3f; return; }
        if ((op & 0xf1f8) == 0xf028) { c->r[SR] = (uint16_t)((c->r[SR] & 0x03ff) | ((c->r[op & 7] & 0x3f) << 10)); return; }
        if ((op & 0xf1f8) == 0xf030 || (op & 0xf1f8) == 0xf038) return;
        mul_op(c, op); return;
    }
    if (sub == 5) {
        int low = op & 0xff;
        switch (low) {
        case 0x40: c->irq_en = c->fiq_en = 0; break;
        case 0x41: c->irq_en = 1; break;
        case 0x42: c->fiq_en = 1; break;
        case 0x43: c->irq_en = c->fiq_en = 1; break;
        case 0x48: c->irq_en = 0; break;
        case 0x49: c->irq_en = 1; break;
        case 0x4c: c->fiq_en = 0; break;
        case 0x4e: c->fiq_en = 1; break;
        case 0x4a:                                           /* secbank off */
            if (c->bnk) for (int k = 0; k < 4; k++) { uint16_t t = c->r[R1 + k]; c->r[R1 + k] = c->secbank[k]; c->secbank[k] = t; }
            c->bnk = 0; break;
        case 0x4b:                                           /* secbank on */
            if (!c->bnk) for (int k = 0; k < 4; k++) { uint16_t t = c->r[R1 + k]; c->r[R1 + k] = c->secbank[k]; c->secbank[k] = t; }
            c->bnk = 1; break;
        case 0x64: case 0x6c: case 0x74: case 0x7c: {        /* EXP: R2 = (leading same-bits of R4) - 1 */
            uint16_t r4 = c->r[R4]; int n = 0;
            if (r4 & 0x8000) { uint16_t x = r4; while ((x & 0x8000) && n < 16) { n++; x <<= 1; } }
            else             { uint16_t x = r4; while (!(x & 0x8000) && n < 16) { n++; x <<= 1; } }
            c->r[R2] = (uint16_t)(n - 1);
            break;
        }
        case 0x63: case 0x6b: case 0x73: case 0x7b: {        /* DIVQ: one iterative division step */
            if (c->divq_bit == 0xffffffffu) {
                c->divq_bit = 15;
                c->divq_dividend = ((uint32_t)c->r[R4] << 16) | c->r[R3];
                c->divq_divisor = c->r[R2];
                c->divq_a = 0;
            }
            int aq = (c->divq_a >> 31) & 1;
            if (aq) c->divq_a += c->divq_a + ((c->divq_dividend >> 15) & 1) + c->divq_divisor;
            else    c->divq_a += c->divq_a + ((c->divq_dividend >> 15) & 1) - c->divq_divisor;
            c->divq_dividend <<= 1;
            c->divq_dividend++;
            c->divq_dividend ^= ((c->divq_a >> 31) & 1);
            c->r[R3] = (uint16_t)c->divq_dividend;
            c->divq_bit--;
            break;
        }
        case 0x60: case 0x68: case 0x70: case 0x78: c->halted = 1; break;
        case 0x65: case 0x6d: case 0x75: case 0x7d: break;   /* nop */
        case 0x61: case 0x69: case 0x71: case 0x79:          /* call mr */
            push(c, c->r[PC], SP); push(c, c->r[SR], SP);
            c->r[PC] = c->r[R3]; c->r[SR] = (uint16_t)((c->r[SR] & 0xffc0) | (c->r[R4] & 0x3f));
            break;
        default: break;
        }
        return;
    }
    mul_op(c, op);
}

static void exec_exxx(Cpu *c, uint16_t op) {
    if ((op & 0xf1c8) == 0xe000 || (op & 0xf1c0) == 0xe040) {
        int b = (op >> 4) & 3, rd = (op >> 9) & 7;
        int bit = ((op & 0xf1c0) == 0xe040) ? (op & 0xf) : (c->r[op & 7] & 0xf);
        uint16_t v = c->r[rd], mask = 1u << bit;
        if (b == 0) { c->r[SR] = (c->r[SR] & ~FZ) | ((v & mask) ? 0 : FZ); return; }
        else if (b == 1) v |= mask; else if (b == 2) v &= ~mask; else v ^= mask;
        c->r[rd] = v; return;
    }
    if ((op & 0xf1c0) == 0xe180 || (op & 0xf1c0) == 0xe1c0 || (op & 0xf1c8) == 0xe100 || (op & 0xf1c8) == 0xe140) {
        int b = (op >> 4) & 3, rd = (op >> 9) & 7;
        int ds = ((op & 0xf1c0) == 0xe1c0) || ((op & 0xf1c8) == 0xe140);
        int bit = ((op & 0xf1c0) == 0xe180 || (op & 0xf1c0) == 0xe1c0) ? (op & 0xf) : (c->r[op & 7] & 0xf);
        uint32_t addr = ds ? lreg_i(c, rd) : c->r[rd];
        uint16_t v = read16(c, addr), mask = 1u << bit;
        if (b == 0) { c->r[SR] = (c->r[SR] & ~FZ) | ((v & mask) ? 0 : FZ); return; }
        else if (b == 1) v |= mask; else if (b == 2) v &= ~mask; else v ^= mask;
        write16(c, addr, v); return;
    }
    if ((op & 0xf188) == 0xe108) {          /* 16-bit variable shift (rd = rd <sh> rs) */
        int rd = (op >> 9) & 7, sh = (op >> 4) & 7;
        int shift = c->r[op & 7] & 0x1f;
        uint16_t rdv = c->r[rd];
        switch (sh) {
        case 0: {                            /* asr (arithmetic) */
            int32_t v = (int32_t)(int16_t)rdv;
            c->r[rd] = (uint16_t)(v >> (shift > 31 ? 31 : shift));
            break;
        }
        case 1: {                            /* asror -> R3|=lo, R4=hi (multi-word) */
            int32_t rdval = (int32_t)((uint32_t)rdv << 16);
            uint32_t res = (uint32_t)(rdval >> shift);
            c->r[R3] |= (uint16_t)res; c->r[R4] = (uint16_t)(res >> 16);
            break;
        }
        case 2:                              /* lsl */
            c->r[rd] = (uint16_t)((uint32_t)rdv << shift);
            break;
        case 3: {                            /* lslor -> R3=lo, R4|=hi */
            uint32_t res = (uint32_t)rdv << shift;
            c->r[R3] = (uint16_t)res; c->r[R4] |= (uint16_t)(res >> 16);
            break;
        }
        case 4:                              /* lsr */
            c->r[rd] = (uint16_t)((uint32_t)rdv >> shift);
            break;
        case 5: {                            /* lsror -> R3|=lo, R4=hi */
            uint32_t res = ((uint32_t)rdv << 16) >> shift;
            c->r[R3] |= (uint16_t)res; c->r[R4] = (uint16_t)(res >> 16);
            break;
        }
        case 6: c->r[rd] = shift ? (uint16_t)((rdv << shift) | (rdv >> (16 - (shift & 15)))) : rdv; break; /* rol */
        case 7: c->r[rd] = shift ? (uint16_t)((rdv >> shift) | (rdv << (16 - (shift & 15)))) : rdv; break; /* ror */
        }
        return;
    }
    if ((op & 0xf1f8) == 0xe008 || (op & 0xf180) == 0xe080) { mul_op(c, op); return; }
}

static void execute(Cpu *c, uint16_t op) {
    int op0 = op >> 12, opa = (op >> 9) & 7, op1 = (op >> 6) & 7;
    if (op0 == 0xf) exec_fxxx(c, op);
    else if (op0 < 0xf && opa == 7 && op1 < 2) exec_jumps(c, op);
    else if (op0 == 0xe) exec_exxx(c, op);
    else exec_remaining(c, op);
}

/* ---- FAT32 filesystem HLE helpers (Furby NAND, verified geometry) ----------
 * 512B sectors, 1 sector/cluster, 60 reserved sectors, 2 FATs x 1730 sectors.
 * FAT area starts at byte 60*512 = 0x7800; data region (cluster 2) at 0x1b8000.
 * cluster N -> byte 0x1b8000 + (N-2)*512. */
#define FAT_BYTES_PER_SEC   512u
#define FAT_FAT_START_BYTE  0x7800u
#define FAT_DATA_START_BYTE 0x1b8000u

static int hle_fs_debug = 0;   /* set via cpu_set_hle_debug for tracing */

static inline uint8_t fat_nb(Cpu *c, uint32_t off) {
    return (c->nand && off < c->nand_size) ? c->nand[off] : 0;
}
static uint32_t fat_next_cluster(Cpu *c, uint32_t cl) {
    uint32_t o = FAT_FAT_START_BYTE + cl * 4u;
    uint32_t v = (uint32_t)fat_nb(c, o) | ((uint32_t)fat_nb(c, o + 1) << 8) |
                 ((uint32_t)fat_nb(c, o + 2) << 16) | ((uint32_t)fat_nb(c, o + 3) << 24);
    return v & 0x0fffffffu;
}
static int fat_ci_eq(const char *a, const char *b) {
    while (*a && *b) {
        char ca = *a, cb = *b;
        if (ca >= 'a' && ca <= 'z') ca -= 32;
        if (cb >= 'a' && cb <= 'z') cb -= 32;
        if (ca != cb) return 0;
        a++; b++;
    }
    return *a == 0 && *b == 0;
}
/* Search directory (cluster chain starting at dir_cluster; <2 means root=2) for
 * component `want` (case-insensitive, matched vs LFN or 8.3 dotted name).
 * On match fills out_cluster/out_size/out_isdir and returns 1. */
static int fat_find_in_dir(Cpu *c, uint32_t dir_cluster, const char *want,
                           uint32_t *out_cluster, uint32_t *out_size, int *out_isdir) {
    static const int lfn_offs[13] = {1,3,5,7,9, 14,16,18,20,22,24, 28,30};
    uint32_t cl = (dir_cluster < 2) ? 2u : dir_cluster;
    char lfn[300]; int lfn_len = 0, have_lfn = 0;
    int guard = 0;
    while (cl >= 2 && cl < 0x0ffffff8u && guard++ < 200000) {
        uint32_t base = FAT_DATA_START_BYTE + (cl - 2) * FAT_BYTES_PER_SEC;
        for (uint32_t ei = 0; ei < FAT_BYTES_PER_SEC / 32u; ei++) {
            uint32_t eo = base + ei * 32u;
            uint8_t b0 = fat_nb(c, eo);
            if (b0 == 0x00) return 0;                       /* end of directory */
            if (b0 == 0xe5) { have_lfn = 0; lfn_len = 0; continue; }  /* deleted */
            uint8_t attr = fat_nb(c, eo + 11);
            if (attr == 0x0f) {                             /* LFN piece */
                uint32_t seq = b0 & 0x1fu;
                if (seq >= 1 && seq <= 20) {
                    int posbase = (int)(seq - 1) * 13;
                    for (int j = 0; j < 13; j++) {
                        uint16_t ch = fat_nb(c, eo + lfn_offs[j]) |
                                      ((uint16_t)fat_nb(c, eo + lfn_offs[j] + 1) << 8);
                        int pos = posbase + j;
                        if (ch != 0 && ch != 0xffff && pos < 299) {
                            lfn[pos] = (char)(ch & 0xff);
                            if (pos + 1 > lfn_len) lfn_len = pos + 1;
                        }
                    }
                    have_lfn = 1;
                }
                continue;
            }
            /* regular 8.3 entry: build dotted short name */
            char sfn[13]; int si = 0;
            for (int k = 0; k < 8; k++) { uint8_t ch = fat_nb(c, eo + k); if (ch != ' ') sfn[si++] = (char)ch; }
            int xi = 0; char ext[4];
            for (int k = 8; k < 11; k++) { uint8_t ch = fat_nb(c, eo + k); if (ch != ' ') ext[xi++] = (char)ch; }
            if (xi > 0) { sfn[si++] = '.'; for (int k = 0; k < xi; k++) sfn[si++] = ext[k]; }
            sfn[si] = 0;
            if (have_lfn && lfn_len > 0 && lfn_len < 300) lfn[lfn_len] = 0; else have_lfn = 0;
            int match = (have_lfn && fat_ci_eq(want, lfn)) || fat_ci_eq(want, sfn);
            if (match) {
                uint32_t clo = fat_nb(c, eo + 0x1a) | ((uint32_t)fat_nb(c, eo + 0x1b) << 8);
                uint32_t chi = fat_nb(c, eo + 0x14) | ((uint32_t)fat_nb(c, eo + 0x15) << 8);
                *out_cluster = (chi << 16) | clo;
                *out_size = fat_nb(c, eo + 0x1c) | ((uint32_t)fat_nb(c, eo + 0x1d) << 8) |
                            ((uint32_t)fat_nb(c, eo + 0x1e) << 16) | ((uint32_t)fat_nb(c, eo + 0x1f) << 24);
                *out_isdir = (attr & 0x10) ? 1 : 0;
                return 1;
            }
            have_lfn = 0; lfn_len = 0;
        }
        cl = fat_next_cluster(c, cl);
    }
    return 0;
}

/* resolve a UTF-16 far-pointer path (machine word addr in c->mem) against the FAT;
 * returns 1 + the file's start cluster & size on success. */
static int vfs_resolve(Cpu *c, uint32_t ptr, uint32_t *out_cluster, uint32_t *out_size) {
    char path[300]; int n = 0;
    for (int i = 0; i < 299; i++) {
        uint16_t w = c->mem[(ptr + i) & ADDR_MASK];
        if (w == 0) break;
        path[n++] = (char)(w & 0xff);
    }
    path[n] = 0;
    char *p = path;
    if (n >= 2 && p[1] == ':') p += 2;
    while (*p == '\\' || *p == '/') p++;
    uint32_t cur = 2, fsize = 0; int found = 1;
    while (*p) {
        char comp[300]; int ci = 0;
        while (*p && *p != '\\' && *p != '/') { if (ci < 299) comp[ci++] = *p; p++; }
        comp[ci] = 0;
        while (*p == '\\' || *p == '/') p++;
        if (ci == 0) continue;
        uint32_t ncl, nsz; int nd;
        if (!fat_find_in_dir(c, cur, comp, &ncl, &nsz, &nd)) { found = 0; break; }
        cur = ncl; fsize = nsz;
    }
    if (found) { *out_cluster = cur; *out_size = fsize; }
    return found;
}

/* HLE handlers. Return 1 if handled (skip normal execution of this instruction).
 * Called at function ENTRY (before the prologue), so we read args off the stack
 * (unSP: args pushed right-to-left; a `call` then pushed PC,SR) and return via
 * retf semantics (pop SR, PC), leaving the caller to clean the args. */
static int hle_dispatch(Cpu *c, int id) {
    uint32_t sp = c->r[SP];
    if (id == 1) {
        /* 0x076e8d(sector_lo, sector_hi, dest_lo, dest_hi): read one 512-byte
         * logical sector into the dest buffer. Identity FTL: byte = sector*512. */
        uint16_t slo = c->mem[(uint16_t)(sp + 3)];
        uint16_t shi = c->mem[(uint16_t)(sp + 4)];
        uint16_t dlo = c->mem[(uint16_t)(sp + 5)];
        uint16_t dhi = c->mem[(uint16_t)(sp + 6)];
        uint32_t sector = ((uint32_t)shi << 16) | slo;
        uint32_t dest = (((uint32_t)dhi << 16) | dlo) & ADDR_MASK;
        uint32_t nb = sector * 512u;
        for (int i = 0; i < 256; i++) {
            uint16_t w = 0;
            if (c->nand && (nb + 2 * i + 1) < c->nand_size)
                w = (uint16_t)(c->nand[nb + 2 * i] | (c->nand[nb + 2 * i + 1] << 8));
            write16(c, (dest + i) & ADDR_MASK, w);
        }
        c->r[R1] = 0;               /* success */
        c->r[R2] = 0;
        /* retf: pop SR then PC */
        c->r[SR] = c->mem[(uint16_t)(sp + 1)];
        c->r[PC] = c->mem[(uint16_t)(sp + 2)];
        c->r[SP] = (uint16_t)(sp + 2);
        return 1;
    }
    if (id == 2) {
        /* 0x078730 find-file-by-name(far_ptr): far_ptr at [SP+3](lo):[SP+4](hi),
         * a machine WORD address into c->mem; filename is UTF-16 (one code unit
         * per word, low byte = ASCII). Resolve against the FAT32 filesystem in
         * c->nand; return the file size in r1(lo):r2(hi), 0xffffffff = not found
         * (caller success test: r2 != 0xffff AND r1 != 0xffff). */
        uint16_t plo = c->mem[(uint16_t)(sp + 3)];
        uint16_t phi = c->mem[(uint16_t)(sp + 4)];
        uint32_t ptr = (((uint32_t)phi << 16) | plo) & ADDR_MASK;
        char path[300]; int n = 0;
        for (int i = 0; i < 299; i++) {
            uint16_t w = c->mem[(ptr + i) & ADDR_MASK];
            if (w == 0) break;
            path[n++] = (char)(w & 0xff);
        }
        path[n] = 0;
        /* strip drive prefix ("A:") and leading separators */
        char *p = path;
        if (n >= 2 && p[1] == ':') p += 2;
        while (*p == '\\' || *p == '/') p++;
        /* walk path components from the root directory (cluster 2) */
        uint32_t cur = 2, fsize = 0;
        int isdir = 1, found = 1;
        while (*p) {
            char comp[300]; int ci = 0;
            while (*p && *p != '\\' && *p != '/') { if (ci < 299) comp[ci++] = *p; p++; }
            comp[ci] = 0;
            while (*p == '\\' || *p == '/') p++;
            if (ci == 0) continue;
            uint32_t ncl, nsz; int nd;
            if (!fat_find_in_dir(c, cur, comp, &ncl, &nsz, &nd)) { found = 0; break; }
            cur = ncl; fsize = nsz; isdir = nd;
        }
        (void)isdir;
        if (found) {
            c->r[R1] = (uint16_t)(fsize & 0xffff);
            c->r[R2] = (uint16_t)((fsize >> 16) & 0xffff);
        } else {
            c->r[R1] = 0xffff;
            c->r[R2] = 0xffff;
        }
        if (hle_fs_debug)
            fprintf(stderr, "[HLE find-file] ptr=%06x name='%s' -> %s size=%u (r1=%04x r2=%04x)\n",
                    ptr, path, found ? "FOUND" : "NOTFOUND", found ? fsize : 0xffffffffu,
                    c->r[R1], c->r[R2]);
        /* retf: pop SR then PC */
        c->r[SR] = c->mem[(uint16_t)(sp + 1)];
        c->r[PC] = c->mem[(uint16_t)(sp + 2)];
        c->r[SP] = (uint16_t)(sp + 2);
        return 1;
    }
    if (id == 4) {
        /* open(far_ptr, mode): resolve the file, allocate a VFS handle carrying its
         * start cluster/size/pos; return handle in r1 (0xffff = fail). */
        uint16_t plo = c->mem[(uint16_t)(sp + 3)];
        uint16_t phi = c->mem[(uint16_t)(sp + 4)];
        uint32_t ptr = (((uint32_t)phi << 16) | plo) & ADDR_MASK;
        uint32_t cl, sz; uint16_t handle = 0xffff;
        if (vfs_resolve(c, ptr, &cl, &sz)) {
            int h = -1;
            for (int k = 0; k < 16; k++) if (!c->vfh_used[k]) { h = k; break; }
            if (h >= 0) {
                c->vfh_used[h] = 1; c->vfh_cluster[h] = cl; c->vfh_size[h] = sz;
                c->vfh_pos[h] = 0; handle = (uint16_t)(0x4000 | h);
            }
        }
        if (hle_fs_debug)
            fprintf(stderr, "[HLE open] ptr=%06x -> handle=%04x\n", ptr, handle);
        c->r[R1] = handle;
        c->r[SR] = c->mem[(uint16_t)(sp + 1)];
        c->r[PC] = c->mem[(uint16_t)(sp + 2)];
        c->r[SP] = (uint16_t)(sp + 2);
        return 1;
    }
    if (id == 5) {
        /* read-byte(handle) [SP+3], fgetc-style: return next file byte in r1,
         * 0xffff at EOF. Follows the cluster chain from the handle. */
        uint16_t handle = c->mem[(uint16_t)(sp + 3)];
        uint16_t ret = 0xffff;
        if ((handle & 0xf000) == 0x4000) {
            int h = handle & 0xf;
            if (c->vfh_used[h] && c->vfh_pos[h] < c->vfh_size[h]) {
                uint32_t pos = c->vfh_pos[h];
                uint32_t ci = pos / FAT_BYTES_PER_SEC, off = pos % FAT_BYTES_PER_SEC;
                uint32_t cl = c->vfh_cluster[h];
                for (uint32_t k = 0; k < ci && cl >= 2 && cl < 0x0ffffff8u; k++)
                    cl = fat_next_cluster(c, cl);
                uint32_t nb = FAT_DATA_START_BYTE + (cl - 2) * FAT_BYTES_PER_SEC + off;
                ret = (c->nand && nb < c->nand_size) ? c->nand[nb] : 0;
                c->vfh_pos[h]++;
            }
        }
        c->r[R1] = ret;
        c->r[SR] = c->mem[(uint16_t)(sp + 1)];
        c->r[PC] = c->mem[(uint16_t)(sp + 2)];
        c->r[SP] = (uint16_t)(sp + 2);
        return 1;
    }
    return 0;
}

void cpu_set_hle_debug(Cpu *c, int on) { (void)c; hle_fs_debug = on ? 1 : 0; }

/* debug: read one 512-byte sector through the real controller path (cmd0/type7),
 * mimicking the byte-source DMA (two 0x7854 reads per word). Fills out16[256]. */
void cpu_dbg_readsector(Cpu *c, uint32_t sector, uint16_t *out16) {
    c->nand_cmd = 0x00; c->nand_7856 = 0x0027;
    c->nand_addr_low = sector & 0xffff; c->nand_addr_high = (sector >> 16) & 0xffff;
    nand_recalc(c);
    for (int i = 0; i < 256; i++) {
        uint16_t lo = read16(c, 0x7854) & 0xff;
        uint16_t hi = read16(c, 0x7854) & 0xff;
        out16[i] = (uint16_t)(lo | (hi << 8));
    }
}

uint64_t cpu_run(Cpu *c, uint64_t max_steps);   /* fwd */
uint64_t cpu_run_until(Cpu *c, uint32_t stop_pc, uint64_t max_steps) {
    c->run_until_pc = stop_pc;
    uint64_t n = cpu_run(c, max_steps);
    c->run_until_pc = 0;
    return n;
}

/* run up to max_steps instructions; stop early if halted. returns steps run. */
uint64_t cpu_run(Cpu *c, uint64_t max_steps) {
    uint64_t i;
    for (i = 0; i < max_steps; i++) {
        if (c->halted) break;

        /* timer heartbeat: every timer_period instructions, raise the timer IRQ */
        if (c->timer_period && --c->timer_count == 0) {
            c->timer_count = c->timer_period;
            c->irq_pending |= (uint8_t)(1u << c->timer_line);
            if (c->timer_status_off != 0xffffffffu) {
                c->mmio_last[c->timer_status_off] |= c->timer_status_bits;
                c->mmio_has[c->timer_status_off] = 1;
            }
        }
        /* service a pending IRQ (if enabled and not already in one) */
        if (c->irq_en && c->irq_pending && !c->in_irq) {
            int line = 0;
            while (line < 8 && !(c->irq_pending & (1u << line))) line++;
            if (line < 8) {
                c->irq_pending &= (uint8_t)~(1u << line);
                push(c, c->r[PC], SP);
                push(c, c->r[SR], SP);
                c->r[PC] = (uint16_t)((c->irq_vecbase + 2 * line) & 0xffff);
                c->r[SR] &= 0xffc0;          /* CS = 0 (trampoline lives in SRAM) */
                c->in_irq = 1;
                c->irq_taken++;
            }
        }

        uint32_t prev = lpc(c);
        if (c->run_until_pc && prev == c->run_until_pc) break;
        if (c->nwatch) {
            int wk;
            for (wk = 0; wk < c->nwatch; wk++)
                if (prev == c->watch[wk]) { c->watch_hits[wk]++; break; }
        }
        /* HLE hooks: run a C handler instead of the function body */
        if (c->n_hle) {
            int hk, handled = 0;
            for (hk = 0; hk < c->n_hle; hk++) {
                if (prev == c->hle_pc[hk]) {
                    c->hle_calls[hk]++;
                    if (hle_dispatch(c, c->hle_id[hk])) { c->insns++; handled = 1; }
                    break;
                }
            }
            if (handled) continue;
        }
        uint16_t op = read16(c, prev);
        add_lpc(c, 1);
        c->insns++;
        execute(c, op);
        if (c->cs_trap && ((uint32_t)(c->r[SR] & 0x3f) > c->cs_trap)) {
            c->trapped = 1; c->trap_from = prev; c->trap_to = lpc(c);
            i++; break;
        }
    }
    return i;
}

/* single step (for lockstep validation vs the Python core) */
void cpu_step(Cpu *c) {
    if (c->halted) return;
    uint16_t op = read16(c, lpc(c));
    add_lpc(c, 1);
    c->insns++;
    execute(c, op);
}
