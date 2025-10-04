import inspect
import re
import time
from dataclasses import dataclass
from enum import IntEnum
from functools import cache
from http.cookiejar import CookieJar
from inspect import _empty

import browser_cookie3
from error_helper import error, warning
from fake_useragent import UserAgent
from requests import Response, Session

from ..config import get_config, get_pre_creation_hooks
from ..retries import retry_timeouts

@dataclass
class ApiPart:
    description: str
    image_url: str
    datasheet_url: str
    supplier_link: str
    SKU: str
    manufacturer: str
    manufacturer_link: str
    MPN: str
    quantity_available: float
    packaging: str
    category_path: list[str]
    parameters: dict[str, str]
    price_breaks: dict[int, float]
    currency: str

    def finalize(self):
        if not self.finalize_hook():
            return False
        for pre_creation_hook in get_pre_creation_hooks():
            pre_creation_hook(self)
        return True

    def finalize_hook(self):
        return True

    def get_part_data(self):
        return {
            "name": self.MPN,
            "description": self.description[:250],
            "link": self.manufacturer_link[:200],
            "active": True,
            "component": True,
            "purchaseable": True,
        }

    def get_manufacturer_part_data(self):
        return {
            "MPN": self.MPN,
            "description": self.description[:250],
            "link": self.manufacturer_link[:200],
        }

    def get_supplier_part_data(self):
        data = {
            "description": self.description[:250],
            "link": self.supplier_link[:200],
            "packaging": self.packaging[:50],
        }
        if self.quantity_available:
            data["available"] = min(float(self.quantity_available), 9999999.0)
        return data

class SupplierSupportLevel(IntEnum):
    OFFICIAL_API = 0
    INOFFICIAL_API = 1
    SCRAPING = 2

class Supplier:
    SUPPORT_LEVEL: SupplierSupportLevel

    def setup(self, **kwargs) -> bool:
        return True

    def _get_setup_params(self):
        return {
            name: None if parameter.default is _empty else parameter.default
            for name, parameter in inspect.signature(self.setup).parameters.items()
            if name not in {"self", "kwargs"}
        }

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        raise NotImplementedError()

    @cache
    def cached_search(self, search_term: str) -> tuple[list[ApiPart], int]:
        return self.search(search_term)

    @property
    def name(self):
        return self.__class__.__name__

    def load_error(self, message):
        error(f"failed to load '{self.name}' supplier module ({message})")
        return False

class ScrapeSupplier(Supplier):
    session: Session
    cookies = CookieJar()

    extra_headers = {}
    fallback_domains = [None]

    def scrape(self, url) -> Response | None:
        if not hasattr(self, "session"):
            self._setup_session()

        for retry in retry_timeouts():
            with retry:
                result = self.session.get(
                    url, headers=self.extra_headers, timeout=self.request_timeout
                )
                if result.status_code == 200:
                    return result

        for fallback in self.fallback_domains:
            fallback_str = f"via '{fallback}' " if fallback else ""
            warning(
                f"failed to get page, retrying in {self.retry_timeout}s {fallback_str}"
                f"with new session and user agent"
            )
            time.sleep(self.retry_timeout)

            self._setup_session()

            fallback_url = DOMAIN_REGEX.sub(DOMAIN_SUB.format(fallback), url) if fallback else url
            for retry in retry_timeouts():
                with retry:
                    result = self.session.get(fallback_url, headers=self.extra_headers)
                    if result.status_code == 200:
                        return result

    def cookies_from_browser(self, name: str, domain_name: str):
        all_browsers = browser_cookie3.all_browsers
        if not (browser := getattr(browser_cookie3, name, None)) or browser not in all_browsers:
            warning(
                f"failed to load cookies from browser '{name}' (not in "
                f"[{', '.join(browser.__name__ for browser in browser_cookie3.all_browsers)}])"
            )
            return

        if not (cookies := browser(domain_name=domain_name)):
            warning(f"browser '{name}' has no cookies set for '{domain_name}'")
            return

        self.cookies = cookies

    def setup_hook(self):
        pass

    def _setup_session(self):
        self.session = Session()
        self.session.cookies.update(self.cookies)
        self.session.headers.update({
            # using iOS User-Agents seems to help to with mouser crawling
            "User-Agent": UserAgent(os=["iOS"]).random,
            "Accept-Language": "en-US,en",
        })

        for retry in retry_timeouts():
            with retry:
                self.setup_hook()

    @property
    def request_timeout(self) -> float:
        return config["request_timeout"] if (config := get_config()) else 5.0

    @property
    def retry_timeout(self) -> float:
        return config["request_timeout"] if (config := get_config()) else 0.0

DOMAIN_REGEX = re.compile(r"(https?://)(?:[^./]*\.?)*/")
DOMAIN_SUB = "\\g<1>{}/"

REMOVE_HTML_TAGS = re.compile(r"<.*?>|&([a-z0-9]+|#[0-9]{1,6}|#x[0-9a-f]{1,6});")

def money2float(money):
    money = MONEY2FLOAT_CLEANUP.sub("", money).strip()
    decimal, fraction = MONEY2FLOAT_SPLIT.match(money).groups()
    decimal = MONEY2FLOAT_CLEANUP2.sub("", decimal).strip()
    fraction = MONEY2FLOAT_CLEANUP2.sub("", fraction).strip()
    return float(f"{decimal}.{fraction}")

MONEY2FLOAT_CLEANUP = re.compile(r"[^(\d,.\-)]")
MONEY2FLOAT_SPLIT = re.compile(r"(.*)(?:\.|,)(\d+)")
MONEY2FLOAT_CLEANUP2 = re.compile(r"[^\d\-]")
