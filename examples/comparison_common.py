"""Compare legacy CSV values while explicitly permitting new priority metadata."""
import pandas as pd


def assert_reference_requests_match(reference_path, current_path):
    reference = pd.read_csv(reference_path)
    current = pd.read_csv(current_path)
    additions = set(current.columns) - set(reference.columns)
    allowed = {"base_priority", "priority_class", "priority_source", "effective_priority", "priority_discount"}
    if not additions <= allowed:
        raise AssertionError(f"Unexpected added request columns: {additions - allowed}")
    pd.testing.assert_frame_equal(reference, current.loc[:, reference.columns], check_exact=True)
