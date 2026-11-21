package dev.tickdesk.core

import kotlinx.serialization.json.JsonObject

interface WorkbenchTransaction {
    suspend fun get(scope: String, group: String, id: String): JsonObject?
    suspend fun list(scope: String, group: String): Map<String, JsonObject>
    suspend fun put(scope: String, group: String, id: String, value: JsonObject)
    suspend fun remove(scope: String, group: String, id: String)
    suspend fun clear(scope: String, group: String)
}

interface WorkbenchStore {
    /** The callback either commits all writes or publishes none of them. */
    suspend fun <T> transaction(block: suspend WorkbenchTransaction.() -> T): T
}
