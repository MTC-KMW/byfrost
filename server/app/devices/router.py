"""Devices router - registration, listing, heartbeat.

Endpoints (implemented in Task 1.4):
    POST   /devices/register              - Register device, return device_token
    GET    /devices                       - List user's devices
    DELETE /devices/{device_id}           - Unregister device
    POST   /devices/{device_id}/heartbeat - Update addresses, last_seen
"""

from fastapi import APIRouter

router = APIRouter()
