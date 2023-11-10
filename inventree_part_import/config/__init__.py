from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from inspect import isfunction
from pathlib import Path
import re
import shutil
import sys
from typing import TYPE_CHECKING

from cutie import prompt_yes_or_no, secure_input, select_multiple
from isocodes import countries, currencies, languages
from platformdirs import user_config_path
from requests.exceptions import HTTPError, Timeout
import yaml
from yaml.error import MarkedYAMLError

if TYPE_CHECKING:
    from ..suppliers.base import Supplier

from .. import __package__ as parent_package
from ..error_helper import *
from ..retries import RetryInvenTreeAPI

PARENT_DIR = Path(__file__).parent

_CONFIG_DIR = None
def get_config_dir():
    return _CONFIG_DIR

def set_config_dir(new_config_dir: Path):
    global _CONFIG_DIR
    new_config_dir = Path(new_config_dir).resolve()
    new_config_dir.mkdir(parents=True, exist_ok=True)
    _CONFIG_DIR = new_config_dir
    _setup_gitignore()

def _setup_gitignore():
    # if someone decides to create a git repository in the CONFIG_DIR,
    # stop them from leaking their api keys
    _gitignore = _CONFIG_DIR / ".gitignore"
    if not _gitignore.exists():
        _gitignore.write_text("inventree.yaml\nsuppliers.yaml\n", encoding="utf-8")

# setup default config dir
set_config_dir(user_config_path(parent_package))

INVENTREE_CONFIG = "inventree.yaml"
def setup_inventree_api():
    api_timeout = get_config()["request_timeout"]

    inventree_config = _CONFIG_DIR / INVENTREE_CONFIG
    info("setting up InvenTree API ...")
    if inventree_config.is_file():
        info(f"loading api configuration from '{INVENTREE_CONFIG}' ...")
        try:
            config = yaml.safe_load(inventree_config.read_text(encoding="utf-8"))
            host = config.get("host")
            return RetryInvenTreeAPI(host=host, token=config.get("token"), timeout=api_timeout)
        except MarkedYAMLError as e:
            error(e, prefix="")
        except (ConnectionError, HTTPError, Timeout) as e:
            error(f"failed to connect to '{host}' with '{e}'")
        print()
        if not prompt_yes_or_no("enter new connection details?", default_is_yes=True):
            return None
    else:
        print()

    inventree_api = None
    while not inventree_api:
        prompt("setup your InvenTree API connection:", end="\n")

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
        except (ConnectionError, HTTPError, Timeout) as e:
            error(f"failed to connect to '{host}' with '{e}'")

    yaml_data = yaml.safe_dump({"host": host, "token": inventree_api.token}, sort_keys=False)
    inventree_config.write_text(yaml_data, encoding="utf-8")
    success(f"wrote API configuration to '{inventree_config}'")

    return inventree_api

INVENTREE_HOST_REGEX = re.compile(
    r"^(?P<scheme>[^:/\s]+://)?(?P<hostname>[^:/\s]+)(?::(?P<port>\d{1,5}))?(?P<path>/.*)?$")

DEFAULT_CONFIG_VARS = {
    "max_results": 10,
    "request_timeout": 15.0,
    "retry_timeout": 3.0,
    "interactive": False,
}
VALID_CONFIG_VARS = {"currency", "language", "location", "scraping", *DEFAULT_CONFIG_VARS}

_CONFIG_LOADED = None
CONFIG = "config.yaml"
def get_config():
    global _CONFIG_LOADED
    if _CONFIG_LOADED is not None:
        return _CONFIG_LOADED

    config = _CONFIG_DIR / CONFIG
    if config.is_file():
        try:
            _CONFIG_LOADED = yaml.safe_load(config.read_text(encoding="utf-8"))
            for invalid_parameter in set(_CONFIG_LOADED) - VALID_CONFIG_VARS:
                warning(f"invalid parameter '{invalid_parameter}' in '{CONFIG}'")
                del _CONFIG_LOADED[invalid_parameter]
            _CONFIG_LOADED = {**DEFAULT_CONFIG_VARS, **_CONFIG_LOADED}
            return _CONFIG_LOADED
        except MarkedYAMLError as e:
            error(e, prefix="")
            sys.exit(1)

    info(f"failed to find {CONFIG} config file", end="\n")
    new_configuration_hint()

    prompt("\nsetup your default configuration:", end="\n")
    currency = input_currency()
    language = input_language()
    location = input_location()
    prompt("do you want to enable web scraping? (this is required by some suppliers)", end="\n")
    warning("enabling scraping can get you temporarily blocked sometimes")
    scraping = prompt_yes_or_no("enable scraping?", default_is_yes=True)

    _CONFIG_LOADED = {
        "currency": currency,
        "language": language,
        "location": location,
        "scraping": scraping,
        **DEFAULT_CONFIG_VARS,
    }
    with config.open("w", encoding="utf-8") as file:
        yaml.safe_dump(_CONFIG_LOADED, file, sort_keys=False)
    success("setup default configuration!")
    return _CONFIG_LOADED

CATEGORIES_CONFIG = "categories.yaml"
def get_categories_config():
    return _get_config_file(_CONFIG_DIR / CATEGORIES_CONFIG)

PARAMETERS_CONFIG = "parameters.yaml"
def get_parameters_config():
    return _get_config_file(_CONFIG_DIR / PARAMETERS_CONFIG)

def _get_config_file(config_path: Path):
    if not config_path.is_file():
        info(f"failed to find {config_path.name} config file", end="\n")
        new_configuration_hint()
        if prompt_yes_or_no("copy the default configuration file?", default_is_yes=True):
            shutil.copy(PARENT_DIR / f"default_{config_path.name}", config_path)
        else:
            return None

    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except MarkedYAMLError as e:
        error(e, prefix="")
        return None

@contextmanager
def update_config_file(config_path: Path):
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    try:
        yield config
    finally:
        backup_path = config_path.with_suffix(config_path.suffix + "_bak")
        shutil.copy(config_path, backup_path)
        yaml_data = yaml.safe_dump(config, sort_keys=False)
        config_path.write_text(yaml_data, encoding="utf-8")
        backup_path.unlink()

SUPPLIERS_CONFIG = "suppliers.yaml"
def load_suppliers_config(suppliers: dict[str, Supplier], setup=True):
    suppliers_config = _CONFIG_DIR / SUPPLIERS_CONFIG
    if suppliers_config.is_file():
        suppliers_out = {}
        try:
            with update_config_file(suppliers_config) as suppliers_config_data:
                for id, supplier_config in suppliers_config_data.items():
                    if supplier_config is None:
                        continue
                    if not (supplier := suppliers.get(id)):
                        warning(f"skipping unknown supplier '{id}' in '{SUPPLIERS_CONFIG}'")
                        continue
                    new_supplier_config = update_supplier_config(supplier, supplier_config)
                    if new_supplier_config is not None:
                        suppliers_config_data[id] = new_supplier_config
                        suppliers_out[id] = supplier
        except MarkedYAMLError as e:
            error(e, prefix="")
            sys.exit(1)

        return suppliers_out

    if not setup:
        return {}

    info(suppliers_config)
    info(f"failed to find {SUPPLIERS_CONFIG} config file", end="\n")
    new_configuration_hint()

    suppliers_config_data = {}
    prompt(
        "\nselect the suppliers you want to setup: (SPACEBAR to toggle, ENTER to confirm)",
        end="\n",
    )
    selection = select_multiple(
        [supplier.name for supplier in suppliers.values()],
        ticked_indices=list(range(len(suppliers))),
        deselected_unticked_prefix="  [ ] ",
        deselected_ticked_prefix="  [x] ",
        selected_unticked_prefix="> [ ] ",
        selected_ticked_prefix="> [x] ",
    )

    suppliers_out = {}
    supplier_ids = list(suppliers.keys())
    for id in (supplier_ids[index] for index in selection):
        new_supplier_config = update_supplier_config(suppliers[id], {})
        if new_supplier_config is not None:
            suppliers_config_data[id] = new_supplier_config
            suppliers_out[id] = suppliers[id]

    yaml_data = yaml.safe_dump(suppliers_config_data, indent=4, sort_keys=False)
    suppliers_config.write_text(yaml_data, encoding="utf-8")

    return suppliers_out

def update_supplier_config(supplier: Supplier, supplier_config: dict, force_update=False):
    global_config = get_config()
    used_global_settings = {}

    new_supplier_config = {}
    for name, param_default in supplier._get_setup_params().items():
        if (value := global_config.get(name)) is not None:
            used_global_settings[name] = value
        else:
            new_supplier_config[name] = supplier_config.get(name, param_default)

    if force_update or None in new_supplier_config.values():
        if new_supplier_config:
            prompt(f"\nsetup {supplier.name} configuration:", end="\n")
            for name, default in new_supplier_config.items():
                new_supplier_config[name] = input_default(name, default)
        success(f"setup {supplier.name} configuration!")

    if not supplier.setup(**new_supplier_config, **used_global_settings):
        return None

    return {**supplier_config, **new_supplier_config}

_PRE_CREATION_HOOKS = None
HOOKS_CONFIG = "hooks.py"
def get_pre_creation_hooks():
    global _PRE_CREATION_HOOKS
    if _PRE_CREATION_HOOKS is not None:
        return _PRE_CREATION_HOOKS
    _PRE_CREATION_HOOKS = []

    hooks_config = _CONFIG_DIR / HOOKS_CONFIG
    if not hooks_config.is_file():
        return _PRE_CREATION_HOOKS

    info("loading pre creation hooks ...")
    try:
        hooks_spec = importlib.util.spec_from_file_location(hooks_config.stem, hooks_config)
        hooks_module = importlib.util.module_from_spec(hooks_spec)
        sys.modules[hooks_config.stem] = hooks_module
        hooks_spec.loader.exec_module(hooks_module)
    except ImportError as e:
        error(f"failed to load '{HOOKS_CONFIG}' with {e}")
        return _PRE_CREATION_HOOKS

    _PRE_CREATION_HOOKS = [hook for hook in vars(hooks_module).values() if isfunction(hook)]
    success(f"loaded {len(_PRE_CREATION_HOOKS)} pre creation hooks!")
    return _PRE_CREATION_HOOKS

def input_currency(prompt="currency"):
    while True:
        currency = prompt_input(prompt).upper()
        if currencies.get(alpha_3=currency):
            return currency
        error(f"'{currency}' is not a valid ISO 4217 currency code")

def input_language(prompt="language"):
    while True:
        language = prompt_input(prompt).lower()
        if languages.get(alpha_2=language) or languages.get(alpha_3=language):
            return language.upper()
        error(f"'{language}' is not a valid ISO 639-2 language code")

def input_location(prompt="location"):
    while True:
        location = prompt_input(prompt).upper()
        if countries.get(alpha_2=location) or countries.get(alpha_3=location):
            return location
        error(f"'{location}' is not a valid ISO 3166 country code")

def input_default(prompt, default_value=None):
    suffix = "" if default_value is None else f" [{default_value}]"
    while True:
        value = prompt_input(f"{prompt}{suffix}")
        if value or default_value:
            return value or default_value

_NEW_CONFIGURATION_HINT = True
def new_configuration_hint():
    global _NEW_CONFIGURATION_HINT
    if _NEW_CONFIGURATION_HINT:
        hint("this is normal if you're using this program for the first time")
        _NEW_CONFIGURATION_HINT = False
