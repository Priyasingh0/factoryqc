from .context import RequestContext, reset_request_context, set_request_context


class AuditContextMiddleware:
    """Stores IP + device serial for the request so audit entries can use them.

    The IP comes from REMOTE_ADDR only (not spoofable via X-Forwarded-For).
    The device serial comes from the X-Device-Serial header the tablet sends;
    it is informational and should be set by the tablet/MDM in production.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ctx = RequestContext(
            ip_address=request.META.get("REMOTE_ADDR") or None,
            device_serial=request.META.get("HTTP_X_DEVICE_SERIAL", "")[:64],
        )
        token = set_request_context(ctx)
        try:
            return self.get_response(request)
        finally:
            reset_request_context(token)
