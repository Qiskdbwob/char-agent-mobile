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
import org.json.JSONException
import org.json.JSONObject
import org.json.JSONTokener
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
 *
 * Oltre a `searchBing`/`fetchUrl` (one-shot: naviga, valuta, ritorna) il bridge
 * espone una **sessione di browsing interattiva** per l'agent automation:
 * `browserOpen` carica una pagina e la tiene; `browserSnapshot` restituisce
 * testo + elementi cliccabili con selettori; `browserClick`/`browserType`/
 * `browserSubmit` agiscono sulla pagina correntemente caricata;
 * `browserBack`/`browserClose` chiudono il giro. Cookie, localStorage e login
 * persistono tra una chiamata e l'altra (è lo stesso WebView condiviso), quindi
 * il modello può compiere flussi reali (login, form, navigazioni multi-pagina).
 */
class AgenticSearchBridge(context: Context) {

    companion object {
        private const val TAG = "AgenticSearchBridge"
        private const val DEFAULT_TIMEOUT_SECONDS = 30L
        private const val MAX_RESULTS_DEFAULT = 10
        private const val USER_AGENT_MOBILE =
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"

        // Limiti del browser interattivo: snapshot troncato ed elementi
        // raccolti per non inondare il contesto del modello. L'ulteriore
        // troncatura in Python (AndroidWebBrowserConfig.maxSnapshotChars)
        // resta il capo autorevole; questo è il tetto lato renderer.
        private const val SNAPSHOT_MAX_TEXT_CHARS = 40000
        private const val SNAPSHOT_MAX_ELEMENTS = 50

        // Budget di attesa dopo un'azione che *può* navigare (click, submit):
        // se la pagina parte, onPageFinished sblocca subito; se non naviga
        // (SPA, click no-op) si esce comunque dopo questo tempo invece di
        // tenere il turno dell'agente fermo per l'intero timeout.
        private const val ACTION_SETTLE_SECONDS = 4L

        // Tetto per la creazione della WebView sul main thread: in condizioni
        // normali richiede qualche decina di ms; il cap esiste solo per non
        // bloccare un thread Python per sempre se il main looper è occupato.
        private const val WEBVIEW_INIT_TIMEOUT_SECONDS = 10L
    }

    private val handler = Handler(Looper.getMainLooper())
    private val appContext = context.applicationContext
    private var webView: WebView? = null

    // ── Stato della sessione interattiva ────────────────────────────────────
    //
    // ``browserPageLoaded`` distingue "c'è una pagina su cui agire" dalla
    // WebView appena creata (about:blank). Il WebView resta lo stesso usato
    // da search/fetch: le sessioni interattive condividono cookie e renderer,
    // e una chiamata a fetchUrl in mezzo a una sessione semplicemente naviga
    // altrove (lo stato interattivo non sopravvive a una navigazione esterna).
    private var browserPageLoaded = false

    // Latch osservato dai callback del WebViewClient *persistente*: lo si
    // sostituisce a ogni attesa di navigazione, così onPageFinished/
    // onReceivedError sbloccano solo l'attesa in corso. Tutti i callback
    // girano sul main thread, quindi lo scambio non ha race.
    private var navigationLatch: CountDownLatch? = null
    private var pendingError: String? = null

    // Cache DNS per-call degli host già controllati: `shouldInterceptRequest`
    // gira su thread di lavoro e una pagina genera decine di richieste sugli
    // stessi host. Bounded per costruzione (svuotata a ogni navigazione).
    private val dnsCache = ConcurrentHashMap<String, Boolean>()

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
     * thread), quindi qui il DNS è concesso.
     */
    private fun isRequestSafe(url: Uri): Boolean {
        val scheme = url.scheme?.lowercase(Locale.ROOT)
        // about:/data:/blob: non toccano la rete, nessun filtro necessario.
        if (scheme == "about" || scheme == "data" || scheme == "blob") return true
        if (scheme != "http" && scheme != "https") return false
        val host = normalizedHost(url) ?: return false
        if (host.isEmpty()) return false
        if (looksLikeIpLiteral(host)) return !isLiteralBlockedAddress(host)
        return !isBlockedHostname(host)
    }

    private fun isBlockedHostname(host: String): Boolean {
        dnsCache[host]?.let { return it }
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
        dnsCache[host] = blocked
        return blocked
    }

    /**
     * Garantisce che la WebView esista, creandola sul main thread.
     *
     * Il bridge è chiamato da thread Python (Chaquopy) privi di Looper, ma
     * `WebView` si può costruire SOLO su un thread con Looper
     * ("WebView cannot be initialized on a thread that has no Looper" — il
     * bug che ha rotto web_search/web_fetch a 0.7.2). La creazione viene
     * quindi postata sul main looper e l'attesa avviene QUI, sul thread
     * chiamante: durante una chiamata del bridge il main thread è libero
     * (l'UI aspetta la risposta via WebSocket), quindi il latch non può
     * deadlockare. Se il chiamante è già il main thread, si crea inline.
     */
    private fun ensureWebView() {
        if (webView != null) return
        if (Looper.myLooper() == Looper.getMainLooper()) {
            createWebView()
            return
        }
        val latch = CountDownLatch(1)
        val failure = AtomicReference<Throwable?>(null)
        handler.post {
            try {
                // Doppio check: il post è asincrono, e nel frattempo un'altra
                // chiamata (serializzata dal lock Python) può averla creata.
                if (webView == null) createWebView()
            } catch (t: Throwable) {
                failure.set(t)
            } finally {
                latch.countDown()
            }
        }
        val completed = try {
            latch.await(WEBVIEW_INIT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }
        val err = failure.get()
        if (err != null) {
            throw RuntimeException("WebView initialization failed on main thread", err)
        }
        if (!completed) {
            throw RuntimeException(
                "Timed out waiting for WebView initialization on the main thread"
            )
        }
    }

    /**
     * Guardia di regressione: ogni metodo/proprietà della WebView deve girare
     * sul thread che possiede il suo Looper (il main). Se un futuro refactoring
     * chiamasse la WebView da un thread senza Looper, qui si fallisce con un
     * messaggio chiaro — e col nome del thread colpevole — invece del
     * Throwable criptico di ``WebView.checkThread``.
     */
    private fun assertMainThread(what: String) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            throw IllegalStateException(
                "WebView $what must run on the main Looper thread, but was called on " +
                    Thread.currentThread().name
            )
        }
    }

    /** Crea la WebView nascosta; va chiamata SOLO sul main thread. */
    private fun createWebView() {
        assertMainThread("creation")
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
            webViewClient = makeGuardedClient()
        }
    }

    /**
     * Il WebViewClient è **persistente** (installato una volta sola): i suoi
     * callback devono continuare a girare tra una chiamata e l'altra della
     * sessione interattiva, perché un click avviato da `browserClick` naviga
     * *dopo* che la chiamata è tornata al chiamante. Contiene gli stessi guard
     * SSRF di prima, su ogni navigazione e ogni risorsa.
     */
    private fun makeGuardedClient(): WebViewClient = object : WebViewClient() {
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
            if (reqUrl != null && !isRequestSafe(reqUrl)) {
                Log.w(TAG, "Blocked request to $reqUrl")
                return WebResourceResponse(
                    "text/plain", "utf-8", 403, "Forbidden",
                    emptyMap(),
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
                pendingError = msg
                navigationLatch?.countDown()
            }
        }

        override fun onReceivedHttpError(
            view: WebView?,
            request: WebResourceRequest?,
            errorResponse: WebResourceResponse?
        ) {
            if (request?.isForMainFrame != false) {
                val status = errorResponse?.statusCode ?: -1
                val msg = "WebView HTTP error: $status for ${request?.url ?: "main frame"}"
                Log.e(TAG, msg)
                pendingError = msg
                navigationLatch?.countDown()
            }
        }

        override fun onPageStarted(view: WebView?, startedUrl: String?, favicon: android.graphics.Bitmap?) {
            Log.d(TAG, "onPageStarted: $startedUrl")
        }

        override fun onPageFinished(view: WebView?, finishedUrl: String?) {
            Log.d(TAG, "onPageFinished: $finishedUrl (error=${pendingError})")
            navigationLatch?.countDown()
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

    // ── Sessione di browsing interattiva ────────────────────────────────────
    //
    // Contratto col lato Python (android_web.py): ogni metodo risponde con un
    // JSON object; su fallimento porta `error` e nient'altro. Gli errori
    // strutturati (sessione non aperta, selettore non trovato) NON distruggono
    // il WebView: cookie e pagina restano, e il modello può correggere il tiro.

    /**
     * Apre [url] e attende il caricamento. Inizia (o riavvia) la sessione
     * interattiva: da qui in poi snapshot/click/type/submit/back agiscono sulla
     * pagina corrente. Risposta: { "ok": true, "url": ..., "title": ... }.
     */
    fun browserOpen(url: String, timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        Log.d(TAG, "browserOpen ENTER: url=$url timeout=$timeoutSeconds")
        val result = navigateAndWait(url, timeoutSeconds)
        browserPageLoaded = result.optString("error").isEmpty()
        return result.toString()
    }

    /**
     * Estrae la pagina corrente: { "url", "title", "text", "elements" } con
     * ``elements`` = elenco dei controlli interattivi (link, bottoni, input,
     * select, textarea) completi di selettore CSS stabile per l'uso nei tool
     * successivi.
     */
    fun browserSnapshot(timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        if (!browserPageLoaded) {
            return errorJson("browser session is not open; call browser_open(url) first")
        }
        return decodeJsResult(evaluateOnCurrentPage(SNAPSHOT_JS, timeoutSeconds)).toString()
    }

    /**
     * Clicca l'elemento individuato da [selector] (CSS selector). Se il click
     * avvia una navigazione, attende che la nuova pagina finisca (budget
     * `ACTION_SETTLE_SECONDS`); se non naviga, esce comunque dopo il budget.
     * Risposta: { "ok": true, "found": true, "url": ... }.
     */
    fun browserClick(selector: String, timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        if (!browserPageLoaded) {
            return errorJson("browser session is not open; call browser_open(url) first")
        }
        val js = """
            (function() {
                const sel = ${JSONObject.quote(selector)};
                let el;
                try { el = document.querySelector(sel); } catch (e) {
                    return JSON.stringify({found: false, error: 'invalid selector: ' + e.message});
                }
                if (!el) return JSON.stringify({found: false, error: 'no element matches ' + sel});
                el.scrollIntoView({block: 'center'});
                el.click();
                return JSON.stringify({found: true, tag: el.tagName.toLowerCase()});
            })()
        """.trimIndent()
        val result = decodeJsResult(evaluateOnCurrentPage(js, timeoutSeconds))
        if (result.optString("error").isNotEmpty() || !result.optBoolean("found", false)) {
            return result.toString()
        }
        waitForPageOrSettle(ACTION_SETTLE_SECONDS)
        return JSONObject()
            .put("ok", true)
            .put("found", true)
            .put("url", currentUrl())
            .toString()
    }

    /**
     * Compila [text] nell'elemento di [selector] (input/textarea), usando il
     * setter nativo + eventi `input`/`change` così funziona anche coi framework
     * che ascoltano i cambi di valore (React e simili). Non naviga.
     */
    fun browserType(selector: String, text: String, timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        if (!browserPageLoaded) {
            return errorJson("browser session is not open; call browser_open(url) first")
        }
        val js = """
            (function() {
                const sel = ${JSONObject.quote(selector)};
                const value = ${JSONObject.quote(text)};
                let el;
                try { el = document.querySelector(sel); } catch (e) {
                    return JSON.stringify({found: false, error: 'invalid selector: ' + e.message});
                }
                if (!el) return JSON.stringify({found: false, error: 'no element matches ' + sel});
                if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') {
                    return JSON.stringify({found: false, error: 'element is not an input/textarea'});
                }
                const proto = el.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.focus();
                return JSON.stringify({found: true});
            })()
        """.trimIndent()
        return decodeJsResult(evaluateOnCurrentPage(js, timeoutSeconds)).toString()
    }

    /**
     * Invia il form: usa [selector] se presente (form o controllo submit),
     * altrimenti il primo submit/input[type=submit] o form della pagina.
     * Come il click, attende un'eventuale navigazione entro il budget di settle.
     */
    fun browserSubmit(selector: String?, timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        if (!browserPageLoaded) {
            return errorJson("browser session is not open; call browser_open(url) first")
        }
        val selLit = JSONObject.quote(selector ?: "")
        val js = """
            (function() {
                const sel = $selLit;
                let el = null;
                if (sel) { try { el = document.querySelector(sel); } catch (e) { /* sotto */ } }
                if (!el) {
                    el = document.querySelector(
                        'button[type="submit"], input[type="submit"], input[type="image"], form'
                    );
                }
                if (!el) return JSON.stringify({found: false, error: 'no submit control found'});
                if (el.tagName === 'FORM') {
                    if (el.requestSubmit) { el.requestSubmit(); } else { el.submit(); }
                } else if (el.form && el.form.requestSubmit) {
                    el.form.requestSubmit(el);
                } else {
                    el.click();
                }
                return JSON.stringify({found: true, tag: el.tagName.toLowerCase()});
            })()
        """.trimIndent()
        val result = decodeJsResult(evaluateOnCurrentPage(js, timeoutSeconds))
        if (result.optString("error").isNotEmpty() || !result.optBoolean("found", false)) {
            return result.toString()
        }
        waitForPageOrSettle(ACTION_SETTLE_SECONDS)
        return JSONObject()
            .put("ok", true)
            .put("found", true)
            .put("url", currentUrl())
            .toString()
    }

    /**
     * Torna alla pagina precedente nella history del WebView (una navigazione
     * interattiva di back, non un reset). Attende il caricamento completo.
     */
    fun browserBack(timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS): String {
        if (!browserPageLoaded) {
            return errorJson("browser session is not open; call browser_open(url) first")
        }
        val wv = webView ?: return errorJson("browser is not initialized")
        val wentBack = AtomicReference<Boolean>(false)
        val latch = CountDownLatch(1)
        handler.post {
            // Il latch va posato PRIMA di goBack: se la pagina vecchia non c'è
            // (o goBack fallisce), lo contiamo giù subito; se la navigazione
            // parte, onPageFinished lo sblocca. Stesso ordine di
            // waitForPageOrSettle, ma atomico nel post per non perdere il
            // countDown nel caso "nessuna history".
            navigationLatch = latch
            // goBack() è void: canGoBack() decide, goBack() esegue.
            val ok = wv.canGoBack()
            if (ok) wv.goBack()
            wentBack.set(ok)
            if (!ok) latch.countDown()
        }
        latch.await(timeoutSeconds, TimeUnit.SECONDS)
        if (wentBack.get() != true) {
            return errorJson("no previous page in browser history")
        }
        return JSONObject().put("ok", true).put("url", currentUrl()).toString()
    }

    /**
     * Chiude la sessione interattiva: scarica la pagina corrente (libera il
     * renderer) e resetta lo stato. I cookie restano — una riapertura
     * successiva ritrova i login — perché il WebView è condiviso con
     * search/fetch e distruggerli costerebbe anche il warm-up del renderer.
     */
    fun browserClose(): String {
        Log.d(TAG, "browserClose")
        handler.post {
            webView?.stopLoading()
            // about:blank rilascia il documento pesante ma tiene vivo il
            // renderer per il chiamante successivo (search/fetch o un nuovo open).
            webView?.loadUrl("about:blank")
        }
        browserPageLoaded = false
        return JSONObject().put("ok", true).toString()
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
        browserPageLoaded = false
    }

    private fun errorJson(message: String): String =
        JSONObject().apply { put("error", message) }.toString()

    /**
     * URL corrente della WebView, letto sul main thread.
     *
     * ``wv.url`` è ``WebView.getUrl()``, che esegue ``checkThread``: chiamarlo
     * dal thread Python (Chaquopy, senza Looper) solleva il Throwable che ha
     * rotto tutti i tool web a 0.7.4 ("A WebView method was called on thread
     * 'Thread-7'. All WebView methods must be called on the same thread.").
     * Ogni lettura di proprietà della WebView deve passare dal main looper,
     * esattamente come le chiamate in scrittura già postate su `handler`.
     */
    private fun currentUrl(): String {
        val wv = webView ?: return ""
        if (Looper.myLooper() == Looper.getMainLooper()) {
            return try {
                wv.url ?: ""
            } catch (t: Throwable) {
                ""
            }
        }
        val latch = CountDownLatch(1)
        val ref = AtomicReference<String?>()
        handler.post {
            try {
                ref.set(wv.url)
            } catch (t: Throwable) {
                ref.set(null)
            } finally {
                latch.countDown()
            }
        }
        val completed = try {
            latch.await(5, TimeUnit.SECONDS)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }
        return if (completed) ref.get() ?: "" else ""
    }

    /**
     * Carica [url], attende la fine del caricamento e restituisce
     * { "url": ..., "title": ... } o { "error": ... }. Applica il pavimento
     * SSRF sull'URL iniziale e lascia al client persistente i guard su
     * redirect/risorse.
     */
    private fun navigateAndWait(url: String, timeoutSeconds: Long): JSONObject {
        val initial = Uri.parse(url)
        if (!isMainFrameSafe(initial)) {
            Log.w(TAG, "Blocked initial URL: $url")
            return JSONObject().apply { put("error", "Blocked URL: $url") }
        }
        ensureWebView()
        val wv = webView!!
        pendingError = null
        dnsCache.clear()
        val latch = CountDownLatch(1)
        navigationLatch = latch
        handler.post {
            wv.loadUrl(url)
        }
        val completed = latch.await(timeoutSeconds, TimeUnit.SECONDS)
        if (pendingError != null) {
            handler.post { wv.stopLoading() }
            return JSONObject().apply { put("error", pendingError) }
        }
        if (!completed) {
            handler.post { wv.stopLoading() }
            return JSONObject().apply { put("error", "WebView timeout after ${timeoutSeconds}s for $url") }
        }
        // Il titolo lo si legge con una valutazione sincrona sulla pagina
        // appena caricata (il post asincrono per ``wv.title`` sarebbe in race
        // con la lettura qui sotto).
        val title = try {
            val raw = evaluateOnCurrentPage("document.title || ''", 5)
            (JSONTokener(raw).nextValue() as? String) ?: ""
        } catch (e: Exception) {
            ""
        }
        return JSONObject()
            .put("ok", true)
            .put("url", currentUrl().ifEmpty { url })
            .put("title", title)
    }

    /**
     * Valuta [js] sulla pagina **corrente**, senza navigare. Ritorna il valore
     * grezzo della callback di `evaluateJavascript` (già JSON-encoded da
     * Chromium), oppure una stringa JSON con `error`.
     */
    private fun evaluateOnCurrentPage(js: String, timeoutSeconds: Long): String {
        val wv = webView
        if (wv == null) return errorJson("browser is not initialized")
        val latch = CountDownLatch(1)
        val ref = AtomicReference<String?>(null)
        handler.post {
            wv.evaluateJavascript(js) { value ->
                ref.set(value)
                latch.countDown()
            }
        }
        val completed = latch.await(timeoutSeconds, TimeUnit.SECONDS)
        if (!completed) {
            return errorJson("WebView evaluate timeout after ${timeoutSeconds}s")
        }
        return ref.get() ?: "null"
    }

    /**
     * Attende che la navigazione in corso finisca (onPageFinished/errore
     * contano giù il latch), con [timeoutSeconds] come tetto: se la pagina non
     * naviga (click no-op, SPA), l'attesa scade e si prosegue comunque.
     */
    private fun waitForPageOrSettle(timeoutSeconds: Long): Boolean {
        val latch = CountDownLatch(1)
        handler.post { navigationLatch = latch }
        return latch.await(timeoutSeconds, TimeUnit.SECONDS)
    }

    /**
     * Decodifica il valore di una callback di `evaluateJavascript`: Chromium
     * JSON-encoda il valore di ritorno, e i nostri script ritornano a loro
     * volta `JSON.stringify(...)`, quindi il payload arriva doppiamente
     * codificato (una stringa JSON che contiene JSON). Srotola fino all'oggetto.
     */
    private fun decodeJsResult(raw: String?): JSONObject {
        if (raw.isNullOrBlank() || raw == "null") {
            return JSONObject().apply { put("error", "page returned no result") }
        }
        return try {
            var parsed: Any = JSONTokener(raw).nextValue()
            if (parsed is String) {
                try {
                    parsed = JSONTokener(parsed).nextValue()
                } catch (e: JSONException) {
                    // Il valore non era JSON annidato: resta la stringa.
                }
            }
            if (parsed is JSONObject) parsed
            else JSONObject().apply { put("error", "unexpected result type: ${parsed.javaClass.simpleName}") }
        } catch (e: JSONException) {
            JSONObject().apply { put("error", "invalid result from page: ${e.message}") }
        }
    }

    /**
     * Carica [url], attende la fine e valuta [js] sulla pagina risultante
     * (comportamento one-shot storico di search/fetch). Restituisce il valore
     * grezzo della callback, o un JSON object con `error`.
     */
    private fun evaluateOnPage(url: String, js: String, timeoutSeconds: Long): String {
        Log.d(TAG, "evaluateOnPage ENTER: url=$url timeout=$timeoutSeconds")
        val nav = navigateAndWait(url, timeoutSeconds)
        if (nav.optString("error").isNotEmpty()) {
            return nav.toString()
        }
        return evaluateOnCurrentPage(js, timeoutSeconds)
    }

    // JS di snapshot: testo visibile + controlli interattivi con selettore CSS
    // stabile. Viene eseguito nel contesto della pagina, quindi usa solo API
    // standard del browser (CSS.escape è supportato da Chrome da tempo).
    private val SNAPSHOT_JS = """
        (function() {
            function cssPath(el) {
                if (!el || el.nodeType !== 1) return '';
                if (el.id) return '#' + CSS.escape(el.id);
                var parts = [];
                var node = el;
                while (node && node.nodeType === 1 && parts.length < 10) {
                    var tag = node.tagName.toLowerCase();
                    if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
                    var parent = node.parentNode;
                    if (!parent || parent.nodeType !== 1) { parts.unshift(tag); break; }
                    var siblings = Array.prototype.filter.call(
                        parent.children, function (c) { return c.tagName === node.tagName; }
                    );
                    var idx = siblings.indexOf(node) + 1;
                    parts.unshift(tag + ':nth-of-type(' + idx + ')');
                    node = parent;
                }
                return parts.join(' > ');
            }
            function shortText(s, max) {
                s = (s || '').replace(/[\s\u00a0]+/g, ' ').trim();
                return s.length > max ? s.slice(0, max) + '…' : s;
            }
            var els = document.querySelectorAll(
                'a[href], button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [onclick]'
            );
            var out = [];
            for (var i = 0; i < els.length && out.length < $SNAPSHOT_MAX_ELEMENTS; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                var tag = el.tagName.toLowerCase();
                var item = { tag: tag, selector: cssPath(el) };
                var label = (el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent || '')
                    .replace(/[\s\u00a0]+/g, ' ').trim();
                if (label) item.label = shortText(label, 120);
                if (el.name) item.name = el.name;
                if (el.id) item.id = el.id;
                if (tag === 'a' && el.href) item.href = el.href;
                if ((tag === 'input' || tag === 'textarea') && 'value' in el) {
                    item.value = shortText(el.value, 80);
                }
                if (el.type) item.type = el.type;
                out.push(item);
            }
            return JSON.stringify({
                url: location.href,
                title: document.title || '',
                text: (document.body ? document.body.innerText : '').slice(0, $SNAPSHOT_MAX_TEXT_CHARS),
                elements: out
            });
        })()
    """.trimIndent()
}
