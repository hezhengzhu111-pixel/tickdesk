package dev.tickdesk.app.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import dev.tickdesk.app.R
import dev.tickdesk.app.VerifiedReminder
import dev.tickdesk.core.*
import kotlinx.serialization.json.JsonObject

@Composable
internal fun ForegroundReminder(
    alert: VerifiedReminder,
    position: JsonObject?,
    dismiss: () -> Unit,
    details: () -> Unit,
) {
    val task = alert.task
    val now by deskTime()
    val direction = task.text("direction")
    val held = (task.number("snoozeUntil") ?: 0L) > now
    val active = task.activeTask(now) && !held && now < alert.expiresAt
    val next = task.number("nextReminderAt")?.takeIf { it > now }
    val quote = position?.takeIf { it.number("priceAsOf") != null && it.number("priceAsOf") == task.number("dataAsOf") }?.text("lastPrice")
    Dialog(onDismissRequest = dismiss, properties = DialogProperties(
        dismissOnClickOutside = false, usePlatformDefaultWidth = false, decorFitsSystemWindows = false,
    )) {
        CompositionLocalProvider(LocalContentColor provides MaterialTheme.colorScheme.onBackground) {
            BoxWithConstraints(Modifier.fillMaxSize().criticalBackdrop().statusBarsPadding().navigationBarsPadding()) {
                val compact = maxHeight < 800.dp
                Column(Modifier.fillMaxSize().padding(horizontal = 16.dp).padding(top = 8.dp, bottom = 10.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Row(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(6.dp),
                            verticalAlignment = Alignment.CenterVertically) {
                            Icon(painterResource(R.drawable.ic_ultra_bell), null, Modifier.size(14.dp), DeskColors.redBright)
                            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                                Text("TickDesk · 前台强提醒", style = MaterialTheme.typography.labelSmall, color = Color(0xFFDFB8C0))
                                task.number("remindedCount")?.takeIf { it > 0 }?.let { count ->
                                    val scheduled = task.obj("currentReminder")?.number("scheduledAt")?.let(::timeText)
                                    Text(listOfNotNull("第 $count 次强提醒", scheduled).joinToString(" · "),
                                        style = MaterialTheme.typography.labelSmall, color = Color(0xFFC8A2AA))
                                }
                            }
                        }
                        OutlinedButton(onClick = dismiss, modifier = Modifier.heightIn(min = 48.dp),
                            shape = RoundedCornerShape(11.dp), contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp),
                            border = BorderStroke(1.dp, Color(0x38FFFFFF)),
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = Color(0xFFF6DFE3))) {
                            Text("返回应用", style = MaterialTheme.typography.labelSmall)
                        }
                    }
                    Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState()).padding(vertical = 4.dp),
                        verticalArrangement = Arrangement.spacedBy(if (compact) 12.dp else 17.dp)) {
                    Column(Modifier.fillMaxWidth().padding(top = 9.dp), horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(7.dp)) {
                        Surface(shape = RoundedCornerShape(20.dp), color = Color.Transparent,
                            border = BorderStroke(1.dp, DeskColors.redBright.copy(alpha = .42f))) {
                            Box(Modifier.size(if (compact) 46.dp else 60.dp).background(Brush.linearGradient(listOf(
                                DeskColors.red.copy(alpha = .38f), Color(0x2EE11D48),
                            ))), contentAlignment = Alignment.Center) {
                                Icon(painterResource(R.drawable.ic_ultra_bell), null, Modifier.size(if (compact) 22.dp else 28.dp), tint = Color.White)
                            }
                        }
                        Text(when {
                            held -> "已稍后 · ${taskDirection(direction)}"
                            !active -> "事项窗口已结束"
                            else -> "CRITICAL · ${taskDirection(direction)}"
                        }, fontFamily = deskMono, fontWeight = FontWeight.Bold,
                            letterSpacing = 1.35.sp, style = MaterialTheme.typography.labelSmall, color = DeskColors.redBright)
                        Text("${task.value("name")} (${task.value("symbol")})", Modifier.semantics { heading() },
                            style = MaterialTheme.typography.headlineLarge.copy(fontSize = if (compact) 21.sp else 25.sp),
                            textAlign = TextAlign.Center)
                        val reason = listOfNotNull(task.text("levelReason"), position?.number("availableQty")?.let { "可用持仓 $it 股" })
                        if (reason.isNotEmpty()) Text(reason.joinToString(" · "), textAlign = TextAlign.Center,
                            style = MaterialTheme.typography.bodySmall, color = Color(0xFFE2C0C7))
                        if (quote != null) {
                            Text(tradeNumber(quote, minimumDecimals = 2), color = directionColor(direction), style = MaterialTheme.typography.titleLarge,
                                fontSize = if (compact) 23.sp else 27.sp, fontFamily = deskMono)
                        }
                    }
                    DesignCard(color = Color(0xFF26131B), border = DeskColors.red.copy(alpha = .26f)) {
                        Text(task.obj("reasons")?.text("quant") ?: "请核对此事项。", color = Color(0xFFF3DFE2))
                        val source = task.obj("reasons")?.objects("sources")?.firstOrNull { it.number("asOf") == task.number("dataAsOf") }?.text("name")
                        Text("${source ?: "行情"} ${timeText(task.number("dataAsOf"))} · ${verification(task)}",
                            style = MaterialTheme.typography.labelSmall, color = DeskColors.faint)
                    }
                    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(if (next != null) "距离下一次强提醒" else "事项有效窗口剩余",
                            style = MaterialTheme.typography.labelSmall, color = Color(0xFFD8B2BA), textAlign = TextAlign.Center)
                        Text(remaining(next ?: task.number("validUntil"), now), style = MaterialTheme.typography.displaySmall,
                            fontSize = if (compact) 30.sp else 40.sp, lineHeight = if (compact) 35.sp else 45.sp, color = Color.White)
                        if (next != null) Text("事项有效窗口剩余 ${remaining(task.number("validUntil"), now)}",
                            style = MaterialTheme.typography.labelSmall, color = Color(0xFFC8A2AA), textAlign = TextAlign.Center)
                    }
                    }
                    CriticalDock(details)
                }
            }
        }
    }
}

@Composable
private fun CriticalDock(details: () -> Unit) {
    Surface(Modifier.fillMaxWidth(), color = Color.Transparent, shape = RoundedCornerShape(16.dp),
        border = BorderStroke(1.dp, DeskColors.red.copy(alpha = .34f))) {
        Column(Modifier.background(Brush.verticalGradient(listOf(Color(0xFF241019), Color(0xFF120B12)))).padding(10.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            MainAction("查看事项详情", onClick = details)
        }
    }
}

private fun Modifier.criticalBackdrop() = drawWithCache {
    val base = Brush.verticalGradient(listOf(Color(0xFF190C14), Color(0xFF0A0910)))
    val origin = Offset(size.width / 2, size.height * .18f)
    val glow = Brush.radialGradient(listOf(DeskColors.red.copy(alpha = .28f), Color.Transparent), origin,
        radius = (size.height * .44f).coerceAtLeast(1f))
    val ringCenter = Offset(size.width / 2, 75.dp.toPx())
    onDrawBehind {
        drawRect(base)
        drawRect(glow)
        drawCircle(DeskColors.red.copy(alpha = .025f), 224.dp.toPx(), ringCenter)
        drawCircle(DeskColors.red.copy(alpha = .05f), 182.dp.toPx(), ringCenter)
        drawCircle(DeskColors.red.copy(alpha = .16f), 140.dp.toPx(), ringCenter, style = Stroke(1.dp.toPx()))
    }
}

