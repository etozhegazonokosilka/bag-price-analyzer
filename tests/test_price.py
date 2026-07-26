"""
тесты извлечения и валидации цен"""

import unittest

from utils.price import (
    extract_price_from_jsonld,
    is_valid_price_element,
    normalize_currency_code,
    parse_price_and_currency,
)


class PriceUtilitiesTests(unittest.TestCase):
    def test_currency_normalization_supports_codes_and_symbols(self):
        cases = (
            ("руб.", "RUB"),
            ("€", "EUR"),
            ("gbp", "GBP"),
            ("HK$", "HKD"),
            (None, None),
        )

        for raw_value, expected_currency in cases:
            with self.subTest(raw_value=raw_value):
                self.assertEqual(
                    normalize_currency_code(raw_value),
                    expected_currency,
                )

    def test_price_parser_supports_common_number_formats(self):
        cases = (
            ("$1,234.56", (1234.56, "USD")),
            ("1.234,56 EUR", (1234.56, "EUR")),
            ("10 500 RUB", (10500.0, "RUB")),
            ("£875", (875.0, "GBP")),
            ("Sold for 2,450 USD", (2450.0, "USD")),
            ("bag without a price", (None, None)),
        )

        for raw_text, expected_result in cases:
            with self.subTest(raw_text=raw_text):
                self.assertEqual(
                    parse_price_and_currency(raw_text),
                    expected_result,
                )

    def test_price_validation_rejects_shipping_and_unreasonable_values(self):
        self.assertTrue(is_valid_price_element("$2500"))
        self.assertFalse(is_valid_price_element("shipping $25"))
        self.assertFalse(is_valid_price_element("$15000"))

    def test_jsonld_parser_extracts_offer_price_and_currency(self):
        payload = {
            "@type": "Product",
            "offers": {
                "price": "2199.50",
                "priceCurrency": "EUR",
            },
        }

        self.assertEqual(
            extract_price_from_jsonld(payload),
            (2199.5, "EUR"),
        )


if __name__ == "__main__":
    unittest.main()
