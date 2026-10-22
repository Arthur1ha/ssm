import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from cloud.space.people.provider import FaceProviderNotConfigured
from cloud.space.people.tencent import create_tencent_face_provider_from_env

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TENCENT_FACE_LIVE") != "1",
    reason="set RUN_TENCENT_FACE_LIVE=1 to run Tencent IAI smoke tests",
)


@pytest.mark.asyncio
async def test_live_group_is_face_model_3():
    try:
        provider = create_tencent_face_provider_from_env()
    except FaceProviderNotConfigured as exc:
        pytest.skip(str(exc))
    info = await provider.check_group()
    assert info.face_model_version == "3.0"
