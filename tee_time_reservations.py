#!/usr/bin/env python3
"""
tee_time_bot.py — Reserve golf tee times via RCGS API.

Features
- Earliest or "closest at/after target" slot selection
- Optional sleep-until (e.g., 09:00 in a timezone) and timed polling
- Retries with exponential backoff
- Dry-run mode prints the selected slot without booking
- Token via --token or GOLF_API_TOKEN env var
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import json
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Any, Tuple
from zoneinfo import ZoneInfo

import requests


# ---------- HTTP helpers ----------

def post(base_url: str, path: str, payload: dict, timeout: float = 5.0) -> requests.Response:
    url = base_url.rstrip("/") + path
    # The API accepts JSON without custom headers.
    return requests.post(url, json=payload, timeout=timeout)


# ---------- Domain logic ----------

def to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return 60 * h + m


def get_availability(
    base_url: str,
    token: str,
    for_date: str,
    recorrido: str,
    players: int,
    filtro_hora: Optional[str] = None,
    *,
    timeout: float = 5.0,
) -> List[Dict[str, Any]]:
    payload = {
        "Token": token,
        "FiltroFecha": for_date,                 # "YYYY-MM-DD"
        "FiltroRecorrido": recorrido,            # e.g., "18 HOYOS"
        "FiltroJugadores": int(players),
        "FiltroHora": "08:00"
    }
    # Pass FiltroHora only if provided (API may treat empty differently)
    if filtro_hora:
        payload["FiltroHora"] = filtro_hora
    
    print(payload)

    r = post(base_url, "/api/GolfHorasLeer", payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    # Expect list of {"Fecha","Hora","Recorrido","NumeroJugadoresMaximo"}
    # Filter by capacity; sort by time of day.
    avails = [x for x in data if x.get("NumeroJugadoresMaximo", 4) >= int(players)]
    avails.sort(key=lambda a: to_minutes(a["Hora"]))
    return avails


def pick_slot(
    avails: List[Dict[str, Any]],
    mode: str,
    target_time: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    mode:
      - 'earliest': absolute earliest by time-of-day
      - 'closest': choose the time >= target_time (not before). If none, return None.
    """
    if not avails:
        return None

    if mode == "earliest" or not target_time:
        return avails[0]

    if mode == "closest":
        t0 = to_minutes(target_time)
        candidates = [a for a in avails if to_minutes(a["Hora"]) >= t0]
        if not candidates:
            return None
        return candidates[0]

    raise ValueError(f"Unknown mode: {mode}")


def reserve(
    base_url: str,
    token: str,
    fecha: str,
    hora: str,
    recorrido: str,
    players: int,
    *,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    payload = {
        "Token": token,
        "NumeroJugadores": int(players),
        "Fecha": fecha,
        "Hora": hora,
        "Recorrido": recorrido,
    }
    r = post(base_url, "/api/GolfReservaAlta", payload, timeout=timeout)
    r.raise_for_status()
    return r.json()  # expects {"CodigoReserva": ...}


# ---------- Scheduling / polling ----------

def sleep_until(target_dt: datetime) -> None:
    while True:
        now = datetime.now(target_dt.tzinfo)
        delta = (target_dt - now).total_seconds()
        if delta <= 0:
            return
        # Sleep in small-ish chunks so Ctrl+C remains responsive
        time.sleep(min(delta, 0.5))


def poll_for_slot(
    base_url: str,
    token: str,
    the_date: str,
    recorrido: str,
    players: int,
    filtro_hora: Optional[str],
    mode: str,
    target_time: Optional[str],
    *,
    poll_every: float,
    max_wait_seconds: float,
    timeout: float = 5.0,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Repeatedly fetch availability until a valid selection is possible or time runs out.
    Returns (picked_slot, last_avails).
    """
    deadline = time.time() + max_wait_seconds
    backoff = 0.0  # extra delay after transient HTTP errors
    last_avails: List[Dict[str, Any]] = []

    while True:
        try:
            avails = get_availability(
                base_url, token, the_date, recorrido, players, filtro_hora, timeout=timeout
            )
            last_avails = avails
            pick = pick_slot(avails, mode, target_time)
            if pick is not None:
                return pick, avails
            # No acceptable slot yet; continue polling
            # (e.g., "closest" after a specific time)
        except requests.RequestException as e:
            # Network or server error — short exponential backoff
            backoff = min(2.0 if backoff == 0.0 else backoff * 2.0, 8.0)
            print(f"[warn] availability fetch error: {e}. Backing off {backoff:.1f}s", flush=True)
            time.sleep(backoff)

        if time.time() >= deadline:
            return None, last_avails

        time.sleep(poll_every)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    tz_default = "Europe/Madrid"  # adjust if you prefer another default
    today = date.today().isoformat()

    p = argparse.ArgumentParser(
        description="Reserve a tee time via RCGS API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", default="https://rcgs.abiz.es:8228", help="API base URL")
    p.add_argument("--token", help="API token (or set env GOLF_API_TOKEN)")
    p.add_argument("--date", default=today, help="Booking date YYYY-MM-DD")
    p.add_argument("--recorrido", default="18 HOYOS", help="Course / recorrido")
    p.add_argument("--players", type=int, default=2, help="Number of players")
    p.add_argument("--mode", choices=["earliest", "closest"], default="earliest",
                   help="Slot selection strategy")
    p.add_argument("--target-time", default=None,
                   help="HH:MM used with mode=closest (selects at/after target, never before)")
    p.add_argument("--filtro-hora", default=None,
                   help="Optional FiltroHora for availability (HH:MM); omit to get full list")
    p.add_argument("--dry-run", action="store_true", help="Do not submit reservation")

    # Timing controls
    p.add_argument("--wait-until", default=None,
                   help="Local clock time to start (HH:MM). Can be paired with --tz")
    p.add_argument("--tz", default=tz_default,
                   help="Time zone for --wait-until (IANA name, e.g., Europe/Madrid or America/New_York)")
    p.add_argument("--poll-every", type=float, default=0.25,
                   help="Polling cadence in seconds once started")
    p.add_argument("--max-wait", type=float, default=180.0,
                   help="Max seconds to wait for an acceptable slot after start")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="HTTP timeout per request (seconds)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    token = args.token or os.environ.get("GOLF_API_TOKEN")
    if not token:
        print("Token is required: pass --token or set env GOLF_API_TOKEN", file=sys.stderr)
        return 2

    # Optional sleep-until (e.g., 09:00 local in a given TZ)
    if args.wait_until:
        tz = ZoneInfo(args.tz)
        now = datetime.now(tz)
        hh, mm = map(int, args.wait_until.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # next day if already past
        print(f"[info] waiting until {target.isoformat()} in {args.tz}", flush=True)
        sleep_until(target)

    # Poll for availability and pick a slot according to mode/target.
    print(f"[info] querying availability date={args.date} recorrido='{args.recorrido}' players={args.players}", flush=True)
    pick, avails = poll_for_slot(
        base_url=args.base_url,
        token=token,
        the_date=args.date,
        recorrido=args.recorrido,
        players=args.players,
        filtro_hora=args.filtro_hora,
        mode=args.mode,
        target_time=args.target_time,
        poll_every=args.poll_every,
        max_wait_seconds=args.max_wait,
        timeout=args.timeout,
    )

    # Pretty print what we saw
    def fmt_slot(s: Dict[str, Any]) -> str:
        return f"{s.get('Fecha')} {s.get('Hora')} | {s.get('Recorrido')} | max={s.get('NumeroJugadoresMaximo')}"

    if avails:
        preview = ", ".join(fmt_slot(s) for s in avails[:5])
        overflow = "" if len(avails) <= 5 else f" (+{len(avails)-5} more)"
        print(f"[info] top availability: {preview}{overflow}", flush=True)
    else:
        print("[info] no availability returned", flush=True)

    if not pick:
        print("[result] no acceptable slot found within max-wait window", flush=True)
        return 1

    print(f"[pick] {fmt_slot(pick)}", flush=True)

    if args.dry_run:
        print("[result] dry-run enabled — not booking", flush=True)
        return 0

    # Attempt reservation
    try:
        resp = reserve(
            base_url=args.base_url,
            token=token,
            fecha=pick["Fecha"],
            hora=pick["Hora"],
            recorrido=pick["Recorrido"],
            players=args.players,
            timeout=args.timeout,
        )
        # Minimal structured result to stdout for easy piping
        print("[result] reservation created")
        print(json.dumps(resp, ensure_ascii=False))
        return 0
    except requests.HTTPError as e:
        # Show API error content to aid troubleshooting
        detail = ""
        try:
            detail = f" body={e.response.text}"
        except Exception:
            pass
        print(f"[error] reservation failed: {e}{detail}", file=sys.stderr)
        return 3
    except requests.RequestException as e:
        print(f"[error] network error during reservation: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
