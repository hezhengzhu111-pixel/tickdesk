package dev.tickdesk.app.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.tickdesk.app.R

internal object DeskColors {
    val deepBackground = Color(0xFF05070B)
    val background = Color(0xFF080B11)
    val panel = Color(0xFF0D121C)
    val navy = Color(0xFF192234)
    val nav = Color(0xFF0A0E16)
    val dock = Color(0xFF0E1421)
    val field = Color(0xFF0B101B)
    val border = Color(0xFF273144)
    val borderStrong = Color(0xFF3A475D)
    val text = Color(0xFFF8FAFC)
    val secondary = Color(0xFFDBE4F2)
    val muted = Color(0xFF94A3B8)
    val faint = Color(0xFF899BB4)
    val blue = Color(0xFF60A5FA)
    val amber = Color(0xFFF5A623)
    val red = Color(0xFFFF4D5E)
    val redBright = Color(0xFFFF7582)
    val green = Color(0xFF18D28B)
}

@Composable
internal fun DesignCard(
    modifier: Modifier = Modifier,
    color: Color = DeskColors.panel,
    border: Color = DeskColors.border,
    content: @Composable ColumnScope.() -> Unit,
) {
    val shape = RoundedCornerShape(18.dp)
    val fill = if (color == DeskColors.panel) {
        Brush.verticalGradient(listOf(Color(0xFF141B29), Color(0xFF0F1520)))
    } else {
        Brush.verticalGradient(listOf(color, color))
    }
    Surface(modifier.fillMaxWidth(), shape = shape, color = Color.Transparent, contentColor = DeskColors.text,
        border = BorderStroke(1.dp, border)) {
        Column(Modifier.background(fill).padding(15.dp), verticalArrangement = Arrangement.spacedBy(8.dp), content = content)
    }
}

@Composable
internal fun SectionTitle(title: String, detail: String? = null) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, Modifier.weight(1f).semantics { heading() }, style = MaterialTheme.typography.titleSmall)
        if (detail != null) Text(detail, Modifier.weight(0.72f, fill = false), style = MaterialTheme.typography.labelSmall,
            color = DeskColors.faint, textAlign = androidx.compose.ui.text.style.TextAlign.End)
    }
}

@Composable
internal fun StatusBadge(text: String, tint: Color = DeskColors.blue) {
    val bright = when (tint) {
        DeskColors.red -> Color(0xFFFF949E)
        DeskColors.green -> Color(0xFF6EE7B7)
        DeskColors.amber -> Color(0xFFFCD34D)
        DeskColors.blue -> Color(0xFF93C5FD)
        else -> tint
    }
    Surface(color = Color.Transparent, contentColor = bright, shape = CircleShape,
        border = BorderStroke(1.dp, tint.copy(alpha = 0.32f))) {
        Text(text, Modifier.background(Brush.linearGradient(listOf(tint.copy(alpha = 0.22f), tint.copy(alpha = 0.08f))))
            .padding(horizontal = 9.dp, vertical = 3.dp), style = MaterialTheme.typography.labelSmall,
            fontFamily = deskMono, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
internal fun NoticeStrip(text: String, tint: Color = DeskColors.blue) {
    Surface(color = tint.copy(alpha = 0.09f), shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, tint.copy(alpha = 0.18f))) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("ⓘ", color = tint, style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.clearIconSemantics())
            Text(text, Modifier.weight(1f), color = DeskColors.muted, style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
internal fun PageHeading(title: String, subtitle: String? = null, badge: String? = null, live: String? = null) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 2.dp), horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, Modifier.semantics { heading() }, style = MaterialTheme.typography.headlineLarge, color = Color.White)
            if (subtitle != null) Text(subtitle, color = DeskColors.muted, style = MaterialTheme.typography.bodySmall)
        }
        if (badge != null) StatusBadge(badge)
        if (live != null) LivePill(live, DeskColors.green)
    }
}

@Composable
internal fun LivePill(label: String, tint: Color) {
    Surface(shape = CircleShape, color = tint.copy(alpha = .08f), border = BorderStroke(1.dp, tint.copy(alpha = .22f))) {
        Row(Modifier.padding(horizontal = 9.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Box(Modifier.size(6.dp).background(tint, CircleShape))
            Text(label, color = if (tint == DeskColors.green) Color(0xFFA7F3D0) else tint,
                style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
internal fun DesignTabBar(selected: Tab, taskCount: Int, hasUrgent: Boolean, holdingCount: Int, select: (Tab) -> Unit) {
    Surface(color = DeskColors.nav) {
        Column(Modifier.navigationBarsPadding().fillMaxWidth()) {
            HorizontalDivider(color = DeskColors.border)
            Row(Modifier.selectableGroup().padding(horizontal = 10.dp, vertical = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                for (tab in Tab.entries) {
                    val active = tab == selected
                    val shape = RoundedCornerShape(14.dp)
                    val count = when (tab) {
                        Tab.INBOX -> taskCount
                        Tab.PORTFOLIO -> holdingCount
                        else -> 0
                    }
                    val indicator = if (active) Modifier.background(Brush.verticalGradient(listOf(
                        Color(0xFF3B82F6).copy(alpha = 0.22f), Color(0xFF3B82F6).copy(alpha = 0.08f),
                    )), shape) else Modifier
                    Column(
                        Modifier.weight(1f).heightIn(min = 50.dp).clip(shape).then(indicator)
                            .selectable(active, role = Role.Tab, onClick = { select(tab) })
                            .semantics {
                                if (tab == Tab.INBOX) stateDescription = "$taskCount 项待决策" + if (hasUrgent) "，含紧急事项" else ""
                                if (tab == Tab.PORTFOLIO) stateDescription = "$holdingCount 只持仓"
                            }
                            .padding(horizontal = 1.dp, vertical = 4.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(3.dp, Alignment.CenterVertically),
                    ) {
                        Box(Modifier.size(19.dp)) {
                            Icon(painterResource(tab.icon), null, Modifier.fillMaxSize(), if (active) DeskColors.blue else DeskColors.faint)
                            if (tab == Tab.INBOX && hasUrgent) {
                                Box(Modifier.align(Alignment.TopEnd).offset(x = 2.dp, y = (-2).dp).size(7.dp)
                                    .background(DeskColors.red, CircleShape).border(1.dp, DeskColors.nav, CircleShape))
                            }
                            if (count > 0) {
                                Box(Modifier.align(Alignment.TopCenter).offset(x = 18.dp, y = (-3).dp)
                                    .sizeIn(minWidth = 15.dp, minHeight = 15.dp).clearAndSetSemantics { }
                                    .background(Brush.verticalGradient(listOf(Color(0xFF3B82F6), Color(0xFF2563EB))), CircleShape)
                                    .border(1.dp, DeskColors.nav, CircleShape).padding(horizontal = 3.dp),
                                    contentAlignment = Alignment.Center) {
                                    Text(if (count > 99) "99+" else count.toString(), color = Color.White,
                                        fontFamily = deskMono, fontWeight = FontWeight.Bold, fontSize = 9.sp, lineHeight = 11.sp)
                                }
                            }
                        }
                        Text(stringResource(tab.label), style = MaterialTheme.typography.labelSmall,
                            fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
                            color = if (active) Color.White else DeskColors.faint, textAlign = TextAlign.Center)
                    }
                }
            }
        }
    }
}

@Composable
internal fun ContextAction(eyebrow: String, summary: String, label: String, enabled: Boolean, onClick: () -> Unit) {
    val shape = RoundedCornerShape(17.dp)
    Surface(Modifier.fillMaxWidth(), shape = shape, color = Color.Transparent,
        border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = .24f))) {
        val copy: @Composable () -> Unit = {
            Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(eyebrow, style = MaterialTheme.typography.labelSmall, color = DeskColors.blue, fontWeight = FontWeight.SemiBold)
                Text(summary, style = MaterialTheme.typography.labelMedium, color = DeskColors.secondary)
            }
        }
        val modifier = Modifier.background(Brush.linearGradient(listOf(Color(0xFF182235), Color(0xFF0C121D)))).padding(9.dp)
        if (LocalDensity.current.fontScale >= 1.45f) {
            Column(modifier, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                copy()
                MainAction(label, enabled, R.drawable.ic_ultra_check, onClick)
            }
        } else {
            Row(modifier, horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.weight(1.22f).padding(start = 3.dp)) { copy() }
                Box(Modifier.weight(1f)) { MainAction(label, enabled, R.drawable.ic_ultra_check, onClick) }
            }
        }
    }
}

internal class ScreenFooter {
    var content by mutableStateOf<(@Composable () -> Unit)?>(null)
}

@Composable
internal fun ScreenFooter.Attach(content: @Composable () -> Unit) {
    val latest = rememberUpdatedState(content)
    val slot: @Composable () -> Unit = remember(this) { @Composable { latest.value() } }
    DisposableEffect(this) {
        this@Attach.content = slot
        onDispose { if (this@Attach.content === slot) this@Attach.content = null }
    }
}

@Composable
internal fun ScreenFooter.ResponsiveAction(
    showShortcut: Boolean = true,
    inline: @Composable () -> Unit,
    shortcut: @Composable () -> Unit,
) {
    var visible by remember(this) { mutableStateOf(false) }
    Box(Modifier.fillMaxWidth().onGloballyPositioned { coordinates ->
        val height = coordinates.size.height
        visible = height > 0 && coordinates.boundsInWindow().height >= height * 0.8f
    }) { inline() }
    if (showShortcut && !visible) Attach(shortcut)
}

@Composable
internal fun ActionDock(content: @Composable () -> Unit) {
    Surface(color = DeskColors.dock) {
        Column {
            HorizontalDivider(color = DeskColors.border)
            Box(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) { content() }
        }
    }
}

internal fun Modifier.ultraPageBackground() = drawWithCache {
    val origin = Offset(size.width / 2, 0f)
    val halo = Brush.radialGradient(listOf(Color(0x103B82F6), Color.Transparent), origin,
        radius = (size.width * .82f).coerceAtLeast(1f))
    onDrawBehind {
        drawRect(DeskColors.background)
        scale(1f, .62f, origin) { drawRect(halo) }
    }
}

private fun Modifier.clearIconSemantics() =
    this.then(Modifier.clearAndSetSemantics { })
