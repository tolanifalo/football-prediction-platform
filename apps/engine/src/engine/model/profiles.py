"""The named model configurations (MODEL-BASELINE.md §2).

THIS MODULE IS WHERE "THE PRODUCTION MODEL" IS DEFINED, and it is the only
place. A caller asks for a profile by name rather than assembling knobs, so
"what does production run" has one answer that can be read, tested and
changed deliberately.

    PRODUCTION   independent Poisson + 180-day exponential time decay
    CONTROL      independent unweighted Poisson - the frozen benchmark
    EXPERIMENTAL Dixon-Coles and the other tested decay half-lives

The mathematics is identical across all three: the same estimator, the same
parameterisation, the same cold-start policy. **They differ only in
configuration**, which is what made the multi-season comparison a controlled
one and is why adopting 180-day decay required no change to the estimator.

WHY 180 DAYS, AND WHY IT IS NOT A KNOB TO TURN. It was selected by a
predeclared rule on the 2019/20-2022/23 development period, then confirmed
once on held-out 2023/24-2024/25 (MULTI-SEASON-EVALUATION.md). Re-tuning it
against those held-out seasons would destroy the only clean evidence this
project has, so the value is fixed here and changing it is a new evaluation,
not an edit.

`FitConfig()` with no arguments remains the CONTROL mathematics - unweighted,
no correction. That is deliberate: the low-level dataclass keeps the simplest
possible defaults, and "what production runs" is expressed by a profile
rather than hidden in a field default where a caller could inherit it by
accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from engine.model.decay import DecayConfig
from engine.model.fit import MODEL_FAMILY, FitConfig

#: The adopted half-life. Fixed by evaluation, not by tuning.
PRODUCTION_HALF_LIFE_DAYS: Final[float] = 180.0


@dataclass(frozen=True)
class ModelProfile:
    """One named, reproducible model configuration."""

    name: str
    description: str
    fit: FitConfig

    def as_metadata(self) -> dict[str, object]:
        return {
            "profile": self.name,
            "description": self.description,
            "model_family": MODEL_FAMILY,
            **self.fit.as_metadata(),
        }


def _fit(
    name: str, *, half_life: float | None = None, dixon_coles: bool = False
) -> FitConfig:
    """A profile's configuration, tagged with the profile's own name.

    The name is carried into `FitConfig` so a fitted model reports which
    profile produced it without the caller having to remember.
    """
    return FitConfig(
        decay=DecayConfig(half_life_days=half_life),
        dixon_coles=dixon_coles,
        profile=name,
    )


#: WHAT PRODUCTION RUNS.
PRODUCTION: Final[ModelProfile] = ModelProfile(
    name="production",
    description="independent Poisson, 180-day exponential time decay",
    fit=_fit("production", half_life=PRODUCTION_HALF_LIFE_DAYS),
)

#: THE CONTROL. Kept forever, unchanged, as the thing every future model has
#: to beat. Its numbers on E0 2023/24 are 350 predictions, 214 fits, 0.9802
#: log loss and 0.5771 Brier, and a regression test holds them there.
CONTROL: Final[ModelProfile] = ModelProfile(
    name="control",
    description="independent unweighted Poisson - the frozen benchmark",
    fit=_fit("control"),
)

#: Tested, NOT adopted. Kept available so a later multi-league evaluation is a
#: configuration change rather than new work.
EXPERIMENTAL: Final[dict[str, ModelProfile]] = {
    "dixon-coles": ModelProfile(
        name="dixon-coles",
        description="unweighted Poisson + Dixon-Coles low-score correction",
        fit=_fit("dixon-coles", dixon_coles=True),
    ),
    "decay-30d": ModelProfile(
        name="decay-30d",
        description="independent Poisson, 30-day half-life",
        fit=_fit("decay-30d", half_life=30.0),
    ),
    "decay-90d": ModelProfile(
        name="decay-90d",
        description="independent Poisson, 90-day half-life",
        fit=_fit("decay-90d", half_life=90.0),
    ),
    "decay-365d": ModelProfile(
        name="decay-365d",
        description="independent Poisson, 365-day half-life",
        fit=_fit("decay-365d", half_life=365.0),
    ),
    "decay-365d-dixon-coles": ModelProfile(
        name="decay-365d-dixon-coles",
        description="365-day half-life + Dixon-Coles",
        fit=_fit("decay-365d-dixon-coles", half_life=365.0, dixon_coles=True),
    ),
}

PROFILES: Final[dict[str, ModelProfile]] = {
    PRODUCTION.name: PRODUCTION,
    CONTROL.name: CONTROL,
    **EXPERIMENTAL,
}

#: What a caller gets when it does not say. Production, by name.
DEFAULT_PROFILE: Final[ModelProfile] = PRODUCTION


def profile(name: str) -> ModelProfile:
    """Look one up, refusing an unknown name rather than falling back."""
    found = PROFILES.get(name)
    if found is None:
        raise KeyError(
            f"unknown model profile {name!r}; known: {sorted(PROFILES)}"
        )
    return found
