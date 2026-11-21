package dev.tickdesk.core

import kotlinx.serialization.json.*
import java.time.LocalDate
import java.time.format.DateTimeParseException
import java.util.concurrent.ConcurrentHashMap

internal fun contractResource(name: String): JsonObject =
    checkNotNull(ContractCatalog::class.java.getResourceAsStream("/v2/$name.json"))
        .bufferedReader(Charsets.UTF_8).use { Json.parseToJsonElement(it.readText()).jsonObject }

/**
 * Draft 2020-12 keywords used by the bundled v2 schemas. References are resolved
 * exclusively against packaged documents. Unsupported keywords fail closed.
 */
internal object WireSchema {
    val business by lazy { contractResource("business.schema") }
    val relay by lazy { contractResource("relay.schema") }
    val pairing by lazy { contractResource("pairing.schema") }
    private val regexes = ConcurrentHashMap<String, Regex>()
    private val keywords = setOf(
        "\$schema", "\$id", "\$defs", "\$ref", "title", "type", "required",
        "properties", "additionalProperties", "dependentRequired", "enum", "const",
        "minLength", "maxLength", "pattern", "format", "minItems", "maxItems", "items",
        "uniqueItems", "minimum", "maximum", "oneOf", "anyOf", "allOf", "not", "if", "then", "else",
    )

    fun verify(value: JsonElement, root: JsonObject, definition: String? = null) {
        val schema = if (definition == null) root else root.getValue("\$defs").jsonObject.getValue(definition).jsonObject
        protocolCheck(matches(value, schema, root), "Value does not match v2 schema")
    }

    private fun matches(value: JsonElement, schema: JsonObject, root: JsonObject): Boolean {
        check(keywords.containsAll(schema.keys)) { "Unsupported keyword in bundled contract" }
        // Skip unrelated command/result branches before examining large nested objects.
        if (schema["enum"] != null && value !in schema.getValue("enum").jsonArray) return false
        if (schema["const"] != null && value != schema["const"]) return false
        if (schema.text("type")?.let { !hasType(value, it) } == true) return false
        if (value is JsonObject && !objectMatches(value, schema, root)) return false
        if (value is JsonArray && !arrayMatches(value, schema, root)) return false
        if (value is JsonPrimitive && value.isString && !stringMatches(value.content, schema)) return false
        if (value is JsonPrimitive && !value.isString) {
            value.longOrNull?.let {
                if (it < (schema.number("minimum") ?: Long.MIN_VALUE) ||
                    it > (schema.number("maximum") ?: Long.MAX_VALUE)) return false
            }
        }
        schema.text("\$ref")?.let { ref ->
            val (targetRoot, target) = when {
                ref.startsWith("#/\$defs/") -> root to root.getValue("\$defs").jsonObject.getValue(ref.substringAfterLast('/')).jsonObject
                ref == business.text("\$id") -> business to business
                else -> error("Unrecognized bundled schema reference")
            }
            if (!matches(value, target, targetRoot)) return false
        }
        if (schema.array("allOf")?.all { matches(value, it.jsonObject, root) } == false) return false
        if (schema.array("anyOf")?.any { matches(value, it.jsonObject, root) } == false) return false
        if (schema.array("oneOf")?.count { matches(value, it.jsonObject, root) }?.let { it != 1 } == true) return false
        if (schema.obj("not")?.let { matches(value, it, root) } == true) return false
        schema.obj("if")?.let { condition ->
            val branch = if (matches(value, condition, root)) "then" else "else"
            if (schema.obj(branch)?.let { !matches(value, it, root) } == true) return false
        }
        return true
    }

    private fun objectMatches(value: JsonObject, schema: JsonObject, root: JsonObject): Boolean {
        if (schema.array("required")?.any { it.jsonPrimitive.content !in value } == true) return false
        val properties = schema.obj("properties").orEmpty()
        if (schema.flag("additionalProperties") == false && !properties.keys.containsAll(value.keys)) return false
        if (schema.obj("dependentRequired")?.any { (key, dependencies) ->
                key in value && dependencies.jsonArray.any { it.jsonPrimitive.content !in value }
            } == true) return false
        return properties.all { (key, property) ->
            value[key]?.let { matches(it, property.jsonObject, root) } ?: true
        }
    }

    private fun arrayMatches(value: JsonArray, schema: JsonObject, root: JsonObject): Boolean {
        if (!within(value.size, schema, "minItems", "maxItems")) return false
        if (schema.flag("uniqueItems") == true && value.toSet().size != value.size) return false
        val item = schema.obj("items") ?: return true
        return value.all { matches(it, item, root) }
    }

    private fun stringMatches(value: String, schema: JsonObject): Boolean {
        if (!within(value.codePointCount(0, value.length), schema, "minLength", "maxLength")) return false
        schema.text("pattern")?.let { pattern ->
            val regex = regexes.computeIfAbsent(pattern, ::Regex)
            val matched = if (pattern.startsWith('^') && pattern.endsWith('$')) regex.matches(value)
                else regex.containsMatchIn(value)
            if (!matched) return false
        }
        return when (schema.text("format")) {
            null -> true
            "date" -> try {
                value.length == 10 && LocalDate.parse(value).year > 0
            } catch (_: DateTimeParseException) { false }
            else -> error("Unrecognized bundled schema format")
        }
    }

    private fun within(size: Int, schema: JsonObject, min: String, max: String) =
        size >= (schema.number(min) ?: 0) && size <= (schema.number(max) ?: Long.MAX_VALUE)

    private fun hasType(value: JsonElement, type: String): Boolean = when (type) {
        "object" -> value is JsonObject
        "array" -> value is JsonArray
        "null" -> value === JsonNull
        "string" -> value is JsonPrimitive && value.isString
        "integer" -> value is JsonPrimitive && !value.isString && value.longOrNull != null
        "boolean" -> value is JsonPrimitive && !value.isString && value.booleanOrNull != null
        else -> error("Unrecognized bundled schema type")
    }
}
