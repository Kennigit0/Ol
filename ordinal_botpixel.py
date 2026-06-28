import asyncio, time, re, random, requests, base64, io
import numpy as np
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
wizard_active     = False
wizard_key        = {}
wizard_last_done  = None
wizard_last_click = 0

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

def count_monsters_no_ai(image_bytes):
    """
    Count monster cards without AI — template matching.
    All cards share identical artwork, so flat-background masking doesn't
    work on busy photo backgrounds (sky/ocean/forest). Instead:
    1. Find the patch with highest local variance (busy texture = inside a card)
    2. Use it as a template, slide it across the whole image
    3. Compute normalized cross-correlation at every position
    4. Count distinct peaks above a similarity threshold (with non-max
       suppression so the same card isn't counted twice)
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")  # grayscale
        scale = 200 / max(img.size)
        if scale < 1:
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        arr = np.array(img, dtype=np.float32)
        h, w = arr.shape

        # Estimated card size as a fraction of image (calibrated from real
        # game screenshots — cards are roughly square thumbnails)
        card_w = max(8, int(w * 0.19))
        card_h = max(8, int(h * 0.24))

        if h <= card_h or w <= card_w:
            return None

        # Find the busiest patch (highest variance) — likely inside a card,
        # not the smoother sky/ocean/background
        best_var, best_pos = -1, (0, 0)
        step = 4
        for y in range(0, h - card_h, step):
            for x in range(0, w - card_w, step):
                patch = arr[y:y+card_h, x:x+card_w]
                v = patch.var()
                if v > best_var:
                    best_var = v
                    best_pos = (x, y)

        tx, ty = best_pos
        template = arr[ty:ty+card_h, tx:tx+card_w]
        t_norm   = template - template.mean()
        t_energy = np.sqrt((t_norm**2).sum())
        if t_energy < 1e-6:
            return None

        # Vectorized sliding-window normalized cross-correlation
        from numpy.lib.stride_tricks import sliding_window_view
        windows  = sliding_window_view(arr, (card_h, card_w))
        win_mean = windows.mean(axis=(2, 3), keepdims=True)
        win_norm = windows - win_mean
        win_energy = np.sqrt((win_norm**2).sum(axis=(2, 3)))
        numer = (win_norm * t_norm).sum(axis=(2, 3))
        denom = win_energy * t_energy
        scores = np.divide(numer, denom, out=np.zeros_like(numer), where=denom > 1e-6)

        THRESH = 0.5
        ys, xs = np.where(scores > THRESH)
        if len(xs) == 0:
            return None
        candidates = sorted(zip(scores[ys, xs], xs, ys), reverse=True)

        peaks = []
        for score, x, y in candidates:
            too_close = False
            for px, py in peaks:
                if abs(x - px) < card_w * 0.7 and abs(y - py) < card_h * 0.7:
                    too_close = True
                    break
            if not too_close:
                peaks.append((x, y))

        total = min(9, max(1, len(peaks)))
        log(f"[MONSTER GROUP] Template-match estimate: {total} (peaks={peaks})")
        return total
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
                if cleaned and not cleaned.isascii():
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
        m_line = re.match(r'\[(.+?)\]', line)
        if m_line:
            bracket_content = m_line.group(1).strip()
            if bracket_content and not bracket_content.isascii():
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
    global monster_paused
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

    # ── Monster group — count cards, no AI needed ─────────────
    if "group of monster" in text or "spot the number" in text or "group of monsters" in text:
        log("⚠️ MONSTER GROUP — Counting cards...")

        image_bytes = None
        if m.photo or m.document:
            try:
                image_bytes = await client.download_media(m, bytes)
            except Exception as e:
                log(f"[MONSTER GROUP] Download error: {e}")

        count = None
        if image_bytes:
            count = count_monsters_no_ai(image_bytes)
            if count is None:
                log("[MONSTER GROUP] No-AI method failed, trying AI...")
                count = await count_monsters_with_ai(image_bytes)

        if count is not None:
            for i, b in enumerate(btns):
                if str(count) == b.strip():
                    log(f"[MONSTER GROUP] Clicking '{b}'")
                    await safe_click(m, i)
                    reset_last_action()
                    return
            log(f"[MONSTER GROUP] No button matches count={count}, btns={btns}")

        # Fallback — pause for manual solving if both methods failed
        if not monster_paused:
            monster_paused = True
            log("⚠️ Counting failed — Bot paused!")
            await client.send_message("me", "⚠️ Monster group counting failed! Answer manually then send /resume to continue.")
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
    global monster_paused
    text = (event.message.text or "").strip().lower()
    if text == "/resume":
        monster_paused = False
        log("✅ Bot resumed!")
        await client.send_message("me", "✅ Bot resumed!")

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
        if wizard_active or monster_paused:
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
#  START
# ══════════════════════════════════════════════════════════════

async def main():
    global last_action_time
    await client.start(phone=PHONE)
    log("Connected! Bot started!")
    last_action_time = time.time()
    await client.send_message(BOT, "/explore")
    asyncio.create_task(watchdog())
    await client.run_until_disconnected()

with client:
    client.loop.run_until_complete(main())
