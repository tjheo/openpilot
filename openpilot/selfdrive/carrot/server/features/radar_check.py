"""Same-origin, asynchronous RX-only diagnostic on the existing Carrot server."""
import asyncio
from urllib.parse import urlsplit

from aiohttp import web

from ..services.radar_check import RadarCheck


async def start(request):
  # Existing local UI has no account auth. Do not allow cross-origin browser starts.
  origin = request.headers.get("Origin")
  if origin and urlsplit(origin).netloc != request.host:
    return web.json_response({"ok": False, "error": "same-origin request required"}, status=403)
  if request.headers.get("X-Carrot-Radar-Check") != "1":
    return web.json_response({"ok": False, "error": "diagnostic request header required"}, status=403)
  try:
    body = await request.json()
    if not isinstance(body, dict):
      return web.json_response({"ok": False, "error": "invalid diagnostic request"}, status=400)
    bus = body.get("bus", "auto")
    if bus != "auto" and (type(bus) is not int or bus not in (0, 1, 2)):
      raise ValueError("invalid CAN RX bus")
    job = request.app["radar_check"].start(bus, body.get("mode", "scc"))
  except ValueError as exc:
    return web.json_response({"ok": False, "error": str(exc)}, status=400)
  except Exception:  # device imports/settings can fail; do not expose details
    return web.json_response({"ok": False, "error": "차량 설정 확인 실패"}, status=503)
  return web.json_response({"ok": True, "job": job}, status=202)


async def status(request):
  return web.json_response({"ok": True, "job": request.app["radar_check"].snapshot()}, headers={"Cache-Control": "no-store"})


async def cleanup(app):
  await asyncio.to_thread(app["radar_check"].close)


def register(app):
  app["radar_check"] = RadarCheck()
  app.router.add_post("/api/radar_check/start", start)
  app.router.add_get("/api/radar_check/status", status)
  app.on_cleanup.append(cleanup)
