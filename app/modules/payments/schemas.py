from typing import Literal

from pydantic import BaseModel


class ActivationChecklistOut(BaseModel):
    """What an organizer still has to do before the event can sell paid tickets.

    Everything here is simulated: no real KYC, payout account or money is involved.
    """

    # At least one paid ticket type that buyers can see.
    has_paid_ticket_type: bool
    identity_verified: bool
    payout_account_ready: bool
    activation_fee_paid: bool
    terms_accepted: bool
    # Event.paid_sales_active: true once all five items above are done.
    active: bool
    activation_fee_kzt: int
    is_simulated: Literal[True] = True
