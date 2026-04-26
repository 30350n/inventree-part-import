import inspect
import re
import time
from dataclasses import dataclass
from enum import IntEnum
from http.cookiejar import CookieJar
from typing import Any, Callable, Literal, cast

import browser_cookie3
from error_helper import error, warning
from fake_useragent import UserAgent
from requests import Response, Session

from ..config import get_config, get_pre_creation_hooks
from ..exceptions import SupplierError, SupplierLoadError
from ..retries import setup_session


@dataclass
class ApiPart:
    description: str
    image_url: str | None
    datasheet_url: str | None
    supplier_link: str
    SKU: str
    manufacturer: str
    manufacturer_link: str
    MPN: str
    quantity_available: int | float | Literal[True]
    packaging: str
    category_path: list[str]
    parameters: dict[str, str]
    price_breaks: dict[int | float, float]
    currency: str

    def __post_init__(self):
        self._fix_urls()

    def finalize(self):
        try:
            self.finalize_hook()
        except SupplierError as e:
            error(e)
            return False

        self._fix_urls()
        for pre_creation_hook in get_pre_creation_hooks():
            pre_creation_hook(self)

        return True

    def finalize_hook(self):
        pass

    def get_part_data(self) -> dict[str, Any]:
        return {
            "name": self.MPN,
            "description": self.description[:250],
            "link": self.manufacturer_link[:200],
            "active": True,
            "component": True,
            "purchaseable": True,
        }

    def get_manufacturer_part_data(self) -> dict[str, Any]:
        return {
            "MPN": self.MPN,
            "description": self.description[:250],
            "link": self.manufacturer_link[:200],
        }

    def get_supplier_part_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "description": self.description[:250],
            "link": self.supplier_link[:200],
            "packaging": self.packaging[:50],
        }
        if self.quantity_available:
            data["available"] = min(float(self.quantity_available), 9999999.0)
        return data

    def _fix_urls(self):
        if self.image_url and self.image_url.startswith("//"):
            self.image_url = f"https:{self.image_url}"
        if self.datasheet_url and self.datasheet_url.startswith("//"):
            self.datasheet_url = f"https:{self.datasheet_url}"
        if self.supplier_link and self.supplier_link.startswith("//"):
            self.supplier_link = f"https:{self.supplier_link}"
        if self.manufacturer_link and self.manufacturer_link.startswith("//"):
            self.manufacturer_link = f"https:{self.manufacturer_link}"


class SupplierSupportLevel(IntEnum):
    OFFICIAL_API = 0
    INOFFICIAL_API = 1
    SCRAPING = 2


class Supplier:
    SUPPORT_LEVEL: SupplierSupportLevel

    def setup(self, **kwargs: Any):
        pass

    def get_setup_params(self):
        return {
            name: None if parameter.default is parameter.empty else parameter.default
            for name, parameter in inspect.signature(self.setup).parameters.items()
            if name not in {"self", "kwargs"}
        }

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        raise NotImplementedError()

    def cached_search(self, search_term: str) -> tuple[list[ApiPart], int]:
        if not hasattr(self, "_cache"):
            self._cache: dict[str, tuple[list[ApiPart], int]] = {}
        elif result := self._cache.get(search_term):
            return result
        self._cache[search_term] = (result := self.search(search_term))
        return result

    @property
    def name(self):
        return self.__class__.__name__

    def load_error(self, message: str):
        raise SupplierLoadError(self.__class__.__name__, message)

    def error(self, message: str):
        raise SupplierError(self.__class__.__name__, message)


class ScrapeSupplier(Supplier):
    session: Session
    cookies = CookieJar()

    extra_headers: dict[str, str] = {}
    fallback_domains = [None]

    def scrape(self, url: str) -> Response | None:
        if not hasattr(self, "session"):
            self._setup_session()

        result = self.session.get(url, headers=self.extra_headers, timeout=self.request_timeout)
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
            result = self.session.get(fallback_url, headers=self.extra_headers)
            if result.status_code == 200:
                return result

    def cookies_from_browser(self, name: str, domain_name: str):
        all_browsers = cast(list[Callable[[Any], None]], browser_cookie3.all_browsers)
        if not (browser := getattr(browser_cookie3, name, None)) or browser not in all_browsers:
            warning(
                f"failed to load cookies from browser '{name}' (not in "
                f"[{', '.join(browser.__name__ for browser in all_browsers)}])"
            )
            return

        if not (cookies := browser(domain_name=domain_name)):
            warning(f"browser '{name}' has no cookies set for '{domain_name}'")
            return

        self.cookies = cookies

    def setup_hook(self):
        pass

    def _setup_session(self):
        self.session = setup_session()
        self.session.cookies.update(self.cookies)  # pyright: ignore[reportUnknownMemberType]
        # using iOS User-Agents seems to help to with mouser crawling
        self.session.headers.update(
            {"User-Agent": UserAgent(os=["iOS"]).random, "Accept-Language": "en-US,en"}
        )

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


def money2float(money: str):
    money = MONEY2FLOAT_CLEANUP.sub("", money).strip()
    if split_match := MONEY2FLOAT_SPLIT.match(money):
        decimal, fraction = split_match.groups()
    else:
        decimal = money
        fraction = "0"
    decimal = MONEY2FLOAT_CLEANUP2.sub("", decimal).strip()
    fraction = MONEY2FLOAT_CLEANUP2.sub("", fraction).strip()
    return float(f"{decimal}.{fraction}")


MONEY2FLOAT_CLEANUP = re.compile(r"[^(\d,.\-)]")
MONEY2FLOAT_SPLIT = re.compile(r"(.*)(?:\.|,)(\d+)")
MONEY2FLOAT_CLEANUP2 = re.compile(r"[^\d\-]")
