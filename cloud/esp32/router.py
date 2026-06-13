"""cloud.esp32.router — ESP32 桌面智能体 HTTP 端点。"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/esp32", tags=["esp32"])
