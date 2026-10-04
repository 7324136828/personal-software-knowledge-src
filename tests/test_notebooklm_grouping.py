from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from tests.test_notebooklm import PNG_IMAGE


class NotebookLMGroupingTests(unittest.TestCase):
    def test_default_formats_share_source_group_and_keep_mapping_dependencies_on_retry(self):
        config = {"files": [{"type": "notebooklm", "input": "input/Artifacts", "output": "output",
                              "types": ["datatable", "mindmap", "infographic"]}]}
        common = {"status": "ARTIFACT_STATUS_READY", "sources": [{"sourceId": {"id": "observed-source"}}]}
        members = {
            "study-set-config.json": json.dumps(config),
            "input/Artifacts/Table metadata.json": json.dumps({**common, "title": "Table", "type": "ARTIFACT_TYPE_TABLE"}),
            "input/Artifacts/Table.md": "| Topic | Detail |\n| --- | --- |\n| First | Observed [1] |\n\n[1] chapter.txt\n",
            "input/Artifacts/Map metadata.json": json.dumps({**common, "title": "Map", "type": "ARTIFACT_TYPE_APP",
                "app": {"generationOptions": {"appType": "APP_TYPE_MINDMAP"}}}),
            "input/Artifacts/Map.json": json.dumps({"name": "Observed map", "children": [{"name": "Leaf"}]}),
            "input/Artifacts/Diagram metadata.json": json.dumps({**common, "title": "Diagram", "type": "ARTIFACT_TYPE_INFOGRAPHIC"}),
            "input/Artifacts/Diagram.png": PNG_IMAGE,
            "input/Artifacts/Diagram.content.json": json.dumps({
                "title": "Observed diagram", "subtitle": "Observed chapter detail",
                "sections": [{"type": "quote", "title": "Original detail", "value": "Observed chapter detail",
                              "label": None, "items": [], "panel": None, "span": "full"}],
            }),
            "input/Sources/chapter.txt.html": "<p>Original chapter</p>",
            "input/Sources/chapter.txt metadata.json": json.dumps({"title": "chapter.txt"}),
            "input/metadata/sources.metadata.json": json.dumps({"chapter.txt metadata.json": {"id": "observed-source"}}),
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server, "HISTORY_DIR", Path(temporary) / "history"), \
                patch("server._ensure_queue_dispatcher"), \
                patch("study_set._timestamp", return_value="20261004_120000"), \
                patch("notebooklm_media.shutil.which", return_value=None), \
                patch("server.execute_generation", side_effect=AssertionError("Provider called")):
            for collection in (server.ACTIVE_CONVERSIONS, server.DISPATCHED_CONVERSIONS,
                               server.CANCEL_EVENTS, server.QUEUED_API_KEYS):
                collection.clear()
            client = TestClient(server.app)
            response = client.post("/api/study-sets/import", files={
                "file": ("notebooklm.zip", stream.getvalue(), "application/zip"),
            })
            self.assertEqual(response.status_code, 200, response.text)
            records = [server._read_record(identifier) for identifier in response.json()["session_ids"]]
            self.assertEqual(len(records), 10)
            self.assertEqual(len({record["source_group_id"] for record in records}), 1)
            for record in records:
                self.assertEqual(record["notebooklm_source_names"], ["chapter.txt"])
                self.assertTrue(record["package_output_path"].startswith("output/observed-source/"))
                response = client.post(f"/api/history/{record['id']}/continue")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(server._read_record(record["id"])["metrics"]["provider_calls"], 0)
            wireframe = next(record for record in records if record["output_format"] == "wireframe.txt")
            self.assertTrue(wireframe["output_filename"].endswith(".wireframe.txt"))
            map_record = next(record for record in records if record["action"] == "create_mindmaps")
            # The worker reconstructs with resolved names; the persisted dependency
            # list must still retain the table that established that mapping.
            response = client.get(f"/api/history/{map_record['id']}/download")
            self.assertEqual(response.status_code, 200, response.text)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                for filename in ("observed-source/source/input/Artifacts/Table.md", "observed-source/source/input/Artifacts/Table metadata.json",
                                 "observed-source/source/input/Sources/chapter.txt.html", "observed-source/source/input/Sources/chapter.txt metadata.json",
                                 "observed-source/source/input/metadata/sources.metadata.json"):
                    self.assertIn(filename, archive.namelist())
                self.assertIn("observed-source/mindmaps/" + map_record["output_filename"], archive.namelist())
                self.assertIn("conversion.json", archive.namelist())
            response = client.get(f"/api/history/groups/{map_record['source_group_id']}/download")
            self.assertEqual(response.status_code, 200, response.text)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                names = archive.namelist()
                self.assertEqual(names.count("observed-source/source/input/Sources/chapter.txt.html"), 1)
                self.assertEqual(names.count("observed-source/source/input/metadata/sources.metadata.json"), 1)
                histories = [name for name in names if name.startswith("observed-source/metadata/")]
                self.assertEqual(set(histories), {"observed-source/metadata/" + record["id"] + ".json" for record in records})
                self.assertEqual(len(histories), 10)
                self.assertIn("observed-source/manifest.json", names)
                for record in records:
                    category = record["action"].removeprefix("create_")
                    self.assertIn("observed-source/" + category + "/" + record["output_filename"], names)
                self.assertIn("observed-source/infographics/" + wireframe["output_filename"], names)
                self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
