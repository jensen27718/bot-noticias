import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


DEFAULT_NEWS_URL = "https://cucuta.gov.co/ultimas-noticias/"
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
    news_url: str
    state_file: Path
    initial_send_count: int
    max_seen_urls: int
    request_timeout: int
    dry_run: bool


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    published_at: str | None


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{now}] {message}")


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
    news_url = os.getenv("NEWS_URL", DEFAULT_NEWS_URL).strip()
    state_file = Path(os.getenv("STATE_FILE", DEFAULT_STATE_FILE).strip())
    initial_send_count = parse_int_env("INITIAL_SEND_COUNT", DEFAULT_INITIAL_SEND_COUNT)
    max_seen_urls = parse_int_env("MAX_SEEN_URLS", DEFAULT_MAX_SEEN_URLS)
    request_timeout = parse_int_env("REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
    dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

    if not token and not dry_run:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno.")
    if not chat_id and not dry_run:
        raise RuntimeError("Falta TELEGRAM_CHAT_ID en variables de entorno.")
    if not news_url:
        raise RuntimeError("Falta NEWS_URL en variables de entorno.")

    return Config(
        bot_token=token,
        chat_id=chat_id,
        news_url=news_url,
        state_file=state_file,
        initial_send_count=initial_send_count,
        max_seen_urls=max_seen_urls,
        request_timeout=request_timeout,
        dry_run=dry_run,
    )


def fetch_news(news_url: str, timeout_seconds: int) -> list[NewsItem]:
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(news_url, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
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

        full_url = urljoin(news_url, href)
        if full_url in seen_urls:
            continue

        date_node = post.select_one("time, .entry-date, .elementor-post-info__item--type-date")
        published_at = None
        if date_node:
            raw_date = normalize_text(date_node.get_text(" ", strip=True))
            if raw_date:
                published_at = raw_date

        items.append(NewsItem(title=title, url=full_url, published_at=published_at))
        seen_urls.add(full_url)

    return items


def load_state(state_file: Path) -> dict[str, list[str] | str]:
    if not state_file.exists():
        return {"seen_urls": []}

    try:
        parsed = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log("No se pudo leer el estado anterior. Se reiniciara estado.")
        return {"seen_urls": []}

    raw_seen = parsed.get("seen_urls", [])
    if not isinstance(raw_seen, list):
        raw_seen = []

    seen_urls = [str(url) for url in raw_seen if isinstance(url, str) and url.strip()]
    return {"seen_urls": seen_urls}


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


def save_state(state_file: Path, seen_urls: list[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen_urls": seen_urls,
        "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        lines.append("Noticia reciente de Cucuta (escaneo inicial)")
    else:
        lines.append("Nueva noticia detectada en Cucuta")
    lines.append(item.title)
    if item.published_at:
        lines.append(f"Fecha: {item.published_at}")
    lines.append(item.url)
    return "\n".join(lines)


def run() -> int:
    config = load_config()
    state = load_state(config.state_file)
    previously_seen = list(state.get("seen_urls", []))
    seen_set = set(previously_seen)

    news_items = fetch_news(config.news_url, config.request_timeout)
    if not news_items:
        log("No se encontraron noticias en la pagina.")
        return 0

    first_run = len(previously_seen) == 0
    if first_run:
        log(
            "Primer escaneo: se enviaran solo "
            f"{min(config.initial_send_count, len(news_items))} noticias."
        )
        to_notify = news_items[: config.initial_send_count]
        skip_as_seen = [item.url for item in news_items[config.initial_send_count :]]
    else:
        to_notify = [item for item in news_items if item.url not in seen_set]
        skip_as_seen = []
        log(f"Noticias nuevas detectadas: {len(to_notify)}")

    sent_urls: list[str] = []
    failures: list[str] = []

    for item in to_notify:
        message = format_news_message(item, initial_scan=first_run)
        try:
            send_telegram_message(config, message)
            sent_urls.append(item.url)
            log(f"Enviada: {item.url}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{item.url} -> {exc}")
            log(f"Error enviando noticia: {item.url}")

    if not to_notify:
        log("No hay noticias nuevas para enviar.")

    updated_seen = merge_seen_urls(
        existing=previously_seen,
        new_urls=sent_urls + skip_as_seen,
        max_items=config.max_seen_urls,
    )
    save_state(config.state_file, updated_seen)
    log(f"Estado actualizado en {config.state_file} ({len(updated_seen)} URLs).")

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
