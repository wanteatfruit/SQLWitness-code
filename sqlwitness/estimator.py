"""Laplace early termination for coverage-guided search."""

from abc import ABC, abstractmethod
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


def create_terminator(method: str = 'laplace', target_risk: float = 0.05) -> LaplaceTerminator:
    """Create the supported coverage-based stopping estimator."""
    if method != 'laplace':
        raise ValueError("Only the 'laplace' termination method is supported.")
    return LaplaceTerminator(target_risk=target_risk)
