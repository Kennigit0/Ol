import asyncio, time, re, random, requests, base64, io, os, subprocess
import numpy as np
import cv2
from PIL import Image
from telethon import TelegramClient, events
from telethon.tl.types import ReplyInlineMarkup

API_ID   = 39455771
API_HASH = "0150c2e270dfcf0f3cfdfdce8f0a7a49"
PHONE    = "+917990952611"
BOT      = "OrdinalLegacyBot"

GEMINI_API_KEY = "YOUR_GEMINI_API_KEY_HERE"   # ← paste your key here

CAPTURE_LIST = [
    "aurelite", "violet coil", "moon nyc", "sunphiny",
    "gelarxia", "drion", "gemwave", "apharon", "frost", "Xceynerite"
]

KNOWN_MOVES = {"attack", "small attack", "ultimate", "shield", "small"}

# ── Ultimate move names (add more as you discover) ────────────
ULTIMATE_NAMES = {
    "ultimate", "sword of motion", "cero", "fist",
    "gran rey cero", "getsuga tensho", "bankai", "shunko",
    "final flash", "spirit gun", "rose whip", "dark flame",
}

client           = TelegramClient("ordinalepic_session", API_ID, API_HASH)
last_action_time = 0
last_battle_msg  = None
ultimate_count = 0

monster_paused    = False
bot_running       = True   # /stop sets this False, bot ignores everything
monster_group_msg = None   # the original photo message (for re-clicking)
monster_candidates = []    # remaining untried guesses, best-first
monster_tried     = set()  # numbers already tried for current puzzle
wizard_active     = False
wizard_key        = {}
wizard_last_done  = None
wizard_last_click = 0

monster_current_hash  = None  # ahash of the image currently being solved
monster_last_guess    = None  # count value most recently clicked, for registration on success
monster_pending_image = None  # raw bytes of the most recent monster-group screenshot, for /count

# ─────────────────────────────────────────────────────────────

def get_btns(m):
    out = []
    if m and m.reply_markup and isinstance(m.reply_markup, ReplyInlineMarkup):
        for row in m.reply_markup.rows:
            for b in row.buttons:
                out.append(b.text)
    return out

def has_btn(bl, keyword):
    return any(keyword.lower() in b.lower() for b in bl)

def get_btn_idx(bl, keyword):
    for i, b in enumerate(bl):
        if keyword.lower() in b.lower():
            return i
    return 0

def has_ultimate(bl):
    return any(any(name in b for name in ULTIMATE_NAMES) for b in bl)

def get_ultimate_idx(bl):
    for i, b in enumerate(bl):
        if any(name in b for name in ULTIMATE_NAMES):
            return i
    return None

def is_monster_dead(m):
    if get_btns(m):
        return False
    text = (m.text or "").lower()
    return any(k in text for k in [
        "gelarxia", "also found", "traded", "rejected",
        "you broke free from the spell", "stole", "continue your journey",
    ])

def is_ongoing(m):
    text = (m.text or "").lower()
    return "ongoing" in text or "finish that first" in text

def needs_fight(m):
    text = (m.text or "").lower()
    return "enemy to defeat" in text or "do /fight" in text

def should_capture(m):
    text = (m.text or "").lower()
    for name in CAPTURE_LIST:
        if re.search(r'\b' + re.escape(name) + r'\b', text):
            return True
    return False

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def reset_last_action():
    global last_action_time
    last_action_time = time.time()

async def safe_click(m, idx):
    await asyncio.sleep(random.uniform(0.3, 1.2))  # ← ADD THIS
    try:
        await m.click(idx)
        return True
    except Exception as e:
        log(f"Click error: {e}")
        return False

async def click_battle(m):
    global ultimate_count
    btns = get_btns(m)
    bl = [b.lower() for b in btns]
    if has_ultimate(bl) and ultimate_count < 2:
        idx = get_ultimate_idx(bl)
        ultimate_count += 1
        log(f"Ultimate! ({ultimate_count}/2) btn='{btns[idx]}'")
        await safe_click(m, idx)
        return True
    elif has_btn(bl, "attack"):
        idx = get_btn_idx(bl, "attack")
        log(f"Attack! idx={idx}")
        await safe_click(m, idx)
        return True
    return False

async def explore():
    global last_action_time
    last_action_time = time.time()
    await client.send_message(BOT, "/explore")

HASH_LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monster_hash_library.json")
HASH_MATCH_MAX_DIST = 15   # out of 256 bits — same render ≈ 0-5, different count/monster ≈ 100+

def ahash_bytes(image_bytes, size=16):
    """16x16 average-hash — cheap fingerprint of a screenshot's exact visual layout."""
    img_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    gray = cv2.imdecode(img_arr, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    small = cv2.resize(gray, (size, size))
    avg = small.mean()
    bits = (small > avg).flatten()
    # pack bits into a hex string for compact JSON storage
    return "".join("1" if b else "0" for b in bits)

def hamming(hash_a, hash_b):
    return sum(a != b for a, b in zip(hash_a, hash_b))

def load_hash_library():
    if not os.path.exists(HASH_LIB_PATH):
        return []
    try:
        import json
        with open(HASH_LIB_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"[HASH LIB] Load error: {e}")
        return []

def save_hash_library(entries):
    try:
        import json
        with open(HASH_LIB_PATH, "w") as f:
            json.dump(entries, f)
    except Exception as e:
        log(f"[HASH LIB] Save error: {e}")

def lookup_hash_library(image_bytes):
    """Return (count, matched_hash) if a known screenshot matches closely enough, else (None, hash)."""
    h = ahash_bytes(image_bytes)
    if h is None:
        return None, None
    entries = load_hash_library()
    best = None
    best_dist = HASH_MATCH_MAX_DIST + 1
    for e in entries:
        d = hamming(h, e["hash"])
        if d < best_dist:
            best_dist = d
            best = e["count"]
    if best is not None:
        log(f"[HASH LIB] Match found — count={best} (dist={best_dist}/256)")
    return best, h

def register_hash_library(image_hash, count):
    """Save a confirmed-correct (hash, count) pair so future identical screenshots skip counting entirely."""
    if image_hash is None or count is None:
        return
    entries = load_hash_library()
    # avoid saving near-duplicates of something already stored with the same count
    for e in entries:
        if e["count"] == count and hamming(image_hash, e["hash"]) < 5:
            return
    entries.append({"hash": image_hash, "count": count})
    save_hash_library(entries)
    log(f"[HASH LIB] Registered new entry — count={count} (library size={len(entries)})")


def count_monsters_no_ai(image_bytes, max_count=12):
    """
    Count monster cards using fixed monster templates + cv2 matchTemplate.

    Instead of guessing which patch in the scene is a card (which fails
    when the background is busier than the cards), we use known monster
    artwork files saved alongside this script. For each saved template,
    try multiple scales, find the biggest score GAP in the ranked peaks
    (real cards score high, background scores low → gap marks the cutoff),
    and vote across scales. The most common gap-count wins.

    Returns a ranked list [best_guess, backup1, backup2, ...].
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

        # Load all monster template files from the same directory as this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_files = sorted([
            os.path.join(script_dir, f) for f in os.listdir(script_dir)
            if f.startswith("monster_template") and f.endswith(".jpg")
        ])

        if not template_files:
            log("[MONSTER GROUP] No template files found — falling back to auto-detect")
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

                # Dilation NMS — collapses pixel-jitter blobs to single peaks
                kernel = np.ones((th, tw), np.float32)
                dilated = cv2.dilate(result, kernel)
                local_max = (result == dilated) & (result > 0.0)
                ys, xs = np.where(local_max)
                if len(xs) == 0:
                    continue

                peaks = sorted(result[ys, xs].tolist(), reverse=True)[:max_count+2]
                if len(peaks) < 2:
                    continue

                # Biggest gap in sorted scores = boundary between cards and background
                gaps = [(peaks[i] - peaks[i+1], i+1) for i in range(len(peaks)-1)]
                max_gap, gap_idx = max(gaps)
                # Only vote when gap is meaningful
                if max_gap >= 0.04:
                    all_votes.append(gap_idx)

        if not all_votes:
            return None

        log(f"[MONSTER GROUP] Votes: {all_votes}")

        # Build ranked list: most-voted first, then unique other values
        from collections import Counter
        counter = Counter(all_votes)
        ranked = [count for count, _ in counter.most_common()]
        ranked = [min(max_count, max(1, c)) for c in ranked]
        # Deduplicate while preserving order
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

async def count_monsters_with_ai(image_bytes):
    """Send image to Gemini via REST API, return the count it finds (or None)"""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        payload = {
            "contents": [{
                "parts": [
                    {"text": "Count exactly how many monster/creature card images appear in this picture. Reply with ONLY the number, nothing else."},
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
                ]
            }]
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

        # Retry on rate limit (429) with backoff — don't switch models for this
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=20)
                if r.status_code == 429:
                    wait = 3 * (attempt + 1)
                    log(f"[MONSTER GROUP] Rate limited, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                out_text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r'\d+', out_text)
                if m:
                    count = int(m.group())
                    log(f"[MONSTER GROUP] AI counted: {count}")
                    return count
                log(f"[MONSTER GROUP] AI gave no number: {out_text!r}")
                return None
            except requests.exceptions.HTTPError as e:
                log(f"[MONSTER GROUP] HTTP error: {e}")
                return None

        log("[MONSTER GROUP] Still rate limited after retries")
        return None
    except Exception as e:
        log(f"[MONSTER GROUP] AI error: {e}")
        return None

async def fight():
    global last_action_time
    last_action_time = time.time()
    log("Sending /fight...")
    await client.send_message(BOT, "/fight")

# ══════════════════════════════════════════════════════════════
#  WIZARD HELPERS  (simple version)
# ══════════════════════════════════════════════════════════════

def is_reversed(text):
    return any(w in text.lower() for w in ["draziw", "lobmys", "ciler", "wollof"])

def flip_text(text):
    swaps = {'[':']', ']':'[', '{':'}', '}':'{', '(':')', ')':'('}
    return '\n'.join(''.join(swaps.get(c, c) for c in line[::-1]) for line in text.split('\n'))

def match_scrambled_move_scored(text):
    """Same scoring logic as match_scrambled_move, but returns (move, score)"""
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
    """
    Fuzzy match scrambled/truncated move name using Dice-coefficient scoring.
    This balances against BOTH text and known-move length, so a short move
    like "attack" can't falsely score 1.0 just because it's a letter-subset
    of a longer text (which is what caused "shield" to match the wizard's
    taunt sentence before).
    """
    move, _ = match_scrambled_move_scored(text)
    return move

def get_ignore_emojis(text):
    ignore = set()
    for line in text.split('\n'):
        if 'ignore' in line.lower():
            for token in re.findall(r'\S+', line):
                cleaned = token.strip('[](){}><|!. ')
                if not cleaned:
                    continue
                # Real emoji/unicode symbols, OR short non-alphanumeric
                # ASCII symbols like "+" or "-" (but not stray words).
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
        return s.strip('[](){}><|!. ')

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
    Handles ALL wizard key formats safely:
    - Underline entities (single letter/number buttons)
    - CODE = ScrambledMove
    - CODE-ScrambledMove (no space)
    - CODE space ScrambledMove
    Only scans lines AFTER "tell you that" to avoid the wizard's
    taunt sentence (e.g. "Wizard krrr...") being mistaken for a move.
    """
    from telethon.tl.types import MessageEntityUnderline

    # ── Underline entity detection ──────────────────────────────
    if hasattr(msg_or_text, 'entities'):
        text     = msg_or_text.text or ""
        entities = msg_or_text.entities or []
        underlined_pos = set()
        for ent in (entities or []):
            if isinstance(ent, MessageEntityUnderline):
                for i in range(ent.offset, ent.offset + ent.length):
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

    # ── Code-based formats — only AFTER the reveal trigger line ──
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
        # Reject lines with punctuation typical of sentences (not key lines)
        if any(p in line for p in [',', '!', '?', ';']):
            continue

        # Format: CODE = ScrambledMove  or  CODE - ScrambledMove
        # (no IGNORECASE on code part — keeps code/move boundary exact)
        m = re.match(r'^([A-Z0-9]{4,8})\s*[=\-]\s*(\S.{2,20})$', line)
        if m:
            code, scrambled = m.group(1), m.group(2).strip()
            move = match_scrambled_move(scrambled)
            if move and move not in result:
                result[move] = code
                continue

        # Format: CODE space ScrambledMove
        m = re.match(r'^([A-Z0-9]{4,8})\s+(\S.{2,20})$', line)
        if m:
            code, scrambled = m.group(1), m.group(2).strip()
            move = match_scrambled_move(scrambled)
            if move and move not in result:
                result[move] = code
                continue

        # Format: CODEScrambledMove (no separator — try every possible split
        # point and keep whichever gives the HIGHEST fuzzy match score, since
        # both the code and a capitalized move-start are uppercase letters)
        best_split = None
        best_split_score = 0
        for split in range(4, min(9, len(line) - 1)):
            code_part, move_part = line[:split], line[split:]
            if not re.match(r'^[A-Z0-9]+$', code_part):
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

# ══════════════════════════════════════════════════════════════
#  WIZARD HANDLER
# ══════════════════════════════════════════════════════════════

async def handle_wizard(msg):
    global wizard_active, wizard_key, wizard_last_done, wizard_last_click

    raw  = msg.text or ""
    btns = get_btns(msg)

    if not btns:
        log("[WIZARD] No buttons — skip")
        return

    # Flip reversed text
    if is_reversed(raw):
        raw = flip_text(raw)
        log("[WIZARD] Text flipped")

    # Sequence dedup
    seq_num = parse_seq_num(raw) or 0
    dedup   = (msg.id, seq_num)
    if wizard_last_done == dedup and time.time() - wizard_last_click < 3:
        log(f"[WIZARD] Already handled seq={seq_num} — skip")
        return
    wizard_last_done = dedup

    # Scrambled buttons?
    scrambled = not any(b.lower() in KNOWN_MOVES for b in btns)
    if scrambled:
        key = parse_wizard_key(msg)   # pass full msg for entity detection
        if key:
            wizard_key = key
        elif wizard_key:
            log(f"[WIZARD] Using cached key")
        else:
            log("[WIZARD] No key yet — skip")
            return

    # Build emoji→first_move map from sequence lines
    emoji_map = {}
    for line in raw.split('\n'):
        # Normal bracket form: [SYMBOL] — search (not match) so a stray
        # leading character before the bracket (e.g. "k[🎵🎵🎵]") doesn't
        # hide the whole line from the parser.
        m_line = re.search(r'\[(.{1,4}?)\]', line)
        bracket_content = None
        if m_line:
            bracket_content = m_line.group(1).strip()
        else:
            # Reversed/local-scramble form: SYMBOL][  (symbol sits outside
            # the brackets, brackets themselves swapped) — e.g. "🎵]["
            m_rev = re.search(r'(\S{1,4})\]\[', line)
            if m_rev:
                bracket_content = m_rev.group(1).strip()

        if bracket_content:
            # Accept non-ascii emoji/unicode symbols AND short plain-ASCII
            # symbols like "+" or "-" — only reject if it looks like it's
            # actually a stray letter/word fragment (long or alphabetic).
            is_valid_symbol = (not bracket_content.isascii()) or (
                len(bracket_content) <= 2 and not bracket_content.isalnum()
            )
            if is_valid_symbol:
                moves_found = re.findall(r"'([^']+)'", line)
                if moves_found:
                    emoji_map[bracket_content] = moves_found[0]

    # Find target emoji
    emoji = get_target_emoji(raw, emoji_map)
    log(f"[WIZARD] emoji={emoji} seq={seq_num} map={emoji_map}")
    if not emoji:
        log(f"[WIZARD] Emoji not found! text={raw[:200]}")
        return

    # Find move for that emoji
    move = get_move_for_emoji(raw, emoji)
    log(f"[WIZARD] move={move}")
    if not move:
        log(f"[WIZARD] Move not found for emoji '{emoji}'")
        return

    # Wait then re-fetch
    await asyncio.sleep(random.uniform(1.0, 5.0))
    reset_last_action()
    try:
        fresh = await client.get_messages(BOT, ids=msg.id)
    except Exception:
        fresh = msg
    btns = get_btns(fresh or msg)

    # Click
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
#  SHARED PROCESSOR
# ══════════════════════════════════════════════════════════════

async def process(m):
    global last_action_time, last_battle_msg, ultimate_count
    global wizard_active, wizard_key, wizard_last_done
    global monster_paused, monster_group_msg, monster_candidates, monster_tried
    global monster_current_hash, monster_last_guess, monster_pending_image
    global bot_running

    if not bot_running:
        return  # /stop was sent — ignore everything until /resume

    last_action_time = time.time()

    btns = get_btns(m)
    bl   = [b.lower() for b in btns]
    text = (m.text or "").lower()
    raw  = m.text or ""
    log(f"MSG: btns={btns} text={text[:60]}")

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
        last_battle_msg  = None
        await asyncio.sleep(1)
        await explore()
        return

    # ── Wizard key leak ──────────────────────────────────────
    if "would never tell" in text or "tell you that" in text:
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

        # 2 tries used up OR no more candidates → stop bot
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

    # ── Monster group correctly solved (caption gets edited to success) ─
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
        # Pre-alert messages ("a group of monsters are approaching...") have no
        # buttons and no image yet — the real countable message follows right
        # after. Don't treat this as a failed count / trigger a pause for it.
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

        monster_pending_image = image_bytes  # available for a manual /count reply regardless of outcome below

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
                candidates = count_monsters_no_ai(image_bytes, max_count=max_count)
                if candidates is None:
                    log("[MONSTER GROUP] No-AI method failed, trying AI...")
                    ai_count = await count_monsters_with_ai(image_bytes)
                    candidates = [ai_count] if ai_count is not None else None

        if candidates:
            monster_group_msg  = m
            monster_candidates = candidates[1:]   # remaining ones saved for retry
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

        # Fallback — pause for manual solving if both methods failed
        if not monster_paused:
            monster_paused = True
            log("⚠️ Counting failed — Bot paused!")
            caption = "⚠️ Monster group counting failed! Answer manually, then send /count <n> so I remember it, then /resume."
            if image_bytes:
                await client.send_file("me", image_bytes, caption=caption)
            else:
                await client.send_message("me", caption)
        reset_last_action()
        return

    if monster_paused:
        reset_last_action()
        return

    # ── Enemy to defeat ──────────────────────────────────────
    if needs_fight(m):
        log("Enemy pending! Sending /fight...")
        await fight()
        return

    # ── Ongoing ──────────────────────────────────────────────
    if is_ongoing(m):
        log("Ongoing! Retrying battle...")
        if last_battle_msg:
            await click_battle(last_battle_msg)
        return

    # ── Monster dead ─────────────────────────────────────────
    if is_monster_dead(m):
        log("Monster dead! Exploring...")
        last_battle_msg = None
        ultimate_count  = 0
        await explore()
        return

    # ── Ultimate (max 1x) ────────────────────────────────────
    if has_ultimate(bl) and ultimate_count < 2:
        last_battle_msg = m
        idx = get_ultimate_idx(bl)
        ultimate_count += 1
        log(f"Ultimate! ({ultimate_count}/2) btn='{btns[idx]}'")
        await safe_click(m, idx)
        return

    # ── Attack ───────────────────────────────────────────────
    if has_btn(bl, "attack"):
        last_battle_msg = m
        idx = get_btn_idx(bl, "attack")
        log(f"Attack! idx={idx}")
        await safe_click(m, idx)
        return
    # ── Attack ───────────────────────────────────────────────
    if has_btn(bl, "📦"):
        last_battle_msg = m
        idx = get_btn_idx(bl, "📦")
        log(f"📦! idx={idx}")
        await safe_click(m, idx)
        return

    # ── Freeze Ray ───────────────────────────────────────────
    if has_btn(bl, "freeze ray"):
        idx = get_btn_idx(bl, "freeze ray")
        log(f"Freeze Ray! idx={idx}")
        await safe_click(m, idx)
        return

    # ── Capture ──────────────────────────────────────────────
    if has_btn(bl, "capture"):
        if should_capture(m):
            idx = get_btn_idx(bl, "capture")
            log(f"Capture! idx={idx}")
            await safe_click(m, idx)
        else:
            log("Not in capture list - skipping...")
            await asyncio.sleep(1)
            await explore()
        return

    # ── Offers ───────────────────────────────────────────────
    if has_btn(bl, "check out") or has_btn(bl, "offer"):
        idx = get_btn_idx(bl, "check out") if has_btn(bl, "check out") else get_btn_idx(bl, "offer")
        log(f"Offers! idx={idx}")
        await safe_click(m, idx)
        return

    # ── Trader ───────────────────────────────────────────────
    if has_btn(bl, "buy it") or has_btn(bl, "reject it"):
        price_match = re.search(r'(\d+)\s*coins?\s*per', text)
        price = int(price_match.group(1)) if price_match else 999999
        should_buy = False
        if "pearl" in text and price < 240:
            log(f"Buying pearls! price={price}")
            should_buy = True
        elif "ticket" in text and price < 440:
            log(f"Buying tickets! price={price}")
            should_buy = True
        elif "enchant" in text:
            log(f"Buying enchant! price={price}")
            should_buy = True
        else:
            log(f"Rejecting! price={price}")
        if should_buy:
            await safe_click(m, get_btn_idx(bl, "buy it"))
        else:
            await safe_click(m, get_btn_idx(bl, "reject it"))
        return

    # ── Engage ───────────────────────────────────────────────
    SKIP_BUTTONS = {"collections", "artifacts", "details", "prestige", "essences",
                    "show ability", "rope", "net", "chain", "tranquilizer"}
    if len(btns) >= 1:
        if btns[0].lower() in SKIP_BUTTONS:
            log(f"Skipping: {btns[0]}")
            return
        log("Engage! clicking index 0")
        await safe_click(m, 0)
        return

    log("Ignoring...")

# ══════════════════════════════════════════════════════════════
#  SELF MESSAGE HANDLER
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
        h = ahash_bytes(monster_pending_image)
        register_hash_library(h, count)
        monster_pending_image = None
        log(f"[HASH LIB] Manually registered via /count — count={count}")
        await client.send_message("me", f"✅ Saved — count={count}. I'll recognize this layout next time.")

# ══════════════════════════════════════════════════════════════
#  LISTENERS
# ══════════════════════════════════════════════════════════════

@client.on(events.NewMessage(chats=BOT))
async def on_new(event):
    await process(event.message)

@client.on(events.MessageEdited(chats=BOT))
async def on_edit(event):
    await process(event.message)

# ══════════════════════════════════════════════════════════════
#  WATCHDOG
# ══════════════════════════════════════════════════════════════

async def watchdog():
    global last_action_time, last_battle_msg
    while True:
        await asyncio.sleep(5)
        if not bot_running or wizard_active or monster_paused:
            last_action_time = time.time()
            continue
        if time.time() - last_action_time > 5:
            log("Stuck!")
            if last_battle_msg:
                log("Retrying battle...")
                await click_battle(last_battle_msg)
                last_action_time = time.time()
            else:
                log("Sending /explore...")
                await explore()

# ══════════════════════════════════════════════════════════════
#  NOTIFICATION BUTTONS (Pause / Resume / Stop on screen)
# ══════════════════════════════════════════════════════════════

CONTROL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control.txt")

def show_notification():
    """Shows a persistent notification with Pause/Resume/Stop buttons"""
    try:
        subprocess.run([
            "termux-notification",
            "--id", "100",
            "--title", "Ordinal Legacy Bot",
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
    log("Connected! Bot started!")
    last_action_time = time.time()
    show_notification()
    await client.send_message(BOT, "/explore")
    asyncio.create_task(watchdog())
    asyncio.create_task(control_watcher())
    await client.run_until_disconnected()

with client:
    client.loop.run_until_complete(main())
