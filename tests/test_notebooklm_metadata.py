from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from errors import InputDocumentError, InvalidArgumentsError
from notebooklm_metadata import load_notebooklm_source_metadata


SOURCE_UUID = "1401cb87-2d31-4fa9-8b10-32951a5c8948"
SECOND_UUID = "2328a0b7-5047-4967-a8db-4cd1604b4a25"


class NotebookLMSourceMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "notebook"
        self.root.mkdir()
        self.mapping_path = self.root / "metadata" / "sources.metadata.json"

    def write_mapping(self, value) -> None:
        self.write_raw(json.dumps(value, ensure_ascii=False))

    def write_raw(self, value: str) -> None:
        self.mapping_path.parent.mkdir(exist_ok=True)
        self.mapping_path.write_text(value, encoding="utf-8")

    def test_missing_file_requests_generation_with_the_exact_template(self) -> None:
        with self.assertRaises(InputDocumentError) as caught:
            load_notebooklm_source_metadata(self.root)
        message = str(caught.exception)
        self.assertIn(str(self.mapping_path), message)
        self.assertIn("Generate this file", message)
        template = json.loads(message.split("JSON template: ", 1)[1])
        self.assertEqual(template, {"{metadata.json}": {"id": "source_id"}})
        self.assertFalse(self.mapping_path.exists())

    def test_unicode_metadata_filenames_preserve_recorded_ids(self) -> None:
        values = {"心经_en.txt metadata.json": {"id": SOURCE_UUID},
                  "朱子家训_en.txt metadata.json": {"id": SECOND_UUID}}
        self.write_mapping(values)
        path, mapping = load_notebooklm_source_metadata(self.root)
        self.assertEqual(path, self.mapping_path.resolve())
        self.assertEqual(mapping, {name: entry["id"] for name, entry in values.items()})

    def test_export_subfolder_inputs_find_the_same_notebook_mapping(self) -> None:
        self.write_mapping({"chapter.txt metadata.json": {"id": SOURCE_UUID}})
        for name in ("Artifacts", "Sources"):
            with self.subTest(name=name):
                folder = self.root / name
                folder.mkdir()
                path, mapping = load_notebooklm_source_metadata(folder, contained=True, package_root=self.root)
                self.assertEqual(path, self.mapping_path.resolve())
                self.assertEqual(mapping, {"chapter.txt metadata.json": SOURCE_UUID})

    def test_dot_metadata_suffix_and_safe_opaque_recorded_id_are_supported(self) -> None:
        self.write_mapping({"chapter.txt.metadata.json": {"id": "observed-source"}})
        _, mapping = load_notebooklm_source_metadata(self.root)
        self.assertEqual(mapping, {"chapter.txt.metadata.json": "observed-source"})

    def test_utf8_bom_export_is_supported(self) -> None:
        self.write_raw("\ufeff" + json.dumps({"chapter.txt metadata.json": {"id": SOURCE_UUID}}))
        self.assertEqual(load_notebooklm_source_metadata(self.root)[1], {"chapter.txt metadata.json": SOURCE_UUID})

    def test_malformed_or_empty_mapping_is_rejected(self) -> None:
        for value in ("{", "null", "[]", "{}", '"text"'):
            with self.subTest(value=value):
                self.write_raw(value)
                with self.assertRaises(InputDocumentError):
                    load_notebooklm_source_metadata(self.root)

    def test_duplicate_json_keys_are_rejected_instead_of_overwritten(self) -> None:
        for second_key in ("chapter.txt metadata.json", "CHAPTER.TXT METADATA.JSON"):
            with self.subTest(second_key=second_key):
                self.write_raw('{"chapter.txt metadata.json":{"id":"' + SOURCE_UUID + '"},'
                               + json.dumps(second_key) + ':{"id":"' + SECOND_UUID + '"}}')
                with self.assertRaisesRegex(InputDocumentError, "duplicate JSON key"):
                    load_notebooklm_source_metadata(self.root)

    def test_two_documents_cannot_share_the_same_source_uuid(self) -> None:
        self.write_mapping({"心经_en.txt metadata.json": {"id": SOURCE_UUID},
                            "朱子家训_en.txt metadata.json": {"id": SOURCE_UUID}})
        with self.assertRaises(InputDocumentError) as caught:
            load_notebooklm_source_metadata(self.root)
        message = str(caught.exception)
        self.assertIn(SOURCE_UUID, message)
        self.assertIn("心经_en.txt metadata.json", message)
        self.assertIn("朱子家训_en.txt metadata.json", message)

    def test_uuid_case_aliases_are_rejected_as_duplicate_folder_ids(self) -> None:
        self.write_mapping({"chapter.txt metadata.json": {"id": SOURCE_UUID},
                            "second.txt metadata.json": {"id": SOURCE_UUID.upper()}})
        with self.assertRaisesRegex(InputDocumentError, "multiple metadata filenames"):
            load_notebooklm_source_metadata(self.root)

    def test_entries_require_a_nonempty_safe_string_id(self) -> None:
        entries = [None, [], "uuid", {}, {"id": None}, {"id": 12}, {"id": True},
                   {"id": ""}, {"id": " "}, {"id": "../outside"}, {"id": "a/b"},
                   {"id": "a\\b"}, {"id": "C:outside"}, {"id": "a\n"},
                   {"id": ".."}, {"id": "NUL"}, {"id": "trailing."}]
        for entry in entries:
            with self.subTest(entry=entry):
                self.write_mapping({"chapter.txt metadata.json": entry})
                with self.assertRaises(InputDocumentError):
                    load_notebooklm_source_metadata(self.root)

    def test_mapping_keys_require_bare_source_metadata_filenames(self) -> None:
        names = ["chapter.txt", " metadata.json", "Sources/chapter.txt metadata.json",
                 "../chapter.txt metadata.json", "Sources\\chapter.txt metadata.json",
                 "chapter:txt metadata.json", "chapter\n metadata.json", "CON.metadata.json"]
        for name in names:
            with self.subTest(name=name):
                self.write_mapping({name: {"id": SOURCE_UUID}})
                with self.assertRaises(InputDocumentError):
                    load_notebooklm_source_metadata(self.root)

    def test_notebook_outside_the_package_is_rejected(self) -> None:
        self.write_mapping({"chapter.txt metadata.json": {"id": SOURCE_UUID}})
        with self.assertRaisesRegex(InvalidArgumentsError, "inside the study-set package"):
            load_notebooklm_source_metadata(self.root, contained=True, package_root=self.root / "different-package")


if __name__ == "__main__":
    unittest.main()
