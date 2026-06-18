"""
missing_data_tools.py
 
Helper functions for finding and fixing missing/placeholder values
("None", "unknown", blank cells) across the forecastcards CSV files:
project-*.csv, poi-*.csv, observations-*.csv, forecast-*.csv
 
All functions operate directly on CSV files in a single folder, and
write changes back to those same files in place.
"""
 
import os
import glob
import pandas as pd
 
# Values that count as "missing" when found in a cell (case-insensitive,
# whitespace-trimmed comparison). True NaN / empty cells are always
# treated as missing too.
MISSING_TOKENS = {"none", "unknown", "n/a", "na", "", "null"}
 
# Map of file-type -> the column that holds that file's primary ID.
# Used only for nicer display; not required for the scan to work.
ID_COLUMN_BY_TYPE = {
    "project": "project_id",
    "poi": "poi_id",
    "observations": "obs_id",
    "forecast": "forecast_id",
}
 
FILE_TYPES = ("project", "poi", "observations", "forecast")
 
# Discovery
 
def classify_file(filename):
    """Return the file type ('project', 'poi', 'observations', 'forecast')
    for a given filename, based on its prefix. Returns None if it doesn't
    match any known type.
    """
    base = os.path.basename(filename).lower()
    for ftype in FILE_TYPES:
        if base.startswith(ftype + "-") or base.startswith(ftype + "_"):
            return ftype
    return None
 
 
def find_data_files(data_folder):
    """Scan data_folder for all CSVs matching the four known file types.
 
    Returns a dict: {file_type: [list of full file paths]}
    """
    all_csvs = glob.glob(os.path.join(data_folder, "**", "*.csv"), recursive=True)
    found = {ftype: [] for ftype in FILE_TYPES}
    unmatched = []
 
    for path in all_csvs:
        ftype = classify_file(path)
        if ftype:
            found[ftype].append(path)
        else:
            unmatched.append(path)
 
    if unmatched:
        print(f"Note: {len(unmatched)} CSV file(s) did not match a known "
              f"file type (project/poi/observations/forecast) and were "
              f"skipped:")
        for u in unmatched:
            print(f"   - {os.path.basename(u)}")
 
    return found
 
# Detecting missing values
 
def is_missing(value):
    """True if a single cell value should be treated as missing."""
    if pd.isna(value):
        return True
    return str(value).strip().lower() in MISSING_TOKENS
 
 
def read_csv_robust(filepath, **kwargs):
    """Read a CSV trying UTF-8 first, falling back to cp1252 (Windows
    default) and then latin-1 (never fails) if that raises a decode
    error. Files saved/edited via Excel on Windows commonly contain
    cp1252 characters (e.g. en-dashes, smart quotes) that aren't valid
    UTF-8, which otherwise crashes pd.read_csv.
    """
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(filepath, encoding=encoding, **kwargs)
        except UnicodeDecodeError:
            continue
    # last resort: latin-1 never raises UnicodeDecodeError, so we should
    # never actually reach here, but just in case:
    return pd.read_csv(filepath, encoding="latin-1", **kwargs)
 
 
def scan_file_for_missing(filepath):
    """Read one CSV and return a list of missing-value records:
    [{"row": int, "column": str, "value": original_value, "id": row_id}, ...]
 
    "row" is the 0-indexed row number within the dataframe (not counting
    the header), which can be used to locate/update the cell later.
    """
    df = read_csv_robust(filepath, dtype=str, keep_default_na=True)
    ftype = classify_file(filepath)
    id_col = ID_COLUMN_BY_TYPE.get(ftype)
 
    missing = []
    for row_idx, row in df.iterrows():
        row_id = row[id_col] if id_col and id_col in df.columns else row_idx
        for col in df.columns:
            val = row[col]
            if is_missing(val):
                missing.append({
                    "row": row_idx,
                    "column": col,
                    "value": val,
                    "id": row_id,
                })
    return missing
 
 
def scan_folder(data_folder):
    """Scan every recognized CSV in data_folder for missing values.
 
    Returns a dict: {filepath: [list of missing-value records]}
    Only files with at least one missing value are included.
    """
    found_files = find_data_files(data_folder)
    results = {}
 
    for ftype, paths in found_files.items():
        for path in paths:
            missing = scan_file_for_missing(path)
            if missing:
                results[path] = missing
 
    return results
 
 
def print_scan_summary(scan_results):
    """Pretty-print the results of scan_folder()."""
    if not scan_results:
        print("No missing values found. Everything looks complete!")
        return
 
    total_cells = sum(len(v) for v in scan_results.values())
    print(f"Found {total_cells} missing value(s) across "
          f"{len(scan_results)} file(s):\n")
 
    for path, missing in scan_results.items():
        print(f"{os.path.basename(path)}  ({len(missing)} missing)")
        # Summarize by column so long lists don't scroll forever
        by_col = {}
        for m in missing:
            by_col.setdefault(m["column"], 0)
            by_col[m["column"]] += 1
        for col, count in by_col.items():
            print(f"   - {col}: {count} missing")
        print()
 
# Interactive fixing
 
def fix_missing_interactive(data_folder, file_filter=None):
    """Walk through every missing value found in data_folder and prompt
    the user to fill it in. Updates each CSV file in place immediately
    after every accepted answer (so progress is never lost).
 
    Parameters
    ----------
    data_folder : str
        Folder containing the CSVs.
    file_filter : str, optional
        If given, only process files whose name contains this substring
        (e.g. "40974" to focus on one project, or "project-" for just
        project files).
 
    During the prompt you can type:
      - a value to fill in that cell
      - 'skip' to leave this cell as-is and move to the next one
      - 'unknown' to explicitly mark it as unknown (kept as "unknown")
      - 'quit' to stop the whole session (progress so far is saved)
    """
    scan_results = scan_folder(data_folder)
 
    if file_filter:
        scan_results = {p: m for p, m in scan_results.items()
                         if file_filter in os.path.basename(p)}
 
    if not scan_results:
        print("No missing values found to fix.")
        return
 
    total = sum(len(v) for v in scan_results.values())
    print(f"Starting interactive fix session: {total} missing value(s) "
          f"to review.\n(Type 'skip' to leave a value for later, "
          f"'quit' to stop now.)\n")
 
    fixed_count = 0
    skipped_count = 0
 
    for path, missing_list in scan_results.items():
        # Re-read fresh each file so we always write back current state
        df = read_csv_robust(path, dtype=str, keep_default_na=True)
        file_changed = False
 
        for m in missing_list:
            row_idx, col, row_id = m["row"], m["column"], m["id"]
 
            # Re-check: the cell might have already been fixed earlier in
            # this same session if the same physical row was touched twice.
            current_val = df.at[row_idx, col]
            if not is_missing(current_val):
                continue
 
            print(f"--- {os.path.basename(path)} | row id: {row_id} | "
                  f"column: '{col}' ---")
            user_input = input(f"Enter value for '{col}' "
                                f"(or 'skip' / 'unknown' / 'quit'): ").strip()
 
            if user_input.lower() == "quit":
                print("\nStopping session early. Saving progress made so far...")
                if file_changed:
                    df.to_csv(path, index=False)
                    print(f"Saved updates to {os.path.basename(path)}")
                print(f"\nSession summary: {fixed_count} fixed, "
                      f"{skipped_count} skipped.")
                return
 
            if user_input.lower() == "skip" or user_input == "":
                skipped_count += 1
                print("Skipped.\n")
                continue
 
            # Accept the value as-is (including the user explicitly typing
            # "unknown" if that's their honest answer)
            df.at[row_idx, col] = user_input
            file_changed = True
            fixed_count += 1
            print("Saved.\n")
 
        if file_changed:
            df.to_csv(path, index=False)
            print(f"Updated file written: {os.path.basename(path)}\n")
 
    print(f"Session complete: {fixed_count} fixed, {skipped_count} skipped.")