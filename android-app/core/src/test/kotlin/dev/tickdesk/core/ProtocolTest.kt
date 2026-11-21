package dev.tickdesk.core

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.util.Base64

class ProtocolTest {
    @Test fun businessFixturesMatchTheContract() = fixtures("fixtures", Protocol::business)

    @Test fun relayFixturesMatchTheContract() = fixtures("relay-fixtures", Protocol::frame)

    @Test fun canonicalBytesAndDigestMatchPublishedVectors() {
        val vector = resource("hash-vectors").getValue("business").jsonObject
        val input = vector.getValue("input")
        assertEquals(vector.text("canonicalUtf8"), CanonicalJson.encode(input))
        assertEquals(vector.text("sha256"), CanonicalJson.hash(input))
        for (value in resource("evidence-vectors").getValue("records").jsonArray) {
            val record = value.jsonObject
            val bytes = record.getValue("parts").jsonArray.flatMap {
                Base64.getDecoder().decode(it.jsonPrimitive.content).asIterable()
            }.toByteArray()
            assertArrayEquals(record.text("name"), bytes, CanonicalJson.encode(record.getValue("payload")).toByteArray())
            assertEquals(record.text("name"), record.text("payloadHash"), CanonicalJson.hash(record.getValue("payload")))
        }
    }

    @Test fun rejectsAmbiguousOrLossyJsonBeforeHashing() {
        for (raw in listOf(
            """{"a":1,"\u0061":2}""", "[-0]", "[1.0]", "[1e2]", "[01]",
            "[9007199254740992]", """["\uD800"]""", """{"中":1}""", "[true,]",
        )) assertThrows(raw, ProtocolException::class.java) { CanonicalJson.parse(raw) }
        assertThrows(ProtocolException::class.java) { CanonicalJson.decode(byteArrayOf(0xc0.toByte(), 0xaf.toByte())) }
        assertNotEquals(CanonicalJson.hash(CanonicalJson.parse(""""10.0"""")),
            CanonicalJson.hash(CanonicalJson.parse(""""10.00"""")))
    }

    private fun fixtures(file: String, decode: (String) -> Any) {
        for (value in resource(file).getValue("cases").jsonArray) {
            val case = value.jsonObject
            val payload = case.getValue("payload")
            assertEquals(case.text("name"), case.getValue("valid").jsonPrimitive.boolean,
                runCatching { decode(payload.toString()) }.isSuccess)
        }
    }

    private fun resource(name: String): JsonObject =
        Json.parseToJsonElement(checkNotNull(javaClass.getResourceAsStream("/v2/$name.json"))
            .bufferedReader().use { it.readText() }).jsonObject
}
