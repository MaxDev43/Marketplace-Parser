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

    return main_func(**kwargs) if kwargs else main_func(url)


def main():
    parser = argparse.ArgumentParser(description="Запуск парсера маркетплейса")
    parser.add_argument("url", nargs="?", default=None, help="Ссылка на маркетплейс")
    parser.add_argument(
        "-n", "--num", type=int, default=None,
        help=f"Сколько товаров спарсить (по умолчанию {DEFAULT_PRODUCT_COUNT})",
    )
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    url = args.url
    if not url:
        url = input("🔗 Вставьте ссылку на страницу поиска: ").strip()

    num = args.num if args.num is not None else DEFAULT_PRODUCT_COUNT

    marketplace = detect_marketplace(url)

    if marketplace == "ozon":
        print("Определён маркетплейс: Ozon")
        call_parser(ozon, url, args.output, num)

    elif marketplace == "wildberries":
        print("Определён маркетплейс: Wildberries")
        call_parser(wildberries, url, args.output, num)

    else:
        raise ValueError(f"Не удалось определить маркетплейс: {url}")


if __name__ == "__main__":
    try:
        main()
    finally:
        input("\nНажмите Enter для выхода...")