#!/usr/bin/env python3
"""
Check whether a screenshot already closely matches something in the hash library
before adding it — helps you spot duplicates or catch mislabeling early.

Usage:
    python3 check_dupe.py <path_to_screenshot.jpg>
"""
import sys, os, json
import cv2
import numpy as np

LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monster_hash_library.json")

AHASH_MAX_DIST = 15   # out of 256 bits
PHASH_MAX_DIST = 10   # out of 64 bits — stricter, since pHash is more discriminating

def ahash_bytes(image_bytes, size=16):
    img_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    gray = cv2.imdecode(img_arr, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    small = cv2.resize(gray, (size, size))
    avg = small.mean()
    bits = (small > avg).flatten()
    return "".join("1" if b else "0" for b in bits)

def phash_bytes(image_bytes, size=32, hash_size=8):
    img_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    gray = cv2.imdecode(img_arr, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    small = cv2.resize(gray, (size, size)).astype(np.float32)
    dct = cv2.dct(small)
    block = dct[:hash_size, :hash_size].flatten()[1:]
    avg = block.mean()
    bits = (block > avg)
    return "".join("1" if b else "0" for b in bits)

def compute_hashes(image_bytes):
    a = ahash_bytes(image_bytes)
    p = phash_bytes(image_bytes)
    if a is None:
        return None
    return {"hash": a, "phash": p}

def hamming(a, b):
    return sum(x != y for x, y in zip(a, b))

def is_match(query, entry):
    if query.get("phash") and entry.get("phash"):
        return hamming(query["phash"], entry["phash"]) <= PHASH_MAX_DIST
    return hamming(query["hash"], entry["hash"]) <= AHASH_MAX_DIST

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 check_dupe.py <path_to_screenshot.jpg>")
        sys.exit(1)

    img_path = sys.argv[1]
    if not os.path.exists(img_path):
        print(f"File not found: {img_path}")
        sys.exit(1)

    with open(img_path, "rb") as f:
        data = f.read()

    query = compute_hashes(data)
    if query is None:
        print("Could not read image — is it a valid jpg/png?")
        sys.exit(1)

    if not os.path.exists(LIB_PATH):
        print("Library is empty — nothing to compare against.")
        sys.exit(0)

    with open(LIB_PATH, "r") as f:
        entries = json.load(f)

    if not entries:
        print("Library is empty — nothing to compare against.")
        sys.exit(0)

    # rank all entries by phash distance when available, else ahash
    ranked = []
    for e in entries:
        if query.get("phash") and e.get("phash"):
            d = hamming(query["phash"], e["phash"])
            ranked.append((d, 64, e["count"], "phash"))
        else:
            d = hamming(query["hash"], e["hash"])
            ranked.append((d, 256, e["count"], "ahash"))
    ranked.sort(key=lambda x: x[0] / x[1])

    print(f"Library has {len(entries)} entries. Closest matches (lower = more similar):\n")
    for dist, total, count, kind in ranked[:5]:
        tag = "  <- match (bot would recognize this)" if dist <= (PHASH_MAX_DIST if kind == "phash" else AHASH_MAX_DIST) else ""
        print(f"  {kind} dist={dist:3d}/{total}   count={count}{tag}")

    best_dist, best_total, best_count, best_kind = ranked[0]
    best_is_match = best_dist <= (PHASH_MAX_DIST if best_kind == "phash" else AHASH_MAX_DIST)
    print()
    if best_is_match:
        print(f"⚠️  Likely duplicate of an existing count={best_count} entry — probably no need to add.")
    else:
        print("✅ No close match — this looks like a new layout, safe to add with add_hash.py.")

if __name__ == "__main__":
    main()
