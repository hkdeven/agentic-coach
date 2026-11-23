"""Tests for the display_name setting (1.3.0): the name in the page title and
header, read through common.load_config and changed with config.py.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_site  # noqa: E402
import common  # noqa: E402
import config  # noqa: E402
from test_cost import Base  # noqa: E402


def write_config(raw):
    path = common.config_file()
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path, "w") as fh:
        json.dump(raw, fh)


def head(page):
    """The page's title and h1, the two places the name is shown."""
    title = page.split("</title>", 1)[0]
    h1 = page.split("<h1>", 1)[1].split("</h1>", 1)[0]
    return title, h1


class DisplayNameConfigTest(Base):
    def test_default_is_agentic_coach(self):
        self.assertEqual(common.load_config()["display_name"], "Agentic Coach")
        write_config({"notify": True})
        self.assertEqual(common.load_config()["display_name"], "Agentic Coach")

    def test_set_and_unset(self):
        self.assertIsNone(config.change(["set", "display_name", "Team", "Coach"]))
        self.assertEqual(common.load_config()["display_name"], "Team Coach")
        with open(common.config_file()) as fh:
            self.assertEqual(json.load(fh)["display_name"], "Team Coach")
        self.assertIsNone(config.change(["unset", "display_name"]))
        self.assertEqual(common.load_config()["display_name"], "Agentic Coach")
        with open(common.config_file()) as fh:
            self.assertNotIn("display_name", json.load(fh))

    def test_set_keeps_other_keys(self):
        write_config({"notify": True, "exclude": [{"match": ["side-project"]}]})
        self.assertIsNone(config.change(["set", "display_name", "My", "coach"]))
        with open(common.config_file()) as fh:
            raw = json.load(fh)
        self.assertEqual(raw["notify"], True)
        self.assertEqual(raw["exclude"], [{"match": ["side-project"]}])
        self.assertFalse(common.load_config()["damaged"])

    def test_empty_name_is_refused(self):
        self.assertEqual(config.change(["set", "display_name", "  "]), config.USAGE)
        self.assertEqual(config.change(["set", "display_name"]), config.USAGE)
        self.assertFalse(os.path.exists(common.config_file()))

    def test_unusable_values_fall_back_without_damaging_the_file(self):
        for value in (None, 42, "", "   ", ["Team Coach"], {"name": "x"}):
            write_config({"display_name": value})
            cfg = common.load_config()
            self.assertEqual(cfg["display_name"], "Agentic Coach", value)
            self.assertFalse(cfg["damaged"], value)

    def test_one_line_and_cut_to_40_characters(self):
        write_config({"display_name": "  Team\n\tcoach  "})
        self.assertEqual(common.load_config()["display_name"], "Team coach")
        write_config({"display_name": "x" * 100})
        self.assertEqual(common.load_config()["display_name"], "x" * 40)


class DisplayNamePageTest(Base):
    def setUp(self):
        Base.setUp(self)
        # The Overview's feed reads transcripts from PROJECTS_DIR: keep it on the temp folder.
        self._projects_dir = common.PROJECTS_DIR
        common.PROJECTS_DIR = self.projects

    def tearDown(self):
        common.PROJECTS_DIR = self._projects_dir
        Base.tearDown(self)

    def build(self):
        reports = os.path.join(self.tmp, "reports")
        os.makedirs(reports)
        page, _card = build_site.build_page(reports, self.projects, "2026-11-17", False)
        return page

    def test_page_shows_the_default_name(self):
        page = self.build()
        self.assertEqual(head(page), ("<title>Agentic Coach", "Agentic Coach"))
        self.assertNotIn("Team Coach", page)
        self.assertNotIn("{{NAME}}", page)

    def test_page_shows_the_chosen_name(self):
        self.assertIsNone(config.change(["set", "display_name", "Team", "Coach"]))
        page = self.build()
        self.assertEqual(head(page), ("<title>Team Coach", "Team Coach"))

    def test_name_is_escaped(self):
        write_config({"display_name": "<b>Mine</b> & co"})
        page = self.build()
        self.assertEqual(head(page)[1], "&lt;b&gt;Mine&lt;/b&gt; &amp; co")
        self.assertNotIn("<b>Mine</b>", page)


if __name__ == "__main__":
    unittest.main()
