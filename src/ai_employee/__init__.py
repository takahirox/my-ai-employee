"""Goal-driven orchestration with autonomous, isolated Worker sessions."""

from .engine import Engine
from .models import Candidate, Goal, Plan, RunConfig, Task

__version__ = "0.3.0"
__all__ = ["Candidate", "Engine", "Goal", "Plan", "RunConfig", "Task", "__version__"]
