package com.wattracker.android.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathBuilder
import androidx.compose.ui.unit.dp

/**
 * Vendored navigation icons.
 *
 * The five destinations use four custom icons (speed gauge, bicycle, calendar,
 * bar chart) plus the platform `Settings` gear from `material-icons-core`.
 * The four custom icons are vendored here as `ImageVector` definitions with
 * Material Design path data, so the app does not need the 50 MB
 * `material-icons-extended` AAR just for four icons.
 */

private val whiteFill = SolidColor(Color.White)

/** Speedometer / gauge icon for Dashboard. */
val wtSpeed: ImageVector = ImageVector.Builder(
    name = "wtSpeed",
    defaultWidth = 24.dp,
    defaultHeight = 24.dp,
    viewportWidth = 24f,
    viewportHeight = 24f,
).apply {
    addPath(
        fill = whiteFill,
        pathData = PathBuilder().apply {
            moveTo(20.38f, 8.57f)
            lineTo(19.15f, 10.42f)
            arcTo(8f, 8f, 0f, false, true, 18.93f, 18f)
            lineTo(5.07f, 18f)
            arcTo(8f, 8f, 0f, false, true, 15.58f, 6.85f)
            lineTo(17.43f, 5.62f)
            arcTo(10f, 10f, 0f, false, false, 3f, 12f)
            arcTo(10f, 10f, 0f, false, false, 23f, 12f)
            arcTo(10f, 10f, 0f, false, false, 21.38f, 8.57f)
            close()
            moveTo(10.59f, 15.41f)
            arcTo(2f, 2f, 0f, false, false, 13.42f, 15.41f)
            lineTo(19.08f, 6.92f)
            lineTo(10.59f, 12.58f)
            arcTo(2f, 2f, 0f, false, false, 10.59f, 15.41f)
            close()
        }.nodes,
    )
}.build()

/** Bicycle icon for Activities. */
val wtPedalBike: ImageVector = ImageVector.Builder(
    name = "wtPedalBike",
    defaultWidth = 24.dp,
    defaultHeight = 24.dp,
    viewportWidth = 24f,
    viewportHeight = 24f,
).apply {
    addPath(
        fill = whiteFill,
        pathData = PathBuilder().apply {
            // Rear wheel
            moveTo(5f, 12f)
            arcTo(5f, 5f, 0f, false, true, 0f, 17f)
            arcTo(5f, 5f, 0f, false, true, 5f, 22f)
            arcTo(5f, 5f, 0f, false, true, 10f, 17f)
            arcTo(5f, 5f, 0f, false, true, 5f, 12f)
            close()
            moveTo(5f, 20.5f)
            arcTo(3.5f, 3.5f, 0f, false, false, 1.5f, 17f)
            arcTo(3.5f, 3.5f, 0f, false, false, 5f, 13.5f)
            arcTo(3.5f, 3.5f, 0f, false, false, 8.5f, 17f)
            arcTo(3.5f, 3.5f, 0f, false, false, 5f, 20.5f)
            close()
            // Front wheel
            moveTo(18f, 12f)
            arcTo(5f, 5f, 0f, false, true, 13f, 17f)
            arcTo(5f, 5f, 0f, false, true, 18f, 22f)
            arcTo(5f, 5f, 0f, false, true, 23f, 17f)
            arcTo(5f, 5f, 0f, false, true, 18f, 12f)
            close()
            moveTo(18f, 20.5f)
            arcTo(3.5f, 3.5f, 0f, false, false, 14.5f, 17f)
            arcTo(3.5f, 3.5f, 0f, false, false, 18f, 13.5f)
            arcTo(3.5f, 3.5f, 0f, false, false, 21.5f, 17f)
            arcTo(3.5f, 3.5f, 0f, false, false, 18f, 20.5f)
            close()
            // Handlebar / pedal circle
            moveTo(15.5f, 5.5f)
            arcTo(2f, 2f, 0f, false, false, 17.5f, 3.5f)
            arcTo(2f, 2f, 0f, false, false, 15.5f, 1.5f)
            arcTo(2f, 2f, 0f, false, false, 13.5f, 3.5f)
            arcTo(2f, 2f, 0f, false, false, 15.5f, 5.5f)
            close()
            // Frame
            moveTo(10.8f, 10.5f)
            lineTo(13.2f, 8.1f)
            lineTo(14f, 8.9f)
            arcTo(6.5f, 6.5f, 0f, false, true, 19.1f, 11f)
            lineTo(20.4f, 12.3f)
            lineTo(19.3f, 13.4f)
            lineTo(18f, 12.1f)
            lineTo(16.9f, 12.1f)
            arcTo(6.5f, 6.5f, 0f, false, true, 12f, 10f)
            lineTo(11.2f, 9.2f)
            lineTo(10.8f, 10.5f)
            close()
            moveTo(15f, 10.6f)
            arcTo(5f, 5f, 0f, false, true, 14f, 10.5f)
            lineTo(15.1f, 11.6f)
            lineTo(16.5f, 11.6f)
            lineTo(15.4f, 10.5f)
            lineTo(15f, 10.6f)
            close()
        }.nodes,
    )
}.build()

/** Calendar icon for Calendar. */
val wtCalendarMonth: ImageVector = ImageVector.Builder(
    name = "wtCalendarMonth",
    defaultWidth = 24.dp,
    defaultHeight = 24.dp,
    viewportWidth = 24f,
    viewportHeight = 24f,
).apply {
    addPath(
        fill = whiteFill,
        pathData = PathBuilder().apply {
            moveTo(19f, 3f)
            lineTo(18f, 3f)
            lineTo(18f, 1f)
            lineTo(16f, 1f)
            lineTo(16f, 3f)
            lineTo(8f, 3f)
            lineTo(8f, 1f)
            lineTo(6f, 1f)
            lineTo(6f, 3f)
            lineTo(5f, 3f)
            arcTo(1f, 1f, 0f, false, false, 4f, 4f)
            lineTo(4f, 19f)
            arcTo(1f, 1f, 0f, false, false, 5f, 20f)
            lineTo(19f, 20f)
            arcTo(1f, 1f, 0f, false, false, 20f, 19f)
            lineTo(20f, 4f)
            arcTo(1f, 1f, 0f, false, false, 19f, 3f)
            close()
            moveTo(19f, 18f)
            lineTo(5f, 18f)
            lineTo(5f, 8f)
            lineTo(19f, 8f)
            close()
            moveTo(7f, 10f)
            lineTo(12f, 10f)
            lineTo(12f, 15f)
            lineTo(7f, 15f)
            close()
        }.nodes,
    )
}.build()

/** Bar chart icon for Volume. */
val wtBarChart: ImageVector = ImageVector.Builder(
    name = "wtBarChart",
    defaultWidth = 24.dp,
    defaultHeight = 24.dp,
    viewportWidth = 24f,
    viewportHeight = 24f,
).apply {
    addPath(
        fill = whiteFill,
        pathData = PathBuilder().apply {
            moveTo(5f, 9.2f)
            lineTo(8f, 9.2f)
            lineTo(8f, 19f)
            lineTo(5f, 19f)
            close()
            moveTo(10.6f, 5f)
            lineTo(13.4f, 5f)
            lineTo(13.4f, 19f)
            lineTo(10.6f, 19f)
            close()
            moveTo(16.2f, 13f)
            lineTo(19f, 13f)
            lineTo(19f, 19f)
            lineTo(16.2f, 19f)
            close()
        }.nodes,
    )
}.build()
