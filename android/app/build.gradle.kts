import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// ── Release signing credentials ───────────────────────────────────────────────
// Never stored in the repo. Read from the environment (CI) or, failing that,
// from a gitignored `android/keystore.properties` (local release builds):
//
//   storeFile=/absolute/path/to/jenny-release.jks
//   storePassword=...
//   keyAlias=jenny
//   keyPassword=...
//
// If neither source provides a full set, the release build is left UNSIGNED
// rather than failing: `assembleRelease` must keep working for anyone who only
// wants to reproduce and inspect the artifact.
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) {
        keystorePropsFile.inputStream().use { load(it) }
    }
}

fun signingCredential(envName: String, propName: String): String? =
    (System.getenv(envName) ?: keystoreProps.getProperty(propName))?.takeIf { it.isNotBlank() }

val releaseStoreFile = signingCredential("JENNY_KEYSTORE_PATH", "storeFile")
val releaseStorePassword = signingCredential("JENNY_KEYSTORE_PASSWORD", "storePassword")
val releaseKeyAlias = signingCredential("JENNY_KEY_ALIAS", "keyAlias")
val releaseKeyPassword = signingCredential("JENNY_KEY_PASSWORD", "keyPassword")

val hasReleaseSigning = releaseStoreFile != null &&
    releaseStorePassword != null &&
    releaseKeyAlias != null &&
    releaseKeyPassword != null &&
    file(releaseStoreFile!!).exists()

android {
    namespace = "com.flagdizero.jenny"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.flagdizero.jenny"
        minSdk = 26
        targetSdk = 34
        // versionCode must increase monotonically on every published build.
        // versionName tracks the Python package version in pyproject.toml —
        // keep the two in sync when releasing.
        versionCode = 13
        versionName = "0.7.4"

        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
        }
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = file(releaseStoreFile!!)
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
                // Schemi di firma dichiarati esplicitamente invece di lasciare i
                // default di AGP, che dipendono dal minSdk e cambiano fra versioni.
                // v1 (JAR signing) serve solo sotto API 24: il minSdk è 26, quindi
                // è peso morto nell'APK. v2 copre tutto il parco supportato. v3 è
                // additivo (i dispositivi 28+ lo usano, 26-27 ricadono su v2) e
                // porta il supporto alla rotazione della chiave di firma.
                enableV1Signing = false
                enableV2Signing = true
                enableV3Signing = true
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // null when no credentials were supplied → unsigned APK (see the
            // comment on the credential block above).
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    packaging {
        resources {
            // jsch e bcprov sono entrambi multi-release jar e spediscono lo
            // stesso metadata OSGi sotto piu cartelle di versione (9, 15, …):
            // due input con lo stesso path fanno FALLIRE
            // mergeReleaseJavaResource. Serve il glob e non il path esatto,
            // altrimenti il build fallisce di nuovo alla cartella successiva.
            // Sono metadata per l'OSGi runtime, che su Android non esiste:
            // escluderli non toglie nulla (le classi dei multi-release jar non
            // passano da qui, le dexa D8).
            excludes += "/META-INF/versions/**/OSGI-INF/**"
        }
    }

    kotlinOptions {
        jvmTarget = "11"
    }

    sourceSets {
        getByName("main") {
            // `builtBy` dichiara i produttori sulla FileCollection e serve a
            // Gradle 8.9, che altrimenti fa FALLIRE `assembleRelease`:
            // `generateReleaseLintVitalReportModel` legge questa cartella senza
            // dipendere da chi la scrive (lintVital gira solo sul release, ed è
            // il motivo per cui `assembleDebug` non mostra il problema).
            // ATTENZIONE: da solo NON basta. AGP legge `assets.srcDirs` come
            // semplici percorsi e la dipendenza dichiarata qui va perduta, così
            // i due Copy non entrano nel grafo e l'APK imbarca in silenzio
            // l'ultimo contenuto rimasto in build/ — o niente affatto su un
            // clone pulito. Il gancio che li fa girare davvero è su `preBuild`,
            // più sotto: non toccare l'uno senza l'altro.
            assets.srcDirs(
                files("$buildDir/generated/assets")
                    .builtBy("copyScriptAssets", "copyPackageSourceAssets")
            )
        }
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            install("-r", "../../requirements-android.lock.txt")
        }
    }
    sourceSets {
        maybeCreate("main").apply {
            srcDir("../../")
            include("jenny/**")
        }
    }
}

// Warn about an unsigned release only when a release build was actually
// requested — a configuration-time warning would fire on every debug build too.
gradle.taskGraph.whenReady {
    val buildingRelease = allTasks.any { it.name.contains("Release") }
    if (buildingRelease && !hasReleaseSigning) {
        logger.warn(
            "\n[jenny] WARNING: release signing credentials not found — the APK " +
                "will be UNSIGNED and cannot be installed on a device.\n" +
                "[jenny] Set JENNY_KEYSTORE_PATH / JENNY_KEYSTORE_PASSWORD / " +
                "JENNY_KEY_ALIAS / JENNY_KEY_PASSWORD, or create " +
                "android/keystore.properties (see app/build.gradle.kts).\n"
        )
    }
}

// Copy skill scripts as raw Android assets (Chaquopy compiles .py files
// into .imy, making them unreadable via importlib.resources. By also
// mirroring them as assets, scripts remain extractable at runtime.)
val copyScriptAssets by tasks.registering(Copy::class) {
    from("../../jenny/skills") {
        include("**/scripts/*.py")
    }
    into("$buildDir/generated/assets/skills")
}

// Mirror the whole jenny package as plain .py assets so the agent can
// read its own source on-device (extracted at gateway startup by
// jenny.utils.android_assets.extract_jenny_source).
val copyPackageSourceAssets by tasks.registering(Copy::class) {
    from("../../jenny") {
        include("**/*.py")
        exclude("**/__pycache__/**")
    }
    into("$buildDir/generated/assets/jenny_src/jenny")
}

// I due Copy sopra devono girare prima di QUALUNQUE consumatore della cartella
// generata: `mergeAssets`, ma anche `generate*LintVitalReportModel`. Agganciarli
// per nome uno per uno è fragile (AGP ne aggiunge di nuovi tra le versioni);
// `preBuild` è l'ancora che precede l'intera pipeline della variante, quindi li
// copre tutti, presenti e futuri. Senza questo blocco il build riesce comunque,
// ma silenziosamente con gli asset vecchi: è già capitato.
tasks.named("preBuild") {
    dependsOn(copyScriptAssets, copyPackageSourceAssets)
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    // Chrome Custom Tabs: apre i link esterni della chat in un browser
    // in-app (con pulsante di chiusura) invece di dirottare la WebView SPA.
    implementation("androidx.browser:browser:1.7.0")

    // WorkManager: rete di sicurezza anti-doze indipendente dalle sveglie
    // (GatewayWorker). Gira sul backend JobScheduler, e i gestori batteria dei
    // produttori sono molto piu restii a interferire con un concetto di sistema
    // che con un service nudo. Ferma alla 2.9.x di proposito: dalla 2.10 in su
    // WorkManager richiede compileSdk 35, qui siamo a 34.
    implementation("androidx.work:work-runtime-ktx:2.9.1")

    // SPIKE SSH — client SSH nativo. jsch e puro Java e client-only.
    // BouncyCastle NON e opzionale su Android: X25519 e entrato in Conscrypt
    // solo con Android 14 e qui il minSdk e 26, quindi senza BC lo scambio di
    // chiavi curve25519-sha256 (quello che ogni server moderno negozia) e
    // Ed25519 non sono disponibili. Vedi SshBridge.kt e proguard-rules.pro.
    implementation("com.github.mwiede:jsch:2.28.6")
    implementation("org.bouncycastle:bcprov-jdk18on:1.85")
}
