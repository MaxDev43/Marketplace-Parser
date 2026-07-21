import re
import secrets
import time
import tomllib
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, parse_qs

from curl_cffi import requests

from parsers.common import BASE_DIR, ChromiumNotInstalledError, attach_file_log, clean_text, extra_query_params, format_price_rub, save_products, setup_logger, preview_text, launch_chromium, real_chrome_ua

with open(BASE_DIR / "config.toml", "rb") as f:
    _config = tomllib.load(f)

DEFAULT_PRODUCT_COUNT: int = _config["common"]["DEFAULT_PRODUCT_COUNT"]
_WB = _config["wildberries"]
DEST: int = _WB["DEST"]
REQUEST_DELAY_S: float = _WB["REQUEST_DELAY_S"]
TIMEOUT_S: float = _WB["TIMEOUT_S"]
MAX_RETRIES: int = _WB["MAX_RETRIES"]

logger = setup_logger("parsers.wildberries")

SEARCH_URL = "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search"
DETAIL_URL = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"

SPA_VERSION = "14.16.3"

_session = requests.Session(impersonate="chrome")
_real_ua: str | None = None
_wbauid: str | None = None
_device_id: str | None = None
_bootstrapped_for: set[str] = set()


def _find_device_id(page) -> str | None:
    """deviceid - клиентский ID вида site_<32 hex>. Берется из localStorage под ключом wbx__sessionID или используется сгенерированный."""
    try:
        return page.evaluate(
            """() => {
                const known = localStorage.getItem('wbx__sessionID');
                if (known && /^site_[0-9a-f]{32}$/.test(known)) return known;
                for (let i = 0; i < localStorage.length; i++) {
                    const v = localStorage.getItem(localStorage.key(i));
                    if (/^site_[0-9a-f]{32}$/.test(v)) return v;
                }
                return null;
            }"""
        )
    except Exception:
        return None

def _bootstrap_session(page_url: str) -> None:
    """Настоящий браузер один раз открывает страницу - получает cookie сессии и deviceid."""
    global _real_ua, _wbauid, _device_id
    from playwright.sync_api import sync_playwright

    logger.info("Bootstrap сессии WB через браузер: %s", page_url)
    with sync_playwright() as p:
        browser = launch_chromium(
            p,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        
        tmp_page = browser.new_page()
        honest_ua = real_chrome_ua(tmp_page.evaluate("() => navigator.userAgent"))
        tmp_page.close()

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            user_agent=honest_ua,
        )
        page = context.new_page()
        page.goto(page_url, wait_until="load", timeout=30_000)
        page.wait_for_timeout(3_000)

        _real_ua = honest_ua
        _device_id = _find_device_id(page)
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        browser.close()

#    _session.headers.update({"User-Agent": _real_ua})
    for name, value in cookies.items():
        _session.cookies.set(name, value)
    _wbauid = cookies.get("_wbauid")
    if not _device_id:
        _device_id = f"site_{secrets.token_hex(16)}"
        logger.debug("deviceid не найден в localStorage, сгенерирован свой: %s", _device_id)
    if "x_wbaas_token" not in cookies:
        logger.warning("x_wbaas_token не получен после bootstrap - запросы могут не пройти")
    logger.info("Bootstrap готов: UA=%s, deviceid=%s, cookie=%s", _real_ua, _device_id, list(cookies))


def _ensure_session(page_url: str) -> None:
    if page_url not in _bootstrapped_for:
        _bootstrap_session(page_url)
        _bootstrapped_for.add(page_url)


def _api_headers(referer: str) -> dict:
    """x-queryid собирается по образцу реального запроса: qid + _wbauid + текущее время."""
    query_id = f"qid{_wbauid or ''}{datetime.now():%Y%m%d%H%M%S}"
    return {
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer,
        "deviceid": _device_id or "",
        "x-queryid": query_id,
        "x-requested-with": "XMLHttpRequest",
        "x-spa-version": SPA_VERSION,
        "x-userid": "0",
    }


# ── резолвинг CDN-«корзины» (basket) ─────────────────────────────────────────
# у WB минимум два шаблона доменов для card.json/картинок, и второй карта vol->номер. Корзины со временем съезжает (корзин становится больше). Поэтому вместо жёсткой таблицы здесь - перебор нескольких кандидатов с кэшем результата на процесс и на диск.

BASKET_HOST_TEMPLATES = [
    "https://mow-basket-cdn-{n:02d}.geobasket.ru",
    "https://basket-{n:02d}.wbbasket.ru",
]
BASKET_PROBE_RANGE = range(1, 81)

_BASKET_CACHE_FILE = BASE_DIR / "output" / ".wb_basket_cache.json"
_basket_cache: dict[int, str] = {}


def _load_basket_cache() -> None:
    global _basket_cache
    try:
        import json
        with open(_BASKET_CACHE_FILE, encoding="utf-8") as f:
            _basket_cache = {int(k): v for k, v in json.load(f).items()}
    except Exception:
        _basket_cache = {}


def _save_basket_cache() -> None:
    try:
        import json
        _BASKET_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_BASKET_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_basket_cache, f)
    except Exception as e:
        logger.debug("Не удалось сохранить кэш корзин: %s", e)


def _resolve_basket_host(article: int) -> str | None:
    """Находит рабочий CDN-хост для info/ru/card.json данного артикула.
    Пробует сначала закэшированное значение для этого диапазона vol, затем перебирает шаблоны доменов x номера корзины кольцом от центра диапазона. Результат кэшируется в памяти и на диске."""

    vol = article // 100_000
    if vol in _basket_cache:
        return _basket_cache[vol]

    for template in BASKET_HOST_TEMPLATES:
        for n in BASKET_PROBE_RANGE:
            host = template.format(n=n)
            url = f"{host}/vol{vol}/part{article // 1000}/{article}/info/ru/card.json"
            try:
                r = _session.head(url, timeout=5)
                if r.status_code == 200:
                    _basket_cache[vol] = host
                    _save_basket_cache()
                    logger.info("Корзина для vol=%d найдена: %s", vol, host)
                    return host
            except requests.exceptions.RequestException:
                continue
    logger.warning("Не удалось определить корзину для vol=%d (article=%d)", vol, article)
    return None


_load_basket_cache()


# ── поиск ────────────────────────────────────────────────────────────────────

def _fetch_search_page(query: str, page: int, extra: dict, referer: str) -> tuple[list[dict], int]:
    """Одна страница поисковой выдачи. Возвращает (товары, total)."""
    _ensure_session(referer)
    params = {
        "ab_testid": "",
        "appType": 1,
        "curr": "rub",
        "dest": DEST,
        "hide_vflags": 4294967296,
        "inheritFilters": "false",
        "lang": "ru",
        "page": page,
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": 30,
        "suppressSpellcheck": "false",
        **extra,  # фильтры из ссылки (priceU, sort и т.п.) перекрывают дефолты выше
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session.get(SEARCH_URL, params=params, headers=_api_headers(referer), timeout=TIMEOUT_S)
            r.raise_for_status()
            data = r.json()
            return data.get("products", []), data.get("total", 0)
        except requests.exceptions.RequestException as e:
            body = getattr(getattr(e, "response", None), "text", None)
            logger.warning(
                "Страница поиска %d, попытка %d/%d: %s%s",
                page, attempt, MAX_RETRIES, e,
                f" | body: {body}" if body else "",
            )
            time.sleep(2.0 * attempt)
    return [], 0


def search_wb(query: str, target_count: int, referer: str, extra: dict | None = None) -> list[dict]:
    """До target_count товаров, максимум 100 на страницу."""
    extra = extra or {}
    all_raw: list[dict] = []
    page = 1
    while len(all_raw) < target_count:
        batch, total = _fetch_search_page(query, page, extra, referer)
        if not batch:
            break
        all_raw.extend(batch)
        logger.info("Страница %d: получено %d, накоплено %d/%d", page, len(batch), len(all_raw), total or 0)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(REQUEST_DELAY_S)
    return all_raw[:target_count]


def fetch_by_nm(article: int, referer: str) -> dict | None:
    """Прямой лукап карточки по артикулу (v4/detail) — для режима «одна ссылка
    на товар». Формат ответа как у поиска: {"products":[{...}]}."""
    _ensure_session(referer)
    params = {
        "appType": 1,
        "curr": "rub",
        "dest": DEST,
        "spp": 30,
        "hide_vflags": 4294967296,
        "hide_dtype": 15,
        "mtype": 257,
        "lang": "ru",
        "ab_testing": "false",
        "nm": article,
    }
    try:
        r = _session.get(DETAIL_URL, params=params, headers=_api_headers(referer), timeout=TIMEOUT_S)
        r.raise_for_status()
        products = r.json().get("products", [])
        return products[0] if products else None
    except requests.exceptions.RequestException as e:
        logger.error("Ошибка v4/detail для nm=%d: %s", article, e)
        return None


# ── карточка (описание + характеристики) ────────────────────────────────────

def fetch_card_json(article: int) -> dict:
    """Статический card.json на CDN - описание и характеристики."""
    host = _resolve_basket_host(article)
    if not host:
        return {}
    vol = article // 100_000
    part = article // 1000
    url = f"{host}/vol{vol}/part{part}/{article}/info/ru/card.json"
    try:
        r = _session.get(url, timeout=TIMEOUT_S)
        if r.status_code == 404:
            # Диапазон vol мог "переехать" в другую корзину - сбрасываем кэш и пробуем раз ещё
            _basket_cache.pop(vol, None)
            host = _resolve_basket_host(article)
            if not host:
                return {}
            url = f"{host}/vol{vol}/part{part}/{article}/info/ru/card.json"
            r = _session.get(url, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        logger.error("Ошибка card.json для article=%d: %s", article, e)
        return {}


def extract_characteristics(card: dict) -> dict[str, str]:
    """options (плоский список) + grouped_options (встречается не всегда) -> dict."""
    chars: dict[str, str] = {}
    for opt in card.get("options", []):
        name = clean_text(opt.get("name"))
        value = clean_text(str(opt.get("value", "")))
        if name:
            chars[name] = value
    for group in card.get("grouped_options", []):
        for opt in group.get("options", []):
            name = clean_text(opt.get("name"))
            value = clean_text(str(opt.get("value", "")))
            if name:
                chars.setdefault(name, value)
    return chars


# ── нормализация ─────────────────────────────────────────────────────────────

def normalize_product(raw: dict, card: dict) -> dict[str, Any]:
    """Сырой товар из поиска/detail + card.json -> единый формат вывода.
    Цена: sizes[0].price.product (копейки -> рубли, /100). У WB, в отличие от Ozon, отдельной статической "цены по карте" в этом API нет - цена из здесь не отдаётся. "product" — это и есть основная цена на карточке. """
    article = raw["id"]
    sizes = raw.get("sizes", [])
    price_rub = None
    if sizes:
        price_kopecks = sizes[0].get("price", {}).get("product")
        if price_kopecks:
            price_rub = price_kopecks / 100

    description = clean_text(card.get("description", ""))
    name = clean_text(card.get("imt_name") or raw.get("name", ""))
    brand = card.get("selling", {}).get("brand_name") or raw.get("brand")
    if brand and not name.lower().startswith(brand.lower()):
        name = f"{brand} {name}"

    return {
        "link": f"https://www.wildberries.ru/catalog/{article}/detail.aspx",
        "name": clean_text(name),
        "price": format_price_rub(price_rub),
        "description": description,
        "characteristics": extract_characteristics(card),
    }


# ── разбор входной ссылки ────────────────────────────────────────────────────

def is_product_url(url: str) -> bool:
    return bool(re.search(r"/catalog/\d+/detail\.aspx", url))


def extract_article(url: str) -> int | None:
    m = re.search(r"/catalog/(\d+)/detail\.aspx", url)
    return int(m.group(1)) if m else None


def extract_query(url: str) -> str | None:
    """Достаёт текстовый поисковый запрос из ссылки на страницу поиска WB.
    Поддерживает основные варианты параметра (search/query/text)."""
    qs = parse_qs(urlparse(url).query)
    for key in ("search", "query", "text"):
        if key in qs and qs[key]:
            return qs[key][0]
    return None


WB_RESERVED_PARAMS = {"search", "query", "text", "page"}  # обрабатываются отдельно, не передаются как фильтры


# ── main ─────────────────────────────────────────────────────────────────────

def main(url: str | None = None, output: str | None = None, num: int | None = None) -> None:
    """num не передан -> берётся DEFAULT_PRODUCT_COUNT из config.toml."""
    attach_file_log(logger, "wildberries_parser.log")
    target_count = num or DEFAULT_PRODUCT_COUNT

    logger.info("=== ЗАПУСК ПАРСЕРА WILDBERRIES ===")
    logger.info("Целевое количество товаров: %d", target_count)

    if not url:
        url = input("🔗 Вставьте ссылку на страницу поиска (или на товар) Wildberries: ").strip()

    products: list[dict] = []

    try:
        if is_product_url(url):
            article = extract_article(url)
            print(f"Режим: один товар (артикул {article})")
            logger.info("Режим: один товар (артикул %s)", article)
            raw = fetch_by_nm(article, referer=url)
            if raw is None:
                print("Не удалось получить товар — проверьте ссылку.")
                logger.warning("Товар %s: fetch_by_nm вернул None", article)
            else:
                card = fetch_card_json(article)
                products.append(normalize_product(raw, card))
                logger.info("Товар %s: успешно собран", article)
        else:
            query = extract_query(url)
            if not query:
                print(
                    "Не нашёл текстовый запрос (?search=... / ?query=...) в этой ссылке.\n"
                    "Поддерживаются: ссылка на страницу поиска WB или прямая ссылка на товар.\n"
                )
                return

            print(f"Режим: поиск по запросу «{query}», нужно товаров: {target_count}")
            extra = extra_query_params(url, WB_RESERVED_PARAMS)
            if extra:
                print(f"Доп. фильтры из ссылки: {extra}")
            all_raw = search_wb(query, target_count, referer=url, extra=extra)
            print(f"Найдено в выдаче: {len(all_raw)} товаров\n")

            for i, raw in enumerate(all_raw, start=1):
                article = raw.get("id")
                name_preview = preview_text(raw.get("name"), 60)
                print(f"[{i}/{len(all_raw)}] {article} {name_preview}")
                logger.info("[%d/%d] %s %s", i, len(all_raw), article, (raw.get("name") or "")[:60])
                try:
                    card = fetch_card_json(article)
                    products.append(normalize_product(raw, card))
                    logger.info("Товар %s: успешно собран", article)
                except Exception as e:
                    logger.error("Товар %s пропущен: %s", article, e)
                    print(f"   -> пропущен: {e}")
                time.sleep(REQUEST_DELAY_S)

    except KeyboardInterrupt:
        print("\nПрервано пользователем")
    except ChromiumNotInstalledError as e:
        logger.error("Chromium не установлен: %s", e)
    except Exception as e:
        logger.error("ФАТАЛЬНАЯ ОШИБКА: %s", e)
        print(f"Критическая ошибка: {e}")
    finally:
        output_path = save_products(products, "wildberries_products.json", output)
        print(f"\nГотово! Собрано товаров: {len(products)}")
        print(f"Файл сохранён: {output_path}")
        logger.info("=== ПАРСЕР WILDBERRIES ЗАВЕРШИЛ РАБОТУ ===")


if __name__ == "__main__":
    main()