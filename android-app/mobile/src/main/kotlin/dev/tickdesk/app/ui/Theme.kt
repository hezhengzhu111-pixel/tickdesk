package dev.tickdesk.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.sp
import dev.tickdesk.app.R

private val deskSans = variableFamily(R.font.inter_variable)
internal val deskMono = variableFamily(R.font.jetbrains_mono_variable)

@OptIn(ExperimentalTextApi::class)
private fun variableFamily(resource: Int) = FontFamily(
    listOf(FontWeight.Normal, FontWeight.Medium, FontWeight.SemiBold, FontWeight.Bold).map { weight ->
        Font(resource, weight = weight, variationSettings = FontVariation.Settings(FontVariation.weight(weight.weight)))
    },
)

private fun deskType(size: Int, height: Int, weight: FontWeight = FontWeight.Normal, spacing: TextUnit = TextUnit.Unspecified) =
    TextStyle(fontFamily = deskSans, fontSize = size.sp, lineHeight = height.sp, fontWeight = weight,
        letterSpacing = spacing, fontFeatureSettings = "tnum")

private val colors = darkColorScheme(
    primary = DeskColors.blue, onPrimary = Color(0xFF08152A),
    background = DeskColors.background, onBackground = DeskColors.text,
    surface = DeskColors.panel, onSurface = DeskColors.text,
    surfaceVariant = DeskColors.navy, onSurfaceVariant = DeskColors.muted,
    secondaryContainer = Color(0xFF1B2D49), onSecondaryContainer = Color(0xFFDCEAFF),
    outline = Color(0xFF5D6F8A), error = DeskColors.redBright,
)

@Composable fun TickDeskTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = colors, typography = Typography(
        headlineLarge = deskType(23, 28, FontWeight.Bold, (-0.60).sp),
        headlineMedium = deskType(21, 27, FontWeight.Bold),
        headlineSmall = deskType(19, 25, FontWeight.Bold),
        displaySmall = deskType(32, 40, FontWeight.SemiBold).copy(fontFamily = deskMono),
        titleLarge = deskType(22, 28, FontWeight.Bold, (-0.4).sp),
        titleMedium = deskType(16, 23, FontWeight.SemiBold),
        titleSmall = deskType(14, 20, FontWeight.SemiBold),
        bodyLarge = deskType(15, 23),
        bodyMedium = deskType(13, 20),
        bodySmall = deskType(12, 18),
        labelLarge = deskType(14, 19, FontWeight.SemiBold),
        labelMedium = deskType(12, 17, FontWeight.Medium),
        labelSmall = deskType(12, 17),
    ), content = content)
}
