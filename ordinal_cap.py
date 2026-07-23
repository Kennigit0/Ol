import asyncio, time, re, random, os, unicodedata, json, subprocess, io
import numpy as np
import cv2
from telethon import TelegramClient, events
from telethon.tl.types import ReplyInlineMarkup, MessageEntityUnderline

API_ID   = 39455771
API_HASH = "0150c2e270dfcf0f3cfdfdce8f0a7a49"
PHONE    = "+917990952611"
BOT      = "OrdinalLegacyBot"

CAPTURE_LIST = [
    "aurelite", "violet coil", "moon nyc", "sunphiny",
    "gelarxia", "drion", "gemwave", "apharon", "frost", "Xceynerite"
]

# Any pet sighting containing one of these emoji gets captured
# UNCONDITIONALLY (any rarity), regardless of name (e.g. "You saw Apharon🔥!")
CAPTURE_EMOJIS = {"🔥"}

# Pet sightings with these emoji are ONLY captured if the pet is also
# Epic+ (i.e. its name isn't on the known Common/Rare list). All Common/Rare
# pets of this type are already caught, so skip re-capturing them.
# (e.g. "You saw Froghare🏝!" — Froghare is Rare -> skip; unrecognized name -> capture)
EPIC_ONLY_EMOJIS = {"🏝"}

# Every documented Common and Rare pet name (from Ordinal Legacy Pet Guide 2.0).
# Any sighting whose name ISN'T in this list is presumed Epic or higher
# (including undocumented Exotic/Super Rare pets with no known name yet)
# and gets captured too — no need to know the name in advance.
KNOWN_COMMON_RARE_NAMES = {
    # Common
    "dionysus", "ugla", "bastet", "drake", "asclepius", "sciurus", "felpis", "cinco", "nightingle",
    "fillow", "breary", "owlhog", "shadoweyes", "feline paw", "embertail", "capnolithyl", "dragoth", "larham",
    "saphira", "falkor", "breezy", "glassterint", "pikura", "crawstar", "jelloww", "bobo", "zevrat",
    # Rare
    "sagara", "scriper", "froghare", "honeydripper", "mymphy",
    "oculus hex", "rosy whisker", "dilong", "hyxallian", "durafin",
}

KNOWN_MOVES = {"attack", "small attack", "ultimate", "shield", "small"}

MAX_PEARL_PRICE  = 250
MAX_TICKET_PRICE = 450

client = TelegramClient("ordinalepic_session", API_ID, API_HASH)

last_action_time = 0
monster_paused    = False
bot_running       = True   # /stop sets this False, bot ignores everything

monster_group_msg     = None   # the original photo message (for re-clicking)
monster_candidates    = []     # remaining untried guesses, best-first
monster_tried         = set()  # numbers already tried for current puzzle
monster_current_hash  = None   # {hash, phash} of the image currently being solved
monster_last_guess    = None   # count value most recently clicked, for registration on success
monster_pending_image = None   # raw bytes of the most recent monster-group screenshot, for /count

wizard_active     = False
wizard_key        = {}
wizard_last_done  = None
wizard_last_click = 0

# ══════════════════════════════════════════════════════════════
#  BASIC HELPERS
# ══════════════════════════════════════════════════════════════

def get_btns(m):
    out = []
    if m and m.reply_markup and isinstance(m.reply_markup, ReplyInlineMarkup):
        for row in m.reply_markup.rows:
            for b in row.buttons:
                out.append(b.text)
    return out

def strip_accents(s):
    """Strip accent/diacritic marks (e.g. 'Ệngage' -> 'Engage') — the game
    sometimes obfuscates button labels this way."""
    return "".join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))

def has_btn(bl, keyword):
    return any(keyword.lower() in strip_accents(b).lower() for b in bl)

def get_btn_idx(bl, keyword):
    for i, b in enumerate(bl):
        if keyword.lower() in strip_accents(b).lower():
            return i
    return 0

def is_monster_dead(m):
    """Empty-button confirmation messages (trade done, item found, spell broken,
    etc.) that mean it's safe to keep exploring."""
    if get_btns(m):
        return False
    text = (m.text or "").lower()
    return any(k in text for k in [
        "gelarxia", "also found", "traded", "rejected",
        "you broke free from the spell", "stole", "continue your journey",
    ])

def extract_pet_name(raw_text):
    """Pull the pet's name out of a sighting message like 'You saw X!' or
    'You tracked down X!' — used to check it against the known Common/Rare
    list, since anything NOT on that list is presumed Epic or higher."""
    m = re.search(r'(?:saw|tracked down)\s+([A-Za-z][A-Za-z ]*?)(?:[^\w\s]|$)', raw_text, re.IGNORECASE)
    return m.group(1).strip() if m else None

def should_capture(m):
    raw = m.text or ""
    text = raw.lower()
    for name in CAPTURE_LIST:
        if re.search(r'\b' + re.escape(name.lower()) + r'\b', text):
            return True
    if any(e in raw for e in CAPTURE_EMOJIS):
        return True
    # Unrecognized name (not documented as Common or Rare) — presumed
    # Epic/Exotic/Super Rare, capture it even without knowing it by name.
    name = extract_pet_name(raw)
    if name and name.lower() not in KNOWN_COMMON_RARE_NAMES:
        return True
    return False

def get_matched_capture_name(m):
    """Which creature matched — by CAPTURE_LIST name, by capture-emoji, or
    by having a name that isn't on the known Common/Rare list."""
    raw = m.text or ""
    text = raw.lower()
    for name in CAPTURE_LIST:
        if re.search(r'\b' + re.escape(name.lower()) + r'\b', text):
            return name
    for e in CAPTURE_EMOJIS:
        idx = raw.find(e)
        if idx != -1:
            j = idx
            while j > 0 and raw[j-1].isalnum():
                j -= 1
            name = raw[j:idx].strip()
            if name:
                return name
            return f"pet ({e})"
    name = extract_pet_name(raw)
    if name and name.lower() not in KNOWN_COMMON_RARE_NAMES:
        return f"{name} (unrecognized — likely Epic+)"
    return None

def is_buy_reject(btns):
    bl = [strip_accents(b).lower() for b in btns]
    return any("buy" in b for b in bl) or any("reject" in b for b in bl)

def check_trader_offer(text):
    if "enchant" in text:
        log("Enchant offer - always buying!")
        return "buy"

    pearl_match  = re.search(r'(\d+)\s*coins?\s*per\s*pearls?', text)
    ticket_match = re.search(r'(\d+)\s*coins?\s*per\s*tickets?', text)

    if pearl_match:
        price = int(pearl_match.group(1))
        log(f"Pearl: {price}/pearl (max={MAX_PEARL_PRICE})")
        return "buy" if price <= MAX_PEARL_PRICE else "reject"

    if ticket_match:
        price = int(ticket_match.group(1))
        log(f"Ticket: {price}/ticket (max={MAX_TICKET_PRICE})")
        return "buy" if price <= MAX_TICKET_PRICE else "reject"

    log("Unknown offer - rejecting!")
    return "reject"

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def reset_last_action():
    global last_action_time
    last_action_time = time.time()

async def safe_click(m, idx):
    await asyncio.sleep(random.uniform(0.3, 1.2))
    try:
        await m.click(idx)
        return True
    except Exception as e:
        log(f"Click error: {e}")
        return False

async def explore():
    global last_action_time
    last_action_time = time.time()
    await client.send_message(BOT, "/explore")

def as_photo_file(image_bytes, name="monster_group.jpg"):
    """Wrap raw bytes with a filename so Telethon knows it's a JPEG and
    Telegram shows it as a viewable photo instead of an unnamed document."""
    f = io.BytesIO(image_bytes)
    f.name = name
    return f

# ══════════════════════════════════════════════════════════════
#  MONSTER-GROUP COUNTING — hash library (no AI)
# ══════════════════════════════════════════════════════════════

HASH_LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monster_hash_library.json")

# ahash alone is a coarse fingerprint (brightness pattern only) — fine for a
# small library, but collision risk grows as more entries pile up, especially
# since these screenshots share a lot of visual similarity (same ocean/sky
# background, similar card layouts). pHash (DCT-based, captures actual
# structure/frequency content, not just brightness) is added as a second,
# more discriminating check for anything registered from now on. Legacy
# entries that only have an ahash still work — they just get the older,
# looser check.
AHASH_MAX_DIST = 15   # out of 256 bits
PHASH_MAX_DIST = 10   # out of 64 bits — stricter, since pHash is more discriminating
# Ambiguity gap as a normalized RATIO (not raw bits) — comparisons can mix
# ahash-only legacy entries (256-bit scale) with ahash+phash entries
# (64-bit phash scale), so the gap has to be computed in the same
# normalized space as _distance() uses, not a fixed bit count.
AMBIGUITY_GAP_RATIO = 0.025   # ≈ 6/256 on the ahash scale, ≈ 1.6/64 on the phash scale

def ahash_bytes(image_bytes, size=16):
    """16x16 average-hash — cheap fingerprint of overall brightness pattern."""
    img_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    gray = cv2.imdecode(img_arr, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    small = cv2.resize(gray, (size, size))
    avg = small.mean()
    bits = (small > avg).flatten()
    return "".join("1" if b else "0" for b in bits)

def phash_bytes(image_bytes, size=32, hash_size=8):
    """DCT-based perceptual hash — captures actual image structure (edges,
    layout) rather than just overall brightness, so it's much better at
    telling apart two screenshots that happen to have similar brightness
    but genuinely different card layouts."""
    img_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    gray = cv2.imdecode(img_arr, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    small = cv2.resize(gray, (size, size)).astype(np.float32)
    dct = cv2.dct(small)
    # low-frequency block, excluding the DC term (index 0,0) which is just
    # average brightness and would skew the threshold
    block = dct[:hash_size, :hash_size].flatten()[1:]
    avg = block.mean()
    bits = (block > avg)
    return "".join("1" if b else "0" for b in bits)

def compute_hashes(image_bytes):
    """Compute both fingerprints for an image at once."""
    a = ahash_bytes(image_bytes)
    p = phash_bytes(image_bytes)
    if a is None:
        return None
    return {"hash": a, "phash": p}

def hamming(hash_a, hash_b):
    return sum(a != b for a, b in zip(hash_a, hash_b))

def load_hash_library():
    if not os.path.exists(HASH_LIB_PATH):
        return []
    try:
        with open(HASH_LIB_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"[HASH LIB] Load error: {e}")
        return []

def save_hash_library(entries):
    try:
        with open(HASH_LIB_PATH, "w") as f:
            json.dump(entries, f)
    except Exception as e:
        log(f"[HASH LIB] Save error: {e}")

def _is_match(query, entry):
    """True if query hashes are close enough to this library entry.
    Uses pHash when both sides have it (stricter, more reliable); falls
    back to ahash-only for legacy entries that predate pHash."""
    if query.get("phash") and entry.get("phash"):
        return hamming(query["phash"], entry["phash"]) <= PHASH_MAX_DIST
    return hamming(query["hash"], entry["hash"]) <= AHASH_MAX_DIST

def _distance(query, entry):
    """Single comparable distance score for ranking (0-1 normalized)."""
    if query.get("phash") and entry.get("phash"):
        return hamming(query["phash"], entry["phash"]) / 64
    return hamming(query["hash"], entry["hash"]) / 256

def lookup_hash_library(image_bytes):
    """Return (count, query_hashes) if a known screenshot matches closely
    enough AND unambiguously, else (None, query_hashes)."""
    query = compute_hashes(image_bytes)
    if query is None:
        return None, None

    entries = load_hash_library()
    matches = [(e, _distance(query, e)) for e in entries if _is_match(query, e)]
    if not matches:
        return None, query

    matches.sort(key=lambda x: x[1])
    best_entry, best_dist = matches[0]

    # Ambiguity check: if a DIFFERENT count is nearly as close a match,
    # refuse to trust it rather than risk a wrong auto-click. Compared
    # directly in normalized-ratio space so it's consistent whether the
    # best/second-best entries used ahash or phash for their distance.
    for entry, dist in matches[1:]:
        if entry["count"] != best_entry["count"] and (dist - best_dist) <= AMBIGUITY_GAP_RATIO:
            log(f"[HASH LIB] Ambiguous match — count={best_entry['count']} (dist={best_dist:.3f}) "
                f"vs count={entry['count']} (dist={dist:.3f}) too close to call. Falling back to counting.")
            return None, query

    log(f"[HASH LIB] Match found — count={best_entry['count']} (dist={best_dist:.3f})")
    return best_entry["count"], query

def register_hash_library(query_hashes, count):
    """Save a confirmed-correct (hashes, count) pair so future identical
    screenshots skip counting entirely."""
    if query_hashes is None or count is None:
        return
    entries = load_hash_library()

    for e in entries:
        if e["count"] == count and _is_match(query_hashes, e):
            # Legacy entry (ahash-only) matched — upgrade it in place with
            # a pHash now that we have one, instead of just skipping it.
            # This is how the whole library gradually gains pHash coverage
            # through normal play, with zero data loss and no manual re-work.
            if not e.get("phash") and query_hashes.get("phash"):
                e["phash"] = query_hashes["phash"]
                save_hash_library(entries)
                log(f"[HASH LIB] Upgraded legacy entry (count={count}) with pHash")
            return

    # Flag (but don't block) conflicts — a close match with a DIFFERENT
    # count likely means an existing entry was mislabeled at some point,
    # since this new one was just confirmed correct by the game itself.
    for e in entries:
        if e["count"] != count and _is_match(query_hashes, e):
            log(f"[HASH LIB] ⚠️ Conflict — new count={count} closely matches an existing "
                f"count={e['count']} entry. One of them may be mislabeled — worth checking "
                f"with check_dupe.py.")

    entries.append({"hash": query_hashes["hash"], "phash": query_hashes.get("phash"), "count": count})
    save_hash_library(entries)
    log(f"[HASH LIB] Registered new entry — count={count} (library size={len(entries)})")
    clear_hash_miss(query_hashes)

# ── Miss tracking — how many DISTINCT unregistered layouts are still
#    outstanding, so there's a visible sense of "how much is left to cover" ──
MISS_LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monster_hash_misses.json")

def load_hash_misses():
    if not os.path.exists(MISS_LIB_PATH):
        return []
    try:
        with open(MISS_LIB_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"[HASH MISSES] Load error: {e}")
        return []

def save_hash_misses(entries):
    try:
        with open(MISS_LIB_PATH, "w") as f:
            json.dump(entries, f)
    except Exception as e:
        log(f"[HASH MISSES] Save error: {e}")

def record_hash_miss(query_hashes):
    """Track a distinct unregistered layout that was just counted instead
    of matched. Deduplicated so repeat sightings of the same unregistered
    layout don't inflate the tally."""
    if query_hashes is None:
        return
    misses = load_hash_misses()
    for e in misses:
        if _is_match(query_hashes, e):
            return  # already tracked
    misses.append({"hash": query_hashes["hash"], "phash": query_hashes.get("phash")})
    save_hash_misses(misses)
    log(f"[HASH MISSES] New unregistered layout tracked (total outstanding: {len(misses)})")

def clear_hash_miss(query_hashes):
    """Once a layout gets registered, it's no longer 'outstanding' — drop
    it from the miss tally if it was being tracked."""
    misses = load_hash_misses()
    remaining = [e for e in misses if not _is_match(query_hashes, e)]
    if len(remaining) != len(misses):
        save_hash_misses(remaining)
        log(f"[HASH MISSES] Cleared — {len(misses) - len(remaining)} outstanding entr(y/ies) now covered")

def count_monsters_no_ai(image_bytes, max_count=12):
    """
    Count monster cards using fixed monster templates + cv2 matchTemplate.
    Multi-scale gap-vote heuristic. Returns a ranked list [best_guess, backup1, ...]
    used only as a fallback candidate list — the hash library is the primary path.
    """
    try:
        img_arr = np.frombuffer(image_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return None

        h, w = bgr.shape[:2]
        scale = 300 / max(h, w)
        bgr = cv2.resize(bgr, (int(w*scale), int(h*scale)))
        sh, sw = bgr.shape[:2]
        scene_gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_files = sorted([
            os.path.join(script_dir, f) for f in os.listdir(script_dir)
            if f.startswith("monster_template") and f.endswith(".jpg")
        ])

        if not template_files:
            log("[MONSTER GROUP] No template files found")
            return None

        all_votes = []
        for tpl_path in template_files:
            tpl = cv2.imread(tpl_path, cv2.IMREAD_GRAYSCALE)
            if tpl is None:
                continue

            for scale_frac in [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25]:
                tw = max(8, int(sw * scale_frac))
                th = max(8, int(sh * scale_frac))
                if tw >= sw or th >= sh:
                    continue
                t_resized = cv2.resize(tpl, (tw, th))
                result = cv2.matchTemplate(scene_gray, t_resized, cv2.TM_CCOEFF_NORMED)

                kernel = np.ones((th, tw), np.float32)
                dilated = cv2.dilate(result, kernel)
                local_max = (result == dilated) & (result > 0.0)
                ys, xs = np.where(local_max)
                if len(xs) == 0:
                    continue

                peaks = sorted(result[ys, xs].tolist(), reverse=True)[:max_count+2]
                if len(peaks) < 2:
                    continue

                gaps = [(peaks[i] - peaks[i+1], i+1) for i in range(len(peaks)-1)]
                max_gap, gap_idx = max(gaps)
                if max_gap >= 0.04:
                    all_votes.append(gap_idx)

        if not all_votes:
            return None

        log(f"[MONSTER GROUP] Votes: {all_votes}")

        from collections import Counter
        counter = Counter(all_votes)
        ranked = [count for count, _ in counter.most_common()]
        ranked = [min(max_count, max(1, c)) for c in ranked]
        seen = set()
        ranked_dedup = []
        for c in ranked:
            if c not in seen:
                seen.add(c)
                ranked_dedup.append(c)

        log(f"[MONSTER GROUP] Candidate counts (best first): {ranked_dedup}")
        return ranked_dedup

    except Exception as e:
        log(f"[MONSTER GROUP] No-AI count error: {e}")
        return None

# ══════════════════════════════════════════════════════════════
#  WIZARD HELPERS
# ══════════════════════════════════════════════════════════════

def is_reversed(text):
    return any(w in text.lower() for w in ["draziw", "lobmys", "ciler", "wollof"])

def flip_text(text):
    swaps = {'[':']', ']':'[', '{':'}', '}':'{', '(':')', ')':'('}
    return '\n'.join(''.join(swaps.get(c, c) for c in line[::-1]) for line in text.split('\n'))

def match_scrambled_move_scored(text):
    """Dice-coefficient fuzzy match, balanced against both text and known-move length."""
    from collections import Counter
    text_clean = re.sub(r'[^a-z]', '', text.lower())
    if len(text_clean) < 2:
        return None, 0
    best_move, best_score = None, 0
    for known in sorted(KNOWN_MOVES, key=len, reverse=True):
        known_clean = known.replace(' ', '').lower()
        if not (len(known_clean) - 2 <= len(text_clean) <= len(known_clean) + 5):
            continue
        text_counter  = Counter(text_clean)
        known_counter = Counter(known_clean)
        matches = sum(min(text_counter[c], known_counter[c]) for c in known_counter)
        score = (2 * matches) / (len(text_clean) + len(known_clean))
        if score >= 0.75 and score > best_score:
            best_score = score
            best_move  = known
    return best_move, best_score

def match_scrambled_move(text):
    move, _ = match_scrambled_move_scored(text)
    return move

def normalize_symbol(s):
    """Strip invisible variation-selector/combining-mark characters (e.g. U+FE0F).
    The game's scrambling can reorder these relative to the base character even
    though it looks visually identical, which breaks exact-string matching."""
    return "".join(c for c in s if unicodedata.category(c) not in ("Mn", "Cf"))

def extract_line_symbol(line):
    """
    Pull the actual choice-symbol out of a sequence line directly, without
    relying on bracket punctuation — it's been observed missing, reversed,
    prefixed with stray characters, and collapsed empty across different
    obfuscation variants, but the symbol character itself is always present.
    """
    non_ascii = [c for c in line if ord(c) > 127]
    filtered = [c for c in non_ascii if unicodedata.category(c) not in ("Mn", "Cf")]
    if filtered:
        seen = []
        for c in filtered:
            if c not in seen:
                seen.append(c)
        return seen[0]

    m = re.search(r'[\[\s]([+\-*^~])[\]\s]', line)
    if m:
        return m.group(1)
    return None

def looks_like_scrambled_word(token, target_word, threshold=0.75):
    """Dice-coefficient fuzzy match — tolerant of reordering AND inserted/
    extra characters, not just pure anagram scrambling. Needed because
    obfuscation sometimes scrambles the trigger word itself in different
    ways ('ignore' -> 'negrIo' is a reorder, 'IgnoTrEe' has extra letters
    inserted), not just move names."""
    from collections import Counter
    t = re.sub(r'[^a-z]', '', token.lower())
    if len(t) < 2:
        return False
    if not (len(target_word) - 2 <= len(t) <= len(target_word) + 5):
        return False
    t_counter = Counter(t)
    target_counter = Counter(target_word)
    matches = sum(min(t_counter[c], target_counter[c]) for c in target_counter)
    score = (2 * matches) / (len(t) + len(target_word))
    return score >= threshold

def get_ignore_emojis(text):
    ignore = set()
    for line in text.split('\n'):
        tokens = re.findall(r'\S+', line)
        ignore_idx = None
        for i, tok in enumerate(tokens):
            if 'ignore' in tok.lower() or looks_like_scrambled_word(tok, "ignore"):
                ignore_idx = i
                break
        if ignore_idx is None:
            continue
        for token in tokens[ignore_idx + 1:]:
            cleaned = token.strip('[](){}><|!. ')
            if not cleaned:
                continue
            cleaned = normalize_symbol(cleaned)
            if not cleaned:
                continue
            if (not cleaned.isascii()) or (len(cleaned) <= 2 and not cleaned.isalnum()):
                ignore.add(cleaned)
    return ignore

def is_ignored(emoji, ignore_set):
    for ign in ignore_set:
        if ign in emoji or emoji in ign:
            return True
    return False

def get_target_emoji(text, emoji_map=None):
    def clean(s):
        return normalize_symbol(s.strip('[](){}><|!. '))

    for m in re.finditer(r'(\S+)\s+s\w*mbol', text, re.IGNORECASE):
        c = clean(m.group(1))
        if c and not c.isascii(): return c

    for m in re.finditer(r's\w*mbol\s+(\S+)', text, re.IGNORECASE):
        c = clean(m.group(1))
        if c and not c.isascii(): return c

    m = re.search(r'lobmys\s+(\S+)', text)
    if m:
        c = clean(m.group(1))
        if c and not c.isascii(): return c

    if emoji_map:
        ignore = get_ignore_emojis(text)
        for emoji in emoji_map:
            if not is_ignored(emoji, ignore):
                return emoji

    return None

def get_move_for_emoji(text, emoji):
    for line in text.split('\n'):
        if emoji in line and "'" in line:
            moves = re.findall(r"'([^']+)'", line)
            for raw in moves:
                m = raw.strip().lower()
                rev = m[::-1]
                if m in KNOWN_MOVES: return m
                if rev in KNOWN_MOVES: return rev
                matched = match_scrambled_move(m)
                if matched: return matched
    return None

def parse_wizard_key(msg_or_text):
    """
    Handles all wizard key formats: underline entities, CODE=Move, CODE-Move,
    CODE Move, and CODEMove (no separator). Only scans lines after "tell you
    that" to avoid the wizard's taunt sentence being mistaken for a move.
    """
    def utf16_range_to_str_indices(text, utf16_offset, utf16_length):
        """Telegram reports entity offset/length in UTF-16 code units, not
        Python string indices. Since these messages are full of emoji
        (some needing 2+ UTF-16 units each) appearing BEFORE the key
        section, treating the raw offset as a direct string index drifts
        out of alignment — silently pointing at the wrong character and
        making underline detection fail entirely. Convert properly."""
        start_idx, end_idx = None, None
        units = 0
        target_end = utf16_offset + utf16_length
        for i, ch in enumerate(text):
            if units == utf16_offset:
                start_idx = i
            if units == target_end:
                end_idx = i
                break
            units += 2 if ord(ch) > 0xFFFF else 1
        if start_idx is None:
            start_idx = len(text)
        if end_idx is None:
            end_idx = len(text)
        return start_idx, end_idx

    if hasattr(msg_or_text, 'entities'):
        text     = msg_or_text.text or ""
        entities = msg_or_text.entities or []
        underlined_pos = set()
        for ent in (entities or []):
            if isinstance(ent, MessageEntityUnderline):
                start_idx, end_idx = utf16_range_to_str_indices(text, ent.offset, ent.length)
                for i in range(start_idx, end_idx):
                    underlined_pos.add(i)
        if underlined_pos:
            result = {}
            for m in re.finditer(r'(\S+)\s*=\s*([\w\.\- ]+)', text):
                code       = m.group(1)
                scrambled  = m.group(2).strip()
                code_start = m.start(1)
                for i, char in enumerate(code):
                    if (code_start + i) in underlined_pos:
                        move = match_scrambled_move(scrambled)
                        if move:
                            result[move] = char
                        break
            if result:
                log(f"[WIZARD] Underline key: {result}")
                return result
        text_for_parse = text
    else:
        text_for_parse = msg_or_text

    lines = text_for_parse.split('\n')
    start_idx = 0
    for i, line in enumerate(lines):
        if "tell you" in line.lower() or "never tell" in line.lower():
            start_idx = i + 1
            break

    result = {}
    for line in lines[start_idx:]:
        line = line.strip()
        if not line or len(line) < 5 or len(line) > 40:
            continue
        if any(p in line for p in [',', '!', '?', ';']):
            continue

        m = re.match(r'^([A-Za-z0-9]{4,14})\s*[=\-]\s*(\S.{2,20})$', line)
        if m:
            code, scrambled = m.group(1), m.group(2).strip()
            move = match_scrambled_move(scrambled)
            if move and move not in result:
                result[move] = code
                continue

        m = re.match(r'^([A-Za-z0-9]{4,14})\s+(\S.{2,20})$', line)
        if m:
            code, scrambled = m.group(1), m.group(2).strip()
            move = match_scrambled_move(scrambled)
            if move and move not in result:
                result[move] = code
                continue

        best_split = None
        best_split_score = 0
        for split in range(4, min(15, len(line) - 1)):
            code_part, move_part = line[:split], line[split:]
            if not re.match(r'^[A-Za-z0-9]+$', code_part):
                continue
            if not re.match(r'^[A-Za-z]', move_part):
                continue
            move, score = match_scrambled_move_scored(move_part)
            if move and score > best_split_score:
                best_split_score = score
                best_split = (move, code_part)
        if best_split:
            move, code = best_split
            if move not in result:
                result[move] = code

    if result:
        log(f"[WIZARD] Code key: {result}")
    return result

def parse_seq_num(text):
    m = re.search(r'[Ss]\w*\s*[Nn]\w*\s*[:\-]?\s*(\d+)', text)
    if m: return int(m.group(1))
    m = re.search(r'umber\s*[:\-]?\s*(\d+)', text, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r':\s*(\d+)\s*$', text, re.MULTILINE)
    if m: return int(m.group(1))
    return None

def find_btn_for_move(move, key):
    move_low = move.lower()
    for action, btn in key.items():
        if action == move_low or move_low in action or action in move_low:
            return btn
    return None

def match_move_to_btn(move, btns):
    move_low = move.lower()
    for i, b in enumerate(btns):
        b_low = b.lower()
        if b_low == move_low or move_low in b_low or b_low in move_low:
            return i
    return None

async def handle_wizard(msg):
    global wizard_active, wizard_key, wizard_last_done, wizard_last_click

    raw  = msg.text or ""
    btns = get_btns(msg)

    if not btns:
        log("[WIZARD] No buttons — skip")
        return

    if is_reversed(raw):
        raw = flip_text(raw)
        log("[WIZARD] Text flipped")

    seq_num = parse_seq_num(raw) or 0
    dedup   = (msg.id, seq_num)
    if wizard_last_done == dedup and time.time() - wizard_last_click < 3:
        log(f"[WIZARD] Already handled seq={seq_num} — skip")
        return
    wizard_last_done = dedup

    scrambled = not any(b.lower() in KNOWN_MOVES for b in btns)
    if scrambled:
        key = parse_wizard_key(msg)
        if key:
            wizard_key = key
        elif wizard_key:
            log("[WIZARD] Using cached key")
        else:
            log("[WIZARD] No key yet — skip")
            return

    emoji_map = {}
    for line in raw.split('\n'):
        if "'" not in line:
            continue
        symbol = extract_line_symbol(line)
        if symbol:
            moves_found = re.findall(r"'([^']+)'", line)
            if moves_found:
                emoji_map[symbol] = moves_found[0]

    emoji = get_target_emoji(raw, emoji_map)
    log(f"[WIZARD] emoji={emoji} seq={seq_num} map={emoji_map}")
    if not emoji:
        log(f"[WIZARD] Emoji not found! text={raw[:200]}")
        return

    move = get_move_for_emoji(raw, emoji)
    log(f"[WIZARD] move={move}")
    if not move:
        log(f"[WIZARD] Move not found for emoji '{emoji}'")
        return

    await asyncio.sleep(random.uniform(1.0, 5.0))
    reset_last_action()
    try:
        fresh = await client.get_messages(BOT, ids=msg.id)
    except Exception:
        fresh = msg
    btns = get_btns(fresh or msg)

    clicked = False
    if scrambled:
        btn = find_btn_for_move(move, wizard_key)
        log(f"[WIZARD] btn={btn} btns={btns}")
        if btn:
            idx = next((j for j, b in enumerate(btns) if b == btn), None)
            if idx is not None:
                await safe_click(fresh, idx)
                clicked = True
    else:
        idx = match_move_to_btn(move, btns)
        if idx is not None:
            await safe_click(fresh, idx)
            clicked = True

    reset_last_action()
    if clicked:
        wizard_last_click = time.time()
        log(f"[WIZARD] ✓ Clicked '{move}' seq={seq_num}")
    else:
        log(f"[WIZARD] ✗ No button for '{move}' btns={btns}")

# ══════════════════════════════════════════════════════════════
#  MAIN PROCESSOR — trades, captures, wizard, monster-group only.
#  No general battles: anything unrecognized just skips + re-explores.
# ══════════════════════════════════════════════════════════════

async def process_msg(m):
    global last_action_time
    global monster_paused, monster_group_msg, monster_candidates, monster_tried
    global monster_current_hash, monster_last_guess, monster_pending_image
    global wizard_active, wizard_key, wizard_last_done
    global bot_running

    if not bot_running:
        return  # /stop was sent — ignore everything until /resume

    last_action_time = time.time()

    btns = get_btns(m)
    bl   = [b.lower() for b in btns]
    text = (m.text or "").lower()
    raw  = m.text or ""
    log(f"MSG: btns={btns} text={text[:60]}")

    # ── Found a Core ─────────────────────────────────────────
    m_core = re.search(r'found a[n]?\s+([\w\s]+?core)\b', text, re.IGNORECASE)
    if m_core:
        core_name = m_core.group(1).strip().title()
        log(f"[FOUND] Core: {core_name}")
        await client.send_message("me", f"💠 Found a {core_name}!\n\n{raw}")

    # ── Found an Artifact ────────────────────────────────────
    m_artifact = re.search(r'found an artifact\s*-\s*(.+)', text, re.IGNORECASE)
    if m_artifact:
        artifact_name = m_artifact.group(1).strip().title()
        log(f"[FOUND] Artifact: {artifact_name}")
        await client.send_message("me", f"🗿 Found an artifact — {artifact_name}!")

    # ── /chat ────────────────────────────────────────────────
    if "you are now connected with user" in text or "start chatting with" in text:
        if not monster_paused:
            monster_paused = True
            log("💬 CHAT — Bot paused!")
            await client.send_message("me", "💬 Someone started a chat in Ordinal Legacy!\nReply manually then send /resume to continue.")
        reset_last_action()
        return

    # ── Wizard defeated ──────────────────────────────────────
    if "broke free from the spell" in text or "continue your journey" in text:
        log("[WIZARD] Defeated! Resetting...")
        wizard_active    = False
        wizard_key       = {}
        wizard_last_done = None
        await asyncio.sleep(1)
        await explore()
        return

    # ── Wizard key leak ──────────────────────────────────────
    # Trigger on the underline entities themselves (not just specific taunt
    # phrases like "would never tell" / "tell you that"), since the game
    # uses multiple different wordings for this reveal message (e.g. "The
    # underlined letter or number is the corresponding key to the move").
    # Relying on exact phrase text is brittle; the underline formatting
    # itself is the reliable signal that this message carries the key.
    has_underline = any(
        isinstance(e, MessageEntityUnderline) for e in (getattr(m, 'entities', None) or [])
    )
    if "would never tell" in text or "tell you that" in text or has_underline:
        new_key = parse_wizard_key(m)
        if new_key:
            wizard_key = new_key
            log(f"[WIZARD] Key stored: {new_key}")
            if not btns:
                return

    # ── Wizard step ──────────────────────────────────────────
    if "mystic wizard" in text or "evil mystic wizard" in text or "draziw" in text:
        wizard_active = True
        await handle_wizard(m)
        return

    # ── Block during wizard ──────────────────────────────────
    if wizard_active:
        reset_last_action()
        return

    # ── Monster group — wrong answer feedback → retry with next guess ──
    if "tries left" in text or ("not right" in text and "monster" in text):
        if monster_group_msg is not None and monster_candidates:
            next_guess = None
            while monster_candidates:
                c = monster_candidates.pop(0)
                if c not in monster_tried:
                    next_guess = c
                    break
            if next_guess is not None and len(monster_tried) < 2:
                btns2 = get_btns(monster_group_msg)
                for i, b in enumerate(btns2):
                    if str(next_guess) == b.strip():
                        log(f"[MONSTER GROUP] Wrong — retrying with {next_guess} (attempt {len(monster_tried)+1}/2)")
                        monster_tried.add(next_guess)
                        monster_last_guess = next_guess
                        await safe_click(monster_group_msg, i)
                        reset_last_action()
                        return
                log(f"[MONSTER GROUP] No button for retry guess {next_guess}")

        # Send the actual failed image so a /count reply can teach it,
        # then stop and wait for you rather than guessing again blind.
        if monster_pending_image:
            tried_str = ", ".join(str(t) for t in sorted(monster_tried)) if monster_tried else "?"
            await client.send_file(
                "me", as_photo_file(monster_pending_image),
                caption=f"❌ Both guesses wrong for this layout (tried: {tried_str}).\n"
                        f"Reply /count <n> with the real answer to teach me, then /resume."
            )

        bot_running = False
        monster_group_msg  = None
        monster_candidates = []
        monster_tried      = set()
        monster_current_hash = None
        monster_last_guess   = None
        log("🛑 2 tries failed — Bot stopped!")
        await client.send_message("me", "🛑 Monster group: 2 tries failed!\nAnswer manually then send /resume to restart bot.")
        reset_last_action()
        return

    # ── Monster group correctly solved ────────────────────────
    if "splashed foes" in text or ("earned" in text and "pearl" in text):
        if monster_current_hash is not None and monster_last_guess is not None:
            register_hash_library(monster_current_hash, monster_last_guess)
        monster_group_msg  = None
        monster_candidates = []
        monster_tried       = set()
        monster_current_hash = None
        monster_last_guess   = None
        monster_paused      = False
        reset_last_action()
        return

    # ── Monster group — count cards, no AI needed ─────────────
    if "group of monster" in text or "spot the number" in text or "group of monsters" in text:
        if not btns and not (m.photo or m.document):
            log("[MONSTER GROUP] Pre-alert text only (no image/buttons yet) — ignoring")
            return

        log("⚠️ MONSTER GROUP — Counting cards...")

        image_bytes = None
        if m.photo or m.document:
            try:
                image_bytes = await client.download_media(m, bytes)
            except Exception as e:
                log(f"[MONSTER GROUP] Download error: {e}")

        monster_pending_image = image_bytes

        candidates = None
        current_hash = None
        if image_bytes:
            numeric_btns = [int(b) for b in btns if b.strip().isdigit()]
            max_count = max(numeric_btns) if numeric_btns else 12

            lib_count, current_hash = lookup_hash_library(image_bytes)
            if lib_count is not None:
                log(f"[MONSTER GROUP] Hash library hit — count={lib_count} (no counting needed)")
                candidates = [lib_count]
            else:
                record_hash_miss(current_hash)
                candidates = count_monsters_no_ai(image_bytes, max_count=max_count)

        if candidates:
            monster_group_msg  = m
            monster_candidates = candidates[1:]
            monster_tried       = {candidates[0]}
            monster_current_hash = current_hash
            monster_last_guess   = candidates[0]
            best = candidates[0]
            for i, b in enumerate(btns):
                if str(best) == b.strip():
                    log(f"[MONSTER GROUP] Clicking '{b}' (backups: {monster_candidates})")
                    await safe_click(m, i)
                    reset_last_action()
                    return
            log(f"[MONSTER GROUP] No button matches count={best}, btns={btns}")

        if not monster_paused:
            monster_paused = True
            log("⚠️ Counting failed — Bot paused!")
            caption = "⚠️ Monster group counting failed! Answer manually, then send /count <n> so I remember it, then /resume."
            if image_bytes:
                await client.send_file("me", as_photo_file(image_bytes), caption=caption)
            else:
                await client.send_message("me", caption)
        reset_last_action()
        return

    if monster_paused:
        reset_last_action()
        return

    # ── Monster dead / trade done / item found (no buttons) ──
    if is_monster_dead(m):
        log("Monster dead! Exploring...")
        await explore()
        return

    # ── Trader: Buy it / Reject it ────────────────────────────
    if is_buy_reject(btns):
        decision = check_trader_offer(text)
        if decision == "buy":
            idx = get_btn_idx(bl, "buy")
            log(f"Buying! idx={idx}")
            await safe_click(m, idx)
        else:
            idx = get_btn_idx(bl, "reject")
            log(f"Rejecting! idx={idx}")
            await safe_click(m, idx)
        return

    # ── Trader: check out offers ──────────────────────────────
    if has_btn(bl, "check out") or has_btn(bl, "offer"):
        idx = get_btn_idx(bl, "check out") if has_btn(bl, "check out") else get_btn_idx(bl, "offer")
        log(f"Trader! Checking offers... idx={idx}")
        await safe_click(m, idx)
        return

    # ── Capture (freeze ray is part of the capture sequence) ──
    if has_btn(bl, "freeze ray"):
        idx = get_btn_idx(bl, "freeze ray")
        log(f"Freeze Ray! idx={idx}")
        await safe_click(m, idx)
        return

    if has_btn(bl, "capture"):
        if should_capture(m):
            idx = get_btn_idx(bl, "capture")
            name = get_matched_capture_name(m)
            log(f"Capture! idx={idx} name={name}")
            await safe_click(m, idx)
            await client.send_message("me", f"🎯 Capturing {name.title()} — it's on your watch list!")
        else:
            log("Not in capture list - skipping...")
            await asyncio.sleep(1)
            await explore()
        return

    # ── Everything else (incl. battles) — skip and explore again ──
    if len(btns) >= 1:
        log("Skipping - exploring...")
        await asyncio.sleep(1)
        await explore()
        return

    log("Ignoring...")

# ══════════════════════════════════════════════════════════════
#  SELF-MESSAGE COMMANDS
# ══════════════════════════════════════════════════════════════

@client.on(events.NewMessage(outgoing=True))
async def on_self(event):
    global monster_paused, bot_running, monster_pending_image
    text = (event.message.text or "").strip().lower()

    if text == "/pause":
        monster_paused = True
        log("⏸ Bot paused!")
        await client.send_message("me", "⏸ Bot paused! Send /resume to continue.")

    elif text == "/resume":
        monster_paused = False
        bot_running = True
        log("✅ Bot resumed!")
        await client.send_message("me", "✅ Bot resumed!")

    elif text == "/stop":
        bot_running = False
        log("🛑 Bot stopped!")
        await client.send_message("me", "🛑 Bot stopped! Send /resume to start again.")

    elif text.startswith("/count"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await client.send_message("me", "Usage: /count <n>  e.g. /count 7")
            return
        count = int(parts[1])
        if monster_pending_image is None:
            await client.send_message("me", "No pending monster-group image to label — this only works right after a group appears.")
            return
        hashes = compute_hashes(monster_pending_image)
        register_hash_library(hashes, count)
        monster_pending_image = None
        log(f"[HASH LIB] Manually registered via /count — count={count}")
        await client.send_message("me", f"✅ Saved — count={count}. I'll recognize this layout next time.")

    elif text == "/misses":
        registered = len(load_hash_library())
        outstanding = len(load_hash_misses())
        await client.send_message(
            "me",
            f"📊 Hash library: {registered} registered layout(s)\n"
            f"🕳️ Outstanding: {outstanding} distinct unregistered layout(s) seen so far\n\n"
            f"Outstanding ones get counted (less reliably) until you /count them."
        )

# ══════════════════════════════════════════════════════════════
#  LISTENERS
# ══════════════════════════════════════════════════════════════

@client.on(events.NewMessage(from_users=BOT))
async def on_new(event):
    await process_msg(event.message)

@client.on(events.MessageEdited(from_users=BOT))
async def on_edit(event):
    log("EDITED MSG!")
    await process_msg(event.message)

# ══════════════════════════════════════════════════════════════
#  WATCHDOG
# ══════════════════════════════════════════════════════════════

async def watchdog():
    global last_action_time
    while True:
        await asyncio.sleep(10)
        if not bot_running or wizard_active or monster_paused:
            last_action_time = time.time()
            continue
        if time.time() - last_action_time > 60:
            log("Stuck! Sending /explore...")
            await explore()

# ══════════════════════════════════════════════════════════════
#  NOTIFICATION BUTTONS (Pause / Resume / Stop on screen)
# ══════════════════════════════════════════════════════════════

CONTROL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_cap.txt")

def show_notification():
    """Shows a persistent notification with Pause/Resume/Stop buttons"""
    try:
        subprocess.run([
            "termux-notification",
            "--id", "101",
            "--title", "Ordinal Legacy — Capture & Trade Bot",
            "--content", "Tap a button to control the bot",
            "--button1", "Pause",
            "--button1-action", f"echo pause > {CONTROL_FILE}",
            "--button2", "Resume",
            "--button2-action", f"echo resume > {CONTROL_FILE}",
            "--button3", "Stop",
            "--button3-action", f"echo stop > {CONTROL_FILE}",
            "--ongoing"
        ], check=False)
    except Exception as e:
        log(f"[NOTIFICATION] Error showing notification: {e}")

async def control_watcher():
    """Polls the control file written by notification button taps"""
    global monster_paused, bot_running
    while True:
        await asyncio.sleep(2)
        if os.path.exists(CONTROL_FILE):
            try:
                with open(CONTROL_FILE) as f:
                    cmd = f.read().strip()
                os.remove(CONTROL_FILE)
            except Exception:
                cmd = ""

            if cmd == "pause":
                monster_paused = True
                log("⏸ Paused via notification button")
            elif cmd == "resume":
                monster_paused = False
                bot_running = True
                log("✅ Resumed via notification button")
            elif cmd == "stop":
                bot_running = False
                log("🛑 Stopped via notification button")

# ══════════════════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════════════════

async def main():
    global last_action_time
    await client.start(phone=PHONE)
    log("Connected! Capture & Trade bot started (with wizard + monster-group solving)!")
    log(f"Capture: {CAPTURE_LIST}")
    log(f"Max pearl: {MAX_PEARL_PRICE} | Max ticket: {MAX_TICKET_PRICE} | Enchants: always buy")
    last_action_time = time.time()
    show_notification()
    await client.send_message(BOT, "/explore")
    asyncio.create_task(watchdog())
    asyncio.create_task(control_watcher())
    await client.run_until_disconnected()

with client:
    client.loop.run_until_complete(main())
