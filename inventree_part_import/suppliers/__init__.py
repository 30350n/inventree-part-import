import importlib
from error_helper import error, hint
from inspect import isclass
from multiprocessing.pool import ThreadPool
from pathlib import Path

from ..config import SUPPLIERS_CONFIG, get_config, load_suppliers_config, update_config_file
from ..inventree_helpers import Company
from .base import ScrapeSupplier, Supplier

_SUPPLIERS = None
def search(search_term, supplier_id: str = None, only_supplier=False):
    global _SUPPLIERS
    if _SUPPLIERS is None:
        assert _SUPPLIER_COMPANIES is not None, "call setup_supplier_companies(...) first"
        supplier_objects, _ = get_suppliers()
        assert supplier_objects.keys() == _SUPPLIER_COMPANIES.keys()
        _SUPPLIERS = dict(zip(
            supplier_objects.keys(),
            zip(supplier_objects.values(), _SUPPLIER_COMPANIES.values())
        ))

    suppliers = list(_SUPPLIERS.values())
    if supplier_id:
        if supplier := _SUPPLIERS.get(supplier_id):
            if only_supplier:
                suppliers = [supplier]
            else:
                suppliers.remove(supplier)
                suppliers.insert(0, supplier)
        else:
            error(f"supplier id '{supplier_id}' not defined in {SUPPLIERS_CONFIG}")
            return None

    thread_pool = ThreadPool(processes=8)
    return (
        (api_company, thread_pool.apply_async(supplier_object.cached_search, (search_term,)))
        for supplier_object, api_company in suppliers
    )

_SUPPLIER_COMPANIES = None
def setup_supplier_companies(inventree_api):
    global _SUPPLIER_COMPANIES
    _SUPPLIER_COMPANIES = {}
    global_config = get_config()
    with update_config_file(SUPPLIERS_CONFIG) as suppliers_config:
        for id, supplier_object in _SUPPLIER_OBJECTS.items():
            supplier_config = suppliers_config.get(id)
            if supplier_config is None:
                supplier_config = suppliers_config[id] = {}
            api_company = Company(
                name=supplier_object.name,
                currency=supplier_config.get("currency", global_config["currency"]),
                is_supplier=True,
                primary_key=supplier_config.get("_primary_key"),
            ).setup(inventree_api)
            if not hasattr(inventree_api, "DRY_RUN"):
                supplier_config["_primary_key"] = api_company.pk
            _SUPPLIER_COMPANIES[id] = api_company

_SUPPLIER_OBJECTS = None
_AVAILABLE_SUPPLIER_OBJECTS = None
def get_suppliers(reload=False, setup=True) -> tuple[dict, dict]:
    global _SUPPLIER_OBJECTS, _AVAILABLE_SUPPLIER_OBJECTS
    if not reload and _SUPPLIER_OBJECTS is not None:
        return _SUPPLIER_OBJECTS, _AVAILABLE_SUPPLIER_OBJECTS

    _SUPPLIER_OBJECTS = {}
    _AVAILABLE_SUPPLIER_OBJECTS = {}
    for path in Path(__file__).parent.glob("supplier_*.py"):
        module_name = path.stem
        try:
            if module_name in locals():
                module = importlib.reload(locals()[module_name])
            else:
                module = importlib.import_module(f".{module_name}", package=__package__)
        except ImportError as e:
            error(f"failed to load supplier module '{module_name}' with {e}")
            continue

        supplier_classes = [
            cls for cls in vars(module).values()
            if isclass(cls) and cls not in (Supplier, ScrapeSupplier) and issubclass(cls, Supplier)
        ]
        if len(supplier_classes) != 1:
            suffix = "multiple Supplier classes" if supplier_classes else "no Supplier class"
            error(f"failed to load supplier module '{module_name}' ({suffix} defined)")
            continue

        if supplier_classes[0].SUPPORT_LEVEL is None:
            error(f"failed to load supplier module '{module_name}' (undefined SUPPORT_LEVEL)")
            continue

        id = module_name.split("supplier_", 1)[-1]
        _AVAILABLE_SUPPLIER_OBJECTS[id] = supplier_classes[0]()

    _AVAILABLE_SUPPLIER_OBJECTS = dict(sorted(
        _AVAILABLE_SUPPLIER_OBJECTS.items(),
        key=lambda supplier_item: (supplier_item[1].SUPPORT_LEVEL, supplier_item[1].name),
    ))

    _SUPPLIER_OBJECTS = load_suppliers_config(_AVAILABLE_SUPPLIER_OBJECTS, setup=setup)

    if (available := len(_AVAILABLE_SUPPLIER_OBJECTS)) > (loaded := len(_SUPPLIER_OBJECTS)):
        if setup:
            hint(f"only {loaded} of {available} available supplier modules are configured")

    return _SUPPLIER_OBJECTS, _AVAILABLE_SUPPLIER_OBJECTS
