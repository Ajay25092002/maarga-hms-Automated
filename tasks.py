
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

from celery_app import celery_app

# Load local .env when running outside Docker.
# Docker --env-file settings continue to work as usual.
load_dotenv()

logger = logging.getLogger(__name__)

INDIA_TZ = ZoneInfo("Asia/Kolkata")
MONEY = Decimal("0.01")

# Keep these service names aligned with the backend's billing configuration.
AUTOMATED_IP_SERVICE_NAMES = [
    "ROOM RENT",
    "HOLISTIC THERAPIES",
    "DIET & FOOD",
    "DOCTOR CONSULTATION",
    "NURSING CHARGES",
    "PSYCHIATRIC CONSULTATION/TREATMENT",
    "PSYCHOLOGIST COUNSELLING/TREATMENT",
    "RECREATION ACTVITIES/THERAPY",
]


class BillingRuleError(Exception):
    """A service or pricing configuration cannot be used for IP billing."""


def money(value):
    """Apply the same two-decimal rounding rule as the backend."""
    return Decimal(str(value)).quantize(
        MONEY,
        rounding=ROUND_HALF_UP,
    )


def get_db_connection():
    """
    Connect to the same PostgreSQL database as the backend.

    DATABASE_URL must be provided to the scheduler container
    through its environment configuration.
    """
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not configured in the scheduler environment."
        )

    return psycopg2.connect(
        database_url,
        connect_timeout=10,
        options="-c timezone=Asia/Kolkata",
    )


def resolve_service_price(
    cursor,
    service_id,
    bill_type,
    doctor_id=None,
    room_type_id=None,
):
    """
    Resolve an active service price using the backend's pricing rules.

    IP pricing:
    - If room-specific prices exist, select the unique price for the
      inpatient's room type.
    - Otherwise, select the unique fixed IP price.
    """
    cursor.execute(
        """
        SELECT
            s.id,
            s.name AS service_name,
            s.service_group_id,
            s.is_active AS service_is_active,
            s.available_for_op,
            s.available_for_ip,
            sg.name AS service_group_name,
            sg.is_active AS group_is_active
        FROM public.services s
        JOIN public.service_groups sg
            ON sg.id = s.service_group_id
        WHERE s.id = %s
        """,
        (service_id,),
    )

    service = cursor.fetchone()

    if service is None:
        raise BillingRuleError(f"Service {service_id} does not exist.")

    if not service["service_is_active"]:
        raise BillingRuleError(
            f"Service '{service['service_name']}' is inactive."
        )

    if not service["group_is_active"]:
        raise BillingRuleError(
            f"Service group '{service['service_group_name']}' is inactive."
        )

    availability_field = (
        "available_for_op" if bill_type == "OP" else "available_for_ip"
    )

    if not service[availability_field]:
        raise BillingRuleError(
            f"Service '{service['service_name']}' is not available "
            f"for {bill_type} billing."
        )

    cursor.execute(
        """
        SELECT id, bill_type, doctor_id, room_type_id, amount
        FROM public.service_prices
        WHERE service_id = %s
          AND bill_type = %s
          AND is_active = TRUE
        ORDER BY id
        """,
        (service_id, bill_type),
    )

    prices = cursor.fetchall()

    if not prices:
        raise BillingRuleError(
            f"No active {bill_type} price is configured for "
            f"service '{service['service_name']}'."
        )

    if bill_type != "IP":
        raise BillingRuleError(
            "This scheduler is intended only for IP billing."
        )

    if any(price["doctor_id"] is not None for price in prices):
        raise BillingRuleError(
            f"Invalid IP pricing configuration for '{service['service_name']}'."
        )

    room_specific_prices = [
        price for price in prices if price["room_type_id"] is not None
    ]

    if room_specific_prices:
        if room_type_id is None:
            raise BillingRuleError(
                f"A room type is required for '{service['service_name']}'."
            )

        matching_prices = [
            price
            for price in room_specific_prices
            if price["room_type_id"] == room_type_id
        ]

        if len(matching_prices) != 1:
            raise BillingRuleError(
                f"No unique IP price is configured for "
                f"'{service['service_name']}' and room type {room_type_id}."
            )

        selected_price = matching_prices[0]

    else:
        fixed_prices = [
            price for price in prices if price["room_type_id"] is None
        ]

        if len(fixed_prices) != 1:
            raise BillingRuleError(
                f"No unique fixed IP price is configured for "
                f"'{service['service_name']}'."
            )

        selected_price = fixed_prices[0]

    return {
        "service_id": service["id"],
        "service_group_id": service["service_group_id"],
        "service_price_id": selected_price["id"],
        "service_name": service["service_name"],
        "service_group_name": service["service_group_name"],
        "rate": money(selected_price["amount"]),
    }


def run_automated_ip_billing_service(service_date):
    """
    Add missing automated service line items to active IP bills
    for the specified service_date.

    Repeated executions skip an existing service_id on the same bill
    and service_date. Existing line items are not deleted or rewritten.
    """
    if isinstance(service_date, str):
        service_date = date.fromisoformat(service_date)

    if isinstance(service_date, datetime):
        service_date = service_date.date()

    if not isinstance(service_date, date):
        raise ValueError("service_date must be a date or ISO date string.")

    service_names = [
        name.strip()
        for name in AUTOMATED_IP_SERVICE_NAMES
        if name.strip()
    ]

    if not service_names:
        raise RuntimeError("No automated IP services are configured.")

    summary = {
        "service_date": service_date.isoformat(),
        "eligible_bills": 0,
        "bills_processed": 0,
        "items_added": 0,
        "items_skipped_existing": 0,
        "failures": [],
    }

    connection = None

    try:
        connection = get_db_connection()

        # All successful changes in this run are committed together.
        # Unexpected database errors roll the transaction back.
        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                cursor.execute(
                    """
                    SELECT id
                    FROM public.users
                    WHERE LOWER(login_id) = 'system'
                       OR LOWER(full_name) = 'system'
                    ORDER BY id
                    LIMIT 1
                    """
                )
                system_row = cursor.fetchone()
                system_user_id = system_row["id"] if system_row else None
                if system_user_id is None:
                    logger.warning(
                        "Dedicated System user not found; scheduler audit actor will be null."
                    )

                cursor.execute(
                    """
                    SELECT id
                    FROM public.bills
                    WHERE bill_type = 'IP'
                      AND discharge_date IS NULL
                    ORDER BY id
                    """
                )

                bill_ids = [row["id"] for row in cursor.fetchall()]
                summary["eligible_bills"] = len(bill_ids)

                normalized_names = [
                    name.casefold() for name in service_names
                ]

                cursor.execute(
                    """
                    SELECT s.id, s.name
                    FROM public.services s
                    JOIN public.service_groups sg
                      ON sg.id = s.service_group_id
                    WHERE s.is_active = TRUE
                      AND s.available_for_ip = TRUE
                      AND sg.is_active = TRUE
                      AND LOWER(BTRIM(s.name)) = ANY(%s)
                    ORDER BY s.id
                    """,
                    (normalized_names,),
                )

                services = cursor.fetchall()

                found_names = {
                    row["name"].strip().casefold() for row in services
                }

                for missing_name in service_names:
                    if missing_name.casefold() not in found_names:
                        summary["failures"].append({
                            "service_name": missing_name,
                            "error": (
                                "Configured service was not found as "
                                "an active IP service."
                            ),
                        })

                for bill_id in bill_ids:
                    cursor.execute(
                        """
                        SELECT
                            b.id,
                            b.bill_type,
                            b.discharge_date,
                            b.room_type_id,
                            COALESCE(
                                NULLIF(BTRIM(b.room_type_name), ''),
                                rt.name
                            ) AS room_type_name,
                            b.bed_number,
                            b.discount_amount,
                            b.subtotal,
                            b.total_amount
                        FROM public.bills b
                        LEFT JOIN public.room_types rt
                          ON rt.id = b.room_type_id
                        WHERE b.id = %s
                        FOR UPDATE OF b
                        """,
                        (bill_id,),
                    )

                    bill = cursor.fetchone()

                    # Revalidate after acquiring the bill row lock.
                    if (
                        not bill
                        or bill["bill_type"] != "IP"
                        or bill["discharge_date"] is not None
                    ):
                        continue

                    if bill["room_type_id"] is None:
                        summary["failures"].append({
                            "bill_id": bill_id,
                            "error": (
                                "Active IP bill has no room_type_id; "
                                "automated services were not added."
                            ),
                        })
                        continue

                    added_for_bill = 0

                    for service in services:
                        # Duplicate check is based on service_date,
                        # not the date on which the task executes.
                        cursor.execute(
                            """
                            SELECT id
                            FROM public.bill_items
                            WHERE bill_id = %s
                              AND service_id = %s
                              AND service_date = %s
                            LIMIT 1
                            """,
                            (
                                bill_id,
                                service["id"],
                                service_date,
                            ),
                        )

                        if cursor.fetchone():
                            summary["items_skipped_existing"] += 1
                            continue

                        try:
                            resolved = resolve_service_price(
                                cursor=cursor,
                                service_id=service["id"],
                                bill_type="IP",
                                doctor_id=None,
                                room_type_id=bill["room_type_id"],
                            )
                        except BillingRuleError as exc:
                            summary["failures"].append({
                                "bill_id": bill_id,
                                "service_name": service["name"],
                                "error": str(exc),
                            })
                            continue

                        rate = money(resolved["rate"])
                        quantity = Decimal("1.00")
                        amount = money(rate * quantity)

                        cursor.execute(
                            """
                            INSERT INTO public.bill_items (
                                bill_id,
                                service_id,
                                service_group_id,
                                service_price_id,
                                service_name,
                                service_group_name,
                                service_date,
                                room_type_id,
                                room_type_name,
                                bed_number,
                                rate,
                                quantity,
                                amount,
                                discount_amount,
                                net_amount,
                                gst_amount
                            )
                            VALUES (
                                %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, 0, %s, 0
                            )
                            RETURNING id
                            """,
                            (
                                bill_id,
                                resolved["service_id"],
                                resolved["service_group_id"],
                                resolved["service_price_id"],
                                resolved["service_name"],
                                resolved["service_group_name"],
                                service_date,
                                bill["room_type_id"],
                                bill["room_type_name"],
                                bill["bed_number"],
                                rate,
                                quantity,
                                amount,
                                amount,
                            ),
                        )

                        inserted = cursor.fetchone()
                        item_id = inserted["id"] if isinstance(inserted, dict) else inserted[0]
                        audit_item = {
                            "service_id": resolved["service_id"],
                            "service_group_id": resolved["service_group_id"],
                            "service_price_id": resolved["service_price_id"],
                            "service_name": resolved["service_name"],
                            "service_group_name": resolved["service_group_name"],
                            "service_date": service_date.isoformat(),
                            "room_type_id": bill["room_type_id"],
                            "room_type_name": bill["room_type_name"],
                            "bed_number": bill["bed_number"],
                            "rate": str(rate),
                            "quantity": str(quantity),
                            "amount": str(amount),
                            "discount_amount": "0.00",
                            "net_amount": str(amount),
                            "gst_amount": "0.00",
                        }
                        cursor.execute(
                            """
                            INSERT INTO public.bill_audit_history (
                                bill_id, action, entity_type, entity_id,
                                old_values, new_values, changed_by, changed_at, reason
                            )
                            VALUES (%s, %s, %s, %s, NULL, %s::jsonb, %s,
                                    clock_timestamp(), %s)
                            """,
                            (
                                bill_id,
                                "bill_item.added",
                                "bill_item",
                                item_id,
                                json.dumps(audit_item, ensure_ascii=False),
                                system_user_id,
                                "Automatically added by the daily IP billing scheduler.",
                            ),
                        )
                        added_for_bill += 1
                        summary["items_added"] += 1

                    if added_for_bill:
                        cursor.execute(
                            """
                            SELECT
                                COALESCE(SUM(amount), 0) AS subtotal,
                                COALESCE(SUM(discount_amount), 0)
                                    AS item_discounts,
                                COALESCE(SUM(gst_amount), 0) AS gst_total
                            FROM public.bill_items
                            WHERE bill_id = %s
                            """,
                            (bill_id,),
                        )

                        totals = cursor.fetchone()

                        subtotal = money(totals["subtotal"] or 0)
                        item_discounts = money(
                            totals["item_discounts"] or 0
                        )
                        gst_total = money(totals["gst_total"] or 0)
                        bill_discount = money(
                            bill["discount_amount"] or 0
                        )

                        total_amount = money(
                            subtotal - item_discounts
                            - bill_discount + gst_total
                        )

                        cursor.execute(
                            """
                            UPDATE public.bills
                            SET subtotal = %s,
                                total_amount = %s,
                                updated_by = %s,
                                updated_at = clock_timestamp()
                            WHERE id = %s
                            RETURNING subtotal, total_amount, updated_at
                            """,
                            (subtotal, total_amount, system_user_id, bill_id),
                        )
                        updated_totals = dict(cursor.fetchone())
                        cursor.execute(
                            """
                            INSERT INTO public.bill_audit_history (
                                bill_id, action, entity_type, entity_id,
                                old_values, new_values, changed_by, changed_at, reason
                            )
                            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                                    clock_timestamp(), %s)
                            """,
                            (
                                bill_id,
                                "bill.totals_updated",
                                "bill",
                                bill_id,
                                json.dumps({
                                    "subtotal": str(money(bill.get("subtotal") or 0)),
                                    "total_amount": str(money(bill.get("total_amount") or 0)),
                                }),
                                json.dumps({
                                    "subtotal": str(subtotal),
                                    "total_amount": str(total_amount),
                                    "updated_at": updated_totals["updated_at"].isoformat()
                                    if updated_totals.get("updated_at") else None,
                                }),
                                system_user_id,
                                "Bill totals recalculated after automated IP services were added.",
                            ),
                        )

                    summary["bills_processed"] += 1

        logger.info("Automated IP service run completed: %s", summary)
        return summary

    except Exception:
        logger.exception(
            "Automated IP service run failed for service_date=%s",
            service_date,
        )
        raise

    finally:
        if connection is not None:
            connection.close()


@celery_app.task(
    bind=True,
    name="tasks.run_automated_ip_billing",
    max_retries=5,
    default_retry_delay=60,
)
def run_automated_ip_billing(self, service_date=None):
    """
    Celery entry point.

    If no service_date is supplied, use today's date in Asia/Kolkata.
    The same service_date is retained when Celery retries the task.
    """
    if service_date is None:
        service_date = datetime.now(INDIA_TZ).date().isoformat()
    elif isinstance(service_date, date):
        service_date = service_date.isoformat()

    try:
        logger.info(
            "Starting automated IP services for service_date=%s",
            service_date,
        )

        result = run_automated_ip_billing_service(service_date)

        logger.info(
            "Automated IP services finished for service_date=%s: %s",
            service_date,
            result,
        )

        # Return the summary even if individual services failed pricing
        # validation; those failures are explicitly present in the result.
        return result

    except Exception as exc:
        logger.exception(
            "Celery task failed for service_date=%s",
            service_date,
        )

        raise self.retry(
            exc=exc,
            countdown=min(60 * (2 ** self.request.retries), 1800),
            kwargs={"service_date": service_date},
        )
