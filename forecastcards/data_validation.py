"""
data_validation.py

A detailed, human-readable validator for forecastcards CSV data.
For each project it tells you EXACTLY what is wrong, organized into
four categories:

"""

import os
import glob
import csv
import json
import requests
import pandas as pd


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

SCHEMA_FILENAMES = {
    "project"      : "project-schema.json",
    "poi"          : "poi-schema.json",
    "observations" : "observations-schema.json",
    "forecast"     : "forecast-schema.json",
}

GITHUB_SCHEMA_URLS = {
    "project"      : "https://raw.githubusercontent.com/uky-transport-data-science/forecastcards_v2/master/spec/en/project-schema.json",
    "poi"          : "https://raw.githubusercontent.com/uky-transport-data-science/forecastcards_v2/master/spec/en/poi-schema.json",
    "observations" : "https://raw.githubusercontent.com/uky-transport-data-science/forecastcards_v2/master/spec/en/observations-schema.json",
    "forecast"     : "https://raw.githubusercontent.com/uky-transport-data-science/forecastcards_v2/master/spec/en/forecast-schema.json",
}

# Values that count as "missing" (case-insensitive, stripped)
MISSING_TOKENS = {"", "none", "unknown", "n/a", "na", "null", "nan"}

# How many bad rows to show per issue before saying "and N more..."
DEFAULT_MAX_ROWS_SHOWN = 3


def _load_schemas(local_schema_dir=None):
    """
    Return a dict of {card_type: schema_dict} where schema_dict is the
    parsed JSON from the schema file.  Uses local files if local_schema_dir
    is given, otherwise fetches from GitHub.
    """
    schemas = {}
    for card_type, filename in SCHEMA_FILENAMES.items():
        if local_schema_dir:
            path = os.path.join(local_schema_dir, filename)
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"Schema file not found: {path}\n"
                    f"Expected {filename} in {local_schema_dir}"
                )
            with open(path, "r", encoding="utf-8") as f:
                schemas[card_type] = json.load(f)
        else:
            url = GITHUB_SCHEMA_URLS[card_type]
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                schemas[card_type] = resp.json()
            except Exception as e:
                raise RuntimeError(
                    f"Could not fetch {card_type} schema from GitHub: {e}\n"
                    f"If you're offline, pass local_schema_dir= to "
                    f"validate_data_folder()."
                )
    return schemas


def _parse_schema(schema_dict):
    """
    Extract useful lookups from a raw schema dict:
      fields        - ordered list of field names
      required      - set of required field names
      enums         - {field_name: [allowed_values]}
      types         - {field_name: 'string'|'number'|'integer'|'date'|'time'}
    """
    fields   = []
    required = set()
    enums    = {}
    types    = {}

    for f in schema_dict.get("fields", []):
        name = f["name"]
        fields.append(name)
        types[name] = f.get("type", "string")
        c = f.get("constraints", {})
        if c.get("required"):
            required.add(name)
        if "enum" in c:
            enums[name] = c["enum"]

    return {
        "fields"  : fields,
        "required": required,
        "enums"   : enums,
        "types"   : types,
    }


# ---------------------------------------------------------------------------
# CSV discovery  (same logic as missing_data_tools / cardset)
# ---------------------------------------------------------------------------

FILE_TYPES = ("project", "poi", "observations", "forecast")


def _classify_file(filename):
    base = os.path.basename(filename).lower()
    for ftype in FILE_TYPES:
        if base.startswith(ftype + "-") or base.startswith(ftype + "_"):
            return ftype
    return None


def _clean_columns(df):
    """
    Normalize column headers so downstream `col in df.columns` / `df[col]`
    checks behave predictably no matter how messy the source CSV is:
      - strip whitespace (and stray BOM leftovers) from each header
      - de-duplicate exact-duplicate headers so df[col] always returns a
        Series (never a DataFrame), which is what caused KeyError-style
        crashes further down when a column name was accidentally repeated
    """
    new_cols = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    df = df.copy()
    df.columns = new_cols

    # If stripping revealed duplicate names, keep only the FIRST occurrence
    # of each and drop the rest, rather than letting df[col] return a
    # multi-column DataFrame later on.
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep="first")]

    return df.reset_index(drop=True)


def _read_csv(filepath):
    """Read a CSV trying UTF-8 (with BOM handling) → cp1252 → latin-1
    (handles Windows files), then normalizes column headers."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(filepath, dtype=str, keep_default_na=False,
                             encoding=enc)
            return _clean_columns(df)
        except UnicodeDecodeError:
            continue
    df = pd.read_csv(filepath, dtype=str, keep_default_na=False,
                     encoding="latin-1")
    return _clean_columns(df)


def _find_projects(data_folder):
    """
    Walk data_folder recursively looking for project-*.csv files.
    Each one is treated as the root of a project.
    Returns a list of dicts:
      {project_id, project_path, card_locs: {type: [paths]}}
    """
    projects = []
    for filepath in glob.iglob(
            os.path.join(data_folder, "**/project*.csv"), recursive=True):
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                rows = list(reader)
        except Exception:
            continue

        if not rows or rows[0][0].strip() != "project_id":
            continue

        project_id   = rows[1][0].strip().lower() if len(rows) > 1 else "(unknown)"
        project_path = os.path.dirname(filepath)

        card_locs = {
            "project"     : [filepath],
            "poi"         : glob.glob(os.path.join(project_path, "poi*.csv")),
            "observations": glob.glob(os.path.join(project_path, "**/observations*.csv"),
                                      recursive=True),
            "forecast"    : glob.glob(os.path.join(project_path, "**/forecast*.csv"),
                                      recursive=True),
        }
        projects.append({
            "project_id"  : project_id,
            "project_path": project_path,
            "card_locs"   : card_locs,
        })

    return projects


# ---------------------------------------------------------------------------
# Per-file checking  (the heart of the validator)
# ---------------------------------------------------------------------------

def _is_missing(value):
    return str(value).strip().lower() in MISSING_TOKENS


def _check_file(filepath, card_type, schema, max_rows=DEFAULT_MAX_ROWS_SHOWN):
    """
    Check one CSV file against its schema.
    Returns a dict of issues found:
      {
        "structure"      : [...strings...],
        "missing_data"   : [...strings...],
        "invalid_values" : [...strings...],
        "time_errors"    : [...strings...],
      }
    All lists are empty if no problems were found for that category.
    """
    issues = {
        "structure"      : [],
        "missing_data"   : [],
        "invalid_values" : [],
        "time_errors"    : [],
    }

    try:
        df = _read_csv(filepath)
    except Exception as e:
        issues["structure"].append(f"Could not read file: {e}")
        return issues

    fname = os.path.basename(filepath)

    # ---- 1. STRUCTURE: check columns ----
    schema_cols  = set(schema["fields"])
    csv_cols     = set(df.columns)
    required     = schema["required"]

    missing_cols = required - csv_cols          # required cols not in CSV at all
    unknown_cols = csv_cols - schema_cols       # cols in CSV not in schema

    for col in sorted(missing_cols):
        issues["structure"].append(
            f"Required column '{col}' is completely absent from the file "
            f"(the CSV has no column with that name)."
        )
    for col in sorted(unknown_cols):
        issues["structure"].append(
            f"Column '{col}' is not in the schema — check for a typo or "
            f"renamed column."
        )

    # Only continue deeper checks on columns that actually exist
    present_cols = [c for c in schema["fields"] if c in df.columns]

    # Everything below this point pokes at actual cell values. Wrap it so
    # that any unexpected data-quality surprise (weird dtype, stray
    # duplicate-looking column, malformed row, etc.) gets reported as a
    # structure issue for THIS file instead of crashing the entire
    # validate_data_folder() run and taking every other project down with it.
    try:
        _check_file_deep(df, card_type, schema, present_cols, required,
                         max_rows, issues)
    except Exception as e:
        issues["structure"].append(
            f"Could not fully validate this file's contents due to an "
            f"unexpected error: {type(e).__name__}: {e}. "
            f"This usually means a column has unexpected/mixed data — "
            f"double check the header row and cell values in {fname}."
        )

    return issues


def _check_file_deep(df, card_type, schema, present_cols, required,
                     max_rows, issues):
    """
    The value-level checks (missing data, invalid values, time errors).
    Split out from _check_file so it can be wrapped in a single try/except
    there without one giant try block swallowing the structure checks too.
    """
    # ---- 2. MISSING DATA: required fields with blank/placeholder values ----
    for col in present_cols:
        if col not in required:
            continue
        bad_rows = df.index[df[col].apply(_is_missing)].tolist()
        if bad_rows:
            shown = bad_rows[:max_rows]
            extra = len(bad_rows) - len(shown)
            row_list = ", ".join(str(r + 2) for r in shown)  # +2: header + 0-index
            suffix   = f" (and {extra} more)" if extra else ""
            issues["missing_data"].append(
                f"'{col}' is required but missing/blank in "
                f"{len(bad_rows)} row(s) — CSV rows: {row_list}{suffix}."
            )

    # ---- 3. INVALID VALUES: enum violations & type errors ----
    enums = schema["enums"]
    types = schema["types"]

    for col in present_cols:
        non_missing = df[df[col].apply(lambda v: not _is_missing(v))][col]

        # Enum check
        if col in enums:
            allowed  = enums[col]
            bad_mask = ~non_missing.isin(allowed)
            bad_vals = non_missing[bad_mask]
            if not bad_vals.empty:
                unique_bad = sorted(bad_vals.unique().tolist())
                rows_shown = bad_vals.index[:max_rows].tolist()
                extra      = len(bad_vals) - min(max_rows, len(bad_vals))
                row_list   = ", ".join(str(r + 2) for r in rows_shown)
                suffix     = f" (and {extra} more)" if extra > 0 else ""
                issues["invalid_values"].append(
                    f"'{col}' contains value(s) not in the allowed list.\n"
                    f"          Allowed: {allowed}\n"
                    f"          Found:   {unique_bad}\n"
                    f"          CSV rows: {row_list}{suffix}."
                )

        # Type check  (number / integer / date / time)
        col_type = types.get(col, "string")

        if col_type in ("number", "integer"):
            def _to_num(v):
                try:
                    float(v)
                    return True
                except (ValueError, TypeError):
                    return False
            bad_mask = ~non_missing.apply(_to_num)
            bad_vals = non_missing[bad_mask]
            if not bad_vals.empty:
                unique_bad = sorted(bad_vals.unique().tolist())[:5]
                rows_shown = bad_vals.index[:max_rows].tolist()
                extra      = len(bad_vals) - min(max_rows, len(bad_vals))
                row_list   = ", ".join(str(r + 2) for r in rows_shown)
                suffix     = f" (and {extra} more)" if extra > 0 else ""
                issues["invalid_values"].append(
                    f"'{col}' must be a {col_type} but has non-numeric "
                    f"value(s): {unique_bad} in CSV rows: {row_list}{suffix}."
                )

        elif col_type == "date":
            def _to_date(v):
                try:
                    pd.to_datetime(v)
                    return True
                except Exception:
                    return False
            bad_mask = ~non_missing.apply(_to_date)
            bad_vals = non_missing[bad_mask]
            if not bad_vals.empty:
                unique_bad = sorted(bad_vals.unique().tolist())[:5]
                rows_shown = bad_vals.index[:max_rows].tolist()
                extra      = len(bad_vals) - min(max_rows, len(bad_vals))
                row_list   = ", ".join(str(r + 2) for r in rows_shown)
                suffix     = f" (and {extra} more)" if extra > 0 else ""
                issues["invalid_values"].append(
                    f"'{col}' must be a date but can't be parsed: "
                    f"{unique_bad} in CSV rows: {row_list}{suffix}."
                )

        elif col_type == "time":
            def _to_time(v):
                # accept HH:MM, HH:MM:SS, and the common 24:00:00 edge case
                v2 = "23:59:59" if v in ("24:00:00", "24:00") else v
                try:
                    pd.to_datetime(v2)
                    return True
                except Exception:
                    return False
            bad_mask = ~non_missing.apply(_to_time)
            bad_vals = non_missing[bad_mask]
            if not bad_vals.empty:
                unique_bad = sorted(bad_vals.unique().tolist())[:5]
                rows_shown = bad_vals.index[:max_rows].tolist()
                extra      = len(bad_vals) - min(max_rows, len(bad_vals))
                row_list   = ", ".join(str(r + 2) for r in rows_shown)
                suffix     = f" (and {extra} more)" if extra > 0 else ""
                issues["invalid_values"].append(
                    f"'{col}' must be a time (HH:MM:SS) but can't be "
                    f"parsed: {unique_bad} in CSV rows: {row_list}{suffix}."
                )

    # ---- 4. TIME ERRORS: start_time must be before end_time ----
    if card_type in ("forecast", "observations"):
        if "start_time" in df.columns and "end_time" in df.columns:
            def _norm_time(v):
                v = str(v).strip()
                return "23:59:59" if v in ("24:00:00", "24:00") else v

            try:
                st = pd.to_datetime(df["start_time"].apply(_norm_time),
                                    errors="coerce")
                et = pd.to_datetime(df["end_time"].apply(_norm_time),
                                    errors="coerce")
                bad_mask = st >= et
                bad_rows = df.index[bad_mask].tolist()
                if bad_rows:
                    shown    = bad_rows[:max_rows]
                    extra    = len(bad_rows) - len(shown)
                    row_list = ", ".join(str(r + 2) for r in shown)
                    suffix   = f" (and {extra} more)" if extra else ""
                    issues["time_errors"].append(
                        f"start_time is NOT before end_time in "
                        f"{len(bad_rows)} row(s) — CSV rows: "
                        f"{row_list}{suffix}."
                    )
            except Exception as e:
                issues["time_errors"].append(
                    f"Could not compare start_time / end_time: {e}"
                )
    # (mutates issues in place — no return needed, _check_file returns it)


# ---------------------------------------------------------------------------
# Project-level validation
# ---------------------------------------------------------------------------

REQUIRED_CARD_TYPES = {"project", "poi", "observations", "forecast"}


def _validate_project(project_info, schemas, max_rows=DEFAULT_MAX_ROWS_SHOWN):
    """
    Validate all cards for one project.
    Returns a dict:
      {
        project_id : str,
        passed     : bool,
        missing_card_types : [str],      # required types with NO files at all
        file_issues: {filepath: {category: [issue_strings]}}
      }
    """
    pid      = project_info["project_id"]
    locs     = project_info["card_locs"]
    result   = {
        "project_id"        : pid,
        "passed"            : True,
        "missing_card_types": [],
        "file_issues"       : {},
    }

    # Check for entirely missing card types
    for ctype in REQUIRED_CARD_TYPES:
        if not locs.get(ctype):
            result["missing_card_types"].append(ctype)
            result["passed"] = False

    # Check each file that does exist
    for ctype, paths in locs.items():
        schema = _parse_schema(schemas[ctype])
        for path in paths:
            try:
                file_issues = _check_file(path, ctype, schema, max_rows=max_rows)
            except Exception as e:
                # Final safety net: a single file should NEVER be able to
                # crash validate_data_folder() for every other project.
                file_issues = {
                    "structure"      : [f"Unexpected error validating this "
                                        f"file: {type(e).__name__}: {e}"],
                    "missing_data"   : [],
                    "invalid_values" : [],
                    "time_errors"    : [],
                }
            any_issue = any(v for v in file_issues.values())
            if any_issue:
                result["file_issues"][path] = file_issues
                result["passed"] = False

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_CATEGORY_LABELS = {
    "structure"      : "STRUCTURE (wrong/missing columns)",
    "missing_data"   : "MISSING DATA (required fields are blank)",
    "invalid_values" : "INVALID VALUES (bad type or not in allowed list)",
    "time_errors"    : "TIME ERRORS (start_time not before end_time)",
}


def _format_project_report(project_result, data_folder):
    """Build the report text for ONE project as a list of lines (no printing)."""
    lines  = []
    pid    = project_result["project_id"]
    passed = project_result["passed"]

    status = "✓ PASSED" if passed else "✗ FAILED"
    lines.append(f"\n  [{status}]  {pid}")

    if project_result["missing_card_types"]:
        for ct in project_result["missing_card_types"]:
            lines.append(f"    ● MISSING FILE TYPE: no {ct}-*.csv file found "
                          f"for this project folder.")

    for filepath, categories in project_result["file_issues"].items():
        # Show path relative to data_folder for readability
        try:
            rel = os.path.relpath(filepath, data_folder)
        except ValueError:
            rel = filepath
        lines.append(f"\n    File: {rel}")

        for cat_key, label in _CATEGORY_LABELS.items():
            issues = categories.get(cat_key, [])
            if issues:
                lines.append(f"      ── {label}:")
                for issue in issues:
                    # indent multi-line issue strings neatly
                    sub_lines = issue.split("\n")
                    lines.append(f"         • {sub_lines[0]}")
                    for line in sub_lines[1:]:
                        lines.append(f"           {line}")

    return lines


def _build_full_report(all_results, data_folder):
    """Build the ENTIRE report (every project, in full) as one big string."""
    passed  = [r for r in all_results if r["passed"]]
    failed  = [r for r in all_results if not r["passed"]]
    n_total = len(all_results)

    lines = []
    lines.append("\n" + "=" * 70)
    lines.append("  FORECAST CARDS — DATA VALIDATION REPORT")
    lines.append("=" * 70)
    lines.append(f"  Projects found : {n_total}")
    lines.append(f"  Passed         : {len(passed)}")
    lines.append(f"  Failed         : {len(failed)}")
    lines.append("=" * 70)

    if not all_results:
        lines.append("\n  No project*.csv files were found in the data folder.")
        lines.append("  Check that DATA_FOLDER is set to the right path.")
        return "\n".join(lines)

    if failed:
        lines.append(f"\n{'─'*70}")
        lines.append(f"  FAILED PROJECTS ({len(failed)})")
        lines.append(f"{'─'*70}")
        for r in failed:
            lines.extend(_format_project_report(r, data_folder))

    if passed:
        lines.append(f"\n{'─'*70}")
        lines.append(f"  PASSED PROJECTS ({len(passed)})")
        lines.append(f"{'─'*70}")
        for r in passed:
            lines.extend(_format_project_report(r, data_folder))

    lines.append("\n" + "=" * 70)
    if not failed:
        lines.append("  All projects passed. Safe to proceed to missing_data_tools.")
    else:
        lines.append("  Fix the FAILED projects above before building a Dataset.")
        lines.append("  Tip: structure issues must be fixed in the CSV file itself.")
        lines.append("       Missing data issues can be fixed with missing_data_tools.")
    lines.append("=" * 70 + "\n")

    return "\n".join(lines)


def _print_full_report(all_results, data_folder, report_path=None):
    """
    Print the full report to the notebook AND (by default) save it to a
    .txt file. Notebooks (Jupyter/VS Code/Colab) often truncate very long
    cell output, which is why a report covering many projects can look
    like it stops after the first failure — the data was always there,
    the notebook just stopped rendering it. Writing the report to disk
    guarantees you can always see every project's issues in full.
    """
    report_text = _build_full_report(all_results, data_folder)
    print(report_text)

    if report_path is None:
        report_path = os.path.join(data_folder, "validation_report.txt")

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(f"Full report (all {len(all_results)} project(s), untruncated) "
              f"saved to:\n  {report_path}\n"
              f"Open this file if the notebook output above looks cut off.")
    except Exception as e:
        print(f"WARNING: could not save report to {report_path}: {e}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_data_folder(data_folder,
                         local_schema_dir=None,
                         verbose=False,
                         report_path=None):
    """
    Validate every project found in data_folder.

    Parameters
    ----------
    data_folder : str
        Folder containing project CSVs (searched recursively).
    local_schema_dir : str, optional
        Path to a folder containing the four schema JSON files.
        If not given, schemas are fetched from GitHub.
    verbose : bool
        If True, show all bad rows instead of the first 3 per issue.
    report_path : str, optional
        Where to save the full, untruncated text report. Defaults to
        "validation_report.txt" inside data_folder. Pass report_path=False
        to skip saving a file and only print.

    Returns
    -------
    dict with keys:
        'passed'   - list of project_id strings that passed
        'failed'   - list of project_id strings that failed
        'details'  - list of per-project result dicts (full detail)
        'valid'    - bool, True only if every project passed
        'report'   - the full report as a single string (every project)
    """
    max_rows = 9999 if verbose else DEFAULT_MAX_ROWS_SHOWN

    print("Loading schemas...")
    schemas = _load_schemas(local_schema_dir)
    print(f"Schemas loaded for: {', '.join(schemas.keys())}")

    print(f"\nScanning for projects in:\n  {data_folder}\n")
    projects = _find_projects(data_folder)

    if not projects:
        print("No project*.csv files found. Check your DATA_FOLDER path.")
        return {"passed": [], "failed": [], "details": [], "valid": False}

    print(f"Found {len(projects)} project(s). Validating...\n")

    all_results = []
    for p in projects:
        r = _validate_project(p, schemas, max_rows=max_rows)
        all_results.append(r)

    if report_path is False:
        report_text = _build_full_report(all_results, data_folder)
        print(report_text)
    else:
        _print_full_report(all_results, data_folder, report_path=report_path)

    passed = [r["project_id"] for r in all_results if     r["passed"]]
    failed = [r["project_id"] for r in all_results if not r["passed"]]

    return {
        "passed" : passed,
        "failed" : failed,
        "details": all_results,
        "valid"  : len(failed) == 0,
        "report" : _build_full_report(all_results, data_folder),
    }