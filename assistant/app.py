import logging
import secrets
from contextlib import ExitStack, asynccontextmanager, closing
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from assistant.clients import (
    DependencyError, EmbeddingClient, build_model_router, model_http_client, qdrant_client,
)
from assistant.config import MODEL_ID_PATTERN, Settings, load_models
from assistant.service import AssistantService, UnknownModel
from assistant.scenarios import DEFAULT_SCENARIO, SLUG_PATTERN, ScenarioError, UnknownScenario, get_scenario, list_scenarios


logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    scenario: str = Field(default=DEFAULT_SCENARIO, pattern=SLUG_PATTERN, max_length=40)
    model: str | None = Field(default=None, pattern=MODEL_ID_PATTERN.pattern, max_length=60)


def create_app(settings: Settings | None = None, service: AssistantService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with ExitStack() as stack:
            if service is not None:
                app.state.service = service
            else:
                database = stack.enter_context(closing(qdrant_client(settings)))
                embedding_http = stack.enter_context(httpx.Client(
                    base_url=settings.embedding_url, timeout=120, trust_env=False,
                ))
                model_http = stack.enter_context(model_http_client(settings))
                options = load_models(settings)
                app.state.service = AssistantService(
                    database, EmbeddingClient(embedding_http),
                    build_model_router(model_http, settings, options), options,
                )
            yield

    app = FastAPI(title="Azure Local PDF Assistant", lifespan=lifespan)
    csrf_token = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method in {"POST", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return Response("Cross-origin requests are not allowed.", status_code=403)
            if not secrets.compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf_token.encode("utf-8"),
            ):
                return Response("Invalid request token. Reload the page.", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    def session_id(request: Request, response: Response) -> str:
        value = request.cookies.get("assistant_session")
        try:
            if value is None:
                raise ValueError
            UUID(value)
        except ValueError:
            value = str(uuid4())
            response.set_cookie(
                "assistant_session", value, httponly=True, samesite="strict",
                secure=request.url.scheme == "https",
            )
        return value

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(UnknownScenario)
    async def unknown_scenario(request: Request, exc: UnknownScenario):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ScenarioError)
    async def scenario_error(request: Request, exc: ScenarioError):
        from fastapi.responses import JSONResponse
        logger.error("Scenario configuration failed: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(UnknownModel)
    async def unknown_model(request: Request, exc: UnknownModel):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/models")
    def models() -> dict:
        available = app.state.service.models
        return {
            "models": [option.public() for option in available],
            "default_model": available[0].id if available else None,
        }

    @app.get("/api/scenarios")
    def scenarios() -> dict:
        try:
            available = list_scenarios(app.state.service.database)
        except (httpx.HTTPError, UnexpectedResponse, ResponseHandlingException) as exc:
            logger.exception("Scenario catalog lookup failed")
            raise HTTPException(503, "Scenario catalog is unavailable.") from exc
        return {"scenarios": [scenario.public() for scenario in available], "default_scenario": DEFAULT_SCENARIO}

    @app.get("/api/status")
    def status(scenario: str = Query(default=DEFAULT_SCENARIO, pattern=SLUG_PATTERN, max_length=40)) -> dict:
        try:
            database = app.state.service.database
            selected = get_scenario(database, scenario)
            collection = selected.collection
            count = database.count(collection, exact=True).count if collection else 0
        except (httpx.HTTPError, UnexpectedResponse, ResponseHandlingException) as exc:
            logger.exception("Vector database status failed")
            raise HTTPException(503, "Vector database is unavailable.") from exc
        return {
            "indexed": collection is not None, "chunks": count,
            "model": settings.model_id, "csrf_token": csrf_token,
            "scenario": selected.public(),
        }

    @app.post("/api/chat")
    def chat(body: ChatRequest, request: Request, response: Response) -> dict:
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Question must not be blank.")
        identity = session_id(request, response)
        try:
            return app.state.service.answer(identity, question, body.scenario, body.model)
        except DependencyError as exc:
            logger.warning("Assistant dependency failed: %s", exc)
            raise HTTPException(503, str(exc)) from exc
        except (httpx.HTTPError, UnexpectedResponse, ResponseHandlingException) as exc:
            logger.exception("Vector retrieval failed")
            raise HTTPException(503, "Vector retrieval failed. Check the local services.") from exc

    @app.post("/api/reset")
    def reset(
        request: Request, response: Response,
        scenario: str = Query(default=DEFAULT_SCENARIO, pattern=SLUG_PATTERN, max_length=40),
    ) -> dict[str, str]:
        try:
            get_scenario(app.state.service.database, scenario)
        except (httpx.HTTPError, UnexpectedResponse, ResponseHandlingException) as exc:
            logger.exception("Scenario reset lookup failed")
            raise HTTPException(503, "Scenario catalog is unavailable.") from exc
        app.state.service.reset(session_id(request, response), scenario)
        return {"status": "reset"}

    static_dir = Path(settings.static_dir)
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    return app


app = create_app()
