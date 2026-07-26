"""
пакет парсеров для различных сайтов"""

from scrapers.base import fetch_html, fetch_html_rendered
from scrapers.poshmark import scrape_poshmark
from scrapers.vestiaire import scrape_vestiaire
from scrapers.ebay import scrape_ebay
from scrapers.rebag import scrape_rebag
from scrapers.tlc import scrape_tlc
from scrapers.universal import scrape_universal
from scrapers.dispatcher import scrape_by_domain
from scrapers.fashionphile import scrape_fashionphile
from scrapers.jolicloset import scrape_jolicloset
from scrapers.yoogiscloset import scrape_yoogiscloset
from scrapers.therealreal import scrape_therealreal
from scrapers.celebrityowned import scrape_celebrityowned
from scrapers.aretrotale import scrape_aretrotale
from scrapers.dallasdesignerhandbags import scrape_dallasdesignerhandbags
from scrapers.popchill import scrape_popchill
from scrapers.designerexchange import scrape_designerexchange
from scrapers.annsfabulousfinds import scrape_annsfabulousfinds

__all__ = [
    "fetch_html",
    "fetch_html_rendered",
    "scrape_poshmark",
    "scrape_vestiaire",
    "scrape_ebay",
    "scrape_rebag",
    "scrape_tlc",
    "scrape_universal",
    "scrape_by_domain",
    "scrape_fashionphile",
    "scrape_jolicloset",
    "scrape_yoogiscloset",
    "scrape_therealreal",
    "scrape_celebrityowned",
    "scrape_aretrotale",
    "scrape_dallasdesignerhandbags",
    "scrape_popchill",
    "scrape_designerexchange",
    "scrape_annsfabulousfinds",
]
