"""Sanity checks from docs/simulation-plan.md section 8.

If any of these fail, fix the simulator before collecting results.
"""

import math

import numpy as np
import pytest

import simulator as sim

# Small maps keep the suite fast; the rules are the same at every size.
SMALL = 60


def trial(n_robots, trial_index=0, master_seed=11):
    return sim.make_trial_inputs(master_seed, n_robots, trial_index, open_cells_per_robot=SMALL)


def as_inf(first_seen):
    out = first_seen.astype(float)
    out[out < 0] = math.inf
    return out


# ---------------------------------------------------------------------------
# r_c = 0: independent robots
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trial_index", [0, 1, 2])
def test_rc_zero_matches_solo_runs(trial_index):
    true_map, starts, seeds, _ = trial(4, trial_index)
    team = sim.simulate(true_map, starts, seeds, r_c=0, record=True)
    team_pos = np.array(team.history["positions"])
    solo_seen = []
    for i in range(4):
        solo = sim.simulate(true_map, [starts[i]], [seeds[i]], r_c=0, record=True)
        solo_pos = np.array(solo.history["positions"])[:, 0]
        steps = min(len(team_pos), len(solo_pos))
        assert np.array_equal(team_pos[:steps, i], solo_pos[:steps])
        solo_seen.append(as_inf(solo.first_seen))
    # Team coverage equals the union of solo coverage over the same steps.
    union = np.minimum.reduce(solo_seen)
    union[union > team.final_step] = math.inf
    assert np.array_equal(as_inf(team.first_seen), union)


def test_rc_zero_network_measures_are_zero():
    true_map, starts, seeds, _ = trial(4)
    res = sim.simulate(true_map, starts, seeds, r_c=0)
    s = res.steps
    assert all(v == 0 for v in s["mean_degree"])
    assert all(v == 0 for v in s["lambda_2"])
    assert not any(s["connected"])
    assert res.meetings == []
    # Nobody ever hears from anyone, so information age equals elapsed time.
    assert s["mean_information_age"] == [float(t) for t in s["t"]]


# ---------------------------------------------------------------------------
# r_c = unlimited: one shared map
# ---------------------------------------------------------------------------

def test_rc_unlimited_gives_identical_maps():
    true_map, starts, seeds, _ = trial(4)

    def check(t, maps, u, adj):
        assert (maps == maps[0]).all()

    sim.simulate(true_map, starts, seeds, r_c=math.inf, on_step=check)


def test_rc_unlimited_network_measures():
    n = 4
    true_map, starts, seeds, _ = trial(n)
    res = sim.simulate(true_map, starts, seeds, r_c=math.inf)
    s = res.steps
    assert all(v == n - 1 for v in s["mean_degree"])
    assert np.allclose(s["lambda_2"], n)
    assert all(s["connected"])
    assert all(v == 0 for v in s["mean_information_age"])
    assert res.wasted_effort == 0
    assert res.meetings == []


# ---------------------------------------------------------------------------
# Sharing and timestamps
# ---------------------------------------------------------------------------

def random_partial_maps(rng, k=4, shape=(12, 12)):
    truth = rng.choice([sim.OPEN, sim.WALL], size=shape, p=[0.7, 0.3]).astype(np.int8)
    maps = []
    for _ in range(k):
        m = truth.copy()
        m[rng.random(shape) < 0.6] = sim.UNKNOWN
        maps.append(m)
    return maps


def test_merge_order_does_not_change_result():
    rng = np.random.default_rng(0)
    maps = random_partial_maps(rng)
    reference = sim.merge_maps(maps)
    for _ in range(10):
        order = rng.permutation(len(maps))
        assert np.array_equal(sim.merge_maps([maps[i] for i in order]), reference)
    # Merging in two stages (different grouping) gives the same answer too.
    staged = sim.merge_maps([sim.merge_maps(maps[:2]), sim.merge_maps(maps[2:])])
    assert np.array_equal(staged, reference)


def test_merge_with_itself_changes_nothing():
    m = random_partial_maps(np.random.default_rng(1), k=1)[0]
    assert np.array_equal(sim.merge_maps([m, m]), m)


def test_merge_disagreement_raises():
    a = np.array([[sim.OPEN, sim.UNKNOWN]], dtype=np.int8)
    b = np.array([[sim.WALL, sim.UNKNOWN]], dtype=np.int8)
    with pytest.raises(sim.MapDisagreementError):
        sim.merge_maps([a, b])


def test_known_cells_never_become_unknown():
    true_map, starts, seeds, _ = trial(4)
    previous = {}

    def check(t, maps, u, adj):
        if "maps" in previous:
            was_known = previous["maps"] != sim.UNKNOWN
            assert (maps[was_known] != sim.UNKNOWN).all()
        previous["maps"] = maps.copy()

    sim.simulate(true_map, starts, seeds, r_c=6, on_step=check)


def test_group_members_share_one_map_after_sharing():
    true_map, starts, seeds, _ = trial(6)

    def check(t, maps, u, adj):
        for g in sim.compute_groups(adj):
            assert (maps[g] == maps[g[0]]).all()

    sim.simulate(true_map, starts, seeds, r_c=6, on_step=check)


def test_relayed_updates_keep_original_timestamp():
    u = np.zeros((3, 3), dtype=np.int64)
    u[1, 2] = 5                       # robot 1 last heard from robot 2 at step 5
    t = 9
    u[np.arange(3), np.arange(3)] = t
    sim.merge_timestamps(u, [0, 1])   # robots 0 and 1 meet at step 9
    assert u[0, 2] == 5               # relayed, keeps its original time
    assert u[0, 1] == t and u[1, 0] == t


def test_three_robot_chain_shares_end_to_end_in_one_phase():
    positions = [(0, 0), (0, 3), (0, 6)]
    adj = sim.compute_links(positions, r_c=4)
    assert adj[0, 1] and adj[1, 2] and not adj[0, 2]
    groups = sim.compute_groups(adj)
    assert len(groups) == 1
    maps = np.full((3, 7), sim.UNKNOWN, dtype=np.int8)
    maps[2, 6] = sim.OPEN             # only the far-end robot knows this cell
    merged = sim.merge_maps(maps[groups[0]])
    assert merged[6] == sim.OPEN
    u = np.zeros((3, 3), dtype=np.int64)
    u[np.arange(3), np.arange(3)] = 4
    sim.merge_timestamps(u, groups[0])
    assert u[0, 2] == 4 and u[2, 0] == 4
