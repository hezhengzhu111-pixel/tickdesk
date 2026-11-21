package dev.tickdesk.core

import kotlinx.serialization.json.*
import java.net.URI
import java.util.Base64

data class CommandRule(
    val writes: Boolean,
    val duringRecovery: Boolean,
    val recoveryOnly: Boolean,
    val ackTimeoutMs: Long,
    val completionEvent: String?,
)

class BusinessMessage internal constructor(val json: JsonObject) {
    val kind = json.text("kind")!!
    val id = json.text("id")!!
    val dedupeKey = json.text("dedupeKey")!!
    val epoch = json.text("serverEpoch")
    val sequence = json.number("snapshotSeq")
    val timestamp = json.number("ts")!!
    val data = json.obj("data")!!
    val command = data.text("cmd")
    val clientMsgId = data.text("clientMsgId")
}

enum class FrameType { HELLO, WELCOME, PRESENCE, DELIVER, RECEIPT, ERROR }

data class DeliveryKey(val id: String, val hash: String, val from: String, val to: String) {
    internal fun fields(type: String) = buildJsonObject {
        put("v", 2)
        put("type", type)
        put("deliveryId", id)
        put("payloadHash", hash)
        put("fromIncarnation", from)
        put("toIncarnation", to)
    }
}

class RelayFrame internal constructor(val json: JsonObject, val message: BusinessMessage? = null) {
    val type = FrameType.valueOf(json.text("type")!!.uppercase())
    val delivery = json.text("deliveryId")?.let {
        DeliveryKey(it, json.text("payloadHash")!!, json.text("fromIncarnation")!!, json.text("toIncarnation")!!)
    }
    val bytes: String get() = CanonicalJson.encode(json)
}

/** Do not log this object or serialize it to saved UI state. */
class PairingConfig internal constructor(val relayUrl: String, val pairId: String, val pins: List<String>,
                                        val appToken: String) {
    fun encode(): String = CanonicalJson.encode(buildJsonObject {
        put("protocol", 2)
        put("relayUrl", relayUrl)
        put("pairId", pairId)
        put("pins", JsonArray(pins.map(::JsonPrimitive)))
        put("appToken", appToken)
    })
}

object Protocol {
    const val BUSINESS_LIMIT = 64 * 1024
    const val FRAME_LIMIT = 128 * 1024
    const val CHUNK_SIZE = 32768
    const val MAX_FILE_BYTES = 20 * 1024 * 1024
    val commands: Map<String, CommandRule> by lazy {
        contractResource("commands").array("commands")!!.associate {
            val item = it.jsonObject
            item.text("name")!! to CommandRule(
                item.text("mode") == "write", item.flag("allowedDuringRecovery")!!,
                item.flag("onlyDuringRecovery")!!, item.number("ackTimeoutMs")!!, item.text("completionEvent"),
            )
        }
    }

    fun business(raw: String): BusinessMessage {
        protocolCheck(raw.toByteArray(Charsets.UTF_8).size <= BUSINESS_LIMIT, "Business byte limit exceeded")
        return business(CanonicalJson.parse(raw))
    }

    fun business(value: JsonElement): BusinessMessage {
        WireSchema.verify(value, WireSchema.business)
        protocolCheck(CanonicalJson.encode(value).toByteArray(Charsets.UTF_8).size <= BUSINESS_LIMIT, "Business byte limit exceeded")
        return BusinessMessage(value.jsonObject).also(::verifyBusiness)
    }

    fun frame(raw: String): RelayFrame {
        protocolCheck(raw.toByteArray(Charsets.UTF_8).size <= FRAME_LIMIT, "Frame byte limit exceeded")
        val value = CanonicalJson.parse(raw)
        WireSchema.verify(value, WireSchema.relay)
        val json = value.jsonObject
        val payload = json["payload"]
        val business = when (json.text("category")) {
            "business" -> business(payload!!)
            "file_chunk" -> { chunkBytes(payload!!.jsonObject); null }
            else -> null
        }
        if (payload != null) {
            protocolCheck(CanonicalJson.hash(payload) == json.text("payloadHash"), "Payload hash mismatch")
        }
        json.obj("peer")?.let { peer ->
            val online = peer.flag("online")!!
            protocolCheck(online == (peer.text("incarnation") != null) &&
                online == (peer.text("serverEpoch") != null), "Invalid local presence")
        }
        return RelayFrame(json, business)
    }

    fun pairing(raw: String): PairingConfig {
        protocolCheck(raw.length <= 8192, "Configuration too large")
        val value = CanonicalJson.parse(raw)
        WireSchema.verify(value, WireSchema.pairing)
        val config = value.jsonObject
        val url = config.text("relayUrl")!!
        val uri = try { URI(url) } catch (_: Exception) { throw ProtocolException("Invalid relay address") }
        val host = uri.host?.removeSurrounding("[", "]").orEmpty()
        val ipv4 = host.split('.').let { fields -> fields.size == 4 &&
            fields.all { it.matches(Regex("0|[1-9][0-9]{0,2}")) && it.toInt() <= 255 } }
        val ipv6 = host.contains(':') && host.all { it in "0123456789abcdefABCDEF:." }
        protocolCheck((ipv4 || ipv6) && uri.rawUserInfo == null && uri.rawQuery == null &&
            uri.rawFragment == null && uri.port == 8443 && uri.rawPath == "/relay/v2/connect", "Invalid relay address")
        val token = config.text("appToken")!!
        val tokenBytes = Base64.getUrlDecoder().decode(token)
        protocolCheck(tokenBytes.size == 32 && Base64.getUrlEncoder().withoutPadding().encodeToString(tokenBytes) == token,
            "Invalid role token encoding")
        val pins = config.array("pins")!!.map { it.jsonPrimitive.content }
        for (pin in pins) protocolCheck(decodeBase64(pin.removePrefix("sha256/")).size == 32, "Invalid SPKI digest")
        return PairingConfig(url, config.text("pairId")!!, pins, token)
    }

    fun command(name: String, eventId: String, clientId: String, epoch: String?, at: Long,
                params: JsonObject = JsonObject(emptyMap())): BusinessMessage {
        protocolCheck(params.keys.none { it in setOf("cmd", "clientMsgId", "serverEpoch") }, "Reserved command field")
        return business(buildJsonObject {
            put("v", 2)
            put("kind", "command")
            put("id", eventId)
            put("dedupeKey", "command:$clientId")
            put("ts", at)
            put("serverEpoch", epoch)
            putJsonObject("data") {
                put("cmd", name)
                put("clientMsgId", clientId)
                put("serverEpoch", epoch)
                params.forEach { (key, item) -> put(key, item) }
            }
        })
    }

    fun deliver(message: BusinessMessage, id: String, from: String, to: String): RelayFrame {
        val key = DeliveryKey(id, CanonicalJson.hash(message.json), from, to)
        return frame(CanonicalJson.encode(JsonObject(key.fields("deliver") + mapOf(
            "category" to JsonPrimitive("business"), "payload" to message.json,
        ))))
    }

    fun receipt(key: DeliveryKey): String = CanonicalJson.encode(key.fields("receipt"))

    fun deliverChunk(transferId: String, fileId: String, index: Int, totalChunks: Int, bytes: ByteArray,
                     id: String, from: String, to: String): RelayFrame =
        deliverChunk(buildJsonObject {
            put("transferId", transferId); put("fileId", fileId); put("index", index); put("totalChunks", totalChunks)
            put("chunkHash", CanonicalJson.sha256(bytes)); put("bytes", Base64.getEncoder().encodeToString(bytes))
        }, id, from, to)

    internal fun deliverChunk(payload: JsonObject, id: String, from: String, to: String): RelayFrame {
        val key = DeliveryKey(id, CanonicalJson.hash(payload), from, to)
        return frame(CanonicalJson.encode(JsonObject(key.fields("deliver") + mapOf(
            "category" to JsonPrimitive("file_chunk"), "payload" to payload,
        ))))
    }

    fun chunkBytes(chunk: JsonObject): ByteArray {
        val bytes = decodeBase64(chunk.text("bytes")!!)
        val index = chunk.number("index")!!
        val count = chunk.number("totalChunks")!!
        protocolCheck(index < count && bytes.size in 1..CHUNK_SIZE &&
            (index == count - 1 || bytes.size == CHUNK_SIZE), "Invalid chunk bounds")
        protocolCheck(CanonicalJson.sha256(bytes) == chunk.text("chunkHash"), "Chunk hash mismatch")
        return bytes
    }

    private fun decodeBase64(raw: String): ByteArray {
        val bytes = try { Base64.getDecoder().decode(raw) } catch (_: IllegalArgumentException) {
            throw ProtocolException("Invalid base64")
        }
        protocolCheck(Base64.getEncoder().encodeToString(bytes) == raw, "Invalid base64 padding")
        return bytes
    }

    private fun verifyBusiness(message: BusinessMessage) {
        val data = message.data
        when (message.kind) {
            "command" -> {
                protocolCheck(message.epoch == data.text("serverEpoch"), "Command epoch mismatch")
                protocolCheck(message.epoch != null || commands.getValue(message.command!!).let { !it.writes || it.duringRecovery },
                    "Missing write epoch")
            }
            "state_snapshot" -> {
                protocolCheck(message.epoch == data.text("serverEpoch") &&
                    message.sequence == data.number("snapshotSeq"), "Snapshot envelope mismatch")
                protocolCheck(SnapshotAssembly.collections.keys.sumOf { data.array(it)!!.size } <= 20,
                    "Too many snapshot items")
            }
        }
    }
}
