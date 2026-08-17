"""Tests for Android WebView-backed web_search/web_fetch tools."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from jenny.agent.tools import android_web
from jenny.agent.tools.android_web import (
    AndroidWebFetchTool,
    AndroidWebSearchTool,
    _looks_like_captcha,
    _looks_like_consent,
    _parse_search_response,
)
from jenny.config.schema import ToolsConfig
from jenny.config.tool_schemas import AndroidWebFetchConfig, AndroidWebSearchConfig

RESULTS = [
    {"title": "Python", "url": "https://python.org", "snippet": "Official site"},
    {"title": "Docs", "url": "https://docs.python.org", "snippet": "Documentation"},
]


@pytest.fixture(autouse=True)
def _clean_bridge_state():
    """Ensure the module-level bridge cache and lock never leak across tests.

    Recreating the lock (not just the bridge instance) matters because each
    test runs on its own event loop, and asyncio.Lock binds to the loop it's
    first awaited on.
    """
    android_web.reset_android_web_state()
    yield
    android_web.reset_android_web_state()


class TestLooksLikeCaptcha:
    @pytest.mark.parametrize(
        "text",
        [
            "please solve this CAPTCHA to continue",
            "verify you are human before proceeding",
            "are you a robot?",
            # Real DuckDuckGo challenge wording (regression: previously missed).
            "Unfortunately, bots use DuckDuckGo too. Please complete the challenge.",
            "Select all squares containing a duck.",
            # Bing/Microsoft block-page wording.
            "Pardon our interruption. We detected unusual traffic.",
            "Your request has been blocked due to automated queries.",
            # Structural markers.
            '<form id="challenge-form" method="post">',
            '<form id="captcha-form">',
            "https://www.google.com/sorry/index",
        ],
    )
    def test_detects_block_pages(self, text):
        assert _looks_like_captcha(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Results for: python asyncio\n1. asyncio docs\n   https://docs.python.org",
            "<html><body><ol><li>Ordinary search result</li></ol></body></html>",
        ],
    )
    def test_no_false_positive_on_ordinary_pages(self, text):
        assert _looks_like_captcha(text) is False


class TestLooksLikeConsent:
    @pytest.mark.parametrize(
        "text",
        [
            # consent.bing.com / pagine di consenso cookie reali.
            "https://consent.bing.com/choose-your-preferences",
            "We use cookies to improve your experience. Manage cookie preferences.",
            "Choose your preferences to continue.",
            "Your privacy settings: accept all cookies to continue.",
            "We're updating our terms. Please review and consent.",
        ],
    )
    def test_detects_consent_pages(self, text):
        assert _looks_like_consent(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            # Una SERP vera vuota NON deve sembrare una pagina di consenso
            # (regressione: footer "Privacy" comuni non devono innescare).
            "There are no results for your search. Privacy | Legal | About",
            "Results for: python asyncio\n1. asyncio docs\n   https://docs.python.org",
        ],
    )
    def test_no_false_positive_on_ordinary_pages(self, text):
        assert _looks_like_consent(text) is False


class TestParseSearchResponse:
    def test_results_object(self):
        raw = json.dumps({"results": RESULTS, "pageText": ""})
        assert _parse_search_response(raw) == RESULTS

    def test_double_encoded_results_object(self):
        # evaluateJavascript JSON-encodes the JS string return value, so the
        # real payload arrives as a JSON string containing JSON (Bug C).
        raw = json.dumps(json.dumps({"results": RESULTS, "pageText": ""}))
        assert _parse_search_response(raw) == RESULTS

    def test_empty_results_with_captcha_page_text_raises(self):
        raw = json.dumps(
            {
                "results": [],
                "pageText": "Unfortunately, bots use DuckDuckGo too. Select all squares.",
            }
        )
        with pytest.raises(ValueError, match="CAPTCHA"):
            _parse_search_response(raw)

    def test_empty_results_with_consent_page_text_raises_consent(self):
        # consent.bing.com: la SERP non c'è, ma la diagnosi deve essere
        # "consenso", non "CAPTCHA" — e non un silenzioso "No results".
        raw = json.dumps(
            {
                "results": [],
                "pageText": "consent.bing.com — choose your preferences to continue.",
            }
        )
        with pytest.raises(ValueError, match="consent"):
            _parse_search_response(raw)

    def test_non_json_consent_page_raises_consent(self):
        with pytest.raises(ValueError, match="consent"):
            _parse_search_response("<html>Manage cookie preferences</html>")

    def test_empty_results_with_clean_page_text_returns_empty(self):
        # Genuinely empty SERP must NOT raise — the caller reports
        # "No results for: ..." to the model.
        raw = json.dumps({"results": [], "pageText": "No results matched your search."})
        assert _parse_search_response(raw) == []

    def test_double_encoded_empty_with_captcha_raises(self):
        raw = json.dumps(
            json.dumps({"results": [], "pageText": "verify you are human"})
        )
        with pytest.raises(ValueError, match="CAPTCHA"):
            _parse_search_response(raw)

    def test_error_object_raises_with_message(self):
        raw = json.dumps({"error": "WebView timeout after 30s for https://bing.com"})
        with pytest.raises(ValueError, match="WebView timeout"):
            _parse_search_response(raw)

    def test_double_encoded_error_object_raises(self):
        raw = json.dumps(json.dumps({"error": "WebView HTTP error: 403"}))
        with pytest.raises(ValueError, match="403"):
            _parse_search_response(raw)

    def test_non_json_raises_invalid(self):
        with pytest.raises(ValueError, match="Invalid search response"):
            _parse_search_response("<<<garbage>>>")

    def test_non_json_captcha_page_raises_captcha(self):
        with pytest.raises(ValueError, match="CAPTCHA"):
            _parse_search_response("<html>verify you are human</html>")

    def test_legacy_bare_list_raises(self):
        # Kotlin and Python ship in the same APK, so a bare list is a
        # contract violation, not a compat case.
        with pytest.raises(ValueError, match="Unexpected search response type"):
            _parse_search_response(json.dumps(RESULTS))

    def test_captcha_marker_inside_snippet_is_not_a_false_positive(self):
        # A legitimate search about captchas must not be classified as blocked.
        results = [
            {
                "title": "What is reCAPTCHA?",
                "url": "https://example.com",
                "snippet": "reCAPTCHA verifies you are human.",
            }
        ]
        raw = json.dumps({"results": results, "pageText": ""})
        assert _parse_search_response(raw) == results


class TestSearchEngineSchema:
    """Il validator di `AndroidWebSearchConfig` ricade sul default se il valore
    in config.json è stato scritto male a mano (la route di settings rifiuta
    già con 400; qui si copre la strada che la route non vede)."""

    def test_default_is_bing(self):
        assert AndroidWebSearchConfig().search_engine == "bing"

    def test_invalid_engine_falls_back_to_bing(self):
        cfg = AndroidWebSearchConfig.model_validate({"search_engine": "altavista"})
        assert cfg.search_engine == "bing"

    def test_invalid_engine_camel_case_key_falls_back(self):
        cfg = AndroidWebSearchConfig.model_validate({"searchEngine": "google"})
        assert cfg.search_engine == "bing"

    def test_invalid_engine_type_falls_back(self):
        cfg = AndroidWebSearchConfig.model_validate({"search_engine": 42})
        assert cfg.search_engine == "bing"

    def test_case_insensitive_normalization(self):
        cfg = AndroidWebSearchConfig.model_validate({"search_engine": "BING"})
        assert cfg.search_engine == "bing"


class TestResetAndroidWebState:
    def test_recreates_the_lock(self):
        """A stale asyncio.Lock bound to a previous (now-closed) event loop
        raises 'bound to a different event loop' the moment it's acquired
        again after a gateway restart within the same process — the lock
        must be replaced, not just the cached bridge instance."""
        old_lock = android_web._BRIDGE_LOCK
        android_web.reset_android_web_state()
        assert android_web._BRIDGE_LOCK is not old_lock

    def test_destroys_cached_bridge_instance(self):
        bridge = Mock()
        android_web._BRIDGE_INSTANCE = bridge
        android_web.reset_android_web_state()
        bridge.destroy.assert_called_once()
        assert android_web._BRIDGE_INSTANCE is None


class TestGetBridge:
    async def test_construction_failure_raises_clear_runtime_error(self):
        failing_cls = Mock(side_effect=Exception("ClassNotFoundException: stripped"))
        with patch.object(android_web, "_resolve_bridge_class", return_value=failing_cls):
            with pytest.raises(RuntimeError, match="Failed to construct AgenticSearchBridge"):
                await android_web._get_bridge(context=object())
        assert android_web._BRIDGE_INSTANCE is None

    async def test_instance_is_cached(self):
        instance = Mock(name="bridge")
        bridge_cls = Mock(return_value=instance)
        with patch.object(android_web, "_resolve_bridge_class", return_value=bridge_cls):
            first = await android_web._get_bridge(context=object())
            second = await android_web._get_bridge(context=object())
        assert first is instance and second is instance
        bridge_cls.assert_called_once()


class TestBridgeSearch:
    async def test_success(self):
        bridge = Mock()
        bridge.searchBing.return_value = json.dumps({"results": RESULTS, "pageText": ""})
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            results = await android_web._bridge_search(object(), "python", 5)
        assert results == RESULTS

    async def test_unsupported_engine_raises(self):
        bridge = Mock()
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            with pytest.raises(ValueError, match="Unsupported Android search engine"):
                await android_web._bridge_search(object(), "python", 5, search_engine="google")

    async def test_timeout_propagates(self):
        bridge = Mock()

        def slow_search(query, max_results, timeout):
            import time

            time.sleep(1)
            return "[]"

        bridge.searchBing = slow_search

        async def fake_wait_for(coro, *args, **kwargs):
            coro.close()  # avoid an un-awaited to_thread() coroutine warning
            raise asyncio.TimeoutError

        with patch.object(android_web, "_get_bridge", return_value=bridge):
            with patch.object(android_web.asyncio, "wait_for", fake_wait_for):
                with pytest.raises(asyncio.TimeoutError):
                    await android_web._bridge_search(object(), "python", 5)

    async def test_bridge_exception_propagates(self):
        bridge = Mock()
        bridge.searchBing.side_effect = Exception("JS bridge blew up")
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            with pytest.raises(Exception, match="JS bridge blew up"):
                await android_web._bridge_search(object(), "python", 5)


class TestBridgeFetchDecoding:
    def _bridge(self, raw):
        bridge = Mock()
        bridge.fetchUrl.return_value = raw
        return bridge

    async def test_dict_response_returns_html_and_final_url(self):
        bridge = self._bridge(json.dumps({"html": "<p>hi</p>", "finalUrl": "https://final"}))
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            html, final = await android_web._bridge_fetch(object(), "https://example.com")
        assert html == "<p>hi</p>"
        assert final == "https://final"

    async def test_error_object_raises(self):
        bridge = self._bridge(json.dumps({"error": "boom"}))
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            with pytest.raises(ValueError, match="boom"):
                await android_web._bridge_fetch(object(), "https://example.com")

    async def test_json_null_raises_clean_error_not_attributeerror(self):
        # Regressione: un URL che punta a un binario (es. .../foo.png) non ha
        # un documento HTML, il bridge restituisce JSON `null`. Prima causava
        # `AttributeError: 'NoneType' object has no attribute 'get'`.
        bridge = self._bridge("null")
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            with pytest.raises(ValueError, match="no HTML document"):
                await android_web._bridge_fetch(object(), "https://example.com/x.png")

    async def test_json_string_response_returned_as_content(self):
        bridge = self._bridge(json.dumps("<p>bare</p>"))
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            html, final = await android_web._bridge_fetch(object(), "https://example.com")
        assert html == "<p>bare</p>"
        assert final == "https://example.com"

    async def test_non_json_returned_as_raw_html(self):
        bridge = self._bridge("<html>plain</html>")
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            html, final = await android_web._bridge_fetch(object(), "https://example.com")
        assert html == "<html>plain</html>"
        assert final == "https://example.com"

    @pytest.mark.parametrize("raw", ["", "   \n"])
    async def test_blank_result_raises_instead_of_empty_success(self, raw):
        # `fetchUrl` non replica il controllo che `searchBing` fa sul proprio
        # risultato: `evaluateJavascript` senza valore arriva qui come stringa
        # vuota. Senza guardia diventava un fetch "riuscito" con testo vuoto e
        # status 200 — un fallimento silenzioso.
        bridge = self._bridge(raw)
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            with pytest.raises(ValueError, match="empty result"):
                await android_web._bridge_fetch(object(), "https://example.com")


class TestBridgeCallsAreSerialized:
    """The hidden WebView shares its Chromium renderer with the app's visible
    WebView, so two overlapping bridge calls can starve the visible WebView's
    input dispatch. _BRIDGE_LOCK must cover the whole operation (bridge
    acquisition + the actual blocking call), not just construction."""

    async def test_concurrent_bridge_search_calls_never_overlap(self):
        import time

        active = 0
        max_active = 0

        def slow_search(query, max_results, timeout):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            time.sleep(0.05)
            active -= 1
            return json.dumps({"results": RESULTS, "pageText": ""})

        bridge = Mock()
        bridge.searchBing = slow_search
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            await asyncio.gather(
                android_web._bridge_search(object(), "a", 5),
                android_web._bridge_search(object(), "b", 5),
            )
        assert max_active == 1

    async def test_concurrent_search_and_fetch_never_overlap(self):
        import time

        active = 0
        max_active = 0

        def slow_search(query, max_results, timeout):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            time.sleep(0.05)
            active -= 1
            return json.dumps({"results": RESULTS, "pageText": ""})

        def slow_fetch(url, timeout):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            time.sleep(0.05)
            active -= 1
            return json.dumps({"html": "<p>hi</p>", "finalUrl": url})

        bridge = Mock()
        bridge.searchBing = slow_search
        bridge.fetchUrl = slow_fetch
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            await asyncio.gather(
                android_web._bridge_search(object(), "a", 5),
                android_web._bridge_fetch(object(), "https://example.com"),
            )
        assert max_active == 1


class TestAndroidWebSearchToolExecute:
    def _tool(self) -> AndroidWebSearchTool:
        return AndroidWebSearchTool(android_context=object(), timeout=5)

    async def test_bridge_success_formats_results(self):
        bridge = Mock()
        bridge.searchBing.return_value = json.dumps({"results": RESULTS, "pageText": ""})
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            out = await self._tool().execute("python")
        assert "Results for: python" in out
        assert "https://python.org" in out

    async def test_bridge_empty_results_reports_no_results_without_fallback(self):
        bridge = Mock()
        bridge.searchBing.return_value = json.dumps(
            {"results": [], "pageText": "nothing matched"}
        )
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            out = await self._tool().execute("qzxv nonsense")
        assert "No results for: qzxv nonsense" in out

    async def test_bridge_captcha_reports_error_without_fallback(self):
        bridge = Mock()
        bridge.searchBing.return_value = json.dumps(
            {"results": [], "pageText": "verify you are human"}
        )
        with patch.object(android_web, "_get_bridge", return_value=bridge):
            with patch.object(android_web, "destroy_bridge") as destroy:
                out = await self._tool().execute("python")
        destroy.assert_called_once()
        assert "web_search unavailable" in out
        assert "CAPTCHA" in out

    async def test_bridge_failure_reports_error(self):
        with patch.object(android_web, "_get_bridge", side_effect=RuntimeError("no bridge")):
            with patch.object(android_web, "destroy_bridge") as destroy:
                out = await self._tool().execute("python")
        destroy.assert_called_once()
        assert "web_search unavailable" in out

    async def test_bridge_timeout_destroys_bridge_and_reports_error(self):
        with patch.object(android_web, "_bridge_search", side_effect=asyncio.TimeoutError("timed out")):
            with patch.object(android_web, "destroy_bridge") as destroy:
                out = await self._tool().execute("python")
        destroy.assert_called_once()
        assert "web_search unavailable" in out

    async def test_cancelled_destroys_bridge_and_reraises(self):
        with patch.object(android_web, "_bridge_search", side_effect=asyncio.CancelledError):
            with patch.object(android_web, "destroy_bridge") as destroy:
                with pytest.raises(asyncio.CancelledError):
                    await self._tool().execute("python")
        destroy.assert_called_once()


class TestAndroidWebFetchToolExecute:
    def _tool(self) -> AndroidWebFetchTool:
        return AndroidWebFetchTool(android_context=object(), timeout=5)

    async def test_bridge_success(self):
        payload = json.dumps(
            {"html": "<html><body><h1>Hello</h1></body></html>", "finalUrl": "https://example.com/"}
        )
        with patch.object(
            android_web, "_bridge_fetch", return_value=(
                "<html><body><h1>Hello</h1></body></html>", "https://example.com/"
            )
        ):
            out = json.loads(await self._tool().execute("https://example.com"))
        assert out["finalUrl"] == "https://example.com/"
        assert "Hello" in out["text"]
        assert payload  # payload shape documented above

    async def test_bridge_failure_reports_error_without_fallback(self):
        with patch.object(android_web, "_bridge_fetch", side_effect=RuntimeError("no bridge")):
            with patch.object(android_web, "destroy_bridge") as destroy:
                out = json.loads(await self._tool().execute("https://example.com"))
        destroy.assert_called_once()
        assert "WebView fetch failed" in out["error"]
        assert "no bridge" in out["error"]

    async def test_no_document_error_is_actionable_and_spares_the_bridge(self):
        """Un URL che non è una pagina HTML non deve costare il WebView condiviso.

        Il bridge risponde, ma senza documento: plain text servito con CSP
        `sandbox` (raw.githubusercontent.com), download, binari. Il WebView è
        sano — è l'URL a non essere una pagina — e distruggerlo butterebbe
        cookie, localStorage e warm-up del renderer per il chiamante successivo.
        L'errore deve inoltre dire cosa fare, non solo che è andata male.
        """
        with patch.object(
            android_web,
            "_bridge_fetch",
            side_effect=ValueError("WebView returned no HTML document"),
        ):
            with patch.object(android_web, "destroy_bridge") as destroy:
                out = json.loads(await self._tool().execute("https://example.com/a.md"))
        destroy.assert_not_called()
        assert "no HTML document" in out["error"]
        assert "http_get" in out["hint"]

    async def test_timeout_error_also_suggests_http_get(self):
        # Una risposta che il browser scarica invece di aprire non fa mai
        # scattare `onPageFinished` (nessun DownloadListener lato Kotlin): da
        # qui si vede solo un timeout, quindi anche quello porta il consiglio.
        with patch.object(android_web, "_bridge_fetch", side_effect=asyncio.TimeoutError):
            with patch.object(android_web, "destroy_bridge") as destroy:
                out = json.loads(await self._tool().execute("https://example.com/x.zip"))
        destroy.assert_called_once()
        assert "timed out" in out["error"]
        assert "http_get" in out["hint"]

    async def test_final_url_redirected_to_loopback_is_blocked(self):
        """The WebView follows redirects/JS navigation with no per-hop SSRF
        check of its own (unlike httpx, this can't be fixed from the Python
        side before the fact). The best we can do after the fact is
        re-validate the finalUrl the bridge reports and withhold the fetched
        content if it landed somewhere disallowed."""
        with patch.object(
            android_web,
            "_bridge_fetch",
            return_value=("<html><body>admin panel</body></html>", "http://127.0.0.1:9111/admin"),
        ):
            out = json.loads(await self._tool().execute("https://example.com"))
        assert "error" in out
        assert "blocked" in out["error"].lower()
        assert out["finalUrl"] == "http://127.0.0.1:9111/admin"
        # The fetched content must never reach the caller once the final
        # destination fails the SSRF check.
        assert "text" not in out

    async def test_final_url_redirected_to_metadata_endpoint_is_blocked(self):
        with patch.object(
            android_web,
            "_bridge_fetch",
            return_value=("<html>creds</html>", "http://169.254.169.254/latest/meta-data/"),
        ):
            out = json.loads(await self._tool().execute("https://example.com"))
        assert "error" in out
        assert "text" not in out

    async def test_final_url_on_same_allowed_host_still_returns_content(self):
        """Regression guard: a normal fetch with no redirect (finalUrl ==
        initial url, both public) must still return content — the new
        post-fetch check must not break the common case."""
        with patch.object(
            android_web,
            "_bridge_fetch",
            return_value=("<html><body><h1>Hello</h1></body></html>", "https://example.com/"),
        ):
            out = json.loads(await self._tool().execute("https://example.com"))
        assert "error" not in out
        assert "Hello" in out["text"]
        assert out["finalUrl"] == "https://example.com/"


class TestToolGating:
    """search/fetch gate solely on the top-level ``android_web.enable`` flag.

    ``AndroidWebSearchConfig.enable`` / ``AndroidWebFetchConfig.enable`` were
    declared in the schema but never read anywhere (the tool ``enabled()``
    classmethods only checked ``ctx.config.android_web.enable``), so they were
    removed rather than wired up — see docs/configuration.md, which never
    documented them as independently configurable.
    """

    def test_per_tool_enable_fields_no_longer_exist(self):
        assert not hasattr(AndroidWebSearchConfig(), "enable")
        assert not hasattr(AndroidWebFetchConfig(), "enable")

    def test_top_level_flag_enables_both_tools(self):
        ctx = SimpleNamespace(android_context=Mock(), config=ToolsConfig())
        assert AndroidWebSearchTool.enabled(ctx) is True
        assert AndroidWebFetchTool.enabled(ctx) is True

    def test_top_level_flag_disables_both_tools(self):
        cfg = ToolsConfig()
        cfg.android_web.enable = False
        ctx = SimpleNamespace(android_context=Mock(), config=cfg)
        assert AndroidWebSearchTool.enabled(ctx) is False
        assert AndroidWebFetchTool.enabled(ctx) is False

    def test_legacy_per_tool_enable_key_in_config_is_silently_ignored(self):
        """A config.json written before the cleanup (or hand-edited to add
        the never-wired flag) must still parse and must not disable the tool:
        the key is unknown now and gets dropped, not enforced."""
        cfg = ToolsConfig.model_validate(
            {"androidWeb": {"search": {"enable": False}, "fetch": {"enable": False}}}
        )
        ctx = SimpleNamespace(android_context=Mock(), config=cfg)
        assert AndroidWebSearchTool.enabled(ctx) is True
        assert AndroidWebFetchTool.enabled(ctx) is True
