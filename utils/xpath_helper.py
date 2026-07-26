"""
вспомогательные функции для работы с xpath в beautifulsoup"""
from typing import Optional
from bs4 import BeautifulSoup
from lxml import etree, html


def find_by_xpath(soup: BeautifulSoup, xpath: str) -> Optional[any]:
    """
    находит первый элемент по xpath

    аргументы:
        soup: объект beautifulsoup
        xpath: xpath-выражение

    возвращает:
        найденный элемент или none
"""
    try:
        # конвертируем beautifulsoup в lxml tree
        dom = html.fromstring(str(soup))
        elements = dom.xpath(xpath)
        if elements:
            return elements[0]
        return None
    except Exception:
        return None


def get_text_by_xpath(soup: BeautifulSoup, xpath: str) -> Optional[str]:
    """
    извлекает текст элемента по xpath

    аргументы:
        soup: объект beautifulsoup
        xpath: xpath-выражение

    возвращает:
        текст элемента или none
"""
    try:
        element = find_by_xpath(soup, xpath)
        if element is not None:
            # очищаем текст от лишних пробелов и переносов строк
            text = element.text_content() if hasattr(element, 'text_content') else str(element)
            return text.strip()
        return None
    except Exception:
        return None


def get_attribute_by_xpath(soup: BeautifulSoup, xpath: str, attribute: str) -> Optional[str]:
    """
    извлекает значение атрибута элемента по xpath

    аргументы:
        soup: объект beautifulsoup
        xpath: xpath-выражение
        attribute: название атрибута

    возвращает:
        значение атрибута или none
"""
    try:
        element = find_by_xpath(soup, xpath)
        if element is not None and hasattr(element, 'get'):
            value = element.get(attribute)
            if value:
                return value.strip()
        return None
    except Exception:
        return None


def xpath_exists(soup: BeautifulSoup, xpath: str) -> bool:
    """
    проверяет существование элемента по xpath

    аргументы:
        soup: объект beautifulsoup
        xpath: xpath-выражение

    возвращает:
        true если элемент существует, иначе false
"""
    try:
        element = find_by_xpath(soup, xpath)
        return element is not None
    except Exception:
        return False

