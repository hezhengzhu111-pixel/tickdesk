package dev.tickdesk.app.ui

import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.FullReplyText
import dev.tickdesk.core.ProtocolException
import dev.tickdesk.core.ReplyPageSequence
import dev.tickdesk.core.WorkbenchProjection
import dev.tickdesk.core.obj
import dev.tickdesk.core.text
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject

internal const val GET_REPLY_PAGE = "get_reply_page"

internal data class ReplyReadScope(val pair: String, val epoch: String, val peer: String)

internal fun WorkbenchProjection.replyReadScope(): ReplyReadScope? {
    if (connection.recoveryRequired || requiresReview || serviceRecoveryActive) return null
    return ReplyReadScope(pairId ?: return null, epoch ?: return null, peerIncarnation ?: return null)
}

internal fun WorkbenchProjection.supportsReplyPages() =
    GET_REPLY_PAGE in reads["get_status"]?.obj("protocol")?.stringList("supportedCommands").orEmpty()

@Composable
internal fun FullReplyButton(state: WorkbenchProjection, id: String, reply: JsonObject, go: (String, String) -> Unit) {
    val ref = reply.objects("sources").firstOrNull()?.text("ref") ?: return
    if (!state.supportsReplyPages() || state.replyReadScope() == null || !idPattern.matches(id) ||
        !ref.startsWith("local:chat-output:") || !idPattern.matches(ref.removePrefix("local:chat-output:"))) return
    TextButton(onClick = { go("reply_text", id) }, modifier = Modifier.heightIn(min = 48.dp)) { Text("查看全文") }
}

internal class ReplyPageReadException(val code: String? = null) : IllegalStateException(code)

private sealed interface ReplyTextRead {
    data class Loading(val chars: Long = 0, val total: Long? = null) : ReplyTextRead
    data class Ready(val blocks: List<String>, val sources: List<JsonObject>) : ReplyTextRead
    data class Failed(val message: String) : ReplyTextRead
}

@Composable
internal fun ReplyTextPage(
    state: WorkbenchProjection, id: String, model: WorkbenchViewModel, modifier: Modifier, go: (String, String) -> Unit,
) {
    val scope = state.replyReadScope()
    val supported = scope != null && state.supportsReplyPages() && idPattern.matches(id)
    val connected = supported && state.connection.canWrite
    var read by remember(scope, id) { mutableStateOf<ReplyTextRead>(ReplyTextRead.Loading()) }
    var retry by remember(scope, id) { mutableIntStateOf(0) }
    var sequence by remember(scope, id) { mutableStateOf(ReplyPageSequence(id)) }
    var verifiedText by remember(scope, id) { mutableStateOf<FullReplyText?>(null) }
    LaunchedEffect(scope, id, connected, retry) {
        if (scope == null || !connected || read is ReplyTextRead.Ready) return@LaunchedEffect
        try {
            while (verifiedText == null) {
                read = ReplyTextRead.Loading(sequence.loadedChars, sequence.totalChars)
                val page = model.readReplyPage(scope, sequence.request())
                verifiedText = sequence.append(page)
            }
            val full = checkNotNull(verifiedText)
            val blocks = withContext(Dispatchers.Default) { full.displayBlocks() }
            if (model.state.value.replyReadScope() == scope && model.state.value.supportsReplyPages()) {
                read = ReplyTextRead.Ready(blocks, full.sources)
            }
        } catch (cancel: CancellationException) {
            throw cancel
        } catch (_: ProtocolException) {
            sequence = ReplyPageSequence(id)
            verifiedText = null
            read = ReplyTextRead.Failed("全文校验未通过，请重新读取。")
        } catch (failure: ReplyPageReadException) {
            if (failure.code == "INVALID_PARAMS") {
                sequence = ReplyPageSequence(id)
                verifiedText = null
            }
            read = ReplyTextRead.Failed(when (failure.code) {
                "AI_REPLY_UNAVAILABLE" -> "工作台无法读取这条回复的原文。"
                "INVALID_PARAMS" -> "原文版本已变化，请重新读取。"
                "MESSAGE_TOO_LARGE" -> "工作台无法发送此页，请检查工作台版本。"
                else -> "全文读取中断，请检查连接后重试。"
            })
        }
    }

    LazyColumn(modifier, contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 2.dp, bottom = 24.dp)) {
        val current = read
        when {
            !supported -> item { NoticeStrip("当前工作台无法读取全文。返回可查看已保存的节选。") }
            current is ReplyTextRead.Ready -> {
                if (current.blocks.isEmpty()) item { NoticeStrip("原文为空。") }
                items(current.blocks) { text ->
                    SelectionContainer { Text(text, style = MaterialTheme.typography.bodyMedium) }
                }
                if (current.sources.isNotEmpty()) item { DesignCard { ChatSourceChips(current.sources) { go("task", it) } } }
            }
            !connected -> item { NoticeStrip("等待工作台连接并同步后读取全文。") }
            current is ReplyTextRead.Loading -> item {
                val progress = current.total?.let { " ${current.chars} / $it 字符" }.orEmpty()
                NoticeStrip("正在读取并校验全文…$progress")
            }
            current is ReplyTextRead.Failed -> item {
                NoticeStrip(current.message, DeskColors.amber)
                TextButton(onClick = { retry++ }, modifier = Modifier.heightIn(min = 48.dp)) { Text("重新读取") }
            }
        }
    }
}

// Join transport pages before layout. Prefer paragraph boundaries and keep long paragraphs renderable.
private fun FullReplyText.displayBlocks() = buildList {
    val pending = StringBuilder()
    for (page in pages) {
        pending.append(page)
        if (pending.length < 3_000) continue
        val line = pending.lastIndexOf('\n')
        val end = if (line >= 0) line + 1 else if (pending.length >= 8_000) pending.length else 0
        if (end == 0) continue
        add(pending.substring(0, end))
        pending.delete(0, end)
    }
    if (pending.isNotEmpty()) add(pending.toString())
}
