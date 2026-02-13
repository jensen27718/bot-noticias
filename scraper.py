import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


DEFAULT_STATE_FILE = "state/seen_news.json"
DEFAULT_INITIAL_SEND_COUNT = 5
DEFAULT_MAX_SEEN_URLS = 1000
DEFAULT_REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    state_file: Path
    initial_send_count: int
    max_seen_urls: int
    request_timeout: int
    dry_run: bool
    enabled_sources: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    url: str


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    published_at: str | None
    source_key: str
    source_name: str


SOURCES: tuple[Source, ...] = (
    Source(
        key="cucuta",
        name="Alcaldia de Cucuta - Ultimas noticias",
        url="https://cucuta.gov.co/ultimas-noticias/",
    ),
    Source(
        key="mintic_convocatorias",
        name="MinTIC - Convocatorias",
        url="https://www.mintic.gov.co/portal/inicio/Sala-de-prensa/Convocatorias/",
    ),
    Source(
        key="mintic_noticias",
        name="MinTIC - Noticias",
        url="https://www.mintic.gov.co/portal/inicio/Sala-de-prensa/Noticias/",
    ),
)
SOURCES_BY_KEY = {source.key: source for source in SOURCES}
MINTIC_ARTICLE_PATTERN = re.compile(r"/Sala-de-prensa/Noticias/\d+:")
MINTIC_ARTICLE_ID_PATTERN = re.compile(r"/Noticias/(\d+):")
MINTIC_AID_CLASS_PATTERN = re.compile(r"^aid-(\d+)$")


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    full_message = f"[{now}] {message}"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_message = full_message.encode(encoding, errors="replace").decode(
        encoding, errors="replace"
    )
    print(safe_message)


def normalize_text(raw: str) -> str:
    return " ".join(raw.split())


def parse_int_env(name: str, default_value: int) -> int:
    raw = os.getenv(name, str(default_value)).strip()
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        log(f"Valor invalido para {name}='{raw}'. Usando {default_value}.")
        return default_value


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    state_file = Path(os.getenv("STATE_FILE", DEFAULT_STATE_FILE).strip())
    initial_send_count = parse_int_env("INITIAL_SEND_COUNT", DEFAULT_INITIAL_SEND_COUNT)
    max_seen_urls = parse_int_env("MAX_SEEN_URLS", DEFAULT_MAX_SEEN_URLS)
    request_timeout = parse_int_env("REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
    dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    enabled_sources = parse_enabled_sources(os.getenv("ENABLED_SOURCES", "").strip())

    if not token and not dry_run:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno.")
    if not chat_id and not dry_run:
        raise RuntimeError("Falta TELEGRAM_CHAT_ID en variables de entorno.")

    return Config(
        bot_token=token,
        chat_id=chat_id,
        state_file=state_file,
        initial_send_count=initial_send_count,
        max_seen_urls=max_seen_urls,
        request_timeout=request_timeout,
        dry_run=dry_run,
        enabled_sources=enabled_sources,
    )


def parse_enabled_sources(raw_value: str) -> tuple[str, ...]:
    if not raw_value:
        return tuple(source.key for source in SOURCES)

    requested = [value.strip().lower() for value in raw_value.split(",") if value.strip()]
    unique_requested: list[str] = []
    for key in requested:
        if key not in unique_requested:
            unique_requested.append(key)

    unknown = [key for key in unique_requested if key not in SOURCES_BY_KEY]
    if unknown:
        valid = ", ".join(sorted(SOURCES_BY_KEY))
        unknown_text = ", ".join(unknown)
        raise RuntimeError(
            f"ENABLED_SOURCES contiene valores no validos: {unknown_text}. "
            f"Opciones: {valid}."
        )

    if not unique_requested:
        return tuple(source.key for source in SOURCES)

    return tuple(unique_requested)


def fetch_news_cucuta(source: Source, timeout_seconds: int) -> list[NewsItem]:
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(source.url, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()

    # Use bytes so BeautifulSoup can detect the correct encoding from meta tags.
    soup = BeautifulSoup(response.content, "html.parser")
    posts = soup.select("div.post.type-post, div.post")
    if not posts:
        posts = soup.select("article.post")

    seen_urls: set[str] = set()
    items: list[NewsItem] = []

    for post in posts:
        title_link = post.select_one(
            "h1 a, h2 a, h3 a, .elementor-heading-title a, a[rel='bookmark']"
        )
        if not title_link:
            continue

        title = normalize_text(title_link.get_text(" ", strip=True))
        href = (title_link.get("href") or "").strip()
        if not title or not href:
            continue

        full_url = urljoin(source.url, href)
        if full_url in seen_urls:
            continue

        date_node = post.select_one("time, .entry-date, .elementor-post-info__item--type-date")
        published_at = None
        if date_node:
            raw_date = normalize_text(date_node.get_text(" ", strip=True))
            if raw_date:
                published_at = raw_date

        items.append(
            NewsItem(
                title=title,
                url=full_url,
                published_at=published_at,
                source_key=source.key,
                source_name=source.name,
            )
        )
        seen_urls.add(full_url)

    return items


def extract_mintic_aid(tag: Tag) -> str | None:
    class_tokens = tag.get("class", [])
    for token in class_tokens:
        match = MINTIC_AID_CLASS_PATTERN.match(token)
        if match:
            return match.group(1)
    return None


def fetch_news_mintic(source: Source, timeout_seconds: int) -> list[NewsItem]:
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(source.url, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()

    # MinTIC pages often declare a misleading encoding in HTTP headers.
    # Parsing from bytes lets BeautifulSoup detect UTF-8 correctly.
    soup = BeautifulSoup(response.content, "html.parser")
    date_by_aid: dict[str, str] = {}

    for date_node in soup.select("div.fecha"):
        date_text = normalize_text(date_node.get_text(" ", strip=True))
        if not date_text:
            continue
        aid = extract_mintic_aid(date_node)
        if aid and aid not in date_by_aid:
            date_by_aid[aid] = date_text

    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for title_node in soup.select("div.titulo"):
        link = title_node.select_one("a[href]")
        if not link:
            continue

        href = (link.get("href") or "").strip()
        title = normalize_text(link.get_text(" ", strip=True))
        if not href or not title:
            continue

        full_url = urljoin(source.url, href)
        if full_url in seen_urls:
            continue
        if not MINTIC_ARTICLE_PATTERN.search(full_url):
            continue

        aid = extract_mintic_aid(title_node)
        if not aid:
            article_match = MINTIC_ARTICLE_ID_PATTERN.search(full_url)
            if article_match:
                aid = article_match.group(1)

        published_at = date_by_aid.get(aid or "")
        items.append(
            NewsItem(
                title=title,
                url=full_url,
                published_at=published_at,
                source_key=source.key,
                source_name=source.name,
            )
        )
        seen_urls.add(full_url)

    return items


def fetch_news_for_source(source: Source, timeout_seconds: int) -> list[NewsItem]:
    if source.key == "cucuta":
        return fetch_news_cucuta(source, timeout_seconds)
    if source.key.startswith("mintic_"):
        return fetch_news_mintic(source, timeout_seconds)
    raise RuntimeError(f"Fuente no soportada: {source.key}")


def load_state(state_file: Path) -> dict[str, list[str]]:
    if not state_file.exists():
        return {}

    try:
        parsed = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log("No se pudo leer el estado anterior. Se reiniciara estado.")
        return {}

    if not isinstance(parsed, dict):
        return {}

    raw_sources = parsed.get("sources")
    state_by_source: dict[str, list[str]] = {}

    if isinstance(raw_sources, dict):
        for key, data in raw_sources.items():
            if not isinstance(key, str) or not isinstance(data, dict):
                continue
            raw_seen = data.get("seen_urls", [])
            if not isinstance(raw_seen, list):
                continue
            state_by_source[key] = [
                str(url).strip() for url in raw_seen if isinstance(url, str) and str(url).strip()
            ]

    # Compatibilidad con el formato anterior: {"seen_urls": [...]}
    legacy_seen = parsed.get("seen_urls")
    if isinstance(legacy_seen, list) and "cucuta" not in state_by_source:
        state_by_source["cucuta"] = [
            str(url).strip()
            for url in legacy_seen
            if isinstance(url, str) and str(url).strip()
        ]

    return state_by_source


def merge_seen_urls(existing: list[str], new_urls: list[str], max_items: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for url in new_urls + existing:
        clean_url = url.strip()
        if not clean_url or clean_url in seen:
            continue
        merged.append(clean_url)
        seen.add(clean_url)
        if len(merged) >= max_items:
            break
    return merged


def save_state(state_file: Path, seen_by_source: dict[str, list[str]]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "sources": {},
        "updated_at_utc": now,
    }
    for key in sorted(seen_by_source):
        payload["sources"][key] = {
            "seen_urls": seen_by_source[key],
            "updated_at_utc": now,
        }
    state_file.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )


def send_telegram_message(config: Config, text: str) -> None:
    if config.dry_run:
        log(f"DRY_RUN activo. Mensaje simulado:\n{text}\n")
        return

    endpoint = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    payload = {
        "chat_id": config.chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }
    response = requests.post(endpoint, json=payload, timeout=config.request_timeout)
    response.raise_for_status()

    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API respondio error: {data}")


def format_news_message(item: NewsItem, initial_scan: bool) -> str:
    lines = []
    if initial_scan:
        lines.append("Noticia reciente detectada (escaneo inicial)")
    else:
        lines.append("Nueva noticia detectada")
    lines.append(f"Fuente: {item.source_name}")
    lines.append(item.title)
    if item.published_at:
        lines.append(f"Fecha: {item.published_at}")
    lines.append(item.url)
    return "\n".join(lines)


def run() -> int:
    config = load_config()
    previous_by_source = load_state(config.state_file)
    updated_by_source = dict(previous_by_source)
    failures: list[str] = []
    for source_key in config.enabled_sources:
        source = SOURCES_BY_KEY[source_key]
        previously_seen = previous_by_source.get(source_key, [])
        seen_set = set(previously_seen)

        try:
            news_items = fetch_news_for_source(source, config.request_timeout)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{source.key} -> fetch error: {exc}")
            updated_by_source[source_key] = previously_seen
            log(f"[{source.key}] Error consultando fuente.")
            continue

        if not news_items:
            updated_by_source[source_key] = previously_seen
            log(f"[{source.key}] No se encontraron noticias.")
            continue

        first_run = len(previously_seen) == 0
        if first_run:
            log(
                f"[{source.key}] Primer escaneo: se enviaran solo "
                f"{min(config.initial_send_count, len(news_items))} noticias."
            )
            to_notify = news_items[: config.initial_send_count]
            skip_as_seen = [item.url for item in news_items[config.initial_send_count :]]
        else:
            to_notify = [item for item in news_items if item.url not in seen_set]
            skip_as_seen = []
            log(f"[{source.key}] Noticias nuevas detectadas: {len(to_notify)}")

        sent_urls: list[str] = []
        for item in to_notify:
            message = format_news_message(item, initial_scan=first_run)
            try:
                send_telegram_message(config, message)
                sent_urls.append(item.url)
                log(f"[{source.key}] Enviada: {item.url}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{item.url} -> {exc}")
                log(f"[{source.key}] Error enviando noticia: {item.url}")

        if not to_notify:
            log(f"[{source.key}] No hay noticias nuevas para enviar.")

        updated_seen = merge_seen_urls(
            existing=previously_seen,
            new_urls=sent_urls + skip_as_seen,
            max_items=config.max_seen_urls,
        )
        updated_by_source[source_key] = updated_seen

    save_state(config.state_file, updated_by_source)
    log(f"Estado actualizado en {config.state_file}.")

    if failures:
        log("Hubo errores de envio:")
        for failure in failures:
            log(f" - {failure}")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001
        log(f"Fallo fatal: {exc}")
        sys.exit(1)
