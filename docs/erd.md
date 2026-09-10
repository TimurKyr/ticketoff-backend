# ticketoff — Data Model (ERD)

Единый источник правды по данным платформы: 24 сущности из раздела 6 ТЗ, их ключевые
поля, статусы и связи. Утверждаем перед тем, как писать модели и первую миграцию.

- **БД:** PostgreSQL · **ORM:** SQLAlchemy 2.0 (async) · **Миграции:** Alembic
- **Валюта:** KZT · **MVP:** платежи/выплаты симулируются, реальных денег нет

Обозначения связей («воронья лапка»): `||` — ровно один, `o{` — ноль или много,
`|{` — один или много. `PK` — первичный ключ, `FK` — внешний ключ.

## ER-диаграмма

```mermaid
erDiagram
    USER ||--o| ORGANIZER_PROFILE : has
    USER ||--o{ ORDER : places
    USER ||--o{ STAFF_ASSIGNMENT : "assigned as"
    USER ||--o{ CHECK_IN_RECORD : "scanned by"
    USER ||--o{ NOTIFICATION : receives
    USER ||--o{ SUPPORT_CASE : opened
    USER ||--o{ SUPPORT_CASE : handles
    USER ||--o{ SUPPORT_MESSAGE : sent
    USER ||--o{ AUDIT_LOG : acted
    ORGANIZER_PROFILE ||--o{ PAYOUT_ACCOUNT : owns
    ORGANIZER_PROFILE ||--o{ EVENT : organizes
    VENUE ||--o{ EVENT : hosts
    VENUE ||--o{ VENUE_SECTION : has
    VENUE_SECTION ||--o{ ROW : has
    ROW ||--o{ SEAT : has
    VENUE_SECTION ||--o{ TICKET_TYPE : "priced as"
    EVENT ||--o{ TICKET_TYPE : defines
    EVENT ||--o{ ORDER : receives
    EVENT ||--o{ SEAT_HOLD : holds
    EVENT ||--o{ STAFF_ASSIGNMENT : "staffed by"
    EVENT ||--o{ PROMOTIONAL_CAMPAIGN : runs
    EVENT ||--o{ SUPPORT_CASE : about
    EVENT ||--o{ PAYMENT : activation
    SEAT ||--o{ SEAT_HOLD : "held by"
    SEAT ||--o| ORDER_ITEM : "assigned to"
    SEAT ||--o| TICKET : seats
    TICKET_TYPE ||--o{ ORDER_ITEM : "sold as"
    ORDER ||--|{ ORDER_ITEM : contains
    ORDER ||--o{ ATTENDEE : lists
    ORDER ||--o{ SEAT_HOLD : reserves
    ORDER ||--o{ PAYMENT : "paid by"
    ORDER ||--o| PROMO_REDEMPTION : applied
    ORDER ||--o{ SUPPORT_CASE : about
    ORDER_ITEM ||--|{ TICKET : issues
    ATTENDEE ||--o{ TICKET : "named on"
    TICKET ||--o{ CHECK_IN_RECORD : "checked in"
    TICKET ||--o{ SUPPORT_CASE : about
    PAYMENT ||--o{ REFUND : "refunded by"
    SUPPORT_CASE ||--|{ SUPPORT_MESSAGE : contains
    PROMOTIONAL_CAMPAIGN ||--|{ PROMO_CODE : generates
    PROMO_CODE ||--o{ PROMO_REDEMPTION : "redeemed as"

    USER {
      uuid id PK
      string email
      string password_hash
      enum system_role
      bool email_verified
    }
    ORGANIZER_PROFILE {
      uuid id PK
      uuid user_id FK
      string display_name
      enum verification_status
    }
    PAYOUT_ACCOUNT {
      uuid id PK
      uuid organizer_id FK
      string provider_ref
      enum status
    }
    STAFF_ASSIGNMENT {
      uuid id PK
      uuid event_id FK
      uuid user_id FK
      enum role
    }
    EVENT {
      uuid id PK
      uuid organizer_id FK
      uuid venue_id FK
      enum status
      enum visibility
      enum seating_type
      bool paid_sales_active
      datetime start_at
    }
    VENUE {
      uuid id PK
      string name
      string address
      string timezone
    }
    VENUE_SECTION {
      uuid id PK
      uuid venue_id FK
      string name
      string price_category
    }
    ROW {
      uuid id PK
      uuid section_id FK
      string label
    }
    SEAT {
      uuid id PK
      uuid row_id FK
      string number
      bool is_accessible
    }
    SEAT_HOLD {
      uuid id PK
      uuid seat_id FK
      uuid event_id FK
      uuid order_id FK
      datetime held_until
      enum status
    }
    TICKET_TYPE {
      uuid id PK
      uuid event_id FK
      uuid section_id FK
      enum kind
      int price_kzt
      int quantity_total
      int max_per_order
      bool is_hidden
    }
    ORDER {
      uuid id PK
      uuid event_id FK
      uuid buyer_user_id FK
      enum status
      int total_kzt
      int discount_kzt
    }
    ORDER_ITEM {
      uuid id PK
      uuid order_id FK
      uuid ticket_type_id FK
      uuid seat_id FK
      int unit_price_kzt
      int quantity
    }
    TICKET {
      uuid id PK
      uuid order_item_id FK
      uuid attendee_id FK
      uuid seat_id FK
      string qr_token
      enum status
    }
    ATTENDEE {
      uuid id PK
      uuid order_id FK
      string name
      string email
    }
    PAYMENT {
      uuid id PK
      uuid order_id FK
      uuid event_id FK
      enum purpose
      enum status
      bool is_simulated
      int amount_kzt
    }
    REFUND {
      uuid id PK
      uuid payment_id FK
      int amount_kzt
      enum status
      string reason
    }
    CHECK_IN_RECORD {
      uuid id PK
      uuid ticket_id FK
      uuid event_admin_id FK
      datetime checked_in_at
      bool is_reversed
    }
    NOTIFICATION {
      uuid id PK
      uuid user_id FK
      enum type
      enum channel
      datetime read_at
    }
    SUPPORT_CASE {
      uuid id PK
      uuid opened_by FK
      uuid assignee_id FK
      enum kind
      enum category
      enum status
    }
    SUPPORT_MESSAGE {
      uuid id PK
      uuid case_id FK
      uuid sender_id FK
      text body
    }
    PROMOTIONAL_CAMPAIGN {
      uuid id PK
      uuid event_id FK
      enum discount_type
      int discount_value
      int max_redemptions
    }
    PROMO_CODE {
      uuid id PK
      uuid campaign_id FK
      string code
      string qr_token
      enum status
    }
    PROMO_REDEMPTION {
      uuid id PK
      uuid promo_code_id FK
      uuid order_id FK
      int discount_kzt
    }
    AUDIT_LOG {
      uuid id PK
      uuid actor_user_id FK
      uuid event_id FK
      string action_type
      string entity_type
    }
```

## Сущности по доменам

Ключевые `enum`-статусы задают бизнес-логику — держим их согласованными у всех.

### Identity & Access
- **User** — аккаунт с email/паролем. `system_role`: `attendee` · `platform_admin`.
- **OrganizerProfile** — профиль организатора. `verification_status`: `unverified` · `pending` · `verified`.
- **PayoutAccount** — счёт для выплат (sandbox).
- **StaffAssignment** — назначение пользователя контролёром (Event Admin) на конкретное событие.

### Events & Venue
- **Event** — событие. `status`: `draft` · `published` · `cancelled` · `completed`. `visibility`: `public` · `unlisted` · `private`. `seating_type`: `general` · `assigned`.
- **Venue** — предопределённая площадка.
- **VenueSection** — секция + ценовая категория.
- **Row** — ряд в секции.
- **Seat** — место; помечает доступные (`is_accessible`).

### Seating
- **SeatHold** — временная атомарная бронь места на время checkout. `status`: `held` · `released` · `converted`.

### Orders & Tickets
- **TicketType** — тип билета. `kind`: `free` · `paid`.
- **Order** — заказ/регистрация. `status`: `pending` · `paid` · `free_confirmed` · `cancelled` · `refunded`.
- **OrderItem** — строка заказа: тип×кол-во или одно место.
- **Ticket** — один вход с уникальным QR. `status`: `valid` · `checked_in` · `cancelled` · `refunded`.
- **Attendee** — гость, указанный в билете (может не иметь аккаунта).

### Payments
- **Payment** — движение денег (симуляция). `purpose`: `ticket_order` · `activation`. `status`: `pending` · `succeeded` · `failed`.
- **Refund** — возврат по платежу. `status`: `pending` · `succeeded` · `failed`.

### Ops & Support
- **CheckInRecord** — скан на входе; поддерживает отмену (`is_reversed`).
- **Notification** — исходящее уведомление. `channel`: `email` · `in_app`.
- **SupportCase** — тред поддержки. `kind`: `attendee` · `organizer`. `status`: `open` · `in_progress` · `waiting_customer` · `resolved`.
- **SupportMessage** — сообщение внутри кейса.

### Promotions
- **PromotionalCampaign** — скидочная кампания события. `discount_type`: `percent` · `fixed`.
- **PromoCode** — промокод + токен Campaign QR. `status`: `active` · `disabled` · `exhausted` · `expired`.
- **PromoRedemption** — факт применения кода к заказу.

### Audit
- **AuditLog** — неизменяемая запись значимого действия (кто, что, над чем, когда).

## Ключевые решения

1. **Роли — три механизма, а не одно поле.** `User.system_role` (attendee / platform_admin), способность организатора — через `OrganizerProfile`, Event Admin — через `StaffAssignment` на конкретное событие. Один человек может быть и покупателем, и организатором.
2. **Плата за активацию ≠ оплата билетов.** Обе идут через `Payment`, разделяются полем `purpose`: `activation` ссылается на `Event`, `ticket_order` — на `Order`.
3. **Места не продаются дважды.** `SeatHold` с `held_until` + уникальное ограничение в БД. Бронь: `held` → `converted` при покупке, либо `released` по таймауту. Резервация атомарна.
4. **Два разных QR.** `Ticket.qr_token` — вход на событие; `PromoCode.qr_token` — трекируемая ссылка кампании. Сканер контролёра принимает **только** билетные токены и никогда — промо.
5. **Деньги симулируются.** `Payment.is_simulated = true` для всего MVP; демо-записи никогда не подаются как реальные транзакции.
6. **Аудит только на добавление.** `AuditLog` неизменяем — никаких edit/delete через интерфейсы.
7. **Гость ≠ покупатель.** Покупатель — это `User` (`Order.buyer_user_id`); `Attendee` — имя на билете, у него может не быть аккаунта.

---

_ticketoff · CSCI 361 Software Engineering — черновик модели данных (раздел 6 ТЗ). Правки вносятся в этот документ и в первую миграцию через PR._
