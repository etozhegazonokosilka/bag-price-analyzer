"""
обертка для парсеров с автоматической диагностикой"""
from utils.debug_http import save_debug_html
from utils.domain import domain_of


def wrap_parser_with_debug(parser_func):
    """
    декоратор для парсеров, добавляет автоматическое сохранение HTML при ошибках

    аргументы:
        parser_func: функция парсера

    возвращает:
        обернутая функция
"""
    def wrapper(url: str):
        result = parser_func(url)

        # проверяем, нашлись ли данные
        has_title = result and result.get("title")
        has_price = result and result.get("price") is not None

        # если нет ни названия, ни цены - сохраняем HTML
        if not has_title and not has_price:
            # пытаемся получить HTML из базовой функции
            try:
                from scrapers.base import fetch_html
                soup, diagnosis = fetch_html(url)
                if soup:
                    save_debug_html(url, str(soup), "no_data_parsed")
                    print(f"   ⚠️  Парсер не нашел данные, HTML сохранен для отладки")
            except Exception:
                pass

        return result

    return wrapper


def check_parser_result(url: str, result: dict, html_content: str = None) -> dict:
    """
    проверяет результат парсинга и сохраняет HTML если нужно

    аргументы:
        url: URL страницы
        result: результат парсинга
        html_content: HTML контент (опционально)

    возвращает:
        дополненный result с флагами диагностики
"""
    domain = domain_of(url)

    has_title = result and result.get("title")
    has_price = result and result.get("price") is not None
    has_data = has_title or has_price

    result_info = {
        "has_data": has_data,
        "has_title": has_title,
        "has_price": has_price,
        "needs_debug": not has_data,
    }

    # если нет данных и есть HTML - сохраняем
    if not has_data and html_content:
        filename = save_debug_html(url, html_content, "no_data_parsed")
        result_info["debug_file"] = filename
        print(f"   💾 HTML сохранен: {filename}")
        print(f"   ℹ️  Проверьте файл на наличие капчи, блокировок или ошибок")

    # добавляем в result
    if result:
        result["_debug"] = result_info

    return result

