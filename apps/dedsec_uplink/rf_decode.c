/* Protocol recognition for recorded OOK bursts.  See rf_decode.h.
 *
 * Every protocol is a small sequential decoder over the (level, duration) timings: it looks
 * for the protocol's sync at one index and reads one frame of PWM bit pairs.  The burst is
 * scanned once per protocol; the protocol with the most frames wins.  Timing constants are
 * the protocol facts (base pulse, sync length, bit polarity); the code is our own.
 *
 * Levels: a timing's level is the level of the pulse that just ended, as the Sub-GHz capture
 * ISR reports it (true = carrier on).  Bits are stored first-received = most significant. */

#include "rf_decode.h"
#include "rf_capture.h"

#include <stdio.h>
#include <string.h>

typedef struct {
    const uint32_t* t;
    uint32_t n;
} RfSeq;

typedef struct {
    uint64_t key; /* first 64 bits */
    uint8_t bits; /* bits read */
    uint8_t extra; /* bits beyond 64 (KeeLoq status bits) */
    uint32_t te_sum; /* sum of the short elements seen */
    uint32_t te_n;
} RfFrame;

/* One PWM bit = two timings.  `first_high`: pairs are (high, low), else (low, high).
 * `short_first_bit`: the bit value when the first element is the short one.
 * `symmetric`: Starline-style bits, both elements short (0) or both long (1).
 * The frame ends when the element named by `end_first` (first/second of a pair) is at
 * least `end_min` long; `last_pair_is_bit` then classifies the first element of that pair
 * as a bit on its own (Linear's guard follows the last bit). */
typedef struct {
    uint16_t te_short, te_long;
    uint16_t tol_short, tol_long;
    uint32_t end_min;
    bool first_high;
    bool short_first_bit;
    bool symmetric;
    bool end_first;
    bool last_pair_is_bit;
} PwmSpec;

static inline bool lvl(const RfSeq* s, uint32_t i) {
    return rf_timing_level(s->t[i]);
}

static inline uint32_t dur(const RfSeq* s, uint32_t i) {
    return rf_timing_duration(s->t[i]);
}

/* |d - nominal| < tolerance */
static inline bool near_us(uint32_t d, uint32_t nominal, uint32_t tolerance) {
    return d + tolerance > nominal && d < nominal + tolerance;
}

static void frame_reset(RfFrame* f) {
    memset(f, 0, sizeof(*f));
}

static void frame_add_bit(RfFrame* f, bool bit) {
    if(f->bits < 64)
        f->key = (f->key << 1) | (bit ? 1U : 0U);
    else if(f->bits < 72)
        f->extra = (uint8_t)((f->extra << 1) | (bit ? 1U : 0U));
    f->bits++;
}

/* Reads bit pairs from index i.  Returns true on a clean frame end (the end element seen)
 * and sets *next to the index of the ending pair's first element (a sync may start
 * there); false on a mismatching pair or at the end of the timings. */
static bool read_pairs(const RfSeq* s, uint32_t i, const PwmSpec* p, RfFrame* f, uint32_t* next) {
    while(i + 1 < s->n) {
        uint32_t a = dur(s, i), b = dur(s, i + 1);
        if(lvl(s, i) != p->first_high || lvl(s, i + 1) == p->first_high) return false;
        if(p->end_first && a >= p->end_min) {
            *next = i;
            return true;
        }
        if(!p->end_first && b >= p->end_min) {
            if(p->last_pair_is_bit) {
                if(near_us(a, p->te_short, p->tol_short))
                    frame_add_bit(f, p->short_first_bit);
                else if(near_us(a, p->te_long, p->tol_long))
                    frame_add_bit(f, !p->short_first_bit);
                else
                    return false;
                *next = i + 1;
            } else {
                *next = i;
            }
            return true;
        }
        bool a_short = near_us(a, p->te_short, p->tol_short);
        bool a_long = near_us(a, p->te_long, p->tol_long);
        bool b_short = near_us(b, p->te_short, p->tol_short);
        bool b_long = near_us(b, p->te_long, p->tol_long);
        if(p->symmetric) {
            if(a_short && b_short)
                frame_add_bit(f, false);
            else if(a_long && b_long)
                frame_add_bit(f, true);
            else
                return false;
        } else if(a_short && b_long) {
            frame_add_bit(f, p->short_first_bit);
        } else if(a_long && b_short) {
            frame_add_bit(f, !p->short_first_bit);
        } else {
            return false;
        }
        f->te_sum += a_short ? a : (p->symmetric ? a / 2 : b);
        f->te_n++;
        if(f->bits > 80) return false;
        i += 2;
    }
    return false;
}

/* ------------------------------------------------------------------ protocols */

/* Princeton / PT2262 / EV1527: sync = low ~31..36 te, 24 pairs (high, low), short high +
 * long low = 0, long high + short low = 1.  The chips run at 150..650 us depending on their
 * resistor, so the base pulse is taken from the frame itself: within a frame every short
 * element stays within 40 % of the first one and every long one is 2.2..4 times its short. */
static bool dec_princeton(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(lvl(s, i) || !near_us(dur(s, i), 390 * 36, 300 * 36)) return false;
    frame_reset(f);
    uint32_t te = 0;
    for(i++; i + 1 < s->n; i += 2) {
        if(!lvl(s, i) || lvl(s, i + 1)) return false;
        uint32_t a = dur(s, i), b = dur(s, i + 1);
        if(b >= (te ? te * 6 : 2340)) {
            *next = i;
            return f->bits == 24;
        }
        uint32_t sh = a < b ? a : b, lg = a < b ? b : a;
        if(sh < 90 || sh > 690 || lg * 10 < sh * 22 || lg > sh * 4 + 200) return false;
        if(!te)
            te = sh;
        else if(sh * 10 < te * 6 || sh * 10 > te * 14)
            return false;
        frame_add_bit(f, a > b);
        f->te_sum += sh;
        f->te_n++;
        if(f->bits > 24) return false;
    }
    return false;
}

/* CAME (TOP-432 and friends, also Holtek HT12E): sync = low 36 te, start bit high te,
 * pairs (low, high): short low + long high = 0, long low + short high = 1, 1:2, te 320. */
static bool dec_came(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 1 >= s->n || lvl(s, i) || !near_us(dur(s, i), 320 * 56, 150 * 63)) return false;
    if(!lvl(s, i + 1) || !near_us(dur(s, i + 1), 320, 150)) return false;
    static const PwmSpec p = {320, 640, 150, 150, 320 * 4, false, false, false, true, false};
    frame_reset(f);
    if(!read_pairs(s, i + 2, &p, f, next)) return false;
    return f->bits == 12 || f->bits == 18 || f->bits == 24 || f->bits == 25;
}

/* Nice FLO: the CAME grammar with te 700. */
static bool dec_nice_flo(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 1 >= s->n || lvl(s, i) || !near_us(dur(s, i), 700 * 36, 250 * 29)) return false;
    if(!lvl(s, i + 1) || !near_us(dur(s, i + 1), 700, 250)) return false;
    static const PwmSpec p = {700, 1400, 250, 250, 700 * 4, false, false, false, true, false};
    frame_reset(f);
    return read_pairs(s, i + 2, &p, f, next) && f->bits >= 12 && f->bits <= 24;
}

/* Holtek 40-bit: sync low 36 te (te 430), start bit high te, CAME-style pairs, frame
 * starts with the 0x5 header nibble. */
static bool dec_holtek(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 1 >= s->n || lvl(s, i) || !near_us(dur(s, i), 430 * 36, 100 * 36)) return false;
    if(!lvl(s, i + 1) || !near_us(dur(s, i + 1), 430, 100)) return false;
    static const PwmSpec p = {430, 870, 100, 200, 430 * 10 + 100, false, false, false, true, false};
    frame_reset(f);
    if(!read_pairs(s, i + 2, &p, f, next) || f->bits != 40) return false;
    return (f->key >> 36) == 0x5;
}

/* GateTX: sync low 47 te (te 350), start bit high te_long, CAME-style pairs, 24 bits. */
static bool dec_gate_tx(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 1 >= s->n || lvl(s, i) || !near_us(dur(s, i), 350 * 47, 100 * 47)) return false;
    if(!lvl(s, i + 1) || !near_us(dur(s, i + 1), 700, 300)) return false;
    static const PwmSpec p = {350, 700, 100, 300, 350 * 10 + 100, false, false, false, true, false};
    frame_reset(f);
    return read_pairs(s, i + 2, &p, f, next) && f->bits == 24;
}

/* KeeLoq (HCS200/300/301...): preamble of >= 3 te/te pulses, header low 10 te, 66 pairs
 * (high, low): short high + long low = 1, long high + short low = 0, te 400. */
static bool dec_keeloq(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    uint32_t count = 0;
    while(i + 1 < s->n && lvl(s, i) && near_us(dur(s, i), 400, 180) && !lvl(s, i + 1) &&
          near_us(dur(s, i + 1), 400, 180)) {
        count++;
        i += 2;
    }
    if(count < 3 || i + 1 >= s->n) return false;
    /* the header: the last preamble pulse's low is 10 te instead of te */
    if(!(lvl(s, i) && near_us(dur(s, i), 400, 180) && !lvl(s, i + 1) &&
         near_us(dur(s, i + 1), 400 * 10, 180 * 10)))
        return false;
    static const PwmSpec p = {400, 800, 180, 360, 400 * 2 + 180, true, true, false, false, false};
    frame_reset(f);
    return read_pairs(s, i + 2, &p, f, next) && f->bits >= 64 && f->bits <= 67;
}

/* Starline: preamble of >= 5 1000/1000 us pulses, then symmetric bits: short+short = 0,
 * long+long = 1 (te 250/500), 64 bits, frame ends with a high >= te_long + delta. */
static bool dec_starline(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    uint32_t count = 0;
    while(i + 1 < s->n && lvl(s, i) && near_us(dur(s, i), 1000, 240) && !lvl(s, i + 1) &&
          near_us(dur(s, i + 1), 1000, 240)) {
        count++;
        i += 2;
    }
    if(count < 5 || i >= s->n || !lvl(s, i)) return false;
    static const PwmSpec p = {250, 500, 120, 120, 500 + 120, true, false, true, true, false};
    frame_reset(f);
    return read_pairs(s, i, &p, f, next) && f->bits >= 64 && f->bits <= 66;
}

/* Nice FloR-S: sync low 38 te (te 500), high 3 te, low 3 te, 52 pairs (high, low):
 * short high + long low = 0, long high + short low = 1, stop bit high 3 te. */
static bool dec_nice_flor_s(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 2 >= s->n || lvl(s, i) || !near_us(dur(s, i), 500 * 38, 300 * 38)) return false;
    if(!lvl(s, i + 1) || !near_us(dur(s, i + 1), 1500, 900)) return false;
    if(lvl(s, i + 2) || !near_us(dur(s, i + 2), 1500, 900)) return false;
    static const PwmSpec p = {500, 1000, 300, 300, 1500 - 300, true, false, false, true, false};
    frame_reset(f);
    return read_pairs(s, i + 3, &p, f, next) && (f->bits == 52 || f->bits == 72);
}

/* Linear (315 MHz garage remotes): sync low 42 te (te 500), 10 pairs (high, low) with a
 * 1:3 ratio, short high = 0; the 10th bit's low is the next guard. */
static bool dec_linear(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(lvl(s, i) || !near_us(dur(s, i), 500 * 42, 350 * 15)) return false;
    static const PwmSpec p = {500, 1500, 350, 350, 500 * 5, true, false, false, false, true};
    frame_reset(f);
    return read_pairs(s, i + 1, &p, f, next) && f->bits == 10;
}

/* Hormann HSM (868 MHz): sync high 24 te (te 500), low te, 44 pairs (high, low): short
 * high + long low = 0, long high + short low = 1, frame ends with a high >= 5 te. */
static bool dec_hormann(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 1 >= s->n || !lvl(s, i) || !near_us(dur(s, i), 500 * 24, 200 * 24)) return false;
    if(lvl(s, i + 1) || !near_us(dur(s, i + 1), 500, 200)) return false;
    static const PwmSpec p = {500, 1000, 200, 200, 500 * 5, true, false, false, true, false};
    frame_reset(f);
    return read_pairs(s, i + 2, &p, f, next) && f->bits >= 44 && f->bits <= 48;
}

/* FAAC SLH: preamble high 2 te_long, low 2 te_long, 64 pairs (high, low): short high +
 * long low = 0, long high + short low = 1 (te 255/595), ends with a high >= 3 te. */
static bool dec_faac_slh(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 1 >= s->n || !lvl(s, i) || !near_us(dur(s, i), 1190, 300)) return false;
    if(lvl(s, i + 1) || !near_us(dur(s, i + 1), 1190, 300)) return false;
    static const PwmSpec p = {255, 595, 100, 100, 255 * 3 + 100, true, false, false, true, false};
    frame_reset(f);
    return read_pairs(s, i + 2, &p, f, next) && f->bits == 64;
}

/* Nexus-TH / Rubicson weather sensors: high 500 us then a gap of 1000 (0) or 2000 (1) us,
 * 36 bits, sync = a pulse followed by a ~4 ms gap. */
static bool dec_nexus(const RfSeq* s, uint32_t i, RfFrame* f, uint32_t* next) {
    if(i + 1 >= s->n || !lvl(s, i) || !near_us(dur(s, i), 500, 250)) return false;
    if(lvl(s, i + 1) || !near_us(dur(s, i + 1), 4000, 1200)) return false;
    frame_reset(f);
    i += 2;
    while(i + 1 < s->n) {
        uint32_t a = dur(s, i), b = dur(s, i + 1);
        if(!lvl(s, i) || lvl(s, i + 1) || !near_us(a, 500, 250)) return false;
        if(b >= 3000) {
            *next = i;
            return f->bits == 36;
        }
        if(near_us(b, 1000, 350))
            frame_add_bit(f, false);
        else if(near_us(b, 2000, 500))
            frame_add_bit(f, true);
        else
            return false;
        f->te_sum += a;
        f->te_n++;
        if(f->bits > 36) return false;
        i += 2;
    }
    return false;
}

typedef bool (*RfDecoder)(const RfSeq*, uint32_t, RfFrame*, uint32_t*);

typedef struct {
    uint8_t id;
    const char* name;
    RfDecoder decode;
    bool rolling;
} RfProtoDesc;

/* In order of preference when two grammars fit the same burst equally well. */
static const RfProtoDesc rf_protocols[] = {
    {RfProtoKeeloq, "KeeLoq", dec_keeloq, true},
    {RfProtoStarline, "Starline", dec_starline, true},
    {RfProtoNiceFlorS, "Nice FloR-S", dec_nice_flor_s, true},
    {RfProtoFaacSlh, "FAAC SLH", dec_faac_slh, true},
    {RfProtoNexus, "Nexus-TH", dec_nexus, false},
    {RfProtoPrinceton, "Princeton", dec_princeton, false},
    {RfProtoHoltek, "Holtek", dec_holtek, false},
    {RfProtoGateTx, "GateTX", dec_gate_tx, false},
    {RfProtoNiceFlo, "Nice FLO", dec_nice_flo, false},
    {RfProtoCame, "CAME", dec_came, false},
    {RfProtoLinear, "Linear", dec_linear, false},
    {RfProtoHormann, "Hormann", dec_hormann, false},
};

/* ------------------------------------------------------------------ descriptions */

static void princeton_info(RfDecode* d) {
    uint32_t key = (uint32_t)(d->key & 0xFFFFFF);
    uint8_t low = (uint8_t)(key & 0xFF);
    /* some encoders use an 8-bit button field with these fixed values */
    if(low == 0x30 || low == 0xC0 || low == 0x03 || low == 0x0C) {
        snprintf(
            d->info, sizeof(d->info), "sn %04lX btn %02X", (unsigned long)(key >> 8), low);
    } else {
        snprintf(
            d->info, sizeof(d->info), "sn %05lX btn %lX", (unsigned long)(key >> 4),
            (unsigned long)(key & 0xF));
    }
}

static void keeloq_info(RfDecode* d) {
    /* bits arrive LSB first: hop = bits 0..31, serial = 32..59, button = 60..63 */
    uint64_t rev = 0;
    for(int i = 0; i < 64; i++) rev = (rev << 1) | ((d->key >> i) & 1U);
    uint32_t serial = (uint32_t)((rev >> 32) & 0x0FFFFFFF);
    uint32_t btn = (uint32_t)(rev >> 60);
    snprintf(d->info, sizeof(d->info), "sn %07lX btn %lX", (unsigned long)serial, (unsigned long)btn);
}

static void starline_info(RfDecode* d) {
    uint32_t fix = (uint32_t)(d->key >> 32);
    snprintf(
        d->info, sizeof(d->info), "sn %06lX btn %02lX", (unsigned long)(fix & 0xFFFFFF),
        (unsigned long)(fix >> 24));
}

static void nexus_info(RfDecode* d) {
    uint64_t k = d->key;
    uint32_t id = (uint32_t)((k >> 28) & 0xFF);
    bool battery = ((k >> 27) & 1U) != 0;
    uint32_t channel = (uint32_t)((k >> 24) & 0x3) + 1;
    int32_t temp = (int32_t)((k >> 12) & 0xFFF);
    if(temp & 0x800) temp -= 0x1000;
    uint32_t humidity = (uint32_t)(k & 0xFF);
    char sign = temp < 0 ? '-' : '+';
    if(temp < 0) temp = -temp;
    snprintf(
        d->info, sizeof(d->info), "%c%ld.%ldC %lu%% ch%lu id%02lX%s", sign, (long)(temp / 10),
        (long)(temp % 10), (unsigned long)humidity, (unsigned long)channel, (unsigned long)id,
        battery ? "" : " LOW BAT");
}

static void key_info(RfDecode* d) {
    if(d->bits > 32)
        snprintf(
            d->info, sizeof(d->info), "key %08lX%08lX", (unsigned long)(d->key >> 32),
            (unsigned long)(d->key & 0xFFFFFFFFUL));
    else
        snprintf(d->info, sizeof(d->info), "key %0*lX", (d->bits + 3) / 4, (unsigned long)d->key);
}

/* The generic description: base pulse, symbol count, frame repeats. */
static void describe_ook(const RfSeq* s, RfDecode* d) {
    enum { BUCKETS = 64, BUCKET_US = 50 };
    uint16_t hist[BUCKETS];
    memset(hist, 0, sizeof(hist));
    uint32_t total = 0;
    for(uint32_t i = 0; i < s->n; i++) {
        uint32_t us = dur(s, i);
        if(us < BUCKETS * BUCKET_US) {
            hist[us / BUCKET_US]++;
            total++;
        }
    }
    /* te: the shortest width that is common (at least 12 % of the short timings, >= 3) */
    uint32_t te = 0;
    for(uint32_t b = 1; b < BUCKETS && !te; b++) {
        uint32_t c = hist[b] + (b + 1 < BUCKETS ? hist[b + 1] : 0);
        if(c >= 3 && c * 100 >= total * 12) te = b * BUCKET_US + BUCKET_US; /* bucket pair centre */
    }
    if(!te) te = 500;
    uint64_t sum = 0;
    uint32_t cnt = 0;
    for(uint32_t i = 0; i < s->n; i++) {
        uint32_t us = dur(s, i);
        if(us * 4 >= te * 3 && us * 4 <= te * 5) {
            sum += us;
            cnt++;
        }
    }
    if(cnt) te = (uint32_t)(sum / cnt);
    d->te_us = te;
    /* frames: runs of timings separated by lows of 6 te (or 2.5 ms) and more */
    uint32_t gap = te * 6 > 2500 ? te * 6 : 2500;
    uint32_t symbols = 0, frames = 0, identical = 0;
    uint32_t hash = 2166136261U, first_hash = 0, first_len = 0, cur_len = 0;
    for(uint32_t i = 0; i < s->n; i++) {
        uint32_t us = dur(s, i);
        bool separator = !lvl(s, i) && us >= gap;
        if(separator || i + 1 == s->n) {
            if(!separator) {
                cur_len++;
                uint32_t q = (us * 2 + te) / (te * 2);
                hash = (hash ^ q) * 16777619U;
            }
            if(cur_len >= 4) {
                frames++;
                if(frames == 1) {
                    first_hash = hash;
                    first_len = cur_len;
                } else if(hash == first_hash && cur_len == first_len) {
                    identical++;
                }
            }
            hash = 2166136261U;
            cur_len = 0;
            continue;
        }
        cur_len++;
        symbols++;
        uint32_t q = (us * 2 + te) / (te * 2); /* width in te, rounded */
        hash = (hash ^ q) * 16777619U;
    }
    d->protocol = RfProtoOok;
    snprintf(d->name, sizeof(d->name), "OOK");
    d->bits = (uint8_t)(symbols / 2 > 255 ? 255 : symbols / 2);
    d->frames = (uint8_t)(frames > 255 ? 255 : frames);
    d->identical = (uint8_t)(identical > 255 ? 255 : identical);
    if(frames >= 2)
        snprintf(
            d->info, sizeof(d->info), "te %luus %usym x%u %s", (unsigned long)te, d->bits,
            d->frames, identical + 1 >= frames ? "same" : "differ");
    else
        snprintf(d->info, sizeof(d->info), "te %luus %usym", (unsigned long)te, d->bits);
    d->confidence = 20;
}

/* ------------------------------------------------------------------ public */

void rf_decode(const uint32_t* timings, uint32_t count, RfDecode* out) {
    memset(out, 0, sizeof(*out));
    RfSeq s = {timings, count};
    if(!timings || count < 8) {
        out->protocol = RfProtoNone;
        snprintf(out->name, sizeof(out->name), "carrier");
        snprintf(out->info, sizeof(out->info), "no OOK data");
        return;
    }
    const RfProtoDesc* best = NULL;
    RfFrame best_first;
    uint32_t best_frames = 0, best_identical = 0;
    frame_reset(&best_first);
    for(size_t p = 0; p < sizeof(rf_protocols) / sizeof(rf_protocols[0]); p++) {
        const RfProtoDesc* desc = &rf_protocols[p];
        RfFrame first, f;
        uint32_t frames = 0, identical = 0, next = 0;
        frame_reset(&first);
        for(uint32_t i = 0; i < count;) {
            if(desc->decode(&s, i, &f, &next) && next > i) {
                frames++;
                if(frames == 1) {
                    first = f;
                } else {
                    if(f.key == first.key && f.bits == first.bits) identical++;
                    first.te_sum += f.te_sum;
                    first.te_n += f.te_n;
                }
                i = next;
            } else {
                i++;
            }
        }
        if(frames > best_frames) {
            best = desc;
            best_first = first;
            best_frames = frames;
            best_identical = identical;
        }
    }
    if(!best) {
        describe_ook(&s, out);
        return;
    }
    out->protocol = best->id;
    snprintf(out->name, sizeof(out->name), "%s", best->name);
    out->bits = best_first.bits;
    out->key = best_first.key;
    out->frames = (uint8_t)(best_frames > 255 ? 255 : best_frames);
    out->identical = (uint8_t)(best_identical > 255 ? 255 : best_identical);
    out->te_us = best_first.te_n ? best_first.te_sum / best_first.te_n : 0;
    out->rolling = best->rolling;
    out->confidence = (uint8_t)(best_frames >= 2 ? 90 : (best_first.bits >= 40 ? 75 : 60));
    switch(best->id) {
    case RfProtoPrinceton:
        princeton_info(out);
        break;
    case RfProtoKeeloq:
        keeloq_info(out);
        break;
    case RfProtoStarline:
        starline_info(out);
        break;
    case RfProtoNexus:
        nexus_info(out);
        break;
    case RfProtoNiceFlorS:
        snprintf(out->info, sizeof(out->info), "encrypted, %u bits", out->bits);
        break;
    case RfProtoFaacSlh:
        snprintf(
            out->info, sizeof(out->info), "sn %07lX",
            (unsigned long)((out->key >> 32) & 0x0FFFFFFF));
        break;
    default:
        key_info(out);
        break;
    }
}

uint64_t rf_decode_identity(const RfDecode* d) {
    if(!d) return 0;
    switch(d->protocol) {
    case RfProtoKeeloq: {
        uint64_t rev = 0;
        for(int i = 0; i < 64; i++) rev = (rev << 1) | ((d->key >> i) & 1U);
        return (rev >> 32) & 0x0FFFFFFF;
    }
    case RfProtoStarline:
        return (d->key >> 32) & 0xFFFFFF;
    case RfProtoFaacSlh:
        return (d->key >> 32) & 0x0FFFFFFF;
    case RfProtoNexus:
        return (d->key >> 24) & 0xFF3; /* id and channel */
    case RfProtoPrinceton:
    case RfProtoCame:
    case RfProtoNiceFlo:
    case RfProtoLinear:
    case RfProtoHormann:
    case RfProtoGateTx:
    case RfProtoHoltek:
        return d->key ? d->key : 1;
    default:
        return 0;
    }
}

void rf_decode_label(const RfDecode* d, char* out, size_t size) {
    if(!out || !size) return;
    if(!d || !d->name[0]) {
        out[0] = '\0';
        return;
    }
    if(d->protocol == RfProtoOok)
        snprintf(out, size, "OOK %usym", d->bits);
    else if(d->protocol == RfProtoNone || d->protocol == RfProtoNexus)
        snprintf(out, size, "%s", d->name);
    else
        snprintf(out, size, "%s %ub", d->name, d->bits);
}
