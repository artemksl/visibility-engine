"""Isolated local DOM browser fixtures with network denied."""
from __future__ import annotations
import asyncio
import importlib
import importlib.metadata
from contextlib import asynccontextmanager
from pathlib import Path
import uuid
import pytest
from visibility_runner.browser.profile_matching import browser_context_id, browser_target_id
from visibility_runner.browser.tabs import OwnedTab, OwnedTabRegistry, TabSessionIdentity
from visibility_runner.websites.chat_actions import PageSubmission, strict_page_identity
from visibility_runner.websites.chatgpt import CHATGPT_SPEC
from visibility_runner.websites.gemini import GEMINI_SPEC


PROVIDERS = {"chatgpt": CHATGPT_SPEC, "gemini": GEMINI_SPEC}


DOCUMENT_PREFIX = """<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
  script-src 'unsafe-inline'; style-src 'unsafe-inline'">
<style>
  model-response, user-query, rich-textarea { display: block; }
  [contenteditable] { min-height: 48px; border: 1px solid; white-space: pre-wrap; }
</style>"""


@pytest.fixture(scope="module")
def isolated_browser(request):
    config = request.config if request is not None else None
    driver_name = config.getoption("--browser-driver") if config is not None else "playwright"
    requested_executable = config.getoption("--browser-executable") if config is not None else None
    expected_version = config.getoption("--expected-browser-version") if config is not None else None
    expected_driver_version = config.getoption("--browser-driver-version") if config is not None else None
    headless = not config.getoption("--browser-headed") if config is not None else True
    if driver_name not in ("playwright", "patchright"):
        pytest.fail("unsupported isolated browser driver: " + driver_name)
    if (driver_name == "patchright" or requested_executable or expected_version or expected_driver_version) and not (
        requested_executable and expected_version and expected_driver_version
    ):
        pytest.fail("the browser matrix requires an exact driver version, executable and expected browser version")
    try:
        driver_version = importlib.metadata.version(driver_name)
        if expected_driver_version and driver_version != expected_driver_version:
            pytest.fail(f"driver version mismatch: expected {expected_driver_version}, got {driver_version}")
        api = importlib.import_module(driver_name + ".async_api")
        require_guard_capability(api.BrowserContext)
    except (ModuleNotFoundError, importlib.metadata.PackageNotFoundError):
        pytest.fail("Python " + driver_name + " is required for local_browser verification")

    loop = asyncio.new_event_loop()
    manager = api.async_playwright()
    browser = None
    entered = False
    try:
        playwright = loop.run_until_complete(manager.__aenter__())
        entered = True
        candidates = (Path(requested_executable),) if requested_executable else (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path(playwright.chromium.executable_path),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/google-chrome"),
        )
        executable = next(
            (
                candidate
                for candidate in candidates
                if candidate.exists()
            ),
            None,
        )
        if executable is None:
            pytest.fail("a provisioned Chromium is required for local_browser verification")
        browser = loop.run_until_complete(
            playwright.chromium.launch(headless=headless, executable_path=str(executable))
        )
        if expected_version and browser.version != expected_version:
            pytest.fail(f"browser version mismatch: expected {expected_version}, got {browser.version}")
        yield loop, GuardedBrowser(browser, driver_name=driver_name, driver_version=driver_version,
                                   executable=str(executable), headless=headless)
    finally:
        try:
            if browser is not None:
                loop.run_until_complete(browser.close())
        finally:
            try:
                if entered:
                    loop.run_until_complete(manager.__aexit__(None, None, None))
            finally:
                loop.close()


def run_dom_case(isolated_browser, markup, check):
    loop, browser = isolated_browser

    async def exercise():
        # Effectful fixtures need an admitted HTTP origin, just like borrowed
        # production pages. The guarded factory fulfills this document locally
        # while all unlisted network requests remain denied.
        url = "https://fixture.test/isolated-dom"
        async with browser.context({url: DOCUMENT_PREFIX + markup}) as guarded:
            page = await guarded.context.new_page()
            await page.goto(url)
            return await check(page)

    return loop.run_until_complete(exercise())


async def evaluate_main_world(page, expression, arg=None):
    """Read synthetic fixture globals without changing production DOM evaluation.

    Patchright otherwise evaluates in an isolated world. Only fixture calls
    requiring a page-script global use this helper; locators keep their defaults.
    """
    options = {"isolated_context": False} if type(page).__module__.startswith("patchright.") else {}
    return await page.evaluate(expression, arg, **options)


async def bound_submission(page, platform, *, profile_id="offline-fixture", directory="Synthetic isolated profile", spec=None):
    context_id = await browser_context_id(page)
    target_id = await browser_target_id(page)
    assert context_id and target_id
    registry = OwnedTabRegistry()
    registry.add(
        OwnedTab(
            page=page,
            identity=TabSessionIdentity(
                profile_id=profile_id,
                chrome_directory=directory,
                provider=platform,
                browser_context_reference=context_id,
                tab_reference=f"visibility-runner-owned-tab:{uuid.uuid4().hex}",
                browser_target_reference=f"cdp-target:{target_id}",
                conversation_reference=page.url,
            ),
            marker_url=page.url,
        )
    )
    identity = await strict_page_identity(
        page, profile_id=profile_id, chrome_directory=directory
    )
    return PageSubmission(
        page=page,
        profile_id=profile_id,
        chrome_directory=directory,
        observation_id="synthetic-access-guard",
        spec=spec if spec is not None else PROVIDERS[platform],
        identity=identity,
    )


def require_guard_capability(context_type):
    if not callable(getattr(context_type, "route_web_socket", None)):
        raise RuntimeError("isolated browser tests require route_web_socket support")


class GuardedContext:
    def __init__(self, context):
        self.context = context
        self.denied = []
        self.fulfilled = []


class GuardedBrowser:
    """Test capability: all contexts are guarded before their first page.

    This prevents fixture provider requests, not arbitrary native-process
    egress. The runtime owns no persistent profile and exposes no raw browser.
    """
    def __init__(self, browser, *, driver_name="playwright", driver_version=None, executable=None, headless=True):
        self._browser = browser
        self.driver_name = driver_name
        self.driver_version = driver_version
        self.executable = executable
        self.version = getattr(browser, "version", None)
        self.headless = headless
        self.last_context = None

    @property
    def contexts(self):
        return self._browser.contexts

    async def new_browser_cdp_session(self):
        return await self._browser.new_browser_cdp_session()

    async def new_context(self, **kwargs):
        raise RuntimeError("use the guarded context factory")

    async def new_page(self, **kwargs):
        raise RuntimeError("implicit browser contexts are prohibited")

    @asynccontextmanager
    async def context(self, documents=None, *, allow_denials=False):
        documents = dict(documents or {})
        context = await self._browser.new_context(service_workers="block", accept_downloads=False)
        guarded = self.last_context = GuardedContext(context)
        failed = False
        try:
            await context.set_offline(True)
            async def dispatch(route):
                request = route.request
                try:
                    top_level = request.is_navigation_request() and request.frame.parent_frame is None
                except Exception:  # Early popup requests may not yet expose a frame.
                    top_level = False
                if request.method == "GET" and request.resource_type == "document" and top_level and request.url in documents:
                    guarded.fulfilled.append(request.url)
                    await route.fulfill(status=200, content_type="text/html", body=documents[request.url]() if callable(documents[request.url]) else documents[request.url])
                else:
                    guarded.denied.append((request.method, request.resource_type, request.url))
                    await route.abort()
            async def deny_websocket(websocket):
                guarded.denied.append(("WEBSOCKET", "websocket", websocket.url))
                await websocket.close()
            await context.route("**/*", dispatch)
            await context.route_web_socket("**/*", deny_websocket)
            yield guarded
        except BaseException:
            failed = True
            raise
        finally:
            await context.close()
            if not failed and not allow_denials and guarded.denied:
                raise AssertionError(f"unexpected synthetic browser requests: {guarded.denied!r}")
