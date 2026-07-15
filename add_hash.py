#!/usr/bin/env python3
"""
Manually add a confirmed (screenshot, count) pair to the monster hash library.

Usage:
    python3 add_hash.py <path_to_screenshot.jpg> <count>

Example:
    python3 add_hash.py ~/storage/pictures/monster_group.jpg 5
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

def load_library():
    if not os.path.exists(LIB_PATH):
        return []
    with open(LIB_PATH, "r") as f:
        return json.load(f)

def save_library(entries):
    with open(LIB_PATH, "w") as f:
        json.dump(entries, f)

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 add_hash.py <path_to_screenshot.jpg> <count>")
        sys.exit(1)

    img_path, count_str = sys.argv[1], sys.argv[2]
    if not count_str.isdigit():
        print(f"Count must be a whole number, got: {count_str}")
        sys.exit(1)
    count = int(count_str)

    if not os.path.exists(img_path):
        print(f"File not found: {img_path}")
        sys.exit(1)

    with open(img_path, "rb") as f:
        data = f.read()

    query = compute_hashes(data)
    if query is None:
        print("Could not read image — is it a valid jpg/png?")
        sys.exit(1)

    entries = load_library()

    # warn if this looks like a near-duplicate of an existing entry with a DIFFERENT count
    for e in entries:
        if is_match(query, e) and e["count"] != count:
            print(f"⚠️  Warning: this screenshot closely matches an existing "
                  f"entry with count={e['count']}, but you're adding count={count}. "
                  f"Double check you're not mislabeling.")
        if is_match(query, e) and e["count"] == count:
            print(f"Already have a near-identical entry with count={count} — skipping duplicate.")
            sys.exit(0)

    entries.append({"hash": query["hash"], "phash": query.get("phash"), "count": count})
    save_library(entries)
    print(f"✅ Added — count={count}. Library size is now {len(entries)}.")

if __name__ == "__main__":
    main()
