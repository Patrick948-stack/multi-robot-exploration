<p align="center">
  <img src="img/mars-rover-mission-at-sunset.png" alt="Illustration of a team of rovers exploring a Martian landscape at sunset generated with OpenAI Images 2.5" width="800">
</p>

<p align="center"><sub><i>Illustrative artwork only, not output from the simulator.</i></sub></p>

# Multi-Robot Exploration: Communication and Wasted Effort

Imagine a team of robots sent into a building, a cave, the surface of new planet, or any other unexplored terrain with no map. Every robot explores on its own, but can only share what it's learned with the robots in the swarm learned when they get close enough to communicate. The goal is to map the terrain in the most efficient way possible. One problem is that, when it's out of range with its teammates, a robot can end up mapping parts of the terrain that another has already explored without realizing it. That duplicated work is what this project is about.

I'm studying how the *pattern* of communication over time (which robots can communicate among each other, and how often) shapes how much exploration effort gets wasted. Everything happens in grid simulations; there's no hardware involved.

This is undergraduate research at Lehigh University, supervised by Prof. Hsuan-Wei Lee.

## The question

**How does the communication network's connectivity over time determine how much exploration effort is wasted?**

The usual way to describe a network is a snapshot measure like algebraic connectivity (λ₂). We predict that a snapshot doesn't tell the whole story, and that temporal measures, such as how often robots actually meet and how stale their information about each other is, to explain wasted effort much better.

The simulations are meant to *show* whether that holds up, not to prove it.

## Where things stand

Planning is finished and I'm starting to build the simulator. There are no results yet. If you want every design decision and the reasoning behind it, read the full plan in [`docs/simulation-plan.md`](docs/simulation-plan.md).

## How the simulation works

<p align="center">
  <img src="img/simulation-illustration.png" alt="Illustration of five rovers on a rocky grid. Each has a colored sensing radius, four are linked by communication beams and share a grid map, and one purple rover is out of range on its own" width="800">
</p>

<p align="center"><sub><i>An artist's take on the setup, not a frame from the actual simulator. Robots within range link up and merge their maps, while the purple one is out of range and exploring on its own.</i></sub></p>

- **The world** is a grid with randomly placed wall blocks. Every trial gets a fresh random map, and every open cell can be reached from every other one. There are about 300 open cells per robot.
- **Time** moves in steps. On each step, every robot either moves one cell or stays put.
- **The robots** know their exact positions and sense perfectly within a radius of 5 cells, using Bresenham's line algorithm for line of sight. They don't collide, and they all start near a single entrance.
- **Decisions** are deliberately simple. Each robot heads for the nearest reachable frontier on its own map, with no coordination, and picks a new goal as soon as the current one stops being a frontier.
- **Communication** happens only when two robots are closer than a range r_c. Robots in the same connected group merge their maps every step. Nothing else passes information between them.
- **The sweep** covers team sizes N ∈ {1, 2, 4, 8, 16, 32} and communication ranges r_c ∈ {0, 3, 6, 12, unlimited}, with at least 100 trials per combination. Trials are paired across ranges, so the only thing that changes is r_c.
- **What gets measured:** coverage over time, wasted effort W (a robot rediscovering cells it had no way of knowing were already seen), mean degree, λ₂, connected fraction, information age, and meetings between robots.

Before trusting any results, the simulator has to pass two sanity checks. With r_c = 0, the robots should behave like completely independent explorers. With unlimited range, they should behave like one team sharing a single map.

The headline figure will plot wasted effort per open cell against r_c, with one line for each team size.

## What's in the repo

```text
configs/   experiment settings
docs/      simulation plan
figures/   plots made from results
img/       illustrative images (not experiment output)
results/   raw and summarized trial output
src/       simulator code
tests/     automated checks
```

## Tools

Python, NumPy, SciPy, matplotlib, pandas, and pytest.

## Setup

You'll need Python 3.10 or newer. From the project folder, create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Once the simulator code lands, you can run the automated checks with:

```bash
pytest
```

## Reproducibility

A single recorded master seed generates separate random streams for map generation, starting positions, and each robot's tie-breaking. Trials are independent and their seeds are assigned before anything is scheduled, so running them in a different order gives exactly the same results.

## Further reading

- [`docs/simulation-plan.md`](docs/simulation-plan.md): the full plan, the decisions behind it, and what's still open.

## License

Released under the MIT License. See [`LICENSE`](LICENSE).
