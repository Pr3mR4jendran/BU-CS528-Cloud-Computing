"""Translate the instructor client's headers into nullable SQL fields."""
from datetime import datetime, timezone
import ipaddress
from urllib.parse import unquote, urlsplit

from service import FORBIDDEN_COUNTRIES

AGE_GROUPS = frozenset(("0-16", "17-25", "26-35", "36-45", "46-55", "56-65", "66-75", "76+"))
INCOME_GROUPS = frozenset(("0-10k", "10k-20k", "20k-40k", "40k-60k", "60k-100k", "100k-150k", "150k-250k", "250k+"))


def address(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def extract_record(service, method, target, headers, body, peer_ip, response, received_at=None):
    headers = {name.casefold(): value.strip() for name, value in headers.items()}
    country = " ".join(headers.get("x-country", "").split())[:128] or None
    gender = {"male": "Male", "female": "Female"}.get(headers.get("x-gender", "").casefold())
    age = headers.get("x-age")
    income = headers.get("x-income")
    age = age if age in AGE_GROUPS else None
    income = income if income in INCOME_GROUPS else None
    try:
        client_time = datetime.strptime(headers.get("x-time", ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        client_time = None
    try:
        path = urlsplit(target).path
        requested = (service.post_filename(body, headers.get("content-type", ""))
                     if method == "POST" else unquote(path, errors="strict"))
        requested_file = service.object_name(requested)
    except (ValueError, UnicodeError):
        path = target
        requested_file = target
    client_ip = address(headers.get("x-client-ip", ""))
    complete = all((country, client_ip, gender, age, income, client_time))
    if path == "/__hw5/experiment":
        category = "control"
    elif path == "/healthz":
        category = "health"
    elif complete and peer_ip in service.config.get("workloadPeerIps", []):
        category = "workload"
    elif complete:
        category = "development"
    else:
        category = "other"
    received_at = received_at or datetime.now(timezone.utc)
    return dict(request_id=response.request_id, request_time=received_at.astimezone(timezone.utc).replace(tzinfo=None),
                country=country, client_ip=client_ip, gender=gender, age=age, income=income,
                is_banned=bool(country and country.casefold() in FORBIDDEN_COUNTRIES),
                time_of_day=client_time.time() if client_time else None, client_time=client_time,
                requested_file=requested_file, status_code=response.status, peer_ip=address(peer_ip),
                method=method[:16], run_id=service.config.get("runId", "development"), traffic_category=category)
