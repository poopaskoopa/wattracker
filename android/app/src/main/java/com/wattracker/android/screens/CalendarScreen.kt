package com.wattracker.android.screens

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import com.wattracker.android.R
import com.wattracker.android.shell.ScreenScaffold
import com.wattracker.android.shell.StubPanel

/**
 * Calendar (#198): the month grid of plan, rides and races.
 *
 * Stub for now -- #193 is the shell only; no network code in this step.
 */
@Composable
fun CalendarScreen() {
    ScreenScaffold(
        title = stringResource(R.string.destination_calendar),
        subtitle = stringResource(R.string.screen_calendar_subtitle),
    ) {
        StubPanel(
            note = stringResource(R.string.stub_calendar_note),
            issue = stringResource(R.string.stub_issue_calendar),
        )
    }
}
