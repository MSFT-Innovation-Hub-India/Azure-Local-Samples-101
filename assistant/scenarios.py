import hashlib
import json
import re
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from qdrant_client import QdrantClient, models

from assistant.config import INDEX_ALIAS
from assistant.indexer import active_collection, index_folder
from assistant.clients import Embedder


DEFAULT_SCENARIO = "operations"
CATALOG = "assistant-scenario-catalog"
SLUG_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
MAX_PACKAGE_BYTES = 48 * 1024 * 1024
MAX_SCENARIOS = 100


class ScenarioError(ValueError):
    pass


class UnknownScenario(ScenarioError):
    pass


class ScenarioDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int = Field(ge=1, le=1)
    id: str = Field(pattern=SLUG_PATTERN, min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    example_questions: list[str] = Field(default_factory=list, max_length=6)


class PublishedScenario(ScenarioDefinition):
    system_prompt: str = Field(max_length=2000)
    collection: str | None
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    def public(self) -> dict:
        return {
            **self.model_dump(include={"id", "name", "description", "example_questions"}),
            "revision": self.revision,
        }


def validate_definition(data: dict) -> ScenarioDefinition:
    try:
        definition = ScenarioDefinition.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise ScenarioError(f"Invalid scenario definition fields: {fields}") from None
    if not definition.name.strip() or not definition.description.strip() or any(
        not question.strip() or len(question) > 200 for question in definition.example_questions
    ):
        raise ScenarioError("Scenario labels/questions must be nonblank; example questions allow 200 characters.")
    return definition


def read_source(folder: Path) -> tuple[ScenarioDefinition, str, list[Path]]:
    root = folder.resolve()
    if folder.is_symlink() or not root.is_dir():
        raise ScenarioError("Scenario source must be a regular directory.")
    for name in ("scenario.json", "system-prompt.md"):
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ScenarioError(f"Missing, linked or oversized scenario file: {name}")
    definition = validate_definition(json.loads((root / "scenario.json").read_text(encoding="utf-8")))
    prompt = (root / "system-prompt.md").read_text(encoding="utf-8").strip()
    if not prompt or len(prompt) > 2000:
        raise ScenarioError("The domain prompt must contain 1-2000 characters.")
    documents = root / "documents"
    if not documents.is_dir() or documents.is_symlink():
        raise ScenarioError("A regular documents folder containing text PDFs is required.")
    paths = [root / "scenario.json", root / "system-prompt.md"]
    pdfs = []
    for path in documents.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ScenarioError("Scenario symlinks or paths outside the source directory are not allowed.")
        if path.is_file():
            if path.name == ".gitkeep":
                continue
            if path.suffix.lower() != ".pdf":
                raise ScenarioError(f"Only PDF documents are supported: {path.name}")
            if path.stat().st_size > 25 * 1024 * 1024:
                raise ScenarioError(f"PDF exceeds 25 MiB: {path.name}")
            pdfs.append(path)
    if not 1 <= len(pdfs) <= 200:
        raise ScenarioError("A scenario requires 1-200 text PDFs.")
    paths.extend(sorted(pdfs))
    if sum(path.stat().st_size for path in paths) > MAX_PACKAGE_BYTES:
        raise ScenarioError("Scenario packages are limited to 48 MiB of expanded content.")
    return definition, prompt, paths


def package_scenario(folder: Path, destination: Path) -> Path:
    definition, _, paths = read_source(folder)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{definition.id}-{uuid.uuid4().hex[:12]}.zip"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(folder.resolve()).as_posix())
    if output.stat().st_size > MAX_PACKAGE_BYTES:
        output.unlink()
        raise ScenarioError("Compressed scenario package exceeds 48 MiB.")
    return output


def extract_package(package: Path, destination: Path) -> None:
    if package.stat().st_size > MAX_PACKAGE_BYTES:
        raise ScenarioError("Compressed scenario package exceeds 48 MiB.")
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        if not 3 <= len(members) <= 202:
            raise ScenarioError("Scenario package requires two configuration files and 1-200 PDFs.")
        seen = set()
        total = 0
        for member in members:
            path = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if (
                path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
                or member.orig_filename != member.filename
                or member.filename != path.as_posix()
                or "\\" in member.filename or ":" in member.filename
                or member.filename.casefold() in seen or member.is_dir()
                or member.flag_bits & 1 or stat.S_ISLNK(mode)
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            ):
                raise ScenarioError("Scenario ZIP contains an unsafe, encrypted or duplicate entry.")
            if member.filename not in {"scenario.json", "system-prompt.md"} and not (
                len(path.parts) >= 2 and path.parts[0] == "documents" and path.suffix.lower() == ".pdf"
            ):
                raise ScenarioError("Scenario ZIP contains files outside its definition, prompt and PDF folder.")
            limit = 25 * 1024 * 1024 if path.suffix.lower() == ".pdf" else 16384
            total += member.file_size
            if member.file_size > limit or total > MAX_PACKAGE_BYTES:
                raise ScenarioError("Expanded scenario package exceeds its file or total size limit.")
            seen.add(member.filename.casefold())
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source:
                content = source.read(limit + 1)
            if len(content) > limit:
                raise ScenarioError("Expanded ZIP entry exceeds its size limit.")
            target.write_bytes(content)
        if not {"scenario.json", "system-prompt.md"} <= seen:
            raise ScenarioError("Scenario ZIP is missing its definition or domain prompt.")


def scenario_point_id(scenario_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"azure-local-assistant/scenario/{scenario_id}"))


def legacy_operations(database: QdrantClient) -> PublishedScenario:
    collection = active_collection(database)
    return PublishedScenario(
        schema_version=1, id=DEFAULT_SCENARIO, name="Operations guide",
        description="The original synthetic recovery and backup policy demo.",
        example_questions=["What is the service recovery code?", "How often are backups taken?"],
        system_prompt="", collection=collection,
        revision=hashlib.sha256((collection or INDEX_ALIAS).encode()).hexdigest(),
    )


def parse_published(payload: dict | None) -> PublishedScenario:
    try:
        scenario = PublishedScenario.model_validate(payload)
        validate_definition(scenario.model_dump(exclude={"system_prompt", "collection", "revision"}))
    except (ValidationError, ScenarioError):
        raise ScenarioError("Stored scenario metadata is invalid. Republish its package.") from None
    prefix = "assistant-pdf" if scenario.id == DEFAULT_SCENARIO else f"assistant-{scenario.id}-pdf"
    if scenario.collection is not None and not re.fullmatch(
        re.escape(prefix) + r"-[0-9a-f]{24}-[0-9a-f]{8}", scenario.collection,
    ):
        raise ScenarioError("Stored scenario references an invalid index snapshot.")
    return scenario


def list_scenarios(database: QdrantClient) -> list[PublishedScenario]:
    scenarios = {}
    if database.collection_exists(CATALOG):
        points, offset = database.scroll(CATALOG, limit=MAX_SCENARIOS + 1, with_payload=True, with_vectors=False)
        if offset is not None or len(points) > MAX_SCENARIOS:
            raise ScenarioError("Scenario catalog exceeds the 100-scenario demo limit.")
        for point in points:
            scenario = parse_published(point.payload)
            if str(point.id) != scenario_point_id(scenario.id):
                raise ScenarioError("Scenario catalog identifier mismatch.")
            scenarios[scenario.id] = scenario
    if DEFAULT_SCENARIO not in scenarios:
        scenarios[DEFAULT_SCENARIO] = legacy_operations(database)
    if len(scenarios) > MAX_SCENARIOS:
        raise ScenarioError("Scenario catalog exceeds the 100-scenario demo limit.")
    return sorted(scenarios.values(), key=lambda scenario: (scenario.name.casefold(), scenario.id))


def get_scenario(database: QdrantClient, scenario_id: str) -> PublishedScenario:
    if len(scenario_id) > 40 or not re.fullmatch(SLUG_PATTERN, scenario_id):
        raise UnknownScenario("Invalid scenario identifier.")
    if database.collection_exists(CATALOG):
        points = database.retrieve(CATALOG, [scenario_point_id(scenario_id)], with_payload=True)
        if points:
            scenario = parse_published(points[0].payload)
            if scenario.id != scenario_id:
                raise ScenarioError("Scenario catalog identifier mismatch.")
            return scenario
    if scenario_id == DEFAULT_SCENARIO:
        return legacy_operations(database)
    raise UnknownScenario("Unknown scenario. Refresh the scenario list.")


def publish_scenario(package: Path, database: QdrantClient, embedder: Embedder) -> dict:
    with tempfile.TemporaryDirectory(prefix="scenario-") as temporary:
        folder = Path(temporary)
        extract_package(package, folder)
        definition, prompt, _ = read_source(folder)
        existing = {scenario.id: scenario for scenario in list_scenarios(database)}
        if definition.id not in existing and len(existing) >= MAX_SCENARIOS:
            raise ScenarioError("The demo supports at most 100 scenarios.")
        if not database.collection_exists(CATALOG):
            database.create_collection(CATALOG, vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE))
        # Pin the legacy prompt/index pair before its alias can advance for the first time.
        if definition.id == DEFAULT_SCENARIO and not database.retrieve(CATALOG, [scenario_point_id(DEFAULT_SCENARIO)]):
            old = existing[DEFAULT_SCENARIO]
            database.upsert(CATALOG, [models.PointStruct(
                id=scenario_point_id(old.id), vector=[1.0], payload=old.model_dump(),
            )], wait=True)
        alias = INDEX_ALIAS if definition.id == DEFAULT_SCENARIO else f"assistant-{definition.id}-active"
        prefix = "assistant-pdf" if definition.id == DEFAULT_SCENARIO else f"assistant-{definition.id}-pdf"
        result = index_folder(folder / "documents", database, embedder, alias=alias, prefix=prefix)
        revision = hashlib.sha256(json.dumps(
            [definition.model_dump(), prompt, result["collection"]], sort_keys=True,
        ).encode()).hexdigest()
        published = PublishedScenario(
            **definition.model_dump(), system_prompt=prompt,
            collection=result["collection"], revision=revision,
        )
        # Readers see one atomic record: the prompt and immutable vector snapshot always match.
        database.upsert(CATALOG, [models.PointStruct(
            id=scenario_point_id(definition.id), vector=[1.0], payload=published.model_dump(),
        )], wait=True)
        return {**result, "scenario": definition.id, "revision": revision}
