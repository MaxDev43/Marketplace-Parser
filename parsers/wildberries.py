import json
import logging
import random
import re
import time
from pathlib import Path
import tomllib
import subprocess
import sys

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Предотвращает WinError 6 из-за повторного quit() в GC-деструкторе undetected-chromedriver
uc.Chrome.__del__ = lambda self: None

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

LOG_DIR = BASE_DIR / "logs"
OUTPUT_DIR = BASE_DIR / "output"
LOG_FILE = LOG_DIR / "wildberries_parser.log"

with open(BASE_DIR / "config.toml", "rb") as f:
    _config = tomllib.load(f)

DEFAULT_PRODUCT_COUNT = _config["common"]["DEFAULT_PRODUCT_COUNT"]
WAIT_FOR_REGION_SELECTION = _config["wildberries"]["WAIT_FOR_REGION_SELECTION"]
MAX_ANTIBOT_ATTEMPTS = _config["wildberries"]["MAX_ANTIBOT_ATTEMPTS"]
ANTIBOT_WAIT_BEFORE_RETRY = _config["wildberries"]["ANTIBOT_WAIT_BEFORE_RETRY"]
ANTIBOT_WAIT_AFTER_REFRESH = _config["wildberries"]["ANTIBOT_WAIT_AFTER_REFRESH"]
ANTIBOT_MARKERS = _config["wildberries"]["ANTIBOT_MARKERS"]


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("parsers.wildberries")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.info("=== ЛОГГЕР WILDBERRIES ИНИЦИАЛИЗИРОВАН ===")
    return logger


logger = setup_logger()

def get_chrome_major_version() -> int | None:
    """Определяет установленную major-версию Chrome на момент запуска.

    Передаётся в version_main, чтобы undetected_chromedriver скачивал
    chromedriver строго под неё, а не под "последнюю доступную" — иначе
    из-за известного расхождения в uc (issue #2158) можно получить
    driver новее реально установленного Chrome после его автообновления.
    Определяется заново при каждом запуске, поэтому не протухает.
    """
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Google\Chrome\BLBeacon")
            version, _ = winreg.QueryValueEx(key, "version")
            winreg.CloseKey(key)
            major = int(version.split(".")[0])
            logger.info("✅ Обнаружена версия Chrome: %s (major %d)", version, major)
            return major
        except Exception as e:
            logger.warning("Не удалось определить версию Chrome через реестр (%s)", e)
            return None

    if sys.platform == "darwin":
        binaries = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        binaries = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]

    for binary in binaries:
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
            match = re.search(r"(\d+)\.", result.stdout)
            if match:
                major = int(match.group(1))
                logger.info("✅ Обнаружена версия Chrome (%s): major %d", binary, major)
                return major
        except Exception:
            continue

    logger.warning("Не удалось определить версию Chrome автоматически")
    return None

def get_driver():
    """Создаёт и возвращает undetected Chrome driver."""
    logger.info("Создаём undetected_chromedriver...")
    chrome_major = get_chrome_major_version()
    options = uc.ChromeOptions()
    options.page_load_strategy = "eager"
    # options.add_argument("--headless=new")         # раскомментировать при необходимости
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    driver = uc.Chrome(
        options=options,
        use_subprocess=True,
        version_main=chrome_major,
    )

    driver.set_page_load_timeout(12)
    logger.info("Браузер успешно создан (Chrome %s)",
                chrome_major if chrome_major else "auto")
    return driver


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================

def wait_for_user_region_selection():
    """Останавливает выполнение и ждёт, пока пользователь вручную выберет регион."""
    print("\n" + "="*70)
    print("ОЖИДАНИЕ ВЫБОРА РЕГИОНА")
    print("1. В открывшемся браузере выберите нужный регион (город), для точности цены.")
    print("2. Дождитесь, пока страница полностью обновится после выбора региона")
    print("3. Когда всё готово — нажмите клавишу **ENTER** в этом окне консоли.")
    print("="*70)

    input("Нажмите ENTER для продолжения парсинга... ")
    print("✅ Регион выбран. Продолжаем работу...\n")
    time.sleep(2)


def normalize_url(url: str) -> str:
    """Приводит относительные ссылки к полному виду"""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.wildberries.ru" + url
    return url


def is_product_url(url: str) -> bool:
    """Проверяет, является ли ссылка ссылкой на карточку товара WB"""
    return bool(url and "/catalog/" in url and "/detail.aspx" in url)


def clean_text(value: str) -> str:
    """Очищает текст от лишних пробелов и переносов"""
    return re.sub(r"\s+", " ", value or "").strip()


def get_body_text(driver) -> str:
    """Возвращает весь видимый текст страницы"""
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return ""


def is_antibot_page(driver, extra_text: str = "") -> bool:
    """Проверка на наличие страницы AntiBot / Cloudflare"""
    haystack = " ".join([clean_text(driver.title), clean_text(get_body_text(driver)), clean_text(extra_text)]).lower()
    result = any(marker in haystack for marker in ANTIBOT_MARKERS)
    logger.debug("Проверка на AntiBot (WB): %s", "ОБНАРУЖЕН" if result else "нет")
    return result


def collect_product_links(driver, max_items: int) -> list[str]:
    """Собирает ссылки на товары с поисковой страницы WB.
    Игнорирует блок «Возможно, вам понравится» и другие слайдеры рекомендаций."""
    logger.info("Начинаем сбор ссылок на товары WB (нужно %d)", max_items)
    links: list[str] = []
    seen = set()
    scroll_rounds = 10  # количество скролла

    for step in range(scroll_rounds):
        new_links = 0

        xpath = (
            "//a[contains(@class, 'product-card__link') "
            "and contains(@href, '/detail.aspx') "
            "and not(ancestor::div[contains(@class, 'sliderContainer--k0JDd')]) "
            "and not(ancestor::section[contains(@class, 'j-b-recommended-goods-wrapper')])]"
        )

        for a in driver.find_elements(By.XPATH, xpath):
            try:
                href = normalize_url(a.get_attribute("href") or "")
                if is_product_url(href) and href not in seen:
                    seen.add(href)
                    links.append(href)
                    new_links += 1
            except Exception:
                continue

        logger.debug("Скролл %d/%d: найдено %d новых ссылок | Всего: %d",
                     step + 1, scroll_rounds, new_links, len(links))

        if len(links) >= max_items:
            logger.info("Достигнут лимит %d товаров, прерываем сбор", max_items)
            break

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(0.3, 0.7))

    logger.info("Сбор ссылок завершён. Собрано: %d товаров (рекомендации исключены)", len(links))
    return links


def safe_text(driver, by, selector, default="Не найдено") -> str:
    """Безопасное извлечение текста элемента"""
    try:
        elems = driver.find_elements(by, selector)
        if elems:
            return clean_text(elems[0].text) or default
    except Exception:
        pass
    return default


# ====================== ОТКРЫТИЕ ПАНЕЛИ ======================

def open_product_details_panel(driver):
    """Открывает панель характеристик и описания."""
    logger.debug("=== ОТКРЫТИЕ ПАНЕЛИ ХАРАКТЕРИСТИК ===")

    button_patterns = [
        "//button[contains(., 'Характеристики и описание')]",
        "//button[contains(., 'Характеристики')]",
        "//button[contains(., 'О товаре')]",
        "//button[contains(., 'Все характеристики')]",
        "//*[contains(@class, 'btnDetail') or contains(@class, 'moreAboutButton')]",
    ]

    clicked = False
    for pattern in button_patterns:
        try:
            buttons = WebDriverWait(driver, 6).until(
                EC.presence_of_all_elements_located((By.XPATH, pattern))
            )
            for btn in buttons:
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(0.2)
                    driver.execute_script("arguments[0].click();", btn)
                    logger.info(f"✅ Кликнули кнопку: {pattern}")
                    clicked = True
                    break
            if clicked:
                break
        except Exception:
            continue

    if not clicked:
        try:
            any_btn = driver.find_element(By.XPATH, "//*[contains(text(), 'Характеристики') and (self::button or contains(@role, 'button'))]")
            driver.execute_script("arguments[0].click();", any_btn)
            logger.info("✅ Кликнули по любому элементу с текстом 'Характеристики'")
            clicked = True
        except:
            pass

    if clicked:
        try:
            WebDriverWait(driver, 6).until(
                EC.any_of(
                    EC.presence_of_element_located((By.XPATH, "//table[contains(@class, 'table--UAo6u')]")),
                    EC.presence_of_element_located((By.XPATH, "//table[contains(@class, 'articleTable')]")),
                    EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'characteristics')]"))
                )
            )
            time.sleep(0.5)
            logger.info("Панель открыта, таблицы появились")
            return True
        except TimeoutException:
            logger.warning("Панель открыта, но таблицы не появились за 15 сек")
    else:
        logger.warning("❌ Ни одну кнопку открытия панели найти не удалось")

    return False


# ====================== ИЗВЛЕЧЕНИЕ ДАННЫХ ======================

def extract_price(driver) -> str:
    logger.debug("Извлечение цены...")
    selectors = [
        "//h2[contains(@class, 'mo-typography_variant_title2')]",
        "//h3[contains(@class, 'mo-typography_variant_title3')]",
        "//ins[contains(@class, 'price__lower-price')]",
        "//div[contains(@class, 'price')]//span[contains(text(), '₽')]"
    ]
    for sel in selectors:
        try:
            for el in driver.find_elements(By.XPATH, sel):
                txt = clean_text(el.text)
                if "₽" in txt and re.search(r"\d", txt):
                    return txt
        except Exception:
            continue
    return "Не найдено"


def extract_name(driver) -> str:
    """Извлечение названия (бренд + заголовок) — работает и в полноэкранном, и в полуэкранном режиме"""
    logger.debug("Извлечение названия (brand + title)")

    # 1. Бренд
    brand = safe_text(driver, By.XPATH,
        "//a[contains(@class, 'productHeaderBrand')]//span | "
        "//span[contains(@class, 'brandBadgeText')] | "
        "//div[contains(@class, 'brand')]//span | "
        "//span[contains(@class, 'brand-name')] | "
        "//div[contains(@class, 'productHeader__brand')]//span",
        default="")

    # 2. Название
    title_selectors = [
        "//h1",
        "//h2[contains(@class, 'productTitle')]",
        "//h2[contains(@class, 'mo-typography_variant_title3')]",
        "//h2[contains(@class, 'mo-typography_variant_title2')]",
        "//div[contains(@class, 'product-page__title')]",
        "//h2[contains(@class, 'title')]",
    ]

    title = ""
    for sel in title_selectors:
        title = safe_text(driver, By.XPATH, sel, default="")
        if title and len(title.strip()) > 15:
            break

    # 3. Финальная сборка названия
    if brand and title and brand.lower() not in title.lower():
        full_name = f"{brand} {title}".strip()
    elif title:
        full_name = title.strip()
    elif brand:
        full_name = brand.strip()
    else:
        full_name = safe_text(driver, By.XPATH, "//h1", default="Не найдено")

    logger.debug("Извлечено название: %s", full_name[:100])
    return full_name


def extract_description(driver) -> str:
    selectors = [
        "//section[@id='section-description']//p",
        "//div[contains(@class, 'mdDescriptionText')]//p",
        "//div[contains(@class, 'descriptionText')]//p",
        "//div[contains(@class, 'content--zb_r9')]//p",
    ]
    for sel in selectors:
        desc = safe_text(driver, By.XPATH, sel)
        if len(desc) > 80:
            return desc
    return "Не найдено"


def extract_characteristics(driver) -> dict[str, str]:
    """Извлекает характеристики товара."""
    logger.debug("Извлечение характеристик...")
    chars = {}

    tables = driver.find_elements(By.XPATH,
        "//table[contains(@class, 'table--UAo6u') or contains(@class, 'articleTable') or contains(@class, 'characteristics')]")

    if not tables:
        logger.debug("Таблицы по известным классам не найдены — пробуем любые <table> на странице")
        tables = driver.find_elements(By.XPATH, "//table")

    for table in tables:
        try:
            rows = table.find_elements(By.XPATH, ".//tr")
            for row in rows:
                cells = row.find_elements(By.XPATH, ".//th | .//td")
                if len(cells) >= 2:
                    key = clean_text(cells[0].text).rstrip(":").strip()
                    value = clean_text(cells[1].text)
                    if key and value and len(key) < 150:
                        chars[key] = value
        except Exception:
            continue

    # Fallback, если таблиц не нашли
    if len(chars) < 5:
        try:
            body = get_body_text(driver)
            lines = [line.strip() for line in body.split('\n') if ':' in line and len(line) < 250]
            for line in lines:
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = clean_text(key).rstrip(":").strip()
                    value = clean_text(value)
                    if key and value and len(key) > 1:
                        chars[key] = value
        except Exception:
            pass

    logger.info("Извлечено характеристик: %d", len(chars))
    if len(chars) == 0:
        logger.warning("Характеристики не найдены — панель, скорее всего, не открылась")

    return chars


# ====================== ПАРСИНГ ОДНОЙ КАРТОЧКИ ======================

def parse_product_page(driver, url: str, product_index: int, total: int) -> dict | None:
    logger.info("=== ПАРСИНГ ТОВАРА %d/%d ===", product_index, total)
    logger.info("URL: %s", url)

    for attempt in range(1, MAX_ANTIBOT_ATTEMPTS + 1):
        if product_index <= 4:
            time.sleep(random.uniform(0.6, 1.0))
        else:
            time.sleep(random.uniform(0.2, 0.6))

        driver.get(url)
        time.sleep(random.uniform(0.6, 1.2))

        try:
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "h1")))
        except TimeoutException:
            pass

        # Извлекаем название
        name = extract_name(driver)
        price = extract_price(driver)

        # Открываем панель для описания и характеристик
        open_product_details_panel(driver)

        if not is_antibot_page(driver):
            logger.info("AntiBot НЕ обнаружен")
            break

        logger.warning("ANTI BOT на попытке %d/%d", attempt, MAX_ANTIBOT_ATTEMPTS)
        print(f"\n⚠️ ANTI BOT на товаре {product_index}/{total} (попытка {attempt}/{MAX_ANTIBOT_ATTEMPTS})")

        if attempt == 1:
            time.sleep(ANTIBOT_WAIT_BEFORE_RETRY)
        elif attempt == 2:
            driver.refresh()
            time.sleep(ANTIBOT_WAIT_AFTER_REFRESH)
        else:
            break

    if is_antibot_page(driver):
        print(f"Товар {product_index}/{total} → AntiBot, отложен")
        return None

    description = extract_description(driver)
    characteristics = extract_characteristics(driver)

    logger.info("=== ТОВАР %d УСПЕШНО СПАРСЕН ===", product_index)

    return {
        "link": url,
        "name": name,
        "price": price,
        "description": description,
        "characteristics": characteristics,
    }


# ====================== MAIN ======================

def main(url: str = None, output: str = None, num: int = None):
    """num не передан → берётся DEFAULT_PRODUCT_COUNT из config.toml."""
    target_count = num or DEFAULT_PRODUCT_COUNT

    logger.info("=== ЗАПУСК ПАРСЕРА WILDBERRIES ===")
    logger.info("Целевое количество товаров: %d", target_count)

    if not url:
        url = input("🔗 Вставьте ссылку на страницу поиска Wildberries: ").strip()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    driver = None
    products = []
    failed_links = []

    try:
        driver = get_driver()
        print("Открываем страницу поиска WB...")
        driver.get(url)
        time.sleep(1.5)

        if WAIT_FOR_REGION_SELECTION:
            wait_for_user_region_selection()     # Ожидание ручного выбора региона

        WebDriverWait(driver, 6).until(lambda d: len(d.find_elements(By.XPATH, "//a[contains(@class, 'product-card__link')]")) > 0)

        links = collect_product_links(driver, max_items=target_count)
        print(f"Найдено товаров: {len(links)}\n")

        to_process = min(target_count, len(links))

        for i, link in enumerate(links[:to_process], start=1):
            print(f"Товар {i}/{to_process}")
            try:
                product = parse_product_page(driver, link, i, to_process)
                if product is None:
                    failed_links.append(link)
                    continue

                products.append(product)
                print(f"Товар {i}/{to_process} → OK: {product['name'][:80]}...")
                time.sleep(random.uniform(0.8, 1.4))

            except Exception as e:
                logger.error("Ошибка на товаре %d: %s", i, e)
                print(f"Товар {i}/{to_process} → Ошибка")

        if failed_links:
            print(f"\n🔄 ПОВТОРНАЯ ПРОВЕРКА {len(failed_links)} товаров...")
            for idx, link in enumerate(failed_links, start=1):
                try:
                    product = parse_product_page(driver, link, idx, len(failed_links))
                    if product:
                        products.append(product)
                        print(f"Повтор {idx} → OK")
                except Exception:
                    print(f"Повтор {idx} → Ошибка")

    except KeyboardInterrupt:
        print("\nПрервано пользователем")
    except Exception as e:
        logger.error("ФАТАЛЬНАЯ ОШИБКА: %s", e)
        print("Критическая ошибка!")
    finally:
        if 'driver' in locals() and driver is not None:
            try:
                driver.quit()
            except:
                pass

        output_filename = output or "wildberries_products.json"
        output_path = OUTPUT_DIR / output_filename

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

        print(f"\nГотово ✅! Собрано товаров: {len(products)}")
        print(f"Файл сохранён: {output_path}\n")


if __name__ == "__main__":
    try:
        main()
    finally:
        input("\nНажмите Enter для выхода...")