plugins { alias(libs.plugins.kotlin.jvm) }
kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
java { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
sourceSets.main {
    resources.srcDir(rootProject.file("contracts"))
    resources.exclude("**/*fixtures.json", "**/*vectors.json", "**/*.example.json")
}
sourceSets.test { resources.srcDir(rootProject.file("contracts")) }
dependencies {
    implementation(libs.serialization.json)
    implementation(libs.coroutines.core)
    testImplementation(libs.junit)
}
