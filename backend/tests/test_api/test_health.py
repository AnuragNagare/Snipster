"""Basic smoke tests for the FastAPI app."""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_upload_without_ownership_fails():
    """Uploading without ownership confirmation should return 400."""
    import io
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/videos/upload",
            data={"ownership_confirmed": "false"},
            files={"file": ("test.mp4", io.BytesIO(b"fake video"), "video/mp4")},
        )
    assert response.status_code == 400
    assert "rights" in response.json()["detail"].lower()
