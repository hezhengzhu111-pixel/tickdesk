plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.compose) apply false
}

// Keep USB integration outputs separate from ordinary App development builds.
if (providers.gradleProperty("tickdeskMockDemo").orNull == "true") {
    allprojects {
        layout.buildDirectory.set(layout.projectDirectory.dir("build/mock-demo"))
    }
} else if (providers.gradleProperty("tickdeskUsbIntegration").orNull == "true") {
    allprojects {
        layout.buildDirectory.set(layout.projectDirectory.dir("build/usb-integration"))
    }
}
