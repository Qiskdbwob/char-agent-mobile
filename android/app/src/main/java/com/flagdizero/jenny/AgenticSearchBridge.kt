package com.flagdizero.jenny

import android.content.Context
import android.content.pm.ApplicationInfo
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.net.InetAddress
import java.net.UnknownHostException
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Hidden WebView bridge for agentic web search and fetch on Android.
 *
 * The WebView is a real Chrome browser instance: it executes JS, handles TLS,
 * cookies and localStorage just like the visible browser. It is kept hidden
 * (GONE) and reused across calls to amortise startup cost.
 */
class AgenticSearchBridge(context: Context) {

    companion object {
        private const val TAG = "AgenticSearchBridge"
        private const val DEFAULT_TIMEOUT_SECONDS = 30L
        private const val MAX_RESULTS_DEFAULT = 10
        private const val USER_AGENT_MOBILE =
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
    }

    private val handler = Handler(Looper.getMainLooper())
    private val appContext = context.applicationContext
    private var webView: WebView? = null

    // ── Pavimento di sicurezza per le URL ──────────────────────────────────
    //
    // La policy SSRF completa vive in Python (`jenny/security/network.py`) e
    // non è replicabile qui: Kotlin non legge config.json. Questi controlli
    // sono un **pavimento** conservativo, nello stesso senso di
    // `UpdateBridge.openHttps`: bloccano ciò che nessuna policy potrebbe mai
    // permettere (schemi non-web, loopback, link-local, metadata) e lasciano
    // il resto alla validazione Python — quella iniziale sull'URL passato dal
    // tool e quella post-fetch sull'URL finale in `android_web.py`.
    //
    // RFC1918/ULA/CGNAT NON sono bloccati qui: la `ssrfWhitelist` può
    // legittimamente aprirli (server di casa, Tailscale), e bloccarli in
    // Kotlin romperebbe proprio il caso che la whitelist esiste per servire.
    //
    // Perché servono se Python valida già: il WebView segue redirect e
    // navigazioni JS per conto proprio (Chromium reale, non httpx), quindi un
    // 302 verso `http://127.0.0.1:port/...` comparso dopo la validazione
    // iniziale verrebbe comunque fetchato — la validazione Python post-fetch
    // può solo sopprimere il risultato, non impedire la richiesta. Prima di
    // questi guard ogni hop era permesso; ora una navigazione verso il telefono
    // stesso (o verso uno schema non-web) viene fermata qui, al confine.
    private val ipv4Literal = Regex("^\\d{1,3}(\\.\\d{1,3}){3}$")
    private val ipv6Literal = Regex("^[0-9a-fA-F:]+$")

    private fun looksLikeIpLiteral(host: String): Boolean =
        ipv4Literal.matches(host) || (host.contains(':') && ipv6Literal.matches(host))

    /** Host di una URL, minuscolo, senza trailing dot né parentesi IPv6. */
    private fun normalizedHost(url: Uri): String? {
        val host = url.host ?: return null
        var h = host.lowercase(Locale.ROOT).trimEnd('.')
        if (h.startsWith("[") && h.endsWith("]")) {
            h = h.substring(1, h.length - 1)
        }
        return h
    }

    /**
     * Indirizzi che nessuna policy potrebbe mai voler raggiungere da qui:
     * il telefono stesso (loopback), la rete del metadata/link-local e gli
     * indirizzi di broadcast/multicast. Su un literal IP non c'è DNS da fare,
     * quindi è sicuro chiamarla anche dal main thread.
     */
    private fun isLiteralBlockedAddress(host: String): Boolean {
        val addr = try {
            InetAddress.getByName(host)
        } catch (e: UnknownHostException) {
            return false
        }
        return addr.isAnyLocalAddress || addr.isLoopbackAddress ||
            addr.isLinkLocalAddress || addr.isMulticastAddress
    }

    /**
     * Navigazione del main frame: controlli SENZA DNS (main thread, un lookup
     * bloccante qui sarebbe un ANR). Gli hostname che risolvono a un indirizzo
     * bloccato vengono coperti da `shouldInterceptRequest`, che gira su un
     * thread di lavoro.
     */
    private fun isMainFrameSafe(url: Uri): Boolean {
        val scheme = url.scheme?.lowercase(Locale.ROOT) ?: return false
        if (scheme != "http" && scheme != "https") return false
        val host = normalizedHost(url) ?: return false
        if (host.isEmpty()) return false
        if (host == "localhost" || host.endsWith(".localhost")) return false
        if (looksLikeIpLiteral(host) && isLiteralBlockedAddress(host)) return false
        return true
    }

    /**
     * Qualunque risorsa (main frame compreso): gira su un thread di lavoro
     * (`shouldInterceptRequest` è documentato per girare fuori dal main
     * thread), quindi qui il DNS è concesso. `cache` è per-call: fresca a ogni
     * pagina e bounded per costruzione.
     */
    private fun isRequestSafe(url: Uri, cache: MutableMap<String, Boolean>): Boolean {
        val scheme = url.scheme?.lowercase(Locale.ROOT)
        // about:/data:/blob: non toccano la rete, nessun filtro necessario.
        if (scheme == "about" || scheme == "data" || scheme == "blob") return true
        if (scheme != "http" && scheme != "https") return false
        val host = normalizedHost(url) ?: return false
        if (host.isEmpty()) return false
        if (looksLikeIpLiteral(host)) return !isLiteralBlockedAddress(host)
        return !isBlockedHostname(host, cache)
    }

    private fun isBlockedHostname(host: String, cache: MutableMap<String, Boolean>): Boolean {
        cache[host]?.let { return it }
        val blocked = try {
            InetAddress.getAllByName(host).any { addr ->
                addr.isAnyLocalAddress || addr.isLoopbackAddress ||
                    addr.isLinkLocalAddress || addr.isMulticastAddress
            }
        } catch (e: UnknownHostException) {
            // Irrisolvibile: la richiesta fallirà da sola, non è un obiettivo
            // da sondare.
            false
        }
        cache[host] = blocked
        return blocked
    }

    private fun ensureWebView() {
        if (webView != null) return
        // Remote debugging (chrome://inspect) SOLO su build debuggable. Il
        // flag non è condizionato da `android:debuggable` — va esplicito — e su
        // una build di release esporrebbe il DOM di TUTTI i WebView del
        // processo (la WebUI compresa, cioè chat/workspace/settings) a chiunque
        // abbia adb. Il check sul flag è l'unica cosa che distingue i due
        // mondi.
        val debuggable = (appContext.applicationInfo.flags and
            ApplicationInfo.FLAG_DEBUGGABLE) != 0
        if (debuggable) {
            try {
                WebView.setWebContentsDebuggingEnabled(true)
            } catch (e: Exception) {
                Log.w(TAG, "Could not enable web contents debugging", e)
            }
        }
        webView = WebView(appContext).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.setSupportZoom(false)
            settings.builtInZoomControls = false
            settings.displayZoomControls = false
            settings.loadsImagesAutomatically = false
            settings.mediaPlaybackRequiresUserGesture = true
            settings.javaScriptCanOpenWindowsAutomatically = false
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            settings.userAgentString = USER_AGENT_MOBILE
            visibility = View.GONE
            webChromeClient = object : WebChromeClient() {
                override fun onConsoleMessage(msg: ConsoleMessage?): Boolean {
                    Log.d(TAG, "JS console [${msg?.sourceId()}:${msg?.lineNumber()}] ${msg?.message()}")
                    return super.onConsoleMessage(msg)
                }
            }
        }
    }

    /**
     * Search Bing and return a JSON object string: { "results": [...], "pageText": "..." }.
     * Each result has: title, url, snippet. "pageText" carries the visible page
     * text only when results is empty, so the Python side can distinguish a
     * genuinely empty SERP from a bot-verification/block page.
     */
    fun searchBing(query: String, maxResults: Int, timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        val encoded = Uri.encode(query)
        val url = "https://www.bing.com/search?q=$encoded"
        Log.d(TAG, "searchBing ENTER: query=$query maxResults=$maxResults timeout=$timeoutSeconds")

        val js = """
            (function() {
                function collectResults(selector) {
                    const results = [];
                    document.querySelectorAll(selector).forEach(el => {
                        const linkEl = el.querySelector('a');
                        const titleEl = el.querySelector('h2, .b_title');
                        const snippetEl = el.querySelector('p, .b_caption p, .b_snippet, .b_lineclamp');
                        if (linkEl && titleEl) {
                            results.push({
                                title: titleEl.textContent.trim(),
                                url: linkEl.href,
                                snippet: snippetEl ? snippetEl.textContent.trim() : ''
                            });
                        }
                    });
                    return results;
                }
                let results = collectResults('li.b_algo');
                if (results.length === 0) {
                    results = collectResults('div.b_algo');
                }
                if (results.length === 0) {
                    results = collectResults('.b_results > li');
                }
                results = results.slice(0, ${maxResults.coerceIn(1, MAX_RESULTS_DEFAULT)});
                const pageText = results.length === 0
                    ? (document.body ? document.body.innerText.slice(0, 4000) : '')
                    : '';
                return JSON.stringify({results: results, pageText: pageText});
            })()
        """.trimIndent()

        val result = evaluateOnPage(url, js, timeoutSeconds)

        if (result.isBlank() || result == "null") {
            Log.w(TAG, "Bing search returned no data from WebView")
            return JSONObject().apply {
                put("error", "WebView returned no data for Bing search")
            }.toString()
        }

        Log.d(TAG, "Bing search response length: ${result.length}")
        return result
    }

    /**
     * Fetch any URL through the hidden WebView and return the rendered HTML.
     * Returns a JSON object: { "html": "...", "finalUrl": "..." }.
     */
    fun fetchUrl(url: String, timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        Log.d(TAG, "fetchUrl ENTER: url=$url timeout=$timeoutSeconds")
        val js = """
            JSON.stringify({
                html: document.documentElement.outerHTML,
                finalUrl: window.location.href
            })
        """.trimIndent()
        return evaluateOnPage(url, js, timeoutSeconds)
    }

    /**
     * Destroy the hidden WebView and release resources.
     */
    fun destroy() {
        Log.d(TAG, "Destroying bridge")
        handler.post {
            webView?.stopLoading()
            webView?.destroy()
            webView = null
        }
    }

    /**
     * Load [url], wait for page finish, then run [js] and return the result.
     * Runs on the main thread and blocks the calling thread.
     */
    private fun evaluateOnPage(url: String, js: String, timeoutSeconds: Long): String {
        Log.d(TAG, "evaluateOnPage ENTER: url=$url timeout=$timeoutSeconds")
        val latch = CountDownLatch(1)
        val ref = AtomicReference<String>("")
        val errorRef = AtomicReference<String?>(null)
        // Cache DNS per-call dei host già controllati: `shouldInterceptRequest`
        // gira su thread di lavoro e una pagina di Bing genera decine di
        // richieste sugli stessi host. Fresca a ogni pagina, bounded per
        // costruzione.
        val dnsCache = ConcurrentHashMap<String, Boolean>()

        handler.post {
            Log.d(TAG, "evaluateOnPage: setting up WebViewClient and loading URL")
            // Difesa in profondità sull'URL iniziale: Python lo ha già
            // validato, ma se un giorno quel controllo salta, qui non si carica
            // nulla che non sia http(s) pubblico.
            val initial = Uri.parse(url)
            if (!isMainFrameSafe(initial)) {
                Log.w(TAG, "Blocked initial URL: $url")
                errorRef.set("Blocked URL: $url")
                latch.countDown()
                return@post
            }
            ensureWebView()
            val wv = webView!!
            wv.webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                    val navUrl = request?.url ?: return false
                    if (!isMainFrameSafe(navUrl)) {
                        Log.w(TAG, "Blocked main-frame navigation to $navUrl")
                        return true
                    }
                    return super.shouldOverrideUrlLoading(view, request)
                }

                override fun shouldInterceptRequest(
                    view: WebView?,
                    request: WebResourceRequest?
                ): WebResourceResponse? {
                    val reqUrl = request?.url
                    if (reqUrl != null && !isRequestSafe(reqUrl, dnsCache)) {
                        Log.w(TAG, "Blocked request to $reqUrl")
                        return WebResourceResponse(
                            "text/plain", "utf-8", 403, "Forbidden",
                            ByteArrayInputStream("blocked by network policy".toByteArray(Charsets.UTF_8))
                        )
                    }
                    return super.shouldInterceptRequest(view, request)
                }

                override fun onReceivedError(
                    view: WebView?,
                    request: WebResourceRequest?,
                    error: WebResourceError?
                ) {
                    if (request?.isForMainFrame != false) {
                        val msg = "WebView error: ${error?.description ?: "unknown"} (${error?.errorCode ?: -1})"
                        Log.e(TAG, msg)
                        errorRef.set(msg)
                        latch.countDown()
                    }
                }

                override fun onReceivedHttpError(
                    view: WebView?,
                    request: WebResourceRequest?,
                    errorResponse: android.webkit.WebResourceResponse?
                ) {
                    if (request?.isForMainFrame != false) {
                        val status = errorResponse?.statusCode ?: -1
                        val msg = "WebView HTTP error: $status for ${request?.url ?: url}"
                        Log.e(TAG, msg)
                        errorRef.set(msg)
                        latch.countDown()
                    }
                }

                override fun onPageStarted(view: WebView?, startedUrl: String?, favicon: android.graphics.Bitmap?) {
                    Log.d(TAG, "onPageStarted: $startedUrl")
                }

                override fun onPageFinished(view: WebView?, finishedUrl: String?) {
                    Log.d(TAG, "onPageFinished: $finishedUrl (error=${errorRef.get()})")
                    if (errorRef.get() != null) {
                        latch.countDown()
                        return
                    }
                    Log.d(TAG, "evaluateOnPage: running evaluateJavascript")
                    view?.evaluateJavascript(js) { value ->
                        Log.d(TAG, "evaluateJavascript callback: value length=${value?.length ?: 0} null=${value == null}")
                        ref.set(value ?: "")
                        latch.countDown()
                    }
                }
            }
            Log.d(TAG, "evaluateOnPage: calling loadUrl($url)")
            wv.loadUrl(url)
            Log.d(TAG, "evaluateOnPage: loadUrl returned (post)")
        }

        Log.d(TAG, "evaluateOnPage: waiting on latch...")
        val completed = latch.await(timeoutSeconds, TimeUnit.SECONDS)
        Log.d(TAG, "evaluateOnPage: latch completed=$completed error=${errorRef.get()}")

        if (errorRef.get() != null) {
            handler.post { webView?.stopLoading() }
            return JSONObject().apply {
                put("error", errorRef.get())
            }.toString()
        }
        if (!completed) {
            handler.post { webView?.stopLoading() }
            val msg = "WebView timeout after ${timeoutSeconds}s for $url"
            Log.w(TAG, msg)
            return JSONObject().apply {
                put("error", msg)
            }.toString()
        }

        Log.d(TAG, "evaluateOnPage EXIT: ref length=${ref.get().length}")
        return ref.get()
    }
}
