"""Test fixtures.

Each test module gets its own temporary database and artifact directory, so
tests never see each other's campaigns and a failing test cannot leave state that
makes the next one pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import pytest

from app import config
from app.domain.enums import ProviderMode, SizeStrategy
from app.graph.context import RunContext
from app.providers.fixtures import FixtureImageProvider, FixtureLLMProvider
from app.providers.limits import ConcurrencyGate, TokenBucketLimiter
from app.providers.registry import FailureInjector, ProviderBundle
from app.providers.capabilities import resolve_capabilities
from app.storage import db as db_module
from app.storage.artifacts import ArtifactStore
from app.storage.db import init_db
from app.storage.repo import CampaignRepository


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated settings pointing at a per-test sqlite file and artifact dir."""
    monkeypatch.setenv("PROVIDER_MODE", "fixture")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite'}")
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("FAILURE_INJECT", "")
    monkeypatch.setenv("MAX_RETRIES", "3")
    monkeypatch.setenv("RETRY_BASE_DELAY_S", "0.01")

    config.get_settings.cache_clear()
    resolved = config.get_settings()
    resolved.artifact_dir.mkdir(parents=True, exist_ok=True)
    yield resolved
    config.get_settings.cache_clear()


@pytest.fixture
async def database(settings):
    """A fresh schema per test, with the engine disposed afterwards."""
    await db_module.dispose_engine()
    await init_db()
    yield
    await db_module.dispose_engine()


@pytest.fixture
def artifacts(settings) -> ArtifactStore:
    return ArtifactStore(settings.artifact_dir)


class CountingLLM(FixtureLLMProvider):
    """Fixture model that records how many times it was called.

    The counters are the evidence for the stage-reuse test: a retry that truly
    reuses upstream output must leave these unchanged.
    """

    def __init__(self) -> None:
        super().__init__()
        self.chat_calls = 0
        self.complete_calls = 0

    async def chat(self, **kwargs):
        self.chat_calls += 1
        return await super().chat(**kwargs)

    async def complete(self, **kwargs):
        self.complete_calls += 1
        return await super().complete(**kwargs)


class CountingImage(FixtureImageProvider):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.generate_calls = 0
        self.edit_calls = 0

    async def generate(self, **kwargs):
        self.generate_calls += 1
        return await super().generate(**kwargs)

    async def edit(self, **kwargs):
        self.edit_calls += 1
        return await super().edit(**kwargs)

    @property
    def total_calls(self) -> int:
        return self.generate_calls + self.edit_calls


@pytest.fixture
def providers(settings) -> ProviderBundle:
    llm = CountingLLM()
    image = CountingImage()
    return ProviderBundle(
        llm=llm,
        image=image,
        capabilities=resolve_capabilities("gpt-image-2.5-sunburst"),
        size_strategy=SizeStrategy.NATIVE,
        image_gate=ConcurrencyGate(
            # Effectively unbounded in tests: the limiter is exercised in its own
            # unit test, and throttling here would only make the suite slow.
            limiter=TokenBucketLimiter(rate_per_minute=120, name="test"),
            max_concurrent=2,
        ),
        injector=FailureInjector({}),
        mode=ProviderMode.FIXTURE,
        image_model="gpt-image-2.5-sunburst",
    )


@pytest.fixture
async def campaign_id(database, settings) -> str:
    from app.storage.db import session_scope

    cid = f"c_{secrets.token_hex(8)}"
    async with session_scope() as session:
        await CampaignRepository(session).create(
            campaign_id=cid,
            title="Test campaign",
            brief=sample_brief(),
            size_strategy="native",
            image_model="gpt-image-2.5-sunburst",
            provider_mode="fixture",
        )
    return cid


@pytest.fixture
def context(settings, providers, artifacts, campaign_id) -> RunContext:
    return RunContext(
        campaign_id=campaign_id,
        run_id=f"{campaign_id}:r1",
        settings=settings,
        providers=providers,
        artifacts=artifacts,
        worker_epoch="test-epoch",
    )


def sample_brief() -> dict:
    return {
        "product_name": "Meridian Daily Protein",
        "product_description": (
            "A powdered protein supplement sold in a 1kg matte charcoal tub with a "
            "wide screw lid. Each serving is a single scoop mixed with water or milk."
        ),
        "target_audience": "Adults aged 25-40 who train at a commercial gym.",
        "campaign_objective": "introduce_product",
        "tone": "practical, energetic",
        "call_to_action": "Explore the range",
        "verified_claims": [],
        "reference_image_id": None,
    }


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.get_event_loop_policy()
