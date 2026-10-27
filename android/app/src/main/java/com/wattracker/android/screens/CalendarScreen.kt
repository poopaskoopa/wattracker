package com.wattracker.android.screens

import android.content.res.Configuration
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.hideFromAccessibility
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.viewmodel.compose.viewModel
import com.wattracker.android.R
import com.wattracker.android.WatTrackerApp
import com.wattracker.android.cloud.CalendarMonth
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.ReadModel
import com.wattracker.android.json.JsonValue
import com.wattracker.android.screens.activities.ChartTicks
import com.wattracker.android.screens.activities.TickScale
import com.wattracker.android.screens.activities.chartTickLabel
import com.wattracker.android.screens.activities.updatedAt
import com.wattracker.android.screens.calendar.CalendarData
import com.wattracker.android.screens.calendar.CalendarDayEntry
import com.wattracker.android.screens.calendar.CalendarJSON
import com.wattracker.android.screens.calendar.CalendarUiState
import com.wattracker.android.screens.calendar.CalendarViewModel
import com.wattracker.android.screens.calendar.CalendarWorkoutFormatting
import com.wattracker.android.screens.calendar.CalendarWorkoutProfileBlock
import com.wattracker.android.screens.calendar.CalendarWorkoutProfileDecoder
import com.wattracker.android.screens.calendar.WorkoutStatus
import com.wattracker.android.shell.Panel
import com.wattracker.android.shell.ScrollableScreenScaffold
import com.wattracker.android.ui.theme.Palette
import com.wattracker.android.ui.theme.wtChevronLeft
import com.wattracker.android.ui.theme.wtChevronRight
import com.wattracker.android.ui.theme.wtFlag
import com.wattracker.android.ui.theme.wtFlight
import com.wattracker.android.ui.theme.wtPedalBike
import kotlinx.coroutines.CancellationException
import kotlin.math.max

/**
 * Calendar (#198): the month grid of plan, rides and races, and one day's
 * detail.
 *
 * Layout decision (issue #198's "hard case", stated here as #197's was in
 * its layout): on the wide, short viewports -- a phone in landscape and any
 * tablet -- the grid and the day detail sit side by side; width is not the
 * scarce axis there and push navigation would cost the rider a round trip on
 * the scarce one. The month grid is the widest fixed-geometry thing in the
 * app (seven cells per row), so the grid pane keeps its natural width and
 * the detail takes the remainder. A phone in portrait has no spare width:
 * the day detail opens as a bottom sheet over the grid, and the back
 * gesture dismisses it.
 *
 * The cells are keyed by ISO date (the parity table's rule, from the #431
 * bug): one identity per real date, so no cell can be reused for another
 * day and shift the month's weekdays.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CalendarScreen(
    onOpenRide: (Int) -> Unit = {},
    onNavigateToSettings: (() -> Unit)? = null,
) {
    val context = LocalContext.current
    val activeSource by WatTrackerApp.dataSourceFlow.collectAsState()

    val readModelResult by produceState<Result<ReadModel>?>(initialValue = null, activeSource) {
        value = try {
            Result.success(WatTrackerApp.readModel(context))
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    val currentReadModel = readModelResult?.getOrNull()
    if (currentReadModel == null) {
        ScrollableScreenScaffold(
            title = stringResource(R.string.destination_calendar),
            subtitle = stringResource(R.string.screen_calendar_subtitle),
        ) {
            if (readModelResult == null) {
                Text(
                    text = stringResource(R.string.dashboard_starting),
                    style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                )
            } else {
                Text(
                    text = stringResource(R.string.dashboard_start_failed),
                    style = TextStyle(fontSize = 14.sp, color = Palette.alert),
                )
            }
        }
        return
    }

    val factory = remember(currentReadModel, activeSource) {
        object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                return CalendarViewModel(currentReadModel) as T
            }
        }
    }
    val viewModel: CalendarViewModel = viewModel(key = activeSource.name, factory = factory)
    val uiState by viewModel.uiState.collectAsState()

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, viewModel) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) viewModel.refresh()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    val configuration = LocalConfiguration.current
    val wideShort = configuration.orientation == Configuration.ORIENTATION_LANDSCAPE ||
        configuration.smallestScreenWidthDp >= 600

    if (wideShort) {
        Row(modifier = Modifier.fillMaxSize()) {
            PullToRefreshBox(
                isRefreshing = uiState.isRefreshing,
                onRefresh = { viewModel.pullToRefresh() },
                // 520dp on a tablet is not styling: seven detailed cells
                // (number, ooto/race marks, phase, counts) need ~64dp each,
                // and the grid is the widest fixed geometry in the app. A
                // landscape phone gets 400dp and honestly falls back to the
                // compact cell -- the width fact is the cell's, not a flag.
                modifier = Modifier
                    .width(if (configuration.smallestScreenWidthDp >= 600) 520.dp else 400.dp)
                    .fillMaxHeight(),
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(Palette.bg)
                        .verticalScroll(rememberScrollState())
                        .padding(16.dp),
                ) {
                    CalendarContent(
                        uiState = uiState,
                        viewModel = viewModel,
                        onNavigateToSettings = onNavigateToSettings,
                        showsOwnHeader = true,
                    )
                }
            }

            Box(
                modifier = Modifier
                    .fillMaxHeight()
                    .width(1.dp)
                    .background(Palette.surfaceBorder),
            )

            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Palette.bg)
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp),
            ) {
                val day = uiState.selectedDay
                if (day != null) {
                    CalendarDayDetail(day = day, onOpenRide = onOpenRide)
                } else {
                    Text(
                        text = stringResource(R.string.calendar_tap_hint),
                        style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                    )
                }
            }
        }
    } else {
        ScrollableScreenScaffold(
            title = stringResource(R.string.destination_calendar),
            subtitle = stringResource(R.string.screen_calendar_subtitle),
            isRefreshing = uiState.isRefreshing,
            onRefresh = { viewModel.pullToRefresh() },
        ) {
            CalendarContent(
                uiState = uiState,
                viewModel = viewModel,
                onNavigateToSettings = onNavigateToSettings,
                // The scaffold already paints "Calendar" and its subtitle;
                // the content adds only the state chrome below it.
                showsOwnHeader = false,
            )
        }

        val day = uiState.selectedDay
        if (day != null) {
            ModalBottomSheet(
                onDismissRequest = { viewModel.selectDay(null) },
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .verticalScroll(rememberScrollState())
                        .padding(horizontal = 16.dp)
                        .padding(bottom = 24.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    CalendarDayDetail(day = day, onOpenRide = {
                        // Dismiss the sheet, then let the shell push the
                        // shared ride detail: one ride screen in the app,
                        // fed the same way the activities list feeds it.
                        viewModel.selectDay(null)
                        onOpenRide(it)
                    })
                }
            }
        }
    }
}

// MARK: - The grid pane

/**
 * Everything the grid column renders: the header, the syncing banner, the
 * month (or the honest state for it) and the tap hint.
 */
@Composable
private fun CalendarContent(
    uiState: CalendarUiState,
    viewModel: CalendarViewModel,
    onNavigateToSettings: (() -> Unit)?,
    showsOwnHeader: Boolean,
) {
    // The badge follows a cached paint whether it produced a grid or an empty
    // month. The cloud's read seam answers from the cache when its network step
    // fails, so a month that looks empty while offline is an empty the app
    // inferred from what it already knew, and it must not read as a server
    // answer.
    val isCached = uiState.data?.source == CloudSnapshot.Source.cache ||
        (uiState.monthIsEmpty && uiState.monthIsEmptyFromCache)
    Column(verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
        if (showsOwnHeader) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = stringResource(R.string.destination_calendar),
                    style = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
                Spacer(modifier = Modifier.weight(1f))
                if (isCached) {
                    Text(
                        text = stringResource(R.string.activities_offline_badge),
                        style = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.Medium, color = Palette.muted),
                    )
                }
            }
        } else if (isCached) {
            Text(
                text = stringResource(R.string.activities_offline_badge),
                style = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.Medium, color = Palette.muted),
                modifier = Modifier.fillMaxWidth(),
                textAlign = TextAlign.End,
            )
        }
        if (uiState.data != null) {
            Text(
                text = if (uiState.data.asOf > 0) {
                    stringResource(R.string.calendar_updated, updatedAt(uiState.data.asOf))
                } else {
                    stringResource(R.string.screen_calendar_subtitle)
                },
                style = TextStyle(fontSize = 14.sp, color = Palette.muted),
            )
        } else if (showsOwnHeader) {
            Text(
                text = stringResource(R.string.screen_calendar_subtitle),
                style = TextStyle(fontSize = 14.sp, color = Palette.muted),
            )
        }
        if (uiState.isWaking) {
            Text(
                text = stringResource(R.string.dashboard_waking_title),
                style = TextStyle(fontSize = 12.sp, color = Palette.muted),
            )
        }
        val throttled = uiState.throttledRetrySeconds
        if (throttled != null) {
            Text(
                text = stringResource(R.string.dashboard_throttled_notice, throttled),
                style = TextStyle(fontSize = 12.sp, color = Palette.alert),
            )
        }
        val errorLine = uiState.error
        if (errorLine != null && uiState.data != null) {
            Text(
                text = errorLine,
                style = TextStyle(fontSize = 12.sp, color = Palette.alert),
                maxLines = 1,
            )
        }
        if (uiState.isLoading && uiState.data != null) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Palette.surfaceInset)
                    .padding(horizontal = 12.dp, vertical = 6.dp),
            ) {
                Text(
                    text = stringResource(R.string.calendar_syncing),
                    style = TextStyle(fontSize = 12.sp, color = Palette.muted),
                )
            }
        }

        // The month is navigable in every paired state -- with a grid, with an
        // error, with the spinner, and with a month the backend answered empty.
        val isPairedState = uiState.isPaired &&
            uiState.deviceState != CloudSession.DeviceState.unpaired &&
            uiState.deviceState != CloudSession.DeviceState.removed
        if (isPairedState) {
            CalendarMonthNavigation(
                month = uiState.month,
                onMoveMonth = { offset -> viewModel.moveMonth(offset) },
            )
        }

        val data = uiState.data
        if (data != null) {
            CalendarMonthGrid(
                uiState = uiState,
                onSelectDay = { iso -> viewModel.selectDay(if (iso == uiState.selectedDate) null else iso) },
            )
            Text(
                text = stringResource(R.string.calendar_tap_hint),
                style = TextStyle(fontSize = 12.sp, color = Palette.muted),
                modifier = Modifier.padding(horizontal = 4.dp),
            )
        } else {
            CalendarStates(uiState = uiState, onNavigateToSettings = onNavigateToSettings)
        }
    }
}

/**
 * The month stepper.
 *
 * It is painted above the month's body rather than inside the grid panel, and
 * that placement is the whole point. It used to live in the grid panel, so a
 * month the backend answered empty had no chevrons at all: on the local
 * backend one step past the end of the plan -- or the 1st of a month with no
 * plan and no ride yet, which is where the screen opens -- the rider was stuck
 * there until they changed backend or killed the app. iOS's status panel has
 * no chevrons either; always offering the stepper is Android's deliberate
 * difference, recorded in `docs/mobile-parity.md`.
 *
 * The chevrons are vendored vectors, and Material mirrors its own icons by
 * name -- not these. In a right-to-left locale the previous month is the one
 * on the right, so mirroring them is ours to do.
 */
@Composable
private fun CalendarMonthNavigation(month: CalendarMonth, onMoveMonth: (Int) -> Unit) {
    val mirrored = if (LocalLayoutDirection.current == LayoutDirection.Rtl) {
        Modifier.scale(-1f, 1f)
    } else {
        Modifier
    }
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        IconButton(onClick = { onMoveMonth(-1) }) {
            Icon(
                imageVector = wtChevronLeft,
                contentDescription = stringResource(R.string.calendar_prev_month),
                modifier = mirrored,
                tint = Palette.accent,
            )
        }
        Text(
            text = month.title(),
            style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            modifier = Modifier.weight(1f),
            textAlign = TextAlign.Center,
        )
        IconButton(onClick = { onMoveMonth(1) }) {
            Icon(
                imageVector = wtChevronRight,
                contentDescription = stringResource(R.string.calendar_next_month),
                modifier = mirrored,
                tint = Palette.accent,
            )
        }
    }
}

/**
 * The month grid: the weekday header and the Monday-to-Sunday grid in full
 * weeks covering the month -- the desktop's own layout, pinned by
 * `calendar_grid_v1.json`. Rows are real weeks, cells are real dates: the
 * leading and trailing cells are the adjacent months' days, present as
 * context and receded, never blanks.
 */
@Composable
private fun CalendarMonthGrid(
    uiState: CalendarUiState,
    onSelectDay: (String) -> Unit,
) {
    val data = uiState.data ?: return
    val month = uiState.month
    val grid = remember(month) { month.gridDates() }
    // The two names a cell outside the month can be announced by. ISO dates
    // sort chronologically as strings, so a date before the 1st belongs to the
    // month before and a date after it to the month after.
    val firstIso = remember(month) { month.isoDate(1) }
    val previousTitle = remember(month) { month.addingMonths(-1).title() }
    val nextTitle = remember(month) { month.addingMonths(1).title() }
    Panel(modifier = Modifier.fillMaxWidth()) {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(modifier = Modifier.fillMaxWidth()) {
                weekdayLabels().forEach { label ->
                    Text(
                        text = label,
                        style = TextStyle(fontSize = 10.sp, fontWeight = FontWeight.SemiBold, color = Palette.muted),
                        modifier = Modifier.weight(1f),
                        textAlign = TextAlign.Center,
                    )
                }
            }

            // Whole weeks, seven cells each; cell identity is the ISO date.
            grid.chunked(7).forEach { week ->
                Row(modifier = Modifier.fillMaxWidth()) {
                    week.forEach { cell ->
                        val entry = data.entry(cell.isoDate)
                        Box(modifier = Modifier.weight(1f).padding(2.dp)) {
                            CalendarDayCell(
                                day = cell.day,
                                isInMonth = cell.isInMonth,
                                entry = entry,
                                a11yLabel = dayCellLabel(cell, firstIso, previousTitle, nextTitle),
                                onClick = { onSelectDay(cell.isoDate) },
                            )
                        }
                    }
                }
            }
        }
    }
}

/**
 * What a cell is announced as. The bare number is not enough: every month has
 * a 12th, and an adjacent month's 1st otherwise sounds exactly like this
 * month's, so the neighbour's month is named too.
 */
@Composable
private fun dayCellLabel(
    cell: CalendarMonth.GridDay,
    firstIso: String,
    previousTitle: String,
    nextTitle: String,
): String = if (cell.isInMonth) {
    stringResource(R.string.calendar_a11y_day, cell.day)
} else {
    stringResource(
        R.string.calendar_a11y_adjacent_day,
        cell.day,
        if (cell.isoDate < firstIso) previousTitle else nextTitle,
    )
}

/** The weekday header, Monday first: the grid's first column is Monday. */
@Composable
private fun weekdayLabels(): List<String> = listOf(
    stringResource(R.string.calendar_weekday_mon),
    stringResource(R.string.calendar_weekday_tue),
    stringResource(R.string.calendar_weekday_wed),
    stringResource(R.string.calendar_weekday_thu),
    stringResource(R.string.calendar_weekday_fri),
    stringResource(R.string.calendar_weekday_sat),
    stringResource(R.string.calendar_weekday_sun),
)

/**
 * One day. Detailed when the cell is wide enough (a landscape phone, a
 * tablet): number, ooto and race marks, the training phase, and the
 * workout/ride counts with the missed warning. Compact when it is not (a
 * portrait phone's seven columns): just the number and a content dot. The
 * threshold is a cell-width fact, so the two idioms cannot be configured
 * out of sync with the space they are drawn in.
 */
@Composable
private fun CalendarDayCell(
    day: Int,
    isInMonth: Boolean,
    entry: CalendarDayEntry?,
    a11yLabel: String,
    onClick: () -> Unit,
) {
    BoxWithConstraints(
        modifier = Modifier
            .fillMaxWidth()
            .background(
                cellBackground(entry, isInMonth),
                RoundedCornerShape(8.dp),
            )
            .border(
                1.dp,
                // The border is the cell-content flag, not the month's
                // `hasContent`: a plan window stamps a phase on every day it
                // covers, and bordering those made the accent mean "the coach
                // wrote something this month" instead of "something happened
                // here".
                if (entry?.hasCellContent == true) {
                    Palette.accent.copy(alpha = if (isInMonth) 0.55f else 0.3f)
                } else {
                    Palette.surfaceBorder
                },
                RoundedCornerShape(8.dp),
            )
            // One thing to the rider, so one thing to TalkBack, named by its
            // date rather than by the digits inside it.
            .semantics { contentDescription = a11yLabel }
            .clickable(onClick = onClick)
            .padding(6.dp),
    ) {
        if (maxWidth < 48.dp) {
            Column(
                modifier = Modifier.fillMaxWidth(),
                horizontalAlignment = Alignment.Start,
                verticalArrangement = Arrangement.spacedBy(3.dp),
            ) {
                Text(
                    text = "$day",
                    // The cell already names the day; the bare digit would be
                    // read a second time.
                    modifier = Modifier.semantics { hideFromAccessibility() },
                    style = TextStyle(
                        fontSize = 12.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = if (isInMonth) Palette.textBright else Palette.muted,
                    ),
                )
                if (entry?.hasCellContent == true) {
                    Box(
                        modifier = Modifier
                            .size(5.dp)
                            .background(
                                if (entry.ooto) Palette.accent else Palette.ok,
                                RoundedCornerShape(50),
                            ),
                    )
                }
            }
        } else {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(44.dp),
                verticalArrangement = Arrangement.spacedBy(3.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = "$day",
                        modifier = Modifier.semantics { hideFromAccessibility() },
                        style = TextStyle(
                            fontSize = 13.sp,
                            fontWeight = FontWeight.SemiBold,
                            color = if (isInMonth) Palette.textBright else Palette.muted,
                        ),
                    )
                    Spacer(modifier = Modifier.weight(1f))
                    if (entry?.ooto == true) {
                        Icon(
                            imageVector = wtFlight,
                            contentDescription = stringResource(R.string.calendar_ooto),
                            modifier = Modifier.size(11.dp),
                            tint = Palette.accent,
                        )
                    }
                    if (entry?.race != null) {
                        Icon(
                            imageVector = wtFlag,
                            contentDescription = stringResource(R.string.calendar_race_label),
                            modifier = Modifier.size(11.dp),
                            tint = Palette.alert,
                        )
                    }
                }
                val phaseText = entry?.phase?.takeUnless { it.isEmpty() }
                if (phaseText != null) {
                    Text(
                        text = phaseText,
                        style = TextStyle(fontSize = 9.sp, color = Palette.accent),
                        maxLines = 1,
                    )
                }
                if (entry != null && (entry.workoutCount > 0 || entry.activityCount > 0 || entry.missedCount() > 0)) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(5.dp),
                    ) {
                        if (entry.workoutCount > 0) {
                            Icon(
                                imageVector = wtPedalBike,
                                contentDescription = stringResource(R.string.calendar_a11y_workouts, entry.workoutCount),
                                modifier = Modifier.size(11.dp),
                                tint = Palette.muted,
                            )
                            Text(text = "${entry.workoutCount}", style = TextStyle(fontSize = 10.sp, color = Palette.muted))
                        }
                        if (entry.activityCount > 0) {
                            Icon(
                                imageVector = Icons.Filled.CheckCircle,
                                contentDescription = stringResource(R.string.calendar_a11y_rides, entry.activityCount),
                                modifier = Modifier.size(11.dp),
                                tint = Palette.muted,
                            )
                            Text(text = "${entry.activityCount}", style = TextStyle(fontSize = 10.sp, color = Palette.muted))
                        }
                        if (entry.missedCount() > 0) {
                            Icon(
                                imageVector = Icons.Filled.Warning,
                                contentDescription = stringResource(R.string.calendar_a11y_missed, entry.missedCount()),
                                modifier = Modifier.size(11.dp),
                                tint = Palette.alert,
                            )
                        }
                    }
                }
            }
        }
    }
}

private fun cellBackground(entry: CalendarDayEntry?, isInMonth: Boolean): Color = when {
    entry?.ooto == true -> Palette.surface2.copy(alpha = if (isInMonth) 1f else 0.5f)
    isInMonth -> Palette.panel
    else -> Palette.surfaceInset
}

/** The day-cell warning count, read the way the model reads it. */
private fun CalendarDayEntry.missedCount(): Int = CalendarJSON.missedCount(workouts)

// MARK: - The day's honest states (when there is no month to show)

/**
 * The states shown in the body when there is no month grid to show: the
 * unpaired/removed cards, a failed load with no cache, the waking notice, the
 * first-load spinner, a month that was read and held nothing, and the
 * nothing-ever-published case. Same order of meaning as the activities list's:
 * a failed load is never presented as "no data", and a month with nothing in
 * it is never presented as a backend that has published nothing. The month
 * stepper is not part of this -- it is painted above the body in every paired
 * state, so every one of these is a state the rider can step out of.
 */
@Composable
private fun CalendarStates(
    uiState: CalendarUiState,
    onNavigateToSettings: (() -> Unit)?,
) {
    when {
        uiState.deviceState == CloudSession.DeviceState.removed -> CalendarStateCard(
            title = stringResource(R.string.calendar_removed_title),
            note = stringResource(R.string.calendar_removed_note),
            onNavigateToSettings = onNavigateToSettings,
        )

        !uiState.isPaired || uiState.deviceState == CloudSession.DeviceState.unpaired -> CalendarStateCard(
            title = stringResource(R.string.calendar_unpaired_title),
            note = stringResource(R.string.calendar_unpaired_note),
            onNavigateToSettings = onNavigateToSettings,
        )

        uiState.error != null -> Panel {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(
                    text = stringResource(R.string.calendar_unavailable),
                    style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
                Text(
                    text = uiState.error,
                    style = TextStyle(fontSize = 13.sp, color = Palette.alert),
                )
            }
        }

        uiState.isWaking -> Panel {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(
                    text = stringResource(R.string.dashboard_waking_title),
                    style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
                Text(
                    text = stringResource(R.string.dashboard_waking_notice),
                    style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                )
            }
        }

        uiState.isStarting || uiState.isLoading -> Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(140.dp)
                .background(Palette.surfaceInset),
            contentAlignment = Alignment.Center,
        ) {
            CircularProgressIndicator(modifier = Modifier.size(24.dp), color = Palette.accent, strokeWidth = 2.dp)
        }

        // Read successfully, and this month holds nothing: a month before the
        // first ride, or after the end of the plan. "Your paired backend has
        // not published calendar data" would be wrong twice over here -- it has
        // published, and this is not the only month there is.
        uiState.monthIsEmpty -> Panel {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(
                    text = stringResource(R.string.calendar_month_empty_title),
                    style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
                Text(
                    text = stringResource(R.string.calendar_month_empty_note, uiState.month.title()),
                    style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                )
            }
        }

        else -> CalendarStateCard(
            title = stringResource(R.string.calendar_no_data_title),
            note = stringResource(R.string.calendar_no_data_note),
            onNavigateToSettings = null,
        )
    }
}

@Composable
private fun CalendarStateCard(
    title: String,
    note: String,
    onNavigateToSettings: (() -> Unit)?,
) {
    Panel {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                text = title,
                style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
            )
            Text(text = note, style = TextStyle(fontSize = 13.sp, color = Palette.muted))
            if (onNavigateToSettings != null) {
                OutlinedButton(onClick = onNavigateToSettings) {
                    Text(stringResource(R.string.dashboard_pair_cta))
                }
            }
        }
    }
}

// MARK: - Day detail (the wide pane, or the portrait sheet)

/**
 * One day: the training context, the planned workouts with their power
 * profiles, and the completed rides. The ride rows open the same ride detail
 * screen the activities list uses -- one ride screen in the app, and the
 * parity table's "Ride graphs chart in the calendar" holds by reuse rather
 * than by a second chart to keep in sync.
 */
@Composable
private fun CalendarDayDetail(day: CalendarDayEntry, onOpenRide: (Int) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
        Panel {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(
                    text = CalendarJSON.displayDate(day.dateISO),
                    style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                )
                if (day.ooto) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Icon(imageVector = wtFlight, contentDescription = null, modifier = Modifier.size(14.dp), tint = Palette.accent)
                        Text(
                            text = stringResource(R.string.calendar_ooto),
                            style = TextStyle(fontSize = 13.sp, color = Palette.accent),
                        )
                    }
                }
                day.phase?.takeUnless { it.isEmpty() }?.let { phase ->
                    DetailRow(stringResource(R.string.calendar_phase_label), phase)
                }
                day.race?.let { race ->
                    DetailRow(stringResource(R.string.calendar_race_label), CalendarJSON.raceTitle(race))
                }
            }
        }

        if (day.workouts.isNotEmpty()) {
            Panel {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text(
                        text = stringResource(R.string.calendar_workouts_title),
                        style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                    )
                    day.workouts.forEachIndexed { index, workout ->
                        if (index > 0) {
                            Box(modifier = Modifier.fillMaxWidth().height(1.dp).background(Palette.surfaceBorder))
                        }
                        CalendarWorkoutRow(workout, day.currentFTP)
                    }
                }
            }
        }

        if (day.activities.isNotEmpty()) {
            Panel {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(
                        text = stringResource(R.string.calendar_rides_title),
                        style = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.SemiBold, color = Palette.textBright),
                    )
                    day.activities.forEach { activity ->
                        CalendarActivityRow(activity) {
                            CalendarData.activityID(activity)?.let(onOpenRide)
                        }
                    }
                }
            }
        }

        if (day.workouts.isEmpty() && day.activities.isEmpty()) {
            Panel {
                Text(
                    text = stringResource(R.string.calendar_day_empty),
                    style = TextStyle(fontSize = 14.sp, color = Palette.muted),
                )
            }
        }
    }
}

@Composable
private fun DetailRow(label: String, value: String) {
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(text = label, style = TextStyle(fontSize = 13.sp, color = Palette.muted))
        Spacer(modifier = Modifier.weight(1f))
        Text(
            text = value,
            style = TextStyle(fontSize = 13.sp, color = Palette.text),
            textAlign = TextAlign.End,
        )
    }
}

/** One planned workout: the row, its status, and -- when it has one -- the power profile. */
@Composable
private fun CalendarWorkoutRow(workout: JsonValue, ftp: Double?) {
    val status = CalendarJSON.workoutStatus(workout)
    val blocks = remember(workout) { CalendarWorkoutProfileDecoder.blocks(workout) }
    Column(verticalArrangement = Arrangement.spacedBy(3.dp), modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.Top, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(
                // "Workout", iOS's fallback: an unnamed planned block is still
                // a workout, not the name of the screen the rider is standing
                // in.
                text = CalendarJSON.title(workout, fallback = stringResource(R.string.calendar_workout_default)),
                style = TextStyle(fontSize = 14.sp, fontWeight = FontWeight.SemiBold, color = Palette.text),
                modifier = Modifier.weight(1f),
            )
            Text(
                text = stringResource(statusLabelRes(status)),
                style = TextStyle(fontSize = 12.sp, color = statusColor(status)),
            )
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            CalendarJSON.string(workout, "type")?.let {
                Text(text = it, style = TextStyle(fontSize = 12.sp, color = Palette.muted))
            }
            CalendarJSON.duration(workout)?.let {
                Text(text = it, style = TextStyle(fontSize = 12.sp, color = Palette.muted))
            }
            CalendarJSON.number(workout, "tss")?.let {
                Text(
                    text = "TSS ${CalendarJSON.numberString(it)}",
                    style = TextStyle(fontSize = 12.sp, color = Palette.muted),
                )
            }
        }
        if (blocks.isNotEmpty()) {
            // Rounded, not truncated: the steps under this line are computed
            // from the unrounded FTP, so "at FTP 249 W" next to watts built on
            // 249.7 contradicts itself two lines down.
            CalendarWorkoutFormatting.ftpWatts(ftp)?.let { watts ->
                Text(
                    text = stringResource(R.string.calendar_at_ftp, watts),
                    style = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = Palette.accent),
                )
            }
            CalendarWorkoutProfileChart(
                blocks = blocks,
                ftp = ftp,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(190.dp),
            )
            val descriptions = CalendarWorkoutFormatting.stepDescriptions(blocks, ftp)
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                descriptions.forEach { description ->
                    Text(
                        text = description,
                        style = TextStyle(fontSize = 13.sp, fontFamily = FontFamily.Monospace, color = Palette.text),
                    )
                }
                CalendarWorkoutFormatting.coachingTexts(blocks).forEach { text ->
                    Text(text = text, style = TextStyle(fontSize = 12.sp, color = Palette.muted))
                }
            }
        }
    }
}

private fun statusLabelRes(status: WorkoutStatus): Int = when (status) {
    WorkoutStatus.Completed -> R.string.calendar_status_completed
    WorkoutStatus.Missed -> R.string.calendar_status_missed
    WorkoutStatus.Skipped -> R.string.calendar_status_skipped
    WorkoutStatus.Adapted -> R.string.calendar_status_adapted
    WorkoutStatus.Planned -> R.string.calendar_status_planned
}

/** The status colours follow iOS: ok green, alert red, accent for adapted, muted for planned. */
private fun statusColor(status: WorkoutStatus): Color = when (status) {
    WorkoutStatus.Completed -> Palette.ok
    WorkoutStatus.Missed, WorkoutStatus.Skipped -> Palette.alert
    WorkoutStatus.Adapted -> Palette.accent
    WorkoutStatus.Planned -> Palette.muted
}

private val profileChartLabelStyle = TextStyle(fontSize = 9.sp, color = Palette.muted)

/**
 * The planned workout's power-profile chart: the published %FTP targets
 * scaled to watts when the rider has an FTP (and to %FTP x 100 when they do
 * not -- iOS's choice, kept so both phones read a targetless rider's plan
 * the same way), a fill opacity per the seven power zones, the FTP dashed
 * across when it exists, and time in seconds. The shape per block: a target
 * that may step between the block's start and end, filled from the baseline
 * and outlined on top -- the same step picture the desktop paints.
 *
 * The axis is built to hold the FTP line, not only the targets. A Z2 ride at
 * FTP 250 tops out near 200 W, and an axis that stops at 200 has nowhere to
 * put the line -- while that line is the one thing that makes a workout read
 * as under or over threshold. The desktop's own `maxW` starts at the FTP for
 * exactly that reason (`workout_graph.js`), and iOS's chart widens for its
 * FTP `RuleMark` the same way.
 *
 * What can be measured is measured in composition and remembered; the canvas
 * only places it. `RideGraphsChart` works the same way -- measuring inside the
 * draw lambda lays every label out again on every frame.
 */
@Composable
private fun CalendarWorkoutProfileChart(
    blocks: List<CalendarWorkoutProfileBlock>,
    ftp: Double?,
    modifier: Modifier = Modifier,
) {
    val textMeasurer = rememberTextMeasurer()
    val hasFtp = ftp != null && ftp.isFinite() && ftp > 0
    val powerAxisTitle = stringResource(
        if (hasFtp) R.string.calendar_chart_power_axis else R.string.calendar_chart_target_axis,
    )
    val timeAxisTitle = stringResource(R.string.calendar_chart_time_axis)
    val ftpLabel = stringResource(R.string.calendar_chart_ftp_label)
    val noTargets = stringResource(R.string.calendar_chart_no_targets)

    val steps = remember(blocks, ftp) {
        blocks.filter {
            !it.free &&
                CalendarWorkoutFormatting.chartTarget(it.targetStart, ftp) != null &&
                CalendarWorkoutFormatting.chartTarget(it.targetEnd, ftp) != null
        }
    }
    val timeMax = remember(blocks) { blocks.maxOfOrNull { it.end } ?: 0.0 }
    val scale: TickScale = remember(steps, ftp, hasFtp) {
        val targets = steps.flatMap {
            listOfNotNull(
                CalendarWorkoutFormatting.chartTarget(it.targetStart, ftp),
                CalendarWorkoutFormatting.chartTarget(it.targetEnd, ftp),
            )
        }
        val ceiling = max(targets.maxOrNull() ?: 0.0, ftp?.takeIf { it.isFinite() && it > 0 } ?: 0.0)
        // The same headroom the targets get, so a workout whose top step *is*
        // FTP does not print its line along the top gridline.
        ChartTicks.nice(0.0, if (ceiling <= 0) 1.0 else ceiling * 1.1, 4)
    }
    // Tick text keeps one decimal only when the value needs it: a ramp whose
    // axis steps by 0.5 must not read "0, 0, 1, 1".
    val tickLabels = remember(scale, textMeasurer) {
        scale.ticks.map { textMeasurer.measure(chartTickLabel(it), profileChartLabelStyle) }
    }
    val ftpLabelResult = remember(ftpLabel, textMeasurer) { textMeasurer.measure(ftpLabel, profileChartLabelStyle) }
    val zeroLabel = remember(textMeasurer) { textMeasurer.measure("0", profileChartLabelStyle) }
    val lengthLabel = remember(timeMax, textMeasurer) { textMeasurer.measure(chartTickLabel(timeMax), profileChartLabelStyle) }
    val yTitleResult = remember(powerAxisTitle, textMeasurer) { textMeasurer.measure(powerAxisTitle, profileChartLabelStyle) }
    val xTitleResult = remember(timeAxisTitle, textMeasurer) { textMeasurer.measure(timeAxisTitle, profileChartLabelStyle) }

    Box(
        modifier = modifier
            .background(Palette.surfaceInset, RoundedCornerShape(6.dp))
            .padding(6.dp),
    ) {
        if (steps.isEmpty() || timeMax <= 0 || !timeMax.isFinite()) {
            // This sentence sits directly under a planned workout's name, so
            // "nothing planned or recorded on this day" is a contradiction
            // here. The blocks exist; none of them carries a target to plot.
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    text = noTargets,
                    style = TextStyle(fontSize = 13.sp, color = Palette.muted),
                )
            }
        } else {
            Canvas(modifier = Modifier.fillMaxSize()) {
                val padLeft = 34.dp.toPx()
                val padRight = 8.dp.toPx()
                val padTop = 14.dp.toPx()
                val padBottom = 22.dp.toPx()
                val plotLeft = padLeft
                val plotRight = size.width - padRight
                val plotTop = padTop
                val plotBottom = size.height - padBottom

                fun xPos(seconds: Double): Float =
                    plotLeft + (seconds / timeMax).toFloat() * (plotRight - plotLeft)

                fun yPos(value: Double): Float =
                    plotBottom - (value / scale.upper).toFloat() * (plotBottom - plotTop)

                // Grid and y labels on the nice ticks.
                scale.ticks.forEachIndexed { index, tick ->
                    val y = yPos(tick)
                    drawLine(
                        color = Palette.surfaceBorder,
                        start = Offset(plotLeft, y),
                        end = Offset(plotRight, y),
                        strokeWidth = 1f,
                    )
                    val label = tickLabels[index]
                    drawText(
                        textLayoutResult = label,
                        topLeft = Offset(4.dp.toPx(), y - label.size.height / 2f),
                    )
                }

                // One step per block: filled from the baseline, outlined on top.
                for (step in steps) {
                    val startTarget = CalendarWorkoutFormatting.chartTarget(step.targetStart, ftp) ?: continue
                    val endTarget = CalendarWorkoutFormatting.chartTarget(step.targetEnd, ftp) ?: continue
                    val opacity = CalendarWorkoutFormatting.zoneOpacity(
                        ((step.targetStart ?: 0.0) + (step.targetEnd ?: 0.0)) / 2,
                    )
                    val fill = Path().apply {
                        moveTo(xPos(step.start), plotBottom)
                        lineTo(xPos(step.start), yPos(startTarget))
                        lineTo(xPos(step.end), yPos(endTarget))
                        lineTo(xPos(step.end), plotBottom)
                        close()
                    }
                    drawPath(fill, color = Palette.accent.copy(alpha = opacity.toFloat()))
                    val top = Path().apply {
                        moveTo(xPos(step.start), yPos(startTarget))
                        lineTo(xPos(step.end), yPos(endTarget))
                    }
                    drawPath(
                        top,
                        color = Palette.accent,
                        style = Stroke(width = 1.5.dp.toPx()),
                    )
                }

                // FTP across the chart, dashed, in watts mode only (in %FTP
                // mode the scale is already FTP-relative). The axis is built to
                // reach it, so there is no "is it inside the plot" question to
                // ask -- and asking it used to drop the line for every workout
                // below threshold, which is the whole reason to draw it.
                if (ftp != null && hasFtp) {
                    val ftpY = yPos(ftp)
                    drawLine(
                        color = Palette.muted,
                        start = Offset(plotLeft, ftpY),
                        end = Offset(plotRight, ftpY),
                        strokeWidth = 1f,
                        pathEffect = PathEffect.dashPathEffect(floatArrayOf(10f, 8f)),
                    )
                    drawText(
                        textLayoutResult = ftpLabelResult,
                        topLeft = Offset(plotRight - ftpLabelResult.size.width, ftpY - ftpLabelResult.size.height - 2f),
                    )
                }

                // x end labels: 0 and the profile's length in seconds.
                drawText(
                    textLayoutResult = zeroLabel,
                    topLeft = Offset(plotLeft, plotBottom + 2.dp.toPx()),
                )
                drawText(
                    textLayoutResult = lengthLabel,
                    topLeft = Offset(plotRight - lengthLabel.size.width, plotBottom + 2.dp.toPx()),
                )

                // Axis titles: y at the top-left, x along the bottom.
                drawText(textLayoutResult = yTitleResult, topLeft = Offset(2.dp.toPx(), 0f))
                drawText(
                    textLayoutResult = xTitleResult,
                    topLeft = Offset((plotLeft + plotRight) / 2f - xTitleResult.size.width / 2f, size.height - xTitleResult.size.height - 1f),
                )
            }
        }
    }
}

/** One completed ride: what it was, when it started, how long and how far. */
@Composable
private fun CalendarActivityRow(activity: JsonValue, onOpen: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(enabled = CalendarData.activityID(activity) != null, onClick = onOpen)
            .padding(vertical = 5.dp),
        verticalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        Text(
            text = CalendarJSON.title(activity, fallback = stringResource(R.string.calendar_recorded_ride)),
            style = TextStyle(fontSize = 14.sp, fontWeight = FontWeight.SemiBold, color = Palette.text),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            CalendarJSON.string(activity, "start_time")?.let {
                Text(
                    text = CalendarJSON.displayDateTime(it),
                    style = TextStyle(fontSize = 12.sp, color = Palette.muted),
                )
            }
            CalendarJSON.duration(activity)?.let {
                Text(text = it, style = TextStyle(fontSize = 12.sp, color = Palette.muted))
            }
            CalendarJSON.number(activity, "distance_m")?.let {
                Text(text = CalendarJSON.distanceString(it), style = TextStyle(fontSize = 12.sp, color = Palette.muted))
            }
        }
    }
}
