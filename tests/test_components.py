"""Unit tests for the simulator's building blocks (plan section 10):
map generation, movement, sensing, frontier detection, goal choice,
links, and wasted-effort counting.
"""

import math

import numpy as np

import simulator as sim


def open_field(rows, cols):
    grid = np.zeros((rows, cols), dtype=np.int8)
    return sim.TrueMap(grid=grid, entrance=(rows // 2, 0), n_open=rows * cols)


def cells(true_map, flat):
    w = true_map.shape[1]
    return {divmod(int(x), w) for x in flat}


# ---------------------------------------------------------------------------
# Map generation and starts
# ---------------------------------------------------------------------------

def test_generated_map_is_connected_and_sized():
    rng = np.random.default_rng(3)
    for n in (1, 4):
        m = sim.generate_map(n, 0.2, rng, open_cells_per_robot=100)
        open_mask = m.grid == sim.OPEN
        dist = sim._walking_distances(m.grid, m.entrance)
        assert (dist[open_mask] >= 0).all()                 # every open cell reachable
        assert m.n_open == open_mask.sum()
        assert m.n_open >= 100 * n - 8                      # about target, walls on top
        wall_share = 1 - m.n_open / m.grid.size
        assert 0.15 < wall_share < 0.25


def test_same_seed_gives_same_map():
    a = sim.make_trial_inputs(5, 4, 2, open_cells_per_robot=80)
    b = sim.make_trial_inputs(5, 4, 2, open_cells_per_robot=80)
    assert np.array_equal(a[0].grid, b[0].grid)
    assert a[1] == b[1]


def test_start_cells_distinct_and_near_entrance():
    m = sim.generate_map(32, 0.2, np.random.default_rng(1), open_cells_per_robot=60)
    starts, radius = sim.choose_start_cells(m, 32, np.random.default_rng(2))
    assert len(set(starts)) == 32
    assert radius >= sim.START_ZONE_RADIUS
    dist = sim._walking_distances(m.grid, m.entrance)
    assert all(0 <= dist[s] <= radius for s in starts)


# ---------------------------------------------------------------------------
# Movement
# ---------------------------------------------------------------------------

def test_robots_move_at_most_one_cell_per_step_onto_open_cells():
    true_map, starts, seeds, _ = sim.make_trial_inputs(4, 4, 0, open_cells_per_robot=60)
    res = sim.simulate(true_map, starts, seeds, r_c=6, record=True)
    pos = np.array(res.history["positions"])
    step = np.abs(np.diff(pos, axis=0)).sum(axis=2)
    assert step.max() <= 1
    assert (true_map.grid[pos[..., 0], pos[..., 1]] == sim.OPEN).all()
    assert res.total_movement == step.sum()


# ---------------------------------------------------------------------------
# Sensing
# ---------------------------------------------------------------------------

def test_sensing_in_open_field_is_a_full_circle():
    m = open_field(21, 21)
    seen = cells(m, sim.Sensor(m, 5).visible((10, 10)))
    expected = {(r, c) for r in range(21) for c in range(21) if (r - 10) ** 2 + (c - 10) ** 2 <= 25}
    assert seen == expected


def test_sensing_is_clipped_at_grid_edge():
    m = open_field(11, 11)
    seen = cells(m, sim.Sensor(m, 5).visible((0, 0)))
    assert all(0 <= r < 11 and 0 <= c < 11 for r, c in seen)
    assert (5, 0) in seen and (0, 5) in seen


def test_wall_blocks_view_but_first_wall_is_seen():
    m = open_field(11, 11)
    m.grid[5, 7] = sim.WALL
    seen = cells(m, sim.Sensor(m, 5).visible((5, 5)))
    assert (5, 7) in seen          # the wall itself
    assert (5, 8) not in seen      # cells behind it
    assert (5, 10) not in seen


def test_diagonal_view_blocked_between_two_corner_walls():
    m = open_field(5, 5)
    m.grid[1, 2] = sim.WALL
    m.grid[2, 1] = sim.WALL
    seen = cells(m, sim.Sensor(m, 5).visible((2, 2)))
    assert (1, 1) not in seen      # squeezed between two walls
    m.grid[2, 1] = sim.OPEN
    seen = cells(m, sim.Sensor(m, 5).visible((2, 2)))
    assert (1, 1) in seen          # one wall alone does not block a diagonal


# ---------------------------------------------------------------------------
# Frontiers and goal choice
# ---------------------------------------------------------------------------

U, O, X = sim.UNKNOWN, sim.OPEN, sim.WALL


def test_frontier_detection():
    own = np.array([
        [O, O, U],
        [O, X, U],
        [O, O, O],
    ], dtype=np.int8)
    expected = np.array([
        [False, True, False],
        [False, False, False],
        [False, False, True],
    ])
    assert np.array_equal(sim.find_frontiers(own), expected)


def test_grid_edge_does_not_make_a_frontier():
    own = np.full((3, 3), O, dtype=np.int8)
    assert not sim.find_frontiers(own).any()


def test_goal_is_nearest_frontier_by_walking_distance():
    own = np.array([
        [O, O, O, O, U],
        [O, X, X, X, X],
        [O, O, U, X, X],
    ], dtype=np.int8)
    goal, path = sim.choose_goal(own.ravel(), 0, own.shape, np.random.default_rng(0))
    w = own.shape[1]
    # (2, 1) is 3 steps away, (0, 3) is also 3 steps away: a tie.
    assert divmod(goal, w) in {(2, 1), (0, 3)}
    assert len(path) == 3 and path[-1] == goal


def test_goal_ties_use_the_robot_stream_deterministically():
    own = np.array([[U, O, O, O, U]], dtype=np.int8)
    picks = {sim.choose_goal(own.ravel(), 2, own.shape, np.random.default_rng(s))[0] for s in range(30)}
    assert picks == {1, 3}
    a = sim.choose_goal(own.ravel(), 2, own.shape, np.random.default_rng(7))
    b = sim.choose_goal(own.ravel(), 2, own.shape, np.random.default_rng(7))
    assert a == b


def test_no_frontier_means_no_goal():
    own = np.full((3, 3), O, dtype=np.int8)
    assert sim.choose_goal(own.ravel(), 4, own.shape, np.random.default_rng(0)) == (None, [])


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def test_links_use_strict_distance():
    adj = sim.compute_links([(0, 0), (0, 3)], r_c=3)
    assert not adj[0, 1]                       # exactly r_c apart: not linked
    adj = sim.compute_links([(0, 0), (0, 3)], r_c=3.01)
    assert adj[0, 1] and adj[1, 0]


def test_rc_zero_links_nobody_even_in_same_cell():
    assert not sim.compute_links([(2, 2), (2, 2)], r_c=0).any()


def test_unlimited_links_everybody():
    adj = sim.compute_links([(0, 0), (50, 50), (99, 0)], r_c=math.inf)
    assert adj.sum() == 6


def test_algebraic_connectivity_known_values():
    assert sim.algebraic_connectivity(np.zeros((3, 3), dtype=bool)) == 0
    complete = ~np.eye(4, dtype=bool)
    assert math.isclose(sim.algebraic_connectivity(complete), 4)


# ---------------------------------------------------------------------------
# Wasted effort
# ---------------------------------------------------------------------------

def test_count_discoveries_separates_repeats_from_same_step_duplicates():
    first_seen = np.full(10, -1, dtype=np.int64)
    first_seen[3] = 2                  # cell 3 was seen by the team at step 2
    newly = [np.array([3, 5]), np.array([5, 6]), np.array([3])]
    found = sim.count_discoveries(newly, first_seen, t=4)
    assert found.repeated == 2                 # robots 0 and 2 rediscover cell 3
    assert found.same_step_duplicates == 1     # cell 5 found by two robots at once
    assert found.new_team_cells == 2           # cells 5 and 6
    assert first_seen[5] == 4 and first_seen[6] == 4
    assert first_seen[3] == 2                  # unchanged
