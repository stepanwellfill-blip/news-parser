"""
Парсер новостей: requests + BeautifulSoup + rich.

Собирает новости с сайта, спрашивает у пользователя ключевое слово,
фильтрует, сохраняет в CSV/JSON и красиво печатает результат в консоль.
"""

import csv
import json
import logging
import os
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from rich.box import SIMPLE_HEAD
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ────────────────────────────── КОНФИГ ──────────────────────────────
URL = "https://habr.com/ru/news/"

SELECTORS = {
    "item": "article.tm-articles-list__item",
    "title": "a.tm-title__link",
    "link": "a.tm-title__link",
    "date": "time",
    "description": "div.tm-article-snippet__lead",
}

# Дефолт, если пользователь нажмёт Enter, не вводя ничего.
DEFAULT_KEYWORD = "технолог"

MAX_ITEMS = 30
TIMEOUT = 10
DELAY_BETWEEN_REQUESTS = 1  # секунд между запросами (на будущее)
OUTPUT_DIR = "output"
LOG_FILE = "parser.log"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


# ────────────────────────────── ЛОГИРОВАНИЕ ──────────────────────────────

class _InfoOnlyFilter(logging.Filter):
    """Пускает в консоль только INFO-сообщения (WARNING/ERROR — только в файл)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == logging.INFO


def setup_logging() -> None:
    """Настраивает logging: INFO в файл и консоль, WARNING/ERROR — только в файл."""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")

    # Файл — все уровни от INFO и выше
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Консоль — только INFO (чтобы не засорять)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_InfoOnlyFilter())
    logger.addHandler(console_handler)


# ────────────────────────────── ВВОД ПОЛЬЗОВАТЕЛЯ ──────────────────────────────

def ask_keyword(console: Console, default: str) -> str:
    """Спрашивает у пользователя ключевое слово; при пустом вводе возвращает default."""
    console.print(
        f"[cyan]Введите ключевое слово для фильтра[/cyan] "
        f"[dim](Enter — по умолчанию '{default}'):[/dim]"
    )
    try:
        raw = console.input("[cyan]→[/cyan] ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        console.print(f"[yellow]Ввод прерван, использую '{default}'[/yellow]")
        return default

    if not raw:
        return default
    return raw


# ────────────────────────────── СЕТЬ ──────────────────────────────

def fetch_page(url: str) -> str | None:
    """Загружает HTML-страницу, возвращает текст или None при ошибке."""
    logging.info("GET %s (timeout=%s)", url, TIMEOUT)

    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        logging.error("Таймаут при запросе к %s", url, exc_info=True)
        return None
    except requests.exceptions.ConnectionError:
        logging.error("Ошибка соединения с %s", url, exc_info=True)
        return None
    except requests.exceptions.RequestException:
        logging.error("Ошибка запроса к %s", url, exc_info=True)
        return None

    if response.status_code != 200:
        logging.warning("Неожиданный статус-код: %s", response.status_code)
        return None

    size_kb = len(response.content) / 1024
    logging.info("Страница загружена: 200 OK, %.0f KB", size_kb)

    # Пауза оставлена «на будущее» — если будем обходить пагинацию.
    time.sleep(DELAY_BETWEEN_REQUESTS)

    return response.text


# ────────────────────────────── ПАРСИНГ ──────────────────────────────

def _parse_date(date_el) -> str:
    """Парсит ISO-дату из тега <time datetime="..."> в формат ДД.ММ.ГГГГ."""
    if date_el is None:
        return ""
    raw = date_el.get("datetime")
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return ""


def parse_page(html: str) -> list[dict]:
    """Извлекает из HTML список новостей: title, link, date, description."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select(SELECTORS["item"])

        if not items:
            logging.warning("Селектор %r ничего не нашёл", SELECTORS["item"])
            return []

        news: list[dict] = []
        for item in items:
            title_el = item.select_one(SELECTORS["title"])
            if title_el is None:
                continue

            title = title_el.get_text(strip=True)

            link_el = item.select_one(SELECTORS["link"])
            href = link_el.get("href", "") if link_el else ""
            # Habr отдаёт относительные ссылки — склеиваем с базовым URL.
            link = urljoin(URL, href) if href else ""

            date_el = item.select_one(SELECTORS["date"])
            date_str = _parse_date(date_el)

            desc_el = item.select_one(SELECTORS["description"])
            description = desc_el.get_text(strip=True) if desc_el else ""

            news.append(
                {
                    "title": title,
                    "link": link,
                    "date": date_str,
                    "description": description,
                }
            )

        logging.info("Найдено карточек: %d", len(news))
        return news

    except Exception as e:  # noqa: BLE001 — на всякий случай, чтобы не падать
        logging.error("Ошибка парсинга: %s", e, exc_info=True)
        return []


# ────────────────────────────── ФИЛЬТР ──────────────────────────────

def filter_news(news: list[dict], keyword: str) -> list[dict]:
    """Регистронезависимо фильтрует новости по ключевому слову в заголовке и описании."""
    kw = keyword.lower()
    filtered = [
        item
        for item in news
        if kw in item["title"].lower() or kw in item["description"].lower()
    ]
    logging.info("После фильтра по %r: %d новостей", keyword, len(filtered))
    # Ограничиваем финальный список — MAX_ITEMS.
    return filtered[:MAX_ITEMS]


# ────────────────────────────── СОХРАНЕНИЕ ──────────────────────────────

def save_to_csv(news: list[dict], path: str | os.PathLike) -> tuple[bool, str | None]:
    """Сохраняет список новостей в CSV. Возвращает (успех, причина_ошибки)."""
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f, fieldnames=["title", "link", "date", "description"]
            )
            writer.writeheader()
            writer.writerows(news)
        logging.info("CSV сохранён: %s", path)
        return True, None
    except OSError as e:
        logging.error("Ошибка сохранения CSV: %s", e, exc_info=True)
        return False, str(e)


def save_to_json(news: list[dict], path: str | os.PathLike) -> tuple[bool, str | None]:
    """Сохраняет список новостей в JSON. Возвращает (успех, причина_ошибки)."""
    try:
        # ensure_ascii=False — чтобы кириллица была читаемой.
        with open(path, "w", encoding="utf-8") as f:
            json.dump(news, f, indent=2, ensure_ascii=False)
        logging.info("JSON сохранён: %s", path)
        return True, None
    except OSError as e:
        logging.error("Ошибка сохранения JSON: %s", e, exc_info=True)
        return False, str(e)


# ────────────────────────────── RICH-РЕНДЕР ──────────────────────────────

def render_status(
    console: Console,
    step: str,
    status: str = "info",
    message: str = "",
) -> None:
    """Печатает одну строку статуса с цветной иконкой."""
    icons = {
        "info": "[cyan]→[/cyan]",
        "success": "[green]✓[/green]",
        "error": "[red]✗[/red]",
    }
    icon = icons.get(status, icons["info"])

    if status == "error":
        text = f"Ошибка: {step}"
    else:
        text = step

    if message:
        text = f"{text} {message}"

    console.print(f"{icon} {text}")


def render_results(console: Console, news: list[dict], keyword: str) -> None:
    """Рендерит таблицу результатов или жёлтую плашку «Ничего не найдено»."""
    if not news:
        console.print(
            Panel(
                f"Ничего не найдено по запросу '{keyword}'",
                border_style="yellow",
                style="yellow",
            )
        )
        return

    table = Table(
        title="РЕЗУЛЬТАТЫ",
        title_style="bold cyan",
        title_justify="center",
        box=SIMPLE_HEAD,
        border_style="cyan",
        header_style="bold cyan",
        padding=(0, 1),
        show_edge=True,
    )

    table.add_column("#", width=4, justify="right", no_wrap=True)
    table.add_column("Дата", width=12, no_wrap=True)
    table.add_column("Заголовок", width=65, no_wrap=True, overflow="ellipsis")
    table.add_column("Ссылка", width=30, no_wrap=True, overflow="ellipsis")

    for idx, item in enumerate(news, start=1):
        # Зебра-эффект: чётные строки — на очень лёгком сером фоне.
        bg = " on grey11" if idx % 2 == 0 else ""

        num_cell = Text(str(idx), style=f"dim{bg}")

        if item["date"]:
            date_cell = Text(item["date"], style=f"yellow{bg}")
        else:
            date_cell = Text("—", style=f"dim{bg}")

        title_cell = Text(item["title"], style=f"white{bg}")

        link_url = item["link"]
        if link_url:
            # «link <url>» делает ячейку кликабельной в терминале.
            link_cell = Text(link_url, style=f"blue underline{bg}")
            link_cell.stylize(f"link {link_url}")
        else:
            link_cell = Text("", style=f"blue underline{bg}")

        table.add_row(num_cell, date_cell, title_cell, link_cell)

    console.print(table)


def render_summary(
    console: Console,
    total: int,
    filtered: int,
    elapsed: float,
    saved_files: list[str],
    save_errors: list[str],
) -> None:
    """Рендерит итоговую статистику и строку о сохранённых файлах."""
    # Ровно 60 символов «─» — как в ТЗ.
    console.print(Text("─" * 60, style="dim"))

    console.print(f"[green]●[/green] Всего карточек: {total}")
    console.print(f"[green]●[/green] После фильтра: {filtered}")
    console.print(f"[green]●[/green] Время работы: {elapsed:.2f} сек")
    console.print()

    if saved_files:
        console.print(
            "[cyan]Файлы сохранены:[/cyan] " + ", ".join(str(p) for p in saved_files)
        )
    for err in save_errors:
        console.print(f"[red]{err}[/red]")


# ────────────────────────────── MAIN ──────────────────────────────

def main() -> None:
    """Точка входа: оркестрация шагов парсера."""
    console = Console()
    setup_logging()
    start_time = time.time()

    # Шапка
    console.print(Text("═" * 60, style="cyan"))
    console.print(
        Panel(
            Text("ПАРСЕР НОВОСТЕЙ", style="bold cyan", justify="center"),
            border_style="cyan",
        )
    )
    console.print(f"Источник: {URL}", style="dim")

    # Спрашиваем ключевое слово у пользователя.
    console.print()
    keyword = ask_keyword(console, DEFAULT_KEYWORD)
    console.print(f"Ключевое слово: {keyword}", style="dim")
    console.print()

    logging.info("Старт парсера. URL=%s, KEYWORD=%s", URL, keyword)

    # Загрузка страницы
    render_status(console, "Загружаю страницу...", "info")
    html = fetch_page(URL)

    if html is None:
        render_status(console, "не удалось загрузить страницу", "error")
        news: list[dict] = []
    else:
        size_kb = len(html.encode("utf-8")) / 1024
        render_status(
            console,
            "Страница загружена",
            "success",
            f"(200 OK, {size_kb:.0f} KB)",
        )

        render_status(console, "Парсю новости...", "info")
        news = parse_page(html)
        render_status(console, "Найдено карточек:", "success", str(len(news)))

    # Фильтрация
    render_status(console, f"Фильтрую по слову '{keyword}'...", "info")
    filtered = filter_news(news, keyword)
    render_status(console, "После фильтра:", "success", f"{len(filtered)} новостей")
    console.print()

    # Таблица результатов
    render_results(console, filtered, keyword)

    # Сохранение файлов
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except OSError as e:
        logging.error("Не удалось создать папку %s: %s", OUTPUT_DIR, e, exc_info=True)

    # Секунды в имени — чтобы запуски в одну минуту не перезаписывали друг друга.
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    csv_path = os.path.join(OUTPUT_DIR, f"news_{timestamp}.csv")
    json_path = os.path.join(OUTPUT_DIR, f"news_{timestamp}.json")

    saved_files: list[str] = []
    save_errors: list[str] = []

    ok_csv, err_csv = save_to_csv(filtered, csv_path)
    if ok_csv:
        saved_files.append(csv_path)
    else:
        save_errors.append(f"Не удалось сохранить CSV: {err_csv}")

    ok_json, err_json = save_to_json(filtered, json_path)
    if ok_json:
        saved_files.append(json_path)
    else:
        save_errors.append(f"Не удалось сохранить JSON: {err_json}")

    # Итоги
    elapsed = time.time() - start_time
    logging.info(
        "Готово за %.2f сек. Всего: %d, после фильтра: %d",
        elapsed,
        len(news),
        len(filtered),
    )

    render_summary(
        console,
        total=len(news),
        filtered=len(filtered),
        elapsed=elapsed,
        saved_files=saved_files,
        save_errors=save_errors,
    )


if __name__ == "__main__":
    main()