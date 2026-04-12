import time
from typing import Any, Callable, TypeVar, overload

from inventree.api import InvenTreeAPI
from requests import HTTPError, Session
from requests.adapters import HTTPAdapter, PoolManager, Retry


class TLSv1_2HTTPAdapter(HTTPAdapter):
    def init_poolmanager(
        self, connections: int, maxsize: Any, block: bool = False, **pool_kwargs: Any
    ):
        from ssl import PROTOCOL_TLSv1_2

        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_version=PROTOCOL_TLSv1_2,
            **pool_kwargs,
        )


T = TypeVar("T", bound=Session)


@overload
def setup_session(session: T, *, use_tlsv1_2: bool = False) -> T: ...
@overload
def setup_session(session: None = None, *, use_tlsv1_2: bool = False) -> Session: ...
def setup_session(session: Session | None = None, *, use_tlsv1_2: bool = False) -> Session:
    retry = Retry(5, backoff_factor=0.5, backoff_max=5)
    AdapterClass = TLSv1_2HTTPAdapter if use_tlsv1_2 else HTTPAdapter
    http_adapter = AdapterClass(max_retries=retry)

    if session is None:
        session = Session()
    session.mount("http://", http_adapter)
    session.mount("https://", http_adapter)

    return session


class RetryInvenTreeAPI(InvenTreeAPI):
    def testServer(self):
        return self._retry(super().testServer)

    def request(self, url: str, **kwargs: Any):
        return self._retry(super().request, url, **kwargs)  # pyright: ignore[reportUnknownArgumentType]

    def downloadFile(
        self,
        url: str,
        destination: str,
        overwrite: bool = False,
        params: dict[str, Any] | None = None,
        proxies: dict[str, Any] = {},
    ):
        return self._retry(super().downloadFile, url, destination, overwrite, params, proxies)  # pyright: ignore[reportUnknownArgumentType]

    R = TypeVar("R")

    @staticmethod
    def _retry(func: Callable[..., R], *args: Any, **kwargs: Any) -> R:
        for _ in range(4):
            try:
                return func(*args, **kwargs)
            except ConnectionError:
                pass
            except HTTPError as e:
                status_code = None
                if e.response is not None:
                    status_code = e.response.status_code
                elif e.args:
                    status_code = e.args[0].get("status_code")
                if status_code not in {408, 409, 500, 502, 503, 504}:
                    raise e

            time.sleep(5)

        return func(*args, **kwargs)
