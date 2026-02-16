"""Pairing router - certificate generation, credential distribution, rotation.

Endpoints (implemented in Tasks 1.8-1.10):
    POST /pair/initiate                            - Create pairing
    GET  /pair/{pairing_id}/credentials/worker     - Fetch worker certs
    GET  /pair/{pairing_id}/credentials/controller - Fetch controller certs
    GET  /pair/{pairing_id}/addresses              - Get paired device addresses
    POST /pair/{pairing_id}/rotate                 - Rotate HMAC secret
    POST /pair/{pairing_id}/revoke                 - Revoke pairing
"""

from fastapi import APIRouter

router = APIRouter()
