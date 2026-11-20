#!/usr/bin/env python3
"""
SportMember-Platzwaechter
=========================

Prueft regelmaessig, ob bei ausgebuchten Trainings auf sportmember.ch ein Platz
frei wird, und schickt dann eine Push-Nachricht ueber ntfy (https://ntfy.sh)
aufs Handy. Gebucht wird NICHT automatisch. Braucht nur Python 3.

Zwei Betriebsarten
------------------
1) Auf GitHub Actions (empfohlen): siehe .github/workflows/platzwaechter.yml.
   Zugangsdaten kommen aus den GitHub-Secrets SM_USER, SM_PASS, NTFY_TOPIC.

2) Lokal am Computer:
   python3 sportmember_watch.py 57121432 --topic MEIN-NTFY-THEMA --until 2026-11-30T12:00
   (fragt Benutzername und Passwort beim Start ab)

Die Zahl ist die Aktivitaets-ID aus der URL der Trainingsseite
(https://www.sportmember.ch/team/activities/<ID>). Mehrere IDs sind moeglich.
"""

import argparse
import datetime as dt
import getpass
import html as htmllib
import http.cookiejar
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://www.sportmember.ch"
TZ = ZoneInfo("Europe/Zurich")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(msg):
    print(f"[{dt.datetime.now(TZ):%d.%m. %H:%M:%S}] {msg}", flush=True)


class SessionExpired(Exception):
    pass


class LoginFailed(Exception):
    pass


class SportMember:
    def __init__(self, user, password):
        self.user, self.password = user, password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", UA),
                                  ("Accept-Language", "de-CH,de;q=0.9")]

    def _open(self, path, data=None, headers=None):
        req = urllib.request.Request(BASE + path, data=data, headers=headers or {})
        with self.opener.open(req, timeout=30) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    def login(self):
        _, page = self._open("/sign_in")
        m = (re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', page)
             or re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', page))
        if not m:
            raise LoginFailed("Login-Formular nicht gefunden (Seite hat sich evtl. geaendert).")
        data = urllib.parse.urlencode({
            "utf8": "✓",
            "authenticity_token": htmllib.unescape(m.group(1)),
            "session[username]": self.user,
            "session[password]": self.password,
            "commit": "Einloggen",
        }).encode()
        final_url, _ = self._open("/sessions", data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE, "Referer": BASE + "/sign_in"})
        if "sign_in" in final_url or final_url.rstrip("/").endswith("/sessions"):
            raise LoginFailed("Login fehlgeschlagen - Benutzername/Passwort pruefen.")
        log("Eingeloggt.")

    def activities(self, days_ahead=35):
        start = (dt.datetime.now(TZ) - dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=days_ahead)
        q = urllib.parse.urlencode({
            "team_ids": "", "event_type_ids": "", "attendance_status_ids": "",
            "appvagt_up_for_graps": "", "appvagt_show_available": "",
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "timeZone": "Europe/Zurich",
        })
        url, body = self._open("/team/activities.json?" + q, headers={
            "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        if "sign_in" in url:
            raise SessionExpired()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise SessionExpired()


def parse_limits(a):
    """'<i ...></i>24/24' -> (24, 24)"""
    m = re.search(r"(\d+)\s*/\s*(\d+)", a.get("participantLimits") or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def has_free_spot(a):
    if a.get("cancelled"):
        return False
    taken, limit = parse_limits(a)
    if taken is not None and limit:
        return taken < limit
    return not a.get("fully_booked", True)


def places(a):
    taken, limit = parse_limits(a)
    return f"{taken}/{limit}" if taken is not None else "?"


def describe(a):
    when = dt.datetime.fromisoformat(a["start"]).astimezone(TZ)
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][when.weekday()]
    return f"{a['title']} ({wd} {when:%d.%m. %H:%M}) - {places(a)}"


def parse_local(s):
    """'2026-11-30T12:00' -> Zeitpunkt in Schweizer Zeit (auch auf UTC-Servern)."""
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=TZ)


def notify(topic, title, message, click=None, priority=4, tags=None):
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # Benachrichtigung darf das Skript nicht abstuerzen lassen
        log(f"Benachrichtigung fehlgeschlagen: {e}")


def main():
    p = argparse.ArgumentParser(description="Meldet freie Plaetze bei SportMember-Trainings.")
    p.add_argument("ids", nargs="+", type=int, help="Aktivitaets-IDs")
    p.add_argument("--topic", default=os.environ.get("NTFY_TOPIC"),
                   help="ntfy-Thema (oder Umgebungsvariable NTFY_TOPIC)")
    p.add_argument("--interval", type=int, default=180, help="Sekunden zwischen Abfragen (Standard 180)")
    p.add_argument("--until", help="Stoppen ab diesem Zeitpunkt (Schweizer Zeit), z. B. 2026-11-30T12:00")
    p.add_argument("--max-minutes", type=int, help="Nach so vielen Minuten beenden (fuer GitHub Actions)")
    p.add_argument("--ci", action="store_true",
                   help="Server-Modus: keine Eingabeabfragen, knappe Protokolle, keine Wiederholungs-Pushes")
    p.add_argument("--test", action="store_true", help="Einmal pruefen + Testnachricht senden, dann beenden")
    args = p.parse_args()

    if not args.topic:
        p.error("ntfy-Thema fehlt (--topic oder NTFY_TOPIC).")
    ids = set(args.ids)
    until = parse_local(args.until) if args.until else None
    stop_at = (time.monotonic() + args.max_minutes * 60) if args.max_minutes else None

    if until and dt.datetime.now(TZ) >= until and not args.test:
        log("Anmeldefrist ist vorbei - nichts zu tun.")
        return

    user = os.environ.get("SM_USER")
    password = os.environ.get("SM_PASS")
    if args.ci and not (user and password):
        log("SM_USER oder SM_PASS fehlt - bitte die GitHub-Secrets pruefen.")
        sys.exit(1)
    user = user or input("SportMember-Benutzername/E-Mail: ").strip()
    password = password or getpass.getpass("SportMember-Passwort: ")

    sm = SportMember(user, password)
    try:
        sm.login()
    except LoginFailed as e:
        log(str(e))
        notify(args.topic, "Platzwaechter: Login klappt nicht", str(e), priority=3, tags=["warning"])
        sys.exit(1)

    last_free = {}       # id -> bool
    errors_in_row = 0

    while True:
        now = dt.datetime.now(TZ)
        if until and now >= until and not args.test:
            log("Anmeldefrist erreicht - Ende.")
            if not args.ci:
                notify(args.topic, "Platzwaechter beendet", "Anmeldefrist erreicht.", priority=2)
            return
        try:
            try:
                acts = sm.activities()
            except SessionExpired:
                log("Sitzung abgelaufen - logge neu ein.")
                sm.login()
                acts = sm.activities()
            errors_in_row = 0

            by_id = {a["id"]: a for a in acts}
            for aid in sorted(ids):
                a = by_id.get(aid)
                if not a:
                    log(f"{aid}: nicht im Kalender gefunden.")
                    continue
                url = BASE + a["url"]
                if a.get("attending"):
                    log(f"{aid}: du bist angemeldet - beobachte nicht mehr.")
                    if not args.ci:
                        notify(args.topic, "Du bist angemeldet", describe(a), click=url,
                               priority=3, tags=["white_check_mark"])
                    ids.discard(aid)
                    continue
                if dt.datetime.fromisoformat(a["start"]) <= now:
                    log(f"{aid}: vorbei.")
                    ids.discard(aid)
                    continue

                free = has_free_spot(a)
                log(f"{aid}: {'FREI' if free else 'voll'} ({places(a)})" if args.ci
                    else ("FREI   " if free else "voll   ") + describe(a))
                if free and not last_free.get(aid, False):
                    notify(args.topic, "Platz frei - schnell anmelden!", describe(a),
                           click=url, priority=5, tags=["rotating_light"])
                last_free[aid] = free

            if args.test:
                notify(args.topic, "Platzwaechter: Test",
                       "Verbindung klappt. So sieht eine Nachricht aus.",
                       click=BASE + "/activities", priority=3, tags=["tada"])
                log("Testnachricht gesendet. Ende.")
                return
            if not ids:
                log("Nichts mehr zu beobachten - Ende.")
                return

        except LoginFailed as e:
            log(str(e))
            notify(args.topic, "Platzwaechter: Login klappt nicht", str(e), priority=3, tags=["warning"])
            sys.exit(1)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            errors_in_row += 1
            log(f"Fehler ({errors_in_row}x hintereinander): {e}")
            if errors_in_row == 5:
                notify(args.topic, "Platzwaechter hat Probleme",
                       f"Seit 5 Versuchen Fehler: {e}", priority=3, tags=["warning"])

        wait = args.interval + random.randint(-20, 20)
        if stop_at and time.monotonic() + wait >= stop_at:
            log("Laufzeit fuer diesen Durchgang erreicht - der naechste startet automatisch.")
            return
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBeendet.")
