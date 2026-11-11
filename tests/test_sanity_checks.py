"""Sanity checks from docs/simulation-plan.md section 8.

Names only for now. Each test is skipped until the simulator is written, so
pytest reports them as pending instead of passing by accident.
"""

import pytest

pending = pytest.mark.skip(reason="simulator not implemented yet")


# r_c = 0: independent robots
@pending
def test_rc_zero_matches_solo_runs():
    pass


# r_c = unlimited: one shared map
@pending
def test_rc_unlimited_gives_identical_maps():
    pass


# Sharing
@pending
def test_merge_order_does_not_change_result():
    pass


@pending
def test_known_cells_never_become_unknown():
    pass
