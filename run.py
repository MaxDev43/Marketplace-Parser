import argparse
import inspect
from pathlib import Path
from urllib.parse import urlparse
import sys
import tomllib

from parsers import ozon, wildberries

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.toml", "rb") as f:
    _config = tomllib.load(f)

DEFAULT_PRODUCT_COUNT = _config["common"]["DEFAULT_PRODUCT_COUNT"]

MODULES = {"ozon": ozon, "wildberries": wildberries}


def detect_marketplace(url: str) -> str:
    netloc = urlparse(url).netloc.lower()

    if "ozon.ru" in netloc:
        return "ozon"
    if "wildberries.ru" in netloc or "wb.ru" in netloc:
        return "wildberries"

    return "unknown"


def call_parser(module, url=None, output=None, num=None):
    """Вызывает main() парсера, передавая только принимаемые им аргументы."""
    main_func = module.main
    sig = inspect.signature(main_func)
    kwargs = {}

    if "url" in sig.parameters:
        kwargs["url"] = url
    if "output" in sig.parameters and output is not None:
        kwargs["output"] = output
    if "num" in sig.parameters and num is not None:
        kwargs["num"] = num

    return main_func(**kwargs)


def main():
    parser = argparse.ArgumentParser(description="Запуск парсера маркетплейса")
    parser.add_argument("url", nargs="?", default=None, help="Ссылка на маркетплейс (поиск или товар)")
    parser.add_argument(
        "-n", "--num", type=int, default=None,
        help=f"Сколько товаров спарсить (по умолчанию {DEFAULT_PRODUCT_COUNT})",
    )
    parser.add_argument("-o", "--output", default=None, help="Имя выходного файла в output/")
    parser.add_argument(
        "-m", "--marketplace", choices=["ozon", "wildberries"], default=None,
        help="Явно указать маркетплейс, если авто-определение по домену не подходит",
    )
    args = parser.parse_args()

    url = args.url
    if not url:
        url = input("🔗 Вставьте ссылку на маркетплейс (поиск или товар): ").strip()

    marketplace = args.marketplace or detect_marketplace(url)

    if marketplace not in MODULES:
        raise ValueError(f"Не удалось определить маркетплейс: {url}\n"
                          f"Укажите явно флагом -m ozon / -m wildberries")

    print(f"Маркетплейс: {marketplace}")
    call_parser(MODULES[marketplace], url, args.output, args.num)


if __name__ == "__main__":
    try:
        main()
    finally:
        input("\nНажмите Enter для выхода...")