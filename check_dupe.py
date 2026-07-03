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

def ahash_bytes(image_bytes, size=16):
    img_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    gray = cv2.imdecode(img_arr, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    small = cv2.resize(gray, (size, size))
    avg = small.mean()
    bits = (small > avg).flatten()
    return "".join("1" if b else "0" for b in bits)

def hamming(a, b):
    return sum(x != y for x, y in zip(a, b))

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

    h = ahash_bytes(data)
    if h is None:
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

    # rank all entries by distance, closest first
    ranked = sorted(
        [(hamming(h, e["hash"]), e["count"]) for e in entries],
        key=lambda x: x[0]
    )

    print(f"Library has {len(entries)} entries. Closest matches (0 = identical, 256 = totally different):\n")
    for dist, count in ranked[:5]:
        tag = ""
        if dist < 5:
            tag = "  <- near-identical, almost certainly already in library"
        elif dist < 15:
            tag = "  <- same render, likely a duplicate (this is the bot's own match threshold)"
        print(f"  dist={dist:3d}   count={count}{tag}")

    best_dist, best_count = ranked[0]
    print()
    if best_dist < 15:
        print(f"⚠️  Likely duplicate of an existing count={best_count} entry — probably no need to add.")
    else:
        print("✅ No close match — this looks like a new layout, safe to add with add_hash.py.")

if __name__ == "__main__":
    main()
