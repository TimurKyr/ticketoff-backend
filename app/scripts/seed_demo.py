"""Demo data for the web app, the scanner app and the presentation (BE-23).

    python -m app.scripts.seed_demo [--print-tokens] [--qr-dir PATH] [--reset-scans]

On the server:

    docker compose -f docker-compose.prod.yml exec api python -m app.scripts.seed_demo

Idempotent: every row is looked up by a natural key (user email, venue name, event title
of the demo organizer, ticket type name within its event, attendee name within the demo
buyer's orders of an event) and created only if missing. Running it again changes nothing,
except with --reset-scans or a DEMO_PASSWORD that differs from the stored one.

Events, ticket types, activation, staff and tickets go through the same services as the
API, so the tickets carry real tk1. tokens signed with this server's TICKET_SIGNING_SECRET.
There is no cancel or refund service yet, so the cancelled and refunded demo tickets are
set directly (with a simulated Refund row for the refunded one).

Passwords: DEMO_PASSWORD, if set, is the password of every demo account (and resets it on
existing accounts). Without it, newly created accounts get one random password, printed
once; existing accounts keep theirs.
"""

import argparse
import os
import secrets
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.modules.registry  # noqa: F401  (all models, for relationship resolution)
from app.modules.checkin import service as checkin_service
from app.modules.checkin.schemas import ScanResult
from app.modules.events import service as events_service
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.events.schemas import EventCreate
from app.modules.identity.models import OrganizerProfile, StaffAssignment, User
from app.modules.identity.security import hash_password, verify_password
from app.modules.identity.service import normalize_email
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus, TicketKind, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import (
    AttendeeIn,
    CheckoutCreate,
    RegistrationCreate,
    RegistrationItemIn,
    TicketTypeCreate,
)
from app.modules.payments import repository as payments_repository
from app.modules.payments import service as payments_service
from app.modules.payments.enums import RefundStatus
from app.modules.payments.models import Refund
from app.modules.payments.schemas import PayOrderIn

TZ_NAME = "Asia/Almaty"
TZ = ZoneInfo(TZ_NAME)
MIN_PASSWORD_LENGTH = 8  # same as RegisterIn

ORGANIZER_EMAIL = "demo.organizer@ticketoff.store"
SCANNER_EMAIL = "demo.scanner@ticketoff.store"
BUYER_EMAIL = "demo.buyer@ticketoff.store"
ORGANIZER_NAME = "Ticketoff Demo"


# --- what to create -------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueSpec:
    name: str
    address: str
    city: str


@dataclass(frozen=True)
class TypeSpec:
    name: str
    kind: TicketKind
    price_kzt: int
    quantity: int
    description: str | None = None


@dataclass(frozen=True)
class EventSpec:
    key: str
    title: str
    description: str
    category: str
    venue: str
    days_ahead: int  # from today in Almaty; fixed once the event exists
    hour: int  # local start time
    hours: int  # duration
    types: tuple[TypeSpec, ...]

    @property
    def has_paid(self) -> bool:
        return any(t.kind == TicketKind.PAID for t in self.types)


@dataclass(frozen=True)
class TicketSpec:
    label: str
    event: str  # EventSpec.key
    ticket_type: str
    attendee: str
    status: TicketStatus  # status right after the seed creates it


VENUES = (
    VenueSpec("Almaty Arena", "мкр. Нуркент, 7", "Алматы"),
    VenueSpec("Дворец Республики", "пр. Достык, 56", "Алматы"),
    VenueSpec("Astana Arena", "пр. Туран, 48", "Астана"),
    VenueSpec("Барыс Арена", "пр. Туран, 57", "Астана"),
)

EVENTS = (
    EventSpec(
        key="free",
        title="Открытая лекция: IT-карьера в Казахстане",
        description="Бесплатная лекция и нетворкинг. Демо-событие ticketoff.",
        category="education",
        venue="Дворец Республики",
        days_ahead=12,
        hour=18,
        hours=2,
        types=(TypeSpec("Вход свободный", TicketKind.FREE, 0, 200),),
    ),
    EventSpec(
        key="paid",
        title="Kazakh Symphony Night",
        description="Вечер симфонической музыки. Демо-событие ticketoff, оплата симулируется.",
        category="music",
        venue="Astana Arena",
        days_ahead=19,
        hour=19,
        hours=3,
        types=(TypeSpec("Стандарт", TicketKind.PAID, 5000, 300),),
    ),
    EventSpec(
        key="multi",
        title="Almaty Jazz Weekend",
        description="Джазовый фестиваль: несколько типов билетов. Демо-событие ticketoff.",
        category="music",
        venue="Almaty Arena",
        days_ahead=26,
        hour=17,
        hours=5,
        types=(
            TypeSpec("Детский", TicketKind.FREE, 0, 50, "Для детей до 12 лет"),
            TypeSpec("Стандарт", TicketKind.PAID, 8000, 400),
            TypeSpec("VIP", TicketKind.PAID, 25000, 50, "Места у сцены"),
        ),
    ),
)

TICKETS = (
    TicketSpec("free-valid", "free", "Вход свободный", "Демо Гость", TicketStatus.VALID),
    TicketSpec(
        "free-checked-in", "free", "Вход свободный", "Демо Гость Прошёл", TicketStatus.CHECKED_IN
    ),
    TicketSpec(
        "free-cancelled", "free", "Вход свободный", "Демо Гость Отменён", TicketStatus.CANCELLED
    ),
    TicketSpec("paid-valid", "paid", "Стандарт", "Демо Покупатель", TicketStatus.VALID),
    TicketSpec(
        "paid-refunded", "paid", "Стандарт", "Демо Покупатель Возврат", TicketStatus.REFUNDED
    ),
    TicketSpec("multi-vip", "multi", "VIP", "Демо VIP", TicketStatus.VALID),
    TicketSpec("multi-standard", "multi", "Стандарт", "Демо Стандарт", TicketStatus.VALID),
)


# --- result ---------------------------------------------------------------------------------


@dataclass
class AccountOut:
    role: str
    email: str
    password_note: str


@dataclass
class TicketOutRow:
    label: str
    event_title: str
    event_id: uuid.UUID
    ticket_id: uuid.UUID
    attendee: str
    status: TicketStatus
    qr_token: str


@dataclass
class SeedResult:
    accounts: list[AccountOut] = field(default_factory=list)
    generated_password: str | None = None
    venues: list[Venue] = field(default_factory=list)
    events: dict[str, Event] = field(default_factory=dict)
    tickets: list[TicketOutRow] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SeedError(RuntimeError):
    pass


# --- steps ----------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_user(
    session: Session, result: SeedResult, role: str, email: str, password: str | None
) -> User:
    email = normalize_email(email)
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        if password is None:
            if result.generated_password is None:
                result.generated_password = secrets.token_urlsafe(12)
            new_password, note = result.generated_password, "generated (printed below)"
        else:
            new_password, note = password, "DEMO_PASSWORD"
        user = User(email=email, password_hash=hash_password(new_password), email_verified=True)
        session.add(user)
        session.commit()
        result.created.append(f"user {email}")
    elif password is not None:
        note = "DEMO_PASSWORD"
        if not verify_password(password, user.password_hash):
            user.password_hash = hash_password(password)
            session.commit()
            note = "DEMO_PASSWORD (reset)"
    else:
        note = "unchanged"
    result.accounts.append(AccountOut(role, email, note))
    return user


def _ensure_organizer_profile(session: Session, result: SeedResult, user: User) -> OrganizerProfile:
    profile = session.scalar(select(OrganizerProfile).where(OrganizerProfile.user_id == user.id))
    if profile is None:
        profile = OrganizerProfile(user_id=user.id, display_name=ORGANIZER_NAME)
        session.add(profile)
        session.commit()
        result.created.append(f"organizer profile {ORGANIZER_NAME}")
    return profile


def _ensure_venues(session: Session, result: SeedResult) -> dict[str, Venue]:
    venues: dict[str, Venue] = {}
    for spec in VENUES:
        venue = session.scalar(select(Venue).where(Venue.name == spec.name).order_by(Venue.id))
        if venue is None:
            venue = Venue(name=spec.name, address=spec.address, city=spec.city, timezone=TZ_NAME)
            session.add(venue)
            session.commit()
            result.created.append(f"venue {spec.name}")
        venues[spec.name] = venue
        result.venues.append(venue)
    return venues


def _start_at(spec: EventSpec) -> datetime:
    day = datetime.now(TZ).date() + timedelta(days=spec.days_ahead)
    return datetime(day.year, day.month, day.day, spec.hour, tzinfo=TZ)


def _ensure_event(
    session: Session,
    result: SeedResult,
    organizer: OrganizerProfile,
    venue: Venue,
    spec: EventSpec,
) -> Event:
    event = session.scalar(
        select(Event)
        .where(Event.organizer_id == organizer.id, Event.title == spec.title)
        .order_by(Event.start_at)
    )
    if event is None:
        start = _start_at(spec)
        payload = EventCreate(
            title=spec.title,
            description=spec.description,
            category=spec.category,
            venue_id=venue.id,
            start_at=start,
            end_at=start + timedelta(hours=spec.hours),
            capacity=sum(t.quantity for t in spec.types),
        )
        event = events_service.create_event(session, organizer, payload)
        result.created.append(f"event {spec.title}")
    return event


def _ensure_ticket_types(
    session: Session, result: SeedResult, organizer: OrganizerProfile, event: Event, spec: EventSpec
) -> dict[str, TicketType]:
    existing = {
        tt.name: tt
        for tt in session.scalars(select(TicketType).where(TicketType.event_id == event.id))
    }
    for type_spec in spec.types:
        if type_spec.name in existing:
            continue
        orders_service.create_ticket_type(
            session,
            organizer.id,
            event.id,
            TicketTypeCreate(
                name=type_spec.name,
                description=type_spec.description,
                kind=type_spec.kind,
                price_kzt=type_spec.price_kzt,
                quantity_total=type_spec.quantity,
            ),
        )
        result.created.append(f"ticket type {spec.title} / {type_spec.name}")
    return {
        tt.name: tt
        for tt in session.scalars(select(TicketType).where(TicketType.event_id == event.id))
    }


def _activate_paid_sales(
    session: Session, result: SeedResult, event: Event, organizer_user: User
) -> None:
    """The BE-14 checklist, step by step, as the organizer would click it."""
    if event.paid_sales_active:
        return
    payments_service.simulate_verification(session, event)
    payments_service.accept_terms(session, event)
    checklist = payments_service.pay_activation(session, event, organizer_user)
    if not checklist.active:
        raise SeedError(f"Paid sales did not activate for {event.title}: {checklist}")
    result.created.append(f"paid sales activated for {event.title}")


def _ensure_staff(session: Session, result: SeedResult, event: Event, scanner: User) -> None:
    assigned = session.scalar(
        select(StaffAssignment).where(
            StaffAssignment.event_id == event.id, StaffAssignment.user_id == scanner.id
        )
    )
    if assigned is None:
        session.add(StaffAssignment(event_id=event.id, user_id=scanner.id))
        session.commit()
        result.created.append(f"staff {scanner.email} on {event.title}")


def _find_ticket(session: Session, buyer: User, event: Event, attendee: str) -> Ticket | None:
    return session.scalar(
        select(Ticket)
        .join(Attendee, Ticket.attendee_id == Attendee.id)
        .join(OrderItem, Ticket.order_item_id == OrderItem.id)
        .join(Order, OrderItem.order_id == Order.id)
        .where(
            Order.buyer_user_id == buyer.id,
            Order.event_id == event.id,
            Attendee.name == attendee,
        )
        .order_by(Ticket.created_at)
    )


def _issue_ticket(
    session: Session, buyer: User, event: Event, ticket_type: TicketType, attendee: str
) -> uuid.UUID:
    """One ticket in its own order, through registration or checkout + simulated payment."""
    data = {
        "items": [RegistrationItemIn(ticket_type_id=ticket_type.id, quantity=1)],
        "attendees": [AttendeeIn(name=attendee, email=buyer.email)],
    }
    if ticket_type.kind == TicketKind.FREE:
        order = orders_service.register_free(session, buyer, event.id, RegistrationCreate(**data))
    else:
        pending = orders_service.checkout(session, buyer, event.id, CheckoutCreate(**data))
        paid = payments_service.pay_order(
            session, buyer, pending.id, PayOrderIn(simulate="success")
        )
        order = paid.order
    return order.tickets[0].id


def _cancel(session: Session, ticket: Ticket) -> None:
    # No cancel service yet: mark the ticket and its one-ticket order cancelled.
    ticket.status = TicketStatus.CANCELLED
    ticket.order_item.order.status = OrderStatus.CANCELLED
    session.commit()


def _refund(session: Session, ticket: Ticket) -> None:
    # No refund service yet: a simulated succeeded refund of the whole one-ticket order.
    order = ticket.order_item.order
    payment = payments_repository.get_succeeded_order_payment(session, order.id)
    session.add(
        Refund(
            payment_id=payment.id,
            amount_kzt=payment.amount_kzt,
            status=RefundStatus.SUCCEEDED,
            reason="demo seed",
        )
    )
    ticket.status = TicketStatus.REFUNDED
    order.status = OrderStatus.REFUNDED
    session.commit()


def _check_in(session: Session, scanner: User, event: Event, ticket: Ticket) -> None:
    out = checkin_service.scan(session, scanner, event.id, ticket.qr_token)
    if out.result != ScanResult.CHECKED_IN:
        raise SeedError(f"Seed check-in of {ticket.id} gave {out.result}")


def _ensure_ticket(
    session: Session,
    result: SeedResult,
    spec: TicketSpec,
    event: Event,
    ticket_type: TicketType,
    buyer: User,
    scanner: User,
    reset_scans: bool,
) -> None:
    ticket = _find_ticket(session, buyer, event, spec.attendee)
    if ticket is None:
        ticket_id = _issue_ticket(session, buyer, event, ticket_type, spec.attendee)
        ticket = session.get(Ticket, ticket_id)
        if spec.status == TicketStatus.CANCELLED:
            _cancel(session, ticket)
        elif spec.status == TicketStatus.REFUNDED:
            _refund(session, ticket)
        elif spec.status == TicketStatus.CHECKED_IN:
            _check_in(session, scanner, event, ticket)
        result.created.append(f"ticket {spec.label}")
    elif reset_scans and (spec.status, ticket.status) == (
        TicketStatus.VALID,
        TicketStatus.CHECKED_IN,
    ):
        checkin_service.undo_check_in(session, scanner, ticket.id)
        result.created.append(f"check-in undone for {spec.label}")
    session.refresh(ticket)
    result.tickets.append(
        TicketOutRow(
            label=spec.label,
            event_title=event.title,
            event_id=event.id,
            ticket_id=ticket.id,
            attendee=spec.attendee,
            status=ticket.status,
            qr_token=ticket.qr_token,
        )
    )


def run(session: Session, *, password: str | None, reset_scans: bool = False) -> SeedResult:
    """Create whatever demo data is missing. Every service call commits on its own."""
    if password is not None and len(password) < MIN_PASSWORD_LENGTH:
        raise SeedError(f"DEMO_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters")
    result = SeedResult()

    organizer_user = _ensure_user(session, result, "organizer", ORGANIZER_EMAIL, password)
    scanner = _ensure_user(session, result, "scanner (staff)", SCANNER_EMAIL, password)
    buyer = _ensure_user(session, result, "buyer", BUYER_EMAIL, password)
    organizer = _ensure_organizer_profile(session, result, organizer_user)
    venues = _ensure_venues(session, result)

    types_by_event: dict[str, dict[str, TicketType]] = {}
    for spec in EVENTS:
        event = _ensure_event(session, result, organizer, venues[spec.venue], spec)
        result.events[spec.key] = event
        if event.status in (EventStatus.CANCELLED, EventStatus.COMPLETED):
            result.warnings.append(f"{spec.title} is {event.status}: left as is")
            continue
        if (event.end_at or event.start_at) < _now():
            result.warnings.append(f"{spec.title} is in the past: left as is")
            continue
        # Types before publishing: publish requires a visible ticket type (FX-4).
        types_by_event[spec.key] = _ensure_ticket_types(session, result, organizer, event, spec)
        if event.status == EventStatus.DRAFT:
            events_service.publish_event(session, event)
            result.created.append(f"published {spec.title}")
        if spec.has_paid:
            _activate_paid_sales(session, result, event, organizer_user)
        _ensure_staff(session, result, event, scanner)

    for ticket_spec in TICKETS:
        types = types_by_event.get(ticket_spec.event)
        if types is None or ticket_spec.ticket_type not in types:
            result.warnings.append(f"ticket {ticket_spec.label} skipped (event not open)")
            continue
        _ensure_ticket(
            session,
            result,
            ticket_spec,
            result.events[ticket_spec.event],
            types[ticket_spec.ticket_type],
            buyer,
            scanner,
            reset_scans,
        )
    return result


# --- output ---------------------------------------------------------------------------------


def write_qr_pngs(tickets: Sequence[TicketOutRow], directory: Path, scale: int = 10) -> None:
    """One PNG per ticket. Uses reportlab's QR encoder and Pillow, both already in the image."""
    from PIL import Image
    from reportlab.graphics.barcode import qrencoder

    directory.mkdir(parents=True, exist_ok=True)
    border = 4  # quiet zone in modules, as the QR spec asks
    for row in tickets:
        qr = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
        qr.addData(row.qr_token)
        qr.make()
        modules = qr.getModuleCount()
        size = modules + 2 * border
        image = Image.new("1", (size, size), 1)
        for r in range(modules):
            for c in range(modules):
                if qr.isDark(r, c):
                    image.putpixel((c + border, r + border), 0)
        image = image.resize((size * scale, size * scale), Image.Resampling.NEAREST)
        image.save(directory / f"{row.label}-{row.status}.png")


def print_summary(result: SeedResult, *, print_tokens: bool) -> None:
    print("== ticketoff demo seed ==")
    print(f"created: {len(result.created)}" + ("" if result.created else " (nothing, all present)"))
    for line in result.created:
        print(f"  + {line}")
    for line in result.warnings:
        print(f"  ! {line}")

    print("\naccounts:")
    for account in result.accounts:
        print(f"  {account.role:<16} {account.email:<34} password: {account.password_note}")
    if result.generated_password:
        print(f"  generated password (shown once): {result.generated_password}")

    print("\nvenues:")
    for venue in result.venues:
        print(f"  {venue.id}  {venue.name}, {venue.city}")

    print("\nevents:")
    for event in result.events.values():
        paid = " paid_sales_active" if event.paid_sales_active else ""
        local = event.start_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        print(f"  {event.id}  {event.title}  [{event.status}{paid}]  {local} {TZ_NAME}")

    print("\ntickets (buyer " + BUYER_EMAIL + "):")
    for row in result.tickets:
        print(f"  {row.label:<16} {row.status:<11} {row.ticket_id}  {row.event_title}")
        if print_tokens:
            print(f"    {row.qr_token}")
    if not print_tokens:
        print("  (QR tokens: run again with --print-tokens)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create idempotent ticketoff demo data.")
    parser.add_argument("--print-tokens", action="store_true", help="print ticket QR tokens")
    parser.add_argument("--qr-dir", type=Path, help="save a QR PNG per demo ticket here")
    parser.add_argument(
        "--reset-scans",
        action="store_true",
        help="undo check-ins of the demo tickets that should be valid (after a scanner demo)",
    )
    args = parser.parse_args(argv)

    from app.core.db import SessionLocal

    password = os.environ.get("DEMO_PASSWORD") or None
    with SessionLocal() as session:
        try:
            result = run(session, password=password, reset_scans=args.reset_scans)
        except SeedError as exc:
            print(f"seed failed: {exc}", file=sys.stderr)
            return 1
    print_summary(result, print_tokens=args.print_tokens)
    if args.qr_dir:
        write_qr_pngs(result.tickets, args.qr_dir)
        print(f"\nQR PNGs: {args.qr_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
