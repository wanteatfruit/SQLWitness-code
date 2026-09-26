"""
Early termination estimators for coverage-guided fuzzing.

Two estimators:
1. LaplaceTerminator - Uses Laplace Rule of Succession (patience-based)
2. GoodTuringTerminator - Uses Good-Turing frequency estimation (singleton-based)
"""

from abc import ABC, abstractmethod
from collections import Counter
from typing import Optional, Hashable
import math


class TerminatorBase(ABC):
    """Abstract base class for termination estimators."""
    
    @abstractmethod
    def update(self, *args, **kwargs) -> None:
        """Update the estimator with new observation."""
        pass
    
    @abstractmethod
    def should_terminate(self) -> bool:
        """Return True if fuzzing should stop."""
        pass
    
    @abstractmethod
    def get_current_risk(self) -> float:
        """Return current probability of finding new coverage."""
        pass
    
    @abstractmethod
    def get_stats(self) -> dict:
        """Return current statistics for logging."""
        pass


class LaplaceTerminator(TerminatorBase):
    """
    Termination controller using the Laplace Rule of Succession.
    
    Formula: P(new) = 1 / (k + 2)
    where k is the number of consecutive iterations without finding new coverage.
    
    Patience Limit: k_max = ceil(1/target_risk) - 2
    
    Best for: bounded inputs, simple queries, or when coverage signal is sparse.
    """
    
    def __init__(self, target_risk: float = 0.005):
        """
        Args:
            target_risk: Probability threshold below which we terminate (e.g., 0.005 = 0.5%)
        """
        if target_risk <= 0 or target_risk >= 1:
            raise ValueError("target_risk must be in (0, 1)")
        
        self.target_risk = target_risk
        
        # Patience limit: solve 1/(k+2) <= target_risk => k >= 1/target_risk - 2
        self.patience_limit = max(1, math.ceil(1.0 / target_risk) - 2)
        
        self.consecutive_boring_iters = 0
        self.total_iterations = 0
        self.total_new_coverage_events = 0
    
    def update(self, is_new_coverage: bool) -> None:
        """
        Update with observation from latest iteration.
        
        Args:
            is_new_coverage: True if this iteration discovered new coverage
        """
        self.total_iterations += 1
        
        if is_new_coverage:
            self.consecutive_boring_iters = 0
            self.total_new_coverage_events += 1
        else:
            self.consecutive_boring_iters += 1
    
    def should_terminate(self) -> bool:
        """
        Terminate if consecutive_boring_iters >= patience_limit
        """
        return self.consecutive_boring_iters >= self.patience_limit
    
    def get_current_risk(self) -> float:
        """
        Current probability of finding new coverage.
        
        P(new) = 1 / (k + 2) where k = consecutive_boring_iters
        """
        return 1.0 / (self.consecutive_boring_iters + 2)
    
    def get_stats(self) -> dict:
        return {
            'estimator': 'laplace',
            'total_iterations': self.total_iterations,
            'consecutive_boring': self.consecutive_boring_iters,
            'patience_limit': self.patience_limit,
            'current_risk': self.get_current_risk(),
            'target_risk': self.target_risk,
            'new_coverage_events': self.total_new_coverage_events
        }


class GoodTuringTerminator(TerminatorBase):
    """
    Termination controller using incidence-based Good-Turing estimation
    (the STADS residual-discovery estimator, Boehme 2018).

    Species  = a coverage class, i.e. a distinct (source, predicate_idx, 3-valued
               outcome) tuple from the fixed coverage universe.
    Draw / sample = one fuzzing iteration (one generated database instance).

    Formula: P(new) = Q1 / T
    where Q1 = number of coverage classes observed in exactly ONE iteration
              (incidence singletons),
          T  = number of iterations (draws) so far.

    This is the *incidence* form of Good-Turing: each class contributes at most
    once per iteration regardless of row multiplicity, so Q1 decays as iterations
    re-cover the same classes, driving P(new) -> 0. (An earlier version hashed the
    whole-instance coverage fingerprint into a single species; because instances
    grow with cardinality_boost those fingerprints were almost always unique, so
    Q1 ~ T and the estimator never decayed.)

    Best for: rich/diverse coverage signals with many distinct classes.

    Edge case: When Q1 = 0, falls back to a Laplace estimate to avoid premature
    termination.
    """

    def __init__(self, target_risk: float = 0.01):
        """
        Args:
            target_risk: Probability threshold below which we terminate
        """
        if target_risk <= 0 or target_risk >= 1:
            raise ValueError("target_risk must be in (0, 1)")

        self.target_risk = target_risk

        # Incidence count per coverage class: in how many iterations it appeared.
        self.incidence_counts: Counter = Counter()

        # Q1: number of classes with incidence exactly 1 (maintained incrementally).
        self.n1 = 0

        self.total_iterations = 0
        self.unique_signatures = 0  # distinct classes ever seen

    def update(self, species_in_sample=None) -> None:
        """
        Record one iteration (draw).

        O(|classes this iteration|) time, maintaining Q1 incrementally.

        Args:
            species_in_sample: iterable of hashable coverage classes observed in
                this iteration (e.g. the frozenset from coverage_to_signature).
                None or a bool denotes a draw that discovered no coverage class
                (saturated / no-coverage iteration) — it still counts as a draw
                but contributes no incidence.
        """
        self.total_iterations += 1

        # None / bool (legacy "no improvement" signal) => draw with no species.
        if species_in_sample is None or isinstance(species_in_sample, bool):
            return

        for sp in set(species_in_sample):
            old_count = self.incidence_counts[sp]
            self.incidence_counts[sp] += 1

            if old_count == 0:
                # First iteration containing this class -> incidence singleton.
                self.n1 += 1
                self.unique_signatures += 1
            elif old_count == 1:
                # Was an incidence singleton, now seen in a 2nd iteration.
                self.n1 -= 1

    def get_current_risk(self) -> float:
        """
        Good-Turing estimate: P(new) = Q1 / T

        Fallback to Laplace when Q1 = 0 to avoid premature termination:
        P(new) = 1 / (T + 1)
        """
        if self.total_iterations == 0:
            return 1.0  # Maximum uncertainty at start

        if self.n1 == 0:
            # Fallback to Laplace estimate
            return 1.0 / (self.total_iterations + 1)

        return self.n1 / self.total_iterations

    def should_terminate(self) -> bool:
        """
        Terminate if P(new) < target_risk
        """
        return self.get_current_risk() < self.target_risk

    def get_stats(self) -> dict:
        return {
            'estimator': 'good_turing',
            'total_iterations': self.total_iterations,
            'unique_signatures': self.unique_signatures,
            'singletons_n1': self.n1,
            'current_risk': self.get_current_risk(),
            'target_risk': self.target_risk,
            'using_fallback': self.n1 == 0 and self.total_iterations > 0
        }


def create_terminator(
    method: str = 'laplace',
    target_risk: float = 0.01,
) -> TerminatorBase:
    """
    Factory function to create a termination estimator.
    
    Args:
        method: 'laplace' or 'good_turing'
        target_risk: Probability threshold for termination
    
    Returns:
        TerminatorBase instance
    """
    if method == 'laplace':
        return LaplaceTerminator(target_risk=target_risk)
    elif method == 'good_turing':
        return GoodTuringTerminator(target_risk=target_risk)
    else:
        raise ValueError(f"Unknown terminator method: {method}. Use 'laplace' or 'good_turing'")


def coverage_to_signature(
    gt_coverage: list,
    cd_coverage: list,
    gt_outcomes: list,
    cd_outcomes: list,
    num_gt_row_predicates: int,
    num_cd_row_predicates: int,
    num_gt_agg_predicates: int,
    num_cd_agg_predicates: int
) -> Optional[Hashable]:
    """
    Convert coverage outcomes to a hashable signature for Good-Turing estimator.
    
    Aggregates all unique (predicate_index, outcome_value) pairs seen in this iteration.
    
    Returns:
        Frozen set of (source, predicate_idx, outcome) tuples, or None if no coverage
    """
    if not gt_outcomes and not cd_outcomes:
        return None
    
    signature_parts: set = set()
    
    # Process GT row outcomes
    for outcome_type, row in gt_outcomes:
        if outcome_type == 'row':
            for pred_idx, val in enumerate(row):
                if pred_idx < num_gt_row_predicates:
                    signature_parts.add(('gt_row', pred_idx, val))
        elif outcome_type == 'agg':
            num_group_keys = len(row) - num_gt_agg_predicates
            for sig_idx in range(num_gt_agg_predicates):
                col_idx = num_group_keys + sig_idx
                if col_idx < len(row):
                    signature_parts.add(('gt_agg', sig_idx, row[col_idx]))
    
    # Process CD row outcomes
    for outcome_type, row in cd_outcomes:
        if outcome_type == 'row':
            for pred_idx, val in enumerate(row):
                if pred_idx < num_cd_row_predicates:
                    signature_parts.add(('cd_row', pred_idx, val))
        elif outcome_type == 'agg':
            num_group_keys = len(row) - num_cd_agg_predicates
            for sig_idx in range(num_cd_agg_predicates):
                col_idx = num_group_keys + sig_idx
                if col_idx < len(row):
                    signature_parts.add(('cd_agg', sig_idx, row[col_idx]))
    
    if not signature_parts:
        return None
    
    return frozenset(signature_parts)
