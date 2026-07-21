import json
import re
import time
import tomllib
from typing import Any
from urllib.parse import urlparse, parse_qs, quote, urlencode

from parsers.common import BASE_DIR, ChromiumNotInstalledError, attach_file_log, clean_text, extra_query_params, format_price_rub, save_products, setup_logger, rs_text, preview_text, launch_chromium, real_chrome_ua

with open(BASE_DIR / "config.toml", "rb") as f:
    _config = tomllib.load(f)

DEFAULT_PRODUCT_COUNT: int = _config["common"]["DEFAULT_PRODUCT_COUNT"]
_OZ = _config["ozon"]
API_PREFIX: str = _OZ["API_PREFIX"]
CHALLENGE_WAIT_S: float = _OZ["CHALLENGE_WAIT_S"]
REQUEST_DELAY_S: float = _OZ["REQUEST_DELAY_S"]
MAX_RETRIES: int = _OZ["MAX_RETRIES"]
NAV_TIMEOUT_MS: int = int(_OZ["NAV_TIMEOUT_S"] * 1000)
MAX_SEARCH_PAGES: int = _OZ.get("MAX_SEARCH_PAGES", 10)
CHECK_CHEAPER_OFFERS: bool = _OZ.get("CHECK_CHEAPER_OFFERS", True)

logger = setup_logger("parsers.ozon")

HOME_URL = "https://www.ozon.ru/"
API_URL = f"https://www.ozon.ru/api/{API_PREFIX}/page/json/v2?url="


# ====================== БРАУЗЕРНАЯ СЕССИЯ (анти-бот) ======================

class _NonRetryableStatus(RuntimeError):
    """перехватывается до общего except Exception и сразу пробрасывается наружу."""

class OzonSession:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._challenged = False

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright

        logger.info("Запускаем headless Chromium...")
        self._playwright = sync_playwright().start()
        try:
            self._browser = launch_chromium(
                self._playwright,
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--mute-audio",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise

        tmp_page = self._browser.new_page()
        raw_ua = tmp_page.evaluate("() => navigator.userAgent")
        tmp_page.close()

        self._context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            user_agent=real_chrome_ua(raw_ua),
        )
        self._challenged = False

    def _ensure_challenged(self) -> None:
        if self._context is not None and self._challenged:
            return
        if self._browser is None or not self._browser.is_connected():
            self._launch()
        self._page = self._context.new_page()
        logger.info("Проходим анти-бот проверку на главной странице...")
        try:
            response = self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            raise RuntimeError(
                f"Браузер не смог открыть {HOME_URL}: {e}\n"
                "Страница вообще не загрузилась - это НЕ анти-бот Ozon, а проблема соединения у самого браузера. Проверьте playwright/Chromium и обычный браузер на этой машине."
            ) from e
        self._page.wait_for_timeout(int(CHALLENGE_WAIT_S * 1000))

        title = (self._page.title() or "")
        try:
            content = self._page.content()
        except Exception:
            content = ""
        looks_like_ozon = ("ozon" in content.lower() or "ozon" in title.lower()) and len(content) > 50_000

        if response is not None and not response.ok:
            logger.info("Начальная страница вернула HTTP %s (может быть нормально для челленджа)", response.status)
        if response is None or not looks_like_ozon:
            raise RuntimeError(
                f"Похоже, страница не загрузилась по-настоящему (url: {self._page.url}, "
                f"title: {title!r}, HTTP статус: {getattr(response, 'status', None)}, "
                f"длина HTML: {len(content)})."
            )
        if re.search(r"antibot|ограничен|доступ ограничен|подтвердите|Соедине", title, re.I):
            raise RuntimeError(f"Анти-бот проверка не пройдена (title: {title})")

        self._challenged = True
        logger.info("Анти-бот пройден, url: %s, title: %s", self._page.url, title[:60])

    def fetch_json(self, path: str, retries: int = MAX_RETRIES) -> dict:
        """Выполняет fetch(path) изнутри уже прошедшей проверку страницы.

        Ретраятся только HTTP 403/307 (протухшая сессия) и сбои самого
        запроса (сеть, JS). Прочие статусы (404 и т.п.) окончательны -
        товар действительно недоступен, повторять бессмысленно."""
        logger.debug("fetch_json: %s", path)
        for attempt in range(retries + 1):
            try:
                self._ensure_challenged()
                full_url = API_URL + quote(path, safe="")
                result = self._page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, { headers: { accept: "application/json" } });
                        return { status: r.status, text: await r.text() };
                    }""",
                    full_url,
                )
                if result["status"] != 200:
                    body_preview = (result.get("text") or "")[:500]
                    logger.debug("Тело ответа (%s, первые 500 симв.): %s", path, body_preview)
                    if result["status"] not in (403, 307):
                        raise _NonRetryableStatus(
                            f"Ozon вернул HTTP {result['status']} для {path}. "
                            f"Начало тела ответа: {body_preview!r}"
                        )
                    raise RuntimeError(f"HTTP {result['status']} для {path} - сессия протухла")
                if attempt > 0:
                    logger.info("Успешно с %d-й попытки: %s", attempt + 1, path)
                return json.loads(result["text"])
            except _NonRetryableStatus:
                raise
            except ChromiumNotInstalledError:
                raise
            except Exception as e:
                if attempt < retries:
                    logger.warning("Ошибка fetch_json (%s), пробуем ещё раз: %s", path, e)
                    self.shutdown()
                    time.sleep(3.0)
                    continue
                raise
        raise RuntimeError("fetch_json: исчерпаны попытки")

    def shutdown(self) -> None:
        self._challenged = False
        self._page = None
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None


# ====================== ЧИСТЫЕ ФУНКЦИИ РАЗБОРА JSON ======================

def _widget_name(key: str) -> str:
    return key.split("-")[0]


def widget(page: dict, name: str) -> dict | None:
    """Первый виджет с именем `name` (webPrice-3121879-default-1 -> "webPrice")."""
    ws = (page or {}).get("widgetStates", {})
    key = next((k for k in ws if _widget_name(k) == name), None)
    if not key:
        return None
    try:
        return json.loads(ws[key])
    except (json.JSONDecodeError, TypeError):
        return None


def widgets(page: dict, name: str) -> list[dict]:
    ws = (page or {}).get("widgetStates", {})
    out = []
    for k in ws:
        if _widget_name(k) == name:
            try:
                out.append(json.loads(ws[k]))
            except (json.JSONDecodeError, TypeError):
                continue
    return out


def price_to_number(text: str | None) -> float | None:
    """'53 022 ₽' -> 53022.0"""
    if not isinstance(text, str):
        return None
    digits = re.sub(r"[^\d]", "", text)
    return float(digits) if digits else None


def clean_url(link: str | None) -> str | None:
    """Убирает ?at=...-токены и делает ссылку абсолютной."""
    if not link:
        return None
    path = str(link).split("?")[0]
    return path if path.startswith("http") else f"https://www.ozon.ru{path}"


def sku_from_url(url: str | None) -> str | None:
    m = re.search(r"-(\d+)/?(?:\?|$)", str(url or "")) or re.search(r"(\d{6,})", str(url or ""))
    return m.group(1) if m else None


def parse_search_items(page: dict) -> list[dict]:
    """tileGridDesktop.items[] -> список {sku, name, price, oldPrice, url, ...}. Отдаёт ВСЕ валидные товары с этой страницы выдачи - лимит и постраничный сбор делает search()."""
    grid = widget(page, "tileGridDesktop")
    raw_items = (grid or {}).get("items", [])
    out = []
    skipped = 0
    for it in raw_items:
        ms = it.get("mainState", []) if isinstance(it, dict) else []
        price_block = next((s.get("priceV2") for s in ms if s.get("type") == "priceV2"), None)
        prices = (price_block or {}).get("price", [])
        price = price_to_number(next((p["text"] for p in prices if p.get("textStyle") == "PRICE"), None))
        old_price = price_to_number(next((p["text"] for p in prices if p.get("textStyle") == "ORIGINAL_PRICE"), None))
        name = next((s.get("textDS", {}).get("text") for s in ms if s.get("id") == "name"), None)
        url = clean_url(it.get("action", {}).get("link"))
        sku = str(it.get("sku") or it.get("id") or sku_from_url(url) or "") or None
        if not sku or not price:
            skipped += 1
            continue
        out.append({
            "sku": sku, "name": name, "price": price,
            "oldPrice": old_price if old_price and old_price > price else None,
            "url": url,
        })
    if skipped:
        logger.debug("В выдаче пропущено %d элементов без sku/цены (баннеры и т.п.)", skipped)
    return out


def paginator_next_page(page: dict) -> str | None:
    """nextPage из infiniteVirtualPaginator - готовый путь+query для следующей страницы выдачи, отдаёт сам Ozon (там же непрозрачные paginator_token/search_page_state/start_page_id)."""
    for name in ("infiniteVirtualPaginator", "paginator"):
        w = widget(page, name)
        if w and w.get("nextPage"):
            return w["nextPage"]
    return None


def parse_characteristics(base_page: dict, page2: dict) -> dict[str, str]:
    """Полный список (webCharacteristics, обычно на "странице 2"), с запасным вариантом webShortCharacteristics с базовой страницы."""
    full = widget(page2, "webCharacteristics")
    out: dict[str, str] = {}
    if full and full.get("characteristics"):
        for block in full["characteristics"]:
            for item in block.get("short", []) + block.get("long", []):
                name = clean_text(item.get("name"))
                values = item.get("values", [])
                value = clean_text("; ".join(v.get("text", "") for v in values))
                if name and value:
                    out[name] = value
        if out:
            return out

    short = widget(base_page, "webShortCharacteristics")
    for c in (short or {}).get("characteristics", []):
        title = rs_text(c.get("title", {}).get("textRs")) if isinstance(c.get("title"), dict) else clean_text(c.get("title"))
        value = rs_text(c.get("values")) or rs_text(c.get("contentRS"))
        if title and value:
            out[title] = value
    return out


def parse_description(page2: dict) -> str:
    """webDescription.richAnnotationJson -> обычный текст, с запасным
    коротким summary-описанием, если рич-контента нет."""
    rich = next((w for w in widgets(page2, "webDescription") if w.get("richAnnotationJson")), None)
    if rich:
        ra = rich["richAnnotationJson"]
        if isinstance(ra, str):
            try:
                ra = json.loads(ra)
            except json.JSONDecodeError:
                ra = {}
        texts: list[str] = []

        def walk(node):
            if isinstance(node, list):
                for n in node:
                    walk(n)
                return
            if not isinstance(node, dict):
                return
            content = node.get("content")
            if node.get("type") == "text" and isinstance(content, str):
                texts.append(content)
                return
            if isinstance(content, list) and content and all(isinstance(c, str) for c in content):
                # billboard/chess/roll-блоки: текст лежит как {"text"|"title": {..., "content": ["строка1", "строка2", ...]}}
                texts.extend(c.strip() for c in content if c.strip())
                return
            keys = list(node.keys())
            if "title" in node and "text" in node:
                # В самом JSON ключ "text" идёт раньше "title", поэтому обходим "title" первым.
                keys = ["title", "text"] + [k for k in keys if k not in ("title", "text")]
            for k in keys:
                v = node[k]
                if isinstance(v, (dict, list)):
                    walk(v)

        walk(ra.get("content", ra))
        text = clean_text(" ".join(texts))
        if text:
            return text

    short_desc = next((w for w in widgets(page2, "webDescription") if w.get("characteristics")), None)
    if short_desc:
        parts = [f"{c.get('title', '')}: {c.get('content', '')}" for c in short_desc["characteristics"]]
        return clean_text("; ".join(parts))
    return ""


# ====================== "ЕСТЬ ДЕШЕВЛЕ" ======================

# Это отдельный HTTP-запрос на каждый товар (GET /modal/otherOffersFromSellers?product_id={sku}&sort=price&page_changed=true -> виджет webSellerList,"sellers": [...]), поэтому включается флагом CHECK_CHEAPER_OFFERS в config.toml (по умолчанию True).

def find_cheaper_offer(session: OzonSession, sku: str, own_price_rub: float | None) -> float | None:
    """Возвращает цену самого дешёвого стороннего предложения, если она меньше own_price_rub. Иначе - None (в т.ч. если предложений нет, запрос не удался, или own_price_rub неизвестна)."""
    if not sku or own_price_rub is None:
        return None
    path = f"/modal/otherOffersFromSellers?product_id={sku}&sort=price&page_changed=true"
    try:
        data = session.fetch_json(path)
    except Exception as e:
        logger.warning("Товар %s: не удалось проверить предложения других продавцов: %s", sku, e)
        return None
    sellers = (widget(data, "webSellerList") or {}).get("sellers") or []
    if not sellers:
        return None

    cheapest = price_to_number(((sellers[0].get("price") or {}).get("cardPrice") or {}).get("price"))
    if cheapest is None:
        return None
    return cheapest if cheapest < own_price_rub else None


def parse_details(base_page: dict, page2: dict) -> dict[str, Any]:
    """base_page + page2 (тот же путь с ?layout_container=pdpPage2column&layout_page_index=2) -> нормализованный товар."""
    heading = widget(base_page, "webProductHeading")
    price_w = widget(base_page, "webPrice")

    seo_links = (base_page.get("seo") or {}).get("link") or [{}]
    url = clean_url(seo_links[0].get("href"))
    name = (heading or {}).get("title") or (base_page.get("seo") or {}).get("title")
    price_rub = price_to_number((price_w or {}).get("cardPrice")) or price_to_number((price_w or {}).get("price"))

    return {
        "link": url,
        "name": clean_text(name) if name else None,
        "price": format_price_rub(price_rub),
        "description": parse_description(page2),
        "characteristics": parse_characteristics(base_page, page2),
    }


# ====================== ПУТИ / ВХОДНЫЕ ССЫЛКИ ======================

def product_path(product: str) -> str:
    """sku, полная ссылка или slug -> путь "/product/.../"."""
    p = str(product or "").strip()
    if not p:
        raise ValueError("нужен sku, ссылка на товар или slug")
    if re.match(r"^https?://", p):
        return urlparse(p).path.rstrip("/") + "/"
    if p.startswith("/product/"):
        return p.rstrip("/") + "/"
    if p.isdigit():
        return f"/product/{p}/"
    return f"/product/{p.strip('/')}/"


def is_product_url(url: str) -> bool:
    return "/product/" in url


def extract_search_text(url: str) -> str | None:
    qs = parse_qs(urlparse(url).query)
    if "text" in qs and qs["text"]:
        return qs["text"][0]
    return None


OZON_RESERVED_PARAMS = {"text"}  # обрабатывается отдельно, не передаётся как фильтр


# ====================== ВЫСОКОУРОВНЕВЫЕ ОПЕРАЦИИ ======================

def search(session: OzonSession, base_path: str, query: str, limit: int = 12, extra: dict | None = None) -> list[dict]:
    """Собирает товары, переходя по nextPage, пока не наберётся `limit` штук.
    base_path - путь из исходной ссылки - сохраняет фильтры категории/бренда, зашитые в самом пути."""
    qs = {"text": query, "from_global": "true", **(extra or {})}
    path = f"{base_path}?{urlencode(qs)}"
    items: list[dict] = []
    seen_skus: set[str] = set()

    # Потолок страниц считается от limit (с запасом); MAX_SEARCH_PAGES - это ДОПОЛНИТЕЛЬНЫЙ пол для маленьких limit.
    effective_max_pages = min(max(MAX_SEARCH_PAGES, (limit + 4) // 5 + 3), 300)
    for page_num in range(1, effective_max_pages + 1):
        logger.info("Запрашиваем страницу выдачи %d: %s", page_num, path)
        page_json = session.fetch_json(path)
        batch = parse_search_items(page_json)
        new_items = [it for it in batch if it["sku"] not in seen_skus]
        seen_skus.update(it["sku"] for it in new_items)
        items.extend(new_items)
        logger.info("Страница выдачи %d: %d новых товаров, всего %d/%d", page_num, len(new_items), len(items), limit)

        if len(items) >= limit:
            break

        next_path = paginator_next_page(page_json)
        if not next_path:
            logger.info("Пагинация закончилась (нет nextPage) на странице %d", page_num)
            break
        if not new_items:
            logger.warning("Страница %d не дала новых товаров, останавливаемся во избежание цикла", page_num)
            break

        path = next_path
        time.sleep(REQUEST_DELAY_S)

    return items[:limit]


def details(session: OzonSession, product: str) -> dict:
    path = product_path(product)
    logger.info("Товар %s: запрашиваем основную страницу", path)
    base_page = session.fetch_json(path)
    logger.info("Товар %s: основная страница получена, запрашиваем страницу 2", path)
    time.sleep(REQUEST_DELAY_S)
    page2 = session.fetch_json(f"{path}?layout_container=pdpPage2column&layout_page_index=2")
    logger.info("Товар %s: обе страницы получены, разбираем", path)
    result = parse_details(base_page, page2)

    if CHECK_CHEAPER_OFFERS:
        sku = sku_from_url(result.get("link"))
        own_price_rub = price_to_number(result.get("price"))
        if sku and own_price_rub is not None:
            time.sleep(REQUEST_DELAY_S)
            cheaper = find_cheaper_offer(session, sku, own_price_rub)
            if cheaper is not None:
                result["price"] = f"{result['price']} (есть дешевле от {format_price_rub(cheaper)})"

    return result


# ====================== MAIN ======================

def main(url: str | None = None, output: str | None = None, num: int | None = None) -> None:
    """num не передан -> берётся DEFAULT_PRODUCT_COUNT из config.toml."""
    attach_file_log(logger, "ozon_parser.log")
    target_count = num or DEFAULT_PRODUCT_COUNT

    logger.info("=== ЗАПУСК ПАРСЕРА OZON ===")
    logger.info("Целевое количество товаров: %d", target_count)

    if not url:
        url = input("🔗 Вставьте ссылку на страницу поиска (или на товар) Ozon: ").strip()

    products: list[dict] = []
    session = OzonSession()

    try:
        if is_product_url(url):
            print("Режим: один товар")
            logger.info("Режим: один товар (%s)", url)
            products.append(details(session, url))
        else:
            query = extract_search_text(url)
            if not query:
                print(
                    "Не нашёл ?text=... в этой ссылке.\n"
                    "Поддерживаются: ссылка на страницу поиска Ozon (/search/?text=...) или прямая ссылка на товар (/product/...)."
                )
                return

            print(f"Режим: поиск по запросу «{query}», нужно товаров: {target_count}")
            logger.info("Режим: поиск по запросу «%s», нужно товаров: %d", query, target_count)
            extra = extra_query_params(url, OZON_RESERVED_PARAMS)
            if extra:
                print(f"Доп. фильтры из ссылки: {extra}")
            base_path = urlparse(url).path or "/search/"
            items = search(session, base_path, query, limit=target_count, extra=extra)
            print(f"Найдено в выдаче: {len(items)} товаров\n")
            logger.info("Найдено в выдаче (после пагинации): %d товаров", len(items))

            for i, item in enumerate(items, start=1):
                name_preview = preview_text(item.get("name"), 60)
                print(f"[{i}/{len(items)}] {item['sku']} {name_preview}")
                logger.info("[%d/%d] %s %s", i, len(items), item['sku'], (item.get('name') or '')[:60])
                try:
                    products.append(details(session, item["url"] or item["sku"]))
                    logger.info("Товар %s: успешно собран", item["sku"])
                except Exception as e:
                    logger.error("Товар %s пропущен: %s", item["sku"], e)
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
        session.shutdown()
        output_path = save_products(products, "ozon_products.json", output)
        print(f"\nГотово! Собрано товаров: {len(products)}")
        print(f"Файл сохранён: {output_path}")
        logger.info("=== ПАРСЕР OZON ЗАВЕРШИЛ РАБОТУ ===")


if __name__ == "__main__":
    main()