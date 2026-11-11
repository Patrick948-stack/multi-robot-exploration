"""Grid simulator for communication-constrained multi-robot exploration.

SKELETON ONLY. Every function below is a stub that raises NotImplementedError.
The comments say what each piece must do; section numbers refer to
docs/simulation-plan.md.

Conventions used throughout:
- A cell is a (row, col) tuple. Neighbors are up/down/left/right only.
- Time is counted in integer steps, starting at t = 0.
- Robots are indexed 0 .. N-1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------
# Cell states (shared by the true map and every robot's own map)
# ---------------------------------------------------------------------------

UNKNOWN = -1  # only ever appears in robot maps, never in the true map
OPEN = 0
WALL = 1

SENSING_RADIUS = 5        # cells, straight-line distance (plan section 3)
OBSTACLE_DENSITY = 0.20   # main value; 0.10 and 0.30 later (Decisions)
OPEN_CELLS_PER_ROBOT = 300
STEP_CAP_FACTOR = 4       # run stops at 4 x F steps if not finished (section 6)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrueMap:
    """Ground truth, held by the simulator only. Robots never read it (section 2).

    grid:     2D int array of OPEN / WALL. No UNKNOWN cells.
    entrance: the cell robots start near.
    n_open:   F, the number of open cells (all mutually reachable).
    """

    grid: np.ndarray
    entrance: tuple[int, int]
    n_open: int


@dataclass
class RobotState:
    """What one robot knows and where it is (sections 3-4).

    own_map: 2D int array, same shape as the true map, holding
             UNKNOWN / OPEN / WALL. Starts all UNKNOWN. Changes only through
             the robot's own sensing or a group merge. A known cell never
             goes back to UNKNOWN.
    position: current cell.
    goal:     current target frontier cell, or None if no frontier is known.
    rng:      this robot's own random stream, used only for tie-breaking
              between equally near frontiers (section 4).
    """

    own_map: np.ndarray
    position: tuple[int, int]
    goal: tuple[int, int] | None
    rng: np.random.Generator


@dataclass
class Timestamps:
    """Newest update time each robot holds about each teammate (section 7).

    u: N x N int array. u[i, j] is the newest timestamp from robot j that
       robot i holds, including relayed ones. All entries start at 0.
       A robot's own entry u[i, i] is always the current step.
       Information age is a[i, j] = t - u[i, j].
    """

    u: np.ndarray


@dataclass
class TeamRecord:
    """Simulator-side record of what the team as a whole has seen (section 6).

    Robots never read this. It is used for coverage, stopping, and deciding
    whether a discovery is repeated.

    first_seen_step: 2D int array, same shape as the true map. The step at
                     which any robot first observed the cell, or -1 if no
                     robot has observed it yet. "Observed by any robot" is
                     simply first_seen_step >= 0.
    """

    first_seen_step: np.ndarray


@dataclass
class StepMetrics:
    """One row of step-by-step output (section 7). Filled in by the main loop."""

    t: int
    coverage: float                 # C(t), team-wide union / F
    wasted_effort: int              # W(t), running total
    same_step_duplicates: int       # running total, kept out of W
    mean_degree: float
    lambda_2: float
    connected: bool
    mean_information_age: float
    new_meetings: int


@dataclass
class TrialResult:
    """Everything one trial produces."""

    steps: list[StepMetrics] = field(default_factory=list)
    stop_reason: str = ""                          # "full_coverage" or "step_cap"
    first_step_at: dict[str, int | None] = field(default_factory=dict)  # "90", "99", "100"; None = not reached


# ---------------------------------------------------------------------------
# Map generation (section 2)
# ---------------------------------------------------------------------------

def generate_map(n_robots: int, obstacle_density: float, rng: np.random.Generator) -> TrueMap:
    """Build one random map for one trial.

    - Target about OPEN_CELLS_PER_ROBOT * n_robots open cells; walls are added
      on top, so the grid is larger than that.
    - Place wall blocks at random at the given density.
    - Reject and redraw if any open cell cannot reach every other open cell
      by up/down/left/right moves.
    - Choose the entrance and record the actual open-cell count F.
    - Same rules at every team size.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Sensing (section 3)
# ---------------------------------------------------------------------------

def sense(true_map: TrueMap, position: tuple[int, int]) -> set[tuple[int, int]]:
    """Return every cell the robot can see from its position.

    - Candidates: all cells within SENSING_RADIUS straight-line distance.
    - Line of sight by Bresenham's algorithm from the robot to each cell.
    - Walls block the view, but the first wall along a line is itself seen.
    - Corner-touching lines follow one fixed rule (to be chosen and documented).
    - Error-free: returned cells carry their true OPEN / WALL state.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Frontiers and goal choice (section 4)
# ---------------------------------------------------------------------------

def find_frontiers(own_map: np.ndarray) -> set[tuple[int, int]]:
    """Return all frontier cells on one robot's map.

    A frontier is a known OPEN cell with at least one UNKNOWN up/down/left/right
    neighbor. Cells outside the grid do not count as unknown.
    """
    raise NotImplementedError


def choose_goal(robot: RobotState) -> tuple[int, int] | None:
    """Pick the nearest reachable frontier on the robot's own map.

    - BFS from the robot's position over known OPEN cells only.
    - Ties between equally near frontiers: broken with robot.rng.
    - Ties between equal-length paths to the chosen goal: one fixed rule.
    - No coordination with other robots; several may pick the same frontier.
    - Return None if no frontier is reachable (robot stays still this step).
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Communication (section 5)
# ---------------------------------------------------------------------------

def compute_links(positions: list[tuple[int, int]], r_c: float) -> np.ndarray:
    """Return the N x N boolean adjacency matrix of direct links.

    - Robots i != j are linked when straight-line distance < r_c (strictly).
    - r_c = 0: no links at all, even for robots in the same cell.
    - r_c = inf: every pair linked.
    - Walls do not block links.
    """
    raise NotImplementedError


def compute_groups(adjacency: np.ndarray) -> list[list[int]]:
    """Split robots into connected groups (direct links or chains of links).

    Every robot is in exactly one group; an isolated robot is a group of one.
    """
    raise NotImplementedError


def merge_maps(maps: list[np.ndarray]) -> np.ndarray:
    """Combine the maps of one group into a single map.

    - A known cell replaces an UNKNOWN cell.
    - If two maps disagree about a known cell, raise an error and stop the run
      (this should be impossible with error-free sensing).
    - Result must not depend on the order of `maps`, and merging a map with
      itself must change nothing.
    """
    raise NotImplementedError


def merge_timestamps(timestamps: Timestamps, group: list[int], t: int) -> None:
    """Share teammate timestamps within one group (in place).

    - Every robot in the group gets the newest u[., j] held by anyone in it.
    - Robots in the same group are current with each other: u[i, j] = t.
    - Relayed updates keep their original timestamp; they are never reset to t.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Wasted effort (section 7)
# ---------------------------------------------------------------------------

def count_discoveries(
    newly_seen: list[set[tuple[int, int]]],
    team: TeamRecord,
    t: int,
) -> tuple[int, int]:
    """Count repeated discoveries and same-step duplicates for one step.

    Must run after sensing and BEFORE any sharing.

    newly_seen[i] holds the OPEN cells robot i observed with its own sensor
    this step that were UNKNOWN on its own map just before sensing.

    Repeated discovery (adds 1 to W): a cell in newly_seen[i] that some
    robot had already observed on an earlier step (first_seen_step < t).
    Each robot-cell pair counts once.

    Same-step duplicates (kept out of W): for each cell first observed by the
    team at step t, (number of robots newly observing it this step) - 1.

    Returns (repeated_discoveries, same_step_duplicates). Updating
    team.first_seen_step for brand-new cells happens here too.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# One trial
# ---------------------------------------------------------------------------

def run_trial(n_robots: int, r_c: float, trial_seed: int) -> TrialResult:
    """Run one exploration trial and return its measurements."""

    # --- Setup -------------------------------------------------------------
    # Split trial_seed into separate streams: map, start cells, and one
    # tie-breaking stream per robot (section 10). Paired trials at other radii
    # reuse exactly the same streams.
    # Generate the map. Place robots on distinct open cells within 2 cells of
    # the entrance, enlarging the zone until it holds at least N cells
    # (definition of "within 2 cells" is still an open item).
    # Create each robot's all-UNKNOWN map, the N x N timestamps (all 0),
    # and the team record (all -1).

    # --- t = 0 ---------------------------------------------------------------
    # All robots sense. Build the network, groups share maps and timestamps.
    # Record the first measurements. Every robot chooses its first goal.
    # Links present now do not count as meetings.

    # --- Each later step t = 1, 2, ... ----------------------------------------
    # (1) Move: all robots move at once, one cell toward their goal along the
    #     planned path, or stay still if they have no goal.
    # (2) Sense: every robot senses from its new position and adds what it
    #     sees to its own map.
    # (3) Count discoveries BEFORE sharing: call count_discoveries to get new
    #     observations, repeated discoveries (W) and same-step duplicates.
    # (4) Links and groups: compute_links, then compute_groups. Any link that
    #     was absent on the previous step is a meeting.
    # (5) Share: within each group, merge_maps and give every member the
    #     combined map; merge_timestamps for the group.
    # (6) Update information ages a[i, j] = t - u[i, j] and record this step's
    #     metrics: coverage, W, duplicates, mean degree, lambda_2 (Laplacian
    #     L = D - A), connected status, mean information age, meetings.
    #     Note the first step coverage reaches 90%, 99% and 100%.
    # (7) Stop check, using the team record only: stop if every open cell has
    #     been observed ("full_coverage") or t reaches STEP_CAP_FACTOR * F
    #     ("step_cap"). Targets never reached are recorded as None.
    # (8) Choose new goals: a robot picks again when it reached its goal, its
    #     goal is no longer a frontier on its updated map, or it had none.

    raise NotImplementedError
