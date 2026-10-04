"""Extract and validate a configuration-driven study-set ZIP without generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
import zipfile

from document_loader import SUPPORTED_EXTENSIONS
from errors import InputDocumentError, InvalidArgumentsError
from package_paths import relative_package_parts
from study_set import CONFIG_NAME, GenerationJob, plan_study_sets


MAX_ARCHIVE_BYTES = 100 * 1024 ** 2
MAX_EXTRACTED_BYTES = 512 * 1024 ** 2
MAX_MEMBERS = 2000
MAX_GENERATION_JOBS = 1000
MAX_CONFIG_BYTES = 2 * 1024 ** 2


@dataclass(frozen=True)
class PreparedStudySet:
    root: Path
    jobs: list[GenerationJob]


def _members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]]:
    """Validate the complete namespace before creating any extracted files."""

    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise InvalidArgumentsError(f"Study-set ZIP exceeds the {MAX_MEMBERS} member limit.")
    paths: dict[str, tuple[str, bool]] = {}
    declared: set[str] = set()
    expanded = 0
    result = []
    for info in infos:
        parts = relative_package_parts(info.orig_filename)
        mode = stat.S_IFMT(info.external_attr >> 16)
        is_directory = info.orig_filename.endswith(("/", "\\")) or mode == stat.S_IFDIR
        if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or (is_directory and mode == stat.S_IFREG):
            raise InvalidArgumentsError("Study-set ZIP may contain only regular files and directories.")
        if info.flag_bits & 1:
            raise InvalidArgumentsError("Encrypted study-set ZIP files are not supported.")
        if info.file_size < 0 or info.compress_size < 0 or (is_directory and info.file_size):
            raise InvalidArgumentsError("Study-set ZIP contains invalid member sizes.")
        expanded += info.file_size
        if expanded > MAX_EXTRACTED_BYTES:
            raise InvalidArgumentsError("Study-set ZIP exceeds the expanded size limit.")
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            key = spelling.casefold()
            directory = index < len(parts) or is_directory
            existing = paths.get(key)
            if existing is not None and existing != (spelling, directory):
                raise InvalidArgumentsError(f"Study-set ZIP has conflicting paths: {spelling}")
            if index == len(parts):
                if key in declared:
                    raise InvalidArgumentsError(f"Study-set ZIP contains a duplicate path: {spelling}")
                declared.add(key)
            paths[key] = (spelling, directory)
        result.append((info, parts, is_directory))
    return result


def _configuration_root(directory: Path) -> Path:
    children = [child for child in directory.iterdir() if child.name not in {"__MACOSX", ".DS_Store"}]
    nested_configs = [child for child in children if child.is_dir() and (child / CONFIG_NAME).is_file()]
    if (directory / CONFIG_NAME).is_file():
        if nested_configs:
            raise InvalidArgumentsError(f"ZIP contains multiple {CONFIG_NAME} files at its root and in an enclosing folder.")
        return directory
    if len(children) == 1 and children[0].is_dir() and (children[0] / CONFIG_NAME).is_file():
        return children[0]
    raise InputDocumentError(f"ZIP must contain {CONFIG_NAME} at its root or inside one enclosing folder.")


def prepare_study_set_archive(archive_path: Path, extraction_dir: Path) -> PreparedStudySet:
    """Expand into a fresh folder and plan every configured job atomically."""

    archive_path, extraction_dir = Path(archive_path), Path(extraction_dir).resolve()
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise InvalidArgumentsError("Study-set ZIP exceeds the uploaded size limit.")
    if extraction_dir.exists() and any(extraction_dir.iterdir()):
        raise InvalidArgumentsError("ZIP extraction requires an empty destination folder.")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _members(archive)
            extraction_dir.mkdir(parents=True, exist_ok=True)
            expanded = 0
            for info, parts, is_directory in members:
                target = extraction_dir.joinpath(*parts)
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        expanded += len(chunk)
                        if expanded > MAX_EXTRACTED_BYTES:
                            raise InvalidArgumentsError("Study-set ZIP exceeds the expanded size limit.")
                        output.write(chunk)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as exc:
        raise InvalidArgumentsError("Could not extract study-set ZIP. Use an unencrypted, valid ZIP file.") from exc
    root = _configuration_root(extraction_dir)
    if (root / CONFIG_NAME).stat().st_size > MAX_CONFIG_BYTES:
        raise InvalidArgumentsError("Study-set configuration exceeds the size limit.")
    jobs = plan_study_sets(root, contained=True, max_jobs=MAX_GENERATION_JOBS)
    sources = {job.source for job in jobs}
    for job in jobs:
        task = getattr(job, "notebooklm_task", None)
        if task is not None:
            sources.update(task.dependencies)
            sources.add(task.source.path)
        if job.input_type == "notebooklm" and job.notebooklm_artifact is not None:
            sources.add(job.notebooklm_artifact.metadata_path)
            sources.update(job.notebooklm_artifact.dependencies)
    for job in jobs:
        if job.input_type != "notebooklm" and job.source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise InvalidArgumentsError(f"Unsupported input file in study-set ZIP: {job.source.relative_to(root)}")
        if job.output in sources or any(source.is_dir() and source in job.output.parents for source in sources):
            raise InvalidArgumentsError("Study-set output must not overwrite its input file.")
        for field, target, is_directory in (("output", job.output, False), ("work directory", job.args.work_dir, True)):
            relative_package_parts(target.relative_to(root).as_posix())
            if (is_directory and target.is_file()) or (not is_directory and target.is_dir()):
                raise InvalidArgumentsError(f"Study-set {field} conflicts with an existing package path.")
            for parent in target.parents:
                if parent == root:
                    break
                if parent.is_file():
                    raise InvalidArgumentsError(f"Study-set {field} must not be inside a package file.")
    return PreparedStudySet(root, jobs)
