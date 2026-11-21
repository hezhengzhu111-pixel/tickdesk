package dev.tickdesk.core

import kotlinx.serialization.json.*
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.security.MessageDigest

class ProtocolException(message: String) : IllegalArgumentException(message)

internal fun protocolCheck(condition: Boolean, message: String) {
    if (!condition) throw ProtocolException(message)
}

fun JsonObject.text(key: String): String? = (this[key] as? JsonPrimitive)
    ?.takeIf { it.isString }?.content
fun JsonObject.number(key: String): Long? = (this[key] as? JsonPrimitive)
    ?.takeUnless { it.isString }?.longOrNull
fun JsonObject.flag(key: String): Boolean? = (this[key] as? JsonPrimitive)
    ?.takeUnless { it.isString }?.booleanOrNull
fun JsonObject.obj(key: String): JsonObject? = this[key] as? JsonObject
fun JsonObject.array(key: String): JsonArray? = this[key] as? JsonArray

object CanonicalJson {
    const val MAX_INTEGER = 9007199254740991L
    private val integer = Regex("0|-?[1-9][0-9]*")
    private val hex = "0123456789abcdef"

    fun decode(bytes: ByteArray): JsonElement {
        val raw = try {
            Charsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString()
        } catch (_: CharacterCodingException) {
            throw ProtocolException("Invalid UTF-8")
        }
        return parse(raw)
    }

    fun parse(raw: String): JsonElement = Parser(raw).read()

    fun encode(value: JsonElement): String = buildString { appendValue(value, 0) }

    fun hash(value: JsonElement): String = sha256(encode(value).toByteArray(Charsets.UTF_8))

    fun sha256(bytes: ByteArray): String = hexadecimal(MessageDigest.getInstance("SHA-256").digest(bytes))

    internal fun hexadecimal(bytes: ByteArray): String = buildString(bytes.size * 2) {
        for (octet in bytes) {
            append(hex[(octet.toInt() and 255) ushr 4])
            append(hex[octet.toInt() and 15])
        }
    }

    private fun checkedInteger(raw: String): Long {
        val result = raw.toLongOrNull() ?: throw ProtocolException("Invalid JSON integer")
        protocolCheck(integer.matches(raw) && result in -MAX_INTEGER..MAX_INTEGER, "Invalid JSON integer")
        return result
    }

    private fun scalarString(value: String) {
        var index = 0
        while (index < value.length) {
            val c = value[index++]
            if (c.isHighSurrogate()) {
                protocolCheck(index < value.length && value[index++].isLowSurrogate(), "Invalid Unicode")
            } else {
                protocolCheck(!c.isLowSurrogate(), "Invalid Unicode")
            }
        }
    }

    private fun asciiKey(key: String) = protocolCheck(key.all { it.code < 128 }, "Non-ASCII object key")

    private fun StringBuilder.appendValue(value: JsonElement, depth: Int) {
        protocolCheck(depth <= 64, "JSON nesting limit exceeded")
        when (value) {
            is JsonObject -> {
                append('{')
                value.entries.sortedBy { it.key }.forEachIndexed { i, (key, field) ->
                    if (i > 0) append(',')
                    asciiKey(key)
                    appendString(key)
                    append(':')
                    appendValue(field, depth + 1)
                }
                append('}')
            }
            is JsonArray -> {
                append('[')
                value.forEachIndexed { i, item ->
                    if (i > 0) append(',')
                    appendValue(item, depth + 1)
                }
                append(']')
            }
            is JsonPrimitive -> when {
                value.isString -> appendString(value.content)
                value === JsonNull || value.content == "true" || value.content == "false" -> append(value.content)
                else -> append(checkedInteger(value.content))
            }
        }
    }

    private fun StringBuilder.appendString(value: String) {
        scalarString(value)
        append('"')
        for (c in value) when (c) {
            '"' -> append("\\\"")
            '\\' -> append("\\\\")
            '\b' -> append("\\b")
            '\u000c' -> append("\\f")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            '\t' -> append("\\t")
            else -> if (c.code < 32) {
                append("\\u00")
                append(hex[c.code ushr 4])
                append(hex[c.code and 15])
            } else append(c)
        }
        append('"')
    }

    private class Parser(private val raw: String) {
        private var index = 0
        private fun peek() = raw.getOrNull(index)
        private fun take(c: Char): Boolean {
            if (peek() != c) return false
            index++
            return true
        }
        private fun need(c: Char) = protocolCheck(take(c), "Invalid JSON syntax")
        private fun spaces() {
            while (peek() in listOf(' ', '\t', '\r', '\n')) index++
        }

        fun read(): JsonElement {
            val item = value(0)
            spaces()
            protocolCheck(index == raw.length, "Trailing JSON")
            return item
        }

        private fun value(depth: Int): JsonElement {
            protocolCheck(depth <= 64, "JSON nesting limit exceeded")
            spaces()
            return when (peek()) {
                '{' -> objectValue(depth)
                '[' -> arrayValue(depth)
                '"' -> JsonPrimitive(string())
                't' -> literal("true", JsonPrimitive(true))
                'f' -> literal("false", JsonPrimitive(false))
                'n' -> literal("null", JsonNull)
                '-', in '0'..'9' -> {
                    val start = index
                    take('-')
                    while (peek() in '0'..'9') index++
                    JsonPrimitive(checkedInteger(raw.substring(start, index)))
                }
                else -> throw ProtocolException("Invalid JSON value")
            }
        }

        private fun literal(expected: String, result: JsonElement): JsonElement {
            protocolCheck(raw.startsWith(expected, index), "Invalid JSON literal")
            index += expected.length
            return result
        }

        private fun objectValue(depth: Int): JsonObject {
            need('{')
            spaces()
            val fields = linkedMapOf<String, JsonElement>()
            if (!take('}')) {
                do {
                    spaces()
                    val key = string()
                    asciiKey(key)
                    protocolCheck(!fields.containsKey(key), "Duplicate object key")
                    spaces()
                    need(':')
                    fields[key] = value(depth + 1)
                    spaces()
                } while (take(','))
                need('}')
            }
            return JsonObject(fields)
        }

        private fun arrayValue(depth: Int): JsonArray {
            need('[')
            spaces()
            val items = mutableListOf<JsonElement>()
            if (!take(']')) {
                do {
                    items.add(value(depth + 1))
                    spaces()
                } while (take(','))
                need(']')
            }
            return JsonArray(items)
        }

        private fun string(): String {
            need('"')
            val result = buildString {
                while (!take('"')) {
                    val c = peek() ?: throw ProtocolException("Unterminated JSON string")
                    index++
                    protocolCheck(c.code >= 32, "Unescaped control character")
                    append(if (c == '\\') escape() else c)
                }
            }
            scalarString(result)
            return result
        }

        private fun escape(): Char {
            val c = peek() ?: throw ProtocolException("Invalid JSON escape")
            index++
            return when (c) {
                '"', '\\', '/' -> c
                'b' -> '\b'
                'f' -> '\u000c'
                'n' -> '\n'
                'r' -> '\r'
                't' -> '\t'
                'u' -> {
                    val digits = raw.substring(index, minOf(index + 4, raw.length))
                    protocolCheck(digits.length == 4 && digits.all { it in hex || it in 'A'..'F' }, "Invalid Unicode escape")
                    index += 4
                    digits.toInt(16).toChar()
                }
                else -> throw ProtocolException("Invalid JSON escape")
            }
        }
    }
}
