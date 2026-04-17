from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from contextlib import contextmanager
from inspect import isfunction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Generator, Literal, cast, overload

import yaml
from cutie import prompt_yes_or_no, secure_input, select, select_multiple
from error_helper import error, hint, info, prompt, prompt_input, success, warning
from inventree.api import InvenTreeAPI
from platformdirs import user_config_path
from requests.exceptions import HTTPError, Timeout
from yaml.error import MarkedYAMLError

if TYPE_CHECKING:
    from ..suppliers.base import ApiPart, Supplier

from .. import __package__ as parent_package
from ..exceptions import SupplierLoadError
from ..localization import currencies, get_country, get_language
from ..retries import RetryInvenTreeAPI

PARENT_DIR = Path(__file__).parent

_config_dir = None


def get_config_dir():
    return _config_dir


def set_config_dir(new_config_dir: Path):
    global _config_dir
    new_config_dir = Path(new_config_dir).resolve()
    new_config_dir.mkdir(parents=True, exist_ok=True)
    _config_dir = new_config_dir
    _setup_gitignore()


def _setup_gitignore():
    # if someone decides to create a git repository in the CONFIG_DIR,
    # stop them from leaking their api keys
    assert _config_dir is not None  # TODO
    _gitignore = _config_dir / ".gitignore"
    if not _gitignore.exists():
        _gitignore.write_text("inventree.yaml\nsuppliers.yaml\n", encoding="utf-8")


# setup default config dir
set_config_dir(user_config_path(parent_package))

INVENTREE_CONFIG = "inventree.yaml"


def setup_inventree_api():
    api_timeout = get_config()["request_timeout"]

    assert _config_dir is not None  # TODO
    inventree_config = _config_dir / INVENTREE_CONFIG
    info("setting up InvenTree API ...")
    if inventree_config.is_file():
        info(f"loading api configuration from '{INVENTREE_CONFIG}' ...")
        try:
            config = yaml.safe_load(inventree_config.read_text(encoding="utf-8"))
            host = config.get("host")
            try:
                return RetryInvenTreeAPI(host=host, token=config.get("token"), timeout=api_timeout)
            except (ConnectionError, HTTPError, Timeout) as e:
                error(f"failed to connect to '{host}' with '{e}'")
        except MarkedYAMLError as e:
            error(e, prefix="")
        print()
        if not prompt_yes_or_no("enter new connection details?", default_is_yes=True):
            return None
    else:
        print()

    while True:
        prompt("setup your InvenTree API connection", prefix="")

        host = prompt_input("host")
        if not (match := INVENTREE_HOST_REGEX.fullmatch(host)):
            error(f"invalid hostname '{host}'")
            continue
        if not match.group("scheme"):
            scheme = "http" if match.group("hostname") == "localhost" else "https"
            warning(f"hostname is missing scheme, assuming '{scheme}'")
            host = f"{scheme}://{host}"

        username = prompt_input("username")
        password = secure_input("password:").strip()

        try:
            inventree_api = RetryInvenTreeAPI(
                host,
                username=username,
                password=password,
                use_token_auth=True,
                timeout=api_timeout,
            )
            break
        except (ConnectionError, HTTPError, Timeout) as e:
            error(f"failed to connect to '{host}' with '{e}'")

    yaml_data = yaml_dump({"host": host, "token": cast(str, inventree_api.token)}, sort_keys=False)
    inventree_config.write_text(yaml_data, encoding="utf-8")
    success(f"wrote API configuration to '{inventree_config}'")

    return inventree_api


INVENTREE_HOST_REGEX = re.compile(
    r"^(?P<scheme>[^:/\s]+://)?(?P<hostname>[^:/\s]+)(?::(?P<port>\d{1,5}))?(?P<path>/.*)?$"
)

DEFAULT_CONFIG_VARS = {
    "interactive": "twice",
    "interactive_part_matches": 10,
    "request_timeout": 15.0,
    "retry_timeout": 3.0,
}
VALID_CONFIG_VARS = {
    "currency",
    "language",
    "location",
    "scraping",
    "datasheets",
    "interactive_category_matches",
    "interactive_parameter_matches",
    "part_selection_format",
    "auto_detect_columns",
    *DEFAULT_CONFIG_VARS,
}
RENAMED_CONFIG_VARS = {
    "max_results": "interactive_part_matches",
}

_config_loaded = None
CONFIG = "config.yaml"


@overload
def get_config(reload: Literal[False] = False) -> dict[str, Any]: ...
@overload
def get_config(reload: Literal[True]) -> None: ...
def get_config(reload: bool = False):
    global _config_loaded
    if not reload and _config_loaded is not None:
        return _config_loaded

    assert _config_dir is not None  # TODO
    config = _config_dir / CONFIG
    if config.is_file():
        try:
            _config_loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
            for invalid_parameter in set(_config_loaded) - VALID_CONFIG_VARS:
                if renamed_parameter := RENAMED_CONFIG_VARS.get(invalid_parameter):
                    warning(
                        f"deprecated parameter '{invalid_parameter}' in {CONFIG} "
                        f"(renamed to '{renamed_parameter}')"
                    )
                    _config_loaded[renamed_parameter] = _config_loaded.pop(invalid_parameter)
                else:
                    warning(f"invalid parameter '{invalid_parameter}' in '{CONFIG}'")
                    del _config_loaded[invalid_parameter]
            _config_loaded = {**DEFAULT_CONFIG_VARS, **_config_loaded}
            return _config_loaded
        except MarkedYAMLError as e:
            error(e, prefix="")
            sys.exit(1)

    if reload:
        _config_loaded = None
        return _config_loaded

    info(f"failed to find {CONFIG} config file", end="\n")
    new_configuration_hint()

    prompt("setup your default configuration")
    currency = input_currency()
    language = input_language()
    location = input_location()

    prompt(
        "do you want to enable web scraping? (this is required to use some suppliers)",
        prefix="",
        end="\n",
    )
    warning("enabling scraping can get you temporarily blocked sometimes")
    scraping = prompt_yes_or_no("enable scraping?", default_is_yes=True)

    prompt("how do you want to handle datasheets?")
    datasheets_choices = [
        "upload (upload file attachments for parts)",
        "link   (add external link attachments to parts)",
        "false  (do not add datasheets for parts)",
    ]
    datasheets_values = ["upload", "link", False]
    datasheets_index = select(datasheets_choices, deselected_prefix="  ", selected_prefix="> ")

    _config_loaded = {
        "currency": currency,
        "language": language,
        "location": location,
        "scraping": scraping,
        "datasheets": datasheets_values[datasheets_index],
        **DEFAULT_CONFIG_VARS,
    }
    yaml_data = yaml_dump(_config_loaded, sort_keys=False)
    config.write_text(yaml_data, encoding="utf-8")

    success("setup default configuration!")
    return _config_loaded


CATEGORIES_CONFIG = "categories.yaml"


def get_categories_config(inventree_api: InvenTreeAPI):
    assert _config_dir is not None  # TODO
    categories_config = _config_dir / CATEGORIES_CONFIG
    if not categories_config.is_file():
        setup_default_configuration_files(inventree_api)

    try:
        return yaml.safe_load(categories_config.read_text(encoding="utf-8"))
    except MarkedYAMLError as e:
        error(e, prefix="")
        return None


PARAMETERS_CONFIG = "parameters.yaml"


def get_parameters_config(inventree_api: InvenTreeAPI):
    assert _config_dir is not None  # TODO
    parameters_config = _config_dir / PARAMETERS_CONFIG
    if not parameters_config.is_file():
        setup_default_configuration_files(inventree_api)

    try:
        return yaml.safe_load(parameters_config.read_text(encoding="utf-8"))
    except MarkedYAMLError as e:
        error(e, prefix="")
        return None


def setup_default_configuration_files(inventree_api: InvenTreeAPI):
    prompt("setup default categories/parameters configuration")
    choices = [
        "Copy categories from InvenTree",
        "Copy default categories configuration",
        "Create empty configuration (manual setup)",
    ]
    choice_index = select(choices, deselected_prefix="  ", selected_prefix="> ")

    categories = None
    parameters = None
    if choice_index == 0:
        from ..categories import setup_config_from_inventree

        categories, parameters = setup_config_from_inventree(inventree_api)

    assert _config_dir is not None  # TODO
    categories_config = _config_dir / CATEGORIES_CONFIG
    if not categories_config.is_file():
        match choice_index:
            case 0:
                assert categories
                categories_config.write_text(yaml_dump(categories), encoding="utf-8")
            case 1:
                shutil.copy(PARENT_DIR / f"default_{CATEGORIES_CONFIG}", categories_config)
            case 2:
                categories_config.touch()
            case _:
                assert False

    parameters_config = _config_dir / PARAMETERS_CONFIG
    if not parameters_config.is_file():
        match choice_index:
            case 0:
                assert parameters
                parameters_config.write_text(yaml_dump(parameters), encoding="utf-8")
            case 1:
                shutil.copy(PARENT_DIR / f"default_{PARAMETERS_CONFIG}", parameters_config)
            case 2:
                parameters_config.touch()
            case _:
                assert False


@contextmanager
def update_config_file(file_name: str) -> Generator[dict[str, Any]]:
    assert _config_dir
    config_path = _config_dir / file_name
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    try:
        yield config
    finally:
        backup_path = config_path.with_suffix(config_path.suffix + "_bak")
        shutil.copy(config_path, backup_path)
        yaml_data = yaml_dump(config, sort_keys=False)
        config_path.write_text(yaml_data, encoding="utf-8")
        backup_path.unlink()


SUPPLIERS_CONFIG = "suppliers.yaml"


def load_suppliers_config(suppliers: dict[str, Supplier], setup: bool = True):
    suppliers_out: dict[str, Supplier] = {}

    assert _config_dir is not None  # TODO
    suppliers_config = _config_dir / SUPPLIERS_CONFIG
    if suppliers_config.is_file():
        try:
            with update_config_file(SUPPLIERS_CONFIG) as suppliers_config_data:
                previous_supplier = None
                supplier_config: dict[str, Any] | None
                for id, supplier_config in suppliers_config_data.items():
                    if supplier_config is None:
                        supplier_config = {}
                    if not (supplier := suppliers.get(id)):
                        if setup:
                            warning(f"skipping unknown supplier '{id}' in '{SUPPLIERS_CONFIG}'")
                        continue
                    if setup and previous_supplier:
                        if previous_supplier[1].SUPPORT_LEVEL > supplier.SUPPORT_LEVEL:
                            warning(
                                f"supplier '{previous_supplier[0]}' (support level "
                                f"{previous_supplier[1].SUPPORT_LEVEL.name}) is used ahead of "
                                f"supplier '{id}' (support level {supplier.SUPPORT_LEVEL.name})"
                            )
                            hint(f"you might want to reorder them in '{SUPPLIERS_CONFIG}'")
                    previous_supplier = (id, supplier)
                    suppliers_config_data[id] = update_supplier_config(supplier, supplier_config)
                    suppliers_out[id] = supplier

        except MarkedYAMLError as e:
            error(e, prefix="")
            sys.exit(1)
        except SupplierLoadError as e:
            error(e)
            sys.exit(1)

        return suppliers_out

    if not setup:
        return suppliers_out

    info(f"failed to find {SUPPLIERS_CONFIG} config file", end="\n")
    new_configuration_hint()

    new_suppliers_config_data: dict[str, dict[str, Any]] = {}
    if suppliers:
        prompt("select the suppliers you want to setup (SPACEBAR to toggle, ENTER to confirm)")
        selection = select_multiple(
            [supplier.name for supplier in suppliers.values()],
            ticked_indices=list(range(len(suppliers))),
            deselected_unticked_prefix="  [ ] ",
            deselected_ticked_prefix="  [x] ",
            selected_unticked_prefix="> [ ] ",
            selected_ticked_prefix="> [x] ",
        )

        supplier_ids = list(suppliers.keys())
        for id in (supplier_ids[index] for index in selection):
            try:
                new_suppliers_config_data[id] = update_supplier_config(suppliers[id], {})
                suppliers_out[id] = suppliers[id]
            except SupplierLoadError as e:
                error(e)

    yaml_data = yaml_dump(new_suppliers_config_data, sort_keys=False)
    suppliers_config.write_text(yaml_data, encoding="utf-8")

    return suppliers_out


def update_supplier_config(
    supplier: Supplier, supplier_config: dict[str, Any], force_update: bool = False
):
    global_config = get_config()
    used_global_settings: dict[str, Any] = {}

    new_supplier_config: dict[str, Any] = {}
    for name, param_default in supplier.get_setup_params().items():
        if (value := supplier_config.get(name, param_default)) is not None:
            new_supplier_config[name] = value
        elif (value := global_config.get(name)) is not None:
            used_global_settings[name] = value
        else:
            new_supplier_config[name] = None

    if force_update or None in new_supplier_config.values():
        if new_supplier_config:
            prompt(f"setup {supplier.name} configuration")
            for name, default in new_supplier_config.items():
                new_supplier_config[name] = input_default(name, default)
        success(f"setup {supplier.name} configuration!")

    supplier.setup(**new_supplier_config, **used_global_settings)

    return {**supplier_config, **new_supplier_config}


_pre_creation_hooks: list[Callable[[ApiPart], None]] | None = None
HOOKS_CONFIG = "hooks.py"


def get_pre_creation_hooks():
    global _pre_creation_hooks
    if _pre_creation_hooks is not None:
        return _pre_creation_hooks
    _pre_creation_hooks = []

    assert _config_dir is not None  # TODO
    hooks_config = _config_dir / HOOKS_CONFIG
    if not hooks_config.is_file():
        return _pre_creation_hooks

    info("loading pre creation hooks ...")
    try:
        hooks_spec = importlib.util.spec_from_file_location(hooks_config.stem, hooks_config)
        assert hooks_spec and hooks_spec.loader
        hooks_module = importlib.util.module_from_spec(hooks_spec)
        sys.modules[hooks_config.stem] = hooks_module
        hooks_spec.loader.exec_module(hooks_module)
    except ImportError as e:
        error(f"failed to load '{HOOKS_CONFIG}' with {e}")
        return _pre_creation_hooks

    _pre_creation_hooks = [hook for hook in vars(hooks_module).values() if isfunction(hook)]
    success(f"loaded {len(_pre_creation_hooks)} pre creation hooks!")
    return _pre_creation_hooks


def input_currency(prompt: str = "currency"):
    while True:
        currency = prompt_input(prompt).upper()
        if currencies.get(alpha_3=currency):
            return currency
        error(f"'{currency}' is not a valid ISO 4217 currency code")


def input_language(prompt: str = "language"):
    while True:
        language = prompt_input(prompt).lower()
        if get_language(language):
            return language
        error(f"'{language}' is not a valid ISO 639-2 language code")


def input_location(prompt: str = "location"):
    while True:
        location = prompt_input(prompt).upper()
        if get_country(location):
            return location
        error(f"'{location}' is not a valid ISO 3166 country code")


def input_default(prompt: str, default_value: str | None = None):
    suffix = "" if default_value is None else f" [{default_value}]"
    while True:
        value = prompt_input(f"{prompt}{suffix}")
        if value or default_value:
            return value or default_value


_new_configuration_hint = True


def new_configuration_hint():
    global _new_configuration_hint
    if _new_configuration_hint:
        hint("this is normal if you're using this program for the first time")
        _new_configuration_hint = False


def yaml_dump(data: dict[str, Any], sort_keys: bool = True):
    yaml_data = yaml.safe_dump(data, indent=4, sort_keys=sort_keys, allow_unicode=True)
    yaml_data = YAML_REMOVE_NULL_REGEX.sub("", yaml_data)
    yaml_data = YAML_FIX_LIST_INDENTATION_REGEX.sub(YAML_FIX_LIST_INDENTATION_SUB, yaml_data)
    return yaml_data


YAML_REMOVE_NULL_REGEX = re.compile(r" (?:\{\}|null)", re.MULTILINE)
YAML_FIX_LIST_INDENTATION_REGEX = re.compile(r"^(\s*)(- )", re.MULTILINE)
YAML_FIX_LIST_INDENTATION_SUB = r"\g<1>    \g<2>"
