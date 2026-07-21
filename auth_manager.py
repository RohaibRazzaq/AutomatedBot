"""
AuthManager - Dynamically bypasses Cloudflare on any network.

Instead of hardcoding cookies (which are IP-bound and expire), this module
launches a real browser via SeleniumBase UC (Undetected Chromedriver) mode,
solves Cloudflare's challenge on whatever network the bot is connected to,
and executes GraphQL queries from within the browser itself.

This solves:
  - IP-bound Cloudflare cookies (cf_clearance generated fresh on current IP)
  - Corporate SSL/TLS inspection (browser uses system certificate store)
  - Corporate proxy settings (browser uses system proxy automatically)
  - TLS fingerprinting (request comes from real Chrome, not Python requests)
"""

import os
import json
import time
import threading

from seleniumbase import SB

BOT_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "BotProfile")
UPWORK_SEARCH_URL = "https://www.upwork.com/nx/search/jobs/?q={}"
SESSION_REFRESH_INTERVAL = 1200    # Refresh Cloudflare clearance every 20 minutes
BROWSER_RESTART_INTERVAL = 14400   # Restart browser every 4 hours (memory leak fix)
TOKEN_REFRESH_INTERVAL = 39600     # Force token refresh every 11 hours (12-hour token lifetime)


class AuthManager:
    """Manages a persistent browser session for bypassing Cloudflare on Upwork."""

    def __init__(self, headless=True):
        self._gen = None
        self.sb = None
        self._lock = threading.Lock()
        self._last_refresh = 0
        self._last_browser_start = 0
        self._headless = headless
        self._started = False

    def start(self):
        """Launch the browser and navigate to Upwork."""
        print("[AuthManager] Starting browser...")

        if self._headless:
            # Chrome's new headless mode - harder for Cloudflare to detect
            self._gen = SB(
                uc=True,
                headless2=True,
                user_data_dir=BOT_PROFILE_PATH,
            )
        else:
            # Headful mode with window positioned off-screen (most reliable
            # for Cloudflare bypass, since uc_gui_click_captcha can interact
            # with the window if a Turnstile captcha appears)
            self._gen = SB(
                uc=True,
                headless2=False,
                user_data_dir=BOT_PROFILE_PATH,
                window_position="-32000,-32000",
                window_size="800,600",
            )

        # SB is a @contextmanager, so use __enter__ to start the browser
        # (calls sb.setUp() internally) and __exit__ to tear it down later.
        self.sb = self._gen.__enter__()
        self._started = True

        self._navigate_and_solve_cf("python")
        self._last_browser_start = time.time()
        print("[AuthManager] Browser ready. Cloudflare bypassed.")

    def _navigate_and_solve_cf(self, keyword):
        """Navigate to Upwork and solve Cloudflare challenge."""
        url = UPWORK_SEARCH_URL.format(keyword)
        print(f"[AuthManager] Navigating to: {url}")

        # uc_open_with_reconnect disconnects the WebDriver during page load,
        # allowing Cloudflare's JS challenge to run without detecting automation
        self.sb.uc_open_with_reconnect(url, reconnect_time=6)

        # Handle Cloudflare Turnstile captcha if present.
        # (Only works in headful mode; in headless2 this is a silent no-op)
        try:
            self.sb.uc_gui_click_captcha()
        except Exception:
            pass

        # Wait for the page to settle after Cloudflare challenge
        self.sb.sleep(3)
        self._last_refresh = time.time()

    def _restart_browser(self):
        """Restart the browser to clear memory leaks and get fresh tokens."""
        print("[AuthManager] Restarting browser for maintenance...")
        self.stop()
        time.sleep(2)  # Give Chrome time to fully close
        self.start()
        print("[AuthManager] Browser restarted successfully.")

    def _ensure_session(self):
        """Proactively refresh Cloudflare clearance, restart browser, and refresh tokens.

        Three layered checks:
          1. Every 4 hours  -> restart browser (kills Chrome memory leaks, gets fresh cookies/tokens)
          2. Every 11 hours -> force restart (safety net for 12-hour token lifetime, in case #1 failed)
          3. Every 20 min   -> re-navigate to Upwork (refreshes Cloudflare cf_clearance)

        All operations are wrapped in try/except so a failure here never crashes the caller.
        """
        now = time.time()

        # 1. Every 4 hours: full browser restart (memory + fresh state)
        if now - self._last_browser_start > BROWSER_RESTART_INTERVAL:
            try:
                self._restart_browser()
                return
            except Exception as e:
                print(f"[AuthManager] 4-hour browser restart failed: {e}")
                # Fall through to session refresh as fallback

        # 2. Every 11 hours: force restart for token refresh (safety net)
        if now - self._last_browser_start > TOKEN_REFRESH_INTERVAL:
            try:
                print("[AuthManager] 11-hour token refresh: forcing browser restart...")
                self._restart_browser()
                return
            except Exception as e:
                print(f"[AuthManager] 11-hour token refresh failed: {e}")

        # 3. Every 20 minutes: refresh Cloudflare clearance by re-navigating
        if now - self._last_refresh > SESSION_REFRESH_INTERVAL:
            print("[AuthManager] Session stale, refreshing Cloudflare clearance...")
            try:
                self._navigate_and_solve_cf("python")
            except Exception as e:
                print(f"[AuthManager] Session refresh failed: {e}")

    def _is_driver_alive(self):
        """Check if the WebDriver session is still alive."""
        if not self.sb or not self.sb.driver:
            return False
        try:
            self.sb.driver.current_url
            return True
        except Exception:
            return False

    def _get_auth_token(self):
        """Extract the auth token from Upwork cookies using WebDriver's API.

        WebDriver's get_cookies() can access ALL cookies including HttpOnly ones,
        unlike document.cookie which only sees non-HttpOnly cookies.
        Returns the token string or empty string if not found.
        """
        try:
            cookies = self.sb.driver.get_cookies()
            for cookie in cookies:
                name = cookie.get('name', '')
                if name == 'UniversalSearchNuxt_vt':
                    return cookie.get('value', '')
            # Token cookie not found — log available cookie names for diagnostics
            names = [c.get('name', '?') for c in cookies]
            print(f"[AuthManager] Auth token cookie not found. Available cookies ({len(names)}): {', '.join(names[:20])}")
            return ''
        except Exception as e:
            print(f"[AuthManager] Failed to read cookies: {e}")
            return ''

    def execute_graphql(self, json_data):
        """
        Execute a GraphQL query via the browser's fetch API.

        The request is made from within the browser itself, so all Cloudflare
        cookies, proxy settings, and SSL certificates are handled automatically.
        No hardcoded cookies or headers needed.

        Args:
            json_data: dict with 'query' and 'variables' keys.

        Returns:
            Parsed JSON response dict, or None on error.
        """
        if not self._started or not self._is_driver_alive():
            print("[AuthManager] Browser not running. Attempting to start...")
            try:
                self.start()
            except Exception as e:
                print(f"[AuthManager] Failed to start browser: {e}")
                return None

        js_code = """
        var callback = arguments[arguments.length - 1];
        var payload = arguments[0];
        var authToken = arguments[1] || '';

        var headers = {
            'content-type': 'application/json',
            'accept': '*/*',
            'x-upwork-accept-language': 'en-US'
        };
        if (authToken) {
            headers['authorization'] = 'Bearer ' + authToken;
        }

        fetch('/api/graphql/v1?alias=visitorJobSearch', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify(payload),
            credentials: 'include'
        }).then(function(r) {
            return r.text();
        }).then(function(t) {
            callback(t);
        }).catch(function(e) {
            callback(JSON.stringify({error: e.message}));
        });
        """

        with self._lock:
            self._ensure_session()

            # Verify we're still on upwork.com
            try:
                current_url = self.sb.get_current_url()
            except Exception:
                current_url = ""

            if "upwork.com" not in str(current_url):
                print("[AuthManager] Not on Upwork, navigating back...")
                self._navigate_and_solve_cf("python")

            auth_token = self._get_auth_token()

            try:
                self.sb.driver.set_script_timeout(30)
                result = self.sb.driver.execute_async_script(js_code, json_data, auth_token)
            except Exception as e:
                print(f"[AuthManager] GraphQL execution failed: {e}")
                print("[AuthManager] Refreshing session and retrying...")
                try:
                    self._navigate_and_solve_cf("python")
                    self.sb.sleep(2)  # Extra wait for cookies to settle
                    auth_token = self._get_auth_token()
                    self.sb.driver.set_script_timeout(30)
                    result = self.sb.driver.execute_async_script(js_code, json_data, auth_token)
                except Exception as e2:
                    print(f"[AuthManager] Retry also failed: {e2}")
                    return None

        if result is None:
            print("[AuthManager] Empty response from browser")
            return None

        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as e:
            print(f"[AuthManager] Failed to parse GraphQL response: {e}")
            print(f"[AuthManager] Raw response: {str(result)[:300]}")
            return None

        # Detect auth failure in the response and retry once
        msg = parsed.get('message', '') if isinstance(parsed, dict) else ''
        if 'authentication' in msg.lower() or 'auth' in msg.lower():
            print("[AuthManager] Auth failure detected in response, refreshing and retrying...")
            try:
                self._navigate_and_solve_cf("python")
                self.sb.sleep(2)
                new_token = self._get_auth_token()
                self.sb.driver.set_script_timeout(30)
                result2 = self.sb.driver.execute_async_script(js_code, json_data, new_token)
                if result2:
                    return json.loads(result2)
            except Exception as e:
                print(f"[AuthManager] Auth-retry also failed: {e}")
            return parsed  # Return the original error response

        return parsed

    def stop(self):
        """Close the browser and clean up."""
        print("[AuthManager] Stopping browser...")
        if self._gen:
            try:
                self._gen.__exit__(None, None, None)  # calls sb.tearDown()
            except Exception as e:
                print(f"[AuthManager] Error during cleanup: {e}")
            self._gen = None
            self.sb = None
            self._started = False
        print("[AuthManager] Browser stopped.")
