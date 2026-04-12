import re
import shutil
from pathlib import Path

import yaml
from click.testing import CliRunner
from inventree.api import InvenTreeAPI
from inventree.part import ParameterTemplate, PartCategory

from inventree_part_import.categories import setup_config_from_inventree
from inventree_part_import.cli import inventree_part_import
from inventree_part_import.config import (
    CATEGORIES_CONFIG,
    CONFIG,
    INVENTREE_CONFIG,
    PARAMETERS_CONFIG,
    SUPPLIERS_CONFIG,
)

HOST = "http://localhost:55555"
USERNAME = "testuser"
PASSWORD = "testpassword"

TEST_CONFIG_DIR = Path(__file__).parent / "test_config"
DEFAULT_CONFIG_DIR = Path(__file__).parent.parent / "inventree_part_import" / "config"
DEFAULT_CATEGORIES_CONFIG = DEFAULT_CONFIG_DIR / "default_categories.yaml"
DEFAULT_PARAMETERS_CONFIG = DEFAULT_CONFIG_DIR / "default_parameters.yaml"


def test_config_dir_override():
    shutil.rmtree(TEST_CONFIG_DIR, ignore_errors=True)
    result = CliRunner().invoke(
        inventree_part_import,
        ("-c", str(TEST_CONFIG_DIR), "--show-config-dir"),
        input="\n",
    )
    assert result.exit_code == 0
    assert str(TEST_CONFIG_DIR.resolve()) in result.output
    assert TEST_CONFIG_DIR.exists()


class TestCli:
    def setup_class(self):
        shutil.rmtree(TEST_CONFIG_DIR, ignore_errors=True)
        TEST_CONFIG_DIR.mkdir(parents=True)

        self.api = InvenTreeAPI(HOST, username=USERNAME, password=PASSWORD, use_token_auth=True)

        (TEST_CONFIG_DIR / CONFIG).write_text(
            "currency: EUR\nlanguage: EN\nlocation: DE\nscraping: true\ndatasheets: upload\n"
        )
        (TEST_CONFIG_DIR / INVENTREE_CONFIG).write_text(f"host: {HOST}\ntoken: {self.api.token}\n")
        (TEST_CONFIG_DIR / SUPPLIERS_CONFIG).write_text("lcsc:\n    ignore_duplicates: true\n")
        shutil.copy(DEFAULT_CATEGORIES_CONFIG, TEST_CONFIG_DIR / CATEGORIES_CONFIG)
        shutil.copy(DEFAULT_PARAMETERS_CONFIG, TEST_CONFIG_DIR / PARAMETERS_CONFIG)

    def test_setup_categories(self):
        for part_category in PartCategory.list(self.api):
            part_category.delete()
        for parameter_template in ParameterTemplate.list(self.api):
            parameter_template.delete()

        result = CliRunner().invoke(
            inventree_part_import,
            ("-c", str(TEST_CONFIG_DIR), "TL072", "-v", "-i", "false"),
            catch_exceptions=False,
        )
        assert "warning:" not in result.output.split("searching for TL072")[0]

        REMOVE_ALIASES_REGEX = re.compile(r"\s*_aliases:[^:]*(\n[^:]*:)")
        REMOVE_ALIASES_SUB = r"\g<1>"

        categories_yaml = (TEST_CONFIG_DIR / CATEGORIES_CONFIG).read_text(encoding="utf-8")
        categories_yaml = REMOVE_ALIASES_REGEX.sub(REMOVE_ALIASES_SUB, categories_yaml)
        categories_config = yaml.safe_load(categories_yaml)
        parameters_yaml = (TEST_CONFIG_DIR / PARAMETERS_CONFIG).read_text(encoding="utf-8")
        parameters_yaml = REMOVE_ALIASES_REGEX.sub(REMOVE_ALIASES_SUB, parameters_yaml)
        parameters_config = yaml.safe_load(parameters_yaml)

        categories, parameters = setup_config_from_inventree(self.api)

        def make_comparable(dictionary):
            for key, value in dictionary.items():
                if not value:
                    dictionary[key] = None
                elif isinstance(value, dict):
                    dictionary[key] = make_comparable(value)
                elif isinstance(value, list):
                    dictionary[key] = set(value)
            return dictionary

        categories_config = make_comparable(categories_config)
        parameters_config = make_comparable(parameters_config)
        categories = make_comparable(categories)
        parameters = make_comparable(parameters)

        assert categories_config == categories
        assert parameters_config == parameters
