import uuid
from typing import Literal

from pydantic import BaseModel

from app.modules.orders.schemas import OrderOut
from app.modules.payments.enums import PaymentStatus


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


class PayOrderIn(BaseModel):
    """Simulated card result: there is no real payment provider."""

    simulate: Literal["success", "fail"]


class PaymentOut(BaseModel):
    id: uuid.UUID
    status: PaymentStatus
    amount_kzt: int
    is_simulated: bool


class PaymentResultOut(BaseModel):
    """Result of a pay attempt. A failed simulated payment is still a 200: the request
    worked, the simulated card did not. The order then stays pending, without tickets."""

    payment: PaymentOut
    order: OrderOut
