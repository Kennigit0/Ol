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

    h = ahash_bytes(data)
    if h is None:
        print("Could not read image — is it a valid jpg/png?")
        sys.exit(1)

    entries = load_library()

    # warn if this looks like a near-duplicate of an existing entry with a DIFFERENT count
    for e in entries:
        d = hamming(h, e["hash"])
        if d < 15 and e["count"] != count:
            print(f"⚠️  Warning: this screenshot is very close (dist={d}) to an existing "
                  f"entry with count={e['count']}, but you're adding count={count}. "
                  f"Double check you're not mislabeling.")
        if d < 5 and e["count"] == count:
            print(f"Already have a near-identical entry with count={count} — skipping duplicate.")
            sys.exit(0)

    entries.append({"hash": h, "count": count})
    save_library(entries)
    print(f"✅ Added — count={count}. Library size is now {len(entries)}.")

if __name__ == "__main__":
    main()
