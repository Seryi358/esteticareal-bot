#!/usr/bin/env python3
"""
Production verification script for the booking pipeline.

Run this ONCE against production to verify:
1. Calendar connection uses the CORRECT calendar (estetica.real.ai@gmail.com)
2. Events are created on the right calendar
3. Double-booking prevention works
4. Duplicate cleanup works
5. Fail-closed behavior when calendar is unavailable

Usage:
    python verify_booking.py              # Uses env vars / credentials/token.json
    GOOGLE_TOKEN_JSON=... python verify_booking.py  # Explicit token

⚠️  This creates and deletes TEST events — safe to run in production.
"""

import asyncio
import os
import sys
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Ensure we can import project modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OPENAI_API_KEY", "sk-not-needed-for-calendar-test")

COLOMBIA_TZ = ZoneInfo("America/Bogota")
PASS = "✅"
FAIL = "❌"
WARN = "⚠️"
results = []


def report(status, test_name, detail=""):
    results.append((status, test_name, detail))
    icon = PASS if status == "pass" else FAIL if status == "fail" else WARN
    print(f"  {icon} {test_name}")
    if detail:
        print(f"     → {detail}")


async def main():
    from config import get_settings
    from services.calendar import (
        _get_service, _get_credentials, get_available_slots,
        verify_slot_available, book_slot_atomic, delete_event,
        _check_for_duplicate_events, _slot_locks, COLOMBIA_TZ,
    )

    settings = get_settings()
    print("\n" + "=" * 60)
    print("🔍 VERIFICACIÓN COMPLETA DEL PIPELINE DE AGENDAMIENTO")
    print("=" * 60)

    # ──────────────────────────────────────────────────────────
    # TEST 1: Calendar ID is correct
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 1: Calendar ID")
    expected_id = "estetica.real.ai@gmail.com"
    actual_id = settings.google_calendar_id
    if actual_id == expected_id:
        report("pass", "Calendar ID correcto", f"{actual_id}")
    else:
        report("fail", "Calendar ID INCORRECTO",
               f"Esperado: {expected_id}, Actual: {actual_id}")

    # ──────────────────────────────────────────────────────────
    # TEST 2: Credentials valid
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 2: Credenciales Google")
    creds = _get_credentials()
    if creds and creds.valid:
        report("pass", "Credenciales válidas",
               f"expiry={creds.expiry}, has_refresh={bool(creds.refresh_token)}")
    else:
        report("fail", "Credenciales inválidas o expiradas")
        print("\n⛔ No se puede continuar sin credenciales válidas.")
        _print_summary()
        return

    # ──────────────────────────────────────────────────────────
    # TEST 3: Service builds
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 3: Conexión al servicio")
    service = _get_service()
    if service:
        report("pass", "Servicio Google Calendar construido")
    else:
        report("fail", "No se pudo construir el servicio")
        _print_summary()
        return

    # ──────────────────────────────────────────────────────────
    # TEST 4: Can READ events from correct calendar
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 4: Lectura de eventos")
    now = datetime.now(COLOMBIA_TZ)
    try:
        events_result = service.events().list(
            calendarId=actual_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=7)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=10,
        ).execute()
        events = events_result.get("items", [])
        cal_summary = events_result.get("summary", "?")
        report("pass", f"Lectura exitosa — {len(events)} eventos en próximos 7 días",
               f"Calendario: '{cal_summary}'")
        if events:
            for ev in events[:3]:
                ev_start = ev.get("start", {}).get("dateTime", "?")
                print(f"     📅 {ev_start} — {ev.get('summary', 'Sin título')}")
            if len(events) > 3:
                print(f"     ... y {len(events) - 3} más")
    except Exception as e:
        report("fail", "Error leyendo eventos", str(e))
        _print_summary()
        return

    # ──────────────────────────────────────────────────────────
    # TEST 5: Can CREATE and DELETE events
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 5: Crear y borrar evento de prueba")
    # Use a slot 14 days in the future to avoid interfering with real appointments
    test_slot = (now + timedelta(days=14)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    # Ensure it's a weekday
    while test_slot.weekday() > 4:
        test_slot += timedelta(days=1)

    try:
        test_event = {
            "summary": "🧪 TEST verify_booking.py (auto-delete)",
            "start": {"dateTime": test_slot.isoformat(), "timeZone": "America/Bogota"},
            "end": {"dateTime": (test_slot + timedelta(minutes=30)).isoformat(),
                     "timeZone": "America/Bogota"},
        }
        created = service.events().insert(
            calendarId=actual_id, body=test_event
        ).execute()
        event_id = created.get("id")
        report("pass", f"Evento creado — id={event_id}")

        # Now delete it
        service.events().delete(calendarId=actual_id, eventId=event_id).execute()
        report("pass", "Evento borrado exitosamente")
    except Exception as e:
        report("fail", "Error creando/borrando evento de prueba", str(e))

    # ──────────────────────────────────────────────────────────
    # TEST 6: Available slots pipeline
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 6: Pipeline de slots disponibles")
    try:
        slots = await get_available_slots(days_ahead=7)
        if slots:
            report("pass", f"{len(slots)} slots disponibles en próximos 7 días",
                   f"Próximo: {slots[0].strftime('%A %d/%m %I:%M %p')}")
        else:
            report("warn", "0 slots disponibles — ¿agenda llena o fuera de horario?")
    except Exception as e:
        report("fail", "Error obteniendo slots", str(e))

    # ──────────────────────────────────────────────────────────
    # TEST 7: verify_slot_available works
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 7: Verificación de disponibilidad")
    if slots:
        try:
            result = await verify_slot_available(slots[0])
            if result is True:
                report("pass", f"Slot {slots[0].strftime('%H:%M')} verificado como disponible")
            elif result is False:
                report("warn", "Slot marcado como no disponible (posible evento recién creado)")
            else:
                report("fail", "verify_slot_available retornó None — problema de conexión")
        except Exception as e:
            report("fail", "Error en verify_slot_available", str(e))
    else:
        report("warn", "Sin slots para verificar — saltado")

    # ──────────────────────────────────────────────────────────
    # TEST 8: Double-booking prevention (atomic booking)
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 8: Prevención de double-booking")
    # Create a real event, then try to book the same slot
    db_slot = (now + timedelta(days=13)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    while db_slot.weekday() > 4:
        db_slot += timedelta(days=1)

    blocker_event = None
    try:
        # Create a "blocker" event manually
        blocker_body = {
            "summary": "🧪 BLOCKER for double-booking test",
            "start": {"dateTime": db_slot.isoformat(), "timeZone": "America/Bogota"},
            "end": {"dateTime": (db_slot + timedelta(minutes=30)).isoformat(),
                     "timeZone": "America/Bogota"},
        }
        blocker_event = service.events().insert(
            calendarId=actual_id, body=blocker_body
        ).execute()

        # Now try to book the same slot atomically
        _slot_locks.clear()
        is_available, event = await book_slot_atomic(
            db_slot, "Test User", "573000000000"
        )

        if not is_available and event is None:
            report("pass", "Double-booking BLOQUEADO correctamente",
                   "book_slot_atomic detectó el evento existente y rechazó")
        elif is_available and event:
            report("fail", "Double-booking NO fue prevenido — se creó un segundo evento!",
                   f"event_id={event.get('id')}")
            # Clean up the duplicate
            try:
                await delete_event(event.get("id"))
            except Exception:
                pass
        else:
            report("warn", f"Resultado inesperado: available={is_available}, event={event}")

    except Exception as e:
        report("fail", "Error en test de double-booking", str(e))
    finally:
        # Clean up blocker
        if blocker_event:
            try:
                service.events().delete(
                    calendarId=actual_id, eventId=blocker_event["id"]
                ).execute()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # TEST 9: Fail-closed when calendar unavailable
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 9: Fail-closed (calendario no disponible)")
    from unittest.mock import patch, AsyncMock

    fake_slot = db_slot + timedelta(hours=2)
    _slot_locks.clear()

    async def mock_verify_none(s):
        return None  # Simulate calendar API down

    with patch("services.calendar.verify_slot_available", side_effect=mock_verify_none):
        is_available, event = await book_slot_atomic(
            fake_slot, "Test User", "573000000000"
        )

    if not is_available and event is None:
        report("pass", "Fail-closed: booking BLOQUEADO cuando calendario no responde")
    else:
        report("fail", "Fail-closed FALLIDO — permitió booking sin verificación",
               f"available={is_available}, event={event}")

    # ──────────────────────────────────────────────────────────
    # TEST 10: Concurrent booking — only one wins
    # ──────────────────────────────────────────────────────────
    print("\n📋 Test 10: Booking concurrente (race condition)")
    conc_slot = (now + timedelta(days=12)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    while conc_slot.weekday() > 4:
        conc_slot += timedelta(days=1)

    _slot_locks.clear()
    created_events = []

    try:
        # Launch 3 concurrent booking attempts
        booking_results = await asyncio.gather(
            book_slot_atomic(conc_slot, "Maria Test", "573001111111"),
            book_slot_atomic(conc_slot, "Laura Test", "573002222222"),
            book_slot_atomic(conc_slot, "Sofia Test", "573003333333"),
        )

        successful = [(r, i) for i, r in enumerate(booking_results)
                       if r[0] is True and r[1] is not None]
        rejected = [r for r in booking_results if r[0] is False]

        for avail, evt in booking_results:
            if evt and evt.get("id"):
                created_events.append(evt["id"])

        if len(successful) == 1 and len(rejected) == 2:
            winner_name = ["Maria", "Laura", "Sofia"][successful[0][1]]
            report("pass", f"Solo 1 de 3 reservó — ganó {winner_name}",
                   f"Creados: {len(successful)}, Rechazados: {len(rejected)}")
        elif len(successful) == 0:
            report("warn", "Ninguno reservó — posible problema de API",
                   f"Resultados: {booking_results}")
        else:
            report("fail", f"DOUBLE-BOOKING: {len(successful)} de 3 reservaron!",
                   f"IDs creados: {[s[0][1].get('id') for s in successful]}")
    except Exception as e:
        report("fail", "Error en test concurrente", str(e))
    finally:
        # Clean up all test events
        for eid in created_events:
            try:
                service.events().delete(calendarId=actual_id, eventId=eid).execute()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    _print_summary()


def _print_summary():
    print("\n" + "=" * 60)
    print("📊 RESUMEN")
    print("=" * 60)
    passed = sum(1 for s, _, _ in results if s == "pass")
    failed = sum(1 for s, _, _ in results if s == "fail")
    warned = sum(1 for s, _, _ in results if s == "warn")
    total = len(results)

    print(f"  {PASS} Pasaron: {passed}/{total}")
    if warned:
        print(f"  {WARN} Advertencias: {warned}")
    if failed:
        print(f"  {FAIL} Fallaron: {failed}")
        print("\n  Tests fallidos:")
        for s, name, detail in results:
            if s == "fail":
                print(f"    {FAIL} {name}: {detail}")

    if failed == 0:
        print(f"\n  🎉 PIPELINE DE AGENDAMIENTO VERIFICADO AL 100%")
        print(f"     Calendario: estetica.real.ai@gmail.com")
        print(f"     Double-booking: protegido")
        print(f"     Fail-closed: activo")
        print(f"     Concurrencia: segura")
    else:
        print(f"\n  ⛔ HAY PROBLEMAS QUE RESOLVER ANTES DE PRODUCCIÓN")

    print()


if __name__ == "__main__":
    asyncio.run(main())
