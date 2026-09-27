import json
import logging
import os
from typing import Optional
import requests
from mitmproxy import ctx, http

logger = logging.getLogger("yt_dlp_flaresolverr")

FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191/v1")
FLARESOLVERR_PROXY = os.getenv("FLARESOLVERR_PROXY", None)


class FlareSolverrAddon:
    def __init__(self):
        self.flaresolverr_url = FLARESOLVERR_URL
        self.proxy_url = FLARESOLVERR_PROXY
        self.solved_cookies = {}  # domain -> cookie string
        self.user_agent: Optional[str] = None

    def request(self, flow: http.HTTPFlow):
        # Inject previously solved cookies and user-agent if available for this host
        host = flow.request.host
        if host in self.solved_cookies:
            existing_cookie = flow.request.headers.get("cookie", "")
            solved_cookie = self.solved_cookies[host]
            combined = f"{existing_cookie}; {solved_cookie}" if existing_cookie else solved_cookie
            flow.request.headers["cookie"] = combined

        if self.user_agent:
            flow.request.headers["user-agent"] = self.user_agent

    def response(self, flow: http.HTTPFlow):
        # Trigger on 403 Forbidden or 503 Service Unavailable
        if flow.response and flow.response.status_code in [403, 503]:
            try:
                body = flow.response.text.lower()
            except Exception:
                body = ""

            is_blocked = (
                "cloudflare" in body
                or "just a moment" in body
                or "challenge" in body
                or "turnstile" in body
                or "forbidden" in body
                or "cf-browser-verification" in body
            )

            if is_blocked:
                ctx.log.info(f"🛑 FlareSolverr addon: Block detected on {flow.request.url} (Status: {flow.response.status_code})")
                self.solve_challenge(flow)

    def solve_challenge(self, flow: http.HTTPFlow):
        original_url = flow.request.url
        ctx.log.info(f"🚀 Asking FlareSolverr at {self.flaresolverr_url} to solve: {original_url}")

        payload = {
            "cmd": "request.get",
            "url": original_url,
            "maxTimeout": 60000,
        }

        if self.proxy_url:
            payload["proxy"] = {"url": self.proxy_url}

        try:
            res = requests.post(self.flaresolverr_url, json=payload, timeout=70)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "ok":
                    solution = data.get("solution", {})
                    cookies = solution.get("cookies", [])
                    user_agent = solution.get("userAgent")
                    response_text = solution.get("response", "")

                    if user_agent:
                        self.user_agent = user_agent

                    cookie_pairs = []
                    for c in cookies:
                        cookie_pairs.append(f"{c['name']}={c['value']}")

                    if cookie_pairs:
                        cookie_str = "; ".join(cookie_pairs)
                        self.solved_cookies[flow.request.host] = cookie_str

                    ctx.log.info(f"✅ FlareSolverr solved challenge for {flow.request.host}! Injected {len(cookies)} cookies.")

                    # Replace response in flow with solved response
                    flow.response.status_code = solution.get("status", 200)
                    if response_text:
                        flow.response.text = response_text
                        flow.response.headers["content-type"] = "text/html; charset=utf-8"
                else:
                    ctx.log.warn(f"FlareSolverr returned status: {data.get('status')} - {data.get('message')}")
            else:
                ctx.log.warn(f"FlareSolverr request failed with HTTP {res.status_code}")
        except Exception as e:
            ctx.log.error(f"Error communicating with FlareSolverr: {e}")


addons = [FlareSolverrAddon()]
