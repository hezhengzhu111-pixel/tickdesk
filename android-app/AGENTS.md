# Android development

Read README.md, contracts/v2 and, when in the parent workspace, docs/design/android-app.md and docs/11-三端接口契约.md.
Keep Kotlin core independent of Android UI. UI observes ViewModel/Repository state; do not declare business success from a transport receipt.
Preserve five main destinations, readable dark design, 48dp touch targets, unknown values and offline/conflict states. Do not put sample holdings into the production app.
No hardcoded role tokens, insecure TLS overrides, automatic trading or unsupported full-screen notification entitlement.
Verify gradlew :core:test :mobile:assembleDebug :mobile:lintDebug. Keep SDK paths in local environment/local.properties only. Canonical contract edits are exported from the parent workspace, never made in this snapshot.
