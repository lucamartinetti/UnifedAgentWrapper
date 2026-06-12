"""Uniform black-box conformance grader for the snake-game showdown.

Drives each workspace's `snake/logic.py` through the API contract both agents
were given, with identical checks:

    .venv/bin/python scripts/snake_conformance.py examples/workspace/snake/claude \
                                                  examples/workspace/snake/codex
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
VEC = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


class CheckFailure(AssertionError):
    pass


def load_game_class(ws: Path):
    """Import <ws>/snake/logic.py as package module, isolated per workspace."""
    for name in [m for m in sys.modules if m == "snake" or m.startswith("snake.")]:
        del sys.modules[name]
    sys.path.insert(0, str(ws))
    try:
        logic = importlib.import_module("snake.logic")
    finally:
        sys.path.remove(str(ws))
    return logic.SnakeGame


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckFailure(msg)


def in_bounds(cell, w, h) -> bool:
    x, y = cell
    return 0 <= x < w and 0 <= y < h


def invariants(g, w, h) -> None:
    expect(len(set(g.snake)) == len(g.snake), f"snake overlaps itself without game_over: {g.snake}")
    expect(all(in_bounds(c, w, h) for c in g.snake), f"snake out of bounds: {g.snake}")
    expect(in_bounds(tuple(g.food), w, h), f"food out of bounds: {g.food}")
    expect(tuple(g.food) not in {tuple(c) for c in g.snake}, f"food on snake: {g.food}")


class Driver:
    """Tracks the snake's actual direction so we never request a reversal."""

    def __init__(self, game, w: int, h: int):
        self.g, self.w, self.h = game, w, h
        self.direction = "right"
        self.steps = 0

    def step(self, direction: str) -> None:
        prev_head = tuple(self.g.snake[0])
        self.g.step(direction)
        self.steps += 1
        expect(self.steps < 2000, "grader stall: too many steps")
        if not self.g.game_over:
            head = tuple(self.g.snake[0])
            moved = (head[0] - prev_head[0], head[1] - prev_head[1])
            for d, v in VEC.items():
                if v == moved:
                    self.direction = d
            invariants(self.g, self.w, self.h)

    def safe_dirs(self):
        head = tuple(self.g.snake[0])
        body = {tuple(c) for c in self.g.snake[:-1]}  # tail cell vacates
        out = []
        for d, (dx, dy) in VEC.items():
            if d == OPPOSITE[self.direction]:
                continue
            target = (head[0] + dx, head[1] + dy)
            if in_bounds(target, self.w, self.h) and target not in body:
                out.append(d)
        return out

    def step_toward_food(self) -> None:
        head, food = tuple(self.g.snake[0]), tuple(self.g.food)
        prefs = []
        if food[0] > head[0]:
            prefs.append("right")
        elif food[0] < head[0]:
            prefs.append("left")
        if food[1] > head[1]:
            prefs.append("down")
        elif food[1] < head[1]:
            prefs.append("up")
        safe = self.safe_dirs()
        expect(bool(safe), "grader: no safe direction available")
        choice = next((d for d in prefs if d in safe), safe[0])
        self.step(choice)

    def eat(self, n: int) -> None:
        target = self.g.score + n
        while self.g.score < target:
            expect(not self.g.game_over, "game over while pathing to food")
            self.step_toward_food()


# --- checks -------------------------------------------------------------

def check_initial_state(SnakeGame):
    w, h = 30, 20
    g = SnakeGame(w, h, seed=7)
    cx, cy = w // 2, h // 2
    expect([tuple(c) for c in g.snake] == [(cx, cy), (cx - 1, cy), (cx - 2, cy)],
           f"initial snake wrong: {g.snake}")
    expect(g.score == 0, f"initial score {g.score}")
    expect(not g.game_over, "game_over at start")
    invariants(g, w, h)
    return "len-3 centered, body left of head, food free"


def check_movement_and_tail(SnakeGame):
    w, h = 30, 20
    g = SnakeGame(w, h, seed=7)
    d = Driver(g, w, h)
    old = [tuple(c) for c in g.snake]
    pre_score = g.score
    d.step("right")
    expect(tuple(g.snake[0]) == (old[0][0] + 1, old[0][1]), f"head didn't move right: {g.snake[0]}")
    if g.score == pre_score:  # didn't happen to eat
        expect(len(g.snake) == 3, f"length changed without eating: {len(g.snake)}")
        expect(tuple(g.snake[1]) == old[0], "tail didn't follow head")
    return "head advances, tail follows"


def check_reversal_ignored(SnakeGame):
    w, h = 30, 20
    g = SnakeGame(w, h, seed=7)
    head0 = tuple(g.snake[0])
    g.step("left")  # opposite of initial 'right' -> must be ignored
    expect(not g.game_over, "reversal caused game over")
    expect(tuple(g.snake[0]) == (head0[0] + 1, head0[1]),
           f"reversal not ignored: head {g.snake[0]}")
    return "reversal request keeps moving right"


def check_growth_and_score(SnakeGame):
    w, h = 30, 20
    g = SnakeGame(w, h, seed=11)
    d = Driver(g, w, h)
    base_len = len(g.snake)
    d.eat(3)
    expect(g.score == 3, f"score {g.score} != 3")
    expect(len(g.snake) == base_len + 3, f"length {len(g.snake)} != {base_len + 3}")
    return f"ate 3 foods in {d.steps} steps; length/score consistent"


def check_seed_determinism(SnakeGame):
    w, h = 30, 20
    a, b = SnakeGame(w, h, seed=123), SnakeGame(w, h, seed=123)
    expect(tuple(a.food) == tuple(b.food), f"same seed, different first food: {a.food} {b.food}")
    da = Driver(a, w, h)
    for _ in range(80):
        if a.game_over:
            break
        da.step_toward_food()
        b.step(da_last_move(da))
        expect([tuple(c) for c in a.snake] == [tuple(c) for c in b.snake],
               "same seed + same moves diverged (snake)")
        expect(tuple(a.food) == tuple(b.food), "same seed + same moves diverged (food)")
        expect(a.score == b.score, "same seed + same moves diverged (score)")
    expect(a.score >= 1, "determinism run never ate (grader inconclusive)")
    return f"identical move replay stayed identical for 80 steps (score {a.score})"


def da_last_move(driver: Driver) -> str:
    return driver.direction


def check_wall_collision(SnakeGame):
    w, h = 30, 20
    g = SnakeGame(w, h, seed=5)
    d = Driver(g, w, h)
    for _ in range(h + 2):
        if g.game_over:
            break
        d.step("up")
    expect(g.game_over, "no game_over after driving into top wall")
    snap = ([tuple(c) for c in g.snake], tuple(g.food), g.score)
    g.step("down")
    g.step("left")
    expect(snap == ([tuple(c) for c in g.snake], tuple(g.food), g.score),
           "step() after game_over mutated state")
    return "top wall kills; post-game_over steps are no-ops"


def check_self_collision(SnakeGame):
    w, h = 30, 20
    g = SnakeGame(w, h, seed=3)
    d = Driver(g, w, h)
    while len(g.snake) < 6:
        expect(not g.game_over, "died while growing for self-collision test")
        d.step_toward_food()
    # Move head to interior, then box maneuver: three same-way turns re-enter the body.
    turn_cw = {"right": "down", "down": "left", "left": "up", "up": "right"}
    for _ in range(200):
        head = tuple(g.snake[0])
        if 2 <= head[0] < w - 2 and 2 <= head[1] < h - 2:
            break
        expect(not g.game_over, "died seeking interior")
        safe = d.safe_dirs()
        interior = [x for x in safe if in_bounds(
            (head[0] + 3 * VEC[x][0], head[1] + 3 * VEC[x][1]), w, h)]
        d.step((interior or safe)[0])
    for _ in range(3):
        if g.game_over:
            break
        d.step(turn_cw[d.direction])
    expect(g.game_over, f"box maneuver did not self-collide (len {len(g.snake)})")
    return f"self-collision detected at length {len(g.snake)}"


CHECKS = [
    check_initial_state,
    check_movement_and_tail,
    check_reversal_ignored,
    check_growth_and_score,
    check_seed_determinism,
    check_wall_collision,
    check_self_collision,
]


def grade(ws: Path) -> tuple[int, int]:
    print(f"\n=== conformance: {ws} ===")
    try:
        SnakeGame = load_game_class(ws)
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] import snake.logic: {type(e).__name__}: {e}")
        return 0, len(CHECKS)
    passed = 0
    for check in CHECKS:
        name = check.__name__.removeprefix("check_")
        try:
            detail = check(SnakeGame)
            print(f"  [PASS] {name}: {detail}")
            passed += 1
        except CheckFailure as e:
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {name}: crashed: {type(e).__name__}: {e}")
    return passed, len(CHECKS)


def main(argv: list[str]) -> int:
    totals = {}
    for arg in argv:
        ws = Path(arg).resolve()
        totals[ws.name] = grade(ws)
    print("\n" + "=" * 60)
    for name, (p, t) in totals.items():
        print(f"{name}: {p}/{t} conformance checks passed")
    return 0 if all(p == t for p, t in totals.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
