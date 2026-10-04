from __future__ import annotations

import copy
import io
import json
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from cli_runtime import options_from_args
from errors import ApplicationError
import study_package


def package_config() -> dict:
    return {
        "model": "package-default-model",
        "connector": "openai",
        "context_window": 12000,
        "files": [{
            "input": "input",
            "output": "study-output",
            "inputPattern": "*.txt",
            "types": ["podcast"],
            "formats": ["json"],
        }],
    }


def package_members(config: dict | None = None, *, prefix: str = "") -> list[tuple[str | zipfile.ZipInfo, bytes]]:
    value = package_config() if config is None else config
    return [
        (prefix + "study-set-config.json", json.dumps(value).encode("utf-8")),
        (prefix + "input/chapter 1.txt", b"A compact source statement."),
    ]


def archive_bytes(members: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return output.getvalue()


class StudyPackageTests(unittest.TestCase):
    def prepare(self, directory: str, content: bytes):
        base = Path(directory)
        archive = base / "study-set.zip"
        archive.write_bytes(content)
        return study_package.prepare_study_set_archive(archive, base / "extracted")

    def assert_rejected(self, content: bytes) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ApplicationError):
                self.prepare(directory, content)
            self.assertFalse((Path(directory) / "outside.txt").exists())

    def test_root_package_plans_configured_model_formats_and_context_without_execution(self) -> None:
        config = package_config()
        entry = config["files"][0]
        entry.update({
            "model": "package-entry-model", "connector": "the_connector",
            "context_window": 16000, "pipeline": {"context_window": 24000},
            "types": ["podcast", "report"], "formats": ["json", "md"],
        })
        cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, \
                patch("os.chdir") as change_directory, \
                patch("study_set.run_cli_generation") as run_cli, \
                patch("cli_runtime.execute_generation") as generate, \
                patch("cli_runtime.create_connector") as create_connector:
            prepared = self.prepare(directory, archive_bytes(package_members(config)))
            self.assertEqual(prepared.root.resolve(), (Path(directory) / "extracted").resolve())
            self.assertEqual(len(prepared.jobs), 4)
            outputs = set()
            for job in prepared.jobs:
                self.assertEqual(job.source.relative_to(prepared.root).as_posix(), "input/chapter 1.txt")
                self.assertEqual(job.args.model, "package-entry-model")
                self.assertEqual(job.args.connector, "the_connector")
                self.assertEqual(options_from_args(job.args).profile_overrides["context_window"], 24000)
                outputs.add(job.output.relative_to(prepared.root).as_posix())
            self.assertEqual(outputs, {
                "study-output/chapter 1/podcast/podcasts.json",
                "study-output/chapter 1/podcast/podcasts.md",
                "study-output/chapter 1/report/reports.json",
                "study-output/chapter 1/report/reports.md",
            })
            change_directory.assert_not_called()
            run_cli.assert_not_called()
            generate.assert_not_called()
            create_connector.assert_not_called()
        self.assertEqual(Path.cwd(), cwd)

    def test_single_wrapper_folder_is_the_logical_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = self.prepare(directory, archive_bytes(package_members(prefix="course/")))
            self.assertEqual(prepared.root.resolve(), (Path(directory) / "extracted" / "course").resolve())
            self.assertEqual(len(prepared.jobs), 1)
            self.assertEqual(prepared.jobs[0].source.relative_to(prepared.root).as_posix(), "input/chapter 1.txt")

    def test_windows_zip_backslashes_are_normalized(self) -> None:
        members = [(str(name).replace("/", "\\"), value) for name, value in package_members(prefix="course/")]
        with tempfile.TemporaryDirectory() as directory:
            prepared = self.prepare(directory, archive_bytes(members))
            self.assertEqual(prepared.root.name, "course")
            self.assertEqual(prepared.jobs[0].source.name, "chapter 1.txt")

    def test_regular_unix_files_and_explicit_directories_are_accepted(self) -> None:
        directory_entry = zipfile.ZipInfo("input/")
        directory_entry.create_system = 3
        directory_entry.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
        source_entry = zipfile.ZipInfo("input/chapter 1.txt")
        source_entry.create_system = 3
        source_entry.external_attr = (stat.S_IFREG | 0o644) << 16
        members = [package_members()[0], (directory_entry, b""), (source_entry, b"Source evidence")]
        with tempfile.TemporaryDirectory() as directory:
            prepared = self.prepare(directory, archive_bytes(members))
            self.assertEqual(len(prepared.jobs), 1)

    def test_missing_ambiguous_and_deeply_nested_configurations_are_rejected(self) -> None:
        cases = (
            [("input/chapter.txt", b"Source")],
            package_members(prefix="first/") + package_members(prefix="second/"),
            package_members() + [("course/study-set-config.json", b"{}")],
            package_members(prefix="outer/inner/"),
        )
        for members in cases:
            with self.subTest(names=[str(name) for name, _ in members]):
                self.assert_rejected(archive_bytes(members))

    def test_existing_extraction_directory_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            destination = base / "extracted"
            destination.mkdir()
            marker = destination / "keep.txt"
            marker.write_text("existing data", encoding="utf-8")
            with self.assertRaises(ApplicationError):
                self.prepare(directory, archive_bytes(package_members()))
            self.assertEqual(marker.read_text(encoding="utf-8"), "existing data")
            self.assertEqual(list(destination.iterdir()), [marker])

    def test_malformed_archive_and_configuration_are_rejected(self) -> None:
        for content in (b"not a ZIP", archive_bytes([
            ("study-set-config.json", b"{invalid json"),
            ("input/chapter.txt", b"Source"),
        ])):
            with self.subTest(content=content[:20]):
                self.assert_rejected(content)

    def test_unsafe_archive_paths_are_rejected_before_extraction(self) -> None:
        names = (
            "../outside.txt", "input/../../outside.txt", "/outside.txt",
            "C:/outside.txt", "C:outside.txt", "\\\\server\\share\\outside.txt",
            "\\\\?\\C:\\outside.txt", "input/chapter.txt:stream",
            "input/CON.txt", "input/AUX", "input/NUL.txt", "input/COM1.txt",
            "input/LPT9.txt", "input/chapter.txt.", "input/chapter.txt ",
            "input/bad?.txt", "input/bad*.txt", "input/bad|.txt",
            'input/bad".txt', "input/bad<.txt", "input/bad>.txt", "input/control\x01.txt",
        )
        for name in names:
            with self.subTest(name=name):
                self.assert_rejected(archive_bytes(package_members() + [(name, b"unsafe")]))

    def test_nul_in_original_member_name_is_rejected(self) -> None:
        content = archive_bytes(package_members() + [("input/chapterx.txt", b"Source")])
        # Keep the ZIP record sizes intact while introducing a name which
        # ZipInfo.filename truncates, but ZipInfo.orig_filename preserves.
        content = content.replace(b"input/chapterx.txt", b"input/chapter\x00.txt")
        self.assert_rejected(content)

    def test_symlinks_and_other_special_file_types_are_rejected(self) -> None:
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(mode=mode):
                entry = zipfile.ZipInfo("input/special.txt")
                entry.create_system = 3
                entry.external_attr = (mode | 0o777) << 16
                self.assert_rejected(archive_bytes(package_members() + [(entry, b"../../outside.txt")]))

    def test_case_aliases_and_file_directory_collisions_are_rejected(self) -> None:
        cases = (
            [("input/CHAPTER 1.txt", b"Other")],
            [("Input/other.txt", b"Other")],
            [("notes", b"File"), ("notes/chapter.txt", b"Source")],
            [("notes/chapter.txt", b"Source"), ("notes", b"File")],
            [("input/chapter 1.txt", b"Duplicate")],
        )
        for additions in cases:
            with self.subTest(names=[str(name) for name, _ in additions]):
                # zipfile warns on duplicate names, which are deliberately
                # constructed here to verify import rejects them.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    self.assert_rejected(archive_bytes(package_members() + additions))

    def test_encrypted_members_are_rejected(self) -> None:
        content = bytearray(archive_bytes(package_members()))
        for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            start = 0
            while (position := content.find(signature, start)) >= 0:
                flags = struct.unpack_from("<H", content, position + flag_offset)[0]
                struct.pack_into("<H", content, position + flag_offset, flags | 1)
                start = position + 4
        self.assert_rejected(bytes(content))

    def test_configuration_paths_cannot_escape_logical_root(self) -> None:
        cases = (
            ("input", "../input"), ("output", "../outside"),
            ("output", "C:/outside"), ("output", "C:outside"),
            ("output", "\\\\server\\share\\outside"),
            ("pipeline", {"work_dir": "../work"}),
            ("pipeline", {"model_profile": "../profile.json"}),
            ("inputPattern", "../*.txt"), ("inputPattern", "C:/outside/*.txt"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                config = package_config()
                config["files"][0][field] = value
                self.assert_rejected(archive_bytes(package_members(config)))

    def test_wrapper_configuration_cannot_access_sibling_files(self) -> None:
        config = package_config()
        config["files"][0]["input"] = "../input"
        members = package_members(config, prefix="course/") + [("input/outside.txt", b"Sibling source")]
        self.assert_rejected(archive_bytes(members))

    def test_global_pipeline_paths_are_also_contained(self) -> None:
        for settings in ({"work_dir": "../work"}, {"model_profile": "../profile.json"}):
            with self.subTest(settings=settings):
                config = package_config()
                config["pipeline"] = settings
                self.assert_rejected(archive_bytes(package_members(config)))

    def test_output_and_work_directories_cannot_traverse_existing_source_files(self) -> None:
        for field in ("output", "work_dir"):
            with self.subTest(field=field):
                config = package_config()
                if field == "output":
                    config["files"][0]["output"] = "input/chapter 1.txt"
                else:
                    config["pipeline"] = {"work_dir": "input/chapter 1.txt"}
                self.assert_rejected(archive_bytes(package_members(config)))

    def test_source_stems_cannot_create_parent_or_windows_alias_output_paths(self) -> None:
        for filename in ("...txt", "chapter .txt"):
            with self.subTest(filename=filename):
                members = [package_members()[0], ("input/" + filename, b"Source evidence")]
                self.assert_rejected(archive_bytes(members))

    def test_generated_output_cannot_overwrite_a_selected_input(self) -> None:
        config = package_config()
        config["files"][0].update({
            "input": "podcasts/podcast", "output": ".",
            "inputPattern": "podcasts.json",
        })
        members = [package_members(config)[0], ("podcasts/podcast/podcasts.json", b'{"source": "evidence"}')]
        self.assert_rejected(archive_bytes(members))

    def test_generated_output_cannot_replace_an_existing_directory(self) -> None:
        members = package_members() + [("study-output/chapter 1/podcast/podcasts.json/", b"")]
        self.assert_rejected(archive_bytes(members))

    def test_model_profile_is_loaded_and_validated_before_generation(self) -> None:
        config = package_config()
        config.pop("context_window")
        config["pipeline"] = {"model_profile": "profiles/model.json", "work_dir": "pipeline-work"}
        profile = {"context_window": 32000, "max_input_tokens": 24000,
                   "max_output_tokens": 4096, "reserved_output_tokens": 4096}
        members = package_members(config) + [("profiles/model.json", json.dumps(profile).encode("utf-8"))]
        with tempfile.TemporaryDirectory() as directory:
            prepared = self.prepare(directory, archive_bytes(members))
            job = prepared.jobs[0]
            self.assertEqual(options_from_args(job.args).profile_overrides, profile)
            self.assertEqual(job.args.work_dir.relative_to(prepared.root).parts[0], "pipeline-work")
        for invalid in ({"unknown_profile_field": 123}, {"max_input_tokens": False},
                        {"context_window": 0}, {"preferred_input_ratio": "0.5"},
                        {"temperature": "warm"}):
            with self.subTest(profile=invalid):
                members = package_members(config) + [("profiles/model.json", json.dumps(invalid).encode("utf-8"))]
                self.assert_rejected(archive_bytes(members))

    def test_unsupported_matched_inputs_are_rejected(self) -> None:
        config = package_config()
        config["files"][0]["inputPattern"] = "*.exe"
        members = package_members(config) + [("input/program.exe", b"Not a document")]
        self.assert_rejected(archive_bytes(members))

    def test_resource_limits_are_enforced_without_large_test_archives(self) -> None:
        content = archive_bytes(package_members())
        with patch.object(study_package, "MAX_ARCHIVE_BYTES", len(content) - 1):
            self.assert_rejected(content)
        with patch.object(study_package, "MAX_MEMBERS", 1):
            self.assert_rejected(content)
        with patch.object(study_package, "MAX_EXTRACTED_BYTES", 8):
            self.assert_rejected(content)
        config = package_config()
        config["files"][0]["types"] = ["podcast", "report"]
        with patch.object(study_package, "MAX_GENERATION_JOBS", 1):
            self.assert_rejected(archive_bytes(package_members(config)))

    def test_later_invalid_file_group_rejects_whole_plan(self) -> None:
        config = package_config()
        invalid = copy.deepcopy(config["files"][0])
        invalid["types"] = ["unknown-type"]
        config["files"].append(invalid)
        with patch("study_set.run_cli_generation") as generate:
            self.assert_rejected(archive_bytes(package_members(config)))
            generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
