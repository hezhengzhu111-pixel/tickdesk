package dev.tickdesk.core

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class ReplyPageSequenceTest {
    private val first = "开头😀" + "甲".repeat(10_000)
    private val middle = "𠮷\n" + "乙".repeat(10_000)
    private val last = "e\u0301\n" + "丙".repeat(10_000) + "末尾🔎"
    private val full = first + middle + last
    private val hash = CanonicalJson.sha256(full.encodeToByteArray())
    private val source = buildJsonObject { put("name", "来源"); put("ref", "local:fixture"); put("asOf", 1) }

    private fun String.scalars() = codePointCount(0, length).toLong()

    private fun page(text: String, at: Long, next: Long?, sha: String = hash, id: String = "reply", sources: List<JsonObject> = emptyList()) =
        buildJsonObject {
            put("requestId", id); put("offset", at); put("nextOffset", next)
            put("totalChars", full.scalars()); put("sha256", sha); put("text", text)
            put("sources", JsonArray(sources))
        }

    @Test fun preservesAllUnicodeBeyondOneFrameAndVerifiesRawUtf8() {
        val result = ReplyPageSequence("reply")
        assertEquals("reply", result.request().text("requestId"))
        assertNull(result.request().text("sha256"))
        assertNull(result.append(page(first, 0, first.scalars(), sources = listOf(source))))
        assertEquals(first.scalars(), result.request().number("offset"))
        assertEquals(hash, result.request().text("sha256"))
        val next = first.scalars() + middle.scalars()
        assertNull(result.append(page(middle, first.scalars(), next)))
        val verified = result.append(page(last, next, null))!!
        assertEquals(full, verified.pages.joinToString(""))
        assertEquals(listOf(source), verified.sources)
    }

    @Test fun rejectsWrongReplyUtf16OffsetsGapsAndMissingContinuation() {
        assertThrows(ProtocolException::class.java) {
            ReplyPageSequence("reply").append(page(first, 0, first.scalars(), id = "other"))
        }
        for (wrong in listOf(0L, first.length.toLong(), first.scalars() + 4, null)) {
            assertThrows(ProtocolException::class.java) { ReplyPageSequence("reply").append(page(first, 0, wrong)) }
        }
        assertThrows(ProtocolException::class.java) {
            ReplyPageSequence("reply").append(page("\ud800", 0, 1))
        }
    }

    @Test fun rejectsChangedVersionOrAppendedSourceAndWithholdsCorruptText() {
        val next = first.scalars() + middle.scalars()
        fun started() = ReplyPageSequence("reply").apply { append(page(first, 0, first.scalars())) }
        assertThrows(ProtocolException::class.java) {
            started().append(page(middle, first.scalars(), next, sha = "a".repeat(64)))
        }
        assertThrows(ProtocolException::class.java) {
            started().append(page(middle, first.scalars(), next, sources = listOf(source)))
        }
        val corrupted = started()
        assertNull(corrupted.append(page(middle, first.scalars(), next)))
        assertThrows(ProtocolException::class.java) {
            corrupted.append(page(last.replace("丙", "丁"), next, null))
        }
    }
}
