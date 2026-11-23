"""Algorithm 4 operator selection and maximization SA (D007)."""
from math import exp, isfinite
from types import MappingProxyType


OPERATORS = ('p_exchange', 'v_exchange', 'restart', 'insert', 'cut', 'swap', 'reverse')


class OperatorControl:
    """One batch owns its score vector; a later batch can inherit a copy."""

    def __init__(self, inherited=None, *, adaptive=True, initial_score=1.0,
                 theta=.1, psi=.4, omega=.02, floor=1e-9):
        if not all(isfinite(v) for v in (initial_score, theta, psi, omega, floor)):
            raise ValueError("Operator parameters must be finite")
        if min(floor, initial_score) <= 0 or min(theta, omega) < 0 or not 0 <= psi <= 1:
            raise ValueError("Invalid operator score parameters")
        self.adaptive = adaptive
        self.theta = theta
        self.psi = psi
        self.omega = omega
        self.floor = floor
        if inherited is None or not adaptive:
            inherited = dict.fromkeys(OPERATORS, initial_score)
        if set(inherited) != set(OPERATORS) or not all(isfinite(v) for v in inherited.values()):
            raise ValueError("Expected one finite score per operator")
        self._scores = {name: max(floor, inherited[name]) for name in OPERATORS}

    @property
    def scores(self):
        return MappingProxyType(self._scores.copy())

    def choose(self, rng):
        target = rng.random() * sum(self._scores.values())
        cumulative = 0.0
        for name, score in self._scores.items():
            cumulative += score
            if target < cumulative:
                return name
        return OPERATORS[-1]  # rounding at the upper endpoint

    def update(self, operator, old_best, best_after, rng):
        """Reward a new historical best once per completed outer loop, then apply Eq. 24."""
        if operator not in self._scores:
            raise ValueError("Unknown operator")
        if not self.adaptive:
            return False
        rewarded = best_after > old_best
        if rewarded:
            self._scores[operator] += self.theta
        for name, score in self._scores.items():
            if rng.random() < self.psi:
                self._scores[name] = max(self.floor, score + rng.uniform(-self.omega, self.omega))
        return rewarded


def accept_candidate(new, current, temperature, rng, *, feasible=True, annealing=True):
    """D007: compare a fully evaluated candidate with the current legal score."""
    if not feasible:
        return False
    if not all(isfinite(v) for v in (new, current, temperature)) or temperature < 0:
        raise ValueError("SA requires finite scores and a nonnegative finite temperature")
    if new >= current:
        return True
    if not annealing or not temperature:
        return False
    return rng.random() < exp((new - current) / temperature)
