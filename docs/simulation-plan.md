# Simulation plan: communication-constrained multi-robot exploration

Owner: Patrick Mulikuza (Lehigh). Advisor: Prof. Hsuan-Wei Lee.
Source of truth: the email "Research update and simulation plan for your feedback" (sent early Nov 2026), plus decisions and clarifications logged below. If this file and a later decision disagree, the "Decisions and changes" section wins.

## Research question
How does the communication network's connectivity over time determine how much exploration effort is wasted?

Hypothesis (Prof. Lee, Oct 13): snapshot measures (lambda_2) will not fully explain performance, while temporal measures (meeting frequency, information age) will. Watch-out: greedy nearest-frontier robots with shared maps may clump. That is a finding, not a bug. Wording: simulations "show", not "prove".

## 1. Setup
- Grid world, time counted in steps. Each step every robot moves one cell or stays still.
- Steps, not seconds: seconds would pick up how robots physically move (turning, controller delays), unrelated to what they know. Faigl and Kulich (2015, Sec. VI-C) show two strategies with equal distance (3.9 m) but different times (85 s vs 75 s). Steps are a stand-in; no claims about seconds.
- Trial: one exploration run on one random map. Setting: one pairing of team size N and communication radius r_c.
- N in {1, 2, 4, 8, 16, 32}. r_c (cells) in {0, 3, 6, 12, unlimited}.
- At least 100 trials per setting. Seed fixes random choices so a trial replays exactly.
- Paired comparisons: for each N, trial k uses the same map, start cells and per-robot seeds at every radius. Only r_c changes.

## 2. Map
- Open ground with randomly placed wall blocks. A simplified obstacle world, not a validated model of disaster zones.
- New random map every trial (Faigl and Kulich Sec. II, IV-C).
- Obstacle share: fixed before collecting results and recorded. See Decisions (20%).
- Connectivity: all open cells must be reachable from each other by up/down/left/right moves. Maps that fail are rejected.
- Size: target 300 open cells per robot (total open cells = 300 x N). Obstacles are added on top, so the grid is larger than 300N. Record actual open cells F.
- Why scale with team: keeps area per robot roughly constant so team size is separate from crowding (MEF-Explore Sec. VI-2 scales team with area).
- Same generation rules at every team size.
- Ground truth: the simulator stores the full map. Robots know only what they sense or receive.
- Caveat: because the map grows with the team, comparisons across N reflect both team size and map size.

## 3. Robots
- Each robot keeps its own map (unknown / open / wall).
- Exact position in a shared coordinate system. No sensing or movement errors.
- Sensing: at start and after every move, observe all cells within 5 cells straight-line distance (a circle, radius 5). Walls block the view; the first wall along a line of sight is visible. Line of sight via Bresenham's algorithm with a fixed rule for corner-touching lines.
- Why simple sensing: Explore-Bench (Sec. V-B) skips raw laser simulation and uses Bresenham for speed. Thousands of trials are needed. Error-free sensing means differences come from communication.
- r_c = 3 is shorter than sensing range; 6 and 12 are longer. Experimental choices, not hardware specs.
- Start positions: robots start on different open cells near one entrance, within 2 cells of it. If that area has fewer than N open cells, enlarge it until it has at least N and record its final size. Same rule every trial. (Open: define "within 2 cells". See Open items.)
- Movement: one cell up/down/left/right, or stay. Steps equal distance traveled.
- No collisions: robots may share a cell and pass through each other after the start. Starts are on distinct cells.

## 4. How robots choose where to go
- Frontier: a known open cell sharing an edge with an unknown cell. Cells outside the map are not unknown. Only up/down/left/right neighbors count (Faigl and Kulich Sec. IV-B).
- Goal: nearest reachable frontier on the robot's own map by BFS over known open cells.
- Ties: broken by that robot's own random stream; equal-length path ties by a fixed rule.
- Independent choices: no reservation or region splitting, so several robots may pick the same frontier. Greedy = pick the best-looking option now, no planning ahead or coordination.
- Why so simple: duplication should depend only on what robots know. Explore-Bench (Sec. VI-D2) shows methods that push robots apart overlap less.
- Choose again when the goal is reached, or when the goal stops being a frontier on the robot's updated map (immediate replanning, Faigl and Kulich Sec. VI-A).
- Check run: repeat the experiment with robots choosing again only after they arrive, even if the goal stops being a frontier on the way. Reason: greedy did worse under immediate replanning as robots were added in Faigl and Kulich (goal flipping).
- No frontier available: the robot stays still and checks again after later sensing and communication.

## 5. Communication
- Link: two robots are directly linked when straight-line distance is strictly less than r_c. At r_c = 0 nobody communicates, even in the same cell. Unlimited range: every pair linked. Walls do not block links.
- Group: robots connected by direct links or chains of links.
- No separate position channel (MEF-Explore has one; this plan does not). Connectivity must be the only way information moves. Robots in separate groups exchange nothing.
- Sharing: each step every robot in a group gets the group's combined map. A known cell replaces an unknown cell. If maps disagree about a cell, the run stops with an error (should be impossible).
- Information crosses a whole group within one step. No bandwidth limit, delay, loss or energy cost (MEF-Explore Sec. II-C: connected groups end up with identical maps).
- t = 0: robots sense, build the network, groups share maps and timestamps, record first measurements, choose first goals.
- Each later step: (1) all robots move at once using current plans; (2) all sense; (3) identify new observations and repeated discoveries, before any sharing; (4) work out links and groups; (5) each group shares maps and timestamps; (6) update information ages and record results; (7) check whether the run should end; (8) robots choose new goals where needed.

## 6. When a run ends
- Two stopping conditions only: (1) the team has collectively observed every reachable open cell (100%), or (2) the step cap of 4 x (number of open cells). The cap is my choice; finishing before it is not guaranteed.
- Stopping uses the simulator's full record (true map plus the set of cells at least one robot has observed). Robots never see it, and no robot has to know the team is done.
- Record the first step at which coverage reaches 90%, 99% and 100%. These are timestamps, not stopping rules; the run continues past 90% and 99%. Explore-Bench (Sec. IV-A) uses 90% and 99%; the 100% mark is my addition.
- A target not reached is recorded as "not reached", never as the step limit.

## 7. What is tracked
Exploration
- Coverage C(t): fraction of reachable open cells observed by at least one robot by step t. Team-wide, union of all robots' observations divided by F.
- Wasted effort W(t): running count of repeated discoveries. A repeated discovery is when (a) a robot observes an open cell with its own sensor, (b) that cell was unknown in the robot's map just before sensing, and (c) another robot had already observed that cell on an earlier step. Each robot-cell observation counts once.
- W does not measure all wasted travel, energy or overlap, and does not prove sharing would have prevented it.
- Same-step duplicates: for each cell first observed by the team, (number of robots newly observing it that step) minus 1. Kept out of W.
- Total movement (steps summed across robots). Run duration.

Network (every step)
- Mean degree: average direct links per robot.
- lambda_2: second-smallest eigenvalue of the graph Laplacian L = D - A. 0 when split, positive when connected. Not comparable across N; compare within N and also report lambda_2 / N.
- Connected status: whether every robot can reach every other via current links. Per trial, the fraction of steps connected.

Information age
- a_ij(t) = t - u_ij(t), where u_ij(t) is the newest timestamp from robot j that robot i holds (including relayed ones). Every timestamp starts at 0 (bookkeeping, not a map exchange). Same-group robots get current updates, so age 0. Relayed updates keep the original timestamp. Record the average over ordered pairs each step. This is the age of teammate updates, not of map cells.

Meetings
- A meeting is the step a direct link forms between two robots not linked on the previous step. Links present at step 0 do not count.
- Per trial: total meetings, meetings per pair per 100 steps, times between a pair's successive meetings, share of pairs with fewer than two meetings.
- A new link is not necessarily a new exchange of information.
- Information age, lambda_2 and meeting measures are additions of this study, not taken from the papers. For N = 1 they are "not applicable"; mean degree and W are 0.

## 8. Checks before collecting results
r_c = 0 (independent robots)
- Each robot's path matches a solo run with the same map, start, tie-break seed and replanning rule over the same steps.
- Team coverage equals the union of solo coverage over that time.
- For N >= 2: mean degree, lambda_2, connected fraction and meetings are all 0. Information age rises with elapsed time.

r_c = unlimited (one shared map)
- All maps identical after every sharing phase. Mean degree N - 1, lambda_2 = N for N >= 2, always connected.
- Information age 0 after sharing; W is 0. Same-step duplicates can still occur. Meetings are 0.

Sharing and timestamps
- Merging a map with itself changes nothing; merge order and grouping do not matter (MEF-Explore App. A). Known cells never become unknown. Every robot in a group has the same map after sharing. Relayed updates keep their original timestamp. In a three-robot chain, end robots exchange information within one sharing phase.
- If any check fails, fix the simulator before collecting results.

## 9. Analysis
- Main figure: final W/F against r_c, one line per team size, unlimited shown as its own category.
- Also: coverage over time, steps to 90/99/100%, share of trials reaching each target, total movement, run duration.
- Key question: does information age predict wasted effort better than lambda_2? Per trial, average information age, mean degree, lambda_2 and connected status over the period used to measure W. Compare by rank correlation, R^2 and held-out prediction (main basis). Same fitting procedure, splits and outcome definition. Run within each setting, within each N across radii, and across all settings (accounting for N and r_c). Constant measures give "not applicable".
- Prof. Lee's note: for N = 2, lambda_2 time average is exactly 2 x the connected fraction, so run the information-age vs lambda_2 comparison for N >= 4.
- Windowed analysis: fixed-length windows chosen before analysis; test whether network conditions and information age in one window predict extra W in the next, accounting for N, r_c and starting coverage. Hold off until the pilot has been reviewed with Prof. Lee.
- Report whole-run results alongside comparisons at the 90% milestone, and always show completion rates.
- These are associations, not proof of cause. Paired comparisons across radii estimate the effect of changing r_c within this simulator.
- Report medians with 25th and 75th percentiles. Uncertainty by block resampling (a block = results sharing a map and starts). Held-out tests keep same-map results in the same group.
- Keep a log of unexpected behavior, failed runs and implementation changes; mark changes made after seeing results.

## 10. Reproducibility and tools
- One recorded master seed gives separate streams for maps, starts and each robot's tie-breaking. Paired trials reuse map, starts and seeds across radii and replanning rules.
- Trials run as separate processes; seeds assigned before scheduling so order does not matter.
- Python, NumPy, SciPy, matplotlib, pandas, pytest.
- Save: source, software versions, configs, master and trial seeds, maps, starting cells, stopping reasons, trial summaries, step-by-step measurements.
- Automated tests: movement, sensing, frontier detection, map sharing, timestamps, W counting, and the two communication extremes.

## Decisions and changes
- Obstacle density: main value 20%, later robustness check at 10% and 30%. Not yet stated in the email. Rationale: density changes map shape (open space vs corridors/dead ends), which can change meeting rates; high density causes more reachability rejections, which could bias which maps are kept (inference). Xue et al. 2026 used 10/20/30% (see literature log), which shows only what others have used.
- Prof. Lee's reply (Nov 8): plan is much stronger; stop refining and start building. Next steps from him: (1) core simulator on small maps with N in {1, 2, 4} and r_c = 0 and unlimited only, all sanity checks passing, animate a single run; (2) pilot of the full design, about 20 trials per setting, check N = 32 runtime (about 9,600 open cells, 4F cap may be slow in Python); (3) main figure plus a few coverage curves; (4) hold off on windowed and held-out analyses until the pilot is reviewed; (5) put code in a GitHub repo and share it, plot results as figures.

## Open items
- Define "within 2 cells" of the entrance: straight-line, walking steps, or square (in open ground the first two give the same 13-cell diamond; a square gives 25).
- Ask Prof. Lee to confirm 20% obstacle density and sensing radius 5.
- Decide whether to add the "obstacles added on top of 300N open cells" wording to the plan.
- Handout URL in the email uses b191; only the b211 version was confirmed to open.

## Clarifications log (Nov 8)
- Starts are on distinct cells; the zone is enlarged to hold at least N; robots may overlap after the start.
- Main plan replans immediately when a goal stops being a frontier; the check run is the only place robots keep walking to an already-explored goal.
- 90/99/100% are timestamps, not stopping rules. Stopping is 100% or the step cap.
- C(t) counts each cell once across the team.
- Explore-Bench has no "wasted time" metric; its overlap ratio r_o = (sum of S_i - S_total) / S_total is an end-of-run area measure, unlike the running W.
