"""
sync.py — Sincroniza calendarios iCal de Airbnb y Booking con Google Calendar
y genera un archivo JSON público con las reservas para el panel compartido.
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
PROPERTIES = json.loads(os.environ["PROPERTIES_JSON"])
GOOGLE_CREDENTIALS = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
SYNC_WINDOW_DAYS = 180

COLORS = {
    "airbnb":  "11",
    "booking": "9",
    "block":   "8",
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
            "prop":     prop_name,
            "is_block": is_block,
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
        return []

    today     = datetime.now(timezone.utc).date()
    time_min  = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat()
    time_max  = (datetime.now(timezone.utc) + timedelta(days=SYNC_WINDOW_DAYS)).isoformat()

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

    current_uids = {e["uid"] for e in all_events}

    deleted = 0
    for uid, gcal_event in existing.items():
        if uid not in current_uids:
            service.events().delete(calendarId=calendar_id, eventId=gcal_event["id"]).execute()
            deleted += 1

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
    return [e for e in all_events if not e["is_block"]]


# ─── GENERAR JSON PÚBLICO ─────────────────────────────────────────────────────
def generate_public_json(all_reservations):
    """Genera docs/reservas.json para el panel público en GitHub Pages."""
    today = datetime.now(timezone.utc).date()
    window_end = today + timedelta(days=60)

    # Filtrar reservas de los próximos 60 días
    upcoming = []
    for ev in all_reservations:
        try:
            start = datetime.strptime(ev["start"], "%Y-%m-%d").date()
            end   = datetime.strptime(ev["end"],   "%Y-%m-%d").date()
            if end >= today and start <= window_end:
                upcoming.append({
                    "prop":     ev["prop"],
                    "platform": ev["platform"],
                    "start":    ev["start"],
                    "end":      ev["end"],
                    # Solo incluimos el nombre si no es un bloqueo
                    "guest":    ev["summary"].split(" — ")[0].replace(f"[{ev['platform'].upper()}] ", ""),
                })
        except Exception:
            continue

    os.makedirs("docs", exist_ok=True)
    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reservations": upcoming
    }
    with open("docs/reservas.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n📄 JSON público generado: {len(upcoming)} reservas en docs/reservas.json")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"🔄 Iniciando sincronización — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    service = get_calendar_service()
    all_reservations = []
    for prop in PROPERTIES:
        reservations = sync_property(service, prop)
        all_reservations.extend(reservations)
    generate_public_json(all_reservations)
    print("\n✅ Sincronización completada.")

if __name__ == "__main__":
    main()
