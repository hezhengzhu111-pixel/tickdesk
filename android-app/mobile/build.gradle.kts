plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
}
val mockDemo = providers.gradleProperty("tickdeskMockDemo").orNull == "true"
android {
    namespace = "dev.tickdesk.app"
    compileSdk = 36
    defaultConfig {
        applicationId = if (mockDemo) "dev.tickdesk.app.demo" else "dev.tickdesk.app"
        buildConfigField("boolean", "MOCK_DEMO", mockDemo.toString())
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    buildFeatures { compose = true; buildConfig = true }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    buildTypes { release { isMinifyEnabled = false } }
    lint { abortOnError = true }
    sourceSets["androidTest"].assets.srcDir(rootProject.file("contracts"))
    // Opt-in local USB certificate trust; release and ordinary debug are unchanged.
    if (mockDemo || providers.gradleProperty("tickdeskUsbIntegration").orNull == "true") {
        sourceSets["debug"].manifest.srcFile("src/usbDebug/AndroidManifest.xml")
        sourceSets["debug"].res.srcDir("src/usbDebug/res")
    }
    if (mockDemo) sourceSets["debug"].res.srcDir("src/mockDemo/res")
}
kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
dependencies {
    implementation(project(":core"))
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.material3)
    implementation(libs.compose.ui.preview)
    debugImplementation(libs.compose.ui.tooling)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.serialization.json)
    implementation(libs.room.runtime)
    implementation(libs.okhttp)
    implementation(libs.zxing)
    implementation(libs.work.runtime)
    ksp(libs.room.compiler)
    androidTestImplementation(libs.android.test.runner)
    androidTestImplementation(libs.junit)
    androidTestImplementation(libs.okhttp.mockwebserver)
    androidTestImplementation(libs.okhttp.tls)
}
ksp { arg("room.schemaLocation", project.file("schemas").path) }
