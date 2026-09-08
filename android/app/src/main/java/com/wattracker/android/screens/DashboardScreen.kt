package com.wattracker.android.screens

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import com.wattracker.android.R
import com.wattracker.android.shell.ScreenScaffold
import com.wattracker.android.shell.StubPanel

/**
 * Dashboard (#196): training status, load chart, FTP, recent rides.
 *
 * Stub for now -- #193 is the shell only; no network code in this step.
 */
@Composable
fun DashboardScreen() {
    ScreenScaffold(
        title = stringResource(R.string.destination_dashboard),
        subtitle = stringResource(R.string.screen_dashboard_subtitle),
    ) {
        StubPanel(
            note = stringResource(R.string.stub_dashboard_note),
            issue = stringResource(R.string.stub_issue_dashboard),
        )
    }
}
