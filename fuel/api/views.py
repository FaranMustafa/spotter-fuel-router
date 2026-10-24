from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness probe: returns 200 with a static payload."""

    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})
