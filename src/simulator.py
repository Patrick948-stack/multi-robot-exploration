"""Grid simulator for communication-constrained multi-robot exploration.

Section numbers in comments refer to docs/simulation-plan.md.

Conventions used throughout:
- A cell is a (row, col) tuple, or a flat index row * width + col internally.
- Neighbors are up/down/left/right only.
- Time is counted in integer steps, starting at t = 0.
- Robots are indexed 0 .. N-1.

Choices made where the plan leaves room (each is marked "CHOICE" below):
- Walls are placed as random rectangular blocks (1 to 3 cells per side). A
  block that would cut off any open cell from the rest is rejected and a new
  one is drawn, so every finished map is fully connected.
- The entrance is the middle cell of the left edge, and the cells within
  3 walking steps of it are kept free of walls so robots have room to start.
- "Within 2 cells of the entrance" is measured in walking steps.
- Line of sight on a diagonal step is blocked when both cells beside the
  corner are walls (or outside the grid).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np
from scipy import ndimage
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components


# ---------------------------------------------------------------------------
# Cell states and fixed settings
# ---------------------------------------------------------------------------

UNKNOWN = -1  # only ever appears in robot maps, never in the true map
OPEN = 0
WALL = 1
_OUTSIDE = 2  # padding value used inside the sensor only

SENSING_RADIUS = 5        # cells, straight-line distance (section 3)
OBSTACLE_DENSITY = 0.20   # main value; 0.10 and 0.30 later (Decisions)
OPEN_CELLS_PER_ROBOT = 300
STEP_CAP_FACTOR = 4       # run stops at 4 x F steps if not finished (section 6)
START_ZONE_RADIUS = 2     # walking steps from the entrance (section 3)
ENTRANCE_CLEAR_RADIUS = 3  # CHOICE: no walls this close to the entrance
MAX_BLOCK_SIDE = 3        # CHOICE: wall blocks are 1x1 up to 3x3

REPLAN_RULES = ("immediate", "on_arrival")
MILESTONES = (90, 99, 100)

_FOUR_CONNECTED = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])


class MapDisagreementError(RuntimeError):
    """Two robots hold different known values for the same cell (section 5).

    This should be impossible with error-free sensing; if it happens the run
    stops so the bug can be found.
    """


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrueMap:
    """Ground truth, held by the simulator only. Robots never read it (section 2).

    grid:     2D int8 array of OPEN / WALL. No UNKNOWN cells.
    entrance: the cell robots start near.
    n_open:   F, the number of open cells (all mutually reachable).
    """

    grid: np.ndarray
    entrance: tuple[int, int]
    n_open: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.grid.shape


@dataclass
class DiscoveryCounts:
    """What count_discoveries found on one step."""

    repeated: int                 # adds to W
    same_step_duplicates: int     # kept out of W
    new_team_cells: int           # open cells the team saw for the first time
    repeated_cells: np.ndarray    # flat indices of repeated discoveries (with repeats)


@dataclass
class TrialResult:
    """Everything one simulated trial produces."""

    n_robots: int
    r_c: float
    n_open: int
    stop_reason: str                       # "full_coverage" or "step_cap"
    final_step: int                        # last step t that was run
    steps: dict[str, list]                 # one list per measurement, one entry per step
    first_step_at: dict[int, int | None]   # 90 / 99 / 100 -> step, None = not reached
    wasted_effort: int
    same_step_duplicates: int
    wasted_effort_at_90: int | None
    total_movement: int
    meetings: list[tuple[int, int, int]]   # (t, i, j) with i < j
    first_seen: np.ndarray                 # flat: step each cell was first seen by the team, -1 if never
    history: dict | None = None            # only filled when record=True


STEP_COLUMNS = (
    "t",
    "coverage",
    "wasted_effort",
    "same_step_duplicates",
    "new_team_cells",
    "mean_degree",
    "lambda_2",
    "connected",
    "n_groups",
    "mean_information_age",
    "new_meetings",
    "moving_robots",
)


# ---------------------------------------------------------------------------
# Map generation (section 2)
# ---------------------------------------------------------------------------

def generate_map(
    n_robots: int,
    obstacle_density: float = OBSTACLE_DENSITY,
    rng: np.random.Generator | None = None,
    open_cells_per_robot: int = OPEN_CELLS_PER_ROBOT,
) -> TrueMap:
    """Build one random, fully connected map for one trial.

    The grid is a square sized so that, after walls are added on top, about
    open_cells_per_robot * n_robots cells stay open. The same rules apply at
    every team size. The actual open-cell count F is recorded on the map.
    """
    if not 0 <= obstacle_density < 1:
        raise ValueError("obstacle_density must be in [0, 1)")
    if rng is None:
        rng = np.random.default_rng()

    target_open = open_cells_per_robot * n_robots
    side = math.ceil(math.sqrt(target_open / (1 - obstacle_density)))
    entrance = (side // 2, 0)
    rows, cols = np.indices((side, side))
    protected = (np.abs(rows - entrance[0]) + np.abs(cols - entrance[1])) <= ENTRANCE_CLEAR_RADIUS
    target_walls = round(obstacle_density * side * side)

    while True:
        grid = np.zeros((side, side), dtype=np.int8)
        walls = 0
        attempts = 0
        attempt_limit = 100 * max(target_walls, 1)
        while walls < target_walls and attempts < attempt_limit:
            attempts += 1
            h = int(rng.integers(1, MAX_BLOCK_SIDE + 1))
            w = int(rng.integers(1, MAX_BLOCK_SIDE + 1))
            r0 = int(rng.integers(0, side - h + 1))
            c0 = int(rng.integers(0, side - w + 1))
            region = (slice(r0, r0 + h), slice(c0, c0 + w))
            if protected[region].any():
                continue
            block = grid[region]          # a view into grid
            newly = block == OPEN
            added = int(newly.sum())
            if added == 0:
                continue
            block[newly] = WALL
            _, n_parts = ndimage.label(grid == OPEN, structure=_FOUR_CONNECTED)
            if n_parts != 1:              # would disconnect the open space: reject block
                block[newly] = OPEN
                continue
            walls += added
        if walls >= target_walls:
            break
        # Could not reach the density without disconnecting: reject whole map.

    return TrueMap(grid=grid, entrance=entrance, n_open=int((grid == OPEN).sum()))


def _walking_distances(grid: np.ndarray, source: tuple[int, int]) -> np.ndarray:
    """BFS walking distance from source over open cells; -1 where unreachable."""
    h, w = grid.shape
    dist = np.full((h, w), -1, dtype=np.int64)
    dist[source] = 0
    queue = deque([source])
    while queue:
        r, c = queue.popleft()
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= nr < h and 0 <= nc < w and grid[nr, nc] == OPEN and dist[nr, nc] < 0:
                dist[nr, nc] = dist[r, c] + 1
                queue.append((nr, nc))
    return dist


def choose_start_cells(
    true_map: TrueMap,
    n_robots: int,
    rng: np.random.Generator,
    radius: int = START_ZONE_RADIUS,
) -> tuple[list[tuple[int, int]], int]:
    """Place robots on distinct open cells near the entrance (section 3).

    The zone is every open cell within `radius` walking steps of the entrance.
    If it has fewer than N cells it grows one step at a time until it does.
    Returns the start cells and the final zone radius (to be recorded).
    """
    dist = _walking_distances(true_map.grid, true_map.entrance)
    while ((dist >= 0) & (dist <= radius)).sum() < n_robots:
        radius += 1
    zone = np.flatnonzero(((dist >= 0) & (dist <= radius)).ravel())
    chosen = rng.choice(zone, size=n_robots, replace=False)
    width = true_map.shape[1]
    return [tuple(int(v) for v in divmod(int(x), width)) for x in chosen], radius


# ---------------------------------------------------------------------------
# Sensing (section 3)
# ---------------------------------------------------------------------------

def bresenham(r1: int, c1: int) -> list[tuple[int, int]]:
    """Cells on Bresenham's line from (0, 0) to (r1, c1), both ends included."""
    dc, dr = abs(c1), -abs(r1)
    sc = 1 if c1 > 0 else -1
    sr = 1 if r1 > 0 else -1
    err = dc + dr
    r = c = 0
    cells = [(0, 0)]
    while (r, c) != (r1, c1):
        e2 = 2 * err
        if e2 >= dr:
            err += dr
            c += sc
        if e2 <= dc:
            err += dc
            r += sr
        cells.append((r, c))
    return cells


class Sensor:
    """Error-free range sensor with line of sight, cached per cell.

    The map never changes during a trial, so what is visible from a cell is
    worked out once and reused.
    """

    def __init__(self, true_map: TrueMap, radius: int = SENSING_RADIUS):
        self.radius = radius
        self.shape = true_map.shape
        h, w = self.shape
        self._padded = np.full((h + 2 * radius, w + 2 * radius), _OUTSIDE, dtype=np.int8)
        self._padded[radius:radius + h, radius:radius + w] = true_map.grid
        self._cache: dict[int, np.ndarray] = {}

        targets, mids, corners = [], [], []
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                if dr * dr + dc * dc > radius * radius:
                    continue
                line = bresenham(dr, dc)
                targets.append((dr, dc))
                mids.append(line[1:-1])
                pairs = []
                for (ar, ac), (br, bc) in zip(line, line[1:]):
                    if ar != br and ac != bc:          # diagonal step: check the corner
                        pairs.append(((ar, bc), (br, ac)))
                corners.append(pairs)
        k = len(targets)
        max_mid = max(1, max(len(m) for m in mids))
        max_cor = max(1, max(len(p) for p in corners))
        # Pad with the robot's own cell (0, 0), which is always open and never blocks.
        self._targets = np.array(targets, dtype=np.int64)
        self._mids = np.zeros((k, max_mid, 2), dtype=np.int64)
        self._corners = np.zeros((k, max_cor, 2, 2), dtype=np.int64)
        for i, m in enumerate(mids):
            if m:
                self._mids[i, :len(m)] = m
        for i, p in enumerate(corners):
            if p:
                self._corners[i, :len(p)] = p

    def visible(self, position: tuple[int, int]) -> np.ndarray:
        """Flat indices of every cell visible from position (own cell included).

        - Candidates are all cells within `radius` straight-line distance.
        - A wall or the grid edge on the line of sight blocks everything behind
          it, but the first wall along a line is itself seen.
        - CHOICE: on a diagonal step, the view is blocked when both cells beside
          the corner block.
        """
        h, w = self.shape
        key = position[0] * w + position[1]
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        pr, pc = position[0] + self.radius, position[1] + self.radius
        pad = self._padded
        mid_block = (pad[pr + self._mids[..., 0], pc + self._mids[..., 1]] != OPEN).any(axis=1)
        a = pad[pr + self._corners[:, :, 0, 0], pc + self._corners[:, :, 0, 1]] != OPEN
        b = pad[pr + self._corners[:, :, 1, 0], pc + self._corners[:, :, 1, 1]] != OPEN
        corner_block = (a & b).any(axis=1)
        target_vals = pad[pr + self._targets[:, 0], pc + self._targets[:, 1]]
        ok = ~mid_block & ~corner_block & (target_vals != _OUTSIDE)
        rows = position[0] + self._targets[ok, 0]
        cols = position[1] + self._targets[ok, 1]
        flat = np.sort(rows * w + cols)
        self._cache[key] = flat
        return flat


def sense(true_map: TrueMap, position: tuple[int, int], radius: int = SENSING_RADIUS) -> set[tuple[int, int]]:
    """Convenience wrapper: the set of (row, col) cells visible from position."""
    width = true_map.shape[1]
    return {tuple(int(v) for v in divmod(int(x), width)) for x in Sensor(true_map, radius).visible(position)}


# ---------------------------------------------------------------------------
# Frontiers and goal choice (section 4)
# ---------------------------------------------------------------------------

def find_frontiers(own_map: np.ndarray) -> np.ndarray:
    """Boolean mask of frontier cells on one robot's 2D map.

    A frontier is a known OPEN cell with at least one UNKNOWN up/down/left/right
    neighbor. Cells outside the grid do not count as unknown.
    """
    unknown = own_map == UNKNOWN
    has_unknown_neighbor = np.zeros_like(unknown)
    has_unknown_neighbor[1:, :] |= unknown[:-1, :]
    has_unknown_neighbor[:-1, :] |= unknown[1:, :]
    has_unknown_neighbor[:, 1:] |= unknown[:, :-1]
    has_unknown_neighbor[:, :-1] |= unknown[:, 1:]
    return (own_map == OPEN) & has_unknown_neighbor


def is_frontier(own_map_flat: np.ndarray, cell: int, shape: tuple[int, int]) -> bool:
    """Whether one flat cell is a frontier on a flat robot map."""
    if own_map_flat[cell] != OPEN:
        return False
    h, w = shape
    r, c = divmod(cell, w)
    return (
        (r > 0 and own_map_flat[cell - w] == UNKNOWN)
        or (r < h - 1 and own_map_flat[cell + w] == UNKNOWN)
        or (c > 0 and own_map_flat[cell - 1] == UNKNOWN)
        or (c < w - 1 and own_map_flat[cell + 1] == UNKNOWN)
    )


def choose_goal(
    own_map_flat: np.ndarray,
    position: int,
    shape: tuple[int, int],
    rng: np.random.Generator,
) -> tuple[int | None, list[int]]:
    """Pick the nearest reachable frontier on the robot's own map.

    - BFS from the robot's position over known OPEN cells only.
    - Ties between equally near frontiers are broken with the robot's own rng
      (only drawn from when there is a tie).
    - Ties between equal-length paths: fixed neighbor order up, down, left,
      right; the first cell to reach a square is its parent.
    - No coordination with other robots; several may pick the same frontier.

    Returns (goal, path), where path lists the cells to step through, ending
    at the goal. Returns (None, []) if no frontier is reachable.
    """
    h, w = shape
    open_b = (own_map_flat == OPEN).tobytes()
    frontier_b = find_frontiers(own_map_flat.reshape(shape)).ravel().tobytes()
    parent = {position: -1}
    level = [position]
    found: list[int] = []
    while level:
        found = [x for x in level if frontier_b[x]]
        if found:
            break
        nxt = []
        for x in level:
            r, c = divmod(x, w)
            if r > 0:
                y = x - w
                if open_b[y] and y not in parent:
                    parent[y] = x
                    nxt.append(y)
            if r < h - 1:
                y = x + w
                if open_b[y] and y not in parent:
                    parent[y] = x
                    nxt.append(y)
            if c > 0:
                y = x - 1
                if open_b[y] and y not in parent:
                    parent[y] = x
                    nxt.append(y)
            if c < w - 1:
                y = x + 1
                if open_b[y] and y not in parent:
                    parent[y] = x
                    nxt.append(y)
        level = nxt
    if not found:
        return None, []
    goal = found[0] if len(found) == 1 else found[int(rng.integers(len(found)))]
    path = []
    x = goal
    while x != position:
        path.append(x)
        x = parent[x]
    path.reverse()
    return goal, path


# ---------------------------------------------------------------------------
# Communication (section 5)
# ---------------------------------------------------------------------------

def compute_links(positions: np.ndarray | Sequence[tuple[int, int]], r_c: float) -> np.ndarray:
    """N x N boolean adjacency matrix of direct links.

    - Robots i != j are linked when straight-line distance < r_c (strictly).
    - r_c = 0: no links at all, even for robots in the same cell.
    - r_c = inf: every pair linked.
    - Walls do not block links.
    """
    p = np.asarray(positions, dtype=float).reshape(-1, 2)
    n = len(p)
    if r_c <= 0:
        return np.zeros((n, n), dtype=bool)
    d2 = ((p[:, None, :] - p[None, :, :]) ** 2).sum(axis=-1)
    adj = d2 < r_c ** 2
    np.fill_diagonal(adj, False)
    return adj


def compute_groups(adjacency: np.ndarray) -> list[np.ndarray]:
    """Connected groups (direct links or chains of links).

    Every robot is in exactly one group; an isolated robot is a group of one.
    """
    n_groups, labels = connected_components(csr_matrix(adjacency), directed=False)
    return [np.flatnonzero(labels == k) for k in range(n_groups)]


def merge_maps(maps: np.ndarray | Sequence[np.ndarray]) -> np.ndarray:
    """Combine the maps of one group into a single map.

    - A known cell replaces an UNKNOWN cell.
    - Two maps disagreeing about a known cell raises MapDisagreementError.
    - The result does not depend on the order of `maps`, and merging a map
      with itself changes nothing.
    """
    stack = np.asarray(maps)
    if stack.shape[0] == 1:
        return stack[0].copy()
    hi = stack.max(axis=0)
    lo = np.where(stack == UNKNOWN, WALL + 1, stack).min(axis=0)
    if ((lo <= WALL) & (lo != hi)).any():
        raise MapDisagreementError("robots disagree about a known cell")
    return hi


def merge_timestamps(u: np.ndarray, group: Sequence[int]) -> None:
    """Share teammate timestamps within one group (in place).

    u[i, j] is the newest timestamp from robot j that robot i holds. Every
    robot's own entry u[i, i] must already be set to the current step. After
    the merge every member holds the newest u[., j] known to anyone in the
    group, so same-group robots are current with each other and relayed
    updates keep their original timestamp.
    """
    g = np.asarray(group)
    u[g] = u[g].max(axis=0)


def algebraic_connectivity(adjacency: np.ndarray) -> float:
    """lambda_2: second-smallest eigenvalue of the Laplacian L = D - A."""
    n = len(adjacency)
    if n < 2:
        return float("nan")
    a = adjacency.astype(float)
    laplacian = np.diag(a.sum(axis=1)) - a
    return float(np.linalg.eigvalsh(laplacian)[1])


# ---------------------------------------------------------------------------
# Wasted effort (section 7)
# ---------------------------------------------------------------------------

def count_discoveries(
    newly_seen_open: Sequence[np.ndarray],
    first_seen: np.ndarray,
    t: int,
) -> DiscoveryCounts:
    """Count repeated discoveries and same-step duplicates for one step.

    Must run after sensing and BEFORE any sharing.

    newly_seen_open[i] holds the flat indices of OPEN cells robot i observed
    with its own sensor this step that were UNKNOWN on its own map just before
    sensing.

    - Repeated discovery (adds 1 to W): such a cell that some robot already
      observed on an earlier step (0 <= first_seen < t). Each robot-cell pair
      counts once, because the cell is known to that robot from then on.
    - Same-step duplicates (kept out of W): for each cell first observed by
      the team at step t, (number of robots newly observing it) - 1.

    Updates first_seen in place for cells the team saw for the first time.
    """
    if len(newly_seen_open) == 0:
        return DiscoveryCounts(0, 0, 0, np.empty(0, dtype=np.int64))
    cells = np.concatenate(newly_seen_open)
    if cells.size == 0:
        return DiscoveryCounts(0, 0, 0, np.empty(0, dtype=np.int64))
    seen_before = first_seen[cells]
    repeated_mask = (seen_before >= 0) & (seen_before < t)
    brand_new = cells[seen_before < 0]
    unique_new, counts = np.unique(brand_new, return_counts=True)
    first_seen[unique_new] = t
    return DiscoveryCounts(
        repeated=int(repeated_mask.sum()),
        same_step_duplicates=int((counts - 1).sum()),
        new_team_cells=int(unique_new.size),
        repeated_cells=cells[repeated_mask],
    )


# ---------------------------------------------------------------------------
# Seeds (section 10)
# ---------------------------------------------------------------------------

def trial_seeds(master_seed: int, n_robots: int, trial_index: int):
    """Separate seed streams for one (N, trial) pair.

    The streams depend only on the master seed, N and the trial index, never
    on r_c or the replanning rule, so trial k uses the same map, start cells
    and tie-breaking at every radius (paired comparisons, section 1).
    Returns (map_seed, start_seed, [robot_seed, ...]).
    """
    root = np.random.SeedSequence(master_seed, spawn_key=(n_robots, trial_index))
    map_seq, start_seq, robots_seq = root.spawn(3)
    return map_seq, start_seq, robots_seq.spawn(n_robots)


def make_trial_inputs(
    master_seed: int,
    n_robots: int,
    trial_index: int,
    obstacle_density: float = OBSTACLE_DENSITY,
    open_cells_per_robot: int = OPEN_CELLS_PER_ROBOT,
):
    """Map, start cells, per-robot seeds and start-zone radius for one trial."""
    map_seq, start_seq, robot_seqs = trial_seeds(master_seed, n_robots, trial_index)
    true_map = generate_map(n_robots, obstacle_density, np.random.default_rng(map_seq), open_cells_per_robot)
    starts, zone_radius = choose_start_cells(true_map, n_robots, np.random.default_rng(start_seq))
    return true_map, starts, robot_seqs, zone_radius


# ---------------------------------------------------------------------------
# One trial
# ---------------------------------------------------------------------------

def simulate(
    true_map: TrueMap,
    starts: Sequence[tuple[int, int]],
    robot_seeds: Sequence,
    r_c: float,
    replan: str = "immediate",
    sensing_radius: int = SENSING_RADIUS,
    step_cap_factor: int = STEP_CAP_FACTOR,
    record: bool = False,
    watch: int = 0,
    on_step: Callable[..., None] | None = None,
    on_frame: Callable[..., None] | None = None,
) -> TrialResult:
    """Run one exploration trial.

    replan: "immediate" (main plan: pick a new goal as soon as the current one
            stops being a frontier) or "on_arrival" (check run: only after
            arriving).
    record: keep positions, links, goals and repeated discoveries at each
            step, plus the own map of robot `watch`, for animation.
    on_step: optional callback on_step(t, maps, u, adjacency) called after the
            sharing phase each step; used by the tests.
    on_frame: optional observer called with the true map, robot positions, links,
              discovery records and current metrics after each step. Treat the
              arrays passed to the observer as read-only.
    """
    if replan not in REPLAN_RULES:
        raise ValueError(f"replan must be one of {REPLAN_RULES}")
    n = len(starts)
    if len(set(map(tuple, starts))) != n:
        raise ValueError("start cells must be distinct")
    h, w = true_map.shape
    shape = (h, w)
    truth = true_map.grid.ravel()
    f_open = true_map.n_open
    sensor = Sensor(true_map, sensing_radius)

    # --- Setup ---------------------------------------------------------------
    maps = np.full((n, h * w), UNKNOWN, dtype=np.int8)     # each robot's own map
    positions = [r * w + c for r, c in starts]
    rngs = [np.random.default_rng(s) for s in robot_seeds]
    goals: list[int | None] = [None] * n
    paths: list[deque] = [deque() for _ in range(n)]
    u = np.zeros((n, n), dtype=np.int64)                   # timestamps, all start at 0
    diag = np.arange(n)
    first_seen = np.full(h * w, -1, dtype=np.int64)        # team record

    steps = {k: [] for k in STEP_COLUMNS}
    covered = 0
    wasted = 0
    duplicates = 0
    movement = 0
    prev_adj = None
    meetings: list[tuple[int, int, int]] = []
    first_step_at: dict[int, int | None] = {m: None for m in MILESTONES}
    wasted_at_90 = None
    cap = step_cap_factor * f_open
    history = None
    if record:
        history = {"positions": [], "links": [], "goals": [], "repeated": [], "maps": []}

    t = 0
    while True:
        # (1) Move: all robots at once, one cell along their planned path.
        moving = 0
        if t > 0:
            for i in range(n):
                if paths[i]:
                    positions[i] = paths[i].popleft()
                    moving += 1
            movement += moving

        # (2) Sense: every robot adds what it sees to its own map.
        newly_open = []
        for i in range(n):
            vis = sensor.visible(divmod(positions[i], w))
            new = vis[maps[i, vis] == UNKNOWN]
            maps[i, new] = truth[new]
            newly_open.append(new[truth[new] == OPEN])
            # Walls count as observed in the team record but not toward W.
            new_walls = new[(truth[new] == WALL) & (first_seen[new] < 0)]
            first_seen[new_walls] = t

        # (3) Count new and repeated discoveries BEFORE sharing.
        found = count_discoveries(newly_open, first_seen, t)
        covered += found.new_team_cells
        wasted += found.repeated
        duplicates += found.same_step_duplicates

        # (4) Links and groups. A link absent on the previous step is a meeting
        #     (links already present at t = 0 do not count).
        pos_rc = np.array([divmod(p, w) for p in positions])
        adj = compute_links(pos_rc, r_c)
        groups = compute_groups(adj)
        new_meetings = 0
        if prev_adj is not None:
            formed = np.argwhere(np.triu(adj & ~prev_adj, k=1))
            new_meetings = len(formed)
            meetings.extend((t, int(i), int(j)) for i, j in formed)
        prev_adj = adj

        # (5) Share maps and timestamps within each group.
        u[diag, diag] = t
        for g in groups:
            if len(g) > 1:
                maps[g] = merge_maps(maps[g])
                merge_timestamps(u, g)
        if on_step is not None:
            on_step(t, maps, u, adj)

        # (6) Update information ages and record this step.
        steps["t"].append(t)
        steps["coverage"].append(covered / f_open)
        steps["wasted_effort"].append(wasted)
        steps["same_step_duplicates"].append(duplicates)
        steps["new_team_cells"].append(found.new_team_cells)
        steps["mean_degree"].append(float(adj.sum()) / n)
        steps["lambda_2"].append(algebraic_connectivity(adj))
        steps["connected"].append(len(groups) == 1 if n > 1 else float("nan"))
        steps["n_groups"].append(len(groups))
        steps["mean_information_age"].append(
            float((t - u).sum()) / (n * (n - 1)) if n > 1 else float("nan")
        )
        steps["new_meetings"].append(new_meetings)
        steps["moving_robots"].append(moving)
        if on_frame is not None:
            on_frame(true_map, pos_rc, adj, first_seen, found.repeated_cells,
                     {key: values[-1] for key, values in steps.items()})
        for m in MILESTONES:
            if first_step_at[m] is None and covered * 100 >= m * f_open:
                first_step_at[m] = t
                if m == 90:
                    wasted_at_90 = wasted
        if record:
            history["positions"].append(pos_rc.copy())
            history["links"].append(np.argwhere(np.triu(adj, k=1)))
            history["goals"].append([None if g is None else divmod(g, w) for g in goals])
            history["repeated"].append(found.repeated_cells.copy())
            history["maps"].append(maps[watch].copy())

        # (7) Stop check, using the simulator's own record only.
        if covered == f_open:
            stop_reason = "full_coverage"
            break
        if t >= cap:
            stop_reason = "step_cap"
            break

        # (8) Choose new goals where needed.
        for i in range(n):
            g = goals[i]
            need = g is None or positions[i] == g
            if not need and replan == "immediate":
                need = not is_frontier(maps[i], g, shape)
            if need:
                goals[i], path = choose_goal(maps[i], positions[i], shape, rngs[i])
                paths[i] = deque(path)
        t += 1

    if record:
        history["true_map"] = true_map
        history["starts"] = list(starts)
        history["sensing_radius"] = sensing_radius
        history["watch"] = watch

    return TrialResult(
        n_robots=n,
        r_c=r_c,
        n_open=f_open,
        stop_reason=stop_reason,
        final_step=t,
        steps=steps,
        first_step_at=first_step_at,
        wasted_effort=wasted,
        same_step_duplicates=duplicates,
        wasted_effort_at_90=wasted_at_90,
        total_movement=movement,
        meetings=meetings,
        first_seen=first_seen,
        history=history,
    )


# ---------------------------------------------------------------------------
# Per-trial summary and the worker entry point used by experiment.py
# ---------------------------------------------------------------------------

def summarize(result: TrialResult) -> dict:
    """One row of per-trial results (section 7). N = 1 network measures are NaN."""
    n = result.n_robots
    s = result.steps
    f_open = result.n_open
    nan = float("nan")
    row = {
        "F": f_open,
        "stop_reason": result.stop_reason,
        "steps": result.final_step,
        "final_coverage": s["coverage"][-1],
        "W": result.wasted_effort,
        "W_per_F": result.wasted_effort / f_open,
        "W90_per_F": nan if result.wasted_effort_at_90 is None else result.wasted_effort_at_90 / f_open,
        "same_step_duplicates": result.same_step_duplicates,
        "total_movement": result.total_movement,
    }
    for m in MILESTONES:
        v = result.first_step_at[m]
        row[f"t{m}"] = nan if v is None else v
    if n > 1:
        pairs = n * (n - 1) / 2
        lam = np.array(s["lambda_2"], dtype=float)
        by_pair: dict[tuple[int, int], list[int]] = {}
        for t, i, j in result.meetings:
            by_pair.setdefault((i, j), []).append(t)
        gaps = [b - a for times in by_pair.values() for a, b in zip(times, times[1:])]
        row.update(
            mean_degree=float(np.mean(s["mean_degree"])),
            lambda_2=float(lam.mean()),
            lambda_2_over_N=float(lam.mean()) / n,
            connected_fraction=float(np.mean(s["connected"])),
            information_age=float(np.mean(s["mean_information_age"])),
            meetings=len(result.meetings),
            meetings_per_pair_per_100=len(result.meetings) / pairs / max(result.final_step, 1) * 100,
            mean_time_between_meetings=float(np.mean(gaps)) if gaps else nan,
            share_pairs_under_2_meetings=sum(1 for i in range(n) for j in range(i + 1, n)
                                             if len(by_pair.get((i, j), [])) < 2) / pairs,
        )
    else:
        row.update(mean_degree=0.0, lambda_2=nan, lambda_2_over_N=nan, connected_fraction=nan,
                   information_age=nan, meetings=nan, meetings_per_pair_per_100=nan,
                   mean_time_between_meetings=nan, share_pairs_under_2_meetings=nan)
    return row


def run_trial(
    n_robots: int,
    r_c: float,
    trial_index: int,
    master_seed: int,
    obstacle_density: float = OBSTACLE_DENSITY,
    open_cells_per_robot: int = OPEN_CELLS_PER_ROBOT,
    replan: str = "immediate",
    sensing_radius: int = SENSING_RADIUS,
    step_cap_factor: int = STEP_CAP_FACTOR,
    return_map: bool = False,
    on_frame: Callable[..., None] | None = None,
) -> dict:
    """Run one trial from seeds and return plain, picklable results."""
    true_map, starts, robot_seeds, zone_radius = make_trial_inputs(
        master_seed, n_robots, trial_index, obstacle_density, open_cells_per_robot
    )
    result = simulate(true_map, starts, robot_seeds, r_c, replan, sensing_radius,
                      step_cap_factor, on_frame=on_frame)
    summary = {
        "N": n_robots,
        "r_c": r_c,
        "trial": trial_index,
        "replan": replan,
        "obstacle_density": obstacle_density,
        "grid_rows": true_map.shape[0],
        "grid_cols": true_map.shape[1],
        "start_zone_radius": zone_radius,
        "start_cells": ";".join(f"{r},{c}" for r, c in starts),
        **summarize(result),
    }
    out = {"summary": summary, "steps": result.steps}
    if return_map:
        out["map"] = {"grid": true_map.grid, "entrance": true_map.entrance, "starts": starts}
    return out
