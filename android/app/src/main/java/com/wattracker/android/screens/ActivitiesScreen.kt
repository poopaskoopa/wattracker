package com.wattracker.android.screens

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import com.wattracker.android.R
import com.wattracker.android.shell.ScrollableScreenScaffold
import com.wattracker.android.shell.StubPanel

/**
 * Activities (#197): the ride list and ride detail.
 *
 * Stub for now -- #193 is the shell only; no network code in this step.
 */
@Composable
fun ActivitiesScreen() {
    ScrollableScreenScaffold(
        title = stringResource(R.string.destination_activities),
        subtitle = stringResource(R.string.screen_activities_subtitle),
    ) {
        StubPanel(
            note = stringResource(R.string.stub_activities_note),
            issue = stringResource(R.string.stub_issue_activities),
        )
    }
}
