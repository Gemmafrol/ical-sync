"""
sync.py — Sincroniza calendarios iCal de Airbnb y Booking con Google Calendar
Se ejecuta automáticamente cada 10 minutos via GitHub Actions.
"""

import os
import json
import re
import requests
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
# Las URLs iCal y el JSON de credenciales se leen desde variables de entorno
# (configuradas como secrets en GitHub). Ver INSTRUCCIONES.md.

PROPERTIES = json.loads(os.environ["PROPERTIES_JSON"])
# Formato esperado:
# [
#   {
#     "name": "Apartamento Centro 1",
#     "airbnb_ical": "https://www.airbnb.es/calendar/ical/XXXX.ics",
#     "booking_ical": "https://ical.booking.com/v1/export?t=XXXX",
#     "calendar_id": "tu_email@gmail.com"  // o ID de un calendario secundario
#   }
# ]

GOOGLE_CREDENTIALS = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
# Contenido del archivo JSON de la cuenta de servicio de Google

SYNC_WINDOW_DAYS = 180  # Sincronizar reservas de los próximos 6 meses

# ─── COLORES EN GOOGLE CALENDAR ───────────────────────────────────────────────
COLORS = {
    "airbnb":  "11",  # Tomate (rojo)
    "booking": "9",   # Pavo real (azul)
    "block":   "8",   # Grafito
}

# ─── GOOGLE CALENDAR CLIENT ───────────────────────────────────────────────────
def get_calendar_service():
    creds = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDENTIALS,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)


# ─── PARSER ICAL ──────────────────────────────────────────────────────────────
def parse_ical(text, platform, prop_name):
    events = []
    blocks = text.split("BEGIN:VEVENT")
    for block in blocks[1:]:
        def get(key):
            m = re.search(rf"{key}[^:]*:([^\r\n]+)", block)
            return m.group(1).strip() if m else ""

        dtstart = get("DTSTART")
        dtend   = get("DTEND")
        summary = get("SUMMARY") or "Reserva"
        uid     = get("UID")

        if not dtstart or not dtend:
            continue

        def to_date(s):
            s = s[:8]
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

        is_block = any(w in summary.upper() for w in ["BLOCK", "BLOQUEADO", "NOT AVAILABLE", "AIRBNB"])
        color = COLORS["block"] if is_block else COLORS[platform]

        events.append({
            "uid":      uid,
            "summary":  f"[{platform.upper()}] {prop_name}" if is_block else f"{summary} — {prop_name}",
            "start":    to_date(dtstart),
            "end":      to_date(dtend),
            "color":    color,
            "platform": platform,
        })
    return events


# ─── DESCARGA ICAL ────────────────────────────────────────────────────────────
def fetch_ical(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ⚠️  Error descargando {url}: {e}")
        return None


# ─── SINCRONIZACIÓN CON GOOGLE CALENDAR ───────────────────────────────────────
def sync_property(service, prop):
    calendar_id = prop["calendar_id"]
    prop_name   = prop["name"]
    all_events  = []

    print(f"\n🏠 {prop_name}")

    for platform in ("airbnb", "booking"):
        ical_url = prop.get(f"{platform}_ical")
        if not ical_url:
            continue
        print(f"  ↓ Descargando {platform}...")
        text = fetch_ical(ical_url)
        if text:
            events = parse_ical(text, platform, prop_name)
            print(f"    → {len(events)} evento(s) encontrado(s)")
            all_events.extend(events)

    if not all_events:
        print("  ⚠️  Sin eventos, omitiendo.")
        return

    # Rango de fechas a sincronizar
    today     = datetime.now(timezone.utc).date()
    time_min  = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat()
    time_max  = (datetime.now(timezone.utc) + timedelta(days=SYNC_WINDOW_DAYS)).isoformat()

    # Obtener eventos existentes en Google Calendar
    existing = {}
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            privateExtendedProperty=[f"ical_source=true", f"prop={prop_name}"],
            pageToken=page_token,
            maxResults=500,
        ).execute()
        for e in resp.get("items", []):
            uid = e.get("extendedProperties", {}).get("private", {}).get("uid")
            if uid:
                existing[uid] = e
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # UIDs del iCal actual
    current_uids = {e["uid"] for e in all_events}

    # Eliminar eventos que ya no existen en el iCal
    deleted = 0
    for uid, gcal_event in existing.items():
        if uid not in current_uids:
            service.events().delete(calendarId=calendar_id, eventId=gcal_event["id"]).execute()
            deleted += 1

    # Crear o actualizar eventos
    created = updated = 0
    for ev in all_events:
        body = {
            "summary": ev["summary"],
            "start":   {"date": ev["start"]},
            "end":     {"date": ev["end"]},
            "colorId": ev["color"],
            "extendedProperties": {
                "private": {
                    "ical_source": "true",
                    "uid":         ev["uid"],
                    "platform":    ev["platform"],
                    "prop":        prop_name,
                }
            }
        }
        if ev["uid"] in existing:
            gcal_event = existing[ev["uid"]]
            # Solo actualiza si algo cambió
            if (gcal_event.get("start", {}).get("date") != ev["start"] or
                gcal_event.get("end",   {}).get("date") != ev["end"]):
                service.events().update(
                    calendarId=calendar_id,
                    eventId=gcal_event["id"],
                    body=body
                ).execute()
                updated += 1
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            created += 1

    print(f"  ✅ Creados: {created} | Actualizados: {updated} | Eliminados: {deleted}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"🔄 Iniciando sincronización — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    service = get_calendar_service()
    for prop in PROPERTIES:
        sync_property(service, prop)
    print("\n✅ Sincronización completada.")

if __name__ == "__main__":
    main()
