"""Fixed strategy codes used by the Decision Engine."""

from enum import Enum


class Strategy(str, Enum):
    WAIT = "wait"
    BUILD = "build"
    HOLD = "hold"
    HOLD_NO_ADD = "hold_no_add"
    REDUCE = "reduce"
    EXIT = "exit"
