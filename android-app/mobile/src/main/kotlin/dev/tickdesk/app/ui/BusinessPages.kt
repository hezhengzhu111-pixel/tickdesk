package dev.tickdesk.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.*
import kotlinx.serialization.json.*
import java.math.BigDecimal
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

@Composable
internal fun IntentText(intent: IntentView) {
    Text("${commandLabel(intent.command)} · ${intent.stage.label()}${intent.errorCode?.let { " · $it" }.orEmpty()}")
}

@Composable
internal fun Field(label: String, value: String, change: (String) -> Unit,
                   type: KeyboardType = KeyboardType.Text, error: String? = null, lines: Int = 1,
                   focus: FocusRequester? = null) {
    val modifier = if (focus == null) Modifier else Modifier.focusRequester(focus)
    OutlinedTextField(value, change, modifier.fillMaxWidth(), label = { Text(label) },
        keyboardOptions = KeyboardOptions(keyboardType = type, imeAction = if (lines == 1) ImeAction.Next else ImeAction.Default),
        shape = RoundedCornerShape(10.dp),
        colors = OutlinedTextFieldDefaults.colors(
            focusedContainerColor = DeskColors.field,
            unfocusedContainerColor = DeskColors.field,
            unfocusedBorderColor = MaterialTheme.colorScheme.outline,
        ),
        isError = error != null,
        supportingText = error?.let { message -> { Text(message) } }, singleLine = lines == 1,
        minLines = lines, maxLines = if (lines == 1) 1 else 5)
}

@Composable
internal fun Confirm(title: String, message: String, busy: Boolean, dismiss: () -> Unit, proceed: () -> Unit) {
    AlertDialog(onDismissRequest = dismiss, title = { Text(title) }, text = { Text(message) },
        dismissButton = { TextButton(onClick = dismiss) { Text("取消") } },
        confirmButton = { TextButton(onClick = proceed, enabled = !busy) { Text("确认") } })
}

@Composable
internal fun SourceText(source: JsonObject) {
    Text(source.entries.filter { it.value != JsonNull }.joinToString(" · ") { (_, value) ->
        (value as? JsonPrimitive)?.content.orEmpty()
    })
}

internal val symbolPattern = Regex("[0-9]{6}\\.(SH|SZ)")
internal val idPattern = Regex("[A-Za-z0-9_-]{1,64}")
internal fun whole(raw: String, positive: Boolean = false) = raw.trim().toLongOrNull()
    ?.takeIf { it in (if (positive) 1L else 0L)..CanonicalJson.MAX_INTEGER }
internal fun decimal(raw: String, positive: Boolean = false): BigDecimal? = raw.trim()
    .takeIf { it.length <= 48 && it.matches(Regex("(0|[1-9][0-9]*)(\\.[0-9]+)?")) }
    ?.toBigDecimalOrNull()?.takeIf { !positive || it.signum() > 0 }

internal fun dateTime(date: String, time: String): Long? = runCatching {
    LocalDateTime.parse("${date.trim()}T${time.trim()}", DateTimeFormatter.ISO_LOCAL_DATE_TIME)
        .atZone(shanghai).toInstant().toEpochMilli()
}.getOrNull()?.takeIf { it >= 0 }
