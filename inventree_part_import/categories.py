import sys
from dataclasses import asdict, dataclass
from typing import Any, cast

from cutie import prompt_yes_or_no
from error_helper import hint, info, success, warning
from inventree.api import InvenTreeAPI
from inventree.base import ParameterTemplate
from inventree.part import PartCategory, PartCategoryParameterTemplate

from .config import (
    CATEGORIES_CONFIG,
    PARAMETERS_CONFIG,
    get_categories_config,
    get_parameters_config,
    update_config_file,
)
from .exceptions import InvenTreeObjectCreationError


def setup_categories_and_parameters(inventree_api: InvenTreeAPI):
    categories_config = get_categories_config(inventree_api)
    parameters_config = get_parameters_config(inventree_api)

    info("setting up categories ...")
    category_stubs = parse_categories(inventree_api, categories_config)
    parameters = parse_parameters(parameters_config)

    used_parameters = {param for stub in category_stubs.values() for param in stub.parameters}

    for name in parameters:
        if name not in used_parameters:
            warning(f"parameter '{name}' is defined in {PARAMETERS_CONFIG} but not being used")
    for name in used_parameters:
        if name not in parameters:
            warning(f"parameter '{name}' not defined in {PARAMETERS_CONFIG}")
            parameters[name] = Parameter(name, name, [], "")

    part_categories_by_pk = {
        part_category.pk: part_category for part_category in PartCategory.list(inventree_api)
    }
    part_categories: dict[tuple[str, ...], PartCategory] = {}
    for part_category in part_categories_by_pk.values():
        path = [part_category.name]
        parent_category = part_category
        while parent_category := part_categories_by_pk.get(parent_category.parent):
            path.insert(0, parent_category.name)
        part_categories[tuple(path)] = part_category

    categories: dict[tuple[str, ...], Category] = {}
    for category_path, category_stub in category_stubs.items():
        part_category = part_categories.get(tuple(category_stub.path))
        if part_category is None:
            info(f"creating category '{'/'.join(category_stub.path)}' ...")
            parent = part_categories.get(tuple(category_stub.path[:-1]))
            part_category_data = {
                "name": category_stub.name,
                "description": category_stub.description,
                "structural": category_stub.structural,
                "parent": parent.pk if parent else None,
            }
            if not (part_category := PartCategory.create(inventree_api, part_category_data)):
                raise InvenTreeObjectCreationError(PartCategory)

            part_categories[tuple(category_stub.path)] = part_category

        elif category_stub.description != part_category.description:
            info(f"updating description for category '{'/'.join(category_stub.path)}' ...")
            part_category.save({"description": category_stub.description})

        if category_stub.structural and not part_category.structural:
            path_str = part_category.pathstring
            warning(f"category '{path_str}' on host is not structural, but it should be")
        elif not category_stub.structural and part_category.structural:
            path_str = part_category.pathstring
            warning(f"category '{path_str}' on host is structural, but it shouldn't be")

        categories[category_path] = Category.from_stub(category_stub, part_category)

    for category_path, part_category in part_categories.items():
        if category_path in categories:
            continue
        for i in range(1, len(category_path)):
            if (parent := categories.get(category_path[:-i])) and parent.ignore:
                break
        else:
            path_str = part_category.pathstring
            warning(f"category '{path_str}' on host is not defined in {CATEGORIES_CONFIG}")

    parameter_templates: dict[str, ParameterTemplate] = {
        parameter_template.name: parameter_template
        for parameter_template in ParameterTemplate.list(inventree_api)
    }

    for parameter in parameters.values():
        description, units = parameter.description, parameter.units

        if not (parameter_template := parameter_templates.get(parameter.name)):
            info(f"creating parameter template '{parameter.name}' ...")
            template_data = {
                "name": parameter.name,
                "description": description,
                "units": units,
            }
            if not (parameter_template := ParameterTemplate.create(inventree_api, template_data)):
                raise InvenTreeObjectCreationError(ParameterTemplate)
            parameter_templates[parameter.name] = parameter_template

        elif description != parameter_template.description or units != parameter_template.units:
            info(f"updating parameter template '{parameter.name}' ...")
            parameter_template.save(
                {
                    "description": parameter.description,
                    "units": parameter.units,
                }
            )

    part_category_pk_to_category = {
        category.part_category.pk: category for category in categories.values()
    }

    # https://github.com/inventree/InvenTree/pull/10699
    if inventree_api.api_version >= 429:
        migrate_parameter_templates(inventree_api, part_category_pk_to_category)

    category_parameters = {
        (category, param) for category in categories.values() for param in category.parameters
    }
    part_category_parameter_templates = {
        (category, template.template_detail["name"])
        for template in PartCategoryParameterTemplate.list(inventree_api)
        if (category := part_category_pk_to_category.get(template.category))
    }

    for category_parameter in category_parameters:
        if category_parameter not in part_category_parameter_templates:
            category, parameter = category_parameter
            category_str = "/".join(category.path)
            info(f"creating parameter template '{parameter}' for '{category_str}' ...")
            assert category.part_category
            parameter_template_data = {
                "category": category.part_category.pk,
                "template": parameter_templates[parameter].pk,
            }
            if not PartCategoryParameterTemplate.create(inventree_api, parameter_template_data):
                raise InvenTreeObjectCreationError(PartCategoryParameterTemplate)

    for category, template_name in part_category_parameter_templates:
        if (category, template_name) not in category_parameters and not category.ignore:
            warning(
                f"parameter template '{template_name}' for '{'/'.join(category.path)}' "
                f"on host is not defined in {CATEGORIES_CONFIG}"
            )

    category_map: dict[str, Category] = {}
    ignore: set[str] = set()
    for category in categories.values():
        if category.structural or category.ignore:
            continue
        for alias in category.aliases:
            category_map[alias.lower()] = category
        category_slug = category.name.lower()
        if category_slug not in ignore:
            if category_slug not in category_map:
                category_map[category_slug] = category
            else:
                ignore.add(category_slug)
                category_map.pop(category_slug)

    parameter_map: dict[str, list[Parameter]] = {}
    for parameter in parameters.values():
        for alias in (*parameter.aliases, parameter.name):
            if existing := parameter_map.get(alias.lower()):
                existing.append(parameter)
            else:
                parameter_map[alias.lower()] = [parameter]

    success("setup categories!", end="\n\n")

    return category_map, parameter_map


def migrate_parameter_templates(
    inventree_api: InvenTreeAPI, part_category_pk_to_category: dict[Any, "Category"]
):
    category_templates: dict[Category, dict[Any, PartCategoryParameterTemplate]] = {}
    for template in PartCategoryParameterTemplate.list(inventree_api):
        if category := part_category_pk_to_category.get(template.category):
            category_templates.setdefault(category, {})[template.template] = template

    inherited_category_templates = [
        template
        for category, templates in category_templates.items()
        for base_template_pk, template in templates.items()
        if (parent_category := part_category_pk_to_category.get(category.part_category.parent))
        if base_template_pk in category_templates.get(parent_category, [])
    ]

    if inherited_category_templates:
        warning(
            f"found {len(inherited_category_templates)} invalid PartCategoryParameterTemplates "
            "which will have to be DELETED to continue operation",
        )
        hint(
            "visit https://github.com/30350n/inventree-part-import/pull/92#issuecomment-4268494064 "
            "to learn more"
        )
        result = prompt_yes_or_no(
            f"delete {len(inherited_category_templates)} invalid PartCategoryParameterTemplates?",
            default_is_yes=True,
        )
        if result:
            for template in inherited_category_templates:
                template.delete()
        else:
            sys.exit(0)


@dataclass
class CategoryStub:
    name: str
    path: list[str]
    description: str
    ignore: bool
    structural: bool
    aliases: list[str]
    parameters: list[str]

    def add_alias(self, alias: str):
        self.aliases.append(alias)
        with update_config_file(CATEGORIES_CONFIG) as categories_config:
            try:
                category_config = categories_config
                for sub_category_name in self.path:
                    if category_config[sub_category_name] is None:
                        category_config[sub_category_name] = {}
                    category_config = category_config[sub_category_name]

                if aliases := category_config.get("_aliases"):
                    if alias not in aliases:
                        aliases.append(alias)
                    else:
                        warning(
                            f"failed to add alias '{alias}' for category '{self.name}' "
                            f"(alias is already defined)"
                        )
                else:
                    category_config["_aliases"] = [alias]

            except KeyError:
                warning(
                    f"failed to add alias '{alias}' for category '{self.name}' in "
                    f"'{CATEGORIES_CONFIG}'"
                )


@dataclass
class Category(CategoryStub):
    part_category: PartCategory

    def __hash__(self):
        return hash(tuple(self.path))

    @classmethod
    def from_stub(cls, stub: CategoryStub, part_category: PartCategory):
        return cls(**asdict(stub), part_category=part_category)


CATEGORY_ATTRIBUTES = {
    "_parameters",
    "_omit_parameters",
    "_description",
    "_ignore",
    "_structural",
    "_aliases",
}


def parse_categories(inventree_api: InvenTreeAPI, categories_dict: Any):
    return _parse_category_recursive(inventree_api, categories_dict)


def _parse_category_recursive(
    inventree_api: InvenTreeAPI, categories_dict: Any, parent: CategoryStub | None = None
) -> dict[tuple[str, ...], CategoryStub]:
    if not isinstance(categories_dict, dict):
        return {}
    categories_dict = cast(dict[str, Any], categories_dict)

    categories: dict[tuple[str, ...], CategoryStub] = {}
    name: str
    for name, values in categories_dict.items():
        if name.startswith("_"):
            continue

        if values is None:
            values = {}
        elif not isinstance(values, dict):
            warning(f"failed to parse category '{name}' (invalid type, should be dict or null)")
            continue
        values = cast(dict[str, Any], values)

        for child in values.keys():
            if child.startswith("_") and child not in CATEGORY_ATTRIBUTES:
                warning(f"ignoring unknown special attribute '{child}' in category '{name}'")

        omitted_parameters = values.get("_omit_parameters", [])
        parameters: list[str] = []
        # https://github.com/inventree/InvenTree/pull/10699
        if inventree_api.api_version < 429 and parent:
            parameters += list(set(parent.parameters) - set(omitted_parameters))
            for parameter in set(omitted_parameters) - set(parent.parameters):
                warning(f"failed to omit parameter '{parameter}' in category '{name}'")
        elif omitted_parameters:
            warning(
                "_omit_parameters is disfunctional for InvenTree >= 1.2.0 "
                "(parent parameters are inherited automatically)"
            )
        parameters += values.get("_parameters", [])

        category = CategoryStub(
            name=name,
            path=(parent.path if parent else []) + [name],
            description=values.get("_description", name),
            ignore=values.get("_ignore", False),
            structural=values.get("_structural", False),
            aliases=values.get("_aliases", []),
            parameters=parameters,
        )
        categories[tuple(category.path)] = category

        categories.update(_parse_category_recursive(inventree_api, values, category))

    return categories


@dataclass
class Parameter:
    name: str
    description: str
    aliases: list[str]
    units: str

    def add_alias(self, alias: str):
        self.aliases.append(alias)
        with update_config_file(PARAMETERS_CONFIG) as parameters_config:
            if (parameter_config := parameters_config.get(self.name)) is None:
                parameter_config = parameters_config[self.name] = cast(dict[str, Any], {})

            aliases: list[str] | None
            if aliases := parameter_config.get("_aliases"):
                if alias not in aliases:
                    aliases.append(alias)
                else:
                    warning(
                        f"failed to add alias '{alias}' for parameter '{self.name}' "
                        f"(alias is already defined)"
                    )
            else:
                parameter_config["_aliases"] = [alias]


PARAMETER_ATTRIBUTES = {"_description", "_aliases", "_unit"}


def parse_parameters(parameters_dict: Any) -> dict[str, Parameter]:
    if not isinstance(parameters_dict, dict):
        return {}

    parameters: dict[str, Parameter] = {}
    for name, values in cast(dict[str, Any], parameters_dict).items():
        if values is None:
            values = {}
        elif not isinstance(values, dict):
            warning(f"failed to parse parameter '{name}' (invalid type, should be dict or null)")
            continue

        values = cast(dict[str, Any], values)

        for child in values.keys():
            if child.startswith("_") and child not in PARAMETER_ATTRIBUTES:
                warning(f"ignoring unknown special attribute '{child}' in parameter '{name}'")

        parameters[name] = Parameter(
            name=name,
            description=values.get("_description", name),
            aliases=values.get("_aliases", []),
            units=values.get("_unit", ""),
        )

    return parameters


def setup_config_from_inventree(inventree_api: InvenTreeAPI):
    info(f"copying categories and parameters configuration from '{inventree_api.base_url}' ...")
    categories: dict[int, dict[str, Any]] = {
        cast(int, part_category.pk): {
            "name": part_category.name,
            "parent": part_category.parent,
            "_description": part_category.description,
            "_structural": part_category.structural,
            "all_parameters": set(),
            "_parameters": set(),
        }
        for part_category in PartCategory.list(inventree_api)
    }

    parameters: dict[str, dict[str, Any]] = {}
    for template in PartCategoryParameterTemplate.list(inventree_api):
        parameter_name = template.template_detail["name"]
        if parameter_name not in parameters:
            fields: dict[str, Any] = {}
            if units := template.template_detail["units"]:
                fields["_unit"] = units
            if (desc := template.template_detail["description"]) != parameter_name:
                fields["_description"] = desc
            parameters[parameter_name] = fields

        if category := categories.get(template.category):
            category["all_parameters"].add(parameter_name)
            category["_parameters"].add(parameter_name)

    for _, category in categories.items():
        if parent_category := categories.get(category["parent"]):
            parent_category[category["name"]] = category
            category["_parameters"] -= parent_category["all_parameters"]

    for category in categories.values():
        if not category["_structural"]:
            del category["_structural"]
        if category["_description"] == category["name"]:
            del category["_description"]
        if category["parent"] is not None:
            del category["name"]
            del category["parent"]
        if category["_parameters"]:
            category["_parameters"] = sorted(category["_parameters"])
        else:
            del category["_parameters"]
        del category["all_parameters"]

    category_tree: dict[str, Any] = {
        root_category["name"]: root_category
        for root_category in categories.values()
        if "parent" in root_category
    }

    for root_category in category_tree.values():
        del root_category["name"]
        del root_category["parent"]

    return category_tree, parameters
