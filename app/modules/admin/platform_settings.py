"""Effective platform fees (BE-26): the platform_settings row if an admin saved one,
otherwise the env values from app.core.config.

Every read of activation_fee_kzt, processing_fee_percent and checkout_hold_minutes goes
through get_platform_settings, so a change by an admin applies from the next request on.
A payment stores its own amount, so a change never touches what was already paid.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.admin.models import PlatformSettings

SETTINGS_ROW_ID = 1


@dataclass(frozen=True)
class FeeSettings:
    activation_fee_kzt: int
    processing_fee_percent: int
    checkout_hold_minutes: int


def env_defaults() -> FeeSettings:
    env = get_settings()
    return FeeSettings(
        activation_fee_kzt=env.activation_fee_kzt,
        processing_fee_percent=env.processing_fee_percent,
        checkout_hold_minutes=env.checkout_hold_minutes,
    )


def get_platform_settings(session: Session) -> FeeSettings:
    row = session.scalar(select(PlatformSettings).where(PlatformSettings.id == SETTINGS_ROW_ID))
    if row is None:
        return env_defaults()
    return FeeSettings(
        activation_fee_kzt=row.activation_fee_kzt,
        processing_fee_percent=row.processing_fee_percent,
        checkout_hold_minutes=row.checkout_hold_minutes,
    )
