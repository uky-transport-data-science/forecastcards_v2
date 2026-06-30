import os
import forecastcards


def validate_data_folder(data_folder, schema_locs=None, verbose=False):
    """
    Validate every project found in `data_folder` against the
    forecastcards schemas, using the existing Cardset machinery.

    Parameters
    ----------
    data_folder : str
        Folder containing one or more projects' CSVs (project-*.csv,
        poi-*.csv, observations-*.csv, forecast-*.csv). Cardset finds
        project*.csv files recursively under this folder and treats each
        one as the root of a project.
    schema_locs : dict, optional
        Dict of card_type -> schema URL/location. Defaults to the
        package's github_master_schema_loc (same default Cardset uses).
        Pass `forecastcards.Card_schema(...).schema_locs` here if you're
        validating against a customized schema.
    verbose : bool
        If True, print the full goodtables report for each failure
        (column-by-column errors) instead of just a one-line summary.

    Returns
    -------
    dict with keys:
        'valid'        : bool, True only if every discovered project passed
        'cardset'      : the underlying forecastcards.Cardset instance
        'validated'    : list of project_ids that passed validation
        'invalid'      : list of project_ids that failed validation
        'unvalidated'  : list of project_ids that were found but never
                          reached validation (e.g. missing a required
                          card type entirely)
    """
    kwargs = {"data_loc": data_folder, "validate": True}
    if schema_locs:
        kwargs["schema_locs"] = schema_locs

    cardset = forecastcards.Cardset(**kwargs)

    result = {
        "valid": len(cardset.invalid_projects) == 0
                 and len(cardset.unvalidated_projects) == 0
                 and len(cardset.validated_projects) > 0,
        "cardset": cardset,
        "validated": cardset.validated_projects,
        "invalid": cardset.invalid_projects,
        "unvalidated": cardset.unvalidated_projects,
    }

    _print_report(cardset, verbose=verbose)

    return result


def _print_report(cardset, verbose=False):
    print("=" * 60)
    print("DATA VALIDATION REPORT")
    print("=" * 60)

    n_total = len(cardset.projects)
    if n_total == 0:
        print("No project*.csv files were found. Check that your "
              "DATA_FOLDER actually contains project-*.csv files "
              "(directly, or nested in per-project subfolders).")
        return

    print(f"Found {n_total} project(s).\n")

    if cardset.validated_projects:
        print(f"PASSED ({len(cardset.validated_projects)}):")
        for pid in cardset.validated_projects:
            print(f"   - {pid}")
        print()

    if cardset.invalid_projects:
        print(f"FAILED ({len(cardset.invalid_projects)}):")
        for pid in cardset.invalid_projects:
            print(f"   - {pid}")
        print()
        print("   Failure details:")
        for report in cardset.failed_reports:
            for entry in report:
                _print_failure_entry(entry, verbose=verbose)
        print()

    if cardset.unvalidated_projects:
        print(f"INCOMPLETE / NOT VALIDATED ({len(cardset.unvalidated_projects)}):")
        print("   (these projects were found but are missing one or more "
              "required card types: project, poi, observations, forecast)")
        for pid in cardset.unvalidated_projects:
            print(f"   - {pid}")
        print()

    print("=" * 60)
    if cardset.validated_projects and not cardset.invalid_projects and not cardset.unvalidated_projects:
        print("All projects passed validation. Safe to proceed to "
              "missing_data_tools / Dataset.")
    else:
        print("Fix the issues above before relying on missing_data_tools "
              "or building a Dataset -- a structurally broken CSV "
              "(wrong columns, bad types) can silently produce bad "
              "results downstream even after blank cells are filled in.")
    print("=" * 60)


def _print_failure_entry(entry, verbose=False):
    # entries are either a goodtables report dict, or a plain string
    # (e.g. the "Start time isn't before end time" message from cardset.py)
    if isinstance(entry, str):
        print(f"      * {entry.splitlines()[0]}")
        return

    # goodtables report dict: {'valid': False, 'tables': [...], ...}
    tables = entry.get("tables", [])
    for table in tables:
        source = table.get("source", "(unknown file)")
        print(f"      * {os.path.basename(str(source))}")
        for err in table.get("errors", []):
            row = err.get("row-number")
            col = err.get("column-number")
            msg = err.get("message", "unspecified error")
            loc = f"row {row}, col {col}: " if row else ""
            print(f"          - {loc}{msg}")
            if not verbose:
                break  # just show the first error per file unless verbose
