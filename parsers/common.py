"""Общие вспомогательные функции для обоих адаптеров (Ozon и Wildberries)."""
import json
import logging
import re
import sys
import os
import subprocess
from pathlib import Path
from typing import Any

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

LOG_DIR = BASE_DIR / "logs"
OUTPUT_DIR = BASE_DIR / "output"
BROWSERS_DIR = BASE_DIR / "browser"

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)


def setup_logger(name: str) -> logging.Logger:
    """ERROR — в консоль. Файловый хендлер добавляется отдельно."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(console_handler)
    return logger


def attach_file_log(logger: logging.Logger, log_filename: str) -> None:
    """Добавляет файловый хендлер."""
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_DIR / log_filename, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(file_handler)

class ChromiumNotInstalledError(RuntimeError):
    """Исключение для случая, когда Playwright не находит установленный Chromium"""

def install_local_chromium() -> None:
    """Устанавливает Chromium во внутреннюю папку проекта."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver_executable, driver_cli = compute_driver_executable()
    env = get_driver_env()

    print("Chromium не найден, скачиваю в browser/ (ожидайте)...")
    subprocess.run(
        [str(driver_executable), str(driver_cli), "install", "chromium"],
        env=env,
        check=True,
    )


def launch_chromium(playwright, **launch_kwargs):
    """playwright.chromium.launch(**launch_kwargs) с автоустановкой при первом запуске."""
    for attempt in (1, 2):
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Exception as e:
            err_text = str(e)
            is_missing = "Executable doesn't exist" in err_text or "playwright install" in err_text.lower()
            if attempt == 1 and is_missing:
                try:
                    install_local_chromium()
                    continue
                except Exception as install_err:
                    raise ChromiumNotInstalledError(
                        "Не удалось автоматически скачать Chromium. "
                        "Попробуйте запустить программу от имени Администратора."
                    ) from install_err
            if is_missing:
                raise ChromiumNotInstalledError(
                    "Браузер был скачан, но не запускается. Возможно, его блокирует антивирус."
                ) from e
            raise
        
        
def real_chrome_ua(raw_ua: str) -> str:
    """Заменяет в UA 'HeadlessChrome' на 'Chrome'."""
    return raw_ua.replace("HeadlessChrome/", "Chrome/")


def clean_text(value: str | None) -> str:
    """Схлопывает пробелы/переносы строк в один пробел, обрезает края."""
    return re.sub(r"\s+", " ", value or "").strip()


def format_price_rub(rub: float | int | None) -> str:
    """Число рублей -> строка вида '6 148 ₽'."""
    if rub is None:
        return "Не найдено"
    rub_int = int(round(float(rub)))
    return f"{rub_int:,}".replace(",", " ") + " \u20bd"


def save_products(
    products: list[dict[str, Any]],
    default_filename: str,
    output: str | None = None,
) -> Path:
    """Сохраняет список товаров в output/<output|default_filename> и возвращает путь."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / (output or default_filename)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    return output_path


def rs_text(nodes: list[dict] | None) -> str:
    """Склеивает массив узлов rich-текста Ozon в строку."""
    if not nodes:
        return ""
    parts = [n.get("content") or n.get("text") for n in nodes if isinstance(n, dict)]
    return clean_text(" ".join(p for p in parts if p))

def extra_query_params(url: str, exclude: set[str]) -> dict[str, str]:
    """Query-параметры ссылки за вычетом exclude - для передачи фильтров во внутренний API как есть."""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    return {k: v[0] for k, v in qs.items() if k not in exclude and v}

def preview_text(text: str | None, limit: int = 60) -> str:
    text = clean_text(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."