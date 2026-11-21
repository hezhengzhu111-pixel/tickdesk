package dev.tickdesk.core

import kotlinx.serialization.json.*

/**
 * Typed recovery form inputs. The UI may keep text while editing, but every
 * value is parsed once here: JSON numbers stay numbers, missing fields become
 * field errors, and nothing is replaced with zero or the current clock.
 */
data class RecoveryIncludedInput(
    val symbol: String,
    val basePositionRevision: Long,
    val qty: Long,
    val availableQty: Long?,
    val cost: String,
    val asOf: Long,
    val resolvedExecutionIds: List<String>,
) {
    fun positionJson(reason: String = "恢复对账：手机核对的当前持仓基线"): JsonObject = buildJsonObject {
        put("symbol", symbol)
        put("basePositionRevision", basePositionRevision)
        put("qty", qty)
        put("availableQty", availableQty?.let(::JsonPrimitive) ?: JsonNull)
        put("cost", cost)
        put("asOf", asOf)
        put("reason", reason)
        putJsonArray("resolvedExecutionIds") { resolvedExecutionIds.forEach { add(it) } }
        put("pendingLots", JsonNull)
    }
}

data class RecoveryRecordInput(
    val symbol: String,
    val side: String,
    val price: String,
    val qty: Long,
    val executedAt: Long,
    val fees: String?,
    val newExecutionId: String,
) {
    fun executionJson(): JsonObject = buildJsonObject {
        put("symbol", symbol)
        put("side", side)
        put("price", price)
        put("qty", qty)
        put("executedAt", executedAt)
        fees?.let { put("fees", it) }
    }
}

data class RecoveryFormBatch(
    val record: RecoveryRecordInput? = null,
    val included: RecoveryIncludedInput? = null,
)

data class RecoveryFormResult<out T>(val value: T?, val errors: Map<String, String>) {
    val ok: Boolean get() = value != null && errors.isEmpty()
}

object RecoveryForms {
    private val symbolPattern = Regex("[0-9]{6}\\.(SH|SZ)")
    private val decimalPattern = Regex("(0|[1-9][0-9]*)(\\.[0-9]+)?")
    private val idPattern = Regex("[A-Za-z0-9_-]+")

    fun record(draft: JsonObject, newExecutionId: String): RecoveryFormResult<RecoveryRecordInput> {
        val errors = linkedMapOf<String, String>()
        val symbol = readSymbol(draft, "symbol", errors)
        val side = readText(draft, "side")?.trim().orEmpty()
        if (side !in setOf("buy", "sell")) errors["side"] = "请选择买入或卖出"
        val price = readDecimal(draft, "price", "成交价", errors, zeroAllowed = false)
        val qty = readLong(draft, "qty", "数量", 1, CanonicalJson.MAX_INTEGER, errors)
        val executedAt = readLong(draft, "executedAt", "成交时间", 1, CanonicalJson.MAX_INTEGER, errors)
        val fees = readDecimal(draft, "fees", "费用", errors, zeroAllowed = true, optional = true)
        if (!idPattern.matches(newExecutionId) || newExecutionId.length > 64) {
            errors["newExecutionId"] = "手机预分配的新成交 ID 无效"
        }
        val value = if (symbol != null && price != null && qty != null && executedAt != null) {
            RecoveryRecordInput(
                symbol = symbol,
                side = side,
                price = price,
                qty = qty,
                executedAt = executedAt,
                fees = fees,
                newExecutionId = newExecutionId,
            )
        } else {
            null
        }
        return RecoveryFormResult(value, errors)
    }

    fun included(
        draft: JsonObject,
        currentGlobalRevision: Long?,
        resolvedExecutionIds: Set<String>,
    ): RecoveryFormResult<RecoveryIncludedInput> {
        val errors = linkedMapOf<String, String>()
        val symbol = readSymbol(draft, "positionSymbol", errors)
        val base = readLong(
            draft, "basePositionRevision", "持仓基线版本", 0, CanonicalJson.MAX_INTEGER, errors,
        )
        if (currentGlobalRevision != null && base != null && base != currentGlobalRevision) {
            errors["basePositionRevision"] = "请先读取当前全局持仓基线版本 $currentGlobalRevision"
        }
        if (currentGlobalRevision == null && base != null) {
            errors["basePositionRevision"] = "当前全局持仓基线未知，请刷新完整快照"
        }
        val qty = readLong(draft, "positionQty", "持仓数量", 0, CanonicalJson.MAX_INTEGER, errors)
        val available = readLong(
            draft, "availableQty", "可卖数量", 0, CanonicalJson.MAX_INTEGER, errors,
            allowEmpty = true,
        )
        if (qty != null && available != null && available > qty) {
            errors["availableQty"] = "可卖数量不能超过持仓数量"
        }
        val cost = readDecimal(draft, "cost", "成本", errors, zeroAllowed = true)
        val asOf = readLong(draft, "positionAsOf", "核对时点", 1, CanonicalJson.MAX_INTEGER, errors)
        val ids = resolvedExecutionIds.map { it.trim() }.filter { it.isNotEmpty() }
        if (ids.any { !idPattern.matches(it) || it.length > 64 } || ids.size != ids.toSet().size) {
            errors["resolvedExecutionIds"] = "同批核销成交 ID 无效或重复"
        }
        val value = if (symbol != null && base != null && qty != null && cost != null && asOf != null) {
            RecoveryIncludedInput(symbol, base, qty, available, cost, asOf, ids.sorted())
        } else {
            null
        }
        return RecoveryFormResult(value, errors)
    }

    fun recordBatch(
        draft: JsonObject,
        newExecutionId: String,
        includedDraft: JsonObject?,
        currentGlobalRevision: Long?,
        resolvedExecutionIds: Set<String>,
    ): RecoveryFormResult<RecoveryFormBatch> {
        val record = record(draft, newExecutionId)
        val included = includedDraft?.let { included(it, currentGlobalRevision, resolvedExecutionIds) }
        val errors = record.errors.toMutableMap()
        included?.errors?.forEach { (key, value) -> errors["position.$key"] = value }
        return RecoveryFormResult(
            if (record.value != null && (includedDraft == null || included?.value != null)) {
                RecoveryFormBatch(record.value, included?.value)
            } else {
                null
            },
            errors,
        )
    }

    private fun readSymbol(draft: JsonObject, key: String, errors: MutableMap<String, String>): String? {
        val value = readText(draft, key)?.trim()
        if (value.isNullOrEmpty()) {
            errors[key] = "请填写证券代码"
        } else if (!symbolPattern.matches(value)) {
            errors[key] = "证券代码格式应为 000000.SH 或 000000.SZ"
        } else {
            return value
        }
        return null
    }

    private fun readDecimal(
        draft: JsonObject,
        key: String,
        label: String,
        errors: MutableMap<String, String>,
        zeroAllowed: Boolean,
        optional: Boolean = false,
    ): String? {
        val raw = readText(draft, key)?.trim()
        if (raw.isNullOrEmpty()) {
            if (!optional) errors[key] = "请填写${label}"
            return null
        }
        if (raw.length > 48 || !decimalPattern.matches(raw)) {
            errors[key] = "${label}应为非负十进制数"
        } else if (!zeroAllowed && raw.toBigDecimalOrNull()?.signum() == 0) {
            errors[key] = "${label}必须大于 0"
        } else {
            return raw
        }
        return null
    }

    private fun readLong(
        draft: JsonObject,
        key: String,
        label: String,
        minimum: Long,
        maximum: Long,
        errors: MutableMap<String, String>,
        allowEmpty: Boolean = false,
    ): Long? {
        val element = draft[key]
        if (element == null || element is JsonNull) {
            if (!allowEmpty) errors[key] = "请填写${label}"
            return null
        }
        val number = when (element) {
            is JsonPrimitive -> if (element.isString) {
                element.content.trim().takeIf { it.isNotEmpty() }?.toLongOrNull()
            } else {
                element.longOrNull
            }
            else -> null
        }
        if (number == null) {
            errors[key] = "${label}应为整数"
        } else if (number !in minimum..maximum) {
            errors[key] = "${label}超出允许范围"
        } else {
            return number
        }
        return null
    }

    private fun readText(draft: JsonObject, key: String): String? =
        (draft[key] as? JsonPrimitive)?.content
}
