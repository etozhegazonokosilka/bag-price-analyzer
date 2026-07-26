"""
тесты классификации доменов и товарных ссылок"""

import unittest

from utils.domain import domain_of, is_product_page_url, is_supported


class DomainUtilitiesTests(unittest.TestCase):
    def test_domain_normalization_handles_subdomains_and_regional_suffixes(self):
        cases = (
            ("https://shop.rebag.com/products/demo", "rebag.com"),
            ("https://www.ebay.co.uk/itm/123456", "ebay.co.uk"),
            ("https://poshmark.com/listing/demo", "poshmark.com"),
        )

        for url, expected_domain in cases:
            with self.subTest(url=url):
                self.assertEqual(domain_of(url), expected_domain)

    def test_supported_domain_filter_rejects_unknown_sites(self):
        self.assertTrue(is_supported("https://www.ebay.com/itm/123456"))
        self.assertFalse(is_supported("https://example.com/products/demo"))

    def test_product_page_filter_separates_cards_from_catalogs(self):
        cases = (
            ("https://www.ebay.co.uk/itm/123456", True),
            ("https://www.ebay.com/b/Handbags/169291", False),
            ("https://poshmark.com/listing/demo-item-123", True),
            ("https://poshmark.com/category/Women-Bags", False),
            (
                "https://www.vestiairecollective.com/women-bags/"
                "handbags/chanel/black-leather-123456.shtml",
                True,
            ),
            (
                "https://www.vestiairecollective.com/women-bags/"
                "handbags/chanel/",
                False,
            ),
            ("https://www.fashionphile.com/products/chanel-bag-123", True),
            ("https://www.fashionphile.com/shop/chanel", False),
        )

        for url, expected_result in cases:
            with self.subTest(url=url):
                self.assertEqual(is_product_page_url(url), expected_result)


if __name__ == "__main__":
    unittest.main()
