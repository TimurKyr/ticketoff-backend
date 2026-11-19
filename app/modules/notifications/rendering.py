"""Email templates (Jinja2), per language: templates/<lang>/<name>.subject.txt / .txt / .html.

HTML is autoescaped (event titles and names are typed by users); the text part is not, as
plain text can't run markup. StrictUndefined turns a missing variable into an error instead
of a silently empty email. The template name is the NotificationType value.

Context each template needs (values are plain: str, int, bool, aware datetime, lists/dicts):
  order_confirmed       event_title, starts_at, tz_name, venue_line, tickets [{ticket_type,
                        guest_name}], is_paid, total_kzt, discount_kzt, paid_kzt, order_url,
                        tickets_url (None when the PDFs are attached)
  payment_failed        event_title, starts_at, tz_name, amount_kzt, retry_until, order_url
  refund_processed      event_title, starts_at, tz_name, amount_kzt, reason (or None), order_url
  event_cancelled       event_title, starts_at, tz_name, refund_kzt (0: nothing refunded),
                        order_urls (one per order the cancellation changed)
  event_updated         event_title, tz_name, changes [{field: starts_at|ends_at|venue, old,
                        new}] (datetimes, or venue lines for venue)
  account_verification  verify_url, expires_hours
  support_reply         case_subject, message_excerpt, case_url
  support_status        case_subject, status (SupportStatus value), case_url

Links come from notifications.links.
"""

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, pass_context

from app.modules.identity.enums import Language
from app.modules.notifications.formatting import local_datetime, money
from app.modules.notifications.providers import Attachment, EmailMessage

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_LANGUAGE = Language.RU
# Templates whose body carries a secret link: the console provider never logs their body.
SECRET_TEMPLATES = frozenset({"account_verification"})


@pass_context
def _local_dt(context, value, tz_name: str) -> str:
    return local_datetime(value, tz_name, context["language"])


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=lambda name: bool(name) and name.endswith(".html"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    env.filters["local_dt"] = _local_dt
    env.filters["money"] = money
    return env


def _language_dir(name: str, language: str) -> str:
    """The language to use for this template: the asked one if it exists, else ru."""
    if (TEMPLATES_DIR / language / f"{name}.txt").is_file():
        return language
    return DEFAULT_LANGUAGE


def render(name: str, language: str, context: dict) -> tuple[str, str, str | None]:
    """(subject, text, html or None) of template `name` in `language` (falls back to ru)."""
    env, lang = _env(), _language_dir(name, language)
    values = {**context, "language": lang}
    subject = env.get_template(f"{lang}/{name}.subject.txt").render(values).strip()
    text = env.get_template(f"{lang}/{name}.txt").render(values).strip() + "\n"
    try:
        html = env.get_template(f"{lang}/{name}.html").render(values)
    except TemplateNotFound:
        html = None
    return subject, text, html


def build_email(
    to: str,
    name: str,
    language: str,
    context: dict,
    attachments: tuple[Attachment, ...] = (),
) -> EmailMessage:
    subject, text, html = render(name, language, context)
    return EmailMessage(
        to=to,
        subject=subject,
        text=text,
        html=html,
        attachments=attachments,
        contains_secret=name in SECRET_TEMPLATES,
    )
