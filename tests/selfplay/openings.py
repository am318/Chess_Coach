"""Back-compat shim: the curated opening pool now lives in the coach package so
both the self-play harness and the sparring opening-picker can share it."""

from coach.sparring.openings_pool import BALANCED_FENS

__all__ = ["BALANCED_FENS"]
