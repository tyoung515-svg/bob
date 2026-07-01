plugins {
    id("org.jetbrains.kotlin.jvm")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.compose")
    application
}

application {
    mainClass.set("com.bobclaw.MainKt")
}

dependencies {
    implementation(project(":shared"))
    implementation(compose.desktop.currentOs)
    implementation(compose.material3)
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.9.0")
    // Embedded Chromium (JCEF via jcefmaven) for the artifact/canvas pane — desktop only.
    // Pulls NO Compose/skiko transitively (unlike the cwm wrapper), so it can't clash with
    // our pinned Compose 1.6.11. Downloads its own native binaries on first run.
    implementation("me.friwi:jcefmaven:146.0.10")
    // JNA — used by WindowTheme to tint the native Win11 caption (DWMWA_CAPTION_COLOR). Already
    // resolved transitively via jcefmaven (5.6.0 in cache); pinned explicit so the import is
    // deterministic on the compile classpath. Desktop-only; commonMain stays JNA-free.
    implementation("net.java.dev.jna:jna-platform:5.6.0")
}

// JCEF on Java 17 needs these exports (AWT/Java2D interop for the embedded surface).
tasks.named<JavaExec>("run") {
    jvmArgs("--add-exports", "java.base/java.lang=ALL-UNNAMED")
    jvmArgs("--add-exports", "java.desktop/sun.awt=ALL-UNNAMED")
    jvmArgs("--add-exports", "java.desktop/sun.java2d=ALL-UNNAMED")
}

// Headless E2E smoke of the KMM networking layer (RestClient/AuthManager/BoBClawWebSocket
// + kotlinx serialization) against the LIVE gateway — no Compose/GUI. See SmokeMain.kt.
// Run with --no-daemon so BC_PASSWORD / BC_TOTP env vars propagate to the forked JVM:
//   ./gradlew --no-daemon :desktopApp:smoke
tasks.register<JavaExec>("smoke") {
    group = "verification"
    description = "Headless login + faces + conversations + WS chat vs the live gateway"
    mainClass.set("com.bobclaw.SmokeMainKt")
    classpath = sourceSets["main"].runtimeClasspath
}
