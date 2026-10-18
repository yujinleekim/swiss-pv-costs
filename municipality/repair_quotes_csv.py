"""
Check / repair municipality_quotes.csv after an interrupted run.

Why: if the script was stopped while writing, the last row can be cut off in the middle of the
raw_json field. The csv module then reads everything after it as one huge field and fails with
"field larger than field limit". Every record is exactly ONE physical line (json.dumps escapes
newlines), so we can check the file line by line and drop only the damaged lines. Dropped rows are
simply redone when get_municipality_quotes.py is started again (it resumes by EGID).

Usage (from the swiss-pv-costs folder):
  python municipality/repair_quotes_csv.py            # only report
  python municipality/repair_quotes_csv.py --fix      # backup to .bak, then remove damaged lines
"""
import argparse, csv, json, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
csv.field_size_limit(2**31 - 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(HERE, "municipality_quotes.csv"))
    ap.add_argument("--fix", action="store_true")
    a = ap.parse_args()
    with open(a.file, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()
    header = next(csv.reader([lines[0]]))
    n_cols = len(header)
    ix = {h: i for i, h in enumerate(header)}
    print(f"{len(lines) - 1} data lines, {n_cols} columns")
    good, bad = [lines[0]], []
    longest = 0
    for no, line in enumerate(lines[1:], start=2):
        why = None
        try:
            rows = list(csv.reader([line]))
            if len(rows) != 1 or len(rows[0]) != n_cols:
                why = f"wrong column count ({len(rows[0]) if rows else 0})"
            else:
                r = rows[0]
                longest = max(longest, max(len(c) for c in r))
                if not line.endswith("\n"):
                    why = "no line break at end (cut off)"
                elif r[ix["raw_json"]].strip():
                    json.loads(r[ix["raw_json"]])
        except Exception as e:
            why = f"unreadable: {str(e)[:60]}"
        (bad if why else good).append((no, why, line) if why else line)
    print(f"ok: {len(good) - 1}   damaged: {len(bad)}   longest field: {longest} chars")
    for no, why, line in bad[:20]:
        print(f"  line {no}: {why} | {line[:90].strip()}")
    if a.fix and bad:
        shutil.copy(a.file, a.file + ".bak")
        with open(a.file, "w", newline="", encoding="utf-8-sig") as f:
            f.writelines(good)
        print(f"Backup: {a.file}.bak -- removed {len(bad)} damaged lines. Now re-run get_municipality_quotes.py")
    elif bad:
        print("Run again with --fix to remove them.")


if __name__ == "__main__":
    main()