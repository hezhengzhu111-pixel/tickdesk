package dev.tickdesk.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

object ContractCatalog {
    const val VERSION = "2.0.0"
    const val PROTOCOL_MAJOR = 2
    fun definedCommands(): Set<String> {
        val input = checkNotNull(javaClass.getResourceAsStream("/v2/commands.json")) { "Missing bundled v2 contract" }
        val catalog = input.bufferedReader(Charsets.UTF_8).use { Json.parseToJsonElement(it.readText()).jsonObject }
        check(catalog.getValue("contractVersion").jsonPrimitive.content == VERSION) { "Unsupported contract version" }
        val commands = catalog.getValue("commands").jsonArray.map { it.jsonObject.getValue("name").jsonPrimitive.content }
        check(commands.size >= 42 && commands.toSet().size == commands.size) { "Incomplete or duplicated command catalog" }
        return commands.toSet()
    }
    val implementedCommands: Set<String> = emptySet()
}
