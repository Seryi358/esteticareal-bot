"""
Diagnostic script for Google Calendar connection.

Run inside the production container or locally with credentials:
    python diagnose_calendar.py

Tests every step of the calendar pipeline and reports exactly what fails.
"""

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

COLOMBIA_TZ = ZoneInfo("America/Bogota")

# Colors for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg):
    print(f"  {RED}✗{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠{RESET} {msg}")


def header(msg):
    print(f"\n{BOLD}{'─' * 50}")
    print(f"  {msg}")
    print(f"{'─' * 50}{RESET}")


def main():
    errors = []

    # ── Step 1: Environment ──
    header("1. Environment Variables")

    token_b64 = os.environ.get("GOOGLE_TOKEN_JSON", "")
    if token_b64:
        ok(f"GOOGLE_TOKEN_JSON is set ({len(token_b64)} chars)")
        try:
            token_data = base64.b64decode(token_b64).decode()
            token_json = json.loads(token_data)
            ok(f"  Decodes to valid JSON")

            has_refresh = bool(token_json.get("refresh_token"))
            has_client_id = bool(token_json.get("client_id"))
            has_client_secret = bool(token_json.get("client_secret"))
            has_token = bool(token_json.get("token"))

            if has_refresh:
                ok(f"  Has refresh_token")
            else:
                fail(f"  MISSING refresh_token — token cannot be renewed!")
                errors.append("No refresh_token in GOOGLE_TOKEN_JSON")

            if has_client_id and has_client_secret:
                ok(f"  Has client_id and client_secret")
            else:
                fail(f"  Missing client_id or client_secret")
                errors.append("Missing client_id/client_secret")

            if has_token:
                ok(f"  Has access token")
            else:
                warn(f"  No access token (will need refresh)")

        except Exception as e:
            fail(f"  Cannot decode: {e}")
            errors.append(f"Cannot decode GOOGLE_TOKEN_JSON: {e}")
    else:
        warn("GOOGLE_TOKEN_JSON is NOT set (checking token.json file...)")

    token_path = os.path.join(
        os.environ.get("CREDENTIALS_DIR", "credentials"), "token.json"
    )
    if os.path.exists(token_path):
        ok(f"token.json exists at {token_path}")
        try:
            with open(token_path) as f:
                tj = json.load(f)
            if tj.get("refresh_token"):
                ok(f"  token.json has refresh_token")
            else:
                fail(f"  token.json has NO refresh_token")
                errors.append("token.json missing refresh_token")
        except Exception as e:
            fail(f"  Cannot read token.json: {e}")
    elif not token_b64:
        fail(f"No token.json at {token_path} and no GOOGLE_TOKEN_JSON env var")
        errors.append("No credentials found at all")

    cal_id = os.environ.get("GOOGLE_CALENDAR_ID", "estetica.real.ai@gmail.com")
    ok(f"GOOGLE_CALENDAR_ID = {cal_id}")

    # ── Step 2: Credentials ──
    header("2. Google Credentials")

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        ok("google-auth library imported")
    except ImportError:
        fail("google-auth NOT installed")
        errors.append("google-auth not installed")
        _print_summary(errors)
        return

    try:
        from services.calendar import _get_credentials, SCOPES

        creds = _get_credentials()
        if creds is None:
            fail("_get_credentials() returned None — credentials broken")
            errors.append("_get_credentials() returns None")
        elif not creds.valid:
            fail(f"Credentials exist but are NOT valid (expired={creds.expired})")
            errors.append("Credentials invalid")
        else:
            ok(f"Credentials valid!")
            ok(f"  Token expires: {creds.expiry}")
            if creds.refresh_token:
                ok(f"  Has refresh_token for renewal")
            else:
                warn(f"  No refresh_token — will break when token expires")
    except Exception as e:
        fail(f"Error getting credentials: {e}")
        errors.append(f"Credential error: {e}")
        _print_summary(errors)
        return

    # ── Step 3: Calendar Service ──
    header("3. Calendar Service")

    try:
        from services.calendar import _get_service

        service = _get_service()
        if service is None:
            fail("_get_service() returned None")
            errors.append("Calendar service is None")
            _print_summary(errors)
            return
        ok("Calendar service built successfully")
    except Exception as e:
        fail(f"Error building service: {e}")
        errors.append(f"Service build error: {e}")
        _print_summary(errors)
        return

    # ── Step 4: List Events (read test) ──
    header("4. Read Test — List Events")

    try:
        now = datetime.now(COLOMBIA_TZ)
        end = now + timedelta(days=3)
        events_result = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=10,
            )
            .execute()
        )
        events = events_result.get("items", [])
        ok(f"Successfully read calendar — {len(events)} events in next 3 days")
        for ev in events:
            ev_start = ev.get("start", {}).get(
                "dateTime", ev.get("start", {}).get("date", "?")
            )
            summary = ev.get("summary", "(sin título)")
            print(f"       📅 {ev_start} — {summary}")
    except Exception as e:
        fail(f"Cannot read events: {e}")
        errors.append(f"Read events failed: {e}")

    # ── Step 5: Write Test — Create + Delete test event ──
    header("5. Write Test — Create & Delete Event")

    try:
        test_time = now + timedelta(days=14, hours=3)
        test_time = test_time.replace(minute=0, second=0, microsecond=0)
        test_end = test_time + timedelta(minutes=30)

        test_event = {
            "summary": "🔧 TEST — Diagnóstico del bot (borrar automáticamente)",
            "description": "Evento de prueba creado por diagnose_calendar.py. Se borra solo.",
            "start": {"dateTime": test_time.isoformat(), "timeZone": "America/Bogota"},
            "end": {"dateTime": test_end.isoformat(), "timeZone": "America/Bogota"},
        }

        created = (
            service.events()
            .insert(calendarId=cal_id, body=test_event)
            .execute()
        )
        event_id = created.get("id")
        ok(f"Event CREATED: id={event_id}")
        ok(f"  Summary: {created.get('summary')}")
        ok(f"  Start: {created.get('start', {}).get('dateTime')}")

        # Now delete it
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        ok(f"Event DELETED successfully")
        ok(f"  Calendar read+write is FULLY WORKING ✓")

    except Exception as e:
        fail(f"Write test failed: {e}")
        errors.append(f"Write test failed: {e}")

    # ── Step 6: Async booking pipeline test ──
    header("6. Booking Pipeline Test (async)")

    async def _test_booking_pipeline():
        try:
            from services.calendar import get_available_slots, format_slots_for_whatsapp

            slots = await get_available_slots(days_ahead=7)
            if slots:
                ok(f"get_available_slots() returned {len(slots)} slots")
                formatted = format_slots_for_whatsapp(slots)
                ok(f"  Formatted: {formatted[:100]}...")

                # Test verify_slot_available on the first slot
                from services.calendar import verify_slot_available

                first_slot = slots[0]
                result = await verify_slot_available(first_slot)
                if result is True:
                    ok(f"  verify_slot_available({first_slot.isoformat()}) = True (free)")
                elif result is False:
                    warn(f"  verify_slot_available({first_slot.isoformat()}) = False (taken)")
                elif result is None:
                    fail(f"  verify_slot_available() = None (calendar unavailable!)")
                    errors.append("verify_slot_available returns None")
            else:
                warn("get_available_slots() returned 0 slots (calendar empty or all booked)")
        except Exception as e:
            fail(f"Booking pipeline error: {e}")
            errors.append(f"Booking pipeline: {e}")

    asyncio.run(_test_booking_pipeline())

    # ── Summary ──
    _print_summary(errors)


def _print_summary(errors):
    header("DIAGNOSIS RESULT")
    if not errors:
        print(f"\n  {GREEN}{BOLD}ALL TESTS PASSED ✓{RESET}")
        print(f"  Calendar connection is healthy. Events will be created correctly.\n")
    else:
        print(f"\n  {RED}{BOLD}FOUND {len(errors)} PROBLEM(S):{RESET}")
        for i, err in enumerate(errors, 1):
            print(f"  {RED}{i}. {err}{RESET}")
        print()
        print(f"  {YELLOW}Most common fixes:{RESET}")
        print(f"    1. Re-run setup_calendar.py to generate fresh token")
        print(f"    2. Convert token.json to base64: base64 -w0 credentials/token.json")
        print(f"    3. Set GOOGLE_TOKEN_JSON env var in EasyPanel with that base64")
        print(f"    4. If in 'Testing' mode on Google Cloud Console, publish the app")
        print()


if __name__ == "__main__":
    main()
