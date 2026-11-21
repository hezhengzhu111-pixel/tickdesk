package dev.tickdesk.core

/** Shared by RelayClient retry decisions and background monitoring, so they cannot disagree. */
object ConnectionFailure {
    fun isTerminal(reason: String?): Boolean = reason in terminalReasons

    private val terminalReasons = setOf(
        "FRAME_LIMIT", "BINARY_FRAME", "CREDENTIAL_REVOKED", "SESSION_REPLACED", "PROTOCOL_MISMATCH",
        "TLS_VALIDATION", "UNEXPECTED_REDIRECT", "PAIR_MISMATCH", "INVALID_RELAY_FRAME", "PAIRING_KEY_UNAVAILABLE",
    )
}
