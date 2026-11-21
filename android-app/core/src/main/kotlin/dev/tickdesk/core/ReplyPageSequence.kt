package dev.tickdesk.core

import kotlinx.serialization.json.*
import java.nio.charset.CharacterCodingException
import java.security.MessageDigest

data class FullReplyText(val pages: List<String>, val sources: List<JsonObject>)

/** Collects contiguous Unicode code points. The full text is returned only after its UTF-8 digest matches. */
class ReplyPageSequence(private val requestId: String) {
    private val digest = MessageDigest.getInstance("SHA-256")
    private val pages = mutableListOf<String>()
    private var sources = emptyList<JsonObject>()
    private var expectedHash: String? = null
    private var complete = false

    var loadedChars: Long = 0
        private set
    var totalChars: Long? = null
        private set

    fun request(): JsonObject = buildJsonObject {
        protocolCheck(!complete, "Reply is already complete")
        put("requestId", requestId)
        expectedHash?.let {
            put("offset", loadedChars)
            put("sha256", it)
        }
    }

    fun append(page: JsonObject): FullReplyText? {
        protocolCheck(!complete && page.text("requestId") == requestId && page.number("offset") == loadedChars,
            "Reply page belongs to another request or offset")
        val total = page.number("totalChars") ?: throw ProtocolException("Missing reply length")
        val hash = page.text("sha256") ?: throw ProtocolException("Missing reply digest")
        protocolCheck(total in loadedChars..CanonicalJson.MAX_INTEGER && (totalChars == null || totalChars == total),
            "Reply length changed")
        protocolCheck(sha256.matches(hash) && (expectedHash == null || expectedHash == hash), "Reply digest changed")
        val text = page.text("text") ?: throw ProtocolException("Missing reply page text")
        val count = text.codePointCount(0, text.length).toLong()
        protocolCheck(count <= total - loadedChars && (count > 0L || total == 0L), "Reply page did not make progress")
        val end = loadedChars + count
        val last = end == total
        protocolCheck(if (last) page["nextOffset"] == JsonNull else page.number("nextOffset") == end,
            "Reply page continuation is not contiguous")
        val pageSources = page.array("sources") ?: throw ProtocolException("Missing reply sources")
        protocolCheck(pageSources.all { it is JsonObject } && (loadedChars == 0L || pageSources.isEmpty()),
            "Sources must come from the first reply page")
        val utf8 = try { text.encodeToByteArray(throwOnInvalidSequence = true) }
        catch (_: CharacterCodingException) { throw ProtocolException("Invalid reply Unicode") }

        digest.update(utf8)
        if (loadedChars == 0L) sources = pageSources.map { it.jsonObject }
        pages.add(text)
        expectedHash = hash
        totalChars = total
        loadedChars = end
        if (!last) return null
        complete = true
        protocolCheck(CanonicalJson.hexadecimal(digest.digest()) == hash, "Full reply failed digest verification")
        return FullReplyText(pages.toList(), sources)
    }

    private companion object {
        val sha256 = Regex("[a-f0-9]{64}")
    }
}
